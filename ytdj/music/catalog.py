"""Layer on top of ytmusicapi.

Rule: what leaves this module is always a compact `Track`, never a raw dict
from ytmusicapi — that one carries thumbnails and feedbackTokens, i.e.
hundreds of useless tokens per result.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, asdict
from typing import Any

from ytmusicapi import YTMusic

from ..config import BROWSER_AUTH, Config


@dataclass(slots=True)
class Track:
    id: str
    title: str
    artist: str
    album: str | None = None
    duration: int | None = None  # seconds

    def compact(self) -> dict:
        """What the LLM will see — without None fields."""
        return {k: v for k, v in asdict(self).items() if v is not None}

    def label(self) -> str:
        return f"{self.artist} — {self.title}" if self.artist else self.title


# ytmusicapi sometimes stuffs `artists` with the play count ("3,4 tis.
# přehrání"), the like count ("Líbí se 2,7 tis. lidem") or the year. Without
# this filter it drags all the way into the prompt and the history.
_NOT_AN_ARTIST = re.compile(
    r"(přehrání|zhlédnutí|views|plays|streams|likes?|lidem)\s*$"
    r"|^líbí\s+se\b"
    r"|^\d{4}$",
    re.I,
)


def _clean(text: str) -> str:
    return text.replace("\xa0", " ").strip()


def _artists(item: dict) -> str:
    arts = item.get("artists") or []
    names = [_clean(a.get("name", "")) for a in arts if isinstance(a, dict)]
    names = [n for n in names if n and not _NOT_AN_ARTIST.search(n)]
    return ", ".join(names)


def _duration(item: dict) -> int | None:
    if isinstance(item.get("duration_seconds"), int):
        return item["duration_seconds"]
    text = item.get("duration") or item.get("length")
    if not isinstance(text, str):
        return None
    parts = text.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    seconds = 0
    for n in nums:
        seconds = seconds * 60 + n
    return seconds


def to_track(item: dict) -> Track | None:
    vid = item.get("videoId")
    title = item.get("title")
    if not vid or not title:
        return None
    album = item.get("album")
    if isinstance(album, dict):
        album = album.get("name")
    return Track(
        id=vid,
        title=_clean(title),
        artist=_artists(item),
        album=album if isinstance(album, str) else None,
        duration=_duration(item),
    )


class Catalog:
    """YTMusic wrapped in asyncio — YTMusic is a synchronous `requests`
    client, so every call goes to a thread to avoid blocking the loop (and
    with it, playback).
    """

    def __init__(self, cfg: Config) -> None:
        auth = str(BROWSER_AUTH) if BROWSER_AUTH.exists() else None
        self.yt = YTMusic(auth, language=cfg.language, location=cfg.location)
        self.authenticated = auth is not None
        self._cfg = cfg

    async def _call(self, fn, *args, **kwargs) -> Any:
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def search(self, query: str, limit: int = 8) -> list[Track]:
        # CAUTION: ytmusicapi treats `limit` as a lower bound, not an upper
        # one — YTM paginates by 20. Always trim on our side.
        raw = await self._call(self.yt.search, query, filter="songs", limit=limit)
        tracks = [t for t in (to_track(i) for i in raw) if t]
        return tracks[:limit]

    async def radio(self, video_id: str, limit: int = 50) -> list[Track]:
        """Radio from a seed track. Returns a different mix every time — by design."""
        res = await self._call(
            self.yt.get_watch_playlist, videoId=video_id, radio=True, limit=limit
        )
        return [t for t in (to_track(i) for i in res.get("tracks", [])) if t]

    async def mood_categories(self) -> dict[str, list[dict]]:
        """params change over time — never hardcode them, fetch at runtime."""
        return await self._call(self.yt.get_mood_categories)

    async def mood_playlists(self, params: str) -> list[dict]:
        return await self._call(self.yt.get_mood_playlists, params)

    async def playlist_tracks(self, playlist_id: str, limit: int = 50) -> list[Track]:
        # shuffle=True crashes on RDCLAK5 playlists — ytmusicapi only sends it
        # for PL/OLA prefixes. Without shuffle it always goes through.
        res = await self._call(
            self.yt.get_watch_playlist, playlistId=playlist_id, limit=limit
        )
        return [t for t in (to_track(i) for i in res.get("tracks", [])) if t]

    async def rate(self, video_id: str, rating: str) -> None:
        """LIKE | DISLIKE | INDIFFERENT — only when logged in."""
        if not self.authenticated:
            raise RuntimeError("hodnocení vyžaduje přihlášení (ytmusicapi browser)")
        await self._call(self.yt.rate_song, video_id, rating)
