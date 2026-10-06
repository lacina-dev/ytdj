"""Nápověda a „Jak to funguje" — stránky pro kolegy (POZADAVKY #47).

Dvě stránky webu:

- `/napoveda` — rychlý návod (statický `web/static/napoveda.html`); čísla
  v něm (`<span data-cfg="wish_block">3</span>`) se doplní z nastavení, aby
  text platil, i když je správce změní.
- `/jak-to-funguje` — úplná dokumentace skládaná živě z `docs/FUNKCE.md`
  (závazná pravidla) a `docs/JAK-TO-FUNGUJE.md` (úvody k oblastem, výběr
  automatik, nastavení u pravidel). Změna FUNKCE.md se na stránce ukáže sama:
  stránka se znovu složí, když se změní některý ze souborů nebo nastavení.

Jen standardní knihovna — načte ho i falešný server pro snímky
(`tests/fake_ytdj.py`, systémový python bez starlette). Nic tu nesmí běžet
ve smyčce asyncio: `Pages.get` čte disk a volá se přes `asyncio.to_thread`.
"""

from __future__ import annotations

import gzip
import hashlib
import html
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import DEFAULTS

ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = ROOT / "docs"
FUNKCE_MD = DOCS_DIR / "FUNKCE.md"
INTRO_MD = DOCS_DIR / "JAK-TO-FUNGUJE.md"
STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"
NAPOVEDA_HTML = STATIC_DIR / "napoveda.html"
DOCS_HTML = STATIC_DIR / "jak-to-funguje.html"
MANUAL_CSS = STATIC_DIR / "manual.css"
ISSUES_HTML = STATIC_DIR / "hlaseni.html"  # Chyby a nápady (ytdj/issues.py)
THEME_JS = STATIC_DIR / "theme.js"  # vzhled Auto / Den / Noc, společný všem stránkám (F-WEB-09)

# oddíly JAK-TO-FUNGUJE.md, které nejsou úvodem oblasti
AUTO_TITLE = "Automatiky, o kterých možná nevíš"
SETTINGS_TITLE = "Nastavení u pravidel"
CHANGES_TITLE = "Změny se souhlasem vlastníka"

RULE = re.compile(r"^- \*\*(F-[A-Z]+-\d{2})\*\*\s*(.*)$")
# poznámka k pravidlu (souhlas vlastníka, vznik, zrušení) — ukazuje se
# („Změněno …", „Nové pravidlo …", „Upřesněno 26. 9. 2026 …" — slovo a datum na začátku)
NOTE = re.compile(r"^(Změněno|Nové pravidlo|Zrušen[oa]?|Přidáno|Upřesněno|Doplněno|Opraveno)\b"
                  r"|^\S+(?: \S+)? \d{1,2}\. ?\d{1,2}\. ?20\d\d\b")
# „(Přidáno 26. 9. 2026 na přání vlastníka: …)" na konci věty = poznámka
TAIL_NOTE = re.compile(r"\s*\(((?:Přidáno|Změněno|Nové pravidlo)\b[^()]*)\)\s*$")
CFG_KEY = re.compile(r"`([a-z][a-z0-9_]+)`")
# čísla a přepínače z nastavení; texty a seznamy (cesty, cookies, sprostá
# slova navíc) na veřejnou stránku nepatří
SHOWN_KEYS = frozenset(k for k, v in DEFAULTS.items() if isinstance(v, (bool, int)))


def fold(text: str) -> str:
    """Bez diakritiky a malými písmeny — pro kotvy a hledání."""
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", fold(title)).strip("-")


@dataclass
class Rule:
    id: str
    text: str  # markdown jedné věty (bez odkazů na testy)
    notes: list[str] = field(default_factory=list)  # souhlas vlastníka, vznik

    @property
    def keys(self) -> list[str]:
        """Klíče nastavení zmíněné v textu (`wish_block`…)."""
        return [k for k in dict.fromkeys(CFG_KEY.findall(self.text)) if k in DEFAULTS]


@dataclass
class Area:
    title: str
    rules: list[Rule] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return slug(self.title)


