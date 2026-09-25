"""Player interface.

The agent logic (ytmusicapi + Codex + radio pools) is independent of this
layer — swapping mpv for pear-desktop / YTMDesktop means writing a different
`Player` implementation, nothing more.
"""

from __future__ import annotations

import asyncio
import contextlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncContextManager, Awaitable, Callable

from ..music.catalog import Track


@dataclass(slots=True)
class PlayerStatus:
    playing: bool
    paused: bool
    current: Track | None
    position: float  # seconds
    duration: float
    queue: list[Track]
    volume: int
    # Co doopravdy teče z reproduktoru, např. "opus 251 kb/s". Prázdné, dokud
    # se skladba nerozjede — bitrate se pozná až z pár vteřin proudu.
    quality: str = ""
    # Skladba je na řadě a nepozastavená, ale ještě nehraje — yt-dlp a síť
    # teprve dodávají proud (na Pi 3 i několik vteřin). Čas mezitím stojí.
    buffering: bool = False
    # Výpadek (síť / YouTube / cookies): {"reason", "since", "detail"}. Mezitím
    # se nehraje, fronta i přání čekají a přehrávač zkouší spojení.
    outage: dict | None = None


@dataclass(slots=True)
class PlayerEvent:
    # "start" | "sound" (první zvuk) | "finished" | "skipped" | "replaced" | "idle"
    # "error": skladbu nejde přehrát (detail "content|…", "removed|…",
    #          "retry_failed|…") — přání ji může odepsat
    # "unavailable": selhala kvůli výpadku, ne kvůli sobě — nic neodepisovat,
    #          po výpadku se zkusí znovu
    # "outage" / "outage_end": přehrávání stojí kvůli výpadku / zase běží
    kind: str
    track: Track | None = None
    detail: str = ""


EventHandler = Callable[[PlayerEvent], Awaitable[None]]


def queue_transaction(player: object) -> AsyncContextManager:
    """`async with queue_transaction(player):` — sled změn fronty naráz.

    Tah DJe (vyčistit frontu, zařadit vyžádané, dosypat, přeskočit) se nesmí
    prolnout s plničem fronty, jinak se jeho skladby ocitnou mezi vyžádanými
    nebo před nimi. Přehrávač bez transakcí (testovací atrapy) → nic.
    """
    fn = getattr(player, "transaction", None)
    return fn() if callable(fn) else contextlib.nullcontext()


class Player(ABC):
    _txn_lock: asyncio.Lock | None = None
    # Kolik skladeb chce přehrávač mít ve frontě, aby je stihl připravit
    # dopředu (plnič fronty drží aspoň tolik + 1).
    prefetch_depth: int = 0
    # Zvýší se při každém vyčištění fronty — plnič podle toho pozná, že
    # skladby, které si mezitím vybral, patří ke staré náladě.
    generation: int = 0

    async def wait_ready(self, video_id: str, timeout: float) -> bool:
        """Připravit skladbu přednostně a počkat, až půjde pustit bez čekání.

        False = přehrávač to neumí / nestihlo se (volající pak utne hned).
        """
        return False

    def transaction(self) -> asyncio.Lock:
        """Zámek pro sled několika změn fronty (viz queue_transaction)."""
        if self._txn_lock is None:
            self._txn_lock = asyncio.Lock()
        return self._txn_lock
    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def enqueue(self, tracks: list[Track]) -> int: ...

    @abstractmethod
    async def enqueue_next(self, tracks: list[Track]) -> int:
        """Queue right behind the current track, not at the end.

        What someone asked for shouldn't have to wait out the whole queue.
        """

    @abstractmethod
    async def clear_queue(self) -> None: ...

    @abstractmethod
    async def skip(self, by_user: bool = True) -> None:
        """Na další skladbu. `by_user=False`, když hrající skladbu odsouvá
        aplikace sama (nové rádio, vyžádaný odkaz) — pak to není signál, že
        se skladba nelíbila, a konec se ohlásí jako "replaced"."""

    @abstractmethod
    async def toggle_pause(self, paused: bool | None = None) -> None: ...

    @abstractmethod
    async def set_volume(self, volume: int) -> None: ...

    @abstractmethod
    async def status(self) -> PlayerStatus: ...

    @abstractmethod
    def on_event(self, handler: EventHandler) -> None: ...

    @property
    @abstractmethod
    def queue_depth(self) -> int:
        """How many tracks are waiting behind the one currently playing."""
