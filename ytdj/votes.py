"""Hlasování kanceláře: 👍/👎 skladbám i celým interpretům (PLAN H).

Kdo hlasuje: id klienta (`Wish.key` — prohlížeč, relace displeje), jméno je
jen popisek (přezdívka z `nicks.py`, jinak to, co poslal). Jeden hlas na
člověka a cíl; změnit ho i stáhnout jde kdykoli. Hlasy nestárnou.

Cíle a klíče (jiné nahrání / verze téže písně = tatáž píseň):

    skladba    "<interpret>|<název>" — interpret bez diakritiky a mezer
               (první uvedený), název bez závorek, "feat." a značek verze
               (match.split_title): "Kabát — Malá dáma (Official Video)" i
               "Kabát - Topic — Malá dáma [Live]" → "kabat|mala dama".
               Shoda platí pro kteréhokoli uvedeného interpreta ("A & B").
    interpret  jméno bez diakritiky a mezer ("the " a "- Topic" pryč); sedí
               na každého uvedeného v "A, B & C" / "feat. D".

Stav se neukládá, počítá se z hlasů (config `ban_song_votes`,
`ban_artist_votes`, `favourite_artist_votes`):

    skladba    vyřazená     aspoň ban_song_votes (2) lidí 👎 a víc 👎 než 👍
               oblíbená     aspoň jeden 👍 a víc 👍 než 👎
               upozaděná    víc 👎 než 👍, ale na vyřazení to nestačí
    interpret  vyřazený     aspoň ban_artist_votes (3) lidí 👎 a víc 👎 než 👍
               oblíbený     aspoň favourite_artist_votes (2) lidí 👍 a víc 👍 než 👎
                            (jeden 👍 celého interpreta z něj oblíbeného neudělá)
               čeká         nějaké 👎, ale na vyřazení to nestačí (jen 👍 pod prahem = neutrální)

Co to dělá s hudbou (radio.py, codex.py, enforce() níže): podkres (pooly)
vyřazené nikdy nevydá, upozaděné jen napůl, oblíbené (skladby i skladby
oblíbených interpretů) smí zase hrát i dřív než po `repeat_days` a v poolu
se posunou dopředu (boost_pools). "Pusť oblíbené" hraje oblíbené skladby
i známé skladby oblíbených interpretů (favourite_mix). Výslovné přání vyřazené skladby / interpreta se
splní — s poznámkou, kdo ji vyřadil. Při vyřazení zmizí z fronty jen
podkres; hraje-li zrovna podkres, přeskočí se.

Import playlistu (ytdj/imports.py): playlist člověka = jeho 👍 každé
písničce v něm (Ballot.src = id importu). Takové 👍 se neukládají do `votes`,
počítají se z importu (`_resync`): vlastní 👍/👎 člověka má vždycky přednost,
vlastní stažení jen když je novější než písnička v importu. Import dává 👍
jen skladbám, nikdy celému interpretovi.

Férovost oblíbených kanceláře (`fair_order`): "pusť oblíbené" i výběr pro DJ
berou oblíbené po lidech na střídačku — každý, kdo dal 👍, přispěje zhruba
stejným dílem, ať má 5 oblíbených, nebo playlist o 300 písničkách; u každého
napřed nejvíc 👍 kanceláře a jeho vlastní 👍 před písničkami z playlistu.
V poolech podkresu se písničky, které drží jen playlisty, posunou dopředu
nejvýš IMPORT_LIFT za člověka na jedno doplnění.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
import time
from collections import deque
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable, Iterable

from . import telemetry
from .music.match import norm, split_title

log = logging.getLogger(__name__)

SONG, ARTIST = "song", "artist"
TARGETS = (SONG, ARTIST)
RATE_MAX = 30  # hlasů na člověka…
RATE_WINDOW = 600.0  # …za tolik sekund
DOWNWEIGHT_PASS = 0.5  # pravděpodobnost, že upozaděná skladba projde do podkresu
BOOST_LIFT = 8  # o kolik míst v poolu se posune oblíbená (jednou za skladbu)
IMPORT_LIFT = 2  # …z toho skladeb, které drží jen playlisty, nejvýš tolik za člověka na doplnění
ARTIST_MIX = 3  # "pusť oblíbené": kolik známých skladeb od každého oblíbeného interpreta
MIX_TIMEOUT = 6.0  # s — katalog pro oblíbené interprety; pak jen oblíbené skladby
LIST_MAX = 100  # nejvýš tolik položek v jednom seznamu API
WHO_MAX = 24

# stavy (API i telemetrie)
BANNED, FAVOURITE, DOWN, PENDING, NEUTRAL = "banned", "favourite", "downweighted", "pending", "neutral"


class VoteError(ValueError):
    """Hlas neprošel — text je věta pro člověka, `status` HTTP kód."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------
# klíče — čisté funkce
# --------------------------------------------------------------------------

# jisté oddělovače (výčet, host) a ty, co bývají i uvnitř jména kapely
# ("Simon & Garfunkel") — u nich se bere celé jméno i jeho části
_SPLIT = re.compile(r"\s*(?:,|;|\bfeat\.?|\bft\.?|\bfeaturing\b|\bx\b|\bvs\.?)\s*", re.I)
_SPLIT_AMP = re.compile(r"\s*(?:&|\+|/)\s*")
_CHANNEL = re.compile(r"\s*(?:topic|vevo|official)$")


@lru_cache(maxsize=2048)
def artist_key(name: str) -> str:
    """ "The Beatles" / "Beatles - Topic" / "BEATLES" → "beatles"."""
    k = _CHANNEL.sub("", norm(name or ""))
    k = re.sub(r"^the ", "", k)
    return k.replace(" ", "")


@lru_cache(maxsize=2048)
def credits(artist: str) -> tuple[str, ...]:
    """ "A, B & C feat. D" → ("A", "B & C", "B", "C", "D") — v původním zápisu,
    bez duplicit. První je hlavní interpret ("Simon & Garfunkel" celý)."""
    out: list[str] = []
    seen: set[str] = set()

    def add(part: str) -> None:
        part = re.sub(r"\s*-\s*topic$", "", " ".join(part.split()), flags=re.I).strip(" -")
        k = artist_key(part)
        if part and k and k not in seen:
            seen.add(k)
            out.append(part)

    for part in _SPLIT.split(artist or ""):
        add(part)
        subs = _SPLIT_AMP.split(part)
        if len(subs) > 1:
            for sub in subs:
                add(sub)
    return tuple(out)


@lru_cache(maxsize=2048)
def artist_keys(artist: str) -> frozenset[str]:
    """Klíče všech uvedených interpretů (i celého zápisu — "Mňága a Žďorp")."""
    keys = {artist_key(p) for p in credits(artist)}
    whole = artist_key(artist)
    if whole:
        keys.add(whole)
    keys.discard("")
    return frozenset(keys)