@dataclass
class Funkce:
    areas: list[Area]
    changes: list[tuple[str, str, str]]  # (datum, pravidlo, co a proč)

    def rule(self, rid: str) -> Rule | None:
        for a in self.areas:
            for r in a.rules:
                if r.id == rid:
                    return r
        return None

    @property
    def count(self) -> int:
        return sum(len(a.rules) for a in self.areas)


def parse_funkce(text: str) -> Funkce:
    """Oblasti (nadpisy ## s aspoň jedním pravidlem) a tabulka změn.

    Řádky „Testy: …" a „(bez testu: …)" jsou pro vývojáře — vynechají se.
    Poznámky o souhlasu vlastníka a o vzniku pravidla zůstanou.
    """
    areas: list[Area] = []
    changes: list[tuple[str, str, str]] = []
    area: Area | None = None
    rule: Rule | None = None
    section = ""
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            area, rule = Area(section), None
            areas.append(area)
            continue
        if section == CHANGES_TITLE:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if line.startswith("|") and len(cells) == 3 and not set(cells[0]) <= set("-: ") \
                    and cells[0] != "Datum":
                changes.append((cells[0], cells[1], cells[2]))
            continue
        m = RULE.match(line)
        if m and area is not None:
            rule = Rule(m.group(1), m.group(2).strip())
            area.rules.append(rule)
            continue
        if rule is not None and line.startswith("  "):
            body = line.strip()
            if not body or body.startswith("Testy:") or body.startswith("(bez testu"):
                continue
            if NOTE.match(body):
                rule.notes.append(body)
            elif rule.notes:
                rule.notes[-1] += " " + body  # zalomená poznámka
            else:
                rule.text += " " + body  # zalomená věta pravidla
            continue
        rule = None
    for a in areas:
        for r in a.rules:
            m = TAIL_NOTE.search(r.text)
            if m:
                r.notes.insert(0, m.group(1))
                r.text = r.text[: m.start()].rstrip()
    return Funkce([a for a in areas if a.rules], changes)


@dataclass
class Intro:
    areas: dict[str, str]  # název oblasti → úvod (markdown, odstavce)
    auto: list[tuple[str, str]]  # (ID pravidla, krátký nadpis)
    settings: dict[str, list[str]]  # ID pravidla → klíče nastavení navíc


_AUTO_ITEM = re.compile(r"^- \*\*(F-[A-Z]+-\d{2})\*\*\s*(.+)$")
_SETTINGS_ITEM = re.compile(r"^- (F-[A-Z]+-\d{2}):\s*(.+)$")


def parse_intro(text: str) -> Intro:
    """docs/JAK-TO-FUNGUJE.md: text před prvním ## je návod pro autory (nezobrazí se)."""
    out = Intro({}, [], {})
    section: str | None = None
    buf: list[str] = []

    def flush() -> None:
        if section and section not in (AUTO_TITLE, SETTINGS_TITLE):
            out.areas[section] = "\n".join(buf).strip()

    for line in text.splitlines():
        if line.startswith("## "):
            flush()
            section, buf = line[3:].strip(), []
            continue
        if section == AUTO_TITLE:
            if m := _AUTO_ITEM.match(line.strip()):
                out.auto.append((m.group(1), m.group(2).strip()))
        elif section == SETTINGS_TITLE:
            if m := _SETTINGS_ITEM.match(line.strip()):
                out.settings[m.group(1)] = [k.strip(" `") for k in m.group(2).split(",") if k.strip()]
        elif section is not None:
            buf.append(line)
    flush()
    return out


# ---- HTML ----

def inline(md: str) -> str:
    """Markdown jedné věty → HTML: jen `kód` a **tučně**, zbytek escapovaný."""
    out = html.escape(md, quote=False)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    return re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", out)


def paragraphs(md: str) -> str:
    return "".join(f"<p>{inline(' '.join(p.split()))}</p>" for p in re.split(r"\n\s*\n", md) if p.strip())


def value_text(value: Any) -> str:
    if isinstance(value, bool):
        return "zapnuto" if value else "vypnuto"
    return str(value)


def rule_keys(rule: Rule, intro: Intro) -> list[str]:
    keys = list(dict.fromkeys(rule.keys + intro.settings.get(rule.id, [])))
    return [k for k in keys if k in SHOWN_KEYS]


