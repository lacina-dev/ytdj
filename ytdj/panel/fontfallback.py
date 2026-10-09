"""Písmo, které umí i jiné abecedy než latinku (telka, displej).

Telka i displej kreslí text přes Pillow jedním písmem (DejaVu Sans). To umí
latinku, azbuku, řečtinu, arabštinu a hebrejštinu, ale ne korejštinu,
japonštinu, čínštinu, thajštinu, dévanágarí… — místo nich kreslilo prázdné
obdélníčky. Pillow samo náhradní písmo nehledá.

`FallbackFont` se tváří jako obyčejné písmo Pillow (`getlength`, `getbbox`,
`getmask2`, `size`…), takže `d.text(...)`, `d.textlength(...)`, `ellipsize()`
i `wrap()` fungují beze změny. Uvnitř text rozdělí na úseky podle toho, které
písmo znak opravdu má (tabulka `cmap` v souboru písma), každý úsek změří
a nakreslí jeho písmem na společnou účaří. Měření i kreslení jdou přes tytéž
úseky, takže výpustka a zalamování sedí.

  * Text, který celý umí základní písmo, jde rovnou do něj — beze změny
    jediného bodu a bez režie.
  * Náhradní písma se hledají v systému (balíčky `fonts-noto-cjk`,
    `fonts-noto-core`, případně `fonts-droid-fallback`, `fonts-symbola`),
    otevírají se až při prvním znaku, který je potřebuje, a jen ve
    velikostech, které se opravdu kreslí; otevřených je nejvýš `MAX_FACES`
    (Pi má málo paměti: jedna velikost Noto Sans CJK stojí asi 1 MB).
  * Když písmo pro znak chybí, kreslí se dál jen základním (obdélníček jako
    dřív), nic nespadne a do telemetrie jde jednou `font.missing` s balíčkem,
    který chybí. První použití náhradního písma hlásí `font.fallback`.

Pořadí úseků je logické (zleva doprava); směs zprava psaného písma s úsekem
z jiného písma se proto může seřadit jinak než v prohlížeči.
"""

from __future__ import annotations

import logging
import math
import os
import re
import struct
import time
import unicodedata
from array import array
from bisect import bisect_right
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, features

log = logging.getLogger(__name__)

# kde systém drží písma; YTDJ_FONT_DIRS (oddělené dvojtečkou) je přepíše
FONT_DIRS = ("/usr/share/fonts", "/usr/local/share/fonts")
MAX_FACES = 6  # nejvýš tolik otevřených náhradních písem (soubor × velikost) naráz
MAX_SEEN = 4000  # nejvýš tolik zapamatovaných znaků na řetěz


@dataclass(frozen=True)
class Candidate:
    """Jedno náhradní písmo: soubor, tučný řez a pro které znaky se zkouší."""

    regular: str
    bold: str = ""  # "" = tučný řez není, kreslí se obyčejným
    package: str = ""  # balíček Debianu, ve kterém je
    blocks: tuple[tuple[int, int], ...] = ()  # () = zkouší se pro jakýkoli znak
    index: int = 0  # které písmo v kolekci (.ttc)


def _noto(script: str, *blocks: tuple[int, int]) -> Candidate:
    return Candidate(f"NotoSans{script}-Regular.ttf", f"NotoSans{script}-Bold.ttf",
                     "fonts-noto-core", blocks)


