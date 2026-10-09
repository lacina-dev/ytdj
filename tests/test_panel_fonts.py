"""Jiné abecedy než latinka na telce a na displeji (hlášení #29: korejské znaky).

Jako testy panelu se pouští systémovým pythonem (Pillow není ve venvu):

    YTDJ_EVENTS_FILE=/tmp/x.jsonl python3 -m unittest tests.test_panel_fonts

Co je kde:
  * Cmap, Runs, Missing — nezávislé na písmech v systému: pokrytí se čte
    z písem vyrobených v testu (jen tabulka `cmap`), chybějící písma jsou
    prázdný adresář.
  * MeasureAndDraw — rodina DejaVu (na ní stojí celý displej) v roli
    základního i „náhradního" písma.
  * RealScripts — skutečná korejština, japonština, čínština, thajština…;
    písmo, které v systému není, test výslovně přeskočí a řekne které.
"""

from __future__ import annotations

import logging
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if not os.environ.get("YTDJ_EVENTS_FILE"):
    _EVENTS_TMP = tempfile.TemporaryDirectory(prefix="ytdj-font-tests-")
    os.environ["YTDJ_EVENTS_FILE"] = str(Path(_EVENTS_TMP.name) / "events.jsonl")

from PIL import Image, ImageChops, ImageDraw, ImageFont  # noqa: E402

from ytdj.panel import fontfallback as ff  # noqa: E402
from ytdj.panel.fontfallback import Candidate, Chain, Coverage, FallbackFont  # noqa: E402
from ytdj.panel.ui import FONT_DIR, Fonts, ellipsize, wrap  # noqa: E402
from ytdj.panel.ui import Renderer as PanelRenderer, View  # noqa: E402
from ytdj.tv.screen import Renderer, view_from  # noqa: E402
from tv_shots import NOW, SIZES, WISH  # noqa: E402

logging.getLogger(ff.__name__).addHandler(logging.NullHandler())  # varování nepatří do výpisu testů

MIXED = "BTS (방탄소년단) — 봄날 (Spring Day)"
HANGUL = (0xAC00, 0xD7A3)
LATIN = ((0x20, 0x7E), (0xA0, 0x17F), (0x2010, 0x2026))

KOREAN = {
    **WISH,
    "current": {"id": "nocover0001", "title": "봄날 (Spring Day)", "artist": "BTS (방탄소년단)",
                "duration": 214,
                "reason": {"kind": "wish", "who": "지민", "text": "방탄소년단 봄날 틀어줘"}},
    "queue": [{"id": "q1", "title": "夜に駆ける", "artist": "YOASOBI", "req": {"who": "さくら"}}],
    "dj": {"busy": False, "last": {"reply": "Zařazuju 봄날 od 방탄소년단.", "who": "지민",
                                   "at": NOW - 60}},
}

# písmo → ukázka; každý znak ukázky musí mít skutečný glyf
SCRIPTS = {
    "korejština (hangul)": "방탄소년단 봄날",
    "japonština (kana a kandži)": "夜に駆ける ヨアソビ",
    "čínština": "月亮代表我的心 鄧麗君",
    "azbuka": "Группа крови Кино",
    "řečtina": "Ελληνικά τραγούδια",
    "arabština": "حبيبي يا نور العين",
    "hebrejština": "שיר אהבה",
    "thajština": "เพลงรัก ลูกทุ่ง",
    "dévanágarí": "तुम ही हो कभी",
}


def _format12(ranges) -> bytes:
    groups = b"".join(struct.pack(">III", lo, hi, 1 + i) for i, (lo, hi) in enumerate(ranges))
    return struct.pack(">HHIII", 12, 0, 16 + len(groups), 0, len(ranges)) + groups