@lru_cache(maxsize=2048)
def title_key(title: str, artist: str = "") -> str:
    """Název bez verze: "Malá dáma (Live) - Remastered 2011" → "mala dama".

    Video nahrané jako "Kabát - Malá dáma" (interpret v názvu) se bere podle
    části za pomlčkou, když ta před ní je interpret skladby.
    """
    title = title or ""
    parts = re.split(r"\s+[-–—]\s+", title, maxsplit=1)
    if len(parts) == 2 and artist_key(parts[0]) and artist_key(parts[0]) in artist_keys(artist):
        title = parts[1]
    base, _ = split_title(title)
    return norm(base) or norm(title)


def song_key(artist: str, title: str) -> str:
    """Kanonický klíč skladby podle prvního uvedeného interpreta ("Olympic &
    Petr Janda" i samotný "Olympic" → "olympic|…")."""
    first = [p for p in _SPLIT_AMP.split(credits(artist)[0]) if artist_key(p)] \
        if credits(artist) else []
    return f"{artist_key(first[0] if first else artist)}|{title_key(title, artist)}"


@lru_cache(maxsize=2048)
def song_keys(artist: str, title: str) -> frozenset[str]:
    """Všechny klíče, pod kterými tahle skladba může být (každý interpret)."""
    t = title_key(title, artist)
    return frozenset(f"{a}|{t}" for a in artist_keys(artist))


def _tat(track: Any) -> tuple[str, str, str]:
    """(video_id, artist, title) z Track i ze slovníku stavu webu."""
    if isinstance(track, dict):
        return (str(track.get("id") or track.get("video_id") or ""),
                str(track.get("artist") or ""), str(track.get("title") or ""))
    return (str(getattr(track, "id", "") or ""), str(getattr(track, "artist", "") or ""),
            str(getattr(track, "title", "") or ""))


# --------------------------------------------------------------------------
# hlasy v paměti
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Ballot:
    voter: str
    vote: int  # 1 | -1 | 0 (staženo)
    who: str
    ts: float
    video_id: str = ""
    artist: str = ""
    title: str = ""
    src: str = ""  # "" = vlastní hlas; jinak id importu playlistu (👍 z něj)


@dataclass(slots=True)
class ImportItem:
    key: str
    video_id: str
    artist: str
    title: str
    added: float  # kdy se písnička v importu objevila (novější vlastní stažení má přednost)
    pos: int = 0


@dataclass(slots=True)
class PlaylistImport:
    """Import playlistu jednoho člověka: jeho 👍 každé písničce v `items`."""
    id: str
    client: str
    who: str
    playlist_id: str
    title: str
    created: float
    fetched: float
    total: int = 0
    skipped: int = 0
    cut: int = 0
    items: dict = field(default_factory=dict)  # klíč → ImportItem, v pořadí playlistu

    def meta(self) -> tuple:
        return (self.id, self.client, self.who, self.playlist_id, self.title, self.created,
                self.fetched, self.total, self.skipped, self.cut)

    def rows(self) -> list[tuple]:
        return [(it.key, it.video_id, it.artist, it.title, it.added, it.pos)
                for it in self.items.values()]


@dataclass(slots=True)
class Tally:
    up: int
    down: int
    status: str
    need: int  # kolik 👎 ještě chybí k vyřazení (0 = vyřazeno)


@dataclass(slots=True)
class CastResult:
    target: str
    key: str
    vote: int
    prev: int
    before: str
    after: str
    item: dict

    @property
    def changed(self) -> str | None:
        """ "ban" / "unban" — přechod přes práh vyřazení; None = beze změny."""
        if self.after == BANNED and self.before != BANNED:
            return "ban"
        if self.before == BANNED and self.after != BANNED:
            return "unban"
        return None


class _Index:
    __slots__ = ("sig", "banned_songs", "fav_songs", "down_songs", "banned_artists",
                 "fav_artists", "by_vid")

    def __init__(self, sig: tuple) -> None:
        self.sig = sig
        self.banned_songs: set[str] = set()
        self.fav_songs: set[str] = set()
        self.down_songs: set[str] = set()
        self.banned_artists: set[str] = set()
        self.fav_artists: set[str] = set()
        self.by_vid: dict[str, str] = {}  # videoId → klíč skladby