# Nejdřív písma jednoho písma (zkouší se jen pro jeho bloky Unicode — ať se
# nečtou desítky souborů), pak obecná v pořadí, v jakém se mají použít.
CANDIDATES: tuple[Candidate, ...] = (
    _noto("Hebrew", (0x0590, 0x05FF), (0xFB1D, 0xFB4F)),
    _noto("Arabic", (0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF), (0xFB50, 0xFDFF),
          (0xFE70, 0xFEFF)),
    _noto("Armenian", (0x0530, 0x058F)),
    _noto("Devanagari", (0x0900, 0x097F), (0xA8E0, 0xA8FF)),
    _noto("Bengali", (0x0980, 0x09FF)),
    _noto("Gurmukhi", (0x0A00, 0x0A7F)),
    _noto("Gujarati", (0x0A80, 0x0AFF)),
    _noto("Oriya", (0x0B00, 0x0B7F)),
    _noto("Tamil", (0x0B80, 0x0BFF)),
    _noto("Telugu", (0x0C00, 0x0C7F)),
    _noto("Kannada", (0x0C80, 0x0CFF)),
    _noto("Malayalam", (0x0D00, 0x0D7F)),
    _noto("Sinhala", (0x0D80, 0x0DFF)),
    _noto("Thai", (0x0E00, 0x0E7F)),
    _noto("Lao", (0x0E80, 0x0EFF)),
    _noto("Myanmar", (0x1000, 0x109F)),
    _noto("Georgian", (0x10A0, 0x10FF)),
    _noto("Ethiopic", (0x1200, 0x139F)),
    _noto("Khmer", (0x1780, 0x17FF)),
    # korejština, japonština, čínština (v kolekci je první písmo japonské;
    # hangul a kana jsou ve všech stejné)
    Candidate("NotoSansCJK-Regular.ttc", "NotoSansCJK-Bold.ttc", "fonts-noto-cjk"),
    Candidate("DroidSansFallbackFull.ttf", "", "fonts-droid-fallback"),
    Candidate("NotoSans-Regular.ttf", "NotoSans-Bold.ttf", "fonts-noto-core"),
    Candidate("NotoSansSymbols-Regular.ttf", "NotoSansSymbols-Bold.ttf", "fonts-noto-core"),
    Candidate("NotoSansSymbols2-Regular.ttf", "", "fonts-noto-core"),
    Candidate("NotoSansMath-Regular.ttf", "", "fonts-noto-core"),
    Candidate("Symbola_hint.ttf", "", "fonts-symbola"),  # jednobarevné emoji
    Candidate("Symbola.ttf", "", "fonts-symbola"),
)

# bloky, které má fonts-noto-cjk (jen pro radu, který balíček chybí)
_CJK_BLOCKS = ((0x1100, 0x11FF), (0x2E80, 0x33FF), (0x3400, 0x4DBF), (0x4E00, 0x9FFF),
               (0xA960, 0xA97F), (0xAC00, 0xD7FF), (0xF900, 0xFAFF), (0xFE30, 0xFE4F),
               (0xFF00, 0xFFEF), (0x20000, 0x323AF))
_SYMBOL_BLOCKS = ((0x2190, 0x2BFF), (0x1F000, 0x1FAFF))


def _in(cp: int, blocks: tuple[tuple[int, int], ...]) -> bool:
    return any(lo <= cp <= hi for lo, hi in blocks)


def package_hint(cp: int, candidates: tuple[Candidate, ...] = CANDIDATES) -> str:
    """Který balíček písem by znak nejspíš uměl (do hlášení, když chybí)."""
    for c in candidates:
        if c.blocks and _in(cp, c.blocks):
            return c.package
    if _in(cp, _CJK_BLOCKS):
        return "fonts-noto-cjk"
    if _in(cp, _SYMBOL_BLOCKS):
        return "fonts-symbola"
    return "fonts-noto-core"


# ---- které znaky písmo má (tabulka cmap), bez dalších knihoven ----


class Coverage:
    """Množina znaků jako seřazené úseky (pár kB i pro písmo s 60 000 znaky)."""

    def __init__(self, ranges: list[tuple[int, int]]) -> None:
        merged: list[list[int]] = []
        for lo, hi in sorted(r for r in ranges if r[0] <= r[1]):
            if merged and lo <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        self.starts = array("L", (m[0] for m in merged))
        self.ends = array("L", (m[1] for m in merged))

    def __contains__(self, cp: int) -> bool:
        i = bisect_right(self.starts, cp) - 1
        return i >= 0 and cp <= self.ends[i]

    def __len__(self) -> int:
        return sum(e - s + 1 for s, e in zip(self.starts, self.ends))


