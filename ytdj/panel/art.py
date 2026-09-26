"""Cover art for the player: YouTube thumbnails, fetched and decoded off the main thread.

`current.id` is a YouTube video id; YouTube serves a 320×180 JPEG for every
video at i.ytimg.com (`mqdefault`, 3–10 kB). YouTube Music "Art Tracks" are
the square album cover letterboxed into 16:9, so a centre square crop is the
cover itself; for an ordinary music video it is the middle of the frame.

One worker thread fetches, decodes and scales to the exact tile size, and
keeps the last few tiles in RAM (a 124 px tile is 46 kB). Failures are
remembered for a while, so an offline panel doesn't hammer the network. When
a tile is ready the panel is told (`on_ready(video_id)`) and redraws just the
art region.
"""

from __future__ import annotations

import io
import logging
import os
import queue
import re
import threading
import time
import http.client
from urllib.parse import urlsplit
from collections import OrderedDict
from typing import Callable

from PIL import Image

log = logging.getLogger(__name__)

# Plain HTTP on purpose: http.client is loaded by the panel anyway, while TLS
# would add OpenSSL (~4 MB RSS measured after the first handshake) and a
# handshake's CPU per cover on the Pi. A thumbnail is public; if plain HTTP
# is blocked somewhere, HTTPS is tried once.
URL = os.environ.get("YTDJ_PANEL_ART_URL", "http://i.ytimg.com/vi/{id}/mqdefault.jpg")
TIMEOUT = 4.0
KEEP = 6  # tiles in RAM
RETRY_AFTER = 600.0  # s before a failed id is tried again
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def square_tile(data: bytes, side: int) -> Image.Image:
    """JPEG bytes → the centre square, scaled to side×side RGB."""
    im = Image.open(io.BytesIO(data))
    im.draft("RGB", (side * 2, side * 2))  # JPEG: decode at a reduced scale when it's much bigger
    im = im.convert("RGB")
    w, h = im.size
    s = min(w, h)
    box = ((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s)
    return im.resize((side, side), Image.LANCZOS, box=box)


def _http_get(url: str, timeout: float) -> bytes:
    parts = urlsplit(url)
    try:
        return _get(parts.scheme, parts.netloc, parts.path, timeout)
    except (OSError, http.client.HTTPException, ValueError):
        if parts.scheme != "http":
            raise
        return _get("https", parts.netloc, parts.path, timeout)


def _get(scheme: str, host: str, path: str, timeout: float) -> bytes:
    cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    conn = cls(host, timeout=timeout)
    try:
        conn.request("GET", path, headers={"User-Agent": "ytdj-panel"})
        resp = conn.getresponse()
        data = resp.read(256 * 1024)
        if resp.status != 200:
            raise ValueError(f"HTTP {resp.status}")
        return data
    finally:
        conn.close()


class ArtCache:
    def __init__(
        self,
        side: int,
        on_ready: Callable[[str], None],
        stop: threading.Event,
        fetch: Callable[[str, float], bytes] | None = None,
        url: str = URL,
    ) -> None:
        self.side = side
        self.on_ready = on_ready
        self.stop = stop
        self.fetch = fetch or _http_get
        self.url = url
        self._tiles: OrderedDict[str, Image.Image] = OrderedDict()
        self._failed: dict[str, float] = {}
        self._wanted: set[str] = set()
        self._q: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.stats = {"fetched": 0, "failed": 0, "fetch_ms": 0.0, "decode_ms": 0.0}

    def enabled(self) -> bool:
        return bool(self.url)

    def get(self, vid: str) -> Image.Image | None:
        """The tile if it's ready; otherwise asks for it and returns None."""
        if not vid or not self.enabled() or not VIDEO_ID.match(vid):
            return None
        with self._lock:
            tile = self._tiles.get(vid)
            if tile is not None:
                self._tiles.move_to_end(vid)
                return tile
        self.want(vid)
        return None

    def failed(self, vid: str) -> bool:
        with self._lock:
            at = self._failed.get(vid)
        return at is not None and time.monotonic() - at < RETRY_AFTER

    def want(self, vid: str) -> None:
        """Fetch in the background (the next track's art, before it's needed)."""
        if not vid or not self.enabled() or not VIDEO_ID.match(vid):
            return
        with self._lock:
            if vid in self._tiles or vid in self._wanted:
                return
            at = self._failed.get(vid)
            if at is not None and time.monotonic() - at < RETRY_AFTER:
                return
            self._wanted.add(vid)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="panel-art", daemon=True)
                self._thread.start()
        self._q.put(vid)

    def _run(self) -> None:
        while not self.stop.is_set():
            try:
                vid = self._q.get(timeout=1.0)
            except queue.Empty:
                continue
            t0 = time.perf_counter()
            tile = None
            try:
                data = self.fetch(self.url.format(id=vid), TIMEOUT)
                t1 = time.perf_counter()
                tile = square_tile(data, self.side)
                t2 = time.perf_counter()
                self.stats["fetched"] += 1
                self.stats["fetch_ms"] += (t1 - t0) * 1000
                self.stats["decode_ms"] += (t2 - t1) * 1000
                log.debug("obal %s: stažení %.0f ms, dekódování %.1f ms", vid, (t1 - t0) * 1000, (t2 - t1) * 1000)
            except Exception as exc:  # network, 404, a broken JPEG — the fallback tile shows
                self.stats["failed"] += 1
                log.info("obal %s se nepodařilo načíst: %s", vid, str(exc)[:120])
            with self._lock:
                self._wanted.discard(vid)
                if tile is None:
                    self._failed[vid] = time.monotonic()
                    if len(self._failed) > 200:
                        self._failed.clear()
                else:
                    self._failed.pop(vid, None)
                    self._tiles[vid] = tile
                    while len(self._tiles) > KEEP:
                        self._tiles.popitem(last=False)
            try:
                self.on_ready(vid)
            except Exception:
                log.debug("on_ready selhal", exc_info=True)
