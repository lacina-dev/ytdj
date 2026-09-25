"""A stand-in for the real panel: frames go to a PNG, taps come from stdin.

Taps on stdin, one per line, in screen coordinates:

    240 200              tap (down, then up 80 ms later)
    down 200 280         raw events, for drags by hand
    move 260 280
    up 260 280
    drag 150 280 350 280 a whole drag, in ~10 moves
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import time
from pathlib import Path

from PIL import Image

from .hw import Box, TouchEvent
from .ui import H, W

log = logging.getLogger(__name__)


class SimScreen:
    def __init__(self, out: str | Path = "/tmp/ytdj-panel.png", size: tuple[int, int] = (W, H)) -> None:
        self.size = size
        self.out = Path(out)
        self.glass = Image.new("RGB", size)  # what the "glass" shows now
        self.pushed_px = 0
        self.calls = 0
        self.lock = threading.Lock()

    def show(self, img: Image.Image, box: Box | None = None) -> None:
        box = box or (0, 0, *self.size)
        with self.lock:
            self.glass.paste(img.crop(box), box[:2])
            self.pushed_px += (box[2] - box[0]) * (box[3] - box[1])
            self.calls += 1
            tmp = self.out.with_name(f".{self.out.name}.tmp")
            self.glass.save(tmp, "PNG")
            os.replace(tmp, self.out)

    def close(self) -> None:
        pass


class SimTouch:
    def __init__(self, stream=None) -> None:
        self.q: queue.Queue[TouchEvent] = queue.Queue()
        self.stream = stream if stream is not None else sys.stdin
        if self.stream is not None and not self.stream.closed:
            threading.Thread(target=self._read, name="sim-touch", daemon=True).start()

    def feed(self, kind: str, x: int, y: int) -> None:
        self.q.put(TouchEvent(kind, int(x), int(y)))

    def tap(self, x: int, y: int, hold: float = 0.08) -> None:
        self.feed("down", x, y)
        time.sleep(hold)
        self.feed("up", x, y)

    def drag(self, x1: int, y1: int, x2: int, y2: int, steps: int = 10, dt: float = 0.05) -> None:
        self.feed("down", x1, y1)
        for i in range(1, steps + 1):
            time.sleep(dt)
            self.feed("move", round(x1 + (x2 - x1) * i / steps), round(y1 + (y2 - y1) * i / steps))
        time.sleep(dt)
        self.feed("up", x2, y2)

    def _read(self) -> None:
        try:
            for line in self.stream:
                parts = line.split()
                try:
                    if len(parts) == 2:
                        self.tap(int(parts[0]), int(parts[1]))
                    elif len(parts) == 3 and parts[0] in ("down", "move", "up"):
                        self.feed(parts[0], int(parts[1]), int(parts[2]))
                    elif len(parts) == 5 and parts[0] == "drag":
                        self.drag(*(int(p) for p in parts[1:]))
                    elif parts:
                        log.warning("nerozumím dotyku: %r", line.strip())
                except ValueError:
                    log.warning("nerozumím dotyku: %r", line.strip())
        except (OSError, ValueError):
            pass  # stdin closed — no more taps, that's fine

    def poll(self, timeout: float) -> TouchEvent | None:
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        pass