class VoteBook:
    """Všechny hlasy v paměti (kancelář = stovky řádků); zápis přes Store.

    `label(voter, stored_who)` vrací jméno k zobrazení (přezdívka z registru,
    přes kancelářský filtr) — dodá ho aplikace; bez něj uložené jméno přes
    `censor`.
    """

    def __init__(self, store: Any = None, cfg: Any = None,
                 label: Callable[[str, str], str] | None = None,
                 censor: Any = None, rng: Callable[[], float] = random.random,
                 clock: Callable[[], float] = time.time,
                 mono: Callable[[], float] = time.monotonic) -> None:
        self.store = store
        self.cfg = cfg
        self.label_fn = label
        self.censor = censor
        self.rng = rng
        self.clock = clock
        self.mono = mono
        self.items: dict[tuple[str, str], dict[str, Ballot]] = {}
        self._version = 0
        self._index: _Index | None = None
        self._rate: dict[str, deque[float]] = {}
        self._lifted: set[str] = set()  # videoId, které boost_pools už posunul
        # importy playlistů (ytdj/imports.py): id → import; klient → jeho importy
        self.imports: dict[str, PlaylistImport] = {}
        self._imports_by: dict[str, list[PlaylistImport]] = {}
        self.prng = random.Random()  # pořadí oblíbených (testy si ho osadí)
        self._pool_sig: tuple = ()
        # RadioPools (zapojí wire): pool_reject, který plnič volá u každé
        # skladby, přitom posune oblíbené v poolech dopředu — radio.py nic
        # dalšího volat nemusí
        self.pools: Any = None
        self.loaded = False

    # ---- načtení ----

    def load(self, rows: Iterable[tuple] | None = None) -> "VoteBook":
        """Řádky `Store.all_votes()` (bez argumentu je přečte samo — synchronně)."""
        if rows is None:
            rows = self.store.all_votes() if self.store is not None else []
        for target, key, voter, vote, who, vid, artist, title, ts in rows:
            if target in TARGETS and key and voter:
                ballots = self.items.setdefault((target, key), {})
                cur = ballots.get(voter)
                if cur is not None and cur.ts > float(ts):
                    continue  # hlas z doby, kdy se ještě načítalo, je novější
                ballots[voter] = Ballot(
                    voter, int(vote), who or "", float(ts), vid or "", artist or "", title or "")
        self.loaded = True
        self._version += 1
        return self

    async def aload(self) -> "VoteBook":
        """Načtení mimo event loop (SD karta) — hlasy i importy playlistů."""
        aread = getattr(self.store, "aread", None)
        rows = await aread("all_votes") if aread is not None else None
        self.load(rows)
        if aread is not None and hasattr(self.store, "all_imports"):
            try:
                meta, items = await aread("all_imports")
            except Exception:
                log.exception("importy playlistů se nenačetly")
            else:
                self.load_imports(meta, items)
        return self

    def load_imports(self, meta: Iterable[tuple], items: Iterable[tuple]) -> "VoteBook":
        """Řádky `Store.all_imports()` → importy a jejich 👍 (po `load`)."""
        by_id: dict[str, PlaylistImport] = {}
        for iid, client, who, pid, title, created, fetched, total, skipped, cut in meta:
            if iid in self.imports or not client:
                continue  # import z doby načítání je novější
            by_id[iid] = PlaylistImport(iid, client, who or "", pid, title or "", float(created),
                                        float(fetched), int(total or 0), int(skipped or 0),
                                        int(cut or 0))
        for iid, key, vid, artist, title, added, pos in items:
            imp = by_id.get(iid)
            if imp is not None and key and key not in imp.items:
                imp.items[key] = ImportItem(key, vid or "", artist or "", title or "",
                                            float(added), int(pos or 0))
        for imp in by_id.values():
            self.put_import(imp)
        return self

    # ---- importy playlistů: 👍 člověka každé písničce z jeho playlistu ----

    def imports_of(self, voter: str) -> list[PlaylistImport]:
        return list(self._imports_by.get(voter, ()))

    def put_import(self, imp: PlaylistImport) -> set[str]:
        """Přidá nebo nahradí import (stejné id) a přepočítá 👍 toho člověka.
        Vrací klíče, kterých se to týkalo."""
        old = self.imports.get(imp.id)
        keys = set(imp.items) | (set(old.items) if old is not None else set())
        self.imports[imp.id] = imp
        lst = [i for i in self._imports_by.get(imp.client, []) if i.id != imp.id] + [imp]
        lst.sort(key=lambda i: (i.created, i.id))
        self._imports_by[imp.client] = lst
        self._resync(imp.client, keys)
        return keys

    def drop_import(self, import_id: str) -> PlaylistImport | None:
        imp = self.imports.pop(import_id, None)
        if imp is None:
            return None
        lst = [i for i in self._imports_by.get(imp.client, []) if i.id != import_id]
        if lst:
            self._imports_by[imp.client] = lst
        else:
            self._imports_by.pop(imp.client, None)
        self._resync(imp.client, set(imp.items))
        return imp

    def _import_ballot(self, voter: str, key: str) -> Ballot | None:
        """👍 z prvního (nejstaršího) importu člověka, který písničku má."""
        for imp in self._imports_by.get(voter, ()):
            it = imp.items.get(key)
            if it is not None:
                return Ballot(voter, 1, imp.who, it.added, it.video_id, it.artist, it.title,
                              src=imp.id)
        return None

    def _resync(self, voter: str, keys: Iterable[str]) -> None:
        """👍 z importů pro tyhle klíče znovu: vlastní 👍/👎 vyhrává vždy,
        vlastní stažení jen když je novější než písnička v importu."""
        for key in keys:
            ballots = self.items.get((SONG, key))
            cur = ballots.get(voter) if ballots else None
            imp_b = self._import_ballot(voter, key)
            if cur is not None and not cur.src and (cur.vote != 0 or imp_b is None
                                                    or cur.ts >= imp_b.ts):
                continue  # vlastní hlas platí
            if imp_b is not None:
                self.items.setdefault((SONG, key), {})[voter] = imp_b
            elif cur is not None and ballots is not None:
                del ballots[voter]
                if not ballots:
                    del self.items[(SONG, key)]
        self._version += 1

    def import_keys(self, voter: str, exclude: str = "") -> set[str]:
        """Klíče písniček ze všech importů člověka (bez importu `exclude`)."""
        keys: set[str] = set()
        for imp in self._imports_by.get(voter, ()):
            if imp.id != exclude:
                keys |= set(imp.items)
        return keys

    def import_active(self, imp: PlaylistImport) -> int:
        """Kolik 👍 z importu opravdu platí (ne přebité vlastním hlasem ani
        starším importem téhož člověka)."""
        n = 0
        for key in imp.items:
            b = self.items.get((SONG, key), {}).get(imp.client)
            if b is not None and b.src == imp.id:
                n += 1
        return n

    # ---- prahy a stav ----

    def thresholds(self) -> tuple[int, int]:
        song = max(1, int(getattr(self.cfg, "ban_song_votes", 2) or 2))
        artist = max(1, int(getattr(self.cfg, "ban_artist_votes", 3) or 3))
        return song, artist

    def tally(self, target: str, key: str) -> Tally:
        ballots = self.items.get((target, key), {})
        up = sum(1 for b in ballots.values() if b.vote > 0)
        down = sum(1 for b in ballots.values() if b.vote < 0)
        return self._status(target, up, down)

    def fav_artist_threshold(self) -> int:
        """Kolik různých lidí musí dát 👍 celému interpretovi, aby byl oblíbený."""
        return max(1, int(getattr(self.cfg, "favourite_artist_votes", 2) or 2))

    def rules(self) -> dict:
        """Prahy pro web (stránka Hlasování skládá pravidla z nich)."""
        song_t, artist_t = self.thresholds()
        return {"ban_song_votes": song_t, "ban_artist_votes": artist_t,
                "favourite_artist_votes": self.fav_artist_threshold()}

    def _status(self, target: str, up: int, down: int) -> Tally:
        song_t, artist_t = self.thresholds()
        if target == ARTIST:
            if down >= artist_t and down > up:
                return Tally(up, down, BANNED, 0)
            need = max(artist_t - down, up - down + 1, 1)
            # Celý interpret je oblíbený, až když to řekne víc lidí: jeden 👍
            # by jinak posouval všechny jeho skladby v podkresu (rozhodnutí
            # vlastníka 26. 9.). Písnička stačí jedním 👍.
            if up >= self.fav_artist_threshold() and up > down:
                return Tally(up, down, FAVOURITE, need)
            # (jeden 👍 bez 👎 = "neutral": ve výpisu mezi čekajícími, na webu ne jako 👎)
            return Tally(up, down, PENDING if down else NEUTRAL, need)
        if down >= song_t and down > up:
            return Tally(up, down, BANNED, 0)
        need = max(song_t - down, up - down + 1, 1)
        if up >= 1 and up > down:
            return Tally(up, down, FAVOURITE, need)
        return Tally(up, down, DOWN if down > up else NEUTRAL, need)

    def _idx(self) -> _Index:
        sig = (self._version, self.thresholds(), self.fav_artist_threshold())
        if self._index is not None and self._index.sig == sig:
            return self._index
        idx = _Index(sig)
        for (target, key), ballots in self.items.items():
            t = self.tally(target, key)
            if target == ARTIST:
                if t.status == BANNED:
                    idx.banned_artists.add(key)
                elif t.status == FAVOURITE:
                    idx.fav_artists.add(key)
                continue
            if t.status == BANNED:
                idx.banned_songs.add(key)
            elif t.status == FAVOURITE:
                idx.fav_songs.add(key)
            elif t.status == DOWN:
                idx.down_songs.add(key)
            for b in ballots.values():
                if b.video_id:
                    idx.by_vid[b.video_id] = key
        self._index = idx
        return idx

    # ---- skladba → klíč ----

    def song_key_for(self, track: Any) -> str:
        """Klíč, pod kterým se o skladbě hlasuje: ten, kde už hlasy jsou
        (jiné nahrání, jiný první interpret), jinak kanonický."""
        vid, artist, title = _tat(track)
        cands = set(song_keys(artist, title)) if (artist or title) else set()
        known = self._idx().by_vid.get(vid) if vid else None
        if known:
            cands.add(known)
        best = max(
            (k for k in cands if (SONG, k) in self.items),
            key=lambda k: (sum(1 for b in self.items[(SONG, k)].values() if b.vote), k == known),
            default=None,
        )
        return best or known or song_key(artist, title)

    def _song_hits(self, track: Any) -> set[str]:
        vid, artist, title = _tat(track)
        keys = set(song_keys(artist, title)) if (artist or title) else set()
        known = self._idx().by_vid.get(vid) if vid else None
        if known:
            keys.add(known)
        return keys

    # ---- filtr pro podkres a výslovná přání ----

    def banned_artists_of(self, track: Any, exempt: Iterable[str] = ()) -> list[str]:
        """Vyřazení interpreti mezi uvedenými u skladby (bez `exempt`)."""
        idx = self._idx()
        if not idx.banned_artists:
            return []
        _, artist, _ = _tat(track)
        ex: set[str] = set()
        for name in exempt:
            ex |= artist_keys(name)
        return [k for k in artist_keys(artist) if k in idx.banned_artists and k not in ex]

    def banned_song(self, track: Any) -> str | None:
        """Klíč vyřazené skladby, když jí tahle je (jakákoli verze), jinak None."""
        idx = self._idx()
        if not idx.banned_songs:
            return None
        return next(iter(sorted(self._song_hits(track) & idx.banned_songs)), None)

    def blocked(self, track: Any, exempt: Iterable[str] = ()) -> str | None:
        """ "song" / "artist" = do podkresu ne; None = smí."""
        if not self.items:
            return None
        if self.banned_song(track):
            return SONG
        if self.banned_artists_of(track, exempt):
            return ARTIST
        return None

    def pool_reject(self, track: Any, exempt: Iterable[str] = ()) -> str | None:
        """Důvod do statistiky radio.pool: "voted_out" | "downweighted" | None."""
        if not self.items:
            return None
        self._maybe_boost()
        if self.blocked(track, exempt):
            return "voted_out"
        idx = self._idx()
        if idx.down_songs and self._song_hits(track) & idx.down_songs \
                and self.rng() >= DOWNWEIGHT_PASS:
            return "downweighted"
        return None

    def is_favourite(self, track: Any) -> bool:
        """Oblíbená skladba, nebo skladba oblíbeného interpreta (a nevyřazená)."""
        idx = self._idx()
        if idx.fav_songs and self._song_hits(track) & idx.fav_songs:
            return True
        if idx.fav_artists:
            _, artist, _ = _tat(track)
            return bool(artist_keys(artist) & idx.fav_artists) and self.blocked(track) is None
        return False

    def import_backers(self, track: Any) -> set[str]:
        """Lidé, jejichž 👍 z playlistu drží oblíbenou skladbu — prázdné, když
        ji drží vlastní 👍 někoho nebo oblíbený interpret (pak platí vše jako dřív)."""
        if not self.imports:
            return set()
        idx = self._idx()
        if idx.fav_artists:
            _, artist, _ = _tat(track)
            if artist_keys(artist) & idx.fav_artists:
                return set()
        backers: set[str] = set()
        for key in self._song_hits(track) & idx.fav_songs:
            for b in self.items.get((SONG, key), {}).values():
                if b.vote > 0:
                    if not b.src:
                        return set()
                    backers.add(b.voter)
        return backers

    def _maybe_boost(self) -> None:
        """boost_pools, jen když se pooly od minula doplnily (nové seedy, doplnění
        rádiem) nebo se změnily hlasy — ne po každé vydané skladbě.

        Otisk poolu = on sám a jeho poslední skladba: vydání skladby (popleft)
        ho nemění, doplnění (extend) a nové pooly ano. Dřív byla v otisku i
        délka, takže se boost počítal nad všemi pooly u každé zvažované
        skladby (~1 ms tady, na Pi 3 ~10 ms v event loopu)."""
        pools = getattr(self.pools, "pools", None)
        if not pools or getattr(self.pools, "favourites", ""):
            return  # v režimu oblíbených je všechno oblíbené a pořadí je napřeskáčku
        if self._pool_sig == self._sig(pools):
            return
        self.boost_pools(pools)
        self._pool_sig = self._sig(pools)  # po přesunech (poslední se mohla posunout)

    def _sig(self, pools: Any) -> tuple:
        return (getattr(self.pools, "generation", None), self._version) + tuple(
            (id(p), p.tracks[-1].id if p.tracks else "") for p in pools
            if getattr(p, "tracks", None) is not None)

    def boost_pools(self, pools: Iterable[Any]) -> int:
        """Oblíbené (skladby i interpreti) v poolech dopředu — každou jednou
        nejvýš o BOOST_LIFT míst, ať podkres nezaplaví. Vrací počet posunů."""
        idx = self._idx()
        if not (idx.fav_songs or idx.fav_artists):
            return 0
        moved = 0
        quota: dict[str, int] = {}  # 👍 jen z playlistů: posunutí za člověka v tomhle kole
        for pool in pools:
            q = getattr(pool, "tracks", None)
            if not q:
                continue
            items = list(q)
            changed = False
            for t in list(items):
                if t.id in self._lifted or not self.is_favourite(t):
                    continue
                backers = self.import_backers(t)
                if backers:
                    who = min(backers, key=lambda v: (quota.get(v, 0), v))
                    if quota.get(who, 0) >= IMPORT_LIFT:
                        continue  # tenhle člověk už své má; při dalším doplnění třeba
                    quota[who] = quota.get(who, 0) + 1
                self._lifted.add(t.id)
                j = items.index(t)
                k = max(0, j - BOOST_LIFT)
                if k < j:
                    items.insert(k, items.pop(j))
                    changed = True
                    moved += 1
            if changed:
                q.clear()
                q.extend(items)
        if len(self._lifted) > 5000:
            self._lifted.clear()
        return moved

    def ban_note(self, track: Any, asked_artists: Iterable[str] = ()) -> str:
        """Poznámka k výslovnému přání: "(pozn.: vyřazená hlasováním — Petr, Jana)"."""
        key = self.banned_song(track)
        if key:
            names = self._names(SONG, key, -1)
            return f"(pozn.: vyřazená hlasováním — {names})" if names else \
                "(pozn.: vyřazená hlasováním)"
        asked = list(asked_artists)
        for k in self.banned_artists_of(track):
            if asked and not any(k in artist_keys(a) for a in asked):
                continue
            names = self._names(ARTIST, k, -1)
            who = self._display_name(ARTIST, k)
            return f"(pozn.: {who} je vyřazený hlasováním — {names})" if names else \
                f"(pozn.: {who} je vyřazený hlasováním)"
        return ""

    # ---- hlasování ----

    def cast(self, target: str, voter: str, vote: int, who: str = "", *,
             track: Any = None, key: str = "", artist: str = "") -> CastResult:
        """Hlas (1 / -1 / 0 = stáhnout). VoteError s větou pro člověka, když neprojde.

        Cíl: `key` (z API seznamů), jinak skladba `track` / interpret `artist`
        (bez něj první uvedený interpret skladby `track`).
        """
        if target not in TARGETS:
            raise VoteError("Hlasovat jde o skladbu, nebo o interpreta.")
        if isinstance(vote, bool) or vote not in (1, -1, 0):
            raise VoteError("Hlas je 1 (👍), -1 (👎), nebo 0 (stáhnout).")
        if not voter:
            raise VoteError("Chybí id prohlížeče — obnov prosím stránku.")
        vid, t_artist, t_title = _tat(track) if track is not None else ("", "", "")
        if key:
            if (target, key) not in self.items:
                raise VoteError("Tahle položka v hlasování není.", 404)
            last = max(self.items[(target, key)].values(), key=lambda b: b.ts)
            vid = vid or last.video_id
            t_artist, t_title = t_artist or last.artist, t_title or last.title
        elif target == SONG:
            if track is None or not (t_artist or t_title):
                raise VoteError("Nevím, o kterou skladbu jde.", 404)
            key = self.song_key_for(track)
        else:
            name = " ".join(str(artist or "").split())
            if not name and track is not None:
                name = (credits(t_artist) or (t_artist,))[0]
            key = artist_key(name)
            if not key:
                raise VoteError("Nevím, o kterého interpreta jde.")
            t_artist, t_title, vid = name, "", ""
        self._rate_check(voter)

        ballots = self.items.get((target, key), {})
        old = ballots.get(voter)
        prev = old.vote if old else 0
        before = self.tally(target, key).status
        who = " ".join(str(who or "").split())[:WHO_MAX]
        if old is None and vote == 0:
            return CastResult(target, key, 0, 0, before, before, self.item(target, key, voter))
        if old is not None and old.vote == vote and not old.src:
            # stejný hlas znovu: nic se nemění (👍 u písničky z playlistu se
            # ale zapíše jako vlastní — přežije pak i odebrání playlistu)
            old.who = who or old.who
            return CastResult(target, key, vote, prev, before, before,
                              self.item(target, key, voter))
        now = self.clock()
        b = Ballot(voter, vote, who or (old.who if old else ""), now, vid or (old.video_id if old else ""),
                   t_artist or (old.artist if old else ""), t_title or (old.title if old else ""))
        self.items.setdefault((target, key), {})[voter] = b
        self._version += 1
        if self.store is not None:
            with contextlib.suppress(Exception):
                self.store.save_vote(target, key, voter, vote, b.who, b.video_id, b.artist,
                                     b.title, now)
        after = self.tally(target, key)
        res = CastResult(target, key, vote, prev, before, after.status,
                         self.item(target, key, voter))
        label = self._display_name(target, key)
        telemetry.event("vote.cast", target=target, key=key, label=telemetry.clip(label, 120),
                        vote=vote, prev=prev, voter=voter[-6:], who=b.who or None,
                        up=after.up, down=after.down, status=after.status)
        if res.changed == "ban":
            telemetry.event("vote.ban", target=target, key=key, label=telemetry.clip(label, 120),
                            down=after.down, up=after.up,
                            voters=[self._label(x.voter, x.who) for x in self._ballots(target, key, -1)])
            log.info("vyřazeno hlasováním: %s %s", target, label)
        elif res.changed == "unban":
            telemetry.event("vote.unban", target=target, key=key, label=telemetry.clip(label, 120),
                            down=after.down, up=after.up)
            log.info("zpět v nabídce: %s %s", target, label)
        return res

    def _rate_check(self, voter: str) -> None:
        now = self.mono()
        q = self._rate.setdefault(voter, deque())
        while q and now - q[0] > RATE_WINDOW:
            q.popleft()
        if len(q) >= RATE_MAX:
            telemetry.event("vote.rate_limited", voter=voter[-6:], n=len(q))
            raise VoteError("Moc hlasů najednou — zkus to prosím za pár minut.", 429)
        q.append(now)
        if len(self._rate) > 1000:  # zapomenout ty, kdo dlouho nehlasovali
            for v in [v for v, d in self._rate.items() if not d or now - d[-1] > RATE_WINDOW]:
                self._rate.pop(v, None)

    # ---- zobrazení ----

    def _label(self, voter: str, who: str) -> str:
        if self.label_fn is not None:
            with contextlib.suppress(Exception):
                name = self.label_fn(voter, who)
                if name:
                    return name
        name = who or "Host"
        return self.censor.clean(name) if self.censor is not None else name

    def _ballots(self, target: str, key: str, vote: int | None = None) -> list[Ballot]:
        out = [b for b in self.items.get((target, key), {}).values()
               if (b.vote != 0 if vote is None else b.vote == vote)]
        return sorted(out, key=lambda b: b.ts)

    def _names(self, target: str, key: str, vote: int) -> str:
        return ", ".join(self._label(b.voter, b.who) for b in self._ballots(target, key, vote))

    def _display_name(self, target: str, key: str) -> str:
        ballots = self.items.get((target, key), {})
        last = max(ballots.values(), key=lambda b: b.ts, default=None)
        if last is None:
            return key
        if target == ARTIST:
            return last.artist or key
        return f"{last.artist} — {last.title}" if last.artist else last.title or key

    def _voter(self, b: Ballot, tag_of: Callable[[str], str]) -> dict:
        v = {"nick": self._label(b.voter, b.who), "vote": b.vote, "at": round(b.ts, 1),
             "tag": tag_of(b.voter)}
        if b.src:  # 👍 z importu: "z playlistu ‚Název'"
            imp = self.imports.get(b.src)
            v["playlist"] = imp.title if imp is not None and imp.title else "playlist"
        return v

    def item(self, target: str, key: str, viewer: str = "") -> dict:
        """Veřejný popis cíle: kdo jak hlasoval a kdy, stav."""
        from .nicks import tag_of

        ballots = self.items.get((target, key), {})
        last = max(ballots.values(), key=lambda b: b.ts, default=None)
        t = self.tally(target, key)
        out: dict[str, Any] = {
            "target": target,
            "key": key,
            "label": self._display_name(target, key),
            "artist": last.artist if last else "",
            "up": t.up,
            "down": t.down,
            "status": t.status,
            "need": t.need,
            "voters": [self._voter(b, tag_of) for b in self._ballots(target, key)],
            "updated": round(last.ts, 1) if last else None,
        }
        if target == SONG:
            out["title"] = last.title if last else ""
            out["video_id"] = last.video_id if last else ""
        if viewer:
            mine = ballots.get(viewer)
            out["mine"] = mine.vote if mine else 0
        return out

    def lists(self, viewer: str = "") -> dict:
        """GET /api/votes: oblíbené, vyřazené, rozhodující se (+ moje).

        Stav se spočítá pro všechno, slovník položky (jména, značky) jen pro
        to, co se ukáže (LIST_MAX) — s importy playlistů jsou to tisíce
        položek a event loop na Pi by to cítil. `counts` = celé počty.
        "Moje hlasy" jsou jen vlastní hlasy; 👍 z playlistů ukazuje seznam
        importů (/api/votes/imports)."""
        favourites, banned, pending, mine = [], [], [], []
        for (target, key), ballots in self.items.items():
            up = down = 0
            updated = 0.0
            explicit = False
            for b in ballots.values():
                if b.vote > 0:
                    up += 1
                elif b.vote < 0:
                    down += 1
                if b.ts > updated:
                    updated = b.ts
                if b.vote and not b.src:
                    explicit = True
            if not (up or down):
                continue  # vše stažené
            t = self._status(target, up, down)
            row = (target, key, up - down, updated, explicit, t.need)
            if t.status == FAVOURITE:
                favourites.append(row)
            elif t.status == BANNED:
                banned.append(row)
            else:
                pending.append(row)
            if viewer:
                mb = ballots.get(viewer)
                if mb is not None and mb.vote and not mb.src:
                    mine.append(row)
        # oblíbené: nejvíc 👍, pak s vlastním hlasem (ne jen z playlistu), pak nejnovější
        favourites.sort(key=lambda r: (-r[2], not r[4], -r[3]))
        banned.sort(key=lambda r: -r[3])
        pending.sort(key=lambda r: (r[5], -r[3]))
        mine.sort(key=lambda r: -r[3])

        def shown(rows: list) -> list[dict]:
            return [self.item(r[0], r[1], viewer) for r in rows[:LIST_MAX]]

        out = {
            "favourites": shown(favourites),
            "banned": shown(banned),
            "pending": shown(pending),
            "rules": self.rules(),
            "counts": {"favourites": len(favourites), "banned": len(banned),
                       "pending": len(pending)},
        }
        if viewer:
            out["mine"] = shown(mine)
            out["counts"]["mine"] = len(mine)
        return out

    def brief(self, track: Any, full: bool = False) -> dict | None:
        """Malý blok do /api/status: {up, down, status, up_by, down_by[, artists]}.

        `up_by` / `down_by` jsou značky hlasujících (`tag` z /api/me) — "můj
        hlas" si web zjistí porovnáním se svou značkou; snímek je jeden pro
        všechny. Bez hlasů u položky fronty None (payload malý).
        """
        from .nicks import tag_of

        out: dict[str, Any] = {}
        if self.items:
            key = self.song_key_for(track)
            t = self.tally(SONG, key)
            if t.up or t.down or full:
                out = {"up": t.up, "down": t.down, "status": t.status}
                if t.up:
                    out["up_by"] = [tag_of(b.voter) for b in self._ballots(SONG, key, 1)]
                if t.down:
                    out["down_by"] = [tag_of(b.voter) for b in self._ballots(SONG, key, -1)]
        elif full:
            out = {"up": 0, "down": 0, "status": NEUTRAL}
        _, artist, _ = _tat(track)
        arts = []
        for name in credits(artist):
            k = artist_key(name)
            t = self.tally(ARTIST, k)
            if t.up or t.down or full:
                a: dict[str, Any] = {"name": name, "up": t.up, "down": t.down, "status": t.status}
                if t.down:
                    # "by" = 👎 (starší web a displej), "down_by" = totéž pod novým jménem
                    a["by"] = a["down_by"] = [tag_of(b.voter) for b in self._ballots(ARTIST, k, -1)]
                if t.up:
                    a["up_by"] = [tag_of(b.voter) for b in self._ballots(ARTIST, k, 1)]
                arts.append(a)
        if arts:
            out["artists"] = arts
            if not full:
                out.setdefault("status", NEUTRAL)
                worst = next((a for a in arts if a["status"] == BANNED), None)
                if worst:
                    out["artist_status"] = BANNED
                elif any(a["status"] == PENDING for a in arts):
                    out["artist_status"] = PENDING
                elif any(a["status"] == FAVOURITE for a in arts):
                    out["artist_status"] = FAVOURITE
        return out or None

    def detail(self, track: Any, viewer: str = "") -> dict:
        """GET /api/votes/track: skladba a každý uvedený interpret, i bez hlasů."""
        vid, artist, title = _tat(track)
        key = self.song_key_for(track)
        song = self.item(SONG, key, viewer)
        if not self.items.get((SONG, key)):
            song.update(label=f"{artist} — {title}" if artist else title, artist=artist,
                        title=title, video_id=vid)
        arts = []
        for name in credits(artist):
            it = self.item(ARTIST, artist_key(name), viewer)
            if not self.items.get((ARTIST, artist_key(name))):
                it.update(label=name, artist=name)
            it["name"] = name
            arts.append(it)
        return {"video_id": vid, "artist": artist, "title": title, "song": song,
                "artists": arts, "rules": self.rules()}

    # ---- oblíbené jako zdroj hudby ----

    def favourite_tracks(self, voter: str = "", shuffle: bool = True) -> list:
        """Oblíbené kanceláře, nebo (s `voter`) moje 👍 — bez vyřazených.

        Kancelář: po lidech na střídačku (`fair_order`), ať playlist o 300
        písničkách nepřehluší kolegu s dvaceti 👍. Moje: náhodně."""
        from .music.catalog import Track

        idx = self._idx()
        src_of: dict[str, Ballot] = {}
        mine: list[str] = []
        per: dict[str, list[tuple]] = {}
        for (target, key), ballots in self.items.items():
            if target != SONG or key in idx.banned_songs:
                continue
            if voter:
                b = ballots.get(voter)
                if b is None or b.vote <= 0:
                    continue
                mine.append(key)
            else:
                if key not in idx.fav_songs:
                    continue
                t = self.tally(SONG, key)
                for b in ballots.values():
                    if b.vote > 0:
                        # u každého napřed nejvíc 👍 kanceláře, vlastní 👍 před playlistem
                        per.setdefault(b.voter, []).append(
                            (t.down - t.up, 1 if b.src else 0, b.ts, key))
            src = max((b for b in ballots.values() if b.video_id), key=lambda b: b.ts, default=None)
            if src is not None:
                src_of[key] = src
        if voter:
            order = sorted(mine)
            if shuffle:
                self.prng.shuffle(order)
        else:
            order = fair_order(per, self.prng if shuffle else None)
        out, seen = [], set()
        for key in order:
            b = src_of.get(key)
            if b is None or b.video_id in seen:
                continue
            if not self.banned_artists_of(Track(b.video_id, b.title, b.artist)):
                seen.add(b.video_id)
                out.append(Track(b.video_id, b.title, b.artist))
        return out

    def favourite_artists(self, voter: str = "") -> list[str]:
        """Oblíbení interpreti kanceláře, nebo (s `voter`) ti, kterým dal 👍."""
        idx = self._idx()
        keys: list[str] = []
        for (target, key), ballots in self.items.items():
            if target != ARTIST or key in idx.banned_artists:
                continue
            if voter:
                b = ballots.get(voter)
                if b is not None and b.vote > 0:
                    keys.append(key)
            elif key in idx.fav_artists:
                keys.append(key)
        score = {k: self.tally(ARTIST, k).up - self.tally(ARTIST, k).down for k in keys}
        keys.sort(key=lambda k: (-score[k], k))
        return [self._display_name(ARTIST, k) for k in keys]

    async def favourite_mix(self, catalog: Any, voter: str = "", per_artist: int = ARTIST_MIX,
                            timeout: float = MIX_TIMEOUT, alternate: bool = True) -> list:
        """ "Pusť oblíbené": oblíbené skladby prostřídané se známými skladbami
        oblíbených interpretů (katalog; když nestihne, jen oblíbené skladby)."""
        songs = self.favourite_tracks(voter)
        names = self.favourite_artists(voter)
        lookup = getattr(catalog, "artist_tracks", None) if catalog is not None else None
        per: list[list] = []
        if names and lookup is not None:
            async def one(name: str) -> list:
                try:
                    return list(await lookup(name, limit=20))
                except Exception as exc:  # katalog umí selhat na čemkoli
                    log.info("oblíbený interpret %r: katalog selhal (%s)", name, exc)
                    return []
            try:
                found = await asyncio.wait_for(
                    asyncio.gather(*(one(n) for n in names[:6])), timeout)
            except asyncio.TimeoutError:
                found = []
                telemetry.event("vote.mix_timeout", artists=names[:6])
            for tracks in found:
                ok = [t for t in tracks if self.blocked(t) is None][: max(per_artist * 3, 6)]
                random.shuffle(ok)
                per.append(ok[:per_artist])
        out, seen = [], set()
        lists = [list(songs)] + per
        while any(lists):
            for lst in lists:
                if lst:
                    t = lst.pop(0)
                    if t.id not in seen:
                        seen.add(t.id)
                        out.append(t)
        # napřeskáčku: ne dvakrát za sebou týž interpret (když posluchač
        # výslovně nechce jinak — model: alternate_artists = false)
        return spread_artists(out) if alternate else out

    def favourite_rotation(self, voter: str = "", exclude: Iterable[str] = (),
                           alternate: bool = True) -> list:
        """Režim oblíbených (podkres, RadioPools.set_favourites): oblíbené
        kanceláře (nebo 👍 člověka `voter`) po lidech na střídačku,
        napřeskáčku podle interpretů, bez skladeb z `exclude` (už zazněly
        v tomhle kole) a bez vyřazených."""
        skip = set(exclude)
        tracks = [t for t in self.favourite_tracks(voter) if t.id not in skip]
        return spread_artists(tracks) if alternate else tracks

    def summary(self, n_fav: int = 8, n_ban: int = 8) -> tuple[list[str], list[str], list[str]]:
        """(oblíbené — skladby prostřídané s "interpret X", vyřazení interpreti,
        vyřazené skladby) jako popisky."""
        idx = self._idx()
        # po lidech na střídačku jako "pusť oblíbené", ale pevně (DJ nemá
        # dostávat pokaždé jiný text): kdo hlasoval dřív, je v kole první
        per: dict[str, list[tuple]] = {}
        for key in idx.fav_songs:
            t = self.tally(SONG, key)
            for b in self.items.get((SONG, key), {}).values():
                if b.vote > 0:
                    per.setdefault(b.voter, []).append((t.down - t.up, 1 if b.src else 0, b.ts, key))
        favs = fair_order(per, None, n=n_fav)
        songs = [self._display_name(SONG, k) for k in favs]
        arts = [f"interpret {a}" for a in self.favourite_artists()]
        mixed: list[str] = []
        for i in range(max(len(songs), len(arts))):
            mixed += songs[i:i + 1] + arts[i:i + 1]
        return (mixed[:n_fav],
                [self._display_name(ARTIST, k) for k in sorted(idx.banned_artists)[:n_ban]],
                [self._display_name(SONG, k) for k in sorted(idx.banned_songs)[:n_ban]])

    def favourites_overview(self, asker: str = "") -> str:
        """Jeden řádek pro DJ: kolik oblíbených má kancelář (a od koho), hlavní
        interpreti a oblíbené toho, kdo píše — ať model ví, co akce
        `favourites` zahraje (Pi 27. 9. 0:59: "co máme rádi" → model vybral
        z historie dva interprety, oblíbených bylo 93 od 64 interpretů)."""
        idx = self._idx()
        artists: dict[str, int] = {}
        people: set[str] = set()
        for key in idx.fav_songs:
            ballots = self.items.get((SONG, key), {})
            last = max(ballots.values(), key=lambda b: b.ts, default=None)
            if last is not None and last.artist:
                name = (credits(last.artist) or (last.artist,))[0]
                artists[name] = artists.get(name, 0) + 1
            people |= {b.voter for b in ballots.values() if b.vote > 0}
        if not idx.fav_songs and not idx.fav_artists:
            out = "Oblíbené kanceláře: zatím žádné."
        else:
            top = sorted(artists, key=lambda a: (-artists[a], a))[:5]
            out = (f"Oblíbené kanceláře (akce favourites): {len(idx.fav_songs)} skladeb od "
                   f"{len(artists)} interpretů, 👍 od {len(people)} lidí"
                   + (f"; nejvíc {', '.join(top)}" if top else "") + ".")
        if asker:
            mine = sum(1 for (t, _k), bs in self.items.items()
                       if t == SONG and (b := bs.get(asker)) is not None and b.vote > 0)
            lists = [i.title for i in self._imports_by.get(asker, ())]
            out += (f" Kdo píše, má {mine} vlastních oblíbených"
                    + (f" (i z playlistu {', '.join(f'‚{x}‘' for x in lists[:2])})" if lists else "")
                    + "." if mine else " Kdo píše, vlastní oblíbené nemá.")
        return out

    def describe(self, max_chars: int = 800, asker: str = "") -> str:
        """Kompaktní řádky pro DJ (Codex) — "" když se nehlasovalo."""
        favs, arts, songs = self.summary()
        lines = []
        fav_songs = [f for f in favs if not f.startswith("interpret ")]
        fav_artists = [f[len("interpret "):] for f in favs if f.startswith("interpret ")]
        if self.items or asker:
            lines.append(self.favourites_overview(asker))
        if fav_songs:
            lines.append("Oblíbené kanceláře (👍): " + "; ".join(fav_songs))
        if fav_artists:
            lines.append("Oblíbení interpreti kanceláře (👍): " + ", ".join(fav_artists))
        if arts or songs:
            parts = []
            if arts:
                parts.append("interpreti " + ", ".join(arts))
            if songs:
                parts.append("skladby " + "; ".join(songs))
            lines.append("Vyřazené hlasováním — nenavrhuj ani jako seedy (na výslovné přání "
                         "se zahrají): " + "; ".join(parts))
        text = "\n".join(lines)
        return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