def settings_html(keys: list[str], values: dict[str, Any], labels: dict[str, str]) -> str:
    if not keys:
        return ""
    chips = []
    for k in keys:
        v = values.get(k, DEFAULTS[k])
        label = labels.get(k) or k
        default = "" if v == DEFAULTS[k] else f" <small>(výchozí {html.escape(value_text(DEFAULTS[k]))})</small>"
        chips.append(f'<span class="cfg" title="{html.escape(k)}">{html.escape(label)}: '
                     f'<b>{html.escape(value_text(v))}</b>{default}</span>')
    return ('<div class="cfgs"><span class="cfg-h">Teď nastaveno'
            ' <small>(mění se v Nastavení jukeboxu)</small>:</span> ' + " ".join(chips) + "</div>")


def rule_html(rule: Rule, intro: Intro, values: dict, labels: dict, anchor: bool = True) -> str:
    rid = html.escape(rule.id)
    notes = "".join(
        f'<p class="note{" consent" if "souhlasem" in n else ""}">{inline(n)}</p>' for n in rule.notes)
    attr = f' id="{rid}"' if anchor else ""
    return (f'<li class="rule"{attr} data-id="{rid}"><a class="rid" href="#{rid}">{rid}</a>'
            f'<div class="rt"><p class="txt">{_rule_links(rule.text, md=True)}</p>{notes}'
            f"{settings_html(rule_keys(rule, intro), values, labels)}</div></li>")


def render_docs(template: str, funkce: Funkce, intro: Intro, values: dict | None = None,
                labels: dict | None = None, updated: float | None = None) -> str:
    """Stránka „Jak to funguje" ze šablony a rozparsovaných souborů."""
    values = values or {}
    labels = labels or {}
    auto = []
    for rid, headline in intro.auto:
        r = funkce.rule(rid)
        if r is None:
            continue  # test hlídá, že výběr odkazuje jen na existující pravidla
        auto.append(
            f'<li class="auto"><h3>{inline(headline)}</h3><p>{inline(r.text)}</p>'
            f'<a class="more" href="#{html.escape(rid)}">{html.escape(rid)} ›</a></li>')
    areas = []
    for a in funkce.areas:
        rules = "".join(rule_html(r, intro, values, labels) for r in a.rules)
        areas.append(
            f'<details class="area card" id="{a.slug}" data-title="{html.escape(a.title)}">'
            f'<summary><span class="at">{html.escape(a.title)}</span>'
            f'<span class="count">{len(a.rules)}</span></summary>'
            f'<div class="intro">{paragraphs(intro.areas.get(a.title, ""))}</div>'
            f'<ol class="rules">{rules}</ol></details>')
    toc = "".join(
        f'<a href="#{a.slug}">{html.escape(a.title)} <span>{len(a.rules)}</span></a>' for a in funkce.areas)
    rows = "".join(
        f"<tr><td>{html.escape(d)}</td><td>{_rule_links(r)}</td><td>{inline(w)}</td></tr>"
        for d, r, w in funkce.changes)
    when = time.strftime("%-d. %-m. %Y %H:%M", time.localtime(updated)) if updated else ""
    stav = (f"{funkce.count} pravidel v {len(funkce.areas)} oblastech"
            + (f" · změněno {when}" if when else ""))
    out = template
    for key, val in (("STAV", html.escape(stav)), ("AUTO", "".join(auto)), ("OBSAH", "".join(toc)),
                     ("OBLASTI", "".join(areas)), ("ZMENY", rows)):
        out = out.replace(f"<!--YTDJ:{key}-->", val)
    return out


def _rule_links(text: str, md: bool = False) -> str:
    """„F-HLAS-04, F-DISPLEJ-12 (nové)" → odkazy na pravidla."""
    return re.sub(r"F-[A-Z]+-\d{2}", lambda m: f'<a href="#{m.group(0)}">{m.group(0)}</a>',
                  inline(text) if md else html.escape(text))


_CFG_SPAN = re.compile(r'(<span data-cfg="([a-z0-9_]+)"(?: data-forms="([^"]*)")?>)([^<]*)(</span>)')


