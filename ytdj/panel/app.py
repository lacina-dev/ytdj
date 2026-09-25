"""The panel's event loop: server state + touches in, dirty rectangles out.

Threads: `StatusFeed` (SSE/polling), `Commander` (POSTs) and one that sits in
`Touch.poll()`. They all drop messages into one queue; the main thread is the
only one that touches the frame or calls `Screen.show()`. It sleeps on the
queue until something arrives or the next deadline (the clock ticking over
to the next second, a throttled volume send, an optimistic state expiring) —
there is no fixed-rate loop, so a paused player costs no CPU at all.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any

from .client import Api, Commander, StatusFeed
from .hw import Screen, Touch, TouchEvent
from .ui import STRINGS, TARGETS, Renderer, View, volume_at

log = logging.getLogger(__name__)

VOL_STEP = 5
VOL_INTERVAL = 0.25  # at most ~4 volume requests per second while dragging
HOLD = 4.0  # how long an optimistic state wins over a server that disagrees
SKIP_HOLD = 6.0
NOTE_TIME = 3.0
TOUCH_SLOP = 14  # px a finger may wander off a button and still press it
TOUCH_GRAB = 4  # px of grace around a button for the initial touch
MIN_PRESS = 0.02  # s; shorter down→up is contact bounce, not a tap
REPEAT_GUARD = 0.35  # s between two play/next taps — bounce must not double-fire


@dataclass
class _Hold:
    value: Any
    until: float  # monotonic; math.inf while a gesture is still in progress


class PanelApp:
    def __init__(
        self,
        screen: Screen,
        touch: Touch | None,
        url: str,
        lang: str = "cs",
        vol_max: int = 100,
    ) -> None:
        self.screen = screen
        self.touch = touch
        self.api = Api(url)
        self.vol_max = max(1, min(130, vol_max))
        self.s = STRINGS.get(lang, STRINGS["cs"])
        self.renderer = Renderer(lang)

        self.stop = threading.Event()
        self.events: queue.Queue[tuple] = queue.Queue()
        self.feed = StatusFeed(self.api, self._on_state, self._on_offline, self.stop)
        self.commander = Commander(self.api, self._on_result, self.stop)

        # server state
        self.state: dict | None = None
        self.online = False
        self.ever_online = False
        self.track_key: Any = None
        self._running = False  # as the server last said
        self._said_offline = False
        self.pos_base = 0.0
        self.pos_at = 0.0

        # optimistic overrides
        self.hold_running: _Hold | None = None
        self.hold_volume: _Hold | None = None
        self.hold_skip: _Hold | None = None  # value = track key we're leaving
        self.note: _Hold | None = None

        # touch gesture
        self.pressed: str | None = None
        self.press_at = 0.0
        self.last_xy = (0, 0)
        self.inside = False
        self.last_fire: dict[str, float] = {}
        self.vol_sent_at = 0.0
        self.vol_sent: int | None = None
        self.vol_pending: int | None = None

        self._need_full = True
        self.frames = 0  # show() calls — for tests and stats

    # ---- threads → queue ----

    def _on_state(self, state: dict) -> None:
        self.events.put(("state", state, time.monotonic()))

    def _on_offline(self, reason: str) -> None:
        self.events.put(("offline", reason))

    def _on_result(self, action: str, value: int | None, error: str | None) -> None:
        self.events.put(("result", action, value, error))

    def _touch_loop(self) -> None:
        assert self.touch is not None
        while not self.stop.is_set():
            try:
                ev = self.touch.poll(0.5)
            except Exception:
                log.exception("čtení dotyku selhalo")
                self.stop.wait(1.0)
                continue
            if ev is not None:
                self.events.put(("touch", ev))

    # ---- main loop ----

    def run(self) -> None:
        self.feed.start()
        self.commander.start()
        if self.touch is not None:
            threading.Thread(target=self._touch_loop, name="panel-touch", daemon=True).start()
        self._paint()
        while not self.stop.is_set():
            try:
                self._step()
            except Exception:
                # a bug in one update must not leave the panel dead on the wall
                log.exception("chyba v obsluze panelu")
                self._need_full = True
                self.renderer.invalidate()
                self.stop.wait(1.0)

    def _step(self) -> None:
        timeout = self._next_deadline() - time.monotonic()
        try:
            msg = self.events.get(timeout=max(0.0, min(timeout, 60.0)))
        except queue.Empty:
            pass
        else:
            self._handle(msg)
            # drain whatever piled up during a slow show() before drawing
            while True:
                try:
                    self._handle(self.events.get_nowait())
                except queue.Empty:
                    break
        self._timers()
        self._paint()

    def shutdown(self) -> None:
        self.stop.set()
        self.commander.wake()
        self.events.put(("wake",))

    def close_screen(self) -> None:
        """Last words on the glass, so a dead panel doesn't pose as a live one."""
        try:
            boxes = self.renderer.render(self._view(closed=True))
            for box in boxes:
                self.screen.show(self.renderer.frame, box)
        except Exception:
            log.debug("závěrečné překreslení selhalo", exc_info=True)

    def _handle(self, msg: tuple) -> None:
        kind = msg[0]
        if kind == "state":
            self._apply_state(msg[1], msg[2])
        elif kind == "offline":
            if self.online or not self._said_offline:  # once, not every retry
                log.info("ytdj nedostupný: %s", msg[1])
                self._said_offline = True
            self.online = False
            self._end_gesture()
            # nothing we promised will happen now; show what's known
            self.hold_running = self.hold_volume = self.hold_skip = None
            self.vol_pending = None
        elif kind == "result":
            _, action, value, error = msg
            if error:
                self.note = _Hold(self.s["failed"], time.monotonic() + NOTE_TIME)
                if action in ("play", "pause"):
                    self.hold_running = None
                elif action == "next":
                    self.hold_skip = None
                elif action == "volume" and self.pressed != "vol":
                    self.hold_volume = None
            elif action == "volume" and self.hold_volume and self.pressed != "vol":
                # let the server confirm; if it never does, fall back soon
                self.hold_volume.until = min(self.hold_volume.until, time.monotonic() + HOLD)
        elif kind == "touch":
            self._touch(msg[1])

    def _apply_state(self, state: dict, at: float) -> None:
        if not self.online:
            log.info("ytdj připojen")
        self.online = self.ever_online = True
        self.state = state
        cur = state.get("current") or None
        key = (cur.get("id") or cur.get("title")) if isinstance(cur, dict) else None
        running = bool(state.get("playing")) and not bool(state.get("paused"))
        server_pos = _num(state.get("position"))

        predicted = self._position(at)
        if key != self.track_key or not running:
            self._set_pos(server_pos, at)
        else:
            # like the web UI: jump on a big difference, nudge on a small
            # one, so the seconds don't hop back and forth between updates
            diff = server_pos - predicted
            if abs(diff) > 1.5:
                self._set_pos(server_pos, at)
            else:
                self._set_pos(max(predicted - 0.15, predicted + diff * 0.25), at)
        self._running = running
        if key != self.track_key:
            self.track_key = key
            self.hold_skip = None

        if self.hold_running and self.hold_running.value == running:
            self.hold_running = None
        vol = state.get("volume")
        if (
            self.hold_volume
            and self.pressed != "vol"
            and self.vol_pending is None
            and isinstance(vol, (int, float))
            and int(vol) == self.hold_volume.value
        ):
            self.hold_volume = None

    def _set_pos(self, pos: float, at: float) -> None:
        self.pos_base, self.pos_at = pos, at

    def _is_running(self) -> bool:
        return self.hold_running.value if self.hold_running else self._running

    def _position(self, now: float) -> float:
        if self.state and self._is_running():
            return self.pos_base + (now - self.pos_at)
        return self.pos_base

    # ---- view ----

    def _view(self, closed: bool = False) -> View:
        now = time.monotonic()
        st = self.state or {}
        cur = st.get("current") if isinstance(st.get("current"), dict) else None
        running = bool(st.get("playing")) and not bool(st.get("paused"))
        if self.hold_running:
            running = self.hold_running.value
        volume = st.get("volume")
        volume = int(volume) if isinstance(volume, (int, float)) and not isinstance(volume, bool) else 0
        if self.hold_volume:
            volume = self.hold_volume.value

        duration = _num(st.get("duration")) or _num((cur or {}).get("duration"))
        elapsed = self._position(now)
        if duration > 0:
            elapsed = min(elapsed, duration)
        queue_ = st.get("queue")
        online = self.online
        return View(
            online=online,
            connecting=not self.ever_online,
            target=self.api.target,
            has_track=cur is not None,
            title=str((cur or {}).get("title") or ""),
            artist=str((cur or {}).get("artist") or ""),
            running=running and cur is not None,
            paused=bool(st.get("paused")),
            skipping=self.hold_skip is not None,
            elapsed=int(elapsed),
            duration=int(duration),
            volume=volume,
            vol_max=self.vol_max,
            mood=str(st.get("mood") or "").strip(),
            busy=bool(st.get("busy")),
            note=self.note.value if self.note else "",
            pressed=self.pressed if (self.inside and online) else None,
            can_next=cur is not None or bool(queue_),
            closed=closed,
        )

    def _paint(self) -> None:
        view = self._view()
        t0 = time.perf_counter()
        full = self._need_full
        boxes = self.renderer.render(view, full=full)
        t1 = time.perf_counter()
        if not boxes:
            return
        try:
            for box in boxes:
                self.screen.show(self.renderer.frame, None if full else box)
                self.frames += 1
            self._need_full = False
        except Exception:
            # whatever made it to the glass is unknown now — start clean
            log.exception("zápis na displej selhal")
            self._need_full = True
            self.renderer.invalidate()
            self.stop.wait(1.0)
            return
        t2 = time.perf_counter()
        if log.isEnabledFor(logging.DEBUG):
            px = sum((b[2] - b[0]) * (b[3] - b[1]) for b in boxes)
            log.debug(
                "kresba %.1f ms, displej %.1f ms, %d px v %s",
                (t1 - t0) * 1000, (t2 - t1) * 1000, px, boxes,
            )

    # ---- timing ----

    def _next_deadline(self) -> float:
        now = time.monotonic()
        deadlines = [now + 60.0]
        for hold in (self.hold_running, self.hold_volume, self.hold_skip, self.note):
            if hold and hold.until != math.inf:
                deadlines.append(hold.until)
        if self.vol_pending is not None:
            deadlines.append(self.vol_sent_at + VOL_INTERVAL)
        if self.online and self._is_running() and self.state and self.state.get("current"):
            # wake exactly when the displayed second changes
            pos = self._position(now)
            deadlines.append(now + (math.floor(pos) + 1 - pos) + 0.005)
        return min(deadlines)

    def _timers(self) -> None:
        now = time.monotonic()
        for name in ("hold_running", "hold_volume", "hold_skip", "note"):
            hold = getattr(self, name)
            if hold and now >= hold.until:
                setattr(self, name, None)
        if self.vol_pending is not None and now - self.vol_sent_at >= VOL_INTERVAL:
            self._send_volume(self.vol_pending)

    # ---- touch ----

    def _hit(self, x: int, y: int, slop: int) -> str | None:
        for name, (l, t, r, b) in TARGETS.items():
            if l - slop <= x < r + slop and t - slop <= y < b + slop:
                return name
        return None

    def _end_gesture(self) -> None:
        if self.pressed == "vol":
            self._finish_volume()
        self.pressed = None
        self.inside = False

    def _touch(self, ev: TouchEvent) -> None:
        now = time.monotonic()
        if ev.kind == "down":
            if self.pressed:  # lost an "up" somewhere — start over
                self._end_gesture()
            if not self.online:
                return
            name = self._hit(ev.x, ev.y, TOUCH_GRAB)
            if name is None:
                return
            if name == "vol" and ev.x < TARGETS["vol"][0] + 50:
                # the number, not the bar: a touch there would mean "mute",
                # which is never what a finger resting next to the bar wants
                return
            self.pressed, self.press_at, self.inside = name, now, True
            self.last_xy = (ev.x, ev.y)
            if name == "vol":
                self._drag_volume(ev.x)
        elif ev.kind == "move":
            if not self.pressed:
                return
            self.last_xy = (ev.x, ev.y)
            if self.pressed == "vol":
                self._drag_volume(ev.x)
            else:
                self.inside = self._hit(ev.x, ev.y, TOUCH_SLOP) == self.pressed
        elif ev.kind == "up":
            name = self.pressed
            if not name:
                return
            if name == "vol":
                self._end_gesture()
                return
            # resistive controllers often report garbage coordinates on
            # release; the last down/move position is the trustworthy one
            x, y = self.last_xy
            inside = self._hit(x, y, TOUCH_SLOP) == name
            long_enough = now - self.press_at >= MIN_PRESS
            self.pressed, self.inside = None, False
            if inside and long_enough and self.online:
                self._fire(name, now)

    def _fire(self, name: str, now: float) -> None:
        if name in ("play", "next"):
            if now - self.last_fire.get(name, 0.0) < REPEAT_GUARD:
                return
            self.last_fire[name] = now
        view = self._view()
        if name == "play":
            want = not view.running
            # freeze (or restart) the clock where it is right now
            self._set_pos(self._position(now), now)
            self.hold_running = _Hold(want, now + HOLD)
            self.commander.send("play" if want else "pause")
        elif name == "next":
            self.hold_skip = _Hold(self.track_key, now + SKIP_HOLD)
            self.commander.send("next")
        elif name in ("vol_up", "vol_down"):
            cur = view.volume
            if name == "vol_up":
                new = cur if cur >= self.vol_max else min(self.vol_max, (cur // VOL_STEP + 1) * VOL_STEP)
            else:
                new = max(0, ((cur + VOL_STEP - 1) // VOL_STEP - 1) * VOL_STEP)
            if new != cur:
                self._set_volume(new, now + HOLD)

    # ---- volume ----

    def _drag_volume(self, x: int) -> None:
        self._set_volume(volume_at(x, self.vol_max), math.inf)

    def _finish_volume(self) -> None:
        if self.hold_volume is None:
            return
        # the final value goes out now, throttle or not
        if self.vol_pending is not None or self.vol_sent != self.hold_volume.value:
            self._send_volume(self.hold_volume.value)
        self.hold_volume.until = time.monotonic() + HOLD

    def _set_volume(self, value: int, until: float) -> None:
        self.hold_volume = _Hold(value, until)
        if time.monotonic() - self.vol_sent_at >= VOL_INTERVAL:
            self._send_volume(value)
        else:
            self.vol_pending = value

    def _send_volume(self, value: int) -> None:
        self.vol_pending = None
        self.vol_sent_at = time.monotonic()
        if value == self.vol_sent and self._server_volume() == value:
            return
        self.vol_sent = value
        self.commander.volume(value)

    def _server_volume(self) -> int | None:
        v = (self.state or {}).get("volume")
        return int(v) if isinstance(v, (int, float)) else None


def _num(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if math.isfinite(value) else 0.0