# --------------------------------------------------------------------------
# účinek vyřazení na to, co hraje
# --------------------------------------------------------------------------


def fair_order(per: dict[str, list[tuple]], rng: random.Random | None,
               n: int | None = None) -> list[str]:
    """Klíče skladeb po lidech na střídačku.

    `per` = člověk → [(pořadí podle 👍, 1 = z playlistu / 0 = vlastní, čas, klíč)].
    V každém kole dá každý jednu skladbu: u sebe tu s nejvíc 👍 kanceláře,
    vlastní 👍 před playlistem. V kole jde první ten, kdo nese oblíbenější
    skladbu (nejoblíbenější tak hrají první); shody jsou s `rng` náhodné,
    bez něj pevné (skladby podle času, lidé podle prvního hlasu). Skladbu,
    kterou už přinesl někdo jiný, člověk přeskočí a dá další — o kolo
    nepřijde. Každý tak přispěje stejně, ať má oblíbených 5, nebo 300."""
    queues: dict[str, deque] = {}
    first: dict[str, float] = {}
    for voter, rows in per.items():
        rows = list(rows)
        if rng is not None:
            rng.shuffle(rows)
            rows.sort(key=lambda r: (r[0], r[1]))  # stabilní: shody zůstanou zamíchané
        else:
            rows.sort()
        queues[voter] = deque(rows)
        first[voter] = min((r[2] for r in rows), default=0.0)
    out: list[str] = []
    seen: set[str] = set()

    def head(v: str) -> tuple | None:
        q = queues[v]
        while q and q[0][3] in seen:
            q.popleft()
        return q[0] if q else None

    voters = [v for v in queues if queues[v]]
    while voters and (n is None or len(out) < n):
        noms = {v: head(v) for v in voters}
        voters = [v for v in voters if noms[v] is not None]
        tie = {v: rng.random() for v in voters} if rng is not None else {}
        # jen podle 👍 kanceláře: kdo má jen playlist, nesmí být v kole vždycky poslední
        order = sorted(voters, key=lambda v: (noms[v][0], tie.get(v, 0.0), first[v], v))
        for v in order:
            row = head(v)
            if row is None:
                continue
            queues[v].popleft()
            seen.add(row[3])
            out.append(row[3])
            if n is not None and len(out) >= n:
                break
        voters = [v for v in voters if head(v) is not None]
    return out