def _format4(segments, glyph_ids=()) -> bytes:
    """segments: (start, end, delta, range_offset); poslední musí být 0xFFFF."""
    n = len(segments)
    body = struct.pack(f">{n}H", *(s[1] for s in segments)) + b"\0\0"
    body += struct.pack(f">{n}H", *(s[0] for s in segments))
    body += struct.pack(f">{n}H", *(s[2] & 0xFFFF for s in segments))
    body += struct.pack(f">{n}H", *(s[3] for s in segments))
    body += struct.pack(f">{len(glyph_ids)}H", *glyph_ids)
    return struct.pack(">HHHHHHH", 4, 14 + len(body), 0, 2 * n, 0, 0, 0) + body


def _sfnt(subtable: bytes, at: int = 0, platform=(3, 10)) -> bytes:
    """Nejmenší „písmo": adresář tabulek a cmap s jednou tabulkou znaků."""
    cmap = struct.pack(">HH", 0, 1) + struct.pack(">HHI", *platform, 12) + subtable
    head = struct.pack(">IHHHH", 0x00010000, 1, 16, 0, 0)
    return head + struct.pack(">4sIII", b"cmap", 0, at + 28, len(cmap)) + cmap


def fake_font(path: Path, ranges) -> str:
    path.write_bytes(_sfnt(_format12(ranges)))
    return str(path)


class _Clean(unittest.TestCase):
    """Každý test začíná bez zapamatovaných písem a hlášení a nic po sobě nenechá."""

    def setUp(self) -> None:
        ff.reset()
        self.addCleanup(ff.reset)
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="ytdj-fonts-")))
        self.events = self.enterContext(mock.patch("ytdj.telemetry.event"))

    def kinds(self, kind: str) -> list[dict]:
        return [c.kwargs for c in self.events.call_args_list if c.args[0] == kind]


class Cmap(_Clean):
    def test_reads_both_kinds_of_character_tables(self):
        cov = ff.read_coverage(fake_font(self.tmp / "a.ttf", [HANGUL, (0x1F600, 0x1F64F)]))
        self.assertIn(ord("봄"), cov)
        self.assertIn(0x1F600, cov)
        self.assertNotIn(ord("A"), cov)
        self.assertNotIn(0xD7A4, cov)
        self.assertEqual(len(cov), 0xD7A3 - 0xAC00 + 1 + 0x50)
        # starší tabulka (format 4): úsek s posunem, úsek přes pole glyfů, zarážka
        table = _format4([(0x30, 0x39, -0x35, 0),  # „5" vychází na glyf 0 = chybí
                          (0x41, 0x5A, -0x40, 0),
                          (0x61, 0x63, 0, 4),  # a, b, c → glyfy 5, 0, 7: „b" chybí
                          (0xFFFF, 0xFFFF, 1, 0)], glyph_ids=(5, 0, 7))
        (self.tmp / "b.ttf").write_bytes(_sfnt(table, platform=(3, 1)))
        cov = ff.read_coverage(self.tmp / "b.ttf")
        self.assertEqual("".join(c for c in "0459AZ[abc￿" if ord(c) in cov), "049AZac")

    def test_a_glyph_zero_at_the_start_of_a_group_is_missing(self):
        sub = struct.pack(">HHIII", 12, 0, 28, 0, 1) + struct.pack(">III", 0x41, 0x43, 0)
        (self.tmp / "z.ttf").write_bytes(_sfnt(sub))
        cov = ff.read_coverage(self.tmp / "z.ttf")
        self.assertNotIn(0x41, cov)
        self.assertIn(0x42, cov)

    def test_reads_the_right_font_of_a_collection(self):
        first, second = _format12([(0x41, 0x5A)]), _format12([HANGUL])
        at1 = 12 + 8
        f1 = _sfnt(first, at=at1)
        f2 = _sfnt(second, at=at1 + len(f1))
        ttc = b"ttcf" + struct.pack(">II", 0x00020000, 2) \
            + struct.pack(">II", at1, at1 + len(f1)) + f1 + f2
        (self.tmp / "c.ttc").write_bytes(ttc)
        self.assertIn(ord("A"), ff.read_coverage(self.tmp / "c.ttc", 0))
        self.assertNotIn(ord("봄"), ff.read_coverage(self.tmp / "c.ttc", 0))
        self.assertIn(ord("봄"), ff.read_coverage(self.tmp / "c.ttc", 1))
        with self.assertRaises(ValueError):
            ff.read_coverage(self.tmp / "c.ttc", 2)

    def test_a_broken_file_is_no_font_and_is_reported_once(self):
        bad = self.tmp / "bad.ttf"
        bad.write_bytes(b"this is not a font at all, not even close")
        for _ in range(3):
            self.assertIsNone(ff.coverage(str(bad)))
        self.assertIsNone(ff.coverage(str(self.tmp / "nowhere.ttf")))
        errors = self.kinds("font.error")
        self.assertEqual([e["file"] for e in errors], ["bad.ttf", "nowhere.ttf"])

    def test_dejavu_has_czech_cyrillic_greek_but_no_korean_thai_or_hindi(self):
        path = FONT_DIR / "DejaVuSans.ttf"
        self.assertTrue(path.exists(), f"chybí {path} (balíček fonts-dejavu-core)")
        cov = ff.read_coverage(path)
        for ch in "AřŽůЖяαωשع…„“·—":
            self.assertIn(ord(ch), cov, ch)
        for ch in "봄날夜にヨ月กเअह":
            self.assertNotIn(ord(ch), cov, ch)


