"""Oprava uložených klíčů po hlášení #44 („AC/DC rozkouskoval na AC a DC").

Do 13. 11. 2026 se jméno interpreta dělilo i na "&", "/", "+" a samotném
"x" a "Earth, Wind & Fire" i na čárce. Hlasy, importy playlistů a seznam AI
tvorby si proto mohly uložit klíč kousku jména:

- 👍/👎 interpretovi "AC" (z listu, který nabízel "AC" a "DC"),
- skladba pod klíčem "ac|thunderstruck" místo "acdc|thunderstruck"
  (klíč skladby je podle prvního uvedeného interpreta).

`run(store)` to při startu jednou projde (podle tabulky `fixes`) a kousky
přepíše na celé jméno — jen když je to jednoznačné:

- klíč skladby se přepíše, když je přesně tím, co by ze svého uloženého
  interpreta a názvu spočítal starý postup, a nový dává jiný;
- klíč interpreta (hlas, AI tvorba) se přepíše, když není klíčem žádného
  interpreta, který tu kdy hrál nebo měl hlas, a vznikl rozdělením právě
  jednoho jména (z historie `plays`, hlasů a importů). "DC" z "AC/DC" ano;
  kousek, který by mohl patřit ke dvěma jménům, zůstane, jak je.

Když má člověk hlas pod oběma klíči, platí novější. Zápis jde přes vlákno
zápisů (`Store._write_tx`, jedna transakce), každá změna jde do logu
a souhrn do telemetrie (`votes.key_fix`).
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import telemetry
from .music.match import norm, split_title
from .votes import ARTIST, SONG, artist_key, artist_keys, credits, song_key

log = logging.getLogger(__name__)

FIX_NAME = "artist-split-44"

# ---- starý postup (do 13. 11. 2026), jen pro poznání starých klíčů ----

_OLD_SPLIT = re.compile(r"\s*(?:,|;|\bfeat\.?|\bft\.?|\bfeaturing\b|\bx\b|\bvs\.?)\s*", re.I)
_OLD_AMP = re.compile(r"\s*(?:&|\+|/)\s*")


def old_credits(artist: str) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()

    def add(part: str) -> None:
        part = re.sub(r"\s*-\s*topic$", "", " ".join(part.split()), flags=re.I).strip(" -")
        k = artist_key(part)
        if part and k and k not in seen:
            seen.add(k)
            out.append(part)

    for part in _OLD_SPLIT.split(artist or ""):
        add(part)
        subs = _OLD_AMP.split(part)
        if len(subs) > 1:
            for sub in subs:
                add(sub)
    return tuple(out)


def _old_artist_keys(artist: str) -> set[str]:
    keys = {artist_key(p) for p in old_credits(artist)}
    keys.add(artist_key(artist))
    keys.discard("")
    return keys


def old_song_key(artist: str, title: str) -> str:
    cr = old_credits(artist)
    first = [p for p in _OLD_AMP.split(cr[0]) if artist_key(p)] if cr else []
    title = title or ""
    parts = re.split(r"\s+[-–—]\s+", title, maxsplit=1)
    if len(parts) == 2 and artist_key(parts[0]) and artist_key(parts[0]) in _old_artist_keys(artist):
        title = parts[1]
    base, _ = split_title(title)
    return f"{artist_key(first[0] if first else artist)}|{norm(base) or norm(title)}"


# ---- plán ----


@dataclass
class Plan:
    statements: list[tuple[str, Any]] = field(default_factory=list)
    song: list[tuple[str, str]] = field(default_factory=list)  # (starý, nový) klíč skladby
    artist: dict[str, tuple[str, str]] = field(default_factory=dict)  # kousek → (klíč, jméno)
    rows: int = 0  # kolik řádků se mění

    def __bool__(self) -> bool:
        return bool(self.statements)


def fragment_map(wholes: Iterable[str]) -> dict[str, tuple[str, str]]:
    """Kousek jména → (klíč, jméno) celého interpreta, když je to jednoznačné
    a kousek sám není interpretem z `wholes`."""
    real: set[str] = set()
    owners: dict[str, dict[str, str]] = defaultdict(dict)
    for w in set(wholes):
        if not w:
            continue
        real |= artist_keys(w)
        for c in credits(w):
            ck = artist_key(c)
            for f in {artist_key(p) for p in old_credits(c)} - {ck, ""}:
                owners[f].setdefault(ck, c)
    return {f: next(iter(o.items())) for f, o in owners.items() if f not in real and len(o) == 1}


def plan(votes: Iterable[tuple], items: Iterable[tuple] = (), removed: Iterable[tuple] = (),
         ai: Iterable[tuple] = (), plays: Iterable[str] = ()) -> Plan:
    """Řádky jako `Store.all_votes`, `all_imports()[1]`, `import_removed_rows`,
    `all_ai_flags` a interpreti z `plays` → co přepsat."""
    votes, items, removed, ai = list(votes), list(items), list(removed), list(ai)
    p = Plan()
    wholes = set(plays)
    wholes |= {r[6] for r in votes if r[0] == SONG and r[6]}
    wholes |= {r[3] for r in items + removed if r[3]}
    frag = fragment_map(wholes)

    # hlasy: (target, key, voter) → ts, průběžně podle přepisů
    have: dict[tuple[str, str, str], float] = {(r[0], r[1], r[2]): float(r[8]) for r in votes}
    for target, key, voter, _vote, _who, _vid, artist, title, ts in votes:
        new = name = None
        if target == SONG and (artist or title):
            if key == old_song_key(artist, title) and song_key(artist, title) != key:
                new = song_key(artist, title)
                p.song.append((key, new))
        elif target == ARTIST and key in frag:
            new, name = frag[key]
            p.artist[key] = frag[key]
        if not new:
            continue
        p.rows += 1
        ts = float(ts)
        other = have.get((target, new, voter))
        if other is not None and other >= ts:
            p.statements.append(("DELETE FROM votes WHERE target=? AND key=? AND voter=?",
                                 (target, key, voter)))
        elif name is not None:
            p.statements.append(("UPDATE OR REPLACE votes SET key=?, artist=? "
                                 "WHERE target=? AND key=? AND voter=?",
                                 (new, name, target, key, voter)))
            have[(target, new, voter)] = ts
        else:
            p.statements.append(("UPDATE OR REPLACE votes SET key=? "
                                 "WHERE target=? AND key=? AND voter=?", (new, target, key, voter)))
            have[(target, new, voter)] = ts
        have.pop((target, key, voter), None)

    for table, rows in (("import_items", items), ("import_removed", removed)):
        for iid, key, _vid, artist, title, *_ in rows:
            if (artist or title) and key == old_song_key(artist, title) \
                    and song_key(artist, title) != key:
                new = song_key(artist, title)
                p.song.append((key, new))
                p.rows += 1
                # stejná písnička pod novým klíčem už v importu je → starý řádek pryč
                p.statements.append((f"UPDATE OR IGNORE {table} SET key=? WHERE import_id=? AND key=?",
                                     (new, iid, key)))
                p.statements.append((f"DELETE FROM {table} WHERE import_id=? AND key=?", (iid, key)))

    flags = {r[0]: float(r[7] or 0) for r in ai}
    for row in ai:
        key, ts = row[0], float(row[7] or 0)
        if key not in frag:
            continue
        new, name = frag[key]
        p.artist[key] = frag[key]
        p.rows += 1
        if flags.get(new, -1.0) >= ts:
            p.statements.append(("DELETE FROM ai_flags WHERE key=?", (key,)))
        else:
            p.statements.append(("UPDATE OR REPLACE ai_flags SET key=?, name=? WHERE key=?",
                                 (new, name, key)))
            flags[new] = ts
    return p


# ---- spuštění ----

def run(store: Any) -> Plan | None:
    """Jednou za život databáze (tabulka `fixes`): projít a přepsat. Volá se
    mimo event loop (`Store.aread`); po návratu jsou zápisy na disku."""
    read = getattr(store, "_read", None)
    write_tx = getattr(store, "_write_tx", None)
    if read is None or write_tx is None:
        return None
    if read("SELECT 1 FROM fixes WHERE name=?", (FIX_NAME,)):
        return None
    t0 = time.monotonic()
    votes = store.all_votes()
    _, items = store.all_imports()
    removed = store.import_removed_rows()
    ai = store.all_ai_flags()
    plays = [r[0] for r in read("SELECT DISTINCT artist FROM plays WHERE artist IS NOT NULL")]
    p = plan(votes, items, removed, ai, plays)
    for frag, (key, name) in sorted(p.artist.items()):
        log.warning("#44: klíč interpreta %r → %r (%s)", frag, key, name)
    for old, new in sorted(set(p.song)):
        log.warning("#44: klíč skladby %r → %r", old, new)
    note = f"{p.rows} řádků, interpreti {len(p.artist)}, skladby {len(set(p.song))}"
    write_tx(p.statements + [("INSERT OR REPLACE INTO fixes (name, ts, note) VALUES (?,?,?)",
                              (FIX_NAME, time.time(), note))])
    store.flush()
    telemetry.event("votes.key_fix", fix=FIX_NAME, rows=p.rows,
                    artists={k: v[0] for k, v in sorted(p.artist.items())},
                    songs=len(set(p.song)), plays=len(plays),
                    ms=round((time.monotonic() - t0) * 1000))
    log.info("#44: oprava klíčů hotová — %s", note)
    return p
