"""Kontext pro rozjezd bez zadání: někdo stiskne Hrát, nic nehraje, nic nechtěl.

Místo pevného "navaž na to, co jsem poslouchal naposledy" dostane DJ situaci:
kolik je hodin, jaký je den (pracovní / víkend / státní svátek), že hraje
lidem v kanceláři, a co tahle kancelář v podobnou dobu dohrávala a co
přeskakovala.

    ctx = start_context(store)           # pár SQL dotazů, žádná síť
    text = start_instruction(ctx)        # česká instrukce pro model, < ~800 znaků

Obojí je deterministické vůči vstupům (`now`, obsah state.db), takže se to dá
testovat s pevným časem. Taste tu schválně není natvrdo — o náladě rozhoduje
model; tady jsou jen pravidla o tom, *kdy* hrát klidněji a kdy živěji
(ENERGY_RULES) a co říká historie.
"""

from __future__ import annotations

import logging
from bisect import bisect_right
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any

from .. import telemetry

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- kalendář

WEEKDAYS = ("pondělí", "úterý", "středa", "čtvrtek", "pátek", "sobota", "neděle")

# Státní svátky a ostatní svátky ČR, kdy se nepracuje (zákon 245/2000 Sb.).
FIXED_HOLIDAYS: dict[tuple[int, int], str] = {
    (1, 1): "Nový rok",
    (5, 1): "Svátek práce",
    (5, 8): "Den vítězství",
    (7, 5): "Cyril a Metoděj",
    (7, 6): "Jan Hus",
    (9, 28): "Den české státnosti",
    (10, 28): "Den vzniku samostatného československého státu",
    (11, 17): "Den boje za svobodu a demokracii",
    (12, 24): "Štědrý den",
    (12, 25): "1. svátek vánoční",
    (12, 26): "2. svátek vánoční",
}


