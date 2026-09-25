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
    if token == stem:
        return False  # "Olympic" není "Olympica" — koncovou samohlásku čeština nezahodí
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
    joined_t, joined_w = "".join(toks), "".join(words)
    if joined_t == joined_w:
        return True  # "tribalneed" / "Tribal Need", "acdc" / "AC/DC"
    if len(toks) == len(words) and all(declined(w, t) for w, t in zip(words, toks)):
        return True
    # Rozdělené nebo s překlepem: "z nouze cnost" = Znouzectnost, "tata boys" =
    # Tata Bojs. Jen u dlouhých jmen a o jedno písmeno (u velmi dlouhých dvě) —
    # krátká jména by se tak pletla navzájem.
    # Začátek i konec musí sedět: "Olympic" není "Olympica" (jiná kapela,
    # liší se jen koncem — tam, kde čeština skloňuje).
    if (
        len(joined_w) >= 8 and joined_t[:2] == joined_w[:2]
        and joined_t[-1] == joined_w[-1]
    ):
        return _within(joined_t, joined_w, 1 if len(joined_w) < 13 else 2)
    return False


def _within(a: str, b: str, limit: int) -> bool:
    """Levenshteinova vzdálenost a↔b je nejvýš `limit`."""
    if abs(len(a) - len(b)) > limit:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > limit:
            return False
        prev = cur
    return prev[-1] <= limit


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
    if len(tokens) > 1 and "".join(tokens) not in out:
        # "z nouze cnost" → "znouzecnost"; s předložkou hned jako druhý dotaz
        out.insert(1 if len(tokens[0]) == 1 else len(out), "".join(tokens))
    return out


@dataclass
class Parsed:
    """Jméno (nebo jména) z přání; `whole` = celý úsek i se spojkami."""

    whole: list[str]
    groups: list[list[str]] = field(default_factory=list)
    # jednopísmenná předložka těsně před jménem — může k němu patřit
    # ("z nouze ctnost" = Znouzectnost)
    lead: str = ""


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
    lead = tokens[content[0] - 1] if content[0] > 0 else ""
    return Parsed(
        whole=span, groups=groups if len(groups) > 1 else [],
        lead=lead if len(lead) == 1 and lead.isalpha() else "",
    )


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
    options = [(parsed.whole, 2 if parsed.lead else 4)] if not parsed.groups else [(parsed.whole, 1)]
    for groups, tries in [([whole], n) for whole, n in options] + (
        [(parsed.groups, 4)] if parsed.groups else []
    ) + ([([[parsed.lead] + parsed.whole], 2)] if parsed.lead else []):
        # víc interpretů najednou, ne jeden po druhém
        found = await asyncio.gather(*(_one(catalog, g, res, tries) for g in groups))
        if all(found):
            res.artists = [a for a, _ in found]
            res.tracks = [t for _, t in found]
            res.reason = ""
            return res
    return res


# ---- konkrétní skladba: "pusť Jasnou zprávu od Olympicu", "Oasis - Wonderwall" ----

_DASH_SPLIT = re.compile(r"\s+[-–—]\s+")


@dataclass
class SongParse:
    """Kandidáti (interpret, název) jak je posluchač napsal, v pořadí zkoušení."""

    pairs: list[tuple[str, str]]


def _strip_lead(words: list[str]) -> list[str]:
    """Sloveso a vata na začátku pryč ("pusť mi prosím …")."""
    i = 0
    while i < len(words) and norm(words[i]) in _FILLER:
        i += 1
    return words[i:]


def _is_title(words: list[str]) -> bool:
    """Může to být název skladby? Ne, když je to jen vata nebo nálada."""
    toks = [t for t in (norm(w) for w in words) if t]
    content = [t for t in toks if t not in _FILLER]
    if not content or len(toks) > 10:
        return False
    # "něco klidného od Kabátu" — přání nálady, ne název
    return not all(_NOT_NAME.match(t) for t in content)


def parse_song(text: str) -> SongParse | None:
    """Přání konkrétní skladby, nebo None (pak rozhodne model)."""
    raw = norm(text)
    if not raw or local_command(text):
        return None
    if _SINGLE.search(raw) or _LIKE.search(raw) or _EXCEPT.search(raw):
        return None
    pairs: list[tuple[str, str]] = []
    dash = _DASH_SPLIT.split(text.strip(), maxsplit=1)
    if len(dash) == 2:
        left = _strip_lead(dash[0].split())
        right = dash[1].split()
        if left and right and _is_title(left) and _is_title(right):
            a, b = " ".join(left), " ".join(right)
            pairs += [(a, b), (b, a)]  # "Oasis - Wonderwall" i "Wonderwall - Oasis"
    else:
        words = text.strip().split()
        idx = [i for i, w in enumerate(words) if norm(w) == "od"]
        if len(idx) == 1:
            title = _strip_lead(words[: idx[0]])
            artist = [w for w in words[idx[0] + 1:] if norm(w) not in _FILLER]
            if title and artist and _is_title(title) and len(artist) <= MAX_NAME_TOKENS:
                if not any(_NOT_NAME.match(norm(w)) for w in artist):
                    pairs.append((" ".join(artist), " ".join(title)))
    return SongParse(pairs) if pairs else None


def _clean_words(text: str) -> list[str]:
    return [t for t in norm(text).split() if t]


