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
from typing import Any

from .. import telemetry
from ..config import Config
from ..state import Store
from .catalog import Catalog, Track
from .repeats import RepeatGuard, one_per_song, song_keys

log = logging.getLogger(__name__)

RADIO_RETRY = 60.0  # s — po selhání rádia (síť, YouTube) nový pokus nejdřív za tolik
FAV_KEEP = 8  # režim oblíbených: posledních tolik se na začátku nového kola nezopakuje


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


def _is_recent(recent: Any, track: Track) -> bool:
    """Hrála tu ve lhůtě neopakování? (`recent`: RepeatGuard, množina id, nebo None.)"""
    if recent is None:
        return False
    if isinstance(recent, (set, frozenset)):
        return track.id in recent
    return recent.recent(track)


def _same_song(track: Track, others: list[Track]) -> bool:
    keys = set(song_keys(track.artist, track.title))
    return bool(keys) and any(keys & set(song_keys(t.artist, t.title)) for t in others)


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
        # Filtr explicitních zvednutý jen pro jedno přání: posluchač si
        # výslovně řekl o vulgární texty (rozhodl model, `explicit_ok`).
        # Každé další naplnění poolů (set_seeds / set_artist / …) ho vrací.
        self.explicit_ok = False
        # Přání "i s obrazem" (klipy na telce): do podkresu jen oficiální klipy
        # (Track.video_type OMV). Platí jen pro to přání a jeho podkres —
        # každé další naplnění poolů to vrací, stejně jako explicit_ok.
        self.video_only = False
        # Album v podkresu ("pusť album Load"): skladby alba v pořadí, každá
        # jednou, bez rádia a bez jiných interpretů, dokud album neskončí —
        # viz set_album(). "" = není. Jede přes stejný stav jako režim
        # interpreta (`artist` = název alba), ale bez protáčení a opakování.
        self.album: str = ""
        self._album_artists: list[str] = []
        self._album_done: set[str] = set()  # zazněly (nebo patří bloku přání)
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
        # upozaděné napůl, oblíbené dopředu (ale ne dřív než po repeat_days)
        self.votes = None
        # poslední zapsaný stav radio.pool — prázdné dávky se stejným stavem
        # (plnič se ptá každou vteřinu) se do logu nepíšou znovu
        self._last_pool_sig: tuple | None = None
        # roste s každým naplněním poolů (nové seedy, interpret, doplnění) —
        # hlasování podle něj posouvá oblíbené jen jednou na doplnění
        self.generation = 0
        # Režim oblíbených ("hraj, co máme rádi, pořád") — viz set_favourites().
        # "office" / "mine" / "" = není. Hraje oblíbené napřeskáčku, dokud
        # nepřijde jiné přání; bez limitu skladeb (na rozdíl od interpreta).
        self.favourites: str = ""
        self._fav_voter = ""
        self._fav_played: set[str] = set()  # zazněly v tomhle kole oblíbených
        self._fav_alternate = True  # napřeskáčku podle interpretů
        self._fav_last: deque = deque(maxlen=FAV_KEEP)
        # Oblíbené dokola (vlastník 16. 10. 2026): když všechny ve lhůtě
        # neopakování hrály, hrají znovu od nejdéle nehrané (_fav_cycle).
        # `fav_again` = skladby vydané v tomhle kole — pravidlo neopakování je
        # nevyřadí a hlídka je hlásí jako `favourites_rotation`, ne jako díru.
        # `_fav_given` = kdy byla která oblíbená vydaná (čeká ve frontě, dokud
        # nezačne hrát — do dalšího kola nepatří).
        self.fav_again: set[str] = set()
        self._fav_given: dict[str, float] = {}
        self._fav_wait = False
        # Pravidlo neopakování (repeats.py): co tu hrálo v posledních
        # `repeat_days` dnech, podkres nevydá — v žádném režimu (rádio,
        # interpret, album, oblíbené). Vlastník 12. 10. 2026, hlášení #32.
        self.repeats = RepeatGuard(store, cfg)
        # Výjimka jen pro přání, ze kterého je jasné, že chce i to, co už
        # hrálo ("hraj Kabát, klidně i to, co už bylo" — rozhodl model,
        # `repeat_ok`): platí pro režim interpreta / album / oblíbené toho
        # přání; každé další naplnění poolů ji vrací.
        self.repeat_ok = False
        # režim interpreta vyčerpal, co ve lhůtě nehrálo —
        # místo opakování přejde na podobnou hudbu (_soften_dry)
        self._dry = False
        self._batch: set[str] = set()  # id skladeb právě vydávané dávky (_pull)

    def remember_tracks(self, tracks: list[Track]) -> None:
        for t in tracks:
            self.known[t.id] = t

    def session_track(self, video_id: str) -> Track | None:
        return self.known.get(video_id)

    # ---- filling ----

    async def set_seeds(
        self, seeds: list[Track], mood: str = "", allow_long: bool = False,
        explicit_ok: bool = False, video_only: bool = False,
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
        self.explicit_ok = bool(explicit_ok)
        self.video_only = bool(video_only)
        self.repeat_ok = False
        self._dry = False
        self.fav_again = set()
        self.favourites = ""
        self.artist = ""
        self.album = ""
        self._album_artists, self._album_done = [], set()
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
            self.generation += 1
            self.store.record_seed(seed.id, mood)
            summary.append(
                {
                    "seed": seed.label(),
                    "pool_size": len(tracks),
                    "sample": [t.label() for t in tracks[:5]],
                }
            )
        telemetry.event(
            "radio.seeds", mood=mood, allow_long=allow_long, explicit_ok=explicit_ok or None,
            video_only=video_only or None,
            seeds=[p["seed"] for p in summary],
            pool_sizes=[p["pool_size"] for p in summary],
        )
        return {"mood": mood, "pools": summary}

    async def set_artist(
        self, name: str, tracks: list[Track] | None = None, mood: str = "",
        max_tracks: int | None = None, until: float | None = None,
        repeat_ok: bool = False,
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
        Strop dvou skladeb na interpreta tu neplatí: o tohle si posluchač
        řekl jménem. Délku hlídá `max_duration_request` jako u vyžádaného
        interpreta. Pravidlo neopakování (repeat_days) platí i tady: hraje se
        jen to, co ve lhůtě nehrálo, a když nic takového nezbývá, přejde na
        "<interpret> a podobné" — neopakuje. Jen `repeat_ok` (přání výslovně
        chtělo i už hrané) ho zvedá; pak se po projetí všech jede znovu.
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
        self.generation += 1
        self._rr = 0
        self.mood = mood or name
        self.allow_long = True
        self.explicit_ok = False
        self.video_only = False
        self.repeat_ok = bool(repeat_ok)
        self._dry = False
        self.fav_again = set()
        self.favourites = ""
        self.artist = name
        self.album = ""
        self._album_artists, self._album_done = [], set()
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

    async def set_album(self, label: str, tracks: list[Track], mood: str = "",
                        done: set[str] | list[str] | tuple = (),
                        artists: list[str] | None = None, repeat_ok: bool = False) -> dict:
        """Album v podkresu: jeho skladby v pořadí alba, každá jednou.

        Vlastník 6. 10. 2026 ("Album jako podkres"): první skladby alba jdou
        jako přání, zbytek pokračuje tady — bez míchání s interpretem nebo
        rádiem, dokud album neskončí. `done` = co už zaznělo nebo patří bloku
        přání (nehraje se znovu); po cizím přání se pokračuje tam, kde se
        přestalo (`album_remaining`). Strop na interpreta, délka ani filtr
        explicitních tu neplatí — o album si posluchač řekl jménem; vyřazené
        hlasováním zůstávají vyřazené. Pravidlo neopakování platí: skladby
        alba, které tu ve lhůtě hrály, se vynechají (jen `repeat_ok` — přání
        výslovně chtělo i už hrané — je pustí). Když album dohraje, přejde
        podkres na rádio "<album> a podobné".
        Bez zbývajících skladeb nechá pooly být a vrátí pool_size 0.
        """
        done_ids = set(done)
        left = [t for t in tracks if t.id not in done_ids]
        telemetry.event("radio.album_mode", album=label, n=len(tracks), left=len(left),
                        first=left[0].label() if left else None)
        if not left:
            return {"album": label, "pool_size": 0, "sample": []}
        self.pools = [Pool(seed=left[0], tracks=deque(left), last_good=left[0].id)]
        self.generation += 1
        self._rr = 0
        self.mood = mood or f"album {label}"
        self.allow_long = True
        self.explicit_ok = False
        self.video_only = False
        self.repeat_ok = bool(repeat_ok)
        self._dry = False
        self.fav_again = set()
        self.favourites = ""
        self.artist = label
        self.album = label
        self._album_artists = [a for a in (artists or []) if a]
        self._album_done = {t.id for t in tracks if t.id in done_ids}
        self._artist_all = list(tracks)
        self.artist_left = self.artist_until = None
        self.remember_tracks(tracks)
        self.store.record_seed(left[0].id, self.mood)
        return {"album": label, "pool_size": len(left),
                "sample": [t.label() for t in left[:5]]}

    def album_remaining(self) -> list[Track]:
        """Co z alba ještě nezaznělo (v pořadí alba) — i to, co už čeká ve
        frontě přehrávače, ale nezačalo hrát."""
        if not self.album:
            return []
        return [t for t in self._artist_all if t.id not in self._album_done]

    def note_started(self, video_id: str) -> None:
        """Skladba začala hrát — u alba se už nebude opakovat."""
        if self.album and any(t.id == video_id for t in self._artist_all):
            self._album_done.add(video_id)

    async def set_favourites(self, which: str = "office", voter: str = "", mood: str = "",
                             played: list[str] | tuple = (), alternate: bool = True,
                             repeat_ok: bool = False) -> dict:
        """Režim oblíbených: podkres hraje oblíbené kanceláře (`which` "office")
        nebo 👍 jednoho člověka ("mine", `voter`), dokud nepřijde jiný pokyn.

        Vlastník 27. 9.: "ať hraje to, co máme rádi, napřeskáčku interprety …
        chtěl jsem, aby to hrál pořád, ale on naplánoval jen pár." Pořadí dává
        VoteBook.favourite_rotation: po lidech na střídačku, napřeskáčku podle
        interpretů; každá oblíbená zazní jednou, než se kolo zopakuje (pak se
        znovu zamíchá). `played` = co už zaznělo v bloku přání. Vyřazené
        hlasováním neprojdou (pool_reject). Na rozdíl od režimu interpreta po
        přání nemá limit skladeb ani času — "pořád" (FUNKCE F-FRONTA-20).
        Pravidlo neopakování (vlastník 12. a 16. 10. 2026): nejdřív oblíbené,
        které ve lhůtě `repeat_days` nehrály (pořadí jako výš); když dojdou,
        hrají dokola od nejdéle nehrané a žádná se nevrátí, dokud nezazní
        všechny ostatní (_fav_cycle) — nikdy "už všechno hrálo". S `repeat_ok`
        (přání výslovně chtělo i už hrané) jako dřív: zamíchaná kola.
        Bez oblíbených nechá pooly být a vrátí pool_size 0.
        """
        votes = self.votes
        mine = which == "mine" and bool(voter)
        who = voter if mine else ""
        # bez toho, co už zaznělo nebo čeká ve frontě (blok přání); když tím
        # oblíbené došly (jich je jen pár), podkres zůstane rádiem (volající)
        skip = set(played) | self.session_seen
        order = votes.favourite_rotation(who, skip, alternate) if votes is not None else []
        recent = 0
        again = False
        if not repeat_ok and votes is not None:
            await self.repeats.refresh()
            order, old = self.repeats.split(order)
            recent = len(old)
            if not order:
                # všechny ve lhůtě hrály: dokola od nejdéle nehrané (16. 10. 2026)
                self.fav_again, self._fav_given = set(), {}
                order = self._fav_cycle(votes.favourite_tracks(who, shuffle=False), set(played))
                again = bool(order)
        telemetry.event("radio.favourites_mode", which="mine" if mine else "office", n=len(order),
                        played=len(played), artists=len({t.artist for t in order}) or None,
                        recent=recent or None, repeat_ok=repeat_ok or None, again=again or None)
        if not order:
            return {"favourites": which, "pool_size": 0, "sample": []}
        self.pools = [Pool(seed=order[0], tracks=deque(order), last_good=order[0].id)]
        self.generation += 1
        self._rr = 0
        # na webu a displeji to vidí všichni: "vlastní", ne "tvoje"/"moje"
        self.mood = "vlastní oblíbené" if mine else "oblíbené kanceláře"
        self.allow_long = False
        self.explicit_ok = False
        self.video_only = False
        self.repeat_ok = bool(repeat_ok)
        self._dry = False
        if not again:
            self.fav_again, self._fav_given = set(), {}
        self.artist = ""
        self.album = ""
        self._album_artists, self._album_done = [], set()
        self._artist_all = []
        self.artist_left = self.artist_until = None
        self.favourites = "mine" if mine else "office"
        self._fav_voter = voter if mine else ""
        self._fav_alternate = alternate
        self._fav_played = set(played)
        self._fav_last.clear()
        self._fav_last.extend(played)
        self.remember_tracks(order)
        self.store.record_seed(order[0].id, self.mood)
        return {"favourites": self.favourites, "pool_size": len(order),
                "sample": [t.label() for t in order[:5]]}

    def _refill_favourites(self, pool: Pool) -> None:
        """Další oblíbené: nejdřív ty, které ve lhůtě neopakování nehrály;
        když žádné nezbývají, dokola od nejdéle nehrané (_fav_cycle —
        vlastník 16. 10. 2026). S `repeat_ok` po všech nové kolo (znovu
        zamíchané, bez posledních pár)."""
        votes = self.votes
        self._fav_wait = False
        if votes is None:
            return
        skip = self._fav_played | {t.id for t in pool.tracks}
        fresh = votes.favourite_rotation(self._fav_voter, skip, self._fav_alternate)
        fresh = self._unplayed(fresh)
        if not fresh and not pool.tracks:
            every = votes.favourite_tracks(self._fav_voter, shuffle=False)
            if not every:
                return  # oblíbené zmizely (vše vyřazené / odebrané) — pool se zavře
            if not self.repeat_ok:
                fresh = self._fav_cycle(every)
                if not fresh:
                    self._fav_wait = True  # všechny čekají ve frontě — pro teď nic
                    return
                log.info("oblíbené: všechny ve lhůtě hrály, jedu dokola (%d)", len(fresh))
                telemetry.event("radio.favourites_mode", which=self.favourites, again=True,
                                n=len(fresh))
                self.remember_tracks(fresh)
                pool.tracks.extend(fresh)
                self.generation += 1
                return
            log.info("oblíbené dohrány (%d), jedu nové kolo", len(self._fav_played))
            telemetry.event("radio.favourites_mode", which=self.favourites, restart=True,
                            played=len(self._fav_played))
            # nové kolo: co zaznělo, smí znovu — kromě posledních pár (a toho,
            # co mezitím zařadila přání: to je v session_seen, ne v _fav_played)
            # a jen to, co ve lhůtě neopakování nehrálo
            keep = set(self._fav_last)
            self.session_seen -= self._fav_played - keep
            self._fav_played = keep
            fresh = self._unplayed(votes.favourite_rotation(
                self._fav_voter, keep | self.session_seen, self._fav_alternate))
        self.remember_tracks(fresh)
        pool.tracks.extend(fresh)
        self.generation += 1

    def _fav_cycle(self, every: list[Track], exclude: set[str] | frozenset = frozenset()) -> list[Track]:
        """Oblíbené dokola, když všechny ve lhůtě neopakování už hrály.

        Vlastník 16. 10. 2026: „nejdříve hraje ty, co to ve lhůtě ještě
        nehrálo, a pak klidně opakuje, ale zas nezahraje znovu, dokud
        nepřehraje všechny." Pořadí podle toho, kdy která naposledy začala
        hrát (i v jiném nahrání), nejdéle nehraná první — takže se žádná
        nevrátí, dokud od jejího posledního přehrání nezazněly všechny
        ostatní. Bez těch, které už čekají ve frontě (vydané, ještě nehrály)."""
        guard = self.repeats

        def waiting(t: Track) -> bool:
            given = self._fav_given.get(t.id)
            return given is not None and (guard.last_played(t) or 0.0) < given

        order = [t for t in one_per_song(every) if t.id not in exclude and not waiting(t)]
        order.sort(key=lambda t: guard.last_played(t) or 0.0)
        ids = {t.id for t in order}
        self.session_seen -= ids
        self._fav_played -= ids
        self.fav_again |= ids
        return order

    def _unplayed(self, tracks: list[Track]) -> list[Track]:
        """Jen co ve lhůtě neopakování nehrálo (s `repeat_ok` všechno)."""
        return list(tracks) if self.repeat_ok else self.repeats.split(tracks)[0]

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
        if self._dry:
            return True  # nezbývá nic, co by ve lhůtě neopakování nehrálo
        if self.album:
            # album dohrálo: nic nezbývá vydat (poslední skladby už jsou ve frontě)
            return not self.pools or not self.pools[0].tracks
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
            self.album, self._album_artists, self._album_done = "", [], set()
            self.artist_left = self.artist_until = None
            return True
        # interpret hrál jako klipy (přání "i s obrazem"): i "… a podobné" drží klipy
        clips = bool(every) and all(t.video_type == "MUSIC_VIDEO_TYPE_OMV" for t in every)
        if clips:
            await self.set_seeds(seeds, mood=f"{name} a podobné", video_only=True)
        else:
            await self.set_seeds(seeds, mood=f"{name} a podobné")
        return True

    async def next_tracks(self, count: int) -> list[Track]:
        """Pulls `count` tracks, alternating between pools, with filters applied."""
        if self.artist_expired():
            await self.soften_artist(
                "all_played" if self._dry else "album_done" if self.album else "expired")
        out = await self._pull(count)
        if len(out) < count and self._dry and await self._soften_dry():
            # režim interpreta / oblíbených vyčerpal, co ve lhůtě nehrálo:
            # dál podobná hudba — ne opakování a ne ticho
            out += await self._pull(count - len(out), taken=out)
        return out

    async def _soften_dry(self) -> bool:
        """Nezbývá nic nehraného → rádio z téhož ("… a podobné"). True = přepnuto."""
        if self.artist:
            return await self.soften_artist("all_played")
        # oblíbené nedojdou nikdy — jedou dokola (_fav_cycle)
        self._dry = False
        return False

    async def _pull(self, count: int, taken: list[Track] | None = None) -> list[Track]:
        out: list[Track] = []
        if count <= 0:
            return out
        blocked = await _aread(self.store, "blacklisted")
        # Pravidlo neopakování platí ve všech režimech (rádio, interpret,
        # album, oblíbené) — zvedá ho jen přání, které si výslovně řeklo
        # i o už hrané (`repeat_ok`).
        await self.repeats.refresh()
        recent = None if self.repeat_ok else self.repeats
        self._batch = {t.id for t in taken or []}
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
                    if self.artist or (self.favourites and self._fav_wait):
                        # režim interpreta / album: pro tuhle dávku nic dalšího
                        # (nebo `_dry` → next_tracks přejde na "… a podobné");
                        # oblíbené dokola: všechny čekají ve frontě
                        break
                    self.pools.remove(pool)
                    self._rr = 0
                    continue

            track = pool.tracks.popleft()
            reason = self._reject_reason(track, (taken or []) + out, blocked, recent, artist_counts)
            if reason:
                rejected[reason] = rejected.get(reason, 0) + 1
                if self.allow_long and self._acceptable(
                    track, out, blocked, recent, artist_counts, self._long_limit()
                ):
                    long_ones.append(track)
                continue

            self.session_seen.add(track.id)
            self._batch.add(track.id)
            artist_counts[track.artist] = artist_counts.get(track.artist, 0) + 1
            out.append(track)
            if self.favourites:
                self._fav_played.add(track.id)
                self._fav_last.append(track.id)
                self._fav_given[track.id] = time.time()
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
        if self.album:
            return list(self._album_artists)
        return [a.strip() for a in self.artist.split(",") if a.strip()] if self.artist else []

    def _long_limit(self) -> int:
        """Strop pro vyžádaného interpreta — nikdy pod tím obvyklým."""
        return max(self.cfg.max_duration, self.cfg.max_duration_request)

    def _acceptable(
        self,
        track: Track,
        pending: list[Track],
        blocked: set[str],
        recent: Any,
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
        recent: Any,
        artist_counts: dict[str, int],
        max_duration: int | None = None,
    ) -> str | None:
        """Proč skladba do fronty nejde (klíč do statistiky radio.pool), None = jde.

        `recent` = pravidlo neopakování (RepeatGuard; None = zvednuté přáním,
        které chtělo i už hrané; množina id videí jen ze starších volání)."""
        if track.id in blocked:
            return "blacklisted"
        votes = getattr(self, "votes", None)
        if votes is not None:
            # v režimu interpreta si ho někdo vyžádal jménem — jeho vlastní
            # vyřazení neplatí, vyřazené skladby ano
            why = votes.pool_reject(track, self._focus_names())
            if why:
                return why
        if self.album:
            # album je vyžádané jménem celé a v pořadí: žádný další filtr
            # (délka, strop na interpreta, explicitní) — jen pravidlo
            # neopakování: co z něj tu ve lhůtě hrálo, se vynechá
            return "recent" if _is_recent(recent, track) else None
        if self.video_only and not self.artist and not self.favourites \
                and track.video_type != "MUSIC_VIDEO_TYPE_OMV":
            # přání "i s obrazem": do podkresu jen skladby, které samy jsou klip
            return "no_video"
        if track.explicit and not self.artist and not (self.allow_explicit or self.explicit_ok):
            # vulgární texty do podkresu ne; vyžádaný interpret / skladba jménem
            # ano — a taky přání, které si o vulgární texty výslovně řeklo
            # (explicit_ok, jen do dalšího přání)
            return "explicit"
        if track.id in self.session_seen:
            return "session_seen"
        if _is_recent(recent, track) and not (self.favourites and track.id in self.fav_again):
            # i oblíbená kanceláře v rádiu (dřív směla i dřív — zrušeno
            # 12. 10. 2026); v režimu oblíbených jen kolo dokola (16. 10.)
            return "recent"
        if any(t.id == track.id for t in pending):
            return "duplicate"
        if pending and _same_song(track, pending):
            return "duplicate"  # jiné nahrání písně, která už v téhle dávce je
        if track.duration is not None:
            ceiling = max_duration or self.cfg.max_duration
            if track.duration < self.cfg.min_duration:
                return "too_short"
            if track.duration > ceiling:
                return "too_long"
        # at most 2 tracks by the same artist per refill — kromě režimu
        # interpreta, kde je to celý smysl
        if not self.artist and not self.favourites and artist_counts.get(track.artist, 0) >= 2:
            return "artist_cap"
        return None

    async def _refill(self, pool: Pool) -> None:
        """Reseeds from the last track not skipped — implicit feedback."""
        if self.favourites:
            self._refill_favourites(pool)
            return
        if self.album:
            return  # album se nedoplňuje — co zbývá, je v poolu; pak "a podobné"
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
        self.generation += 1
        log.debug("pool %s doplněn o %d", pool.seed.label(), len(new))

    def _refill_artist(self, pool: Pool, history: list | None = None,
                       blocked: set[str] | None = None) -> None:
        """Režim interpreta se nedoplňuje z rádia (to by uteklo jinam), ale
        jeho dalšími skladbami, které ve lhůtě neopakování nehrály; když
        žádné nezbývají, končí (`_dry`). Jen s `repeat_ok` se jede dokola."""
        if blocked is None:
            blocked = self.store.blacklisted()
        skip = {t.id for t in pool.tracks} | blocked
        fresh = [
            t for t in self._artist_rotation(self._artist_all, history)
            if t.id not in self.session_seen and t.id not in skip
        ]
        fresh = self._unplayed(fresh)
        if not fresh and not pool.tracks:
            # Projeto. Znovu smí jen to, co ve lhůtě neopakování nehrálo
            # (vydané, ale nezahrané — třeba vyřazené z fronty); když nic
            # takového není, režim končí a podkres přejde na "<interpret>
            # a podobné" (vlastník 12. 10. 2026: neopakovat — dřív se jelo
            # od začátku). Co už je v právě vydávané dávce, se nevrací (dřív
            # se tu pool protáčel naprázdno: Pi 12. 10., 440 pokusů za 1,5 s).
            again = self._unplayed(self._artist_rotation(self._artist_all, history))
            if not again:
                self._dry = True
                return
            again = [t for t in again if t.id not in self._batch and t.id not in blocked]
            if not again:
                return  # všechno nehrané už je v téhle dávce — pro teď hotovo
            log.info("interpret %s dohrán, jedu jeho skladby znovu", self.artist)
            telemetry.event("radio.artist_mode", artist=self.artist, restart=True,
                            n=len(self._artist_all), unplayed=len(again))
            self.session_seen -= {t.id for t in self._artist_all} - self._batch
            fresh = again
        pool.tracks.extend(fresh)
        self.generation += 1

    # ---- feedback ----

    def give_back(self, tracks: list[Track]) -> None:
        """Skladby vydané next_tracks, které se nakonec do fronty nedostaly
        (plnič je zahodil, protože mezitím přišel nový tah) — nepočítat je
        jako zahrané, ať o ně režim interpreta nepřijde."""
        if self.album and self.pools:
            # zpátky na své místo v pořadí alba
            back = {t.id for t in tracks} | {t.id for t in self.pools[0].tracks}
            self.pools[0].tracks = deque(
                t for t in self._artist_all if t.id in back and t.id not in self._album_done)
        elif self.favourites and self.pools:
            # oblíbené dokola: zpátky na začátek (jsou nejdéle nehrané, jinak by
            # vypadly z kola a vrátily se až v dalším)
            inside = {t.id for t in self.pools[0].tracks}
            self.pools[0].tracks.extendleft(reversed([t for t in tracks if t.id not in inside]))
        for t in tracks:
            self.session_seen.discard(t.id)
            self._fav_played.discard(t.id)
            self._fav_given.pop(t.id, None)
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
            return False  # dojde-li, přejde na "… a podobné" — viz next_tracks
        if self.favourites and self.pools:
            return False  # oblíbené nedojdou — jedou dokola (_fav_cycle)
        return not self.pools or all(len(p) == 0 for p in self.pools)

    def describe(self) -> str:
        if self.album and self.pools:
            return f"album {self.album} (zbývá {len(self.pools[0])} skladeb)"
        if self.artist and self.pools:
            return f"interpret {self.artist} (v zásobě {len(self.pools[0])} skladeb)"
        if self.favourites and self.pools:
            return f"{self.mood} napřeskáčku (v zásobě {len(self.pools[0])} skladeb)"
        if not self.pools:
            return "žádné aktivní seedy"
        return ", ".join(f"{p.seed.label()} ({len(p)})" for p in self.pools)
