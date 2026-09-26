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
    # "jen od X", "originál od X", "od X, ne jinou verzi" — jiná verze nepřipadá v úvahu
    insist: bool = False


# Posluchač trvá na interpretovi. Bez toho platí: nemá-li ji jmenovaný, ale
# skladba zjevně existuje od někoho jiného, nejspíš si spletl interpreta —
# hraje se ta, s poctivou poznámkou.
_INSIST = re.compile(
    r"(?:,\s*)?\b(?:ne\s+jinou\s+verzi|žádnou\s+jinou|zadnou\s+jinou|ne\s+cover\w*|"
    r"not\s+a\s+cover|originál\w*|original\w*|jen(?:om)?(?=\s+od\b)|"
    r"pouze(?=\s+od\b)|only(?=\s+by\b))\b[,\s]*",
    re.I,
)


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
    insist = bool(_INSIST.search(text))
    text = re.sub(r"\s+", " ", _INSIST.sub(" ", text)).strip(" ,")
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
            artist = [w.strip(",") for w in words[idx[0] + 1:] if norm(w) not in _FILLER]
            if title and artist and _is_title(title) and len(artist) <= MAX_NAME_TOKENS:
                if not any(_NOT_NAME.match(norm(w)) for w in artist):
                    pairs.append((" ".join(artist), " ".join(title)))
            elif not title:
                pairs += _artist_then_title(words[idx[0] + 1:])
    return SongParse(pairs, insist) if pairs else None


# "od <interpret> píseň <název>" — Pi 26. 9. 9:23: "Zahraj od Vojtano píseň
# Budulínek nebo galantní jelen nebo jak se to jmenuje" šlo k modelu (a na
# studeném Codexu skončilo chybou), i když interpret i název byly v textu.
_SONG_NOUN = {"pisen", "pisnicku", "pisnicka", "skladbu", "skladba", "song", "vec", "hit", "track"}
_TITLE_TAIL = re.compile(
    r"\s*(?:,|\bnebo jak se (?:to )?jmenuje\b|\bnebo tak nejak\b|\bnebo tak\b|\bnebo co\b"
    r"|\bjak se (?:to )?jmenuje\b|\bprosim\b|\bprosimte\b).*$", re.I)


def _artist_then_title(rest: list[str]) -> list[tuple[str, str]]:
    """ "Vojtano píseň Budulínek nebo galantní jelen" → [(Vojtano, Budulínek),
    (Vojtano, galantní jelen)] — alternativy, které posluchač nabídl."""
    noun = next((i for i, w in enumerate(rest) if norm(w) in _SONG_NOUN), None)
    if noun is None or noun == 0:
        return []
    artist = [w.strip(",") for w in rest[:noun] if norm(w) not in _FILLER]
    if not artist or len(artist) > MAX_NAME_TOKENS or any(_NOT_NAME.match(norm(w)) for w in artist):
        return []
    tail = _TITLE_TAIL.sub("", " ".join(rest[noun + 1:])).strip()
    out: list[tuple[str, str]] = []
    for alt in re.split(r"\s+(?:nebo|or|anebo)\s+", tail)[:2]:
        words = alt.strip(" ,.?!").split()
        if words and _is_title(words):
            out.append((" ".join(artist), " ".join(words)))
    return out


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
    asked = _clean_words(title)
    if name_matches(asked, base):
        return True
    # spojené skladby / medley ("Budulínek vs. Galantní Jelen", "A / B"): stačí
    # celá jedna část — interpret už sedí přísně
    parts = re.split(r"\s+(?:vs\.?|versus|/|&|x|\+)\s+", base, flags=re.I)
    return len(parts) > 1 and any(name_matches(asked, p) for p in parts)


@dataclass
class SongResult:
    track: Any = None
    artist: str = ""
    title: str = ""
    reason: str = ""
    lookups: int = 0
    note: str = ""  # "Od Olympicu ji nemám — hraju verzi …"


SONG_BUDGET = 7.0  # s — na Pi trvá jeden dotaz ~1.5–2 s (dj.fast_path 23:46: 2 dotazy za 3.5 s)


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


async def _search_verified(catalog, artist: str, title: str, queries) -> Any:
    """Všechny dotazy (interpret, název) naráz; první ověřený v pořadí dotazů.

    Na Pi je dotaz do katalogu ~1.5–2 s, takže tvary jména ("Olympicu",
    "Olympic") a názvu se nezkoušejí po sobě, ale současně. `strict=True`:
    bez náhrad "aspoň něco od něj" a bez dalších kol v katalogu.
    """
    async def one(a_q: str, t_q: str):
        try:
            return await catalog.search_song(a_q, t_q, strict=True)
        except TypeError:  # katalog bez `strict` (starší / atrapa)
            return await catalog.search_song(a_q, t_q)
        except Exception:
            return None

    hits = await asyncio.gather(*(one(a, t) for a, t in queries), return_exceptions=True)
    for hit in hits:
        if isinstance(hit, BaseException):
            continue
        if hit is not None and song_matches(hit, artist, title):
            return hit
    return None