class Runs(_Clean):
    def chain(self, bold: bool = False, **files) -> Chain:
        base = fake_font(self.tmp / "Base.ttf", LATIN)
        for name, ranges in files.items():
            fake_font(self.tmp / f"{name}.ttf", ranges)
        cands = (Candidate("Thai.ttf", "ThaiBold.ttf", "fonts-noto-core", ((0x0E00, 0x0E7F),)),
                 Candidate("Korean.ttf", "KoreanBold.ttf", "fonts-noto-cjk"),
                 Candidate("Other.ttf", "", "fonts-droid-fallback"))
        return Chain(base, bold, candidates=cands, dirs=(str(self.tmp),))

    def test_each_character_goes_to_the_first_font_that_has_it(self):
        c = self.chain(Korean=[HANGUL], Other=[HANGUL, (0x3040, 0x30FF)])
        self.assertEqual(c.runs(MIXED), [(0, "BTS ("), (2, "방탄소년단"), (0, ") — "), (2, "봄날"),
                                         (0, " (Spring Day)")])
        self.assertEqual("".join(s for _, s in c.runs(MIXED)), MIXED)
        self.assertEqual(c.runs("ヨアソビ 봄"), [(3, "ヨアソビ"), (0, " "), (2, "봄")])
        self.assertEqual(c.runs("Žluťoučký kůň"), [(0, "Žluťoučký kůň")])
        self.assertEqual(c.runs(""), [])
        self.assertEqual(self.kinds("font.missing"), [])

    def test_a_font_for_one_script_is_asked_only_for_that_script(self):
        # „Thai.ttf" má i hangul, ale zkouší se jen pro thajský blok
        c = self.chain(Thai=[(0x0E00, 0x0E7F), HANGUL], Other=[HANGUL])
        self.assertEqual(c.runs("เพลง 봄"), [(1, "เพลง"), (0, " "), (3, "봄")])

    def test_a_combining_mark_stays_with_its_letter(self):
        c = self.chain(Korean=[HANGUL, (0x0300, 0x036F)], Other=[(0x0E00, 0x0E7F)])
        # čárka (U+0301) je v základním písmu ne, v korejském ano: drží se hangulu
        self.assertEqual(c.runs("a봄́b"), [(0, "a"), (2, "봄́"), (0, "b")])
        # znak varianty (U+FE0F) a spojovač nemají vlastní glyf: úsek nedělí
        self.assertEqual(c.runs("봄️날"), [(2, "봄️날")])

    def test_bold_text_takes_the_bold_cut_when_there_is_one(self):
        c = self.chain(True, Korean=[HANGUL], KoreanBold=[HANGUL], Other=[(0x3040, 0x30FF)])
        self.assertEqual(Path(c.path(2)).name, "KoreanBold.ttf")
        self.assertEqual(Path(c.path(3)).name, "Other.ttf")  # tučný řez nemá
        self.assertEqual(Path(self.chain(False).path(2)).name, "Korean.ttf")
        self.assertIsNone(c.path(1))  # thajské písmo v systému není

    def test_a_missing_font_keeps_the_base_one_and_is_reported_once(self):
        c = self.chain(Korean=[(0x3040, 0x30FF)])
        for _ in range(5):
            self.assertEqual(c.runs(MIXED), [(0, MIXED)])
            self.assertEqual(c.runs("เพลง"), [(0, "เพลง")])
        missing = self.kinds("font.missing")
        self.assertEqual([(m["char"], m["package"]) for m in missing],
                         [("U+BC29", "fonts-noto-cjk"), ("U+0E40", "fonts-noto-core")])
        self.assertEqual(ff.package_hint(ord("夜")), "fonts-noto-cjk")
        self.assertEqual(ff.package_hint(ord("ह")), "fonts-noto-core")
        self.assertEqual(ff.package_hint(0x1F3B8), "fonts-symbola")


