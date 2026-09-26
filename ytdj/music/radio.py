"""Seed pools + round-robin.

The core of the whole thing: the LLM picks 3–5 seed tracks, we pull an
independent radio from each one, and we alternate between the pools when
filling the queue. A single seed on its own drifts to one artist within
~20 tracks; interleaving keeps it going for hours — and for free, since the
LLM is no longer involved.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field

from .. import telemetry
from ..config import Config
from ..state import Store
from .catalog import Catalog, Track

log = logging.getLogger(__name__)

RADIO_RETRY = 60.0  # s — po selhání rádia (síť, YouTube) nový pokus nejdřív za tolik


@dataclass
class Pool:
    seed: Track
    tracks: deque[Track] = field(default_factory=deque)
    last_good: str = ""  # last track not skipped — we reseed from it
    # rádio pro pool selhalo (síť, YouTube) — do té doby ho nezkoušet, ale ani
    # nezahazovat: po výpadku má pokračovat stejná nálada
    retry_at: float = 0.0

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
        # explicitní skladby (ytmusic isExplicit) do podkresu? Výchozí ne.
        self.allow_explicit = bool(getattr(cfg, "allow_explicit_radio", False))
        # registry of everything we have seen in this session — the LLM works
        # only with videoIds, here we translate them back to Tracks
        self.known: dict[str, Track] = {}
        # Režim interpreta ("hraj Midi Lidi") — viz set_artist(). Prázdné =
        # běžná rádia ze seedů.
        self.artist: str = ""
        self._artist_all: list[Track] = []
        # Omezený režim interpreta (podkres po přání): kolik skladeb ještě a do
        # kdy (epoch s, přežije restart); None = bez omezení ("hraj X" sám).
        # Pi 26. 9.: podkres z přání displeje hrál Parni Valjak i po dalších
        # přáních a po restartu znovu — celkem skoro hodinu.
        self.artist_left: int | None = None
        self.artist_until: float | None = None
        # hlasování kanceláře (ytdj/votes.py, zapojí App): vyřazené ne,
        # upozaděné napůl, oblíbené i dřív než po repeat_days
        self.votes = None
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
        self.artist_left = self.artist_until = None
        summary = []
        self.remember_tracks(seeds)
        # rádia všech seedů naráz (dřív po sobě: 5 seedů = 4 s, než přání
        # nálady vůbec vědělo, co zahraje první)
        radios = await asyncio.gather(*(self._fetch_radio(seed.id) for seed in seeds))
        for seed, got in zip(seeds, radios):
            tracks = got or []
            self.remember_tracks(tracks)
            pool = Pool(seed=seed, tracks=deque(tracks), last_good=seed.id,
                        retry_at=0.0 if got is not None else time.monotonic() + RADIO_RETRY)
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
        self, name: str, tracks: list[Track] | None = None, mood: str = "",
        max_tracks: int | None = None, until: float | None = None,
    ) -> dict:
        """Režim interpreta: fronta hraje jen jeho, dokud nepřijde jiný pokyn.

        "Já nechtěl písničku, já chci, aby to hrálo interpreta." Jeden seed a
        rádio z něj do tří skladeb uteče k jiným kapelám; tady se místo rádia
        hraje seznam skladeb toho interpreta (nejznámější první, každá
        jednou, živáky a remixy až nakonec) a když dojde, jede se znovu od
        začátku. Končí to až další set_seeds() nebo set_artist() — nebo po
        `max_tracks` skladbách / v čase `until` (epoch s), pak přejde na rádio
        "<interpret> a podobné" (podkres po přání nesmí viset na jednom
        interpretovi donekonečna).

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
        self.artist_left = max_tracks
        self.artist_until = until
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

    async def _fetch_radio(self, video_id: str) -> list[Track] | None:
        """Rádio ze seedu; None = selhalo (síť, YouTube) — na rozdíl od [],
        kdy rádio prostě nic nového nemá."""
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
            return None
        finally:
            ev["took_ms"] = int((time.monotonic() - t0) * 1000)
            telemetry.event("radio.fetch", **ev)

    # ---- dispensing ----

    def artist_expired(self, now: float | None = None) -> bool:
        """Omezený režim interpreta vypršel (skladby nebo čas)?"""
        if not self.artist:
            return False
        if self.artist_left is not None and self.artist_left <= 0:
            return True
        now = time.time() if now is None else now
        return self.artist_until is not None and now >= self.artist_until

    async def soften_artist(self, why: str = "expired") -> bool:
        """Režim interpreta → rádio "<interpret> a podobné" (jeho známé skladby
        jako seedy). True = přepnuto."""
        if not self.artist:
            return False
        name, every = self.artist, list(self._artist_all)
        seeds, seen = [], set()
        for t in every:  # nejznámější první, od každého uvedeného interpreta
            if t.artist not in seen:
                seen.add(t.artist)
                seeds.append(t)
            if len(seeds) >= 3:
                break
        for t in every:
            if len(seeds) >= 3:
                break
            if t not in seeds:
                seeds.append(t)
        telemetry.event("radio.artist_mode", artist=name, ended=why, seeds=len(seeds))
        log.info("režim interpreta %s končí (%s) → %s a podobné", name, why, name)
        if not seeds:
            self.artist, self._artist_all = "", []
            self.artist_left = self.artist_until = None
            return True
        await self.set_seeds(seeds, mood=f"{name} a podobné")
        return True

    async def next_tracks(self, count: int) -> list[Track]:
        """Pulls `count` tracks, alternating between pools, with filters applied."""
        if self.artist_expired():
            await self.soften_artist()
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
                now = time.monotonic()
                if pool.retry_at > now:
                    # rádio tohoto poolu nedávno selhalo — zkusí se později;
                    # čekají-li tak všechny, nemá smysl točit se dokola
                    if all(not p.tracks and p.retry_at > now for p in self.pools):
                        break
                    continue
                await self._refill(pool)
                if not pool.tracks:
                    if pool.retry_at > time.monotonic():
                        continue  # selhalo — pool zůstává (dřív se smazal i s náladou)
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
            if self.artist and self.artist_left is not None:
                self.artist_left -= 1
                if self.artist_left <= 0:
                    break  # zbytek už z rádia "a podobné" (příští dávka)

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

    def _focus_names(self) -> list[str]:
        return [a.strip() for a in self.artist.split(",") if a.strip()] if self.artist else []

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
        votes = getattr(self, "votes", None)
        if votes is not None:
            # v režimu interpreta si ho někdo vyžádal jménem — jeho vlastní
            # vyřazení neplatí, vyřazené skladby ano
            why = votes.pool_reject(track, self._focus_names())
            if why:
                return why
        if track.explicit and not self.artist and not self.allow_explicit:
            # vulgární texty do podkresu ne; vyžádaný interpret / skladba jménem ano
            return "explicit"
        if track.id in self.session_seen:
            return "session_seen"
        if track.id in recent and not (votes is not None and votes.is_favourite(track)):
            return "recent"  # oblíbená kanceláře smí zase (v jednom běhu hlídá session_seen)
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
        if fresh is None:
            pool.retry_at = time.monotonic() + RADIO_RETRY
            return
        pool.retry_at = 0.0
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
            if self.artist_left is not None and any(t.id == a.id for a in self._artist_all):
                self.artist_left += 1

    def retrying(self) -> bool:
        """Některý pool čeká na nový pokus o rádio (selhalo) — nepřeseedovávat."""
        now = time.monotonic()
        return any(p.retry_at > now for p in self.pools)

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