def _artist_ok(tokens: list[str], name: str) -> bool:
    """Jako name_matches, ale u skladby stačí i příjmení ("Kometu od Nohavici"):
    nejednoznačnost jména tu rozhodne název, který musí sedět taky."""
    if name_matches(tokens, name):
        return True
    words = [w for w in norm(name).split() if w != "the"]
    return (
        len(tokens) == 1 and len(words) >= 2 and len(words[-1]) >= 4
        and declined(words[-1], tokens[0])
    )


def song_matches(track, artist: str, title: str) -> bool:
    """Přísně: interpret i vlastní název (bez verzí v závorkách) sedí na zadání."""
    from ..music.match import split_title  # čistá funkce hudební vrstvy

    a_toks = [t for t in _clean_words(artist) if t != "the"]
    credited = [p for p in re.split(r"\s*(?:,|&| feat\.? | ft\.? | x )\s*", track.artist) if p]
    if not any(_artist_ok(a_toks, c) for c in credited + [track.artist]):
        return False
    base, _tags = split_title(track.title)
    return name_matches(_clean_words(title), base)


@dataclass
class SongResult:
    track: Any = None
    artist: str = ""
    title: str = ""
    reason: str = ""
    lookups: int = 0


SONG_BUDGET = 3.5  # s — déle to nesmí zdržet cestu k modelu (zásah na laptopu 0.4–2 s)


async def find_song(catalog, text: str, budget: float = SONG_BUDGET) -> SongResult:
    """Konkrétní skladba z přání — jen když katalog potvrdí interpreta i název.

    Nejdéle `budget` sekund; pak to jde k modelu, jako by se nic nenašlo.
    """
    res = SongResult()
    parsed = parse_song(text)
    if parsed is None:
        res.reason = "not_song_phrase"
        return res
    try:
        return await asyncio.wait_for(_find_song(catalog, parsed, res), budget)
    except asyncio.TimeoutError:
        res.track = None
        res.reason = "timeout"
        return res


async def _find_song(catalog, parsed: SongParse, res: SongResult) -> SongResult:
    async def attempt(a_q: str, t_q: str, artist: str, title: str) -> bool:
        res.lookups += 1
        hit = await catalog.search_song(a_q, t_q)
        # katalog umí vrátit "aspoň něco od něj" — bere se jen přesná shoda
        if hit is not None and song_matches(hit, artist, title):
            res.track, res.artist, res.title, res.reason = hit, a_q, title, ""
            return True
        return False

    try:
        for artist, title in parsed.pairs:
            a_var, t_var = variants(_clean_words(artist)), variants(_clean_words(title))
            # 1) rovnou hledání skladby: jak to napsal, pak odhad 1. pádu
            #    ("Jasnou zprávu od Olympicu" → "olympic" / "jasna zprava")
            if await attempt(artist, title, artist, title):
                return res
            if len(a_var) > 1 or len(t_var) > 1:
                a1 = a_var[1] if len(a_var) > 1 else artist
                t1 = t_var[1] if len(t_var) > 1 else title
                if await attempt(a1, t1, artist, title):
                    return res
            # 2) přes profil interpreta (1. pád od katalogu), víc kandidátů
            tried: set[str] = set()
            for q in a_var[:3]:
                res.lookups += 1
                found = await catalog.find_artist(q)
                if not found or found.name in tried or not _artist_ok(_clean_words(artist), found.name):
                    continue
                tried.add(found.name)
                for t_q in t_var[:2]:
                    if await attempt(found.name, t_q, artist, title):
                        return res
            res.reason = f"no_strict_match:{artist} — {title}"
    except Exception as exc:  # katalog umí selhat na čemkoli
        res.track = None
        res.reason = f"error:{type(exc).__name__}"
    return res


# ---- ověření vyžádaných skladeb z modelu ----


def title_close(asked: str, catalog_title: str) -> bool:
    """Je skladba z katalogu ta, o kterou se žádalo? Volnější než song_matches.

    Model píše názvy kanonicky, katalog přidává verze a předpony
    ("Einaudi: Nuvole Bianche"). Stačí, když jedna strana celá leží v druhé
    (s českými koncovkami). Co nesdílí ani slovo, je jiná skladba — 25. 9.
    tak na "Kabát — Z nouze ctnost" přišla "Malá dáma" a odpověď lhala.
    """
    from ..music.match import split_title

    a = _clean_words(asked)
    base, _tags = split_title(catalog_title)
    c = _clean_words(base) or _clean_words(catalog_title)
    if not a:
        return True  # "cokoli od něj"
    if not c:
        return False
    if name_matches(a, " ".join(c)):
        return True

    def inside(small: list[str], big: list[str]) -> bool:
        return all(any(t == w or declined(w, t) or declined(t, w) for w in big) for t in small)

    return inside(a, c) or inside(c, a)


def verify_requested(pairs: list[tuple[str, str]], tracks: list[Any]) -> tuple[list[Any], list[str]]:
    """Rozdělí dohledané skladby na (ty pravé, popisy nesedících)."""
    ok, wrong = [], []
    for t in tracks:
        match = next(
            (p for p in pairs if not p[1] or title_close(p[1], t.title)), None
        )
        if match is None:
            wanted = next((f"{a} — {ti}" for a, ti in pairs if a and _artist_ok(_clean_words(a), t.artist)), "")
            wrong.append(f"„{wanted or t.label()}“ (katalog nabídl {t.label()})")
        else:
            ok.append(t)
    return ok, wrong
