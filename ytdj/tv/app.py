"""The TV screen's main loop: follow the jukebox, redraw only what changed.

One thread follows the jukebox's status stream (the same client the touch
panel uses), one fetches covers; the main thread sleeps until something
changes or until the next tick (progress every few seconds while music plays,
otherwise once a minute for the clock). No screen to draw on — no TV, no
framebuffer, an unknown pixel format — is not an error: the process idles and
looks again once a minute.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from ..panel.art import ArtCache
from ..panel.client import Api, StatusFeed
from ..panel.stats import emit
from .screen import PROGRESS_STEP, Renderer, TvView, cover_tile, view_from

log = logging.getLogger(__name__)

# a bigger thumbnail than the panel's: 480×360, the cover is its centre square
ART_URL = "http://i.ytimg.com/vi/{id}/hqdefault.jpg"
RETRY_SCREEN = 60.0  # s between looks for a usable framebuffer
MODE_CHECK = 30.0  # s between checks that the resolution hasn't changed
REPORT_EVERY = 600.0  # s between tv.paint summaries
SEEK_JUMP = 4.0  # s — a position this far from where we thought it was is a seek


class TvApp:
    def __init__(self, open_screen: Callable[[], object], url: str, address: str,
                 art_url: str = ART_URL, agent: str = "ytdj-tv") -> None:
        self.open_screen = open_screen
        self.address = address
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.api = Api(url, agent=agent)
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
        return view_from(state, offline=offline, got_at=got_at, address=self.address,
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

    def _tick(self) -> float:
        """Seconds until something on screen can change by itself."""
        with self._lock:
            state, offline = self._state, self._offline
        now = time.time()
        to_minute = 60.0 - now % 60.0 + 0.05
        playing = (not offline and isinstance(state, dict) and isinstance(state.get("current"), dict)
                   and not state.get("paused"))
        return min(to_minute, float(PROGRESS_STEP)) if playing else to_minute

    def run(self) -> None:
        self.feed.start()
        full = True
        checked = reported = time.monotonic()
        while not self.stop.is_set():
            if not self._ensure_screen():
                self.stop.wait(RETRY_SCREEN)
                continue
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
