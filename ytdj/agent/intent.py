"""Co posluchač chtěl — čistá logika bez sítě, ať se dá testovat.

Tři věci, které se na Pi 25. 9. ukázaly jako slabá místa:

- "Hraj Davida Stypku" model vyložil jako jednu skladbu (play_next) — posluchač
  ale chtěl Stypku, dokud neřekne jinak. `repair_decision` to pozná podle
  toho, že v zadání padlo jméno interpreta, a ne název skladby.
- Po restartu i po automatickém přeseedování se zapomnělo, co posluchač
  naposledy výslovně chtěl. `ListenerIntent` to drží na disku.
- Interpret v 1. pádě v katalogu, ve 4. pádě v zadání ("Stypka" / "Stypku"):
  `mentions` porovnává kmeny slov, ne celá slova.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path


def norm(text: str) -> str:
    """Bez diakritiky, bez interpunkce, malými písmeny."""
    text = unicodedata.normalize("NFKD", (text or "").replace("\xa0", " ").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _stem_hit(word: str, tokens: list[str]) -> bool:
    """Slovo ze jména je v textu, i když jinak skloněné.

    Čeština mění konec slova ("Stypka" → "Stypku", "Kabát" → "Kabátu"), takže
    stačí shoda na začátku: délka slova bez posledních dvou písmen, aspoň tři.
    Krátká slova (U2, ABBA) musí sedět celá.
    """
    if len(word) <= 3:
        if word in tokens:
            return True
        # "Ewa" → "Ewu", "Ema" → "Emu": u krátkého jména se skloňuje jen
        # koncová samohláska
        if len(word) == 3 and word[-1] in "aeiouy":
            return any(len(t) == 3 and t[:2] == word[:2] and t[-1] in "aeiouy" for t in tokens)
        return False
    stem = word[: max(3, len(word) - 2)]
    return any(t.startswith(stem) and len(t) <= len(word) + 3 for t in tokens)


def mentions(text: str, name: str, any_word: bool = False) -> bool:
    """Padlo v textu tohle jméno (interpreta nebo skladby)?

    `any_word` stačí jedno výrazné slovo ze jména ("Nohavicu" pro Jaromíra
    Nohavicu) — pro interpreta, kterého už určil model, jen se ptáme, zda o
    něm posluchač mluvil.
    """
    words = [w for w in norm(name).split() if w]
    if not words:
        return False
    tokens = norm(text).split()
    joined = "".join(tokens)
    # "acdc" / "ac dc" / "AC/DC", "TribalNeed" / "Tribal Need"
    if len("".join(words)) >= 4 and "".join(words) in joined:
        return True
    # vatová slova ze jména ("the", "a") nerozhodují
    significant = [w for w in words if w not in {"the", "a", "and"}] or words
    if any_word:
        return any(_stem_hit(w, tokens) for w in significant if len(w) >= 4) or all(
            _stem_hit(w, tokens) for w in significant
        )
    return all(_stem_hit(w, tokens) for w in significant)


# ---- interpret jako shoda ----

_SPLIT = re.compile(r"\s*(?:,|&|\+|/| feat\.? | ft\.? | x | a | and | vs\.? )\s*", re.I)


def artist_parts(artist: str) -> list[str]:
    """ "David Stypka, Bandjeez" → ["david stypka", "bandjeez"]."""
    return [p for p in (norm(x) for x in _SPLIT.split(artist or "")) if p]


def artist_match(track_artist: str, wanted: str) -> bool:
    """Je skladba od hledaného interpreta?

    Přísnější než podobnost v katalogu: "Lucie" nesmí chytit "Lucie Bílá".
    Proto se porovnává každý z uvedených interpretů zvlášť a celý.
    """
    w = norm(wanted)
    if not w:
        return False
    w_flat = w.replace(" ", "")
    # kanály: "Midi Lidi - Topic", "MidiLidiVEVO", "Kabát Official"
    whole = re.sub(r"\s*(topic|vevo|official)$", "", norm(track_artist))
    if whole == w or whole.replace(" ", "") == w_flat:
        return True  # "Mňága a Žďorp" se nesmí rozpadnout na dvě jména
    for part in artist_parts(track_artist):
        if part == w or part.replace(" ", "") == w_flat:
            return True
        if len(w) >= 6 and SequenceMatcher(None, part, w).ratio() >= 0.88:
            return True  # překlep, jiný přepis ("Jaromir"/"Jaromír" už řeší norm)
    return False


# ---- jak je přání myšleno ----
#
# Model rozhoduje, ale na čtyři druhy přání jsou tvrdá pravidla, protože
# právě tady se na Pi 25. 9. mýlil nebo byl přebit:
#
#   interpret   "hraj Davida Stypku", "písničky od Midi Lidi", "chci slyšet
#               Tata Bojs", "víc od Kabátu"  → režim interpreta (jen on)
#   skladba     "pusť Jasnou zprávu od Olympicu" → ta jedna, pak podobně
#   jedna od    "zahraj jednu od Chinaski" → jedna skladba, nálada zůstává
#   nálada      "něco jako Nirvana", "český rap z devadesátek" → rádio

# "jednu", "jednu písničku od", "one song" — tehdy opravdu jen jedna skladba
_SINGLE = re.compile(
    r"\b(jednu|jedna|jedinou|one|a single|single|pisnicku\s+od|skladbu\s+od|song\s+by)\b"
)
# "něco jako X", "ve stylu X", "třeba X" — to je nálada s příkladem, ne interpret
_LIKE = re.compile(
    r"\b(jako|podobn\w*|ve stylu|stylu|like|similar|a la|treba|napriklad|inspir\w*)\b"
)
# "…, ale ne Y" / "kromě Y" / "bez Y" — co následuje, je výjimka, ne přání
_EXCEPT = re.compile(r"\b(ale ne|ale bez|krome|mimo|bez|but not|except|without)\b")
# "víc takového", "ještě něco podobného", "v tomhle duchu", "more like this"
_MORE_LIKE = re.compile(
    r"\b(vic|jeste|dalsi|neco|dej|hraj|pust)\s+(neco\s+)?(takov\w*|podobn\w*)"
    r"|\bv (tom|tomhle|tomto) duchu\b|\bmore like (this|that)\b|\btohle je dobr\w*"
)

# slova, která kolem jména interpreta nic nemění: sloveso, "písničky od", "prosím"
_FILLER = set("""
hraj hrej zahraj zahrej zahrajte hrajte pust pustit pustte pustis pustte zapni
dej dejte das dame chci chtel chtela bych chceme slyset poslechnout poslouchat
muzes mohl mohla bys prosim prosimte ted hned uz dal porad furt jen jenom
vic jeste dalsi zase znovu taky take
neco nejake nejaky nejakou pisnicky pisnicek pisne pisen skladby skladeb songy
veci hudbu muziku hity hitu nejvetsi zname nejznamejsi od z ze to toho tohle mi
nam si me
hod hodte hodit sup supni soupni soupnete pustis pustite muzete muzes mohli
prosim te tam sem nejaky nejakou nejakej trochu chvili chvilku
ty ta ten tu tech jeho jejich jeji
play put on some songs by music tracks more please the only just can you could
""".split())
# spojky mezi interprety, i pořadí ("Midi Lidi a pak Tata Bojs")
_JOIN = {"a", "and", "i", "nebo", "or", "plus", "pak", "potom", "nakonec",
         "then", "after", "that"}


def main_part(user_text: str) -> str:
    """Zadání bez výjimek: "Kabát, ale ne Pohodu" → "Kabát"."""
    text = norm(user_text)
    m = _EXCEPT.search(text)
    return text[: m.start()].strip() if m else text


def wants_more_like_current(user_text: str) -> bool:
    return bool(_MORE_LIKE.search(norm(user_text)))


def _find_words(words: list[str], tokens: list[str], used: list[bool]) -> list[int]:
    """Indexy tokenů, na které padnou všechna slova; [] když ne všechna."""
    hits: list[int] = []
    for w in words:
        idx = next(
            (i for i, t in enumerate(tokens)
             if not used[i] and i not in hits and _stem_hit(w, [t])),
            None,
        )
        if idx is None:
            return []
        hits.append(idx)
    return hits


def artist_request(user_text: str, candidates: list[str]) -> list[str]:
    """Interpreti, když zadání není nic jiného než "pusť <interpret(y)>".

    `candidates` jsou jména, která v rozhodnutí uvedl model (seedy, vyžádané,
    focus) — v nich se hledá, co z textu posluchače je jméno. Zbytek textu
    musí být jen vata (sloveso, "písničky od", "prosím", spojka). Jakmile v
    něm zbude cokoli dalšího ("…něco klidného od Kabátu", "Wonderwall"),
    nejde o čisté přání interpreta a nic se nevrací.
    """
    text = main_part(user_text)
    if not text or _SINGLE.search(text) or _LIKE.search(text):
        return []
    tokens = text.split()
    used = [False] * len(tokens)
    found: list[tuple[int, str]] = []
    # delší jména napřed, ať "Lucie Bílá" nesežere "Lucie"
    for name in sorted({c.strip() for c in candidates if c.strip()}, key=len, reverse=True):
        words = [w for w in norm(name).split() if w not in {"the"}]
        if not words:
            continue
        # celé jméno, nebo aspoň příjmení ("pusť Stypku" = David Stypka)
        variants = [words]
        if len(words) >= 2 and len(words[-1]) >= 4:
            variants.append(words[-1:])
        for variant in variants:
            hits = _find_words(variant, tokens, used)
            if hits:
                # slepené jméno ("tribalneed") se tu nechytí; to řeší model sám
                for i in hits:
                    used[i] = True
                found.append((min(hits), name))
                break
    if not found:
        return []
    rest = [t for t, u in zip(tokens, used) if not u]
    if any(t not in _FILLER and t not in _JOIN for t in rest):
        return []
    return [name for _, name in sorted(found)]


@dataclass
class Repair:
    focus_artists: list[str]
    action: str
    note: str = ""
    more_like_current: bool = False


def repair_decision(
    user_text: str,
    action: str,
    requested: list[dict],
    focus_artists: list[str],
    seed_artists: list[str] | None = None,
) -> Repair:
    """Srovná rozhodnutí se zadáním tam, kde se model prokazatelně mýlí.

    Jen deterministická pravidla, žádné hádání nálady:

    1. Zadání je jen "pusť <interpret>" (v jakémkoli pádě, s "písničky od",
       "chci slyšet", "víc od"…) → režim interpreta, ať model vrátil cokoli.
       Pi 25. 9.: "Hraj davida Stypku" → play_next jedné skladby;
       "Hraj pisnicky od midi lidi" → rádio, které po jedné skladbě uhnulo.
    2. play_next s vyžádanou skladbou, jejíž název v zadání nepadl, ale jméno
       interpreta ano → taky interpret, ne jedna skladba. Výjimka: "jednu od".
    3. Vyplněné focus_artists s akcí, která hudbu nemění → start_radio.
    4. "Víc takového" → rádio musí vyjít z toho, co právě hraje.
    """
    text = norm(user_text)
    focus = [a for a in focus_artists if a.strip()]
    note = ""
    single = bool(_SINGLE.search(text))

    if not focus and not single and action in ("start_radio", "play_next", "nothing"):
        candidates = [r.get("artist") or "" for r in requested] + list(seed_artists or [])
        if names := artist_request(user_text, candidates):
            focus = names
            note = f"{action} → režim interpreta " + ", ".join(names)
            action = "start_radio"

    if action == "play_next" and not focus and not single and not _LIKE.search(text):
        promoted = []
        for r in requested:
            artist = (r.get("artist") or "").strip()
            title = (r.get("title") or "").strip()
            if not artist or not mentions(user_text, artist, any_word=True):
                continue
            if title and mentions(user_text, title):
                continue  # název padl → chtěl tu skladbu
            if artist not in promoted:
                promoted.append(artist)
        if promoted:
            focus = promoted
            action = "start_radio"
            note = "play_next → režim interpreta " + ", ".join(promoted)

    if focus and action != "start_radio":
        note = note or f"{action} s focus_artists → start_radio"
        action = "start_radio"

    more_like = action == "start_radio" and not focus and wants_more_like_current(user_text)
    return Repair(focus_artists=focus, action=action, note=note, more_like_current=more_like)


def track_avoided(artist: str, title: str, avoid: list[tuple[str, str]]) -> bool:
    """Patří skladba k tomu, co posluchač výslovně nechce ("ale ne Pohodu")?

    `avoid` jsou dvojice (interpret, název); prázdný název = celý interpret.
    """
    t_title = norm(title)
    for a_artist, a_title in avoid:
        a_artist = (a_artist or "").strip()
        a_title = norm(a_title or "")
        if a_artist and not artist_match(artist, a_artist):
            continue
        if not a_title:
            if a_artist:
                return True  # celý interpret ne
            continue
        if a_title == t_title or (len(a_title) >= 4 and a_title in t_title):
            return True
    return False


# ---- co posluchač chtěl naposledy ----

INTENT_TTL = 12 * 3600  # s — po půl dni už to "naposledy" neplatí


@dataclass
class ListenerIntent:
    text: str = ""
    ts: float = 0.0
    focus_artists: list[str] = field(default_factory=list)
    mood: str = ""
    # předchozí přání [(text, ts)], nejnovější první — "jen česky" řečené
    # před chvílí platí i pro "teď něco rychlejšího"
    earlier: list[list] = field(default_factory=list)

    def then(self, text: str, ts: float, focus_artists: list[str], mood: str) -> "ListenerIntent":
        """Nové přání; současné se posune mezi předchozí (max. 3)."""
        earlier = ([[self.text, self.ts]] if self.text else []) + self.earlier
        return ListenerIntent(text, ts, list(focus_artists), mood, earlier[:3])

    def fresh(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return bool(self.text) and now - self.ts < INTENT_TTL

    def describe(self, now: float | None = None) -> str:
        """Řádek do stavu pro model; prázdný, když už neplatí."""
        if not self.fresh(now):
            return ""
        now = time.time() if now is None else now
        minutes = max(0, int((now - self.ts) // 60))
        ago = "právě teď" if minutes < 1 else f"před {minutes} min"
        out = f"„{self.text.strip()[:300]}“ ({ago})"
        if self.focus_artists:
            out += " → režim interpreta: " + ", ".join(self.focus_artists)
        before = [
            f"„{str(t).strip()[:120]}“ (před {max(0, int((now - float(ts)) // 60))} min)"
            for t, ts in self.earlier
            if t and now - float(ts) < INTENT_TTL
        ]
        if before:
            out += "; předtím: " + ", ".join(before)
        return out

    @classmethod
    def load(cls, path: Path) -> "ListenerIntent":
        try:
            data = json.loads(path.read_text())
            return cls(
                text=str(data.get("text") or ""),
                ts=float(data.get("ts") or 0),
                focus_artists=[str(a) for a in data.get("focus_artists") or []],
                mood=str(data.get("mood") or ""),
                earlier=[
                    [str(e[0]), float(e[1])]
                    for e in data.get("earlier") or []
                    if isinstance(e, list) and len(e) == 2
                ][:3],
            )
        except (OSError, ValueError, TypeError, AttributeError):
            return cls()

    def save(self, path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self), ensure_ascii=False))
            tmp.replace(path)
        except OSError:
            pass


# ---- oblíbené (hlasování kanceláře, ytdj/votes.py) ----

# "pusť oblíbené", "hraj oblíbené kanceláře", "dej naše oblíbené písničky",
# "pusť moje oblíbené", "něco z mých oblíbených" — celé zadání, nic navíc:
# "pusť oblíbené od Kabátu" je jiné přání a jde k modelu.
_FAV_LEAD = (r"(?:(?:pust|pustte|pustit|pusti|hraj|hrajte|zahraj|zahrajte|dej|dejte|chci|chceme"
             r"|prosim|neco|nejake|z|ze|ty|nam|mi|si|play)\s+)*")
_FAV_MINE = r"(?P<mine>moje|me|moji|mych|mym|mou|muj|svoje|sve|svych|my)\s+"
_FAV_OFFICE = r"(?:(?:nase|nasi|nasich|kancelarske|kancelarsky|kancelarskych|spolecne|the)\s+)?"
_FAV_WORD = r"(?:oblib\w*|favou?rites?|favs?)"
_FAV_TAIL = (r"(?:\s+(?:kancelare|kanclu|kancl|z kancelare|v kancelari|kancelar|office"
             r"|pisnicky|pisnicek|pisne|pisni|skladby|skladeb|songy|songs|veci|kousky|hudbu|hudby"
             r"|prosim|please))*")
_FAVOURITES = re.compile(
    rf"^{_FAV_LEAD}(?:{_FAV_MINE}|{_FAV_OFFICE}){_FAV_WORD}{_FAV_TAIL}$")


def favourites_request(text: str) -> str | None:
    """ "mine" (moje oblíbené) / "office" (oblíbené kanceláře) / None."""
    t = norm(text)
    if not t or len(t) > 80:
        return None
    m = _FAVOURITES.match(t)
    if m is None:
        return None
    return "mine" if m.group("mine") else "office"


# ---- povely, na které model není potřeba ----

_COMMANDS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^(dalsi|preskoc|skip|next|n|dalsi pisnick\w*|dalsi pisen|dalsi skladb\w*|"
                r"jinou pisnick\w*|jinou skladb\w*|next song)$"), "skip"),
    (re.compile(r"^(pauza|pause|p|ticho|pozastav)$"), "pause"),
    (re.compile(r"^(stop|zastav)$"), "stop"),
    (re.compile(r"^(pokracuj|hraj dal|resume|play)$"), "resume"),
    (re.compile(r"^(hlasiteji|nahlas|louder|volume up|pridej|zesil|hlasitej|hlasit)$"), "louder"),
    (re.compile(r"^(tiseji|potichu|quieter|volume down|uber|ztlum|zeslab|ztis|tisej|tis)$"),
     "quieter"),
]
_VOLUME = re.compile(r"^(?:hlasitost|volume|vol)\s+(\d{1,3})$")
# slova kolem povelu, která nic nemění: "hlasitěji prosím", "trochu hlasitěji",
# "dej to hlasitěji", "další prosím", "ztiš to trochu"
_CMD_FILLER = {
    "prosim", "prosimte", "trochu", "trosku", "malinko", "kousek", "o", "dej", "dejte",
    "to", "tu", "jeste", "uz", "hned", "mi", "nam", "tam", "sem", "moc", "bit", "a",
    "please", "can", "you", "turn", "it", "up", "down", "the", "pust", "tuhle", "tohle",
}


def local_command(text: str) -> tuple[str, int] | None:
    """Jednoznačný povel bez modelu: (akce, hodnota), jinak None.

    Z webu chodil každý text do Codexu — i "hlasitěji", na které se čekalo
    dvacet vteřin. Tady jen krátké povely, i s vatou kolem ("trochu
    hlasitěji prosím"); "pusť něco hlasitějšího" nebo "další od Kabátu" je
    přání, ne povel, a jde dál k modelu.
    """
    t = norm(text)
    if not t or len(t) > 40:
        return None
    if m := _VOLUME.match(t):
        return "volume", int(m.group(1))
    words = t.split()
    core = " ".join(w for w in words if w not in _CMD_FILLER)
    # "turn it up/down" — anglická vata nese směr
    if not core and {"turn", "up"} <= set(words):
        return "louder", 0
    if not core and {"turn", "down"} <= set(words):
        return "quieter", 0
    for candidate in (t, core):
        for pattern, action in _COMMANDS:
            if candidate and pattern.match(candidate):
                return action, 0
    return None
    if m := _VOLUME.match(t):
        return "volume", int(m.group(1))
    for pattern, action in _COMMANDS:
        if pattern.match(t):
            return action, 0
    return None


# ---- série přeskočení ----


class SkipWatch:
    """Kdy má DJ sám změnit směr, protože posluchač přeskakuje.

    Dřív se to počítalo z databáze: skladby přeskočené v posledních deseti
    minutách, porovnané s minulým počtem. 25. 9. to na Pi udělalo tohle:

    - po každém restartu (4×) přeseedování do deseti vteřin — přeskočení
      z doby před restartem se počítala znovu;
    - přeseedování ve 20:06:15, 34 s po výslovném "hraj Davida Stypku", bez
      jediného nového přeskočení: počet *klesl* (stará skladba vypadla z okna)
      a "jiný než minule" stačilo;
    - do výčtu šla i přeskočení z dřívějších nálad, takže model šest nálad po
      sobě zapsal jako "uživateli nesedí".

    Tady se počítají jen přeskočení od posledního tahu DJ, v paměti procesu,
    a po posluchačově pokynu je chvíle klidu.
    """

    def __init__(
        self, threshold: int = 3, window: float = 600, quiet_after_user: float = 180
    ) -> None:
        self.threshold = threshold
        self.window = window
        self.quiet_after_user = quiet_after_user
        self._skips: list[tuple[float, str]] = []
        self._last_user_turn = float("-inf")

    def skipped(self, label: str, now: float) -> None:
        self._skips.append((now, label))

    def turn(self, now: float, by_user: bool) -> None:
        """DJ změnil hudbu — co se přeskakovalo předtím, už neplatí."""
        self._skips.clear()
        if by_user:
            self._last_user_turn = now

    def due(self, now: float) -> list[str] | None:
        """Názvy přeskočených, když je čas zasáhnout; jinak None."""
        self._skips = [(t, lbl) for t, lbl in self._skips if now - t <= self.window]
        if len(self._skips) < self.threshold:
            return None
        if now - self._last_user_turn < self.quiet_after_user:
            return None
        return [lbl for _, lbl in reversed(self._skips)][:6]


# ---- vyložené přání ----

Pair = tuple[str, str]  # (interpret, název); prázdný název = "cokoli od něj"


@dataclass
class Intent:
    """Co posluchač chtěl, vyložené a opravené — ještě bez katalogu a přehrávače.

    kind:
      artist   hrát jen `artists`, dokud neřekne jinak (režim interpreta)
      song     `tracks` hned, pak rádio v jejich duchu (`seeds`)
      songs    jen `tracks` (hned, nebo po hrající — `play_next`); nálada zůstává
      mood     nové rádio ze `seeds` (nálada, žánr, období, "něco jako X")
      control  skip / pause / resume / stop / volume (`control`, `volume`)
      none     jen odpověď (otázka, pozdrav, `remember`)

    `play_next` = zařadit až za hrající skladbu (nikdy ji neutnout).
    Postup: `CodexDJ.interpret()` → Intent, `CodexDJ.resolve()` → Plan
    (konkrétní skladby), `CodexDJ.play()` → přehrávač. Fronta požadavků od
    víc posluchačů může volat první dva a plánovat si sama.
    """

    kind: str
    text: str = ""
    artists: list[str] = field(default_factory=list)
    tracks: list[Pair] = field(default_factory=list)
    seeds: list[Pair] = field(default_factory=list)
    exclude: list[Pair] = field(default_factory=list)
    play_next: bool = False
    more_like_current: bool = False
    mood: str = ""
    control: str = ""
    volume: int = 0
    remember: str = ""
    reply: str = ""
    auto: bool = False  # zadání od aplikace (přeseedování), ne od posluchače
    # hotová semínka (DJ bez modelu) — resolve je nehledá znovu
    seed_tracks: list = field(default_factory=list)
    note: str = ""  # co opravila pravidla — pro log

    @property
    def changes_music(self) -> bool:
        return self.kind in ("artist", "song", "songs", "mood")


def _pairs(items: list[dict]) -> list[Pair]:
    out = []
    for it in items or []:
        artist = str(it.get("artist") or "").strip()
        title = str(it.get("title") or "").strip()
        if artist or title:
            out.append((artist, title))
    return out


_CONTROL = {"skip", "pause", "resume", "stop", "volume"}


def build_intent(user_text: str, data: dict, auto: bool = False) -> Intent:
    """Rozhodnutí modelu (JSON podle schématu) → Intent, s opravami.

    Opravy (`repair_decision`) jen pro posluchače; zadání od aplikace se bere,
    jak ho model vyložil.
    """
    action = str(data.get("action") or "nothing")
    requested = [r for r in data.get("requested") or [] if isinstance(r, dict)]
    seeds = [s for s in data.get("seeds") or [] if isinstance(s, dict)]
    focus = [str(a).strip() for a in data.get("focus_artists") or [] if str(a).strip()]
    reply = str(data.get("reply") or "")
    note = ""
    more_like = False
    if not auto:
        fix = repair_decision(
            user_text, action, requested, focus,
            seed_artists=[str(s.get("artist") or "") for s in seeds],
        )
        if fix.note:
            # odpověď modelu popisuje jeho původní rozhodnutí ("zařazuji jednu
            # skladbu") — teď by lhala; pravdu doplní přehrávání
            reply = ""
        action, focus, note = fix.action, fix.focus_artists, fix.note
        more_like = fix.more_like_current

    if action in _CONTROL:
        kind = "control"
    elif action == "start_radio" and focus:
        kind = "artist"
    elif action == "start_radio" and requested:
        kind = "song"
    elif action == "start_radio":
        kind = "mood"
    elif action == "play_next":
        kind = "songs"
    else:
        kind = "none"

    return Intent(
        kind=kind,
        text=user_text,
        artists=focus if kind == "artist" else [],
        tracks=_pairs(requested),
        seeds=_pairs(seeds),
        exclude=_pairs([a for a in data.get("avoid") or [] if isinstance(a, dict)]),
        play_next=bool(data.get("after_current")),
        more_like_current=more_like,
        # opravené na interpreta: nálada modelu ("stávající nálada") by lhala
        mood=", ".join(focus) if kind == "artist" and note else str(data.get("mood") or ""),
        control=action if kind == "control" else "",
        volume=int(data.get("volume") or 0),
        remember=str(data.get("remember") or ""),
        reply=reply,
        auto=auto,
        note=note,
    )