def ink(text: str, font, anchor: str = "ls", at=(20.0, 80.0), size=(1100, 140)) -> Image.Image:
    im = Image.new("L", size, 0)
    ImageDraw.Draw(im).text(at, text, font=font, fill=255, anchor=anchor)
    return im


class MeasureAndDraw(_Clean):
    """Měření a kreslení jdou přes tytéž úseky. Základní písmo se tu tváří, že
    umí jen ASCII; ostatní (háčky, řečtina, azbuka) kreslí DejaVu Serif."""

    TEXT = "Kino Группа крови — Ωμέγα žluťoučký (live)"
    SIZE = 40

    def setUp(self) -> None:
        super().setUp()
        for name in ("DejaVuSans.ttf", "DejaVuSerif.ttf"):
            if not (FONT_DIR / name).exists():
                self.skipTest(f"chybí {FONT_DIR / name}: bez něj není čím měřit")
        self.base = ImageFont.truetype(str(FONT_DIR / "DejaVuSans.ttf"), self.SIZE)
        self.serif = ImageFont.truetype(str(FONT_DIR / "DejaVuSerif.ttf"), self.SIZE)
        self.chain = Chain(str(FONT_DIR / "DejaVuSans.ttf"),
                           candidates=(Candidate("DejaVuSerif.ttf"),), dirs=(str(FONT_DIR),))
        self.chain.base = Coverage([(0x20, 0x7E)])
        self.font = FallbackFont(self.base, self.chain)

    def parts(self, text: str):
        return [(self.serif if slot else self.base, s) for slot, s in self.chain.runs(text)]

    def test_width_is_the_sum_of_the_runs_each_in_its_own_font(self):
        parts = self.parts(self.TEXT)
        self.assertGreater(len(parts), 4)
        self.assertEqual({f is self.serif for f, _ in parts}, {True, False})
        want = sum(f.getlength(s, direction="ltr" if ff._RAQM else None) for f, s in parts)
        self.assertAlmostEqual(self.font.getlength(self.TEXT), want, places=3)
        d = ImageDraw.Draw(Image.new("L", (10, 10)))
        self.assertAlmostEqual(d.textlength(self.TEXT, font=self.font), want, places=3)
        # a není to šířka, jakou by naměřilo samotné základní písmo
        self.assertNotAlmostEqual(want, self.base.getlength(self.TEXT), places=0)

    def test_what_is_drawn_is_what_was_measured(self):
        got = ink(self.TEXT, self.font)
        want = Image.new("L", got.size, 0)
        d, x = ImageDraw.Draw(want), 20.0
        for f, s in self.parts(self.TEXT):
            d.text((x, 80), s, font=f, fill=255, anchor="ls",
                   direction="ltr" if ff._RAQM else None)
            x += f.getlength(s, direction="ltr" if ff._RAQM else None)
        self.assertIsNone(ImageChops.difference(got, want).getbbox(),
                          "úseky nejsou nakreslené tam, kde byly změřené")
        box = got.getbbox()
        self.assertIsNotNone(box)
        self.assertAlmostEqual(box[2], 20 + self.font.getlength(self.TEXT), delta=3)
        self.assertAlmostEqual(box[0], 20, delta=3)

    def test_anchors_put_the_text_where_plain_text_would_be(self):
        d = ImageDraw.Draw(Image.new("L", (10, 10)))
        width = self.font.getlength(self.TEXT)
        base_la = ink(self.TEXT, self.font, "la").getbbox()
        for anchor, dx in (("la", 0), ("ma", width / 2), ("ra", width)):
            box = ink(self.TEXT, self.font, anchor, at=(20 + dx, 80)).getbbox()
            for got, want in zip(box, base_la):
                self.assertAlmostEqual(got, want, delta=1, msg=anchor)
        # účaří je účaří základního písma, ať je kotva jakákoli
        for v in "amsd":
            want = self.base.getbbox("x", anchor="l" + v)[1] - self.base.getbbox("x", anchor="ls")[1]
            got = ink(self.TEXT, self.font, "l" + v).getbbox()[1] - ink(self.TEXT, self.font).getbbox()[1]
            self.assertEqual(got, want, v)
        # obrys, který písmo hlásí, je obrys toho, co se nakreslí
        for anchor in ("la", "lm", "ls", "rm", "mm", "lt", "lb"):
            said = d.textbbox((400, 80), self.TEXT, font=self.font, anchor=anchor)
            drawn = ink(self.TEXT, self.font, anchor, at=(400, 80), size=(1400, 160)).getbbox()
            self.assertGreaterEqual(drawn[0], said[0] - 1, anchor)
            self.assertLessEqual(drawn[2], said[2] + 1, anchor)
            self.assertGreaterEqual(drawn[1], said[1] - 1, anchor)
            self.assertLessEqual(drawn[3], said[3] + 1, anchor)

    def test_ellipsis_and_wrapping_fit_with_the_fallback(self):
        for max_w in (150, 333, 500):
            out = ellipsize(self.TEXT, self.font, max_w)
            self.assertTrue(out.endswith("…"))
            self.assertLessEqual(self.font.getlength(out), max_w)
            self.assertLessEqual(ink(out, self.font).getbbox()[2], 20 + max_w + 2)
            # o znak delší začátek by se už nevešel
            k = len(out)
            while self.TEXT[:k].rstrip() == out[:-1]:
                k += 1
            self.assertGreater(self.font.getlength(self.TEXT[:k].rstrip() + "…"), max_w)
        lines = wrap(self.TEXT, self.font, 400, 3)
        self.assertGreater(len(lines), 1)
        self.assertEqual(" ".join(lines), self.TEXT)
        for line in lines:
            self.assertLessEqual(self.font.getlength(line), 400)
            self.assertLessEqual(ink(line, self.font).getbbox()[2], 20 + 400 + 2)

    def test_text_the_base_font_has_is_drawn_exactly_as_before(self):
        font = FallbackFont(self.base, Chain(str(FONT_DIR / "DejaVuSans.ttf"),
                                             candidates=(), dirs=(str(self.tmp),)))
        for text in ("Dej mi jen pár minut", "Žluťoučký kůň — „úpěl“ ďábelské ódy…", "Кино · Ελληνικά"):
            self.assertIsNone(font.runs(text))
            self.assertEqual(font.getlength(text), self.base.getlength(text))
            for anchor in ("la", "lm", "rm", "mm"):
                self.assertEqual(font.getbbox(text, anchor=anchor),
                                 self.base.getbbox(text, anchor=anchor))
                self.assertIsNone(ImageChops.difference(ink(text, font, anchor, at=(550, 80)),
                                                        ink(text, self.base, anchor, at=(550, 80))
                                                        ).getbbox())
        self.assertEqual(font.size, self.SIZE)
        self.assertEqual(font.getmetrics(), self.base.getmetrics())

    def test_only_a_few_fallback_fonts_are_open_at_once(self):
        opened = [self.chain.face(1, size) for size in range(10, 10 + 3 * ff.MAX_FACES)]
        self.assertTrue(all(f is not None for f in opened))
        self.assertEqual(len(ff._faces), ff.MAX_FACES)
        self.assertIs(self.chain.face(1, opened[-1].size), opened[-1])  # poslední zůstává
        # první použití náhradního písma jde do telemetrie jednou, ne za každou velikost
        self.assertEqual([e["file"] for e in self.kinds("font.fallback")], ["DejaVuSerif.ttf"])


