"""Hlídač event loopu: když se zasekne, zjistit čím — i v provozu na Pi.

Všechno (přehrávač, web, panel, fronta přání) běží v jednom asyncio loopu.
Stačí jedno synchronní čekání na disk nebo síť a stojí všechno — 25. 9.
10,7 s (INSERT do state.db při iowait 40 %). Tahle úloha tiká každých
TICK s; vlákno vedle sleduje, kdy naposledy tikla. Když loop stojí déle než
THRESHOLD, vlákno si opíše zásobník hlavního vlákna (sys._current_frames)
— právě tam je to, co blokuje. Až se loop rozběhne, zapíše se
`sys.loop_lag {lag_ms, stack}`; když stojí přes STUCK, zapíše se hned
(`ongoing: true`), ať se to dozvíme, i kdyby se už nerozběhl.

Režie: jedno probuzení úlohy a jedno vlákna za TICK; zásobník se čte jen při
zaseknutí.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from typing import Any

from . import telemetry

TICK = 0.25
THRESHOLD = 0.5  # s zpoždění, od kterého se to zapíše
STUCK = 5.0  # s — tak dlouho stojící loop se ohlásí, ještě než se rozběhne
FRAMES = 8


def main_stack(thread_id: int, limit: int = FRAMES) -> list[str]:
    """Posledních `limit` rámců vlákna jako "soubor:řádek:funkce" (nejhlubší první)."""
    frame = sys._current_frames().get(thread_id)
    out: list[str] = []
    while frame is not None and len(out) < limit:
        code = frame.f_code
        path = code.co_filename
        # jen konec cesty: ytdj/state.py, asyncio/base_events.py
        short = os.sep.join(path.split(os.sep)[-2:])
        out.append(f"{short}:{frame.f_lineno}:{code.co_name}")
        frame = frame.f_back
    return out


class LoopWatch:
    def __init__(self, threshold: float = THRESHOLD, tick: float = TICK,
                 stuck: float = STUCK) -> None:
        self.threshold = threshold
        self.tick = tick
        self.stuck = stuck
        self.beat = time.monotonic()  # poslední tik loopu
        self.stack: list[str] | None = None  # opsaný zásobník během zaseknutí
        self.reported_ongoing = False
        self.events = 0
        self.max_lag_ms = 0
        self._main_id = threading.get_ident()
        self._stop = threading.Event()
        self._task: asyncio.Task | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._main_id = threading.get_ident()
        self.beat = time.monotonic()
        self._task = asyncio.create_task(self._ticker(), name="ytdj-loopwatch")
        self._thread = threading.Thread(target=self._watch, name="ytdj-loopwatch", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with_suppress = asyncio.gather(self._task, return_exceptions=True)
            await with_suppress

    async def _ticker(self) -> None:
        while True:
            before = time.monotonic()
            self.beat = before
            await asyncio.sleep(self.tick)
            now = time.monotonic()
            lag = now - before - self.tick
            self.beat = now
            if lag >= self.threshold:
                self._report(lag, ongoing=False)
            self.stack = None
            self.reported_ongoing = False

    def _watch(self) -> None:
        while not self._stop.wait(self.tick / 2):
            stalled = time.monotonic() - self.beat
            if stalled < self.threshold:
                continue
            if self.stack is None:
                # loop stojí — co právě dělá hlavní vlákno?
                try:
                    self.stack = main_stack(self._main_id)
                except Exception:
                    self.stack = ["?"]
            if stalled >= self.stuck and not self.reported_ongoing:
                self.reported_ongoing = True
                self._report(stalled, ongoing=True)

    def _report(self, lag: float, ongoing: bool) -> None:
        lag_ms = int(lag * 1000)
        self.events += 1
        self.max_lag_ms = max(self.max_lag_ms, lag_ms)
        fields: dict[str, Any] = {"lag_ms": lag_ms, "stack": self.stack or None}
        if ongoing:
            fields["ongoing"] = True
        telemetry.event("sys.loop_lag", **fields)
