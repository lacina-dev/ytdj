"""Panel tests: layout helpers, and the whole thing against a fake ytdj.

    python -m unittest tests.test_panel -v
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fake_ytdj import make_server  # noqa: E402

# Testy nesmí psát do skutečného ~/.local/share/ytdj/events.jsonl vývojáře.
if not os.environ.get("YTDJ_EVENTS_FILE"):
    _EVENTS_TMP = tempfile.TemporaryDirectory(prefix="ytdj-panel-tests-")
    os.environ["YTDJ_EVENTS_FILE"] = str(Path(_EVENTS_TMP.name) / "events.jsonl")

from ytdj import telemetry  # noqa: E402
from ytdj.panel import kedei  # noqa: E402
from ytdj.panel.app import PanelApp  # noqa: E402
from ytdj.panel.stats import PanelStats  # noqa: E402
from ytdj.panel.sim import SimScreen, SimTouch  # noqa: E402
from ytdj.panel.ui import (  # noqa: E402
    NEXT,
    PLAY,
    VOL,
    VOL_DOWN,
    VOL_UP,
    Fonts,
    Renderer,
    View,
    ellipsize,
    merge_boxes,
    volume_at,
    wrap,
)


def center(box):
    return (box[0] + box[2]) // 2, (box[1] + box[3]) // 2


class EventLog:
    """Provozní události do dočasného souboru (YTDJ_EVENTS_FILE) po dobu testu."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "events.jsonl"
        self._old = os.environ.get("YTDJ_EVENTS_FILE")
        os.environ["YTDJ_EVENTS_FILE"] = str(self.path)
        self._reset()

    @staticmethod
    def _reset():
        # telemetry si cestu pamatuje po prvním zápisu — přečíst ji znovu
        if hasattr(telemetry, "_path"):
            telemetry._path = None

    def close(self):
        if self._old is None:
            os.environ.pop("YTDJ_EVENTS_FILE", None)
        else:
            os.environ["YTDJ_EVENTS_FILE"] = self._old
        self._reset()
        self.tmp.cleanup()

    def text(self):
        flush = getattr(telemetry, "flush", None)  # zápis může běžet ve vlastním vlákně
        if callable(flush):
            flush()
        try:
            return self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def all(self, kind=None):
        out = [json.loads(line) for line in self.text().splitlines() if line.strip()]
        return [e for e in out if kind is None or e["kind"] == kind]

    def find(self, kind, **match):
        return [e for e in self.all(kind) if all(e.get(k) == v for k, v in match.items())]

    def wait(self, kind, timeout=8.0, **match):
        ok = wait_for(lambda: self.find(kind, **match), timeout=timeout)
        return self.find(kind, **match) if ok else []