def _format4(t: bytes) -> list[tuple[int, int]]:
    seg = struct.unpack_from(">H", t, 6)[0] // 2
    ends = struct.unpack_from(f">{seg}H", t, 14)
    starts = struct.unpack_from(f">{seg}H", t, 16 + 2 * seg)
    deltas = struct.unpack_from(f">{seg}H", t, 16 + 4 * seg)
    ro_at = 16 + 6 * seg
    offsets = struct.unpack_from(f">{seg}H", t, ro_at)
    out: list[tuple[int, int]] = []
    for i in range(seg):
        lo, hi = starts[i], ends[i]
        if lo > hi or lo == 0xFFFF:
            continue
        if offsets[i] == 0:
            zero = (-deltas[i]) & 0xFFFF  # znak, který by vyšel na glyf 0 (= chybí)
            if lo <= zero <= hi:
                out += [(lo, zero - 1), (zero + 1, hi)]
            else:
                out.append((lo, hi))
            continue
        run = None
        for c in range(lo, hi + 1):
            at = ro_at + 2 * i + offsets[i] + 2 * (c - lo)
            has = at + 2 <= len(t) and struct.unpack_from(">H", t, at)[0] != 0
            if has and run is None:
                run = c
            elif not has and run is not None:
                out.append((run, c - 1))
                run = None
        if run is not None:
            out.append((run, hi))
    return out


def _format12(t: bytes) -> list[tuple[int, int]]:
    n = struct.unpack_from(">I", t, 12)[0]
    out = []
    for i in range(n):
        lo, hi, glyph = struct.unpack_from(">III", t, 16 + 12 * i)
        out.append((lo + 1, hi) if glyph == 0 else (lo, hi))
    return out


def read_coverage(path: str | os.PathLike, index: int = 0) -> Coverage:
    """Znaky, které písmo v souboru má. Čte jen adresář tabulek a `cmap`.

    Vyhodí OSError / ValueError / struct.error, když soubor není písmo.
    """
    with open(path, "rb") as f:
        head = f.read(12)
        base = 0
        if head[:4] == b"ttcf":
            count = struct.unpack_from(">I", head, 8)[0]
            if not 0 <= index < count:
                raise ValueError(f"v kolekci není písmo {index}")
            f.seek(12 + 4 * index)
            base = struct.unpack(">I", f.read(4))[0]
            f.seek(base)
            head = f.read(12)
        tables = struct.unpack_from(">H", head, 4)[0]
        cmap = None
        for _ in range(tables):
            tag, _sum, off, length = struct.unpack(">4sIII", f.read(16))
            if tag == b"cmap":
                cmap = (off, length)
                break
        if cmap is None:
            raise ValueError("písmo nemá tabulku cmap")
        f.seek(cmap[0])
        data = f.read(cmap[1])
    ranges: list[tuple[int, int]] = []
    done: set[int] = set()
    for i in range(struct.unpack_from(">H", data, 2)[0]):
        platform, encoding, off = struct.unpack_from(">HHI", data, 4 + 8 * i)
        unicode_table = platform == 0 and encoding != 5 or platform == 3 and encoding in (1, 10)
        if not unicode_table or off in done:
            continue
        done.add(off)
        fmt = struct.unpack_from(">H", data, off)[0]
        if fmt == 4:
            ranges += _format4(data[off:off + struct.unpack_from(">H", data, off + 2)[0]])
        elif fmt == 12:
            ranges += _format12(data[off:off + struct.unpack_from(">I", data, off + 4)[0]])
    if not ranges:
        raise ValueError("písmo nemá tabulku znaků Unicode")
    return Coverage(ranges)


# ---- sdílený stav procesu: nalezené soubory, pokrytí, otevřená písma ----

_files: dict[tuple[str, ...], dict[str, str]] = {}
_coverage: dict[tuple[str, int], Coverage | None] = {}
_faces: OrderedDict[tuple[str, int, int], ImageFont.FreeTypeFont] = OrderedDict()
_reported: set[tuple[str, str]] = set()
_chains: dict[tuple[str, bool], Chain] = {}


def _event(kind: str, key: str, **fields) -> bool:
    """Událost do telemetrie nejvýš jednou za proces pro daný `key`."""
    if (kind, key) in _reported:
        return False
    _reported.add((kind, key))
    try:
        from ..telemetry import event
        event(kind, **fields)
    except Exception:  # hlášení nesmí shodit kreslení
        pass
    return True


