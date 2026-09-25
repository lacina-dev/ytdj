"""Jak poznat, že výsledek hledání je ta skladba, o kterou si posluchač řekl.

Čisté funkce bez sítě — skórují syrové výsledky z ytmusicapi (a z nich
vyrobené `Track`), takže se dají testovat na nahraných odpovědích.

Co rozhoduje, v tomhle pořadí:

1. **Interpret** je veto: cizí kapela se nebere, ani když název sedí na sto
   procent. Porovnává se s každým uvedeným interpretem zvlášť — "Prince &
   The Revolution" je Prince, "Vokální kvintet…, Jiří Suchý" je i Suchý.
2. **Název** je taky veto: interpret, který sedí, nestačí. Dřív na "Monkey
   Business — Piece of My Life" vyhrála jejich "Blue Light Baggie Bingo",
   protože shoda interpreta 1.0 přebila nesouvisející název.
3. **Verze**: živák, remix, akustika, instrumentálka, karaoke, cover,
   zrychlené/zpomalené — to všechno se trestá, *pokud o to nežádal*. Když
   o to žádal ("One More Time (remix)"), trestá se naopak verze bez toho.
4. **Popularita** (počet přehrání z YouTube Music) rozhoduje mezi jinak
   rovnocennými kandidáty: kanonická nahrávka má řádově víc přehrání než
   kompilace z výprodeje ("Praminek vlasu" z alba "Blues pro Tebe", 2,5 tis.
   proti 59 tis. originálu ze Semaforu).
5. Drobnosti na dorovnání: diakritika přesně jako v zadání, interpret uvedený
   jako první, pořadí ve výsledcích hledání.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable, Sequence

# Pod tímhle už nejde o toho interpreta, ale o někoho jiného.
ARTIST_FLOOR = 0.5
# Pod tímhle už nejde o tu skladbu. "Nuvole Bianche" proti "Einaudi: Nuvole
# Bianche" dá 0.82, "Piece of My Life" proti "Blue Light Baggie Bingo" 0.3.
TITLE_FLOOR = 0.7
# Celkové minimum; s oběma vety výše už jen pojistka.
SCORE_FLOOR = 0.55

W_ARTIST = 0.6
W_TITLE = 0.4
W_POPULARITY = 0.15
BONUS_DIACRITICS = 0.05
BONUS_PRIMARY = 0.03
RANK_STEP = 0.005

# Znaky, které NFKD nerozloží — polština, skandinávie, němčina.
_FOLD = str.maketrans({"ł": "l", "ø": "o", "đ": "d", "ß": "ss", "æ": "ae", "œ": "oe"})


def clean(text: str) -> str:
    return text.replace("\xa0", " ").strip()


def norm(text: str) -> str:
    """Bez diakritiky, bez interpunkce, malými písmeny.

    "Děda Mládek" a "Deda Mladek" musí padnout na sebe — YouTube Music vrací
    jednou tak, jednou onak.
    """
    text = unicodedata.normalize("NFKD", clean(text).lower().translate(_FOLD))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def similar(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # Kapely se píšou jednou dohromady, jednou zvlášť: "TribalNeed" a
    # "Tribal Need" je totéž jméno. A "Pánu bohu do oken" je na YTM
    # "Panubohudooken".
    if a.replace(" ", "") == b.replace(" ", ""):
        return 1.0
    # Částečná shoda jen po celých slovech ("nohavica" v "jaromir nohavica"),
    # a tím nižší, čím menší část delšího tvoří. Holé `a in b` dávalo 0.9 i
    # pro "jez" v "jezebel" nebo "love" v "lovesong".
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if f" {short} " in f" {long_} ":
        return 0.7 + 0.2 * len(short) / len(long_)
    return SequenceMatcher(None, a, b).ratio()


# ---- verze skladby ----

# Co se v závorce objevuje, ale verzi to nemění — maže se dřív, než se hledají
# značky, aby "Original Mix" nebo "2009 Stereo Mix" nevypadal jako remix.
_NEUTRAL = re.compile(
    r"\b(original|radio|album|single|clean|explicit)\s+(mix|edit|version|verze)\b"
    r"|\b(19|20)\d\d\s+(stereo\s+|mono\s+)?(re)?mix\b"
    r"|\b(stereo|mono)\s+mix\b"
)

# (kategorie, vzor na normalizovaném textu značek, trest když o ni nežádal)
_TAGS: tuple[tuple[str, re.Pattern[str], float], ...] = (
    ("live", re.compile(
        r"\b(on)?live\b|\bnazivo\b|\bna zivo\b|\bkoncert\w*|\bconcert\b|\bunplugged\b"
        r"|\bsessions?\b|\btour\b|\bfestival\b|\bmtv\b"), 0.3),
    ("remix", re.compile(
        r"\bre ?mix\w*|\brmx\b|\bmix\b|\brework\b|\bbootleg\b|\bvip\b|\bflip\b"
        r"|\bmashup\b|\bdub\b|\bsympho\w*|\borchestral\b|\bsymphonic\b"), 0.3),
    ("extended", re.compile(r"\bextended\b|\b12 inch\b|\b12\b|\bmaxi\b"), 0.1),
    ("acoustic", re.compile(r"\bacoustic\b|\bakusti\w*|\bunplugged\b|\bpiano version\b"), 0.2),
    ("instrumental", re.compile(
        r"\binstrumental\w*|\bkaraoke\b|\bbacking track\b|\bminus one\b|\bplayback\b"), 0.5),
    ("cover", re.compile(
        r"\bcover\b|\btribute\b|\boriginally performed\b|\bin the style of\b"
        r"|\bmade famous\b|\bas performed by\b"), 0.5),
    ("speed", re.compile(
        r"\bsped up\b|\bspeed up\b|\bslowed\b|\bnightcore\b|\b8d\b|\breverb\b"
        r"|\bbass boosted\b|\bdaycore\b"), 0.5),
    ("demo", re.compile(r"\bdemo\b|\brehearsal\b|\bouttake\b|\btake \d+\b"), 0.2),
)
# O co si posluchač řekl a kandidát to má / nemá.
BONUS_ASKED_TAG = 0.15
PENALTY_MISSING_TAG = 0.2

# Slova, která u zadání bez závorky ("Bohemian Rhapsody live") znamenají verzi.
_TRAILING_TAG = re.compile(
    r"\s+(live|remix|rmx|acoustic|akusticky|unplugged|instrumental|karaoke|cover)$",
    re.I,
)
_BRACKETS = re.compile(r"\([^)]*\)|\[[^\]]*\]|\{[^}]*\}")
_DASH = re.compile(r"\s+[-–—]\s+")
_FEAT = re.compile(r"\s+(feat\.?|ft\.?|featuring)\s+.*$", re.I)


def split_title(title: str) -> tuple[str, str]:
    """"Kiss (Live In Utrecht) [2020 Remaster]" → ("Kiss", "Live In Utrecht 2020 Remaster").

    Závorky a to za pomlčkou ("Wonderwall - Remastered") jsou značky verze,
    zbytek je vlastní název. U zadání se jako značka bere i koncové "live".
    """
    title = clean(title)
    tags = [m.group(0)[1:-1] for m in _BRACKETS.finditer(title)]
    base = _BRACKETS.sub(" ", title)
    parts = _DASH.split(base, maxsplit=1)
    if len(parts) == 2 and parts[0].strip():
        base, rest = parts
        tags.append(rest)
    if m := _FEAT.search(base):
        tags.append(m.group(0))
        base = base[: m.start()]
    while m := _TRAILING_TAG.search(base):
        tags.append(m.group(1))
        base = base[: m.start()]
    base = re.sub(r"\s+", " ", base).strip(" -–—:")
    return (base or title), " ".join(tags)


def version_tags(text: str) -> set[str]:
    """Kategorie verzí, které se v textu značek (nebo alba) objevují."""
    t = _NEUTRAL.sub(" ", norm(text))
    return {name for name, rx, _ in _TAGS if rx.search(t)}


_PENALTY = {name: p for name, _, p in _TAGS}


def tag_adjustment(candidate_tags: set[str], wanted_tags: set[str]) -> float:
    adj = 0.0
    for tag in candidate_tags - wanted_tags:
        adj -= _PENALTY[tag]
    for tag in wanted_tags:
        adj += BONUS_ASKED_TAG if tag in candidate_tags else -PENALTY_MISSING_TAG
    return adj


# ---- interpret a název ----

_ARTIST_SPLIT = re.compile(r"\s*(?:,|;|&|\+|/|\bfeat\.?|\bft\.?|\bfeaturing\b|\bx\b|\bvs\.?)\s*", re.I)


def artist_parts(names: Iterable[str]) -> list[str]:
    """Každý uvedený interpret, a každý z nich ještě rozdělený na "&", "feat."…"""
    out: list[str] = []
    for name in names:
        name = clean(name)
        if not name:
            continue
        out.append(name)
        out.extend(p for p in _ARTIST_SPLIT.split(name) if p and p != name)
    return out


_THE = re.compile(r"^the ")


def artist_similar(a: str, b: str) -> float:
    """Jako `similar`, ale na jména přísnější.

    Volná podobnost znaků u jmen lže: "Deda Mladek" a "Ivan Mladek" mají
    0.73, a jsou to dva různí lidé (a na "Jožin z bažin" od Děda Mládek
    Illegal Band pak vyhrál Ivan Mládek). Proto se od oka bere jen skoro
    přesná shoda (překlep), zkrácené křestní jméno ("Miro Žbirka" =
    "Miroslav Zbirka") a celá slova ("Nohavica" v "Jaromír Nohavica").
    """
    na, nb = _THE.sub("", norm(a)), _THE.sub("", norm(b))
    if not na or not nb:
        return 0.0
    if na == nb or na.replace(" ", "") == nb.replace(" ", ""):
        return 1.0
    ta, tb = na.split(), nb.split()
    if len(ta) == len(tb) > 1 and ta[-1] == tb[-1] and all(
        x.startswith(y) or y.startswith(x) for x, y in zip(ta[:-1], tb[:-1])
    ):
        return 0.9
    short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
    if f" {short} " in f" {long_} ":
        return 0.7 + 0.2 * len(short) / len(long_)
    ratio = SequenceMatcher(None, na, nb).ratio()
    return ratio if ratio >= 0.85 else ratio * 0.5


def artist_score(credited: Sequence[str], wanted: str) -> tuple[float, bool]:
    """(shoda, je to první uvedený interpret)."""
    wanted = clean(wanted)
    if not wanted or not credited:
        return 0.0, False
    wanted_parts = [wanted] + [p for p in _ARTIST_SPLIT.split(wanted) if p and p != wanted]
    best, primary = artist_similar(", ".join(credited), wanted), True
    for i, name in enumerate(credited):
        for part in artist_parts([name]):
            for j, w in enumerate(wanted_parts):
                # jen část zadání ("David Stypka" z "David Stypka, Bandjeez")
                # je o chlup slabší než celé zadání
                s = artist_similar(part, w) * (1.0 if j == 0 else 0.95)
                if s > best:
                    best, primary = s, i == 0
    return best, primary


_SEGMENTS = re.compile(r"[,:;/()\[\]\"“”„]|\s[-–—]\s")


def title_score(candidate: str, wanted: str) -> float:
    cb, _ = split_title(candidate)
    wb, _ = split_title(wanted)
    s = max(similar(cb, wb), 0.95 * similar(candidate, wanted))
    # Klasika a soundtracky mají název poskládaný z dílů: "Má vlast, JB
    # 1:112: No. 2, Vltava (My Country: Moldau)" je "Vltava", celá.
    if s < 0.95 and norm(wb) in {norm(p) for p in _SEGMENTS.split(candidate)}:
        s = 0.95
    return s


def has_diacritics(text: str) -> bool:
    return any(unicodedata.combining(c) for c in unicodedata.normalize("NFKD", text))


def exact_diacritics(candidate: str, wanted: str) -> bool:
    """Zadání s háčky a kandidát se stejnými háčky.

    Levné kompilace a nahrávky z výprodeje mívají jména bez diakritiky
    ("Jiri Suchy — Praminek vlasu"), originál je má. Jen dorovnání, ne
    pravidlo: oficiální "Lucie — Cerni andele" je taky bez háčků.
    """
    if not has_diacritics(wanted):
        return False
    return split_title(candidate)[0].casefold() == split_title(wanted)[0].casefold()


# ---- views ----

_VIEWS = re.compile(r"([\d.,]+)\s*([KMB]?)", re.I)
_MULT = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}


def parse_views(text: object) -> int | None:
    """"2.5K" → 2500, "153M" → 153000000 (anglická lokalizace ytmusicapi)."""
    if isinstance(text, int):
        return text
    if not isinstance(text, str):
        return None
    m = _VIEWS.search(text.replace("\xa0", " "))
    if not m:
        return None
    num, suffix = m.group(1), m.group(2).upper()
    if suffix:
        num = num.replace(",", ".")
    else:
        num = num.replace(",", "").replace(".", "")
    try:
        return int(float(num) * _MULT[suffix])
    except ValueError:
        return None


# ---- kandidáti ----

@dataclass(slots=True)
class Candidate:
    """Výsledek hledání připravený ke skórování.

    `artists` a `title` jsou to, s čím se porovnává — u videa to nemusí být
    kanál a celý název ("Marie J." / "Monkey Business - Piece Of My Life"),
    ale z názvu vyčtený interpret a skladba.
    """
    id: str
    title: str
    artists: tuple[str, ...]
    album: str | None = None
    duration: int | None = None
    views: int | None = None
    rank: int = 0

    @property
    def artist(self) -> str:
        return ", ".join(self.artists)


def score(
    c: Candidate,
    artist: str,
    title: str,
    popularity: float = 0.0,
    artist_known: bool = False,
) -> float | None:
    """Skóre kandidáta, nebo None, když ho vetuje interpret nebo název."""
    if title:
        t_score = title_score(c.title, title)
        if t_score < TITLE_FLOOR:
            return None
    else:
        t_score = 0.5

    primary = False
    if artist and not artist_known:
        a_score, primary = artist_score(c.artists, artist)
        if a_score < ARTIST_FLOOR:
            return None
        s = W_ARTIST * a_score + W_TITLE * t_score
    else:
        s = t_score

    wanted_tags = version_tags(split_title(title)[1]) if title else set()
    cand_tags = version_tags(split_title(c.title)[1])
    # karaoke a tribute alba se poznají často jen podle alba
    cand_tags |= version_tags(c.album or "") & {"instrumental", "cover", "speed"}
    s += tag_adjustment(cand_tags, wanted_tags)

    s += W_POPULARITY * popularity
    if title and exact_diacritics(c.title, title):
        s += BONUS_DIACRITICS
    if primary:
        s += BONUS_PRIMARY
    s -= RANK_STEP * c.rank
    return s


def popularity(cands: Sequence[Candidate]) -> list[float]:
    """Log počtu přehrání vztažený k nejhranějšímu kandidátovi (0..1)."""
    top = max((c.views or 0 for c in cands), default=0)
    if top <= 0:
        return [0.0] * len(cands)
    denom = math.log10(top + 1)
    return [math.log10((c.views or 0) + 1) / denom for c in cands]


def rank(
    cands: Sequence[Candidate], artist: str, title: str, artist_known: bool = False
) -> list[tuple[float, Candidate]]:
    """Kandidáti, kteří prošli vety, od nejlepšího."""
    pops = popularity(cands)
    scored = []
    for c, pop in zip(cands, pops):
        s = score(c, artist, title, pop, artist_known)
        if s is not None:
            scored.append((s, c))
    # stabilní řazení: při shodě vyhraje ten, kdo byl ve výsledcích dřív
    scored.sort(key=lambda sc: -sc[0])
    return scored


def best(
    cands: Sequence[Candidate], artist: str, title: str, artist_known: bool = False
) -> Candidate | None:
    ranked = rank(cands, artist, title, artist_known)
    if not ranked or ranked[0][0] < SCORE_FLOOR:
        return None
    return ranked[0][1]


# ---- ze syrových výsledků ytmusicapi ----

# ytmusicapi sometimes stuffs `artists` with the play count ("3,4 tis.
# přehrání"), the like count ("Líbí se 2,7 tis. lidem") or the year.
NOT_AN_ARTIST = re.compile(
    r"(přehrání|zhlédnutí|views|plays|streams|likes?|lidem)\s*$"
    r"|^líbí\s+se\b"
    r"|^\d{4}$",
    re.I,
)


def artist_names(item: dict) -> list[str]:
    arts = item.get("artists") or []
    names = [clean(a.get("name", "")) for a in arts if isinstance(a, dict)]
    return [n for n in names if n and not NOT_AN_ARTIST.search(n)]


def duration(item: dict) -> int | None:
    if isinstance(item.get("duration_seconds"), int):
        return item["duration_seconds"]
    text = item.get("duration") or item.get("length")
    if not isinstance(text, str):
        return None
    try:
        nums = [int(p) for p in text.split(":")]
    except ValueError:
        return None
    seconds = 0
    for n in nums:
        seconds = seconds * 60 + n
    return seconds


def _album(item: dict) -> str | None:
    album = item.get("album")
    if isinstance(album, dict):
        album = album.get("name")
    return album if isinstance(album, str) else None


def from_song(item: dict, rank_: int = 0) -> Candidate | None:
    vid, title = item.get("videoId"), item.get("title")
    if not vid or not title:
        return None
    return Candidate(
        id=vid,
        title=clean(title),
        artists=tuple(artist_names(item)),
        album=_album(item),
        duration=duration(item),
        views=parse_views(item.get("views")),
        rank=rank_,
    )


# Balast v názvech videí — do štítku skladby nepatří.
_VIDEO_NOISE = re.compile(
    r"\s*[(\[]\s*(official|oficiální|oficialni|officiel|lyrics?|lyric video|text|"
    r"videoklip|video ?clip|music video|audio|hd|hq|4k|visuali[sz]er|klip)"
    r"[^)\]]*[)\]]",
    re.I,
)
_CHANNEL_NOISE = re.compile(r"\s*(-\s*topic|vevo|official|oficiální|music)\s*$", re.I)
_QUOTED = re.compile(r"^(?P<artist>[^\"„“”«»]+?)\s*[\"„“«](?P<title>[^\"“”»]+)[\"“”»]")


def from_video(item: dict, rank_: int = 0) -> list[Candidate]:
    """Video jako kandidát — i s tím, co je vyčtené z názvu.

    Spousta písniček je na YouTube jen jako video nahrané někým cizím:
    "Monkey Business - Piece Of My Life" od kanálu "Marie J." (2,7 mil.
    zhlédnutí), v katalogu skladeb YouTube Music vůbec není. Kanál tu
    o interpretovi nic neříká, název ano.
    """
    vid, full = item.get("videoId"), item.get("title")
    if not vid or not full:
        return []
    full = clean(full)
    channel = artist_names(item)
    common = dict(
        album=_album(item),
        duration=duration(item),
        views=parse_views(item.get("views")),
        rank=rank_,
    )
    out = []
    shown = _VIDEO_NOISE.sub("", full).strip()
    parts = _DASH.split(shown, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        out.append(Candidate(vid, parts[1].strip(), (parts[0].strip(),), **common))
    elif m := _QUOTED.match(shown):
        out.append(Candidate(vid, m["title"].strip(), (m["artist"].strip(),), **common))
    if channel:
        name = _CHANNEL_NOISE.sub("", channel[0]).strip() or channel[0]
        out.append(Candidate(vid, shown, (name,), **common))
    return out


# ---- všechno od jednoho interpreta ----


def credited_to(item: dict, artist_id: str | None, name: str) -> bool:
    """Je skladba opravdu od toho interpreta?

    Nejspolehlivější je id kanálu u uvedeného interpreta; jméno až jako
    záloha (u hostů a spoluprací id občas chybí).
    """
    arts = [a for a in item.get("artists") or [] if isinstance(a, dict)]
    if artist_id and any(a.get("id") == artist_id for a in arts):
        return True
    names = [clean(a.get("name", "")) for a in arts]
    return artist_score(tuple(n for n in names if n), name)[0] >= 0.75


def artist_songs(
    items: Iterable[dict], artist_id: str | None, name: str, limit: int = 50
) -> list[Candidate]:
    """Seznam skladeb interpreta upravený k hraní: jen jeho, každá jednou.

    Pořadí ze vstupu se drží (playlist "Songs" z profilu je seřazený podle
    popularity, takže známé věci jdou první). Stejná píseň ve víc verzích
    ("Opakování" a "Opakování (radio edit)") se hraje jednou — ta první.
    Živáky, remixy, instrumentálky a spol. jdou až na konec: když si někdo
    řekne o Midi Lidi, chce jejich písničky, ne koncertní záznam jako třetí.
    """
    plain: list[Candidate] = []
    versions: list[Candidate] = []
    seen: set[str] = set()
    for n, item in enumerate(items):
        if item.get("isAvailable") is False:
            continue
        c = from_song(item, n)
        if c is None or not credited_to(item, artist_id, name):
            continue
        base = norm(split_title(c.title)[0])
        if base in seen:
            continue
        seen.add(base)
        tags = version_tags(split_title(c.title)[1])
        (versions if tags else plain).append(c)
    return (plain + versions)[:limit]


def why_rejected(c: Candidate, artist: str, title: str, artist_known: bool = False) -> str:
    """Krátký důvod, proč kandidát neprošel — do provozního logu."""
    if title:
        t = title_score(c.title, title)
        if t < TITLE_FLOOR:
            return f"title {t:.2f} < {TITLE_FLOOR}"
    if artist and not artist_known:
        a, _ = artist_score(c.artists, artist)
        if a < ARTIST_FLOOR:
            return f"artist {a:.2f} < {ARTIST_FLOOR}"
    s = score(c, artist, title, 0.0, artist_known)
    if s is not None and s < SCORE_FLOOR:
        return f"score {s:.2f} < {SCORE_FLOOR}"
    return "ok"
