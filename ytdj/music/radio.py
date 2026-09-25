"""Seed pools + round-robin.

The core of the whole thing: the LLM picks 3–5 seed tracks, we pull an
independent radio from each one, and we alternate between the pools when
filling the queue. A single seed on its own drifts to one artist within
~20 tracks; interleaving keeps it going for hours — and for free, since the
LLM is no longer involved.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field

from .. import telemetry
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


async def _aread(store, name: str, *args):
    """Čtení ze state.db mimo event loop (Store.aread), jinak přímo (atrapy)."""
    aread = getattr(store, "aread", None)
    if aread is not None:
        return await aread(name, *args)
    return getattr(store, name)(*args)


class RadioPools:
    def __init__(self, catalog: Catalog, store: Store, cfg: Config) -> None:
        self.catalog = catalog
        self.store = store
        self.cfg = cfg
        self.pools: list[Pool] = []
        self._rr = 0  # round-robin pointer
        self.session_seen: set[str] = set()
        self.mood: str = ""
        # Vyžádaný interpret smí i dlouhé kusy — viz set_seeds().
        self.allow_long = False
        # registry of everything we have seen in this session — the LLM works
        # only with videoIds, here we translate them back to Tracks
        self.known: dict[str, Track] = {}
        # Režim interpreta ("hraj Midi Lidi") — viz set_artist(). Prázdné =
        # běžná rádia ze seedů.
        self.artist: str = ""
        self._artist_all: list[Track] = []
        # poslední zapsaný stav radio.pool — prázdné dávky se stejným stavem
        # (plnič se ptá každou vteřinu) se do logu nepíšou znovu
        self._last_pool_sig: tuple | None = None

    def remember_tracks(self, tracks: list[Track]) -> None:
        for t in tracks:
            self.known[t.id] = t

    def session_track(self, video_id: str) -> Track | None:
        return self.known.get(video_id)

    # ---- filling ----

    async def set_seeds(
        self, seeds: list[Track], mood: str = "", allow_long: bool = False
    ) -> dict:
        """Replaces the pools with new seeds and pulls in radios for them.

        `allow_long` se zapíná, když si posluchač vyžádal konkrétního
        interpreta: u něj je hodinový set to, co chtěl slyšet, kdežto v obecné
        náladě by stejně dlouhá stopa frontu jen zablokovala.
        """
        self.pools = []
        self._rr = 0
        self.mood = mood
        self.allow_long = allow_long
        self.artist = ""
        self._artist_all = []
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
        telemetry.event(
            "radio.seeds", mood=mood, allow_long=allow_long,
            seeds=[p["seed"] for p in summary],
            pool_sizes=[p["pool_size"] for p in summary],
        )
        return {"mood": mood, "pools": summary}

    async def set_artist(
        self, name: str, tracks: list[Track] | None = None, mood: str = ""
    ) -> dict:
        """Režim interpreta: fronta hraje jen jeho, dokud nepřijde jiný pokyn.

        "Já nechtěl písničku, já chci, aby to hrálo interpreta." Jeden seed a
        rádio z něj do tří skladeb uteče k jiným kapelám; tady se místo rádia
        hraje seznam skladeb toho interpreta (nejznámější první, každá
        jednou, živáky a remixy až nakonec) a když dojde, jede se znovu od
        začátku. Končí to až další set_seeds() nebo set_artist().

        `tracks` jsou typicky `catalog.artist_tracks(name)`; None = dohledá
        si je samo. Když interpreta nezná, stávající pooly nechá být a vrátí
        pool_size 0 — volající pak může říct "nenašel jsem".
        Filtr opakování (repeat_days) ani strop dvou skladeb na interpreta
        tu neplatí: o tohle si posluchač řekl jménem. Délku hlídá
        `max_duration_request` jako u vyžádaného interpreta.
        """
        if tracks is None:
            tracks = await self.catalog.artist_tracks(name, limit=100)
        telemetry.event(
            "radio.artist_mode", artist=name, n=len(tracks),
            first=tracks[0].label() if tracks else None,
            kept_previous=not tracks,
        )
        if not tracks:
            return {"artist": name, "pool_size": 0, "sample": []}
        order = self._artist_rotation(tracks, await self._history(len(tracks)))
        self.pools = [Pool(seed=tracks[0], tracks=deque(order), last_good=tracks[0].id)]
        self._rr = 0
        self.mood = mood or name
        self.allow_long = True
        self.artist = name
        self._artist_all = list(tracks)
        self.remember_tracks(tracks)
        self.store.record_seed(tracks[0].id, self.mood)
        return {
            "artist": name,
            "pool_size": len(tracks),
            "sample": [t.label() for t in tracks[:5]],
        }

    async def _history(self, n: int) -> list:
        try:
            return await _aread(self.store, "recent_history", max(200, 4 * n))
        except Exception:  # starší / testovací Store bez historie
            return []

    def _artist_rotation(self, tracks: list[Track], history: list | None = None) -> list[Track]:
        """Pořadí pro režim interpreta: dosud nehrané (nejznámější první), pak
        už hrané od nejdávněji hraného.

        Každé "pusť Kabát" (i automatický tah po sérii přeskočení a restart
        ytdj) dřív začínalo znovu od Malé dámy a Burlaků — na Pi 25. 9. zazněly
        tytéž tři skladby třikrát během dvanácti. Takhle se pool protáčí přes
        všechny skladby, než se nějaká zopakuje, i přes více tahů a restartů.
        """
        if history is None:
            try:
                history = self.store.recent_history(max(200, 4 * len(tracks)))
            except Exception:  # starší / testovací Store bez historie
                history = []
        rank: dict[str, int] = {}  # 0 = hrála naposledy
        for i, rec in enumerate(history):
            rank.setdefault(rec.video_id, i)
        fresh = [t for t in tracks if t.id not in rank]
        played = sorted((t for t in tracks if t.id in rank), key=lambda t: -rank[t.id])
        return fresh + played

    async def _fetch_radio(self, video_id: str) -> list[Track]:
        t0 = time.monotonic()
        seed = self.known.get(video_id)
        ev = {"seed": video_id, "label": seed.label() if seed else None}
        try:
            tracks = await self.catalog.radio(video_id, limit=self.cfg.radio_limit)
            ev["n"] = len(tracks)
            ev["new"] = sum(t.id not in self.session_seen for t in tracks)
            return tracks
        except Exception as exc:  # radio can fail for some IDs
            log.warning("rádio pro %s selhalo: %s", video_id, exc)
            ev["n"] = 0
            ev["error"] = f"{type(exc).__name__}: {exc}"[:300]
            return []
        finally:
            ev["took_ms"] = int((time.monotonic() - t0) * 1000)
            telemetry.event("radio.fetch", **ev)

    # ---- dispensing ----

    async def next_tracks(self, count: int) -> list[Track]:
        """Pulls `count` tracks, alternating between pools, with filters applied."""
        out: list[Track] = []
        blocked = await _aread(self.store, "blacklisted")
        # vyžádaný interpret má přednost před pravidlem neopakování
        recent = set() if self.artist else await _aread(
            self.store, "recently_played", self.cfg.repeat_days)
        artist_counts: dict[str, int] = {}
        # Dlouhé kusy se nezahazují, jen odloží: když se fronta z krátkých
        # nenaplní, sáhne se po nich (u vyžádaného interpreta).
        long_ones: list[Track] = []

        rejected: dict[str, int] = {}
        t0 = time.monotonic()
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
            reason = self._reject_reason(track, out, blocked, recent, artist_counts)
            if reason:
                rejected[reason] = rejected.get(reason, 0) + 1
                if self.allow_long and self._acceptable(
                    track, out, blocked, recent, artist_counts, self._long_limit()
                ):
                    long_ones.append(track)
                continue

            self.session_seen.add(track.id)
            artist_counts[track.artist] = artist_counts.get(track.artist, 0) + 1
            out.append(track)

            if len(pool) < self.cfg.pool_low:
                await self._refill(pool)

        long_used = 0
        for track in long_ones:
            if len(out) >= count:
                break
            if not self._acceptable(
                track, out, blocked, recent, artist_counts, self._long_limit()
            ):
                continue
            log.info(
                "beru delší kus (%s s): %s", track.duration, track.label()
            )
            self.session_seen.add(track.id)
            artist_counts[track.artist] = artist_counts.get(track.artist, 0) + 1
            out.append(track)
            long_used += 1

        pools = [len(p) for p in self.pools]
        sig = (count, sorted(rejected.items()), pools, self.artist, self.mood)
        if out or sig != self._last_pool_sig:
            telemetry.event(
                "radio.pool",
                wanted=count,
                got=len(out),
                attempts=attempts,
                rejected=rejected,
                long_used=long_used,
                pools=pools,
                artist_mode=self.artist or None,
                mood=self.mood,
                took_ms=int((time.monotonic() - t0) * 1000),
            )
        self._last_pool_sig = None if out else sig
        return out

    def _long_limit(self) -> int:
        """Strop pro vyžádaného interpreta — nikdy pod tím obvyklým."""
        return max(self.cfg.max_duration, self.cfg.max_duration_request)

    def _acceptable(
        self,
        track: Track,
        pending: list[Track],
        blocked: set[str],
        recent: set[str],
        artist_counts: dict[str, int],
        max_duration: int | None = None,
    ) -> bool:
        return self._reject_reason(
            track, pending, blocked, recent, artist_counts, max_duration
        ) is None

    def _reject_reason(
        self,
        track: Track,
        pending: list[Track],
        blocked: set[str],
        recent: set[str],
        artist_counts: dict[str, int],
        max_duration: int | None = None,
    ) -> str | None:
        """Proč skladba do fronty nejde (klíč do statistiky radio.pool), None = jde."""
        if track.id in blocked:
            return "blacklisted"
        if track.id in self.session_seen:
            return "session_seen"
        if track.id in recent:
            return "recent"
        if any(t.id == track.id for t in pending):
            return "duplicate"
        if track.duration is not None:
            ceiling = max_duration or self.cfg.max_duration
            if track.duration < self.cfg.min_duration:
                return "too_short"
            if track.duration > ceiling:
                return "too_long"
        # at most 2 tracks by the same artist per refill — kromě režimu
        # interpreta, kde je to celý smysl
        if not self.artist and artist_counts.get(track.artist, 0) >= 2:
            return "artist_cap"
        return None

    async def _refill(self, pool: Pool) -> None:
        """Reseeds from the last track not skipped — implicit feedback."""
        if self.artist:
            history = await self._history(len(self._artist_all))
            blocked = await _aread(self.store, "blacklisted")
            self._refill_artist(pool, history, blocked)
            return
        fresh = await self._fetch_radio(pool.last_good or pool.seed.id)
        self.remember_tracks(fresh)
        new = [t for t in fresh if t.id not in self.session_seen]
        pool.tracks.extend(new)
        log.debug("pool %s doplněn o %d", pool.seed.label(), len(new))

    def _refill_artist(self, pool: Pool, history: list | None = None,
                       blocked: set[str] | None = None) -> None:
        """Režim interpreta se nedoplňuje z rádia (to by uteklo jinam), ale
        znovu jeho skladbami; když zazněly všechny, jede se od začátku."""
        if blocked is None:
            blocked = self.store.blacklisted()
        skip = {t.id for t in pool.tracks} | blocked
        fresh = [
            t for t in self._artist_rotation(self._artist_all, history)
            if t.id not in self.session_seen and t.id not in skip
        ]
        if not fresh and not pool.tracks:
            log.info("interpret %s dohrán, jedu jeho skladby znovu", self.artist)
            telemetry.event("radio.artist_mode", artist=self.artist, restart=True,
                            n=len(self._artist_all))
            self.session_seen -= {t.id for t in self._artist_all}
            fresh = self._artist_rotation(self._artist_all, history)
        pool.tracks.extend(fresh)

    # ---- feedback ----

    def give_back(self, tracks: list[Track]) -> None:
        """Skladby vydané next_tracks, které se nakonec do fronty nedostaly
        (plnič je zahodil, protože mezitím přišel nový tah) — nepočítat je
        jako zahrané, ať o ně režim interpreta nepřijde."""
        for t in tracks:
            self.session_seen.discard(t.id)

    def mark_finished(self, video_id: str) -> None:
        """Track finished playing — a good signal, use it as the pool's next seed."""
        for pool in self.pools:
            pool.last_good = video_id
            break

    def exhausted(self) -> bool:
        if self.artist and self._artist_all:
            return False  # dojde-li, jede se znovu — viz _refill_artist
        return not self.pools or all(len(p) == 0 for p in self.pools)

    def describe(self) -> str:
        if self.artist and self.pools:
            return f"interpret {self.artist} (v zásobě {len(self.pools[0])} skladeb)"
        if not self.pools:
            return "žádné aktivní seedy"
        return ", ".join(f"{p.seed.label()} ({len(p)})" for p in self.pools)
