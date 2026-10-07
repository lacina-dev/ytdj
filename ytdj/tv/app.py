"""The TV screen's main loop: follow the jukebox, redraw only what changed.

One thread follows the jukebox's status stream (the same client the touch
panel uses), one fetches covers; the main thread sleeps until something
changes or until the next tick (progress every few seconds while music plays,
otherwise once a minute for the clock). No screen to draw on — no TV, no
framebuffer, an unknown pixel format — is not an error: the process idles and
looks again once a minute.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from typing import Callable

from ..panel.art import ArtCache
from ..panel.client import Api, StatusFeed
from ..panel.stats import emit
from ..panel.webaddr import Watch
from .screen import PROGRESS_STEP, Renderer, TvView, cover_tile, view_from
from .cec import TvPower, wish_from
from .video import Director, want_from

log = logging.getLogger(__name__)

# a bigger thumbnail than the panel's: 480×360, the cover is its centre square
ART_URL = "http://i.ytimg.com/vi/{id}/hqdefault.jpg"
RETRY_SCREEN = 60.0  # s between looks for a usable framebuffer
MODE_CHECK = 30.0  # s between checks that the resolution hasn't changed
REPORT_EVERY = 600.0  # s between tv.paint summaries
SEEK_JUMP = 4.0  # s — a position this far from where we thought it was is a seek


class TvApp:
    def __init__(self, open_screen: Callable[[], object], url: str, address: str = "",
                 art_url: str = ART_URL, agent: str = "ytdj-tv",
                 watch: Watch | None = None, director: Director | None = None,
                 web_name: str = "", power: TvPower | None = None) -> None:
        # vypínání telky přes HDMI-CEC, když se nehraje (cec.TvPower) — jen na
        # skutečné telce; bez něj se telky nikdo nedotkne
        self.power = power
        self.open_screen = open_screen
        # klip místo obrazovky (video.Director) — jen na skutečné telce; bez
        # něj se nic nemění a kreslí se pořád
        self.director = director
        self._video = False
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.api = Api(url, agent=agent)
        # Adresa na obrazovce: pevná jen když ji někdo výslovně zadal, jinak
        # ta, která při posledním ověření opravdu odpověděla (webaddr.Watch —
        # vlastní vlákno, jednou za pár minut).
        self.address = address
        self.watch = watch
        if not address and watch is None:
            self.watch = Watch(self.api.port, self.stop, on_change=lambda _r: self.wake.set(),
                               name=web_name)
        self.feed = StatusFeed(self.api, self._on_state, self._on_offline, self.stop)
        self.art_url = art_url
        self.art: ArtCache | None = None
        self.screen = None
        self.renderer: Renderer | None = None
        self._lock = threading.Lock()
        self._state: dict | None = None
        self._got_at = 0.0
        self._offline = "spojuji se…"
        self._idle_since: float | None = time.monotonic()
        self._no_screen = ""
        self.paints = self.pushed_px = 0
        self.paint_ms = 0.0
        self.reconnects = 0

    # ---- feed thread ----

    def _on_state(self, state: dict) -> None:
        now = time.monotonic()
        with self._lock:
            old, was_offline = self._state, self._offline
            self._state, self._offline = state, ""
            changed = bool(was_offline) or old is None or self._differs(old, state, now)
            self._got_at = now
            playing = isinstance(state.get("current"), dict) and not state.get("paused")
            if playing:
                self._idle_since = None
            elif self._idle_since is None:
                self._idle_since = now
        if changed:
            self.wake.set()  # position-only updates are picked up by the next tick

    def _differs(self, old: dict, new: dict, now: float) -> bool:
        for key in new.keys() | old.keys():
            if key not in ("position", "history", "volume", "people") \
                    and old.get(key) != new.get(key):
                return True
        try:
            expected = float(old.get("position") or 0.0)
            if isinstance(old.get("current"), dict) and not old.get("paused") \
                    and not old.get("buffering"):
                expected += now - self._got_at
            return abs(float(new.get("position") or 0.0) - expected) > SEEK_JUMP
        except (TypeError, ValueError):
            return False

    def _on_offline(self, reason: str) -> None:
        with self._lock:
            if not self._offline:
                self.reconnects += 1
                emit("tv.offline", reason=reason[:120])
                if self._idle_since is None:
                    self._idle_since = time.monotonic()
            changed = self._offline != reason
            self._offline = reason or "nedostupný"
        if changed:
            self.wake.set()

    def _art_ready(self, _vid: str) -> None:
        self.wake.set()

    # ---- main thread ----

    def view(self) -> TvView:
        with self._lock:
            state, offline, got_at, idle = self._state, self._offline, self._got_at, self._idle_since
        art = self.art
        reach = self.watch.reach if self.watch else None
        address = self.address or (reach.address() if reach else "")
        # vedle jména i holá adresa (a ta jde do QR kódu): jména .local stojí
        # na multicastu, který kancelářská Wi-Fi umí ztrácet
        address_ip = reach.ip_address() if reach and not self.address else ""
        return view_from(state, offline=offline, got_at=got_at, address=address,
                         address_ip=address_ip,
                         idle_since=idle,
                         art_ready=(lambda vid: art.get(vid) is not None) if art else None)

    def _ensure_screen(self) -> bool:
        if self.screen is not None:
            return True
        try:
            self.screen = self.open_screen()
        except Exception as exc:  # FbError and anything a strange device throws
            why = str(exc) or type(exc).__name__
            if why != self._no_screen:
                log.info("obrazovka není k použití (%s) — zkusím to za minutu", why)
                emit("tv.no_screen", reason=why[:160])
                self._no_screen = why
            return False
        size = tuple(self.screen.size)
        self.art = ArtCache(1, self._art_ready, self.stop, url=self.art_url, tile=cover_tile)
        self.renderer = Renderer(size, art=self.art.get)
        self.art.side = self.renderer.art_side
        self._no_screen = ""
        log.info("kreslím na %s×%s", *size)
        emit("tv.screen", size=list(size), fmt=getattr(self.screen, "fmt", "png"),
             art_side=self.renderer.art_side)
        return True

    def _drop_screen(self) -> None:
        if self.screen is not None:
            try:
                self.screen.close()
            except Exception:
                log.debug("zavření obrazovky selhalo", exc_info=True)
        self.screen = self.renderer = self.art = None

    def paint(self, full: bool = False) -> int:
        """One pass: redraw what changed and push it; returns pixels pushed."""
        assert self.renderer is not None and self.screen is not None
        t0 = time.perf_counter()
        v = self.view()
        if v.vid and self.art is not None:
            self.art.get(v.vid)  # asks for it when it isn't there yet
        boxes = self.renderer.render(v, full=full)
        px = 0
        for box in boxes:
            self.screen.show(self.renderer.frame, box)
            px += (box[2] - box[0]) * (box[3] - box[1])
        if boxes:
            self.paints += 1
            self.pushed_px += px
            self.paint_ms += (time.perf_counter() - t0) * 1000
        return px

    def music(self) -> tuple[dict | None, float]:
        """The jukebox's status and where the music is right now (seconds)."""
        with self._lock:
            state, offline, got_at = self._state, self._offline, self._got_at
        if offline or not isinstance(state, dict):
            return None, 0.0
        try:
            pos = float(state.get("position") or 0.0)
        except (TypeError, ValueError):
            pos = 0.0
        if isinstance(state.get("current"), dict) and not state.get("paused") \
                and not state.get("buffering"):
            pos += max(0.0, time.monotonic() - got_at)
        return state, pos

    def power_step(self) -> None:
        """Lets the TV go to sleep when nothing plays, and wakes it for music —
        politely (cec.TvPower); what it did or why not goes to the status."""
        if self.power is None:
            return
        with self._lock:
            state, offline, idle = self._state, self._offline, self._idle_since
        idle_s = time.monotonic() - idle if idle is not None else 0.0
        try:
            note = self.power.step(wish_from(None if offline else state, idle_s))
        except Exception as exc:  # the TV's power is a nicety; the screen is not
            log.warning("vypínání telky: %s", exc, exc_info=log.isEnabledFor(logging.DEBUG))
            emit("tv.cec_error", error=f"{type(exc).__name__}: {exc}"[:160])
            return
        if self.director is not None:
            self.director.extra = {"power": self.power.state, "power_note": note}

    def video_step(self) -> bool:
        """Lets the director start, steer or stop the clip; True while it shows
        (then the screen must not draw — the display is the player's)."""
        if self.director is None:
            return False
        state, pos = self.music()
        try:
            return self.director.step(want_from(state, pos)) == "video"
        except Exception as exc:  # the clip is expendable; the screen is not
            log.warning("klip: %s", exc, exc_info=log.isEnabledFor(logging.DEBUG))
            emit("tv.video_error", error=f"{type(exc).__name__}: {exc}"[:160])
            with contextlib.suppress(Exception):
                self.director.close()
            return False

    def _tick(self) -> float:
        """Seconds until something on screen can change by itself."""
        if self.director is not None and (self.director.session is not None or self._wants_video()):
            s = self.director.session
            # while the player starts, look often (when did it open, when is the
            # first frame up); afterwards the clip is steered once a second
            return 0.2 if s is not None and not getattr(s, "showing", True) else 1.0
        with self._lock:
            state, offline = self._state, self._offline
        now = time.time()
        to_minute = 60.0 - now % 60.0 + 0.05
        playing = (not offline and isinstance(state, dict) and isinstance(state.get("current"), dict)
                   and not state.get("paused"))
        wait = min(to_minute, float(PROGRESS_STEP)) if playing else to_minute
        # with the director the status file for the web's switch is kept fresh
        return min(wait, 30.0) if self.director is not None else wait

    def _wants_video(self) -> bool:
        with self._lock:
            tv = (self._state or {}).get("tv") if isinstance(self._state, dict) else None
        return isinstance(tv, dict) and tv.get("on") is True and bool(tv.get("video"))

    def run(self) -> None:
        self.feed.start()
        if self.watch is not None and not self.watch.is_alive():
            self.watch.start()
        full = True
        checked = reported = time.monotonic()
        while not self.stop.is_set():
            if not self._ensure_screen():
                self.stop.wait(RETRY_SCREEN)
                continue
            self.power_step()
            if self.video_step():
                # the clip owns the display: nothing is drawn underneath it
                self._video = True
                self.wake.wait(self._tick())
                self.wake.clear()
                continue
            if self._video:
                self._video = False
                full = True  # back from the clip: the whole screen again
                checked = time.monotonic()
            try:
                now = time.monotonic()
                if now - checked >= MODE_CHECK:
                    checked = now
                    if self.screen.changed():
                        log.info("rozlišení se změnilo — otevírám obrazovku znovu")
                        self._drop_screen()
                        full = True
                        continue
                self.paint(full=full)
                full = False
            except Exception as exc:
                # a framebuffer that went away must not kill the process
                log.warning("kreslení selhalo: %s", exc, exc_info=log.isEnabledFor(logging.DEBUG))
                emit("tv.paint_error", error=f"{type(exc).__name__}: {exc}"[:160])
                self._drop_screen()
                full = True
                self.stop.wait(5.0)
                continue
            if time.monotonic() - reported >= REPORT_EVERY:
                reported = time.monotonic()
                emit("tv.paint", paints=self.paints, mpx=round(self.pushed_px / 1e6, 2),
                     avg_ms=round(self.paint_ms / self.paints, 1) if self.paints else None,
                     reconnects=self.reconnects or None)
                self.paints = self.pushed_px = 0
                self.paint_ms = 0.0
            self.wake.wait(self._tick())
            self.wake.clear()

    def shutdown(self) -> None:
        self.stop.set()
        self.wake.set()