def plural(n: int, forms: str) -> str:
    """České tvary: "skladbu|skladby|skladeb" (1 | 2–4 | 5+) nebo "skladbě|skladbách" (1 | jinak)."""
    f = forms.split("|")
    if n == 1:
        return f[0]
    if len(f) >= 3 and not 2 <= n <= 4:
        return f[2]
    return f[1] if len(f) > 1 else f[0]


def fill_cfg(page: str, values: dict[str, Any]) -> str:
    """`<span data-cfg="wish_block" data-forms="skladba|skladby|skladeb">3</span>`
    → aktuální hodnota z nastavení (i se správným tvarem slova)."""
    def sub(m: re.Match) -> str:
        key, forms = m.group(2), m.group(3)
        if key not in SHOWN_KEYS:
            return m.group(0)
        v = values.get(key, DEFAULTS[key])
        text = value_text(v)
        if forms and isinstance(v, int) and not isinstance(v, bool):
            text += " " + plural(v, forms)
        return m.group(1) + html.escape(text) + m.group(5)
    return _CFG_SPAN.sub(sub, page)


# ---- servírování (cache podle souborů a nastavení) ----

def _stamp(path: Path) -> tuple:
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (None, None)


@dataclass
class Page:
    raw: bytes
    packed: bytes  # gzip
    etag: str
    media: str


MISSING_DOCS = ("<p class=\"missing\">Popis funkcí (<code>docs/FUNKCE.md</code>) na tomhle "
                "zařízení chybí — nasazení ho má kopírovat s aplikací.</p>")


class Pages:
    """Hotové stránky v paměti, zabalené; znovu se složí jen po změně
    souborů nebo nastavení (ETag = otisk obsahu)."""

    NAMES = {"napoveda": NAPOVEDA_HTML, "jak-to-funguje": DOCS_HTML, "manual.css": MANUAL_CSS,
             "hlaseni": ISSUES_HTML, "theme.js": THEME_JS}

    def __init__(self, funkce: Path = FUNKCE_MD, intro: Path = INTRO_MD) -> None:
        self.funkce = funkce
        self.intro = intro
        self._cache: dict[str, tuple[tuple, Page]] = {}
        self._lock = threading.Lock()

    def get(self, name: str, values: dict | None = None, labels: dict | None = None) -> Page | None:
        """Blokuje (disk) — ze smyčky jen přes asyncio.to_thread."""
        src = self.NAMES.get(name)
        if src is None:
            return None
        values = {k: v for k, v in (values or {}).items() if k in SHOWN_KEYS}
        deps = [src] + ([self.funkce, self.intro] if name == "jak-to-funguje" else [])
        key = (tuple(_stamp(p) for p in deps), tuple(sorted(values.items())))
        with self._lock:
            hit = self._cache.get(name)
            if hit and hit[0] == key:
                return hit[1]
        if key[0][0] == (None, None):
            return None
        page = self._build(name, src, values, labels or {})
        with self._lock:
            self._cache[name] = (key, page)
        return page

    def _build(self, name: str, src: Path, values: dict, labels: dict) -> Page:
        text = src.read_text(encoding="utf-8")
        media = ("text/css; charset=utf-8" if name.endswith(".css")
                 else "text/javascript; charset=utf-8" if name.endswith(".js")
                 else "text/html; charset=utf-8")
        if name == "napoveda":
            text = fill_cfg(text, values)
        elif name == "jak-to-funguje":
            try:
                funkce = parse_funkce(self.funkce.read_text(encoding="utf-8"))
                updated = self.funkce.stat().st_mtime
            except OSError:
                funkce, updated = Funkce([], []), None
            try:
                intro = parse_intro(self.intro.read_text(encoding="utf-8"))
            except OSError:
                intro = Intro({}, [], {})
            text = render_docs(text, funkce, intro, values, labels, updated)
            if not funkce.areas:
                text = text.replace("<!--YTDJ:CHYBI-->", MISSING_DOCS)
        raw = text.encode("utf-8")
        etag = '"%s"' % hashlib.sha1(raw).hexdigest()[:16]
        return Page(raw, gzip.compress(raw, 6), etag, media)
