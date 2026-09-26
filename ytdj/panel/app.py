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
from dataclasses import dataclass, replace
from typing import Any

from .art import ArtCache
from .client import Api, Commander, StatusFeed
from .hw import Screen, Touch, TouchEvent
from .netapp import NetController
from .netui import NetRenderer, NetView
from .stats import PanelStats, emit
from .ui import ART, ART_SIDE, STRINGS, TARGETS, QrRenderer, Renderer, View, merge_boxes, volume_at
from .wishapp import WishController
from .wishui import WishRenderer, WishView

log = logging.getLogger(__name__)

VOL_STEP = 5
KEY_VOL_STEP = 2  # na cvaknutí kolečka nebo stisk klávesy na repráku
VOL_INTERVAL = 0.25  # at most ~4 volume requests per second while dragging
# Sklo se občas pošle znovu, i když se nic nezměnilo (kdyby se rozešlo s tím,
# co si pamatujeme — rušení na sběrnici, cokoli). Dřív celý snímek každou
# minutu: 153 600 px naráz, přes 0,2 s točícího se jádra uprostřed hraní.
# Teď po pruzích: jeden pruh (32 řádků, 1/10 snímku) za REFRESH_EVERY, celé
# sklo se tak srovná jednou za 5 minut a žádná dávka nepřesáhne ~20 ms.
REFRESH_BAND = 32
REFRESH_EVERY = 30.0
# Po přepnutí obrazovky ťuknutím: další dotyk na stejném místě v téhle lhůtě
# míří ještě na starou obrazovku (dvojité ťuknutí, zákmit) — nesmí na nové
# nic spustit ("Hotovo" vpravo dole = na přehrávači hlasitost).
PAGE_GUARD = 0.5
GUARD_RADIUS = 60
# Klid: nic nehraje a nikdo nesahá REST_AFTER s → přehrávač ztlumí jas obrazu
# (podsvícení ovládat nejde). První dotyk jen probudí, nic nespustí.
REST_AFTER = 300.0
REST_LEVEL = 0.4
TOAST_TIME = 4.5  # s — "Petr si přeje: …" over the status strip
QR_CLOSE = 90.0  # s — the full-screen QR goes back to the player by itself
HOLD = 4.0  # how long an optimistic state wins over a server that disagrees
SKIP_HOLD = 6.0
NOTE_TIME = 3.0
TOUCH_SLOP = 14  # px a finger may wander off a button and still press it
TOUCH_GRAB = 4  # px of grace around a button for the initial touch
MIN_PRESS = 0.02  # s; shorter down→up is contact bounce, not a tap
REPEAT_GUARD = 0.35  # s between two play/next taps — bounce must not double-fire
# Držené −/+ hlasitosti opakuje krok: první opakování po REPEAT_DELAY (kratší
# stisk je obyčejné ťuknutí = jeden krok při puštění), pak po REPEAT_INTERVAL,
# po REPEAT_FAST_AFTER držení rychleji. Požadavky na server dál brzdí VOL_INTERVAL.
REPEAT_DELAY = 0.45
REPEAT_INTERVAL = 0.15
REPEAT_FAST_AFTER = 1.5
REPEAT_FAST_INTERVAL = 0.10
# Odporová vrstva občas na chvilku "pustí" (výpadek vzorků delší než debounce
# ovladače): nový dotyk na stejném tlačítku do té doby naváže na běžící opakování.
REPEAT_REGRIP = 0.15
VOL_BUTTONS = ("vol_up", "vol_down")
KEY_BURST_GAP = 1.0  # s — cvaknutí kolečka na repráku blíž u sebe jsou jedno otočení (jeden řádek logu)


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
        media_keys: bool = False,
        net_backend=None,
    ) -> None:
        self.screen = screen
        self.touch = touch
        self.media_keys = media_keys
        self.api = Api(url)
        self.vol_max = max(1, min(130, vol_max))
        self.s = STRINGS.get(lang, STRINGS["cs"])
        self.renderer = Renderer(lang)
        take = getattr(touch, "take_stats", None)
        self.stats = PanelStats(touch_stats=take if callable(take) else None)

        self.stop = threading.Event()
        self.events: queue.Queue[tuple] = queue.Queue()
        self.feed = StatusFeed(self.api, self._on_state, self._on_offline, self.stop)
        self.commander = Commander(self.api, self._on_result, self.stop)
        if net_backend is None:
            from .net import NmcliBackend

            net_backend = NmcliBackend()
        # the network screens; web_port is what a phone on the LAN should open
        self.net = NetController(net_backend, self.events.put, lang, self.api.port, count=self.stats.count)
        # the wish screens borrow the network screens' fonts — no second copy in RAM
        self.wish = WishController(
            self.api, self.events.put, lang, share=self.net.renderer, count=self.stats.count, stop=self.stop,
        )
        self.key_vol_at = -math.inf  # last volume change from the speaker's keys
        # cover art: fetched and decoded in its own thread, the player redraws when it's here
        self.art = ArtCache(ART_SIDE, lambda vid: self.events.put(("art", vid)), self.stop)
        self.renderer.art_source = self.art.get
        # the full-screen "wishes from a phone" QR page
        self.qr_renderer = QrRenderer(self.renderer.fonts, lang)
        self.qr_open = False
        self.qr_at = 0.0
        self._art_is_qr = False  # the art slot shows the QR code (tapping it opens the page)
        # a new wish from anyone: (request id, until) — the banner over the status strip
        self.toast: tuple[str, float] | None = None
        self._seen_reqs: set[str] | None = None

        # server state
        self.state: dict | None = None
        self.online = False
        self.ever_online = False
        self.track_key: Any = None
        self._running = False  # as the server last said
        self._loading = False  # running, but the stream hasn't started yet
        self._said_offline = False
        self.link_since = time.monotonic()  # od kdy platí online/offline (pro panel.link)
        self.server_version = ""  # otisk kódu ytdj ze stavu (panel.server_build)
        self.reconnects = 0
        self.offline_reason = ""
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
        self.prompting = False  # běží požadavek na DJ z tlačítka Hrát
        self.vol_sent_at = 0.0
        self.vol_sent: int | None = None
        self.vol_pending: int | None = None
        # auto-repeat of a held −/+ button
        self.repeat_at: float | None = None  # next step; None = not repeating
        self.repeat_steps = 0  # steps done by repeating in this gesture
        self.repeat_start = 0.0  # when the (possibly re-gripped) hold began
        self.repeat_lost: tuple[str, float, float] | None = None  # (button, released at, start)
        # pro provozní log: odkud gesto začalo a s jakou hlasitostí
        self.down_xy = (0, 0)
        self.gesture_vol0 = 0
        self.gesture_moves = 0
        self.gesture_regrip = False
        # otočení kolečka na repráku: [akce, cvaknutí, hlasitost předtím, naposledy]
        self.key_burst: list | None = None

        self._need_full = True
        self._band = 0  # the next refresh band (index)
        self._band_at = time.monotonic() + REFRESH_EVERY
        # touch guard after a page switch: until when, and around which point
        self._guard_until = 0.0
        self.page_guard = PAGE_GUARD
        self._guard_xy = (-1000, -1000)
        self._touch_xy = (0, 0)  # last reliable finger position (down / move)
        self._swallow = False  # this gesture woke the screen or hit the guard: ignore it to the end
        # rest (dimmed player)
        self.rest_after = REST_AFTER
        self._active_at = time.monotonic()
        self.resting = False
        self._dim_lut = [int(i * REST_LEVEL) for i in range(256)] * 3
        self._shown_page = "player"  # which screen the glass shows
        self._page_since = time.monotonic()
        self.frames = 0  # show() calls — for tests and stats

    def _overlay(self):
        """The screen drawn over the player (network or wish), or None."""
        if self.net.page:
            return self.net
        if self.wish.page:
            return self.wish
        return None

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
            except Exception as exc:
                log.exception("čtení dotyku selhalo")
                self.stats.error("touch_poll", exc)
                self.stop.wait(1.0)
                continue
            if ev is not None:
                # stamped on arrival: if the main thread is busy pushing a full
                # frame, a quick tap's down and up are handled back to back and
                # would otherwise look like contact bounce
                self.events.put(("touch", ev, time.monotonic()))

    # ---- main loop ----

    def run(self) -> None:
        self.feed.start()
        self.commander.start()
        if self.touch is not None:
            threading.Thread(target=self._touch_loop, name="panel-touch", daemon=True).start()
        if self.media_keys:
            from .keys import MediaKeys

            MediaKeys(lambda action: self.events.put(("key", action)), self.stop).start()
        self._paint()
        while not self.stop.is_set():
            try:
                self._step()
            except Exception as exc:
                # a bug in one update must not leave the panel dead on the wall
                log.exception("chyba v obsluze panelu")
                self.stats.error("main_loop", exc)
                self._need_full = True
                self.renderer.invalidate()
                self.net.renderer.invalidate()
                self.wish.invalidate()
                self.qr_renderer.invalidate()
                self.stop.wait(1.0)
        # poslední souhrny, ať se neztratí minuta před zastavením
        self._log_gesture()
        self._flush_key_burst()
        self.stats.flush()

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
        # volá se i ze signálu — tady nic nezapisovat (zámek telemetrie), to dělá run()
        self.stop.set()
        self.commander.wake()
        self.events.put(("wake",))

    def close_screen(self) -> None:
        """Last words on the glass, so a dead panel doesn't pose as a live one."""
        try:
            if self._shown_page != "player" or self.resting:
                # the glass shows another page (or the dimmed player): the whole player has to come back
                self.renderer.render(self._view(closed=True), full=True)
                self.screen.show(self.renderer.frame, None)
                return
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
                now = time.monotonic()
                emit(
                    "panel.link", state="offline", reason=str(msg[1])[:120],
                    online_ms=int((now - self.link_since) * 1000) if self.online else None,
                    mode=self.feed.mode,
                )
                self.link_since = now
            self.online = False
            self._end_gesture()
            # nothing we promised will happen now; show what's known
            self.hold_running = self.hold_volume = self.hold_skip = None
            self.vol_pending = None
        elif kind == "result":
            _, action, value, error = msg
            if action == "prompt":
                self.prompting = False
            if error:
                emit("panel.command_error", action=action, value=value, error=str(error)[:120])
                self.note = _Hold(self.s["failed"], time.monotonic() + NOTE_TIME)
                if action in ("play", "pause"):
                    self.hold_running = None
                elif action == "next":
                    self.hold_skip = None
                elif action == "volume" and not self._vol_gesture():
                    self.hold_volume = None
            elif action == "volume" and self.hold_volume and not self._vol_gesture():
                # let the server confirm; if it never does, fall back soon
                self.hold_volume.until = min(self.hold_volume.until, time.monotonic() + HOLD)
        elif kind == "touch":
            self._dispatch_touch(msg[1], msg[2] if len(msg) > 2 else time.monotonic())
        elif kind == "net":
            self.net.handle(msg)
        elif kind == "wish":
            if self.wish.handle(msg) and self._overlay() is None:
                # the answer to a wish from this panel: show it, like the web does
                self.wish.show_answer(time.monotonic())
        elif kind == "key":
            # tlačítka na repráku jdou stejnou cestou jako tlačítka na displeji
            log.info("klávesa: %s", msg[1])
            self._wake(time.monotonic())
            if not self.online:
                self.stats.count("key_offline")
                return
            now = time.monotonic()
            if msg[1] in ("vol_up", "vol_down"):
                # Kolečko na repráku cvaká rychle (~15× za vteřinu) — po
                # pětkách jako tlačítka na displeji by přeletělo celý rozsah
                # jedním otočením.
                cur = self._view().volume
                step = KEY_VOL_STEP if msg[1] == "vol_up" else -KEY_VOL_STEP
                new = max(0, min(self.vol_max, cur + step))
                burst = self.key_burst
                if burst is None or burst[0] != msg[1] or now - burst[3] > KEY_BURST_GAP:
                    self._flush_key_burst()
                    self.key_burst = burst = [msg[1], 0, cur, now]
                burst[1] += 1
                burst[3] = now
                if new != cur:
                    self._set_volume(new, now + HOLD)
                self.key_vol_at = now
            else:
                self._fire(msg[1], now, source="mediakey")

    def _page(self) -> str:
        if self.net.page:
            return self.net.page
        if self.wish.page:
            return f"wish:{self.wish.page}"
        return "qr" if self.qr_open else "player"

    def _dispatch_touch(self, ev: TouchEvent, at: float) -> None:
        if ev.kind in ("down", "move"):
            self._touch_xy = (ev.x, ev.y)
        if ev.kind == "down":
            self._active_at = max(self._active_at, at)
            if self.resting:
                # the first touch only wakes the screen — on a dimmed panel
                # nobody can see what they'd be pressing
                self._wake(at)
                self.stats.count("wake")
                self._swallow = True
                return
            gx, gy = self._guard_xy
            if at < self._guard_until and abs(ev.x - gx) <= GUARD_RADIUS and abs(ev.y - gy) <= GUARD_RADIUS:
                self.stats.count("page_guard")
                self._swallow = True
                return
            self._swallow = False
        elif self._swallow:
            if ev.kind == "up":
                self._swallow = False
            return
        page = self._page()
        overlay = self._overlay()
        if overlay is not None:
            overlay.touch(ev, at)
        elif page == "qr":
            self.qr_at = at
            if ev.kind == "up":  # a tap anywhere goes back
                self.qr_open = False
                self._log_action("qr_close", "touch", at)
        else:
            self._touch(ev, at)
        if ev.kind == "up" and self._page() != page:
            self._guard_until = at + self.page_guard
            self._guard_xy = self._touch_xy

    def _wake(self, now: float) -> None:
        self._active_at = max(self._active_at, now)
        if self.resting:
            self.resting = False
            self._need_full = True
            emit("panel.rest", state="awake")

    def _apply_state(self, state: dict, at: float) -> None:
        if not self.online:
            log.info("ytdj připojen")
            now = time.monotonic()
            if self.ever_online:
                self.reconnects += 1
            emit(
                "panel.link", state="online", mode=self.feed.mode, reconnects=self.reconnects,
                offline_ms=int((now - self.link_since) * 1000) if self._said_offline else None,
            )
            self.link_since = now
            self._said_offline = False
        self.online = self.ever_online = True
        self.state = state
        version = state.get("version")
        if isinstance(version, str) and version and version != self.server_version:
            if self.server_version:
                # ytdj byl nasazen znovu — ať je v logu vidět, s čím panel mluví
                log.info("ytdj má novou verzi: %s → %s", self.server_version, version)
            emit("panel.server_build", version=version, previous=self.server_version or None,
                 ui=state.get("build"))
            self.server_version = version
        if self.wish.on_state(state) and self._overlay() is None:
            # the DJ decided about a wish from this panel: show it, like the web does
            self.wish.show_answer(time.monotonic())
        self._notice_new_wishes(state)
        cur_ = state.get("current")
        queue_ = state.get("queue")
        if isinstance(cur_, dict):
            self.art.want(str(cur_.get("id") or ""))
        if isinstance(queue_, list) and queue_ and isinstance(queue_[0], dict):
            self.art.want(str(queue_[0].get("id") or ""))  # the "Pak:" track, before it's needed
        cur = state.get("current") or None
        key = (cur.get("id") or cur.get("title")) if isinstance(cur, dict) else None
        # při výpadku spojení nic nehraje, ať mpv tvrdí cokoli — hodiny stojí
        running = bool(state.get("playing")) and not bool(state.get("paused")) and not state.get("outage")
        server_pos = _num(state.get("position"))
        # Skladba je na řadě, ale proud ještě neteče — hodiny musí stát, jinak
        # by běžely vteřiny, které z repráku nezazněly.
        loading = running and bool(state.get("buffering"))

        predicted = self._position(at)
        if key != self.track_key or not running or loading or self._loading:
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
        self._loading = loading
        if key != self.track_key:
            self.track_key = key
            self.hold_skip = None

        if self.hold_running and self.hold_running.value == running:
            self.hold_running = None
        vol = state.get("volume")
        if (
            self.hold_volume
            and not self._vol_gesture()
            and self.vol_pending is None
            and isinstance(vol, (int, float))
            and int(vol) == self.hold_volume.value
        ):
            self.hold_volume = None

    def _notice_new_wishes(self, state: dict) -> None:
        """A wish nobody here has seen yet (from the web or wherever): the banner."""
        reqs = state.get("requests")
        if not isinstance(reqs, list):
            return
        ids = {str(r.get("id")) for r in reqs if isinstance(r, dict) and r.get("id")}
        first = self._seen_reqs is None
        new = [] if first else [
            r for r in reqs if isinstance(r, dict) and str(r.get("id")) not in self._seen_reqs
            and str(r.get("id")) not in self.wish.mine
            and r.get("state") in ("waiting", "thinking", "queued", "playing")
        ]
        self._seen_reqs = ids if first else (self._seen_reqs | ids)
        if new:
            newest = max(new, key=lambda r: r.get("created") or 0)
            now = time.monotonic()
            self.toast = (str(newest.get("id")), now + TOAST_TIME)
            self._wake(now)  # somebody wants something: no dimmed screen now
            emit("panel.toast", id=str(newest.get("id")), who=str(newest.get("who") or "")[:24])

    def _toast_view(self, now: float) -> tuple:
        if self.toast is None or now >= self.toast[1]:
            return ()
        r = next((x for x in self.wish.requests if str(x.get("id")) == self.toast[0]), None)
        if r is None:
            return ()
        st = r.get("state")
        if st in ("waiting", "thinking"):
            tail = self.s["toast_thinking"]
        elif st == "playing":
            tail = self.s["toast_playing"]
        elif st == "queued":
            tail = str(r.get("eta") or "")
        else:
            tail = str(r.get("state_cs") or "")
        return (str(r.get("who") or "?"), " ".join(str(r.get("text") or "").split()), tail)

    def _set_pos(self, pos: float, at: float) -> None:
        self.pos_base, self.pos_at = pos, at

    def _is_running(self) -> bool:
        return self.hold_running.value if self.hold_running else self._running

    def _position(self, now: float) -> float:
        if self.state and self._is_running() and not self._loading:
            return self.pos_base + (now - self.pos_at)
        return self.pos_base

    # ---- view ----

    def _view(self, closed: bool = False) -> View:
        now = time.monotonic()
        st = self.state or {}
        cur = st.get("current") if isinstance(st.get("current"), dict) else None
        running = bool(st.get("playing")) and not bool(st.get("paused")) and not st.get("outage")
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
        nxt = queue_[0] if isinstance(queue_, list) and queue_ and isinstance(queue_[0], dict) else {}
        nxt_req = nxt.get("req") if isinstance(nxt.get("req"), dict) else {}
        reason = (cur or {}).get("reason")
        now_who = str(reason.get("who") or "") if isinstance(reason, dict) and reason.get("kind") == "wish" else ""
        # wishes still to come besides the playing one and the one "Pak:" shows
        shown = {nxt_req.get("id")}
        if isinstance(reason, dict) and reason.get("kind") == "wish":
            shown.add(reason.get("id"))
        more = sum(1 for r in self.wish.requests
                   if r.get("state") in ("queued", "thinking", "waiting") and r.get("id") not in shown)
        outage = st.get("outage")
        track_id = str((cur or {}).get("id") or "")
        urls = self.net.urls()
        online = self.online
        return View(
            online=online,
            connecting=not self.ever_online,
            target=self.api.target,
            has_track=cur is not None,
            title=str((cur or {}).get("title") or ""),
            artist=str((cur or {}).get("artist") or ""),
            running=running and cur is not None,
            loading=self._loading and cur is not None,
            paused=bool(st.get("paused")),
            skipping=self.hold_skip is not None,
            elapsed=int(elapsed),
            duration=int(duration),
            volume=volume,
            vol_max=self.vol_max,
            mood=str(st.get("mood") or "").strip(),
            busy=bool(st.get("busy")),
            note=self.note.value if self.note else "",
            pressed=self.pressed if (self.inside and (online or self.pressed in ("net", "phone"))) else None,
            can_next=cur is not None or bool(queue_),
            closed=closed,
            net=self.net.icon(),
            next_title=str(nxt.get("title") or ""),
            next_artist=str(nxt.get("artist") or ""),
            next_who=str(nxt_req.get("who") or ""),
            now_who=now_who,
            wishes=self.wish.active_count(),
            outage=bool(outage),
            outage_reason=str(outage.get("reason") or "") if isinstance(outage, dict) else "",
            more_wishes=more,
            track_id=track_id,
            art_ready=self.art.get(track_id) is not None,
            qr_url=urls[0] if urls else "",
            rest=self.resting,
            toast=self._toast_view(now) if online and not closed else (),
            dj_offline=bool(st.get("dj_offline")),
        )

    def _paint(self) -> None:
        now = t0_mono = time.monotonic()
        page = self._page()
        renderer: Renderer | NetRenderer | WishRenderer
        view: View | NetView | WishView
        if page == "player":
            view = self._view()
            renderer, pressed = self.renderer, self.pressed
        elif page == "qr":
            view = self.net.urls()  # type: ignore[assignment]
            renderer, pressed = self.qr_renderer, None  # type: ignore[assignment]
        else:
            note = ""
            if now - self.key_vol_at < NOTE_TIME and self.online:
                note = self.net.s["volume"].format(v=self._view().volume)
            if self.net.page:
                view = self.net.view(now, note)
                renderer, pressed = self.net.renderer, self.net.pressed
            else:
                busy = bool((self.state or {}).get("busy")) and self.online
                view = self.wish.view(now, note, busy)
                renderer, pressed = self.wish.renderer, self.wish.pressed
        if page != self._shown_page:
            self._need_full = True  # another screen: one full frame
        if page != "player" and self.resting:
            self.resting = False
        if page == "player" and isinstance(view, View):
            if view.running:
                self._active_at = now  # music playing counts as activity
            rest = not self.pressed and now - self._active_at >= self.rest_after
            if rest != self.resting:
                self.resting = rest
                self._need_full = True
                emit("panel.rest", state="resting" if rest else "awake")
                view = replace(view, rest=rest)
        t0 = time.perf_counter()
        full = self._need_full
        boxes = renderer.render(view, full=full)
        if not full and not pressed and now >= self._band_at:
            # one band of the glass re-sent from the retained frame (see REFRESH_BAND)
            y = self._band * REFRESH_BAND
            boxes = merge_boxes(boxes + [(0, y, renderer.frame.width, min(y + REFRESH_BAND, renderer.frame.height))])
            self._band = (self._band + 1) % -(-renderer.frame.height // REFRESH_BAND)
            self._band_at = now + REFRESH_EVERY
        t1 = time.perf_counter()
        if not boxes:
            return
        img = renderer.frame
        if self.resting:
            img = renderer.frame.point(self._dim_lut)
            qr_box = self.renderer.qr_box
            if qr_box is not None:  # the code stays bright: a dimmed one scans badly
                img.paste(renderer.frame.crop(qr_box), qr_box[:2])
        if page == "player":
            self._art_is_qr = self.renderer.qr_box is not None
        show_ms: list[float] = []
        try:
            for box in boxes:
                ts = time.perf_counter()
                self.screen.show(img, None if full else box)
                show_ms.append((time.perf_counter() - ts) * 1000)
                self.frames += 1
            self._need_full = False
            if page != self._shown_page:
                done = time.monotonic()
                if self._guard_until > t0_mono:
                    # the switch took the glass a while: the guard counts from when it shows
                    self._guard_until = max(self._guard_until, done + self.page_guard / 2)
                emit(
                    "panel.screen", previous=self._shown_page, page=page,
                    dwell_ms=int((done - self._page_since) * 1000),
                )
                self._page_since = done
            self._shown_page = page
        except Exception as exc:
            # whatever made it to the glass is unknown now — start clean
            log.exception("zápis na displej selhal")
            self.stats.error("show", exc)
            self._need_full = True
            self.renderer.invalidate()
            self.net.renderer.invalidate()
            self.wish.invalidate()
            self.qr_renderer.invalidate()
            self.stop.wait(1.0)
            return
        t2 = time.perf_counter()
        px = 480 * 320 if full else sum((b[2] - b[0]) * (b[3] - b[1]) for b in boxes)
        self.stats.paint((t1 - t0) * 1000, show_ms, px, full)
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "kresba %.1f ms, displej %.1f ms, %d px v %s",
                (t1 - t0) * 1000, (t2 - t1) * 1000, px, boxes,
            )

    # ---- timing ----

    def _next_deadline(self) -> float:
        now = time.monotonic()
        deadlines = [now + 60.0, self._band_at, self.net.deadline(now), self.wish.deadline(now)]
        if not self.resting:
            deadlines.append(self._active_at + self.rest_after + 0.01)
        if self.toast is not None:
            deadlines.append(self.toast[1])
        if self.qr_open:
            deadlines.append(self.qr_at + QR_CLOSE)
        if now - self.key_vol_at < NOTE_TIME:
            deadlines.append(self.key_vol_at + NOTE_TIME)  # the volume toast on net pages
        for hold in (self.hold_running, self.hold_volume, self.hold_skip, self.note):
            if hold and hold.until != math.inf:
                deadlines.append(hold.until)
        if self.vol_pending is not None:
            deadlines.append(self.vol_sent_at + VOL_INTERVAL)
        if self.repeat_at is not None:
            deadlines.append(self.repeat_at)
        if self.key_burst is not None:
            deadlines.append(self.key_burst[3] + KEY_BURST_GAP)
        flush_at = self.stats.deadline()
        if flush_at is not None:
            deadlines.append(flush_at)
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
        self._repeat(now)
        if self.toast is not None and now >= self.toast[1]:
            self.toast = None
        if self.qr_open and now - self.qr_at >= QR_CLOSE:
            self.qr_open = False
        if self.vol_pending is not None and now - self.vol_sent_at >= VOL_INTERVAL:
            self._send_volume(self.vol_pending)
        self.net.timers(now)
        self.wish.timers(now)
        if self.key_burst is not None and now - self.key_burst[3] >= KEY_BURST_GAP:
            self._flush_key_burst()
        self.stats.maybe_flush(now)

    def _flush_key_burst(self) -> None:
        """Jedno otočení kolečka (klávesy na repráku) = jeden řádek logu."""
        burst, self.key_burst = self.key_burst, None
        if burst is None:
            return
        emit(
            "panel.action", button=burst[0], source="mediakey", steps=burst[1],
            vol_from=burst[2], vol_to=self._view().volume,
        )

    def _log_action(self, button: str, source: str, now: float, **extra: Any) -> None:
        """panel.action — co se stisklo, kde a jak dlouho (dotyk) nebo odkud."""
        if source == "touch":
            extra.setdefault("x", self.down_xy[0])
            extra.setdefault("y", self.down_xy[1])
            extra.setdefault("press_ms", int((now - self.press_at) * 1000))
        emit("panel.action", button=button, source=source, **extra)

    # ---- touch ----

    def _hit(self, x: int, y: int, slop: int) -> str | None:
        for name, (l, t, r, b) in TARGETS.items():
            if l - slop <= x < r + slop and t - slop <= y < b + slop:
                return name
        l, t, r, b = ART
        if self._art_is_qr and l <= x < r and t <= y < b:
            return "phone"  # the QR code in the art slot opens the big one
        return None

    def _vol_gesture(self) -> bool:
        """A finger is setting the volume right now (drag or held −/+)."""
        return self.pressed == "vol" or (self.pressed in VOL_BUTTONS and self.repeat_steps > 0)

    def _end_gesture(self) -> None:
        if self._vol_gesture():
            self._finish_volume()
        self._log_gesture()
        self.pressed = None
        self.inside = False
        self.repeat_at = None
        self.repeat_steps = 0

    def _log_gesture(self) -> None:
        """Konec tažení po liště nebo podrženého −/+ → jeden řádek logu."""
        name = self.pressed
        now = time.monotonic()
        if name == "vol":
            emit(
                "panel.volume_drag", x=self.down_xy[0], y=self.down_xy[1],
                x_end=self.last_xy[0], moves=self.gesture_moves,
                vol_from=self.gesture_vol0, vol_to=self._view().volume,
                press_ms=int((now - self.press_at) * 1000),
            )
        elif name in VOL_BUTTONS and self.repeat_steps:
            self._log_action(
                name, "touch", now, hold=True, steps=self.repeat_steps,
                vol_from=self.gesture_vol0, vol_to=self._view().volume,
                regrip=self.gesture_regrip or None, slid_out=not self.inside or None,
            )

    def _repeat(self, now: float) -> None:
        """Step the volume while −/+ is held (touch events stop while a finger rests)."""
        if self.repeat_at is None or now < self.repeat_at:
            return
        name = self.pressed
        if name not in VOL_BUTTONS or not self.inside or not self.online or self._overlay() is not None:
            self.repeat_at = None
            return
        if self._vol_step(name, math.inf) is None:
            self.repeat_at = None  # at the limit — nothing more to do until release
            if self.repeat_steps == 0:
                self.repeat_steps = 1  # the press did its job; release must not step
            return
        self.repeat_steps += 1
        fast = now - self.repeat_start >= REPEAT_FAST_AFTER
        # from the schedule, not from now, so a late wake-up doesn't slow the pace
        nxt = self.repeat_at + (REPEAT_FAST_INTERVAL if fast else REPEAT_INTERVAL)
        self.repeat_at = max(nxt, now + 0.02)

    def _touch(self, ev: TouchEvent, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if ev.kind == "down":
            if self.pressed:  # lost an "up" somewhere — start over
                self.stats.count("lost_up")
                self._end_gesture()
            name = self._hit(ev.x, ev.y, TOUCH_GRAB)
            # the network button works with ytdj down too — that's when it's needed most
            if name is None:
                self.stats.count("miss")
                return
            if not self.online and name not in ("net", "phone"):
                self.stats.count("offline")
                return
            if name == "vol" and ev.x < TARGETS["vol"][0] + 50:
                # the number, not the bar: a touch there would mean "mute",
                # which is never what a finger resting next to the bar wants
                self.stats.count("vol_number")
                return
            self.pressed, self.press_at, self.inside = name, now, True
            self.last_xy = self.down_xy = (ev.x, ev.y)
            self.gesture_vol0 = self._view().volume
            self.gesture_moves = 0
            self.gesture_regrip = False
            log.info("dotyk: %s na %d,%d", name, ev.x, ev.y)
            if name == "vol":
                self._drag_volume(ev.x)
            elif name in VOL_BUTTONS:
                lost, self.repeat_lost = self.repeat_lost, None
                if lost and lost[0] == name and now - lost[1] <= REPEAT_REGRIP:
                    # a glitch in the middle of a hold: carry on repeating
                    self.stats.count("regrip")
                    self.gesture_regrip = True
                    self.repeat_steps, self.repeat_start = 1, lost[2]
                    if self.hold_volume:
                        self.hold_volume.until = math.inf
                    self.repeat_at = now + REPEAT_INTERVAL
                else:
                    self.repeat_steps, self.repeat_start = 0, now
                    self.repeat_at = now + REPEAT_DELAY
        elif ev.kind == "move":
            if not self.pressed:
                return
            self.last_xy = (ev.x, ev.y)
            self.gesture_moves += 1
            if self.pressed == "vol":
                self._drag_volume(ev.x)
            else:
                self.inside = self._hit(ev.x, ev.y, TOUCH_SLOP) == self.pressed
                if not self.inside and self.repeat_at is not None:
                    self.repeat_at = None  # slid off −/+: stop stepping for good
                    if self.repeat_steps:
                        self._finish_volume()
        elif ev.kind == "up":
            name = self.pressed
            if not name:
                return
            if name == "vol":
                self._end_gesture()
                return
            if name in VOL_BUTTONS and self.repeat_steps:
                # held and repeated: the steps are done, only the final value goes out
                if self.inside and self.repeat_at is not None:
                    self.repeat_lost = (name, now, self.repeat_start)
                self._end_gesture()
                return
            self.repeat_at = None
            # resistive controllers often report garbage coordinates on
            # release; the last down/move position is the trustworthy one
            x, y = self.last_xy
            inside = self._hit(x, y, TOUCH_SLOP) == name
            long_enough = now - self.press_at >= MIN_PRESS
            self.pressed, self.inside = None, False
            if not inside:
                self.stats.count("slid_out")
            elif not long_enough:
                self.stats.count("too_short")
            elif name == "net":
                log.info("dotyk: síť")
                self._log_action("net", "touch", now)
                self.net.open(now)
            elif name == "wish" and self.online:
                log.info("dotyk: přání")
                self._log_action("wish", "touch", now)
                self.wish.open(now)
            elif name == "phone":
                log.info("dotyk: přání z mobilu (QR)")
                self._log_action("phone", "touch", now)
                self.qr_open, self.qr_at = True, now
            elif name == "queue" and self.online:
                log.info("dotyk: fronta přání")
                self._log_action("queue", "touch", now)
                self.wish.open_queue(now)
            elif self.online:
                self._fire(name, now)
            else:
                self.stats.count("offline")

    def _fire(self, name: str, now: float, source: str = "touch") -> None:
        if name in ("play", "next"):
            if now - self.last_fire.get(name, 0.0) < REPEAT_GUARD:
                self.stats.count("debounce" if source == "touch" else "key_debounce")
                return
            self.last_fire[name] = now
        view = self._view()
        if name == "play" and not (self.state or {}).get("current"):
            started = self._start_dj()
            self._log_action("play", source, now, did="start_dj" if started else "dj_busy")
            return
        if name == "play":
            want = not view.running
            self._log_action("play", source, now, did="play" if want else "pause")
            # freeze (or restart) the clock where it is right now
            self._set_pos(self._position(now), now)
            self.hold_running = _Hold(want, now + HOLD)
            log.info("povel: %s", "hrát" if want else "pauza")
            self.commander.send("play" if want else "pause")
        elif name == "next":
            self.hold_skip = _Hold(self.track_key, now + SKIP_HOLD)
            self._log_action("next", source, now, did="next")
            log.info("povel: další")
            self.commander.send("next")
        elif name in VOL_BUTTONS:
            before = view.volume
            new = self._vol_step(name, now + HOLD)
            self._log_action(name, source, now, hold=False, steps=1, vol_from=before,
                             vol_to=before if new is None else new)

    def _vol_step(self, name: str, until: float) -> int | None:
        """One −/+ step (snapped to VOL_STEP); the new volume, or None at the limit."""
        cur = self._view().volume
        if name == "vol_up":
            new = cur if cur >= self.vol_max else min(self.vol_max, (cur // VOL_STEP + 1) * VOL_STEP)
        else:
            new = max(0, ((cur + VOL_STEP - 1) // VOL_STEP - 1) * VOL_STEP)
        if new == cur:
            return None
        self._set_volume(new, until)
        return new

    def _start_dj(self) -> bool:
        """Nic nehraje (třeba po restartu) — "Hrát" rozjede DJ podle situace.

        Server pozná, že není co odpauzovat, a spustí chytrý rozjezd (čas,
        den, kancelář, historie — ytdj.agent.context) jako tah DJe, ne jako
        přání. Panel jen pošle "play", jako web.
        """
        st = self.state or {}
        if st.get("starting") or (st.get("busy") and not st.get("requests")):
            return False
        log.info("povel: rozjet DJ")
        self.commander.send("play")
        return True

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
        log.info("hlasitost → %d", value)
        self.commander.volume(value)

    def _server_volume(self) -> int | None:
        v = (self.state or {}).get("volume")
        return int(v) if isinstance(v, (int, float)) else None


def _num(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if math.isfinite(value) else 0.0