class Missing(_Clean):
    """Náhradní písma v systému nejsou (prázdný adresář): kreslí se jako dřív."""

    def setUp(self) -> None:
        super().setUp()
        self.enterContext(mock.patch.dict(os.environ, {"YTDJ_FONT_DIRS": str(self.tmp)}))

    def test_the_tv_still_draws_and_says_once_what_is_missing(self):
        v = view_from(KOREAN, now=NOW, mono=100.0, got_at=100.0, address="jukebox.local")
        r = Renderer((1280, 720))
        for _ in range(3):
            r.render(v, full=True)
        self.assertIsNotNone(r.frame.getbbox())
        missing = self.kinds("font.missing")
        self.assertEqual([m["package"] for m in missing], ["fonts-noto-cjk"])
        self.assertTrue(missing[0]["char"].startswith("U+"))
        self.assertEqual(self.kinds("font.fallback"), [])
        # měří i kreslí jen základní písmo — stejně jako před opravou
        f = r.font(56, True)
        self.assertEqual(f.getlength(MIXED), f.base.getlength(MIXED))
        self.assertEqual(f.getlength("a 🎸 b"), f.base.getlength("a 🎸 b"))

    def test_the_panel_still_draws(self):
        r = PanelRenderer()
        r.render(View(online=True, connecting=False, has_track=True, running=True,
                      title="봄날 (Spring Day)", artist="BTS (방탄소년단)", now_who="지민",
                      duration=214, elapsed=10, volume=50))
        self.assertIsNotNone(r.frame.getbbox())
        self.assertEqual([m["package"] for m in self.kinds("font.missing")], ["fonts-noto-cjk"])

    def test_without_dejavu_nothing_is_wrapped(self):
        bitmap = ImageFont.load_default()
        self.assertIs(ff.with_fallback(bitmap), bitmap)