def font_dirs() -> tuple[str, ...]:
    env = os.environ.get("YTDJ_FONT_DIRS")
    return tuple(p for p in env.split(":") if p) if env is not None else FONT_DIRS


def _find(name: str, dirs: tuple[str, ...]) -> str | None:
    """Cesta k souboru písma podle jména; adresáře se projdou jednou."""
    index = _files.get(dirs)
    if index is None:
        index = {}
        for top in dirs:
            for folder, _sub, names in os.walk(top):
                for n in names:
                    if n.lower().endswith((".ttf", ".ttc", ".otf")):
                        index.setdefault(n, os.path.join(folder, n))
        _files[dirs] = index
    return index.get(name)


def coverage(path: str, index: int = 0) -> Coverage | None:
    """Pokrytí písma (jednou přečtené); None = soubor nejde přečíst."""
    key = (path, index)
    if key not in _coverage:
        try:
            _coverage[key] = read_coverage(path, index)
        except (OSError, ValueError, struct.error) as exc:
            _coverage[key] = None
            log.warning("písmo %s nejde přečíst: %s", path, exc)
            _event("font.error", path, file=os.path.basename(path),
                   error=f"{type(exc).__name__}: {exc}"[:160])
    return _coverage[key]


def _sticks(ch: str) -> bool:
    """Znak, který patří k předchozímu (kombinující znaménko, spojovač, varianta)."""
    cp = ord(ch)
    return unicodedata.category(ch)[0] == "M" or cp in (0x200C, 0x200D) \
        or 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF or 0x1F3FB <= cp <= 0x1F3FF


def _needs_no_glyph(ch: str) -> bool:
    return unicodedata.category(ch) in ("Cc", "Cf", "Zs", "Zl", "Zp") \
        or 0xFE00 <= ord(ch) <= 0xFE0F


class Chain:
    """Které písmo kreslí který znak: 0 = základní, n = n-tý kandidát."""

    def __init__(self, base_path: str, bold: bool = False, *,
                 candidates: tuple[Candidate, ...] | None = None,
                 dirs: tuple[str, ...] | None = None) -> None:
        self.base_path = base_path
        self.bold = bold
        self.candidates = CANDIDATES if candidates is None else candidates
        self.dirs = font_dirs() if dirs is None else dirs
        self.base = coverage(base_path)
        self._slots: dict[str, int] = {}
        self._paths: dict[int, str | None] = {}

    def path(self, slot: int) -> str | None:
        """Soubor n-tého kandidáta (tučný řez, když je), None = není v systému."""
        if slot not in self._paths:
            c = self.candidates[slot - 1]
            found = (self.bold and c.bold and _find(c.bold, self.dirs)) or _find(c.regular, self.dirs)
            self._paths[slot] = found or None
        return self._paths[slot]

    def _resolve(self, ch: str) -> int:
        cp = ord(ch)
        if self.base is None or cp in self.base or _needs_no_glyph(ch):
            return 0
        for slot, c in enumerate(self.candidates, 1):
            if c.blocks and not _in(cp, c.blocks):
                continue
            path = self.path(slot)
            if path is None:
                continue
            cov = coverage(path, c.index)
            if cov is not None and cp in cov:
                return slot
        hint = package_hint(cp, self.candidates)
        if _event("font.missing", hint, char=f"U+{cp:04X}", package=hint,
                  name=unicodedata.name(ch, "")[:60] or None):
            log.warning("pro znak U+%04X chybí písmo (balíček %s)", cp, hint)
        return 0

    def slot(self, ch: str) -> int:
        s = self._slots.get(ch)
        if s is None:
            if len(self._slots) >= MAX_SEEN:
                self._slots.clear()
            s = self._slots[ch] = self._resolve(ch)
        return s

    def covers(self, slot: int, ch: str) -> bool:
        if slot == 0:
            return self.base is None or ord(ch) in self.base
        path = self.path(slot)
        cov = coverage(path, self.candidates[slot - 1].index) if path else None
        return cov is not None and ord(ch) in cov

    def runs(self, text: str) -> list[tuple[int, str]]:
        """Text rozdělený na úseky (písmo, znaky) v pořadí, jak jdou za sebou."""
        out: list[tuple[int, str]] = []
        cur, start = -1, 0
        for i, ch in enumerate(text):
            if cur >= 0 and _sticks(ch) and (self.covers(cur, ch) or _needs_no_glyph(ch)):
                continue  # znaménko zůstává u svého písmene
            s = self.slot(ch)
            if s != cur:
                if cur >= 0:
                    out.append((cur, text[start:i]))
                cur, start = s, i
        if cur >= 0:
            out.append((cur, text[start:]))
        return out

    def face(self, slot: int, size: int) -> ImageFont.FreeTypeFont | None:
        """Otevřené písmo kandidáta v dané velikosti (nejvýš MAX_FACES naráz)."""
        path = self.path(slot)
        if path is None:
            return None
        index = self.candidates[slot - 1].index
        key = (path, index, size)
        f = _faces.get(key)
        if f is not None:
            _faces.move_to_end(key)
            return f
        t0 = time.monotonic()
        try:
            f = ImageFont.truetype(path, size, index=index)
        except OSError as exc:
            _event("font.error", path, file=os.path.basename(path),
                   error=f"{type(exc).__name__}: {exc}"[:160])
            return None
        _faces[key] = f
        while len(_faces) > MAX_FACES:
            _faces.popitem(last=False)
        _event("font.fallback", path, file=os.path.basename(path),
               package=self.candidates[slot - 1].package, size=size,
               open_ms=int((time.monotonic() - t0) * 1000))
        return f