SPREAD_GAP = 4  # "napřeskáčku": interpret se nevrátí dřív než po tolika jiných
SPREAD_LOOK = 60  # jak daleko dopředu se hledá jiný interpret (Pi: tisíce oblíbených)


def spread_artists(tracks: list, gap: int = SPREAD_GAP, look: int = SPREAD_LOOK) -> list:
    """Pořadí "napřeskáčku": nikdy dvakrát za sebou týž interpret, a když to
    jde, ani v posledních `gap` skladbách. Jinak drží původní pořadí (férovost
    po lidech, nejoblíbenější první) — bere se první vhodná z nejbližších
    `look` skladeb. Interpret = kterýkoli uvedený ("A feat. B")."""
    rest = []
    left: dict[str, int] = {}  # kolik skladeb interpreta ještě zbývá (hlavní klíč)
    for t in tracks:
        keys = artist_keys(getattr(t, "artist", "") or "")
        main = min(keys) if keys else ""
        rest.append((t, keys, main))
        left[main] = left.get(main, 0) + 1
    out: list = []
    recent: deque = deque(maxlen=max(1, gap))
    while rest:
        n = len(rest)
        last = recent[-1] if recent else frozenset()
        window = range(min(look, n))
        pick = None
        # interpret, kterého zbývá přes polovinu, musí jít teď (jinak by na
        # konci zbyl sám a hrál dvakrát za sebou)
        heavy = max(left, key=lambda k: left[k], default="")
        if heavy and left[heavy] * 2 > n and heavy not in last:
            pick = next((i for i in range(n) if rest[i][2] == heavy), None)
        if pick is None:  # v původním pořadí první, který nebyl v posledních `gap`
            pick = next((i for i in window if not any(rest[i][1] & r for r in recent)), None)
        if pick is None:  # jinak aspoň ne jako předchozí — toho, koho zbývá nejvíc
            cands = [i for i in window if not (rest[i][1] & last)]
            if cands:
                pick = max(cands, key=lambda i: (left[rest[i][2]], -i))
        t, keys, main = rest.pop(pick if pick is not None else 0)
        left[main] -= 1
        if not left[main]:
            del left[main]
        out.append(t)
        recent.append(keys)
    return out