class RealScripts(_Clean):
    """Skutečná písma ze systému. Co v systému není, se přeskočí a řekne se co."""

    def font(self, bold: bool) -> FallbackFont:
        f = Renderer((1280, 720)).font(56, bold)
        if not isinstance(f, FallbackFont):
            self.skipTest(f"chybí DejaVu v {FONT_DIR}")
        return f

    def test_tv_and_panel_fonts_fall_back(self):
        self.assertIsInstance(self.font(True), FallbackFont)
        fonts = Fonts()
        for name in ("title", "artist", "status", "chip", "hint"):
            self.assertIsInstance(getattr(fonts, name), FallbackFont, name)

    def test_every_script_is_drawn_with_real_glyphs(self):
        checked = []
        for bold in (False, True):
            f = self.font(bold)
            for script, sample in SCRIPTS.items():
                with self.subTest(script=script, bold=bold):
                    lacking = sorted({ch for ch in sample if not ch.isspace()
                                      and not f.chain.covers(f.chain.slot(ch), ch)})
                    if lacking:
                        packages = sorted({ff.package_hint(ord(ch)) for ch in lacking})
                        self.skipTest(f"{script}: v systému chybí písmo ({', '.join(packages)})")
                    mixed = f"Jukebox · {sample} (live)"
                    runs = f.chain.runs(mixed)
                    self.assertEqual("".join(s for _, s in runs), mixed)
                    width = f.getlength(mixed)
                    box = ink(mixed, f, size=(1600, 140)).getbbox()
                    self.assertIsNotNone(box)
                    self.assertAlmostEqual(box[2], 20 + width, delta=f.size * 0.2)
                    if any(slot for slot, _ in runs):
                        # náhradní písmo kreslí něco jiného než obdélníčky základního
                        self.assertIsNotNone(ImageChops.difference(
                            ink(mixed, f, size=(1600, 140)),
                            ink(mixed, f.base, size=(1600, 140))).getbbox())
                    short = ellipsize(mixed, f, width * 0.6)
                    self.assertLessEqual(f.getlength(short), width * 0.6)
                    checked.append(script)
        self.assertTrue(checked, "žádné písmo se neověřilo")

    def test_korean_title_on_the_tv_is_not_boxes(self):
        f = self.font(True)
        if any(not f.chain.covers(f.chain.slot(ch), ch) for ch in "봄날방탄소년단지민"):
            self.skipTest("v systému chybí korejské písmo (fonts-noto-cjk)")
        v = view_from(KOREAN, now=NOW, mono=100.0, got_at=100.0, address="jukebox.local")
        for size in SIZES:
            with self.subTest(size=size):
                real = Renderer(size)
                real.render(v, full=True)
                with mock.patch.dict(os.environ, {"YTDJ_FONT_DIRS": str(self.tmp)}):
                    ff.reset()
                    boxes = Renderer(size)
                    boxes.render(v, full=True)
                ff.reset()
                diff = ImageChops.difference(real.frame, boxes.frame).getbbox()
                self.assertIsNotNone(diff, "korejský název vypadá stejně jako bez písma")
                # nic nepřeteklo přes bezpečný okraj (ořez telky)
                w, h = size
                dx, dy = round(w * 0.018) + 1, round(h * 0.015) + 1
                keep = (real.mx - dx, real.my - dy, w - real.mx + dx, h - real.my + dy)
                masked = real.frame.copy()
                masked.paste((10, 11, 14), keep)
                self.assertIsNone(ImageChops.difference(
                    masked, Image.new("RGB", size, (10, 11, 14))).getbbox())
        files = {e["file"] for e in self.kinds("font.fallback")}
        self.assertTrue(any(name.startswith(("NotoSansCJK", "DroidSansFallback")) for name in files),
                        files)

    def test_the_text_region_of_a_tv_view_is_unchanged_without_foreign_text(self):
        v = view_from(WISH, now=NOW, mono=100.0, got_at=100.0, address="jukebox.local")
        r = Renderer((1280, 720))
        r.render(v, full=True)
        self.assertEqual(self.kinds("font.fallback"), [])
        self.assertEqual(self.kinds("font.missing"), [])
        self.assertEqual(ff._faces, {})