def wait_for(pred, timeout=8.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return False


class TextTest(unittest.TestCase):
    def setUp(self):
        self.font = Fonts().title

    def test_ellipsize_czech(self):
        text = "Příliš žluťoučký kůň úpěl ďábelské ódy"
        out = ellipsize(text, self.font, 200)
        self.assertTrue(out.endswith("…"))
        self.assertLessEqual(self.font.getlength(out), 200)
        self.assertEqual(ellipsize("Kůň", self.font, 200), "Kůň")

    def test_wrap_two_lines(self):
        text = "Příliš žluťoučký kůň úpěl ďábelské ódy — živě ze Šťastného Žďáru"
        lines = wrap(text, self.font, 450, 2)
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertLessEqual(self.font.getlength(line), 450)
        self.assertTrue(lines[1].endswith("…"))

    def test_wrap_one_giant_word(self):
        lines = wrap("A" * 200, self.font, 300, 2)
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertLessEqual(self.font.getlength(line), 300)


class RenderTest(unittest.TestCase):
    def test_merge(self):
        self.assertEqual(merge_boxes([(0, 0, 10, 10), (5, 5, 20, 20)]), [(0, 0, 20, 20)])
        far = [(0, 0, 10, 10), (400, 300, 410, 310)]
        self.assertEqual(sorted(merge_boxes(far)), far)

    def test_tick_is_tiny(self):
        v = View(online=True, connecting=False, has_track=True, title="X", artist="Y",
                 running=True, elapsed=60, duration=240, volume=50, can_next=True)
        r = Renderer()
        self.assertEqual(r.render(v, full=True), [(0, 0, 480, 320)])
        self.assertEqual(r.render(v), [])  # nothing changed, nothing to push
        boxes = r.render(replace(v, elapsed=61))
        px = sum((b[2] - b[0]) * (b[3] - b[1]) for b in boxes)
        self.assertTrue(boxes)
        self.assertLess(px, 2500)

    def test_volume_mapping(self):
        x0 = VOL[0] + 90
        self.assertEqual(volume_at(0, 100), 0)
        self.assertEqual(volume_at(479, 100), 100)
        self.assertLess(volume_at(x0, 100), volume_at(x0 + 100, 100))


class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.server, self.fake = make_server(0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.screen = SimScreen(Path(self.tmp.name) / "panel.png")
        self.touch = SimTouch(io.StringIO(""))
        self.app = PanelApp(self.screen, self.touch, f"http://127.0.0.1:{self.port}")
        self.thread = threading.Thread(target=self.app.run, daemon=True)
        self.thread.start()
        self.assertTrue(wait_for(lambda: self.app.online), "panel se nepřipojil")

    def tearDown(self):
        self.app.shutdown()
        self.thread.join(3)
        self._stop_server()
        self.tmp.cleanup()

    def _stop_server(self):
        if self.server is not None:
            self.server.closing = True
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    def test_play_pause(self):
        self.touch.tap(*center(PLAY))
        self.assertTrue(wait_for(lambda: self.fake.paused))
        self.assertTrue(wait_for(lambda: not self.app._view().running))
        time.sleep(0.4)  # a second tap sooner than that counts as contact bounce
        self.touch.tap(*center(PLAY))
        self.assertTrue(wait_for(lambda: not self.fake.paused))

    def test_tap_off_button_does_nothing(self):
        x, y = center(PLAY)
        self.touch.feed("down", x, y)
        time.sleep(0.05)
        self.touch.feed("move", x, y + 200)  # slid away, then lifted
        time.sleep(0.05)
        self.touch.feed("up", x, y + 200)
        time.sleep(0.5)
        self.assertEqual(self.fake.controls, [])

    def test_next(self):
        before = self.fake.index
        self.touch.tap(*center(NEXT))
        self.assertTrue(wait_for(lambda: self.fake.index != before))

    def test_volume_step_and_drag(self):
        self.touch.tap(*center(VOL_UP))  # 65 → 70
        self.assertTrue(wait_for(lambda: self.fake.volume == 70))
        y = center(VOL)[1]
        self.touch.drag(VOL[0] + 90, y, VOL[2] - 30, y, steps=30, dt=0.02)
        final = volume_at(VOL[2] - 30, 100)
        self.assertTrue(wait_for(lambda: self.fake.volume == final))
        sent = [c for c in self.fake.controls if c[0] == "volume"]
        # 0.6 s of dragging at most ~4/s, plus the first and the final value
        self.assertLessEqual(len(sent), 6)
        self.assertTrue(wait_for(lambda: self.app.hold_volume is None, timeout=6))
        self.assertEqual(self.app._view().volume, final)

    def _hold(self, box, seconds):
        x, y = center(box)
        self.touch.feed("down", x, y)
        time.sleep(seconds)
        self.touch.feed("up", x, y)
        self.assertTrue(wait_for(lambda: self.app.pressed is None, step=0.005))
        return self.app._view().volume

    def _sent(self):
        return [c[1] for c in self.fake.controls if c[0] == "volume"]

    def test_volume_tap_is_one_step(self):
        self.touch.tap(*center(VOL_DOWN), hold=0.15)  # 65 → 60
        self.assertTrue(wait_for(lambda: self.fake.volume == 60))
        time.sleep(0.8)
        self.assertEqual(self._sent(), [60])
        self.assertEqual(self.app._view().volume, 60)

    def test_volume_hold_repeats(self):
        shown = self._hold(VOL_DOWN, 1.5)
        # 65 → 60 at 0.45 s, then every 0.15 s: several steps, all on the glass
        self.assertLessEqual(shown, 40)
        self.assertGreaterEqual(shown, 15)
        self.assertEqual(shown % 5, 0)
        self.assertTrue(wait_for(lambda: self.fake.volume == shown))
        time.sleep(0.5)
        self.assertEqual(self.app._view().volume, shown)  # no extra tap step on release
        sent = self._sent()
        self.assertEqual(sent[-1], shown)
        # ~1 s of repeating at most ~4 requests/s, plus the final value
        self.assertLessEqual(len(sent), 7)
        self.assertEqual(sent, sorted(sent, reverse=True))

    def test_volume_hold_clamps(self):
        self.assertEqual(self._hold(VOL_UP, 1.6), 100)
        self.assertTrue(wait_for(lambda: self.fake.volume == 100))
        self.assertTrue(all(v <= 100 for v in self._sent()))
        time.sleep(0.4)
        self.assertEqual(self._hold(VOL_DOWN, 2.8), 0)
        self.assertTrue(wait_for(lambda: self.fake.volume == 0))
        self.assertTrue(all(0 <= v <= 100 for v in self._sent()))

    def test_volume_slide_out_stops_repeat(self):
        x, y = center(VOL_UP)
        self.touch.feed("down", x, y)
        time.sleep(0.8)  # 65 → 70 → 75 (→ 80)
        self.touch.feed("move", x, y - 150)  # slid off the button, still touching
        time.sleep(0.1)
        stopped = self.app._view().volume
        self.assertGreater(stopped, 65)
        time.sleep(0.8)
        self.assertEqual(self.app._view().volume, stopped)
        self.touch.feed("up", x, y - 150)
        self.assertTrue(wait_for(lambda: self.fake.volume == stopped))
        time.sleep(0.4)
        self.assertEqual(self.app._view().volume, stopped)

    def test_volume_hold_survives_glitch(self):
        x, y = center(VOL_UP)
        self.touch.feed("down", x, y)
        time.sleep(0.55)  # one repeat: 70
        self.touch.feed("up", x, y)  # the resistive layer lets go for a moment
        time.sleep(0.05)
        self.touch.feed("down", x, y)
        time.sleep(0.4)  # carries on at the repeat pace, no fresh 0.45 s delay
        self.touch.feed("up", x, y)
        self.assertTrue(wait_for(lambda: self.app.pressed is None, step=0.005))
        shown = self.app._view().volume
        self.assertGreaterEqual(shown, 80)
        self.assertTrue(wait_for(lambda: self.fake.volume == shown))
        time.sleep(0.3)
        self.assertEqual(self.app._view().volume, shown)

    def test_offline_and_back(self):
        self._stop_server()
        self.assertTrue(wait_for(lambda: not self.app.online, timeout=10))
        self.assertFalse(self.app._view().online)
        self.server, self.fake = make_server(self.port)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.assertTrue(wait_for(lambda: self.app.online, timeout=10))


class PollingFallbackTest(unittest.TestCase):
    def test_without_sse(self):
        server, fake = make_server(0, sse=False)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        touch = SimTouch(io.StringIO(""))
        with tempfile.TemporaryDirectory() as tmp:
            app = PanelApp(SimScreen(Path(tmp) / "p.png"), touch, f"http://127.0.0.1:{server.server_address[1]}")
            t = threading.Thread(target=app.run, daemon=True)
            t.start()
            try:
                self.assertTrue(wait_for(lambda: app.online))
                touch.tap(*center(PLAY))
                self.assertTrue(wait_for(lambda: fake.paused))
            finally:
                app.shutdown()
                t.join(3)
                server.shutdown()
                server.server_close()


class PanelEventsTest(unittest.TestCase):
    """Provozní log: co se na panelu stalo, se dá dohledat v events.jsonl."""

    def setUp(self):
        self.log = EventLog()
        self.server, self.fake = make_server(0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.touch = SimTouch(io.StringIO(""))
        self.app = PanelApp(SimScreen(Path(self.tmp.name) / "panel.png"), self.touch,
                            f"http://127.0.0.1:{self.port}", net_backend=_NoNet())
        self.thread = threading.Thread(target=self.app.run, daemon=True)
        self.thread.start()
        self.assertTrue(wait_for(lambda: self.app.online), "panel se nepřipojil")

    def tearDown(self):
        self.app.shutdown()
        self.thread.join(3)
        self._stop_server()
        self.tmp.cleanup()
        self.log.close()

    def _stop_server(self):
        if self.server is not None:
            self.server.closing = True
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    def _stop_app(self):
        self.app.shutdown()
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())

    def test_actions_gestures_and_summaries(self):
        self.assertTrue(self.log.wait("panel.link", state="online", mode="sse"))
        x, y = center(PLAY)
        self.touch.tap(x, y)
        ev = self.log.wait("panel.action", button="play")
        self.assertTrue(ev)
        self.assertEqual(ev[0]["source"], "touch")
        self.assertEqual(ev[0]["did"], "pause")
        self.assertEqual((ev[0]["x"], ev[0]["y"]), (x, y))
        self.assertGreaterEqual(ev[0]["press_ms"], 50)
        # bounce: a second tap right away is swallowed and only counted
        self.touch.tap(x, y, hold=0.03)

        self.touch.tap(*center(VOL_UP))
        tap = self.log.wait("panel.action", button="vol_up", hold=False)
        self.assertTrue(tap)
        self.assertEqual((tap[0]["steps"], tap[0]["vol_to"] - tap[0]["vol_from"]), (1, 5))

        vx, vy = center(VOL_DOWN)
        self.touch.feed("down", vx, vy)
        time.sleep(1.0)
        self.touch.feed("up", vx, vy)
        held = self.log.wait("panel.action", button="vol_down", hold=True)
        self.assertTrue(held)
        self.assertGreaterEqual(held[0]["steps"], 2)
        self.assertLess(held[0]["vol_to"], held[0]["vol_from"])

        by = center(VOL)[1]
        self.touch.drag(VOL[0] + 90, by, VOL[2] - 30, by, steps=10, dt=0.02)
        drag = self.log.wait("panel.volume_drag")
        self.assertTrue(drag)
        self.assertEqual(drag[0]["vol_to"], volume_at(VOL[2] - 30, 100))
        self.assertEqual(drag[0]["x"], VOL[0] + 90)

        # slid off a button and lifted: nothing fires, one count
        self.touch.feed("down", x, y)
        time.sleep(0.05)
        self.touch.feed("move", x, y + 200)
        time.sleep(0.05)
        self.touch.feed("up", x, y + 200)
        self.touch.tap(5, 60)  # nowhere near a button

        # the speaker's wheel: one line per turn, not per click
        for _ in range(4):
            self.app.events.put(("key", "vol_up"))
        self.app.events.put(("key", "next"))
        burst = self.log.wait("panel.action", button="vol_up", source="mediakey")
        self.assertEqual(len(burst), 1)
        self.assertEqual(burst[0]["steps"], 4)
        self.assertTrue(self.log.wait("panel.action", button="next", source="mediakey"))

        self._stop_app()  # the last minute's summaries are flushed on the way out
        total = {}
        for r in self.log.all("panel.touch_rejects"):
            for k, v in r.items():
                if isinstance(v, int):
                    total[k] = total.get(k, 0) + v
        self.assertGreaterEqual(total.get("debounce", 0), 1)
        self.assertGreaterEqual(total.get("slid_out", 0), 1)
        self.assertGreaterEqual(total.get("miss", 0), 1)
        render = self.log.all("panel.render")
        self.assertTrue(render)
        self.assertGreater(sum(r["frames"] for r in render), 0)
        for key in ("show_p50_ms", "show_p95_ms", "render_p95_ms", "px", "cpu_ms"):
            self.assertIn(key, render[-1])
        # paints are aggregated, never one line per frame
        self.assertLess(len(render), 5)

    def test_link_transitions(self):
        self._stop_server()
        off = self.log.wait("panel.link", timeout=10, state="offline")
        self.assertTrue(off)
        self.assertIn("reason", off[0])
        self.server, self.fake = make_server(self.port)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        back = self.log.wait("panel.link", timeout=10, state="online", reconnects=1)
        self.assertTrue(back)
        self.assertIn("offline_ms", back[0])

    def test_offline_touch_is_counted(self):
        self._stop_server()
        self.assertTrue(wait_for(lambda: not self.app.online, timeout=10))
        self.touch.tap(*center(PLAY))
        self.assertTrue(wait_for(lambda: self.app.stats.counts.get("offline", 0) >= 1))
        self.assertFalse(self.log.find("panel.action", button="play"))


class PollingEventsTest(unittest.TestCase):
    def test_feed_mode_logged(self):
        log = EventLog()
        server, _ = make_server(0, sse=False)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        with tempfile.TemporaryDirectory() as tmp:
            app = PanelApp(SimScreen(Path(tmp) / "p.png"), SimTouch(io.StringIO("")),
                           f"http://127.0.0.1:{server.server_address[1]}", net_backend=_NoNet())
            t = threading.Thread(target=app.run, daemon=True)
            t.start()
            try:
                self.assertTrue(log.wait("panel.feed_mode", mode="poll"))
                self.assertTrue(log.wait("panel.link", state="online", mode="poll"))
            finally:
                app.shutdown()
                t.join(3)
                server.shutdown()
                server.server_close()
                log.close()


class _NoNet:
    """NetworkManager stand-in that knows nothing (these tests don't open net screens)."""

    def status(self):
        from ytdj.panel.net import NetStatus

        return NetStatus(hostname="test")

    def scan(self, rescan=False):
        return []

    def connect(self, ssid, password):
        raise AssertionError("no connects here")


class _FakeDevice:
    """KeDei bus stand-in: scripted raw samples for the touch driver."""

    def __init__(self, samples):
        self.samples = list(samples)
        self.pen = False

    def init(self, madctl):
        pass

    def touch_raw(self):
        item = self.samples.pop(0) if self.samples else None
        if item == "invalid":  # PENIRQ says down, the converter gave nothing usable
            self.pen = True
            return None
        self.pen = item is not None
        return item

    def pen_down(self):
        return self.pen


class TouchDriverStatsTest(unittest.TestCase):
    def setUp(self):
        self.log = EventLog()
        self.saved = kedei._Device._instance

    def tearDown(self):
        kedei._Device._instance = self.saved
        self.log.close()

    def _touch(self, samples):
        kedei._Device._instance = _FakeDevice(samples)
        cal = kedei.Calibration(1, 0, 0, 0, 1, 0)  # raw = pixels
        t = kedei.KedeiTouch(calibration=cal)
        t.DOWN_INTERVAL = t.IDLE_INTERVAL = 0.0
        return t

    def test_anomalies_are_counted_and_summarised(self):
        p = 4000  # pressure well above MIN_PRESSURE
        samples = [
            (100, 100, p), None,                            # landed, then nothing: aborted press
            (100, 100, p), (300, 100, p), (100, 250, p),    # three samples all over: spread
            "invalid", "invalid",                           # pen down, no usable sample
            (200, 200, 50),                                 # too light
            (200, 200, p), (201, 200, p), (200, 201, p),    # a real press
            (400, 50, p),                                   # unconfirmed jump…
            (50, 300, p),                                   # …replaced by another
            None, None, None,                               # release
        ]
        t = self._touch(samples)
        kinds = []
        for _ in range(len(samples) + 2):
            ev = t.poll(0.0)
            if ev:
                kinds.append(ev.kind)
        self.assertEqual(kinds, ["down", "up"])
        stats = PanelStats(touch_stats=t.take_stats)
        stats.flush()
        drv = self.log.all("panel.touch_driver")
        self.assertEqual(len(drv), 1)
        d = drv[0]
        self.assertEqual(d["downs"], 1)
        self.assertGreaterEqual(d["aborted_press"], 1)
        self.assertGreaterEqual(d["spread_rejected"], 1)
        self.assertEqual(d["invalid_samples"], 2)
        self.assertEqual(d["low_pressure"], 1)
        self.assertEqual(d["jump_dropped"], 1)
        self.assertEqual(t.take_stats(), {})  # taken and reset
        stats.flush()
        self.assertEqual(len(self.log.all("panel.touch_driver")), 1)  # nothing new → no line

    def test_calibration_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = kedei.Calibration.load(Path(tmp) / "none.json")
            self.assertEqual(missing.source, "default")
            path = Path(tmp) / "cal.json"
            kedei.Calibration(1, 0, 0, 0, 1, 0).save(path)
            self.assertEqual(kedei.Calibration.load(path).source, f"file:{path}")

    def test_repeated_errors_are_throttled(self):
        stats = PanelStats()
        for _ in range(50):
            stats.error("show", OSError("SPI timeout"))
        stats.flush()
        errs = self.log.all("panel.driver_error")
        self.assertEqual(len(errs), 2)
        self.assertEqual(errs[0]["error"], "OSError: SPI timeout")
        self.assertEqual(errs[1]["repeated"], 49)


class WishTest(unittest.TestCase):
    """The wish screen: quick picks, the Czech keyboard, the DJ's answer."""

    def setUp(self):
        from ytdj.panel.ui import WISH_TARGET

        self.wish_btn = WISH_TARGET
        self.server, self.fake = make_server(0)
        self.fake.prompt_delay = 0.3
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.touch = SimTouch(io.StringIO(""))
        self.app = PanelApp(SimScreen(Path(self.tmp.name) / "panel.png"), self.touch,
                            f"http://127.0.0.1:{self.port}", net_backend=_NoNet())
        self.thread = threading.Thread(target=self.app.run, daemon=True)
        self.thread.start()
        self.assertTrue(wait_for(lambda: self.app.online), "panel se nepřipojil")

    def tearDown(self):
        self.app.shutdown()
        self.thread.join(3)
        self.server.closing = True
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _open(self):
        self.touch.tap(*center(self.wish_btn))
        self.assertTrue(wait_for(lambda: self.app.wish.page == "home"))
        self.assertTrue(wait_for(lambda: self.app._shown_page == "wish:home"))

    def _key(self, kid):
        from ytdj.panel.netui import kb_keys

        keys = dict(kb_keys(self.app.wish.kb_page, "cs", accents=True))
        self.touch.tap(*center(keys[kid]), hold=0.06)
        time.sleep(0.12)

    def test_quick_pick_sends_and_answers(self):
        from ytdj.panel.wishui import BTN_R, chip_box

        self._open()
        self.touch.tap(*center(chip_box(0)))
        self.assertTrue(wait_for(lambda: self.fake.prompts))
        self.assertEqual(self.fake.prompts[0], {"text": "víc takového", "source": "panel"})
        self.assertTrue(wait_for(lambda: self.app.wish.phase == "ok"))
        self.assertIn("víc takového", self.app.wish.reply)
        self.assertTrue(wait_for(lambda: self.app._shown_page == "wish:sent"))
        self.touch.tap(*center(BTN_R))  # Hotovo
        self.assertTrue(wait_for(lambda: self.app._shown_page == "player"))

    def test_czech_keyboard(self):
        from ytdj.panel.wishui import FIELD_BTN

        self._open()
        self.touch.tap(*center(FIELD_BTN))
        self.assertTrue(wait_for(lambda: self.app.wish.page == "keys"))
        self._key("acc")
        self.assertEqual(self.app.wish.kb_page, "áč")
        self._key("c:č")
        self.assertEqual(self.app.wish.kb_page, "abc")  # one accented letter, then back
        for ch in "echomor":
            self._key(f"c:{ch}")
        self.assertEqual(self.app.wish.text, "čechomor")
        self._key("ok")
        self.assertTrue(wait_for(lambda: self.fake.prompts))
        self.assertEqual(self.fake.prompts[0]["text"], "čechomor")
        self.assertTrue(wait_for(lambda: self.app.wish.phase == "ok"))
        self.assertEqual(self.app.wish.text, "")  # sent drafts are forgotten

    def test_empty_wish_is_not_sent(self):
        from ytdj.panel.wishui import FIELD_BTN

        self._open()
        self.touch.tap(*center(FIELD_BTN))
        self.assertTrue(wait_for(lambda: self.app.wish.page == "keys"))
        self._key("space")
        self._key("ok")
        self.assertTrue(self.app.wish.hint)
        self.assertEqual(self.fake.prompts, [])

    def test_busy_dj_waits_then_sends(self):
        from ytdj.panel.wishui import chip_box

        self.fake.busy = True  # somebody else's wish is being worked on
        self._open()
        self.touch.tap(*center(chip_box(2)))
        self.assertTrue(wait_for(lambda: self.app.wish.phase == "wait"))
        self.assertEqual(self.fake.prompts, [])
        self.fake.busy = False
        self.assertTrue(wait_for(lambda: self.app.wish.phase == "ok", timeout=10))
        self.assertEqual(self.fake.prompts[0]["text"], "jen česky")

    def test_answer_comes_back_after_leaving(self):
        from ytdj.panel.netui import BTN_FULL
        from ytdj.panel.wishui import chip_box

        self.fake.prompt_delay = 1.5
        self._open()
        self.touch.tap(*center(chip_box(1)))
        self.assertTrue(wait_for(lambda: self.app.wish.phase == "busy"))
        self.touch.tap(*center(BTN_FULL))  # Zpět k přehrávání
        self.assertTrue(wait_for(lambda: self.app._shown_page == "player"))
        self.assertTrue(wait_for(lambda: self.app.wish.phase == "ok"))
        self.assertTrue(wait_for(lambda: self.app._shown_page == "wish:sent"))

    def test_failure_offers_retry(self):
        from ytdj.panel.wishui import BTN_R, chip_box

        self.fake.prompt_status = 500
        self._open()
        self.touch.tap(*center(chip_box(3)))
        self.assertTrue(wait_for(lambda: self.app.wish.phase == "error"))
        self.assertIn("chybu", self.app.wish.error)
        self.fake.prompt_status = 200
        self.touch.tap(*center(BTN_R))  # Zkusit znovu
        self.assertTrue(wait_for(lambda: self.app.wish.phase == "ok"))
        self.assertEqual([p["text"] for p in self.fake.prompts], ["něco klidnějšího"] * 2)


class WishLayoutTest(unittest.TestCase):
    def test_keyboard_keys_fit_and_do_not_overlap(self):
        from ytdj.panel.netui import kb_keys
        from ytdj.panel.ui import H, W

        for page in ("abc", "áč", "123", "#+="):
            keys = kb_keys(page, "cs", accents=True)
            ids = [k for k, _ in keys]
            self.assertIn("acc", ids)
            for kid, (l, t, r, b) in keys:
                self.assertTrue(0 <= l < r <= W and 0 <= t < b <= H, (page, kid))
                self.assertGreaterEqual(r - l, 38, (page, kid))  # a fingertip on resistive glass
            for i, (_, a) in enumerate(keys):
                for _, c in keys[i + 1:]:
                    self.assertFalse(a[0] < c[2] and c[0] < a[2] and a[1] < c[3] and c[1] < a[3])
        # the network keyboard is unchanged
        self.assertNotIn("acc", [k for k, _ in kb_keys("abc", "cs")])

    def test_player_targets_do_not_overlap(self):
        from ytdj.panel.ui import TARGETS

        boxes = list(TARGETS.items())
        for i, (n1, a) in enumerate(boxes):
            for n2, c in boxes[i + 1:]:
                self.assertFalse(a[0] < c[2] and c[0] < a[2] and a[1] < c[3] and c[1] < a[3], (n1, n2))

    def test_idle_close(self):
        from ytdj.panel.wishapp import IDLE_CLOSE, WishController

        c = WishController(None, lambda m: None)
        c.open(100.0)
        self.assertFalse(c.timers(100.0 + IDLE_CLOSE - 1))
        self.assertTrue(c.timers(100.0 + IDLE_CLOSE + 1))
        self.assertIsNone(c.page)


if __name__ == "__main__":
    unittest.main()
