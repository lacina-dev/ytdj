"""Provozní statistiky panelu — souhrny za minutu místo záplavy řádků.

Jednotlivé "zajímavé" věci (stisk tlačítka, změna obrazovky, výpadek ytdj)
jdou do `telemetry.event()` rovnou. Co se děje často — odmítnuté dotyky,
anomálie převodníku, každé překreslení — se tu jen počítá a jednou za
`PERIOD` se zapíše jako jeden souhrnný řádek. Nic z toho nesmí panel
zpomalit: přičtení čísla a append do seznamu, víc ne.

Všechny kinds začínají "panel." (viz `ytdj/telemetry.py`).
"""

from __future__ import annotations

import os
import threading
import time
from collections import Counter
from typing import Any, Callable

from .. import telemetry

PERIOD = 60.0  # s — jak často souhrny
ERROR_EVERY = 60.0  # s — stejná chyba nejvýš jednou za tuhle dobu, zbytek jen počet


def _pct(sorted_ms: list[float], q: float) -> float:
    if not sorted_ms:
        return 0.0
    i = min(len(sorted_ms) - 1, int(round(q * (len(sorted_ms) - 1))))
    return round(sorted_ms[i], 1)


def _cpu() -> float:
    t = os.times()
    return t.user + t.system


class PanelStats:
    """Počítadla a časy za běžící minutu; `flush()` je zapíše a vynuluje.

    `count()`/`paint()` volá jen hlavní vlákno panelu; `error()` kdokoli.
    """

    def __init__(self, now: float | None = None, touch_stats: Callable[[], dict] | None = None) -> None:
        self.touch_stats = touch_stats  # ovladač dotyku: vezmi a vynuluj jeho počítadla
        self._lock = threading.Lock()
        self._errors: dict[str, list] = {}  # where → [naposledy zapsáno, potlačeno od té doby]
        self._reset(time.monotonic() if now is None else now)

    def _reset(self, now: float) -> None:
        self.start = now
        self.cpu0 = _cpu()
        self.counts: Counter[str] = Counter()
        self.render_ms: list[float] = []
        self.show_ms: list[float] = []
        self.paints = 0
        self.full = 0
        self.px = 0

    # ---- sběr ----

    def count(self, key: str, n: int = 1) -> None:
        self.counts[key] += n

    def paint(self, render_ms: float, show_ms: list[float], px: int, full: bool) -> None:
        self.paints += 1
        self.full += full
        self.px += px
        self.render_ms.append(render_ms)
        self.show_ms.extend(show_ms)

    def error(self, where: str, exc: BaseException | str) -> None:
        """Chyba ovladače/smyčky: první hned, opakování jen jako počet."""
        now = time.monotonic()
        with self._lock:
            slot = self._errors.get(where)
            if slot is not None and now - slot[0] < ERROR_EVERY:
                slot[1] += 1
                return
            suppressed = slot[1] if slot else 0
            self._errors[where] = [now, 0]
        text = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
        telemetry.event("panel.driver_error", where=where, error=text[:200], repeated=suppressed)

    # ---- výstup ----

    def dirty(self) -> bool:
        return bool(self.paints or self.counts)

    def deadline(self) -> float | None:
        """Kdy flushnout; None = není co (nečinný panel se kvůli tomu nebudí)."""
        return self.start + PERIOD if self.dirty() else None

    def maybe_flush(self, now: float) -> None:
        if now - self.start >= PERIOD:
            self.flush(now)

    def flush(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        period = round(now - self.start, 1)
        if self.paints:
            r = sorted(self.render_ms)
            s = sorted(self.show_ms)
            telemetry.event(
                "panel.render",
                period_s=period,
                paints=self.paints,
                frames=len(s),
                full=self.full,
                px=self.px,
                render_p50_ms=_pct(r, 0.5),
                render_p95_ms=_pct(r, 0.95),
                show_p50_ms=_pct(s, 0.5),
                show_p95_ms=_pct(s, 0.95),
                show_max_ms=round(s[-1], 1) if s else 0.0,
                show_total_ms=int(sum(s)),
                cpu_ms=int((_cpu() - self.cpu0) * 1000),  # celý proces, všechna vlákna
            )
        if self.counts:
            telemetry.event("panel.touch_rejects", period_s=period, **dict(self.counts))
        if self.touch_stats is not None:
            try:
                drv = {k: v for k, v in self.touch_stats().items() if v}
            except Exception:
                drv = {}
            if drv:
                telemetry.event("panel.touch_driver", period_s=period, **drv)
        with self._lock:
            repeated = [(w, slot[1]) for w, slot in self._errors.items() if slot[1]]
            for w, _ in repeated:
                self._errors[w][1] = 0
        for where, n in repeated:
            # chyba se opakovala, ale první výskyt už je v logu — jen kolikrát
            telemetry.event("panel.driver_error", where=where, repeated=n, period_s=period)
        self._reset(now)


def scrub(text: str, secret: str | None) -> str:
    """Pro jistotu: heslo nikdy do logu, ani kdyby ho někdo vrátil v chybě."""
    if secret:
        text = text.replace(secret, "•••")
    return text


def emit(kind: str, **fields: Any) -> None:
    """`telemetry.event()` bez polí s None — řádky ať jsou krátké."""
    telemetry.event(kind, **{k: v for k, v in fields.items() if v is not None})