class Install(unittest.TestCase):
    def test_install_script_adds_the_fonts_where_a_screen_is_installed(self):
        text = (ROOT / "packaging" / "install-service.sh").read_text(encoding="utf-8")
        block = text[text.index("# Písma pro jiné abecedy"):text.index("# Web i na běžném portu 80")]
        self.assertIn('[ "$panel" = yes ] || [ "$tv" = yes ]', block)
        self.assertIn("YTDJ_FONTS", block)
        self.assertIn("--no-install-recommends", block)
        packages = {c.package for c in ff.CANDIDATES}
        for package in ("fonts-noto-cjk", "fonts-noto-core", "fonts-symbola"):
            self.assertIn(package, block)
            self.assertIn(package, packages)
        # soubory, podle kterých skript pozná, že balíček už je, zná i kreslení
        names = {c.regular for c in ff.CANDIDATES}
        for name in ("NotoSansCJK-Regular.ttc", "NotoSansThai-Regular.ttf", "Symbola_hint.ttf"):
            self.assertIn(name, block)
            self.assertIn(name, names)
        notes = (ROOT / "packaging" / "rpi" / "NOTES.md").read_text(encoding="utf-8")
        self.assertIn("fonts-noto-cjk fonts-noto-core fonts-symbola", notes)
        self.assertIn(f"at most {ff.MAX_FACES} at a time", notes)


if __name__ == "__main__":
    unittest.main()