def easter_sunday(year: int) -> date:
    """Velikonoční neděle (gregoriánský kalendář, anonymní algoritmus)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


@lru_cache(maxsize=512)
def czech_holiday(d: date) -> str | None:
    """Název českého svátku (dne pracovního klidu), jinak None."""
    if name := FIXED_HOLIDAYS.get((d.month, d.day)):
        return name
    easter = easter_sunday(d.year)
    if d == easter + timedelta(days=1):
        return "Velikonoční pondělí"
    if d == easter - timedelta(days=2) and d.year >= 2016:
        return "Velký pátek"
    return None


def is_working_day(d: date) -> bool:
    return d.weekday() < 5 and czech_holiday(d) is None


# ---------------------------------------------------------------- denní doba

# (od minuty dne, popisek) — platí do začátku dalšího
PARTS_OF_DAY: tuple[tuple[int, str], ...] = (
    (0, "noc"),
    (5 * 60, "ráno"),
    (9 * 60, "dopoledne"),
    (11 * 60 + 30, "poledne"),
    (13 * 60, "odpoledne"),
    (15 * 60 + 30, "pozdní odpoledne"),
    (18 * 60, "večer"),
    (22 * 60, "noc"),
)


def part_of_day(minute: int) -> str:
    label = PARTS_OF_DAY[0][1]
    for start, name in PARTS_OF_DAY:
        if minute >= start:
            label = name
    return label


# ---------------------------------------------------------------- energie

ENERGY_WORDS = {1: "velmi klidně", 2: "klidně", 3: "středně", 4: "svižně", 5: "živě"}


@dataclass(frozen=True, slots=True)
class EnergyRule:
    """Jedno pravidlo energetické křivky. Vyhrává první, které sedí.

    `days`: "work" = pracovní den, "off" = víkend nebo svátek;
    `weekdays`: omezení na dny v týdnu (0 = pondělí), prázdné = všechny.
    Čas je [start, end) v minutách dne.
    """

    name: str
    days: str
    start: int
    end: int
    energy: int  # 1 (velmi klidně) … 5 (živě)
    hint: str
    weekdays: frozenset[int] = frozenset()

    def matches(self, working: bool, weekday: int, minute: int) -> bool:
        if (self.days == "work") != working:
            return False
        if self.weekdays and weekday not in self.weekdays:
            return False
        return self.start <= minute < self.end


def _h(hours: float) -> int:
    return int(hours * 60)


# Kancelářská křivka: klidný rozjezd, soustředěné dopoledne, po obědě
# nakopnout, ke konci dne živěji, v pátek odpoledne nejvíc. Specifická
# pravidla (pondělí ráno, pátek odpoledne) jsou před obecnými.
ENERGY_RULES: tuple[EnergyRule, ...] = (
    EnergyRule("monday_morning", "work", _h(5), _h(10), 2,
               "pondělní ráno: pomalý, příjemný rozjezd týdne", frozenset({0})),
    EnergyRule("friday_afternoon", "work", _h(13), _h(19), 5,
               "páteční odpoledne, víkend na dohled: živě a s dobrou náladou", frozenset({4})),
    EnergyRule("early", "work", 0, _h(7), 1, "brzy ráno, skoro nikdo tu není: potichu"),
    EnergyRule("morning", "work", _h(7), _h(9.5), 2, "ranní rozjezd: příjemně, nic hlučného"),
    EnergyRule("focus", "work", _h(9.5), _h(11.5), 3,
               "dopolední soustředění: stálé tempo, do pozadí"),
    EnergyRule("lunch", "work", _h(11.5), _h(13), 3, "polední pauza: pohodově"),
    EnergyRule("after_lunch", "work", _h(13), _h(15.5), 4,
               "po obědě přichází útlum: svižněji a pozitivně, ať to lidi nakopne"),
    EnergyRule("end_of_day", "work", _h(15.5), _h(18), 4, "konec pracovního dne: s dobrou náladou"),
    EnergyRule("after_work", "work", _h(18), _h(24), 2, "po pracovní době: uvolněně"),
    EnergyRule("off_night", "off", 0, _h(8), 1, "volný den brzy ráno: potichu"),
    EnergyRule("off_day", "off", _h(8), _h(18), 3,
               "volný den, v práci je jen pár lidí: volněji, pořád ale pro všechny"),
    EnergyRule("off_evening", "off", _h(18), _h(24), 3, "večer volného dne: uvolněně"),
)


def energy_rule(working: bool, weekday: int, minute: int) -> EnergyRule:
    for rule in ENERGY_RULES:
        if rule.matches(working, weekday, minute):
            return rule
    return ENERGY_RULES[-1]  # nedosažitelné, pravidla pokrývají celý den


# ---------------------------------------------------------------- historie

HISTORY_DAYS = 28  # jak daleko zpátky hledat podobné časy
SLOT_MINUTES = 90  # ± kolik minut od teď je "podobná doba"
OVERPLAY_DAYS = 7
OVERPLAY_MIN = 6  # aspoň tolik skladeb interpreta za OVERPLAY_DAYS…
OVERPLAY_SHARE = 0.08  # …a zároveň aspoň takový podíl všeho, co hrálo
LAST_MOOD_DAYS = 7
MAX_NAME = 28
MAX_MOOD = 40


@dataclass(slots=True)
class SlotHistory:
    """Co tahle kancelář v podobnou dobu poslouchala (viz `_slot_history`)."""

    n_plays: int = 0  # kolik přehrání s výsledkem se započítalo
    n_days: int = 0  # z kolika různých dnů
    worked_artists: list[str] = field(default_factory=list)
    skipped_artists: list[str] = field(default_factory=list)
    worked_moods: list[str] = field(default_factory=list)
    skipped_moods: list[str] = field(default_factory=list)
    overplayed: list[str] = field(default_factory=list)
    last_mood: str | None = None
    last_mood_age_h: float | None = None


@dataclass(slots=True)
class StartContext:
    now: datetime
    weekday: int
    weekday_name: str
    part_of_day: str
    day_type: str  # "pracovní den" | "víkend" | "svátek"
    holiday: str | None
    working_day: bool
    office: bool  # hraje lidem v práci (cfg.office, výchozí True)
    rule: EnergyRule
    history: SlotHistory

    @property
    def energy_word(self) -> str:
        return ENERGY_WORDS[self.rule.energy]


class _Tally:
    __slots__ = ("good", "bad", "days", "names")

    def __init__(self) -> None:
        self.good = 0.0
        self.bad = 0.0
        self.days: set[date] = set()
        self.names: Counter[str] = Counter()

    @property
    def name(self) -> str:
        # nejčastější zápis; při shodě abecedně, ať je to deterministické
        return min(self.names.items(), key=lambda kv: (-kv[1], kv[0]))[0]

    @property
    def skip_rate(self) -> float:
        total = self.good + self.bad
        return self.bad / total if total else 0.0


def _is_video_id(text: str) -> bool:
    # Staré záznamy mohly mít v plays.seed_id opravdu id seedu, ne náladu.
    return len(text) == 11 and " " not in text and text.replace("-", "").replace("_", "").isalnum()


def _clip(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


@dataclass(frozen=True, slots=True)
class _Range:
    start: float
    end: float
    day: date
    weight: float


def slot_ranges(now: datetime, days: int = HISTORY_DAYS,
                minutes: int = SLOT_MINUTES) -> list[_Range]:
    """Časová okna "podobná doba" za posledních `days` dní, nejstarší první.

    Každý den stejného druhu jako dnešek (pracovní × volný) dá okno
    [čas − minutes, čas + minutes) v místním čase toho dne — takže přechod na
    letní čas nic neposune. Dnešní okno končí teď.
    """
    working = is_working_day(now.date())
    now_ts = now.timestamp()
    out = []
    for back in range(days, -1, -1):
        day = now.date() - timedelta(days=back)
        if is_working_day(day) != working:
            continue
        center = datetime.combine(day, now.time(), tzinfo=now.tzinfo)
        start = (center - timedelta(minutes=minutes)).timestamp()
        end = min((center + timedelta(minutes=minutes)).timestamp(), now_ts)
        if start < end:
            out.append(_Range(start, end, day, 2.0 if day.weekday() == now.weekday() else 1.0))
    return out


def _slot_history(store: Any, now: datetime) -> SlotHistory:
    """Statistiky z state.db: pět malých dotazů, zbytek v Pythonu.

    Podobná doba = ±SLOT_MINUTES od teď, za posledních HISTORY_DAYS dní, ve
    stejném druhu dne (pracovní × volný); stejný den v týdnu váží dvakrát.
    Dohrané = +1, přeskočené = −1 (dislike −2, like +1, vyžádané jménem +2).
    Osvědčený interpret (i nálada) musí zaznít aspoň ve dvou různých dnech —
    kancelář je víc lidí a jeden den jednoho posluchače ještě neznamená, že
    se to líbí všem.
    """
    hist = SlotHistory()
    now_ts = now.timestamp()
    ranges = slot_ranges(now)
    bounds = [(r.start, r.end) for r in ranges]
    starts = [r.start for r in ranges]

    def slot_of(ts: float) -> _Range | None:
        i = bisect_right(starts, ts) - 1
        return ranges[i] if i >= 0 and ts < ranges[i].end else None

    plays = store.plays_in(bounds)  # replaced/started/error nic neříkají o vkusu
    requests = store.requests_in(bounds)
    ratings = store.ratings() if plays else {}

    artists: dict[str, _Tally] = defaultdict(_Tally)
    moods: dict[str, _Tally] = defaultdict(_Tally)
    days: set[date] = set()

    for p in plays:
        r = slot_of(p.ts)
        if r is None:
            continue
        w = r.weight
        good = p.outcome == "finished"
        rating = ratings.get(p.video_id)
        g = w if good else 0.0
        b = 0.0 if good else w
        if rating == "like":
            g += w
        elif rating == "dislike":
            b += 2 * w
        hist.n_plays += 1
        days.add(r.day)
        tallies = []
        artist = " ".join(p.artist.split())
        if artist:
            tallies.append((artists[artist.casefold()], artist))
        mood = " ".join(p.mood.split())
        if mood and not _is_video_id(mood):
            tallies.append((moods[mood.casefold()], mood))
        for tally, name in tallies:
            tally.good += g
            tally.bad += b
            tally.days.add(r.day)
            tally.names[name] += 1

    for ts, artist, _title in requests:
        r = slot_of(ts)
        artist = " ".join(artist.split())
        if r is not None and artist:
            t = artists[artist.casefold()]
            t.good += 2 * r.weight
            t.days.add(r.day)
            t.names[artist] += 1
            days.add(r.day)

    week_counts: Counter[str] = Counter()
    week_names: dict[str, Counter[str]] = defaultdict(Counter)
    week_total = 0
    for artist, count in store.artist_counts(now_ts - OVERPLAY_DAYS * 86400, now_ts):
        artist = " ".join(artist.split())
        week_total += count
        if artist:
            week_counts[artist.casefold()] += count
            week_names[artist.casefold()][artist] += count

    hist.n_days = len(days)

    threshold = max(OVERPLAY_MIN, OVERPLAY_SHARE * week_total)
    over = sorted(
        (k for k, n in week_counts.items() if n >= threshold),
        key=lambda k: (-week_counts[k], k),
    )[:3]
    hist.overplayed = [
        _clip(min(week_names[k].items(), key=lambda kv: (-kv[1], kv[0]))[0], MAX_NAME)
        for k in over
    ]

    def worked(tallies: dict[str, _Tally], min_good: float, max_rate: float, n: int,
               skip: set[str] = frozenset()) -> list[_Tally]:
        good = [
            (k, t) for k, t in tallies.items()
            if k not in skip and t.good >= min_good and t.skip_rate <= max_rate
            and len(t.days) >= 2
        ]
        good.sort(key=lambda kt: (-(kt[1].good - kt[1].bad), -len(kt[1].days), kt[0]))
        return [t for _, t in good[:n]]

    def skipped(tallies: dict[str, _Tally], min_bad: float, n: int) -> list[_Tally]:
        bad = [(k, t) for k, t in tallies.items() if t.bad >= min_bad and t.skip_rate >= 0.5]
        bad.sort(key=lambda kt: (-kt[1].bad, kt[0]))
        return [t for _, t in bad[:n]]

    hist.worked_artists = [_clip(t.name, MAX_NAME)
                           for t in worked(artists, 2, 0.25, 4, skip=set(over))]
    hist.skipped_artists = [_clip(t.name, MAX_NAME) for t in skipped(artists, 2, 3)]
    hist.worked_moods = [_clip(t.name, MAX_MOOD) for t in worked(moods, 3, 0.35, 2)]
    hist.skipped_moods = [_clip(t.name, MAX_MOOD) for t in skipped(moods, 3, 1)]

    last = store.last_mood(now_ts)
    if last and now_ts - last[1] <= LAST_MOOD_DAYS * 86400:
        hist.last_mood = _clip(last[0], MAX_MOOD)
        hist.last_mood_age_h = round((now_ts - last[1]) / 3600, 1)
    return hist


# ---------------------------------------------------------------- API


def start_context(store: Any, now: datetime | None = None, cfg: Any = None) -> StartContext:
    """Situace pro rozjezd bez zadání. Nikdy nevyhodí výjimku kvůli historii.

    `now` bez časové zóny se bere jako místní čas. `store` = ytdj.state.Store
    (nebo None — pak bez historie). `cfg.office` (výchozí True) vypne
    kancelářský kontext pro domácí použití.
    """
    t0 = time.monotonic()
    now = now or datetime.now()  # naivní = místní čas (i pro historii a DST)
    today = now.date()
    holiday = czech_holiday(today)
    working = is_working_day(today)
    day_type = "svátek" if holiday else ("víkend" if today.weekday() >= 5 else "pracovní den")
    minute = now.hour * 60 + now.minute
    office = bool(getattr(cfg, "office", True)) if cfg is not None else True

    history = SlotHistory()
    if store is not None:
        try:
            history = _slot_history(store, now)
        except Exception:
            log.exception("historie pro rozjezd se nenačetla")

    ctx = StartContext(
        now=now,
        weekday=today.weekday(),
        weekday_name=WEEKDAYS[today.weekday()],
        part_of_day=part_of_day(minute),
        day_type=day_type,
        holiday=holiday,
        working_day=working,
        office=office,
        rule=energy_rule(working, today.weekday(), minute),
        history=history,
    )
    telemetry.event(
        "dj.start_context",
        weekday=ctx.weekday_name,
        part_of_day=ctx.part_of_day,
        day_type=ctx.day_type,
        holiday=ctx.holiday,
        rule=ctx.rule.name,
        energy=ctx.rule.energy,
        office=ctx.office,
        n_plays=history.n_plays,
        n_days=history.n_days,
        worked_artists=history.worked_artists,
        skipped_artists=history.skipped_artists,
        worked_moods=history.worked_moods,
        overplayed=history.overplayed,
        last_mood=history.last_mood,
        took_ms=int((time.monotonic() - t0) * 1000),
    )
    return ctx


def _age(hours: float) -> str:
    if hours < 1:
        return "před chvílí"
    if hours < 36:
        return f"před {round(hours)} h"
    return f"před {round(hours / 24)} dny"


def _quoted(items: list[str]) -> str:
    return ", ".join(f"„{m}“" for m in items)


MAX_CHARS = 800

# Když se instrukce nevejde do MAX_CHARS, zkracuje se v tomhle pořadí:
# (osvědčení, osvědčené nálady, přeskakovaní, přeskakované nálady, přehraní,
# poslední nálada). Pořadí = co je pro výběr nejméně podstatné, jde první.
_BUDGETS: tuple[tuple[int, int, int, int, int, bool], ...] = (
    (4, 2, 3, 1, 3, True),
    (4, 2, 3, 0, 3, True),
    (4, 1, 2, 0, 2, True),
    (3, 1, 2, 0, 2, False),
    (3, 1, 1, 0, 1, False),
    (2, 0, 1, 0, 1, False),
    (2, 0, 0, 0, 0, False),
    (0, 0, 0, 0, 0, False),
)


def start_instruction(ctx: StartContext) -> str:
    """Česká instrukce pro DJ, nejvýš MAX_CHARS znaků. Deterministická vůči `ctx`."""
    text = ""
    for budget in _BUDGETS:
        text = _render(ctx, *budget)
        if len(text) <= MAX_CHARS:
            break
    return text


def _render(ctx: StartContext, n_worked: int, n_wmood: int, n_skipped: int,
            n_smood: int, n_over: int, last: bool) -> str:
    h = ctx.history
    n = ctx.now
    day = f"svátek ({ctx.holiday})" if ctx.holiday else ctx.day_type
    lines = [
        "Nic nehraje a nikdo si nic nevyžádal — vyber sám podle situace.",
        f"Je {ctx.weekday_name} {n.day}. {n.month}. {n.hour}:{n.minute:02d}, "
        f"{ctx.part_of_day}; {day}.",
    ]
    if ctx.office:
        lines.append(
            "Hraješ lidem v práci, má se to líbit všem: známé a oblíbené, "
            "nic vulgárního, agresivního ani rozporuplného, ale ne nudná výplň."
        )
    lines.append(f"Teď {ctx.rule.hint}; tempo {ctx.energy_word}.")

    def clause(artists: list[str], moods: list[str]) -> str:
        parts = []
        if artists:
            parts.append(", ".join(artists))
        if moods:
            parts.append("nálada " + _quoted(moods))
        return "; ".join(parts)

    if not h.n_plays:
        lines.append("Historii pro tuhle dobu zatím nemám — ber široce oblíbený mix.")
    else:
        if good := clause(h.worked_artists[:n_worked], h.worked_moods[:n_wmood]):
            lines.append(f"V tuhle dobu se tu dohrávalo: {good} — můžeš z toho vyjít.")
        if bad := clause(h.skipped_artists[:n_skipped], h.skipped_moods[:n_smood]):
            lines.append(f"Přeskakovalo se: {bad}.")
    if h.overplayed[:n_over]:
        lines.append("Poslední dny hrálo až moc: " + ", ".join(h.overplayed[:n_over])
                     + " — vynech je.")
    if last and h.last_mood and h.last_mood_age_h is not None:
        lines.append(f"Naposledy ({_age(h.last_mood_age_h)}) hrálo „{h.last_mood}“; "
                     "navaž, jen když to sedí k téhle době.")
    lines.append(
        "Dej start_radio: krátká `mood` a 4–5 seedů, známé skladby různých interpretů "
        "a stylů, česky i zahraničně, ať si každý najde své. Starší přání ze stavu "
        "nejsou závazná; focus_artists a remember prázdné; v reply jednou větou proč."
    )
    return "\n".join(lines)