def _queries(artist: str, title: str) -> list[tuple[str, str]]:
    a_var = variants(_clean_words(artist))
    t_var = variants(_clean_words(title))
    out = [(artist, title)]
    for a in [artist] + a_var[1:3]:
        for t in [title] + t_var[1:2]:
            if (a, t) not in out:
                out.append((a, t))
    return out[:6]


def title_strong(asked: str, catalog_title: str) -> bool:
    """Název sedí celý (s českými koncovkami), ne jen z části."""
    from ..music.match import split_title

    base = split_title(catalog_title)[0] or catalog_title
    return name_matches(_clean_words(asked), base)


async def other_version(catalog, title: str) -> Any:
    """Nejznámější nahrávka toho názvu od kohokoli — jen při silné shodě názvu."""
    async def one(t_q: str):
        try:
            return await catalog.search_song("", t_q, strict=True)
        except TypeError:
            return await catalog.search_song("", t_q)
        except Exception:
            return None

    queries = variants(_clean_words(title))[:2]
    queries[0] = title
    for hit in await asyncio.gather(*(one(q) for q in queries), return_exceptions=True):
        if hit is not None and not isinstance(hit, BaseException) and title_strong(title, hit.title):
            return hit
    return None


async def _via_profile(catalog, artist: str, title: str, res: SongResult) -> Any:
    a_tok = _clean_words(artist)

    async def find(q: str):
        try:
            return await catalog.find_artist(q)
        except Exception:
            return None

    found = await asyncio.gather(*(find(q) for q in variants(a_tok)[:3]))
    res.lookups += len(found)
    names: list[str] = []
    for f in found:
        if f and not isinstance(f, BaseException) and f.name not in names \
                and _artist_ok(a_tok, f.name):
            names.append(f.name)
    if not names:
        return None
    more = [(n, t) for n in names for t in [title] + variants(_clean_words(title))[1:2]]
    res.lookups += len(more)
    return await _search_verified(catalog, artist, title, more)


def other_version_note(named: str, track: Any) -> str:
    return f"Od {named} ji nemám — hraju verzi {track.artist}."


async def _scan(catalog, artist: str, title: str) -> Any:
    """Obyčejné hledání "interpret název" a první výsledek, který projde.

    Hledání skladby s interpretem vetuje "Nohavici" vůči "Jaromir Nohavica";
    seznam výsledků ho ale obsahuje a naše kontrola (příjmení + celý název)
    ho pozná. Jeden dotaz.
    """
    search = getattr(catalog, "search", None)
    if search is None:
        return None
    try:
        hits = await search(f"{artist} {title}", limit=10)
    except Exception:
        return None
    return next((h for h in hits if song_matches(h, artist, title)), None)


async def _find_song(catalog, parsed: SongParse, res: SongResult) -> SongResult:
    try:
        for artist, title in parsed.pairs:
            queries = _queries(artist, title)
            res.lookups += len(queries)
            hit, scanned = await asyncio.gather(
                _search_verified(catalog, artist, title, queries),
                _scan(catalog, artist, title),
            )
            res.lookups += 1
            hit = hit or scanned
            if hit is None:
                # Přes profil: katalog dá 1. pád a celé jméno ("Nohavici" →
                # Jaromír Nohavica); hledání skladeb samo příjmení neveme.
                hit = await _via_profile(catalog, artist, title, res)
            if hit is not None:
                res.track, res.artist, res.title, res.reason = hit, artist, title, ""
                return res
            res.reason = f"no_strict_match:{artist} — {title}"
        # Jmenovaný interpret ji nemá. Když posluchač netrval na něm a skladba
        # zjevně existuje od někoho jiného, nejspíš si spletl interpreta.
        if len(parsed.pairs) == 1 and not parsed.insist:
            artist, title = parsed.pairs[0]
            res.lookups += 2
            other = await other_version(catalog, title)
            if other is not None:
                res.track, res.artist, res.title, res.reason = other, artist, title, ""
                res.note = other_version_note(artist, other)
                return res
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


def named_artists(text: str) -> list[str]:
    """Interpret, kterého posluchač v zadání výslovně jmenoval ("… od Olympicu").

    Jen jednoznačná forma "<název> od <interpret>"; u "A - B" není jisté, co je
    co, a vrací se prázdno (pak rozhoduje shoda názvu jako dřív).
    """
    parsed = parse_song(text)
    if not parsed or len(parsed.pairs) != 1:
        return []
    return [parsed.pairs[0][0]]


def credited_to(track, artist: str) -> bool:
    return any(
        _artist_ok(_clean_words(artist), c)
        for c in re.split(r"\s*(?:,|&| feat\.? | ft\.? | x )\s*", track.artist) + [track.artist]
        if c
    )


