"""Seed pools + round-robin.

The core of the whole thing: the LLM picks 3–5 seed tracks, we pull an
independent radio from each one, and we alternate between the pools when
filling the queue. A single seed on its own drifts to one artist within
~20 tracks; interleaving keeps it going for hours — and for free, since the
LLM is no longer involved.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field

from ..config import Config
from ..state import Store
from .catalog import Catalog, Track

log = logging.getLogger(__name__)


@dataclass
class Pool:
    seed: Track
    tracks: deque[Track] = field(default_factory=deque)
    last_good: str = ""  # last track not skipped — we reseed from it

    def __len__(self) -> int:
        return len(self.tracks)


class RadioPools:
    def __init__(self, catalog: Catalog, store: Store, cfg: Config) -> None:
        self.catalog = catalog
        self.store = store
        self.cfg = cfg
        self.pools: list[Pool] = []
        self._rr = 0  # round-robin pointer
        self.session_seen: set[str] = set()
        self.mood: str = ""
        # registry of everything we have seen in this session — the LLM works
        # only with videoIds, here we translate them back to Tracks
        self.known: dict[str, Track] = {}

    def remember_tracks(self, tracks: list[Track]) -> None:
        for t in tracks:
            self.known[t.id] = t

    def session_track(self, video_id: str) -> Track | None:
        return self.known.get(video_id)

    # ---- filling ----

    async def set_seeds(self, seeds: list[Track], mood: str = "") -> dict:
        """Replaces the pools with new seeds and pulls in radios for them."""
        self.pools = []
        self._rr = 0
        self.mood = mood
        summary = []
        self.remember_tracks(seeds)
        for seed in seeds:
            tracks = await self._fetch_radio(seed.id)
            self.remember_tracks(tracks)
            pool = Pool(seed=seed, tracks=deque(tracks), last_good=seed.id)
            self.pools.append(pool)
            self.store.record_seed(seed.id, mood)
            summary.append(
                {
                    "seed": seed.label(),
                    "pool_size": len(tracks),
                    "sample": [t.label() for t in tracks[:5]],
                }
            )
        return {"mood": mood, "pools": summary}

    async def _fetch_radio(self, video_id: str) -> list[Track]:
        try:
            return await self.catalog.radio(video_id, limit=self.cfg.radio_limit)
        except Exception as exc:  # radio can fail for some IDs
            log.warning("rádio pro %s selhalo: %s", video_id, exc)
            return []

    # ---- dispensing ----

    async def next_tracks(self, count: int) -> list[Track]:
        """Pulls `count` tracks, alternating between pools, with filters applied."""
        out: list[Track] = []
        blocked = self.store.blacklisted()
        recent = self.store.recently_played(self.cfg.repeat_days)
        artist_counts: dict[str, int] = {}

        attempts = 0
        max_attempts = count * 40
        while len(out) < count and self.pools and attempts < max_attempts:
            attempts += 1
            pool = self.pools[self._rr % len(self.pools)]
            self._rr += 1

            if not pool.tracks:
                await self._refill(pool)
                if not pool.tracks:
                    self.pools.remove(pool)
                    self._rr = 0
                    continue

            track = pool.tracks.popleft()
            if not self._acceptable(track, out, blocked, recent, artist_counts):
                continue

            self.session_seen.add(track.id)
            artist_counts[track.artist] = artist_counts.get(track.artist, 0) + 1
            out.append(track)

            if len(pool) < self.cfg.pool_low:
                await self._refill(pool)

        return out

    def _acceptable(
        self,
        track: Track,
        pending: list[Track],
        blocked: set[str],
        recent: set[str],
        artist_counts: dict[str, int],
    ) -> bool:
        if track.id in self.session_seen or track.id in blocked:
            return False
        if track.id in recent:
            return False
        if any(t.id == track.id for t in pending):
            return False
        if track.duration is not None:
            if not (self.cfg.min_duration <= track.duration <= self.cfg.max_duration):
                return False
        # at most 2 tracks by the same artist per refill
        if artist_counts.get(track.artist, 0) >= 2:
            return False
        return True

    async def _refill(self, pool: Pool) -> None:
        """Reseeds from the last track not skipped — implicit feedback."""
        fresh = await self._fetch_radio(pool.last_good or pool.seed.id)
        self.remember_tracks(fresh)
        new = [t for t in fresh if t.id not in self.session_seen]
        pool.tracks.extend(new)
        log.debug("pool %s doplněn o %d", pool.seed.label(), len(new))

    # ---- feedback ----

    def mark_finished(self, video_id: str) -> None:
        """Track finished playing — a good signal, use it as the pool's next seed."""
        for pool in self.pools:
            pool.last_good = video_id
            break

    def exhausted(self) -> bool:
        return not self.pools or all(len(p) == 0 for p in self.pools)

    def describe(self) -> str:
        if not self.pools:
            return "žádné aktivní seedy"
        return ", ".join(f"{p.seed.label()} ({len(p)})" for p in self.pools)
