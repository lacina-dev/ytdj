"""Obrazovka „právě hraje“ na telce přes HDMI (POZADAVKY #58, etapa 1).

Jako testy panelu se pouští systémovým pythonem (Pillow není ve venvu):

    YTDJ_EVENTS_FILE=/tmp/x.jsonl python3 -m unittest tests.test_panel_tv
"""

from __future__ import annotations

import configparser
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if not os.environ.get("YTDJ_EVENTS_FILE"):
    _EVENTS_TMP = tempfile.TemporaryDirectory(prefix="ytdj-tv-tests-")
    os.environ["YTDJ_EVENTS_FILE"] = str(Path(_EVENTS_TMP.name) / "events.jsonl")

from fake_ytdj import make_server  # noqa: E402
from PIL import Image  # noqa: E402

from ytdj.tv import app as tvapp  # noqa: E402
from ytdj.tv import console as tvconsole  # noqa: E402
from ytdj.tv import screen as tvscreen  # noqa: E402
from ytdj.tv.__main__ import default_address  # noqa: E402
from ytdj.tv.fb import FbError, FbInfo, Framebuffer, PngScreen, pack  # noqa: E402
from ytdj.tv.screen import BG, Renderer, TvView, view_from  # noqa: E402
from tv_shots import NOW, SIZES, STATES, WISH  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="ytdj-tv-test-"))
RGB565 = dict(bpp=16, red=(11, 5), green=(5, 6), blue=(0, 5))
XRGB = dict(bpp=32, red=(16, 8), green=(8, 8), blue=(0, 8))