def focus_artists(pools: Any) -> list[str]:
    """Režim interpreta: o tyhle si někdo řekl jménem — jejich zákaz neplatí."""
    label = getattr(pools, "artist", "") or ""
    return [a.strip() for a in label.split(",") if a.strip()] if label else []


async def enforce(app: Any, reason: str = "") -> dict:
    """Po vyřazení: z fronty pryč podkres, který teď neprojde; hraje-li podkres
    vyřazený, přeskočit. Přání (i cizí) se nikdy nedotkne."""
    from .player.base import queue_transaction

    votes = getattr(app, "votes", None)
    player = getattr(app, "player", None)
    if votes is None or player is None:
        return {"removed": 0, "skipped": False}
    wq = getattr(app, "wishes", None)
    wish_track = getattr(wq, "is_request_track", None) or (lambda vid: False)
    exempt = focus_artists(getattr(app, "pools", None))
    removed = 0
    skipped = False
    async with queue_transaction(player):
        st = await player.status()
        stale = {t.id for t in st.queue if votes.blocked(t, exempt) and not wish_track(t.id)}
        if stale:
            removed = await player.remove_upcoming(stale)
            telemetry.event("vote.cleanup", reason=reason or None, removed=removed,
                            tracks=sorted(stale)[:20])
    cur = st.current
    if cur is not None and votes.blocked(cur, exempt) and not wish_track(cur.id):
        telemetry.event("vote.skip", reason=reason or None, video_id=cur.id,
                        label=telemetry.clip(cur.label(), 120), why=votes.blocked(cur, exempt))
        log.info("přeskakuji vyřazenou hlasováním: %s", cur.label())
        await player.skip(by_user=False)
        skipped = True
    return {"removed": removed, "skipped": skipped}


def wire(app: Any) -> VoteBook:
    """Hlasování do aplikace: kniha hlasů + podkres + DJ. Volá App.__init__."""
    wq = getattr(app, "wishes", None)

    def label(voter: str, who: str) -> str:
        name = wq.name_for(voter, who) if wq is not None else who
        name = name or "Host"
        return wq.censor.clean(name) if wq is not None else name

    book = VoteBook(app.store, app.cfg, label=label)
    book.pools = getattr(app, "pools", None)
    app.votes = book
    from .imports import Importer  # import playlistů do oblíbených (POZADAVKY #48)

    app.imports = Importer(book, app.store, getattr(app, "catalog", None), app.cfg)
    for part in (getattr(app, "pools", None), getattr(app, "dj", None)):
        if part is not None:
            part.votes = book
    return book


async def aload_quietly(book: VoteBook) -> None:
    try:
        await book.aload()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("hlasy se nepodařilo načíst — hlasování začíná prázdné")
        book.loaded = True
