"""Hlasování kanceláře: 👍/👎 skladbám, 👎 interpretům (PLAN H).

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
`ban_artist_votes`):

    skladba    vyřazená     aspoň ban_song_votes (2) lidí 👎 a víc 👎 než 👍
               oblíbená     aspoň jeden 👍 a víc 👍 než 👎
               upozaděná    víc 👎 než 👍, ale na vyřazení to nestačí
    interpret  vyřazený     aspoň ban_artist_votes (3) lidí 👎 (jen 👎)
               čeká         1–2 👎

Co to dělá s hudbou (radio.py, codex.py, enforce() níže): podkres (pooly)
vyřazené nikdy nevydá, upozaděné jen napůl, oblíbené smí zase hrát i dřív
než po `repeat_days`. Výslovné přání vyřazené skladby / interpreta se
splní — s poznámkou, kdo ji vyřadil. Při vyřazení zmizí z fronty jen
podkres; hraje-li zrovna podkres, přeskočí se.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
import time
from collections import deque
from dataclasses import dataclass
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
    __slots__ = ("sig", "banned_songs", "fav_songs", "down_songs", "banned_artists", "by_vid")

    def __init__(self, sig: tuple) -> None:
        self.sig = sig
        self.banned_songs: set[str] = set()
        self.fav_songs: set[str] = set()
        self.down_songs: set[str] = set()
        self.banned_artists: set[str] = set()
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
        """Načtení mimo event loop (SD karta)."""
        aread = getattr(self.store, "aread", None)
        rows = await aread("all_votes") if aread is not None else None
        return self.load(rows)

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

    def _status(self, target: str, up: int, down: int) -> Tally:
        song_t, artist_t = self.thresholds()
        if target == ARTIST:
            status = BANNED if down >= artist_t else PENDING if down else NEUTRAL
            return Tally(0, down, status, max(0, artist_t - down))
        if down >= song_t and down > up:
            return Tally(up, down, BANNED, 0)
        need = max(song_t - down, up - down + 1, 1)
        if up >= 1 and up > down:
            return Tally(up, down, FAVOURITE, need)
        return Tally(up, down, DOWN if down > up else NEUTRAL, need)

    def _idx(self) -> _Index:
        sig = (self._version, self.thresholds())
        if self._index is not None and self._index.sig == sig:
            return self._index
        idx = _Index(sig)
        for (target, key), ballots in self.items.items():
            t = self.tally(target, key)
            if target == ARTIST:
                if t.status == BANNED:
                    idx.banned_artists.add(key)
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
        if self.blocked(track, exempt):
            return "voted_out"
        idx = self._idx()
        if idx.down_songs and self._song_hits(track) & idx.down_songs \
                and self.rng() >= DOWNWEIGHT_PASS:
            return "downweighted"
        return None

    def is_favourite(self, track: Any) -> bool:
        idx = self._idx()
        return bool(idx.fav_songs) and bool(self._song_hits(track) & idx.fav_songs)

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
        if target == ARTIST and vote > 0:
            raise VoteError("Interpretům se dává jen 👎 — 👍 patří konkrétním skladbám.")
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
        if old is not None and old.vote == vote:
            old.who = who or old.who  # stejný hlas znovu: nic se nemění
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
            "voters": [{"nick": self._label(b.voter, b.who), "vote": b.vote,
                        "at": round(b.ts, 1), "tag": tag_of(b.voter)}
                       for b in self._ballots(target, key)],
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
        """GET /api/votes: oblíbené, vyřazené, rozhodující se (+ moje)."""
        favourites, banned, pending, mine = [], [], [], []
        for (target, key), ballots in self.items.items():
            if not any(b.vote for b in ballots.values()):
                continue  # vše stažené
            it = self.item(target, key, viewer)
            if it["status"] == FAVOURITE:
                favourites.append(it)
            elif it["status"] == BANNED:
                banned.append(it)
            else:
                pending.append(it)
            if viewer and it.get("mine"):
                mine.append(it)
        favourites.sort(key=lambda i: (-(i["up"] - i["down"]), -(i["updated"] or 0)))
        banned.sort(key=lambda i: -(i["updated"] or 0))
        pending.sort(key=lambda i: (i["need"], -(i["updated"] or 0)))
        mine.sort(key=lambda i: -(i["updated"] or 0))
        song_t, artist_t = self.thresholds()
        out = {
            "favourites": favourites[:LIST_MAX],
            "banned": banned[:LIST_MAX],
            "pending": pending[:LIST_MAX],
            "rules": {"ban_song_votes": song_t, "ban_artist_votes": artist_t},
        }
        if viewer:
            out["mine"] = mine[:LIST_MAX]
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
            if t.down or full:
                a: dict[str, Any] = {"name": name, "down": t.down, "status": t.status}
                if t.down:
                    a["by"] = [tag_of(b.voter) for b in self._ballots(ARTIST, k, -1)]
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
        song_t, artist_t = self.thresholds()
        return {"video_id": vid, "artist": artist, "title": title, "song": song,
                "artists": arts,
                "rules": {"ban_song_votes": song_t, "ban_artist_votes": artist_t}}

    # ---- oblíbené jako zdroj hudby ----

    def favourite_tracks(self, voter: str = "", shuffle: bool = True) -> list:
        """Oblíbené kanceláře, nebo (s `voter`) moje 👍 — bez vyřazených."""
        from .music.catalog import Track

        idx = self._idx()
        rows: list[tuple[float, Ballot]] = []
        for (target, key), ballots in self.items.items():
            if target != SONG or key in idx.banned_songs:
                continue
            if voter:
                b = ballots.get(voter)
                if b is None or b.vote <= 0:
                    continue
                score = 1.0
            else:
                if key not in idx.fav_songs:
                    continue
                t = self.tally(SONG, key)
                score = float(t.up - t.down)
            src = max((b for b in ballots.values() if b.video_id), key=lambda b: b.ts, default=None)
            if src is None:
                continue
            rows.append((score, src))
        if shuffle:
            random.shuffle(rows)  # pořadí ne vždycky stejné…
        rows.sort(key=lambda r: -r[0])  # …ale nejoblíbenější první
        out, seen = [], set()
        for _, b in rows:
            if b.video_id not in seen and not self.banned_artists_of(Track(b.video_id, b.title, b.artist)):
                seen.add(b.video_id)
                out.append(Track(b.video_id, b.title, b.artist))
        return out

    def summary(self, n_fav: int = 8, n_ban: int = 8) -> tuple[list[str], list[str], list[str]]:
        """(oblíbené skladby, vyřazení interpreti, vyřazené skladby) jako popisky."""
        idx = self._idx()
        favs = sorted(idx.fav_songs, key=lambda k: -(self.tally(SONG, k).up - self.tally(SONG, k).down))
        return ([self._display_name(SONG, k) for k in favs[:n_fav]],
                [self._display_name(ARTIST, k) for k in sorted(idx.banned_artists)[:n_ban]],
                [self._display_name(SONG, k) for k in sorted(idx.banned_songs)[:n_ban]])

    def describe(self, max_chars: int = 600) -> str:
        """Kompaktní řádky pro DJ (Codex) — "" když se nehlasovalo."""
        favs, arts, songs = self.summary()
        lines = []
        if favs:
            lines.append("Oblíbené kanceláře (👍): " + "; ".join(favs))
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
    app.votes = book
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