def info(w: int, h: int, stride: int | None = None, **fmt) -> FbInfo:
    fmt = fmt or RGB565
    return FbInfo(w, h, stride=stride or w * fmt["bpp"] // 8, **fmt)


def view(name: str, **kw) -> TvView:
    spec = STATES[name]
    return view_from(spec.get("state"), offline=spec.get("offline", ""), now=NOW, mono=100.0,
                     got_at=100.0, address="jukebox.local:8765", **kw)


def fake_fb(i: FbInfo) -> Path:
    path = Path(tempfile.mkstemp(dir=_TMP)[1])
    path.write_bytes(b"\xaa" * (i.stride * i.height))
    return path


class Pixels(unittest.TestCase):
    def test_packs_the_formats_a_pi_framebuffer_uses(self):
        im = Image.new("RGB", (4, 1))
        for x, c in enumerate(((255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255))):
            im.putpixel((x, 0), c)
        # RGB565 little-endian: červená F800, zelená 07E0, modrá 001F
        self.assertEqual(pack(im, "RGB565").hex(), "00f8e0071f00ffff")
        self.assertEqual(pack(im, "BGR565").hex(), "1f00e00700f8ffff")
        self.assertEqual(pack(im, "BGRX")[:8].hex(), "0000ff0000ff0000")
        self.assertEqual(pack(im, "RGBX")[:4].hex(), "ff0000ff")
        self.assertEqual(pack(im, "BGR")[:6].hex(), "0000ff00ff00")
        # mezistupně: 5/6/5 bitů shora
        px = Image.new("RGB", (1, 1), (0x8F, 0x47, 0x1B))
        self.assertEqual(int.from_bytes(pack(px, "RGB565"), "little"),
                         (0x8F >> 3) << 11 | (0x47 >> 2) << 5 | (0x1B >> 3))
        with self.assertRaises(FbError):
            pack(im, "CMYK")

    def test_format_is_recognised_or_the_screen_is_unusable(self):
        self.assertEqual(info(720, 480).fmt, "RGB565")
        self.assertEqual(info(720, 480, bpp=16, red=(0, 5), green=(5, 6), blue=(11, 5)).fmt,
                         "BGR565")
        self.assertEqual(info(1920, 1080, **XRGB).fmt, "BGRX")
        self.assertEqual(info(1920, 1080, bpp=32, red=(0, 8), green=(8, 8), blue=(16, 8)).fmt,
                         "RGBX")
        self.assertEqual(info(800, 600, bpp=24, red=(16, 8), green=(8, 8), blue=(0, 8)).fmt, "BGR")
        self.assertEqual(info(720, 480).usable(), "")
        # paleta, 15 bitů, prťavý nebo nesmyslný framebuffer: nekreslit
        self.assertIn("formát", info(720, 480, bpp=8, red=(0, 8), green=(0, 8), blue=(0, 8)).usable())
        self.assertIn("formát", info(720, 480, bpp=16, red=(10, 5), green=(5, 5), blue=(0, 5)).usable())
        self.assertIn("malý", info(160, 120).usable())
        self.assertIn("kratší", info(720, 480, stride=100).usable())

    def test_draws_regions_to_the_right_bytes_any_size_and_depth(self):
        for i in (info(360, 240), info(360, 240, stride=752), info(480, 270, **XRGB),
                  info(400, 240, stride=1216, bpp=24, red=(16, 8), green=(8, 8), blue=(0, 8))):
            path = fake_fb(i)
            fb = Framebuffer(path, info=i)
            self.assertEqual(fb.size, (i.width, i.height))
            bpp = i.bpp // 8
            img = Image.new("RGB", fb.size, (255, 0, 0))
            fb.show(img)  # celý snímek
            raw = path.read_bytes()
            red = pack(Image.new("RGB", (1, 1), (255, 0, 0)), i.fmt)
            self.assertEqual(raw[:bpp], red)
            last = (i.height - 1) * i.stride + (i.width - 1) * bpp
            self.assertEqual(raw[last:last + bpp], red)
            if i.stride > i.width * bpp:  # výplň řádku zůstala, jak byla
                self.assertEqual(raw[i.width * bpp:i.stride], b"\xaa" * (i.stride - i.width * bpp))
            # výřez: jen jeho body, zbytek beze změny
            img.paste((0, 0, 255), (10, 20, 30, 25))
            before = path.read_bytes()
            fb.show(img, (10, 20, 30, 25))
            after = path.read_bytes()
            blue = pack(Image.new("RGB", (1, 1), (0, 0, 255)), i.fmt)
            changed = [(x, y) for y in range(i.height) for x in range(i.width)
                       if after[y * i.stride + x * bpp:][:bpp] != before[y * i.stride + x * bpp:][:bpp]]
            self.assertEqual(changed, [(x, y) for y in range(20, 25) for x in range(10, 30)], i)
            self.assertTrue(all(after[y * i.stride + x * bpp:][:bpp] == blue for x, y in changed))
            if i.stride > i.width * bpp:
                self.assertEqual(after[20 * i.stride + i.width * bpp:21 * i.stride],
                                 b"\xaa" * (i.stride - i.width * bpp))
            fb.show(img, (700000, 5, 700010, 9))  # mimo obraz: nic, žádná chyba
            fb.close()

    def test_missing_tiny_or_unknown_framebuffer_is_refused_not_drawn(self):
        with self.assertRaises(FbError):
            Framebuffer(_TMP / "neni-fb0")
        tiny = info(160, 120)
        with self.assertRaises(FbError):
            Framebuffer(fake_fb(tiny), info=tiny)
        with self.assertRaises(FbError):  # obyčejný soubor není framebuffer (ioctl selže)
            Framebuffer(fake_fb(info(720, 480)))


class Views(unittest.TestCase):
    def test_wish_shows_who_what_next_and_the_djs_note(self):
        v = view("playing-wish")
        self.assertEqual((v.state, v.title, v.artist), ("playing", "Dej mi jen pár minut", "Ivan Hlas"))
        self.assertEqual((v.who, v.wish), ("Kolega", "něco od Ivana Hlase"))
        self.assertEqual([(n.title, n.who) for n in v.next],
                         [("Jasná zpráva", "Jana"), ("Dej mi víc své lásky", ""),
                          ("Okno mé lásky", "")])
        self.assertEqual(v.more, 2)
        self.assertIn("Zařazuju Ivana Hlase", v.note)
        self.assertEqual((v.clock, v.address), ("14:37", "jukebox.local:8765"))
        self.assertEqual((v.position, v.duration), (80, 214))  # 83 s po krocích 5 s

    def test_background_says_where_the_radio_comes_from(self):
        v = view("playing-background")
        self.assertEqual((v.who, v.origin), ("", "rádio podle přání Kolega"))
        self.assertIn("DJ vybírá", v.note)
        cur = {**WISH["current"], "reason": {"kind": "start"}}
        self.assertEqual(view_from({**WISH, "current": cur}, now=NOW).origin,
                         "rádio podle času a dne")
        cur = {**WISH["current"], "reason": {"kind": "radio", "who": ""}}
        self.assertEqual(view_from({**WISH, "current": cur}, now=NOW).origin, "rádio · český rock")

    def test_states(self):
        self.assertEqual(view("paused").state, "paused")
        self.assertEqual(view("nothing-playing").state, "idle")
        self.assertEqual(view("starting").state, "starting")
        self.assertEqual((view("offline").state, view("offline").detail),
                         ("offline", "spojení odmítnuto"))
        self.assertEqual((view("outage").state, view("outage").detail),
                         ("outage", "YouTube neodpovídá"))
        self.assertEqual(view_from({**WISH, "buffering": True}, now=NOW).state, "loading")
        self.assertEqual(view_from(None, now=NOW).state, "offline")

    def test_position_moves_by_itself_in_steps_and_stops_when_paused(self):
        at = lambda st, mono: view_from(st, now=NOW, mono=mono, got_at=100.0).position  # noqa: E731
        self.assertEqual(at(WISH, 100.0), 80)
        self.assertEqual(at(WISH, 101.9), 80)  # tentýž krok → nic se nekreslí
        self.assertEqual(at(WISH, 102.1), 85)
        self.assertEqual(at(WISH, 400.0), 210)  # nepřeteče délku skladby (214 s)
        self.assertEqual(at({**WISH, "paused": True}, 160.0), 80)
        self.assertEqual(at({**WISH, "buffering": True}, 160.0), 80)

    def test_old_note_disappears_and_idle_screen_dims(self):
        old = {**WISH, "dj": {"last": {"reply": "Dávno.", "at": NOW - 3600}}}
        self.assertEqual(view_from(old, now=NOW).note, "")
        self.assertFalse(view("nothing-playing", idle_since=90.0).dim)
        self.assertTrue(view("nothing-playing", idle_since=100.0 - tvscreen.DIM_AFTER).dim)
        self.assertFalse(view("playing-wish").dim)

    def test_nonsense_from_the_server_never_crashes(self):
        for junk in ({}, {"current": 5, "queue": "x", "dj": []},
                     {"current": {"id": None, "title": None, "reason": "?"}, "queue": [1, None, {}],
                      "position": "abc", "duration": [], "dj": {"last": {"reply": 7, "at": "x"}},
                      "outage": "ano"},
                     {"current": {"title": "T" * 5000, "artist": "\n\t  "}, "queue": [{}] * 500}):
            v = view_from(junk, now=NOW)
            for size in ((720, 480), (1920, 1080)):
                Renderer(size).render(v, full=True)
        self.assertEqual(view_from({"current": {"title": "T" * 5000}}, now=NOW).title, "T" * 200)


class Drawing(unittest.TestCase):
    def _outside(self, frame: Image.Image, box) -> bool:
        """Je mimo `box` jen pozadí?"""
        blank = Image.new("RGB", frame.size, BG)
        masked = frame.copy()
        masked.paste(BG, box)
        from PIL import ImageChops
        return ImageChops.difference(masked, blank).getbbox() is None

    def test_every_state_fits_every_size_inside_the_safe_area(self):
        """Nic nepřeteče: okraje (overscan telky) zůstanou prázdné i s dlouhými
        názvy a v každé poloze proti vypálení."""
        for w, h in (*SIZES, (800, 480), (3840, 2160)):
            r = Renderer((w, h))
            safe = (round(w * 0.03), round(h * 0.025), w - round(w * 0.03), h - round(h * 0.025))
            for name in (STATES if w < 3000 else ("long-titles",)):
                for shift in (0, 2, 6):  # uprostřed a oba krajní rohy
                    v = view(name)
                    v = TvView(**{**v.__dict__, "shift": shift})
                    r.render(v, full=True)
                    self.assertTrue(self._outside(r.frame, safe), (w, h, name, shift))
                    self.assertGreater(len(set(r.frame.resize((64, 36)).getdata())), 3,
                                       (w, h, name))  # něco se nakreslilo

    def test_long_titles_stay_in_their_own_region(self):
        for w, h in SIZES:
            r = Renderer((w, h))
            r.render(view("nothing-playing"), full=True)
            base = r.frame.copy()
            boxes = r.render(view("long-titles"))
            regions = {x.name: x.box for x in r.regions}
            for b in boxes:
                inside = [n for n, g in regions.items()
                          if g[0] <= b[0] and g[1] <= b[1] and b[2] <= g[2] and b[3] <= g[3]]
                self.assertEqual(len(inside), 1, (w, h, b))
            # hlavička se nezměnila (stejný stav hodin) — dlouhý název do ní nepřetekl
            head = regions["head"]
            self.assertEqual(r.frame.crop((head[0], head[1], head[2] - 300, head[3] - 4)).tobytes()
                             != base.crop((head[0], head[1], head[2] - 300, head[3] - 4)).tobytes(),
                             True)  # jen štítek stavu (TICHO → HRAJE)
            # tři řádky názvu nejvýš a zkrácení výpustkou
            lines, _f = r._fit_title(view("long-titles").title, 500 * r.u, 150 * r.u)
            self.assertLessEqual(len(lines), 3)
            self.assertTrue(lines[-1].endswith("…"))

    def test_only_what_changed_is_pushed(self):
        for w, h in SIZES:
            r = Renderer((w, h))
            self.assertEqual(r.render(view("playing-wish"), full=True), [(0, 0, w, h)])
            self.assertEqual(r.render(view("playing-wish")), [])  # beze změny nic
            # krok průběhu: kousek pruhu a čas, zlomek obrazovky
            v = view_from({**WISH, "position": 88.0}, now=NOW, mono=100.0, got_at=100.0,
                          address="jukebox.local:8765")
            boxes = r.render(v)
            bar = next(x.box for x in r.regions if x.name == "bar")
            self.assertTrue(boxes)
            for b in boxes:
                self.assertTrue(bar[0] <= b[0] and bar[1] <= b[1] and b[2] <= bar[2] and b[3] <= bar[3])
            px = sum((b[2] - b[0]) * (b[3] - b[1]) for b in boxes)
            self.assertLess(px, w * h * 0.03, (w, h, px))
            # minuta na hodinách: jen roh hlavičky
            boxes = r.render(TvView(**{**v.__dict__, "clock": "14:38"}))
            self.assertEqual(len(boxes), 1)
            self.assertLess((boxes[0][2] - boxes[0][0]) * (boxes[0][3] - boxes[0][1]), w * h * 0.01)

    def test_burn_in_care_shifts_the_picture_and_dims_when_idle(self):
        r = Renderer((1280, 720))
        v = TvView(**{**view("playing-wish").__dict__, "shift": 0})
        r.render(v, full=True)
        a = r.frame.copy()
        moved = TvView(**{**v.__dict__, "shift": 2})
        self.assertEqual(r.render(moved), [(0, 0, 1280, 720)])  # posun = celý snímek
        self.assertNotEqual(a.tobytes(), r.frame.tobytes())
        # posun je malý: o pár bodů, ne o kus obrazovky
        self.assertLessEqual(r.step, 8)
        self.assertEqual(a.crop((200, 200, 400, 300)).tobytes(),
                         r.frame.crop((200 + r.step, 200 + r.step, 400 + r.step, 300 + r.step)).tobytes())
        # během dne se poloha střídá
        shifts = {view_from(None, now=NOW + n * tvscreen.SHIFT_EVERY).shift for n in range(20)}
        self.assertGreaterEqual(len(shifts), 5)
        # ztlumení v klidu
        bright = Renderer((1280, 720))
        bright.render(view("nothing-playing"), full=True)
        dim = Renderer((1280, 720))
        dim.render(view("nothing-playing", idle_since=-1e6), full=True)
        lum = lambda im: sum(im.convert("L").resize((64, 36)).getdata())  # noqa: E731
        self.assertLess(lum(dim.frame), lum(bright.frame) * 0.7)

    def test_dark_by_default_and_text_big_enough_to_read_from_afar(self):
        r = Renderer((1920, 1080))
        r.render(view("playing-wish"), full=True)
        px = list(r.frame.convert("L").resize((96, 54)).getdata())
        self.assertLess(sum(px) / len(px), 60)  # tmavé
        _lines, f = r._fit_title("Dej mi jen pár minut", 1200, 400)
        self.assertGreaterEqual(f.size, 80)  # název na 1080p aspoň ~80 bodů
        self.assertGreaterEqual(r.font(30).size, 40)  # „dál hraje“

    def test_cover_is_the_square_from_a_letterboxed_thumbnail(self):
        import io
        thumb = Image.new("RGB", (480, 360), (0, 0, 0))  # 4:3 s černými pruhy
        thumb.paste((90, 90, 30), (0, 45, 480, 315))  # obraz 16:9
        thumb.paste((200, 30, 30), (105, 45, 375, 315))  # čtvercový obal uprostřed
        buf = io.BytesIO()
        thumb.save(buf, "JPEG", quality=95)
        tile = tvscreen.cover_tile(buf.getvalue(), 200)
        self.assertEqual(tile.size, (200, 200))
        for xy in ((5, 5), (195, 5), (5, 195), (100, 100)):
            r_, g_, b_ = tile.getpixel(xy)
            self.assertTrue(r_ > 150 and g_ < 80, (xy, r_, g_, b_))  # žádný černý pruh


class Running(unittest.TestCase):
    def setUp(self):
        self.server, self.fake = make_server(0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.out = _TMP / f"tv-{self.id().rsplit('.', 1)[-1]}.png"
        self.apps: list[tvapp.TvApp] = []

    def tearDown(self):
        for a in self.apps:
            a.shutdown()
        self.server.shutdown()
        self.server.server_close()

    def start(self, open_screen=None, size=(720, 480)) -> tvapp.TvApp:
        screens: list[PngScreen] = []

        def default():
            screens.append(PngScreen(self.out, size))
            return screens[-1]

        a = tvapp.TvApp(open_screen or default, f"http://127.0.0.1:{self.port}",
                        "jukebox.local:8765", art_url="")
        a.screens = screens
        self.apps.append(a)
        threading.Thread(target=a.run, daemon=True).start()
        return a

    def until(self, cond, timeout=5.0):
        end = time.monotonic() + timeout
        while not cond():
            if time.monotonic() > end:
                self.fail("podmínka nenastala")
            time.sleep(0.02)

    def test_follows_the_jukebox_and_redraws_only_on_change(self):
        a = self.start()
        self.until(lambda: a.view().state == "playing" and a.screens and a.screens[0].calls)
        self.assertEqual(a.view().title, self.fake.snapshot()["current"]["title"])
        self.assertEqual(a.api.agent, "ytdj-tv")
        time.sleep(0.3)
        calls, px = a.screens[0].calls, a.screens[0].pushed_px
        time.sleep(1.6)  # server posílá pozici každou chvíli — to se nekreslí
        self.assertLessEqual(a.screens[0].calls - calls, 2)
        self.assertLess(a.screens[0].pushed_px - px, 720 * 480 * 0.1)
        # další skladba → překreslí se
        title = a.view().title
        self.fake.control({"action": "next"})
        self.until(lambda: a.view().title != title)
        self.until(lambda: a.screens[0].calls > calls)

    def test_jukebox_gone_is_said_and_it_reconnects(self):
        a = self.start()
        self.until(lambda: a.view().state == "playing")
        self.server.closing = True  # i otevřený proud událostí skončí
        self.server.shutdown()
        self.server.server_close()
        self.until(lambda: a.view().state == "offline", timeout=10)
        self.server, self.fake = make_server(self.port)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.until(lambda: a.view().state == "playing", timeout=10)

    def test_no_usable_screen_idles_cheaply_and_retries(self):
        tries: list[float] = []
        ok = threading.Event()
        made: list[PngScreen] = []

        def open_screen():
            tries.append(time.monotonic())
            if not ok.is_set():
                raise FbError("/dev/fb0: moc malý (160×120)")
            made.append(PngScreen(self.out, (1280, 720)))
            return made[-1]

        with mock.patch.object(tvapp, "RETRY_SCREEN", 0.15):
            a = self.start(open_screen)
            self.until(lambda: len(tries) >= 3)
            self.assertFalse(made)
            self.assertLess(len(tries), 12)  # zkouší zřídka, ne ve smyčce
            ok.set()
            self.until(lambda: made and made[0].calls)
            self.assertEqual(a.renderer.size, (1280, 720))
        self.assertGreaterEqual(tvapp.RETRY_SCREEN, 30)  # naostro jednou za minutu

    def test_broken_screen_does_not_kill_the_process(self):
        class Flaky(PngScreen):
            fail = True

            def show(self, img, box=None):
                if Flaky.fail:
                    Flaky.fail = False
                    raise OSError("framebuffer zmizel")
                return super().show(img, box)

        made: list[Flaky] = []

        def open_screen():
            made.append(Flaky(self.out, (720, 480)))
            return made[-1]

        with mock.patch.object(tvapp.TvApp, "_ensure_screen", tvapp.TvApp._ensure_screen):
            a = self.start(open_screen)
            self.until(lambda: len(made) >= 2 and made[-1].calls, timeout=10)
            self.assertEqual(a.view().state, "playing")


class OnlyWatches(unittest.TestCase):
    def test_never_controls_the_jukebox_or_the_player(self):
        """Jen čte stav: žádné povely, žádné přání, žádné mpv ani zvuk."""
        src = "".join(p.read_text(encoding="utf-8") for p in (ROOT / "ytdj" / "tv").glob("*.py"))
        for banned in ("Commander", ".control(", ".prompt(", "POST", "mpv", "subprocess",
                       "/dev/mem", "import numpy", "import socket"):
            self.assertNotIn(banned, src, banned)
        self.assertEqual(default_address({}), "jukebox.local:8765")
        self.assertEqual(default_address({"web_port": 80}), "jukebox.local")
        self.assertEqual(default_address({"web_port": "x"}), "jukebox.local:8765")


class OnlyTheJukebox(unittest.TestCase):
    """Na telce je jen obrazovka jukeboxu: žádný login, kurzor ani hlášky konzole."""

    def test_console_is_switched_to_graphics_mode_and_back(self):
        calls: list[tuple] = []
        with mock.patch.object(tvconsole.os, "isatty", return_value=True), \
                mock.patch.object(tvconsole.fcntl, "ioctl", lambda *a: calls.append(a)):
            self.assertEqual(tvconsole.grab(7), (True, ""))
            tvconsole.release(7)
        self.assertEqual(calls, [(7, 0x4B3A, 1), (7, 0x4B3A, 0)])  # KDSETMODE: grafika, text

    def test_without_its_own_terminal_nothing_happens_and_nothing_breaks(self):
        with open(os.devnull) as fh:  # spuštěno ručně / v testu: není to terminál
            done, why = tvconsole.grab(fh.fileno())
        self.assertFalse(done)
        self.assertTrue(why)

        def refuse(*_a):
            raise OSError(25, "Inappropriate ioctl for device")  # ssh: pseudoterminál

        with mock.patch.object(tvconsole.os, "isatty", return_value=True), \
                mock.patch.object(tvconsole.fcntl, "ioctl", refuse):
            self.assertEqual(tvconsole.grab(0), (False, "Inappropriate ioctl for device"))
            tvconsole.release(0)  # nevyhodí

    def test_simulated_screen_never_touches_a_terminal(self):
        src = (ROOT / "ytdj" / "tv" / "__main__.py").read_text(encoding="utf-8")
        self.assertIn("if not (args.sim_out or args.sim_fb):", src)
        self.assertIn("console.release()", src)

    def test_unit_takes_tty1_without_root_and_login_prompt_is_off(self):
        raw = (ROOT / "packaging" / "ytdj-tv.service").read_text(encoding="utf-8")
        unit = configparser.ConfigParser(strict=False, interpolation=None)
        unit.read_string(raw)
        s = unit["Service"]
        self.assertEqual((s["TTYPath"], s["StandardInput"]), ("/dev/tty1", "tty-force"))
        # výstup do logu, ne na telku
        self.assertEqual((s["StandardOutput"], s["StandardError"]), ("journal", "journal"))
        self.assertEqual(s["TTYReset"], "yes")
        self.assertEqual(unit["Unit"]["Conflicts"], "getty@tty1.service")
        self.assertNotIn("ExecStartPre=+", raw)  # žádný krok pod rootem
        self.assertNotIn("User=root", raw)
        text = (ROOT / "packaging" / "install-service.sh").read_text(encoding="utf-8")
        block = text[text.index("tv=no"):text.index("# Sandbox Codexu")]
        self.assertIn("systemctl disable --now getty@tty1.service", block)
        self.assertIn("systemctl enable --now getty@tty1", block)  # jak ji vrátit
        self.assertNotIn("cmdline.txt", block)  # start jádra se sám nemění
        notes = (ROOT / "packaging" / "rpi" / "NOTES.md").read_text(encoding="utf-8")
        for word in ("console=tty1", "vt.global_cursor_default=0", "getty@tty1"):
            self.assertIn(word, notes)


class Packaging(unittest.TestCase):
    def test_unit_is_least_privilege_and_goes_first_under_memory_pressure(self):
        unit = configparser.ConfigParser(strict=False, interpolation=None)
        unit.read(ROOT / "packaging" / "ytdj-tv.service", encoding="utf-8")
        s = unit["Service"]
        self.assertEqual(s["User"], "@USER@")  # obyčejný uživatel, ne root
        self.assertEqual(s["SupplementaryGroups"], "video")
        self.assertEqual(s["NoNewPrivileges"], "yes")
        self.assertEqual(s["CapabilityBoundingSet"], "")
        self.assertEqual(s["DevicePolicy"], "closed")
        raw = (ROOT / "packaging" / "ytdj-tv.service").read_text(encoding="utf-8")
        allowed = [ln.split("=", 1)[1] for ln in raw.splitlines() if ln.startswith("DeviceAllow=")]
        self.assertEqual(allowed, ["/dev/fb0 rw", "/dev/tty1 rw"])  # nic dalšího
        self.assertEqual((s["ProtectSystem"], s["ProtectHome"]), ("strict", "read-only"))
        self.assertIn("cookies.txt", s["InaccessiblePaths"])
        self.assertIn("admin-pin", s["InaccessiblePaths"])
        self.assertGreaterEqual(int(s["OOMScoreAdjust"]), 500)
        self.assertGreater(int(s["Nice"]), 0)
        self.assertIn("-m ytdj.tv", s["ExecStart"])
        self.assertEqual(s["Restart"], "always")

    def test_installed_only_on_a_pi_and_config_txt_is_left_alone(self):
        text = (ROOT / "packaging" / "install-service.sh").read_text(encoding="utf-8")
        block = text[text.index("tv=no"):text.index("# Sandbox Codexu")]
        self.assertIn('"$model" == Raspberry\\ Pi*', block)
        self.assertIn("-e /dev/fb0", block)
        self.assertIn("YTDJ_TV", block)
        self.assertIn('-e "s|@USER@|$USER|g"', block)
        for word in ("config.txt", "bootcfg", "gpu_mem", "vc4", "hdmi_"):
            self.assertNotIn(word, block.replace("Do\n# config.txt se tu NESAHÁ", "")
                             .replace("hdmi_force_hotplug", ""), word)
        self.assertIn("systemctl enable ytdj-tv.service", text)
        # nasazení ji posílá s ostatním kódem
        deploy = (ROOT / "packaging" / "rpi" / "deploy.sh").read_text(encoding="utf-8")
        self.assertNotIn("--exclude ytdj/tv", deploy)


if __name__ == "__main__":
    unittest.main()
