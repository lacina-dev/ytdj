"""Pravidlo neopakování: co tu hrálo v posledních `repeat_days` dnech.

Jedno místo, přes které jde všechno, co se má samo zařadit do fronty — podkres
(rádio, režim interpreta, album, oblíbené; `RadioPools`) i sady z přání
(interpret, album, oblíbené, výběr DJe; `WishQueue._drop_repeats`). Vlastník
12. 10. 2026 (hlášení z jukeboxu #32): „aby agent neopakoval písničky co
nebyly hrané před nastavenou lhůtou. Jediná výjimka může být výslovné přání na
konkrétní písničku nebo na opakování již hraného."

Co se počítá jako „hrála": všechno, co opravdu začalo hrát (tabulka `plays`,
kromě toho, co se přehrát nepovedlo) — ať to zařadil kdokoli a ať to dohrálo,
nebo to někdo přeskočil. Jiné nahrání téže písně je tatáž píseň: vedle id
videa se porovnává klíč skladby z hlasování (`votes.song_keys`: interpret +
název bez verze, pro každého uvedeného interpreta).

Paměť se čte z databáze jednou (ve vlákně, ne v event loopu — SD karta je
pomalá) a dál se jen doplňuje z `Store.play_log`, kam se zapíše každý start
skladby. Znovu se načte, jen když se lhůta v nastavení prodlouží nebo když se
skladba přehrát nepovedla.
"""

from __future__ import annotations

import time
from typing import Any, Iterable

DAY = 86400.0


def period_text(days: int) -> str:
    """ "v posledních 5 dnech" — do odpovědí DJe."""
    return "za poslední den" if days == 1 else f"v posledních {days} dnech"


def _tat(track: Any) -> tuple[str, str, str]:
    from ..votes import _tat as tat  # až při volání: votes importuje music.match

    return tat(track)


def song_keys(artist: str, title: str) -> list[str]:
    """Klíče téže písně (jiné nahrání, jiný uvedený interpret); bez názvu žádné."""
    from ..votes import song_keys as keys

    if not (title or "").strip():
        return []
    return [k for k in keys(artist or "", title) if not k.endswith("|") and not k.startswith("|")]


class RepeatGuard:
    """Kdy která píseň naposledy hrála — a jestli je to ještě ve lhůtě."""

    def __init__(self, store: Any, cfg: Any) -> None:
        self.store = store
        self.cfg = cfg
        self._ids: dict[str, float] = {}
        self._keys: dict[str, float] = {}
        self._from: float | None = None  # od kdy je paměť načtená z databáze
        self._rev = -1  # Store.play_rev při načtení
        self._seen = 0  # kolik záznamů Store.play_log už je v paměti
        self.loaded = False

    @property
    def days(self) -> int:
        try:
            return max(0, int(getattr(self.cfg, "repeat_days", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def period(self) -> str:
        return period_text(self.days)

    # ---- načtení ----

    def _build(self, cutoff: float) -> tuple[dict[str, float], dict[str, float]]:
        """Paměť z databáze (běží ve vlákně): id videa i klíče písní → poslední start."""
        log = list(getattr(self.store, "play_log", ()) or ())
        failed = {(r[0], r[3]) for r in log if not r[4]}  # zápis chyby může teprve čekat
        ids: dict[str, float] = {}
        keys: dict[str, float] = {}
        for vid, title, artist, ts in self.store.played_since(cutoff):
            if (vid, ts) in failed:
                continue
            _put(ids, keys, vid, artist, title, ts)
        return ids, keys

    async def refresh(self) -> None:
        """Před každým dotazem: doplnit, co mezitím začalo hrát. Z databáze čte
        jen poprvé (a po změně lhůty / nepovedené skladbě), vždy mimo event loop."""
        days = self.days
        store = self.store
        if days <= 0 or store is None:
            self.loaded = True
            return
        now = time.time()
        cutoff = now - days * DAY
        if not hasattr(store, "played_since"):
            # atrapa nebo starý Store: jen id videí (bez "téže písně")
            read = getattr(store, "recently_played", None)
            if read is not None:
                aread = getattr(store, "aread", None)
                got = await aread("recently_played", days) if aread is not None else read(days)
                self._ids, self._keys = {v: now for v in got}, {}
            self.loaded = True
            return
        rev = getattr(store, "play_rev", 0)
        if self._from is None or cutoff < self._from - 1 or rev != self._rev:
            aread = getattr(store, "aread", None)
            if aread is not None:
                self._ids, self._keys = await aread(self._build, cutoff)
            else:
                self._ids, self._keys = self._build(cutoff)
            self._from, self._rev, self._seen = cutoff, rev, 0
        self._merge()
        self.loaded = True

    def _merge(self) -> None:
        """Co začalo hrát od posledního dotazu (Store.play_log) — jen paměť."""
        log = getattr(self.store, "play_log", None) or []
        if self._seen > len(log):
            self._seen = 0
        for vid, title, artist, ts, ok in log[self._seen:]:
            if ok:
                _put(self._ids, self._keys, vid, artist, title, ts)
        self._seen = len(log)

    # ---- dotazy (jen paměť, bez čekání) ----

    def last_played(self, track: Any, now: float | None = None) -> float | None:
        """Kdy tahle píseň (i v jiném nahrání) naposledy začala hrát, je-li to
        ve lhůtě; jinak None."""
        days = self.days
        if days <= 0:
            return None
        if self._from is not None:
            self._merge()  # i bez refresh(): co mezitím začalo hrát
        vid, artist, title = _tat(track)
        ts = self._ids.get(vid, 0.0)
        for k in song_keys(artist, title):
            ts = max(ts, self._keys.get(k, 0.0))
        now = time.time() if now is None else now
        return ts if ts > now - days * DAY else None

    def recent(self, track: Any) -> bool:
        return self.last_played(track) is not None

    def split(self, tracks: Iterable[Any]) -> tuple[list, list]:
        """(co ve lhůtě nehrálo, co hrálo) — v původním pořadí."""
        fresh, played = [], []
        now = time.time()
        for t in tracks:
            (played if self.last_played(t, now) is not None else fresh).append(t)
        return fresh, played


def _put(ids: dict[str, float], keys: dict[str, float], vid: str, artist: str, title: str,
         ts: float) -> None:
    if vid and ts > ids.get(vid, 0.0):
        ids[vid] = ts
    for k in song_keys(artist, title):
        if ts > keys.get(k, 0.0):
            keys[k] = ts


def one_per_song(tracks: Iterable[Any]) -> list:
    """Každá píseň jednou — další nahrání téže písně (živák, remaster, jiné
    video) vypadnou. Pro sady, kde jde o písně, ne o nahrávky (interpret)."""
    out: list = []
    seen: set[str] = set()
    for t in tracks:
        vid, artist, title = _tat(t)
        ks = set(song_keys(artist, title)) | ({f"id:{vid}"} if vid else set())
        if ks & seen:
            continue
        seen |= ks
        out.append(t)
    return out
