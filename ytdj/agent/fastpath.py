"""Rychlá cesta pro "pusť Kabát" — bez Codexu.

Na Pi trvá od přání ke zvuku 22–30 s a z toho 11–20 s je tah Codexu. Přitom
"pusť <interpret>" je nejčastější přání v kanceláři a není na něm co vykládat:
stačí poznat, že zbytek textu je jméno, a ověřit v katalogu, že takový
interpret existuje.

Zásada: přijmout JEN jistotu. Cokoli nejistého (neznámé jméno, jméno, které
je zároveň název známé skladby, slova nálady) jde dál k modelu jako dřív —
pomalá správná odpověď je lepší než rychlá špatná.

Čistá část (`parse`, `name_matches`, `variants`) je bez sítě a testuje se
zvlášť; `find_artists` dostane katalog.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

from .intent import _EXCEPT, _FILLER, _JOIN, _LIKE, _SINGLE, artist_match, local_command, norm

MAX_NAME_TOKENS = 5

# Slova, která nejsou jméno, ale nálada, žánr nebo povel. Porovnává se začátek
# slova ("klidn" chytí klidné, klidného, klidnou).
_E = r"(?:[aeiouy]|ou|em|ho|mu|ech|ove|ovou|ovy|ovka|ovky|ac|aci)?$"
_NOT_NAME = re.compile(
    # nálada, tempo, hlasitost
    r"^(?:nahlas|potichu|hlasit\w*|tis[e]\w*|ztlum\w*|klidn\w*|vesel\w*|smutn\w*|"
    r"rychl\w*|pomal\w*|tanec|tanecn\w*|tancov\w*|pohodov\w*|romantick\w*|"
    r"zamilovan\w*|relax\w*|chill\w*|energick\w*|hlucn\w*|tvrd\w*|jemn\w*|"
    r"akustick\w*|instrumentaln\w*|filmov\w*|soundtrack\w*|detsk\w*|pohadk\w*|"
    r"vanocn\w*|koled\w*|lepsi|horsi|dobr[aeyo]|dobre\w*)$"
    # žánry (celé slovo, jen s pádovou koncovkou — "Metallica" ani "Radiohead" ne)
    r"|^(?:rock|pop|jazz|rap|hiphop|metal|folk|punk|disco|techno|house|klasik|"
    r"blues|swing|country|reggae|funk|soul|trance|dubstep|edm|indie|alternativ)" + _E +
    # jazyk, doba, obecná slova
    r"|^(?:cesk\w*|slovensk\w*|anglick\w*|nemeck\w*|francouzsk\w*|zahranicn\w*|"
    r"star[aeyo]|stare\w*|starsi\w*|nov[aeyo]|nove\w*|novejsi\w*|"
    r"devadesat\w*|osmdesat\w*|sedmdesat\w*|sedesat\w*|devadesatk\w*|osmdesatk\w*|"
    r"hit|hity|hitu|hitovk\w*|muzik\w*|hudb\w*|rano|ranni\w*|vecer\w*|noc|nocni\w*|"
    r"party|mejdan\w*|nalad\w*|pisn\w*|pisnick\w*|sklad\w*|album\w*|playlist\w*|"
    r"radi[ao]|jin[aeyo]|jinou|jineho|jiny|stejn\w*|takov\w*|podobn\w*|nejak\w*|"
    r"vic|min|nahodn\w*|cokoli\w*|neco|nic)$"
)

# Koncovky, které čeština přilepí ke jménu (Stypka → Stypku, Farná → Farnou,
# Kabát → Kabátu, Lucie → Lucii, David → Davida). Nic jiného se nepřipouští —
# "kabaret" není "Kabát".
_ENDINGS = {
    "", "a", "e", "i", "o", "u", "y", "ou", "em", "ovi", "ho", "mu", "ech",
    "ove", "ami", "ich", "im", "ym", "ymi", "ovou", "ove",
}


def declined(word: str, token: str) -> bool:
    """Je `token` tvar slova `word` (obojí po norm)?"""
    if token == word:
        return True
    if len(word) < 3:
        return False
    stem = word[:-1] if word[-1] in "aeiouy" else word
    return token.startswith(stem) and token[len(stem):] in _ENDINGS


def name_matches(tokens: list[str], artist_name: str) -> bool:
    """Přísná shoda: posluchač řekl přesně tohle jméno (jen jinak skloněné).

    Všechna slova jména musí padnout na různá slova zadání a zadání nesmí mít
    nic navíc. Samotné příjmení se nebere: "pusť Nohavicu" katalog vyložil
    jako Petra Nohavicu (ověřeno 25. 9.), posluchač chtěl Jaromíra — to ať
    rozhodne model, který ví, kdo je známější.
    """
    words = [w for w in norm(artist_name).split() if w not in {"the"}]
    toks = [t for t in tokens if t != "the"]
    if not words or not toks:
        return False
    if "".join(toks) == "".join(words):
        return True  # "tribalneed" / "Tribal Need", "acdc" / "AC/DC"
    if len(toks) == len(words):
        return all(declined(w, t) for w, t in zip(words, toks))
    return False


_UNDECLINE = [("ovou", "ova"), ("ou", "a"), ("ii", "ie"), ("u", "a"), ("u", ""),
              ("y", "a"), ("a", ""), ("e", "a")]


def _guesses(token: str) -> list[str]:
    """Možné 1. pády slova, nejpravděpodobnější první."""
    out = []
    for suffix, repl in _UNDECLINE:
        if len(token) >= len(suffix) + 2 and token.endswith(suffix):
            g = token[: -len(suffix)] + repl
            if g not in out:
                out.append(g)
    return out


def variants(tokens: list[str]) -> list[str]:
    """Co hledat v katalogu: jak to posluchač napsal, a pak odhady 1. pádu.

    Každé slovo zvlášť ("davida stypku" → "david stypka"); katalog hledá
    volně, takže obvykle stačí už první dotaz.
    """
    out = [" ".join(tokens)]
    for k in range(2):
        q = " ".join(
            (_guesses(t)[k] if len(_guesses(t)) > k else (_guesses(t) or [t])[-1])
            for t in tokens
        )
        if q not in out:
            out.append(q)
    return out


@dataclass
class Parsed:
    """Jméno (nebo jména) z přání; `whole` = celý úsek i se spojkami."""

    whole: list[str]
    groups: list[list[str]] = field(default_factory=list)


def parse(text: str) -> Parsed | None:
    """ "pusť Kabát" → Parsed(["kabat"]); cokoli jiného než sloveso+jméno → None."""
    raw = norm(text)
    if not raw or local_command(text):
        return None
    if _SINGLE.search(raw) or _LIKE.search(raw) or _EXCEPT.search(raw):
        return None
    tokens = raw.split()
    content = [i for i, t in enumerate(tokens) if t not in _FILLER]
    if not content:
        return None
    # jméno je jeden souvislý úsek (spojky uvnitř smí být)
    span = tokens[content[0]: content[-1] + 1]
    if any(t in _FILLER and t not in _JOIN for t in span):
        return None
    names = [t for t in span if t not in _JOIN]
    if not names or len(names) > MAX_NAME_TOKENS:
        return None
    if any(_NOT_NAME.match(t) for t in names):
        return None
    if any(t.isdigit() for t in names):
        return None  # "hlasitost 70", roky, čísla skladeb
    groups: list[list[str]] = []
    cur: list[str] = []
    for t in span:
        if t in _JOIN:
            if cur:
                groups.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        groups.append(cur)
    return Parsed(whole=span, groups=groups if len(groups) > 1 else [])


# ---- s katalogem ----


@dataclass
class FastResult:
    artists: list[str] = field(default_factory=list)  # jména z katalogu
    tracks: list[list[Any]] = field(default_factory=list)  # jejich skladby
    reason: str = ""  # proč ne (pro log); prázdné = přijato
    lookups: int = 0


async def _one(
    catalog, tokens: list[str], res: FastResult, max_variants: int = 4
) -> tuple[str, list] | None:
    """Jeden interpret: najít, přísně ověřit, vyloučit název skladby, stáhnout skladby."""
    artist = None
    for q in variants(tokens)[:max_variants]:
        res.lookups += 1
        found = await catalog.find_artist(q)
        if found and name_matches(tokens, found.name):
            artist = found
            break
    if artist is None:
        res.reason = "no_strict_match:" + " ".join(tokens)
        return None

    query = " ".join(tokens)

    async def title_clash() -> bool:
        """Je to spíš název skladby jiného interpreta? ("pusť Wonderwall")"""
        try:
            songs = await catalog.search(query, limit=5)
        except Exception:
            return True  # nevíme → radši model
        for s in songs[:3]:
            if artist_match(s.artist, artist.name):
                continue
            title = norm(s.title).split()
            if title and (title == tokens or name_matches(tokens, s.title)):
                return True
        return False

    # obojí naráz; když jde o název skladby, na skladby interpreta se nečeká
    # (jde se k modelu a každá vteřina tady je vteřina navíc)
    tracks_task = asyncio.ensure_future(catalog.artist_tracks(artist.name, limit=100))
    res.lookups += 2
    try:
        clash = await title_clash()
    except BaseException:
        tracks_task.cancel()
        raise
    if clash:
        tracks_task.cancel()
        res.reason = "song_title:" + query
        return None
    tracks = await tracks_task
    if not tracks:
        res.reason = "no_tracks:" + artist.name
        return None
    return artist.name, tracks


async def find_artists(catalog, text: str) -> FastResult:
    """Interpret(i) z přání, nebo FastResult s `reason`, proč to jde k modelu."""
    res = FastResult()
    parsed = parse(text)
    if parsed is None:
        res.reason = "not_artist_phrase"
        return res
    # nejdřív celé jméno ("Mňága a Žďorp"), pak po jednotlivých jménech;
    # celek se spojkou jen jedním dotazem, ať "Kabát a Škwor" nestojí čtyři
    options = [(parsed.whole, 4)] if not parsed.groups else [(parsed.whole, 1)]
    for groups, tries in [([whole], n) for whole, n in options] + (
        [(parsed.groups, 4)] if parsed.groups else []
    ):
        # víc interpretů najednou, ne jeden po druhém
        found = await asyncio.gather(*(_one(catalog, g, res, tries) for g in groups))
        if all(found):
            res.artists = [a for a, _ in found]
            res.tracks = [t for _, t in found]
            res.reason = ""
            return res
    return res