async def enforce_requested(
    catalog, text: str, pairs: list[tuple[str, str]], tracks: list[Any]
) -> tuple[list[Any], list[str], list[str]]:
    """Vyžádané skladby: (co hrát, co nesedí, poctivé poznámky k odpovědi).

    Název musí sedět vždy. Když posluchač jmenoval interpreta ("… od
    Olympicu"):
      - má ji ten interpret → hraje jeho verze (i když model vybral jinou),
      - nemá, ale název zjevně existuje od jiného → hraje ta, s poznámkou
        "Od Olympicu ji nemám — hraju verzi …" (nejspíš si spletl interpreta),
      - trval na něm ("jen od", "originál od", "ne jinou verzi") nebo se
        název shoduje jen zčásti → nenašel jsem.
    23:46 na Pi: model vybral nahrávku Vágnera, Hložka a Kotvalda a odpověď
    o tom mlčela.
    """
    ok, wrong = verify_requested(pairs, tracks)
    notes: list[str] = []
    parsed = parse_song(text)
    if not parsed or len(parsed.pairs) != 1:
        return ok, wrong, notes
    who, asked_title = parsed.pairs[0]
    from ..music.match import split_title

    good: list[Any] = []
    candidates = ok or [None]  # nic nesedí → zkusit přání doslova
    for t in candidates:
        if t is not None and credited_to(t, who):
            good.append(t)
            continue
        title = asked_title
        if t is not None:
            title = next((ti for _a, ti in pairs if ti and title_close(ti, t.title)), t.title)
            title = split_title(title)[0] or title
        own, scanned = await asyncio.gather(
            _search_verified(catalog, who, title, _queries(who, title)),
            _scan(catalog, who, title),
        )
        own = own or scanned
        if own is None:
            own = await _via_profile(catalog, who, title, SongResult())
        if own is not None:
            good.append(own)
            continue
        if parsed.insist:
            wrong.append(f"„{title}“ od {who}")
            continue
        other = t if t is not None and title_strong(title, t.title) else None
        if other is None:
            other = await other_version(catalog, title)
        if other is not None:
            good.append(other)
            notes.append(other_version_note(who, other))
        else:
            wrong.append(f"„{title}“ od {who}")
    return good, wrong, notes


# ---- přání schované v otázce: "proč nehraješ Kabát?", "kdy bude Bohemian Rhapsody" ----
#
# Otázku / stížnost na frontu (intent.meta_kind) fronta zodpoví sama. Když
# se v ní ale někdo o hudbu hlásí (intent.meta_wants_music), zkusí se zbytek
# textu jako jméno interpreta nebo skladby — přijme se jen to, co potvrdí
# katalog (rychlá cesta), jinak zůstává otázkou. Pi review 26. 9.: 14 přání
# typu "proč nehraješ Kabát?" dostalo jen odpověď o stavu fronty.

_META_WORDS = set("""
proc kdy kde kdo co jak kolik uz bude budou prijde prijdou zahrajes zahraje hraje hrajes
hrat hral hrala hrali nehraje nehrajes nehraj nehrajete nehral nehrala nepustil nepustila
nepustis nepoustis nepousti nezahral nezahrajes nedal nedala jsi jste je jsou se sem
nic vubec zase porad furt dokola same samy sama stejne stejny ted tady
moje muj moji mych me mi mne mne sebou mnou prede pred
chtel chtela pustil pustila vybral vybrala preje prani
ignoruj ignoruju ignorovat predchozi predtim vsechno
nefer fer neni demokracie spravedlive nespravedlive nestrida zadne stridani stridat strida
nenastavil nenastavila musi musis mel mela by bys dlouho cekat cekam cekame
mam rad rada ne ano no tak takze proste fakt displej displeje displeji fronta fronte frontu
poradi rade dojde pustis
""".split())

# celé slovo, které nikdy není jméno ani název (nálada, žánr, obecné)
_SPLIT_CLAUSE = re.compile(r"[,.;:!?()\[\]]+|\s[-–—]\s")


def _content(words: list[str]) -> list[str]:
    return [w for w in words if (n := norm(w)) and n not in _META_WORDS
            and n not in _FILLER and n not in _JOIN]


def meta_candidates(text: str) -> list[str]:
    """Kandidáti na jméno / název z otázky, v pořadí zkoušení; [] = čistá otázka.

    Jen když se otázka o hudbu hlásí (intent.meta_wants_music). "Píseň
    (Interpret)" → nejdřív "Píseň od Interpret" (rychlá cesta skladby), pak
    interpret sám.
    """
    from .intent import meta_wants_music

    if not text or not meta_wants_music(text):
        return []
    out: list[str] = []
    paren = re.search(r"^(.*?)\(([^()]{2,40})\)\s*[?!.]*$", text.strip())
    if paren:
        title = " ".join(paren.group(1).split())
        who = " ".join(paren.group(2).split())
        if title and _is_title(title.split()):
            out.append(f"{title} od {who}")
        out.append(who)
    for clause in _SPLIT_CLAUSE.split(text):
        words = _content(clause.split())
        if not words or len(words) > MAX_NAME_TOKENS + 3:
            continue
        if all(_NOT_NAME.match(norm(w)) for w in words):
            continue
        cand = " ".join(w.strip("\"'„“”‚‘’") for w in words).strip()
        if cand and cand not in out:
            out.append(cand)
    return out[:3]
