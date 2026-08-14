"""Player interface.

The agent logic (ytmusicapi + Codex + radio pools) is independent of this
layer — swapping mpv for pear-desktop / YTMDesktop means writing a different
`Player` implementation, nothing more.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Awaitable, Callable

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


@dataclass(slots=True)
class PlayerEvent:
    kind: str  # "start" | "finished" | "skipped" | "error" | "idle"
    track: Track | None = None
    detail: str = ""


EventHandler = Callable[[PlayerEvent], Awaitable[None]]


class Player(ABC):
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
    async def skip(self) -> None: ...

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