def chain_for(base_path: str, bold: bool) -> Chain:
    key = (base_path, bold)
    c = _chains.get(key)
    if c is None or c.dirs != font_dirs():
        c = _chains[key] = Chain(base_path, bold)
    return c


def reset() -> None:
    """Zapomene nalezené soubory, pokrytí i otevřená písma (testy)."""
    _files.clear()
    _coverage.clear()
    _faces.clear()
    _reported.clear()
    _chains.clear()


_RAQM = bool(features.check("raqm"))
# hebrejština, arabština a jejich prezentační tvary (písma psaná zprava doleva)
_RTL = re.compile("[\u0590-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFC]")
MAX_TEXTS = 512  # nejvýš tolik zapamatovaných rozdělení textu na písmo


def _direction(text, direction):
    """Řádek se skládá zleva doprava jako na webu, i když začíná arabsky či hebrejsky
    (slova psaná zprava doleva řadí raqm uvnitř řádku správně); jinak by se
    oddělovač „ · “ před interpretem ocitl na druhém konci."""
    if direction is None and _RAQM and isinstance(text, str) and _RTL.search(text):
        return "ltr"
    return direction


class FallbackFont:
    """Písmo Pillow, které znaky mimo základní písmo kreslí náhradním.

    Stačí ho dát tam, kam se dává `ImageFont.FreeTypeFont`. Ostatní vlastnosti
    (`size`, `getmetrics()`, `path`…) jsou vlastnosti základního písma.
    """

    def __init__(self, base: ImageFont.FreeTypeFont, chain: Chain) -> None:
        self.base = base
        self.chain = chain
        self.size = base.size
        self._dy: dict[str, int] = {}
        self._texts: dict[str, list[tuple[int, str]] | None] = {}

    def __getattr__(self, name: str):
        return getattr(self.base, name)

    # ---- úseky ----

    def runs(self, text) -> list[tuple[int, str]] | None:
        """Úseky textu podle písma; None = celý ho kreslí základní písmo."""
        if not isinstance(text, str) or text.isascii():
            return None
        try:
            return self._texts[text]
        except KeyError:
            pass
        runs = self.chain.runs(text)
        if all(slot == 0 for slot, _ in runs):
            runs = None
        if len(self._texts) >= MAX_TEXTS:
            self._texts.clear()
        self._texts[text] = runs
        return runs

    def _layout(self, runs, mode, direction, features_, language):
        """[(písmo, znaky, x)], celková šířka a směr; x je od začátku textu."""
        if direction is None and _RAQM:
            direction = "ltr"  # úseky jdou zleva doprava; uvnitř úseku řadí raqm
        placed = []
        x = 0.0
        for slot, s in runs:
            f = (self.chain.face(slot, self.size) if slot else None) or self.base
            placed.append((f, s, x))
            x += f.getlength(s, mode, direction, features_, language)
        return placed, x, direction

    def _baseline(self, v: str) -> int:
        """O kolik je účaří pod bodem, ke kterému se text kotví (jako základní písmo)."""
        dy = self._dy.get(v)
        if dy is None:
            dy = self._dy[v] = int(self.base.getbbox("x", anchor="l" + v)[1]
                                   - self.base.getbbox("x", anchor="ls")[1])
        return dy

    def _extent(self, placed, mode, direction, features_, language, stroke_width):
        """Obrys všech úseků vůči začátku textu na účaří: (l, t, r, b)."""
        box = None
        for f, s, x in placed:
            l, t, r, b = f.getbbox(s, mode, direction, features_, language, stroke_width, "ls")
            l, r = l + x, r + x
            box = (l, t, r, b) if box is None else \
                (min(box[0], l), min(box[1], t), max(box[2], r), max(box[3], b))
        return box or (0, 0, 0, 0)

    def _anchor(self, anchor, total, box) -> tuple[float, float]:
        anchor = anchor or "la"
        h, v = anchor[0], anchor[1]
        dx = {"l": 0.0, "m": -total / 2, "r": -total}.get(h, 0.0)
        if v == "t":
            dy = -box[1]
        elif v == "b":
            dy = -box[3]
        else:
            dy = self._baseline(v)
        return dx, dy

    # ---- rozhraní písma Pillow ----

    def getlength(self, text, mode="", direction=None, features=None, language=None):
        runs = self.runs(text)
        if runs is None:
            return self.base.getlength(text, mode, _direction(text, direction), features,
                                       language)
        return self._layout(runs, mode, direction, features, language)[1]

    def getbbox(self, text, mode="", direction=None, features=None, language=None,
                stroke_width=0, anchor=None):
        runs = self.runs(text)
        if runs is None:
            return self.base.getbbox(text, mode, _direction(text, direction), features,
                                     language, stroke_width, anchor)
        placed, total, direction = self._layout(runs, mode, direction, features, language)
        box = self._extent(placed, mode, direction, features, language, stroke_width)
        dx, dy = self._anchor(anchor, total, box)
        return (math.floor(box[0] + dx), int(box[1] + dy), math.ceil(box[2] + dx),
                int(box[3] + dy))

    def getmask2(self, text, mode="", direction=None, features=None, language=None,
                 stroke_width=0, anchor=None, ink=0, start=None, *args, **kwargs):
        runs = self.runs(text)
        if runs is None or mode == "RGBA":
            return self.base.getmask2(text, mode, direction=_direction(text, direction),
                                      features=features,
                                      language=language, stroke_width=stroke_width,
                                      anchor=anchor, ink=ink, start=start, *args, **kwargs)
        placed, total, direction = self._layout(runs, mode, direction, features, language)
        box = self._extent(placed, mode, direction, features, language, stroke_width)
        dx, dy = self._anchor(anchor, total, box)
        sx, sy = start or (0, 0)
        x0 = dx + sx
        ix = math.floor(x0)
        frac = x0 - ix
        left, top = math.floor(box[0]), int(box[1])
        size = (max(1, math.ceil(box[2]) - left + 2), max(1, int(box[3]) - top))
        canvas = Image.new("L", size, 0)
        d = ImageDraw.Draw(canvas)
        if mode == "1":
            d.fontmode = "1"
        for f, s, x in placed:
            d.text((x + frac - left, -top), s, font=f, fill=255, anchor="ls",
                   direction=direction, features=features, language=language,
                   stroke_width=stroke_width)
        return canvas.im, (ix + left, int(round(dy + sy)) + top)

    def getmask(self, text, mode="", *args, **kwargs):
        return self.getmask2(text, mode, *args, **kwargs)[0]


def with_fallback(font, bold: bool = False):
    """Základní písmo → písmo s náhradními; co není TrueType, vrátí beze změny."""
    path = getattr(font, "path", None)
    if not isinstance(font, ImageFont.FreeTypeFont) or not isinstance(path, (str, os.PathLike)):
        return font
    return FallbackFont(font, chain_for(str(Path(path)), bold))
