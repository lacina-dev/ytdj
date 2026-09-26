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

# no cover art from the internet in tests (fake ids aren't YouTube ids anyway)
os.environ.setdefault("YTDJ_PANEL_ART_URL", "")
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
        self.app.page_guard = 0.0  # the tests tap faster than a finger can (see PageGuardTest)
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
        self.app.page_guard = 0.0  # the tests tap faster than a finger can (see PageGuardTest)
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
        self.touch.tap(300, 182)  # the progress bar — nowhere near a button

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

    def test_new_server_version_is_logged(self):
        first = self.log.wait("panel.server_build", version="fake-1")
        self.assertTrue(first)
        with self.fake.lock:
            self.fake.build = "fake-2"  # ytdj nasazen znovu
        again = self.log.wait("panel.server_build", version="fake-2")
        self.assertTrue(again)
        self.assertEqual(again[0]["previous"], "fake-1")

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
        self.app.page_guard = 0.0  # the tests tap faster than a finger can (see PageGuardTest)
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
        sent = dict(self.fake.prompts[0])
        client = sent.pop("client")
        self.assertTrue(client.startswith("panel-"))  # jedna relace přání = jeden člověk
        self.assertEqual(sent, {"text": "víc takového", "source": "panel", "who": "displej",
                                "play_next": False, "wait": False, "chip": "more"})
        # accepted at once, the DJ's decision comes with the status stream
        self.assertTrue(wait_for(lambda: self.app.wish.phase == "queued"))
        self.assertIn("víc takového", self.app.wish.reply)
        self.assertIn(self.app.wish.req_id, self.app.wish.mine)
        self.assertTrue(wait_for(lambda: self.app._shown_page == "wish:sent"))
        self.touch.tap(*center(BTN_R))  # Hotovo
        self.assertTrue(wait_for(lambda: self.app._shown_page == "player"))

    def test_closing_the_wish_screen_starts_a_new_person(self):
        from ytdj.panel.wishui import BTN_R, chip_box

        self._open()
        self.app.wish.who = "Robert"  # vybral si jméno
        self.touch.tap(*center(chip_box(0)))
        self.assertTrue(wait_for(lambda: self.app.wish.phase == "queued"))
        first = self.fake.prompts[0]["client"]
        self.assertEqual(self.fake.prompts[0]["who"], "Robert")
        self.assertTrue(wait_for(lambda: self.app._shown_page == "wish:sent"))
        self.touch.tap(*center(BTN_R))  # Hotovo
        self.assertTrue(wait_for(lambda: self.app._shown_page == "player"))
        self.assertEqual(self.app.wish.who, "displej")  # další u displeje je někdo jiný
        self._open()
        self.touch.tap(*center(chip_box(3)))
        self.assertTrue(wait_for(lambda: len(self.fake.prompts) == 2))
        self.assertNotEqual(self.fake.prompts[1]["client"], first)
        self.assertEqual(self.fake.prompts[1]["who"], "displej")

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
        self.assertTrue(wait_for(lambda: self.app.wish.phase == "queued"))
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

    def test_busy_dj_does_not_block(self):
        from ytdj.panel.wishui import chip_box

        self.fake.busy = True  # somebody else's wish is being worked on
        self._open()
        self.touch.tap(*center(chip_box(2)))
        # no more waiting for the DJ: the wish goes out at once and queues
        self.assertTrue(wait_for(lambda: self.fake.prompts))
        self.assertEqual(self.fake.prompts[0]["text"], "jen česky")
        self.assertTrue(wait_for(lambda: self.app.wish.phase == "queued"))

    def test_right_after_this(self):
        from ytdj.panel.wishui import BTN_L, chip_box

        self._open()
        self.touch.tap(*center(chip_box(3)))
        self.assertTrue(wait_for(lambda: self.app.wish.phase == "queued"))
        self.assertTrue(wait_for(lambda: self.app._shown_page == "wish:sent"))
        self.touch.tap(*center(BTN_L))  # Hned po téhle
        self.assertTrue(wait_for(lambda: self.fake.actions))
        act = self.fake.actions[0]
        self.assertEqual((act["action"], act["id"]), ("next", self.app.wish.req_id))
        self.assertTrue(wait_for(lambda: self.fake.requests[0]["play_next"]))

    def test_queue_page_removes_only_own(self):
        from ytdj.panel.netui import BTN_R
        from ytdj.panel.ui import TRACK_TARGET
        from ytdj.panel.wishui import chip_box, rm_box, targets

        with self.fake.lock:
            other = self.fake.add_request("Dancing Queen", "Jana", state="queued", reply="ok")
        self._open()
        self.touch.tap(*center(chip_box(0)))
        self.assertTrue(wait_for(lambda: self.app.wish.phase == "queued"))
        self.assertTrue(wait_for(lambda: self.app._shown_page == "wish:sent"))
        self.touch.tap(*center(BTN_R))  # Hotovo
        self.assertTrue(wait_for(lambda: self.app._shown_page == "player"))
        self.assertTrue(wait_for(lambda: self.app._view().wishes == 2), self.app._view().wishes)
        self.touch.tap(*center(TRACK_TARGET))  # "Pak: …" → the queue
        self.assertTrue(wait_for(lambda: self.app._shown_page == "wish:queue"))
        view = self.app.wish.view(time.monotonic())
        rows = {r.who: r for r in view.rows}
        self.assertTrue(rows["displej"].mine)
        self.assertFalse(rows["Jana"].mine)
        mine_at = [r.id for r in view.rows].index(self.app.wish.req_id)
        t = targets(view)
        self.assertIn(f"rm{mine_at}", t)
        self.assertEqual([k for k in t if k.startswith("rm")], [f"rm{mine_at}"])  # × only on ours
        self.touch.tap(*center(rm_box(mine_at)))
        self.assertTrue(wait_for(lambda: any(a.get("action") == "remove" for a in self.fake.actions)))
        self.assertTrue(wait_for(lambda: self.fake.requests[-1]["state"] == "removed"))
        self.assertEqual(other["state"], "queued")

    def test_play_in_silence_asks_the_server_to_start(self):
        from ytdj.panel.ui import PLAY

        self.fake.idle = True
        self.assertTrue(wait_for(lambda: not self.app._view().has_track))
        self.touch.tap(*center(PLAY))
        self.assertTrue(wait_for(lambda: ("play", None) in self.fake.controls))
        self.assertEqual(self.fake.prompts, [])  # not a listener's wish any more
        self.assertTrue(wait_for(lambda: self.app._view().has_track, timeout=5))

    def test_answer_comes_back_after_leaving(self):
        from ytdj.panel.netui import BTN_FULL
        from ytdj.panel.wishui import chip_box

        self.fake.prompt_delay = 1.5
        self._open()
        self.touch.tap(*center(chip_box(1)))
        self.assertTrue(wait_for(lambda: self.app.wish.phase == "busy"))
        self.touch.tap(*center(BTN_FULL))  # Zpět k přehrávání
        self.assertTrue(wait_for(lambda: self.app._shown_page == "player"))
        self.assertTrue(wait_for(lambda: self.app.wish.phase == "queued"))
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
        self.assertTrue(wait_for(lambda: self.app.wish.phase == "queued"))
        self.assertEqual([p["text"] for p in self.fake.prompts], ["něco klidnějšího"] * 2)


class PlayerLookTest(unittest.TestCase):
    """What the player says and how big — the part read from across the room."""

    BASE = View(online=True, connecting=False, has_track=True, title="Holky z naší školky", artist="Olympic",
                running=True, elapsed=60, duration=240, volume=50, can_next=True)

    def test_title_as_big_as_fits(self):
        r = Renderer()
        lines, font = r._fit_title("Holky z naší školky", 452)
        self.assertEqual((lines, font.size), (["Holky z naší školky"], 34))
        lines, font = r._fit_title("Bohemian Rhapsody (Remastered 2011)", 452)
        self.assertEqual(len(lines), 2)
        self.assertFalse(lines[-1].endswith("…"))  # two lines, whole — no ellipsis while it fits
        lines, font = r._fit_title("Příliš žluťoučký kůň úpěl ďábelské ódy " * 3, 452)
        self.assertEqual((len(lines), font.size), (2, 24))
        self.assertTrue(lines[-1].endswith("…"))

    def test_outage_says_why_and_that_wishes_wait(self):
        r = Renderer()
        v = replace(self.BASE, outage=True, running=False)
        self.assertIn("spojení s YouTube", r._outage_line(replace(v, outage_reason="dns")))
        self.assertIn("přihlášení", r._outage_line(replace(v, outage_reason="youtube_login")))
        self.assertIn("přání počkají", r._outage_line(replace(v, outage_reason="whatever")))
        r.render(v, full=True)  # draws without error, whatever the reason

    def test_friendly_copy(self):
        from ytdj.panel.ui import STRINGS

        cs = STRINGS["cs"]
        self.assertNotIn("mozku", cs["dj_offline"])
        self.assertNotIn("neběží", cs["offline_title"])  # the panel only knows it gets no answer

    def test_silence_has_no_empty_progress_bar(self):
        from ytdj.panel.ui import BAR, BG

        r = Renderer()
        r.render(replace(self.BASE, has_track=False, running=False), full=True)
        tile = r.frame.crop(BAR)
        self.assertEqual(tile.getcolors(), [(tile.width * tile.height, BG)])

    def test_next_wish_line_changes_only_its_row(self):
        r = Renderer()
        v = replace(self.BASE, next_title="Amerika", next_artist="Lucie")
        r.render(v, full=True)
        boxes = r.render(replace(v, next_who="Tomáš", more_wishes=2))
        self.assertTrue(boxes)
        for b in boxes:  # the "Pak:" line only: nothing above the artist, nothing below the track area
            self.assertGreaterEqual(b[1], 34 + 90)
            self.assertLessEqual(b[3], 166)


class PanelBehaviourTest(unittest.TestCase):
    """Rest (dimming), the page-switch touch guard, the rolling refresh, the wish count."""

    def setUp(self):
        self.server, self.fake = make_server(0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.screen = SimScreen(Path(self.tmp.name) / "panel.png")
        self.touch = SimTouch(io.StringIO(""))
        self.app = PanelApp(self.screen, self.touch, f"http://127.0.0.1:{self.port}", net_backend=_NoNet())
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

    def _pause(self):
        self.touch.tap(*center(PLAY))
        self.assertTrue(wait_for(lambda: self.fake.paused and not self.app._view().running))

    def test_rest_dims_and_first_touch_only_wakes(self):
        self._pause()
        self.app.rest_after = 0.3
        self.app.events.put(("wake",))
        self.assertTrue(wait_for(lambda: self.app.resting))
        px = center(PLAY)
        self.assertTrue(wait_for(lambda: self.screen.glass.getpixel(px) != self.app.renderer.frame.getpixel(px)))
        glass, frame = self.screen.glass.getpixel(px), self.app.renderer.frame.getpixel(px)
        self.assertLess(sum(glass), sum(frame) * 0.6)  # dimmed on the glass, the frame itself untouched
        self.touch.tap(*px)
        self.assertTrue(wait_for(lambda: not self.app.resting))
        self.app.rest_after = 300.0
        time.sleep(0.5)
        self.assertTrue(self.fake.paused)  # the waking touch did not press Hrát
        self.assertTrue(wait_for(lambda: self.screen.glass.getpixel(px) == self.app.renderer.frame.getpixel(px)))
        self.touch.tap(*px)
        self.assertTrue(wait_for(lambda: not self.fake.paused))

    def test_music_keeps_it_awake(self):
        self.app.rest_after = 0.3
        time.sleep(1.0)
        self.assertFalse(self.app.resting)

    def test_double_tap_through_a_page_switch_does_nothing(self):
        from ytdj.panel.wishui import BTN_R, chip_box
        from ytdj.panel.ui import WISH_TARGET

        self.app.page_guard = 0.0
        self.touch.tap(*center(WISH_TARGET))
        self.assertTrue(wait_for(lambda: self.app._shown_page == "wish:home"))
        self.touch.tap(*center(chip_box(0)))
        self.assertTrue(wait_for(lambda: self.app._shown_page == "wish:sent"))
        self.app.page_guard = 0.5
        vol = self.fake.volume
        x, y = center(BTN_R)  # "Hotovo" — on the player, that spot is the volume bar
        self.touch.tap(x, y, hold=0.05)
        time.sleep(0.03)
        self.touch.tap(x, y, hold=0.05)
        self.assertTrue(wait_for(lambda: self.app._shown_page == "player"))
        time.sleep(0.8)
        self.assertEqual(self.fake.volume, vol)
        self.assertEqual(self.app.hold_volume, None)
        self.touch.tap(*center(VOL_UP))  # a deliberate tap later works as ever
        self.assertTrue(wait_for(lambda: self.fake.volume != vol))

    def test_rolling_refresh_sends_a_band_not_a_frame(self):
        self._pause()
        time.sleep(0.5)
        px = self.screen.pushed_px
        self.app._band_at = 0.0
        self.app.events.put(("wake",))
        self.assertTrue(wait_for(lambda: self.screen.pushed_px > px))
        time.sleep(0.2)
        self.assertLessEqual(self.screen.pushed_px - px, 480 * 32)
        self.assertGreater(self.app._band_at, time.monotonic() + 10)

    def test_new_wish_shows_a_banner_for_a_few_seconds(self):
        from ytdj.panel import app as app_mod

        time.sleep(0.3)
        self.assertEqual(self.app._view().toast, ())
        old = app_mod.TOAST_TIME
        app_mod.TOAST_TIME = 1.0
        try:
            with self.fake.lock:
                self.fake.add_request("něco od Kabátu", "Petr", state="thinking")
            self.assertTrue(wait_for(lambda: self.app._view().toast[:2] == ("Petr", "něco od Kabátu")))
            self.assertTrue(wait_for(lambda: self.app.renderer._sigs.get("toast", ("none",)) != ("none",)))
            self.assertTrue(wait_for(lambda: self.app._view().toast == (), timeout=4))
            self.assertTrue(wait_for(lambda: self.app.renderer._sigs.get("toast") == ("none",)))
        finally:
            app_mod.TOAST_TIME = old

    def test_phone_button_opens_the_qr_page(self):
        from ytdj.panel.ui import PHONE_TARGET

        self.app.page_guard = 0.0
        self.touch.tap(*center(PHONE_TARGET))
        self.assertTrue(wait_for(lambda: self.app._shown_page == "qr"))
        self.touch.tap(240, 160)
        self.assertTrue(wait_for(lambda: self.app._shown_page == "player"))

    def test_more_wishes_counted_besides_next(self):
        with self.fake.lock:
            self.fake.add_request("Amerika", "Tomáš", state="queued")
            self.fake.add_request("Dancing Queen", "Jana", state="queued")
            self.fake.add_request("něco od Queen", "Petr", state="thinking")
        self.assertTrue(wait_for(lambda: self.app._view().next_who == "Tomáš"))
        self.assertTrue(wait_for(lambda: self.app._view().more_wishes == 2))


def _art_jpeg() -> bytes:
    """An "Art Track" thumbnail: a red square cover letterboxed in black, 320×180."""
    from PIL import Image as _Image

    im = _Image.new("RGB", (320, 180), (0, 0, 0))
    im.paste((200, 30, 30), (70, 0, 250, 180))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


class ArtTest(unittest.TestCase):
    def test_centre_square_of_a_letterboxed_cover(self):
        from ytdj.panel.art import square_tile

        tile = square_tile(_art_jpeg(), 144)
        self.assertEqual(tile.size, (144, 144))
        for px in ((3, 3), (140, 140), (72, 72)):  # corners too: the black bars are cut off
            r, g, b = tile.getpixel(px)
            self.assertGreater(r, 150, px)
            self.assertLess(g, 80, px)

    def test_fetched_once_in_the_background_then_cached(self):
        from ytdj.panel.art import ArtCache

        calls, ready = [], threading.Event()

        def fetch(url, timeout):
            calls.append(url)
            return _art_jpeg()

        stop = threading.Event()
        cache = ArtCache(144, lambda vid: ready.set(), stop, fetch=fetch, url="http://x/{id}.jpg")
        try:
            self.assertIsNone(cache.get("qRIsEHxcgDY"))  # not yet — never blocks the caller
            self.assertTrue(ready.wait(5))
            self.assertEqual(cache.get("qRIsEHxcgDY").size, (144, 144))
            cache.get("qRIsEHxcgDY")
            self.assertEqual(calls, ["http://x/qRIsEHxcgDY.jpg"])
            self.assertIsNone(cache.get("a1"))  # not a YouTube id: nothing to fetch
            self.assertEqual(len(calls), 1)
        finally:
            stop.set()

    def test_failure_is_not_retried_at_once(self):
        from ytdj.panel.art import ArtCache

        calls, ready = [], threading.Event()

        def fetch(url, timeout):
            calls.append(url)
            raise OSError("offline")

        stop = threading.Event()
        cache = ArtCache(144, lambda vid: ready.set(), stop, fetch=fetch, url="http://x/{id}.jpg")
        try:
            cache.want("dQw4w9WgXcQ")
            self.assertTrue(ready.wait(5))
            self.assertTrue(cache.failed("dQw4w9WgXcQ"))
            self.assertIsNone(cache.get("dQw4w9WgXcQ"))
            time.sleep(0.2)
            self.assertEqual(len(calls), 1)
        finally:
            stop.set()

    def test_keeps_only_a_few_tiles(self):
        from ytdj.panel import art

        stop = threading.Event()
        done = threading.Semaphore(0)
        cache = art.ArtCache(32, lambda vid: done.release(), stop, fetch=lambda u, t: _art_jpeg(), url="{id}")
        try:
            ids = [f"abcdefghij{c}" for c in "ABCDEFGHIJ"]
            for vid in ids:
                cache.want(vid)
            for _ in ids:
                self.assertTrue(done.acquire(timeout=5))
            self.assertEqual(len(cache._tiles), art.KEEP)
            self.assertIn(ids[-1], cache._tiles)
        finally:
            stop.set()


class NewLookTest(unittest.TestCase):
    BASE = replace(PlayerLookTest.BASE, track_id="qRIsEHxcgDY", qr_url="http://192.168.0.24:8765")

    def test_cover_or_initials_in_the_art_slot(self):
        from ytdj.panel.art import square_tile
        from ytdj.panel.ui import ART

        tile = square_tile(_art_jpeg(), 144)
        r = Renderer()
        r.art_source = lambda vid: tile
        r.render(self.BASE, full=True)  # not fetched yet: the artist's initials
        before = r.frame.getpixel((ART[0] + 80, ART[1] + 30))
        boxes = r.render(replace(self.BASE, art_ready=True))
        self.assertEqual(len(boxes), 1)
        b = boxes[0]
        self.assertTrue(ART[0] <= b[0] and b[2] <= ART[2] and ART[1] <= b[1] and b[3] <= ART[3], b)
        after = r.frame.getpixel((ART[0] + 80, ART[1] + 30))
        self.assertNotEqual(before, after)
        self.assertGreater(after[0], 150)

    def test_silence_and_rest_show_the_qr(self):
        r = Renderer()
        r.render(replace(self.BASE, has_track=False, running=False), full=True)
        self.assertIsNotNone(r.qr_box)
        r.render(self.BASE, full=True)
        self.assertIsNone(r.qr_box)  # playing: the cover
        r.render(replace(self.BASE, running=False, paused=True, rest=True), full=True)
        self.assertIsNotNone(r.qr_box)
        r.render(replace(self.BASE, has_track=False, qr_url=""), full=True)
        self.assertIsNone(r.qr_box)  # no known address, no code

    def test_wish_banner_is_one_strip_in_and_out(self):
        from ytdj.panel.ui import TOAST

        r = Renderer()
        r.render(self.BASE, full=True)
        header = r.frame.crop(TOAST).tobytes()
        boxes = r.render(replace(self.BASE, toast=("Petr", "něco od Kabátu", "DJ vybírá…")))
        self.assertTrue(boxes)
        for b in boxes:
            self.assertLessEqual(b[3], TOAST[3])
        self.assertEqual(r.render(replace(self.BASE, toast=("Petr", "něco od Kabátu", "DJ vybírá…"))), [])
        boxes = r.render(self.BASE)
        for b in boxes:
            self.assertLessEqual(b[3], TOAST[3])
        self.assertEqual(r.frame.crop(TOAST).tobytes(), header)  # the strip is back exactly

    def test_status_strip_says_one_thing(self):
        from ytdj.panel.ui import STATUS

        r = Renderer()
        long_mood = "klidný večer, český rock, trochu jazzu a hodně kytar"
        r.render(replace(self.BASE, mood=long_mood), full=True)
        clean = r.frame.crop(STATUS).tobytes()
        r.render(replace(self.BASE, mood=""), full=True)
        self.assertEqual(r.frame.crop(STATUS).tobytes(), clean)  # a mood that doesn't fit isn't cut to "klidný v…"


class VotesTest(unittest.TestCase):
    """Office votes on the display: ♥ / ▲▼ after the artist, "vyřazená hlasováním", markers."""

    BASE = replace(PlayerLookTest.BASE, title="Zastav mě", artist="Marek Ztracený",
                   next_title="Pohoda", next_artist="Kabát")

    def test_vote_mark(self):
        from ytdj.panel.ui import vote_mark

        self.assertEqual(vote_mark(None), "")
        self.assertEqual(vote_mark({"up": 0, "down": 0, "status": "neutral"}), "")
        self.assertEqual(vote_mark({"up": 3, "down": 0, "status": "favourite"}), "favourite")
        self.assertEqual(vote_mark({"up": 0, "down": 2, "status": "banned"}), "banned")
        self.assertEqual(vote_mark({"status": "neutral", "artist_status": "banned"}), "banned")
        self.assertEqual(vote_mark({"status": "neutral", "artists": [{"name": "Oasis", "status": "banned"}]}), "banned")
        self.assertEqual(vote_mark({"status": "neutral", "artist_status": "pending"}), "")
        # 👍 celému interpretovi: ♥ i u skladby bez vlastních hlasů
        self.assertEqual(vote_mark({"status": "neutral", "artist_status": "favourite"}), "favourite")
        self.assertEqual(vote_mark({"status": "neutral", "artists": [{"name": "Olympic", "status": "favourite"}]}),
                         "favourite")
        # vyřazený spoluinterpret přebije oblíbeného
        self.assertEqual(vote_mark({"status": "neutral", "artists": [{"name": "A", "status": "favourite"},
                                                                     {"name": "B", "status": "banned"}]}), "banned")

    def test_badge_texts(self):
        r = Renderer()
        self.assertEqual(r._vote_badge(self.BASE), ("", ""))  # no votes, nothing shown
        self.assertEqual(r._vote_badge(replace(self.BASE, vote_up=3, vote_status="favourite")), ("♥ 3", "fav"))
        self.assertEqual(r._vote_badge(replace(self.BASE, vote_up=2, vote_down=1, vote_status="neutral")),
                         ("▲ 2  ▼ 1", "plain"))
        banned = replace(self.BASE, vote_down=2, vote_status="banned")
        self.assertEqual(r._vote_badge(banned), ("", ""))
        self.assertEqual(r._voted_out(banned), "vyřazená hlasováním")
        self.assertEqual(r._voted_out(replace(self.BASE, artist_banned=True)), "interpret vyřazen hlasováním")

    def test_no_votes_draws_exactly_as_before(self):
        r1, r2 = Renderer(), Renderer()
        r1.render(self.BASE, full=True)
        r2.render(replace(self.BASE, vote_status="neutral"), full=True)
        self.assertEqual(r1.frame.tobytes(), r2.frame.tobytes())

    def test_a_vote_redraws_only_the_text_block(self):
        from ytdj.panel.ui import TRACK

        r = Renderer()
        r.render(self.BASE, full=True)
        boxes = r.render(replace(self.BASE, vote_up=1, vote_status="favourite"))
        self.assertTrue(boxes)
        for b in boxes:
            self.assertTrue(TRACK[0] <= b[0] and b[2] <= TRACK[2] and TRACK[1] <= b[1] and b[3] <= TRACK[3], b)
        px = sum((b[2] - b[0]) * (b[3] - b[1]) for b in boxes)
        self.assertLess(px, 3000)

    def test_queue_rows_carry_the_verdict_of_their_track(self):
        from ytdj.panel.wishapp import wish_votes

        state = {
            "current": {"id": "a", "reason": {"kind": "wish", "id": "w1", "who": "Petr"},
                        "votes": {"up": 0, "down": 2, "status": "banned"}},
            "queue": [{"id": "b", "req": {"id": "w2", "who": "Jana"}, "votes": {"up": 2, "status": "favourite"}},
                      {"id": "c", "req": {"id": "w1", "who": "Petr"}, "votes": {"up": 5, "status": "favourite"}},
                      {"id": "d", "req": {"id": "w3", "who": "Karel"}}],
        }
        self.assertEqual(wish_votes(state), {"w1": "banned", "w2": "favourite"})


class VotesEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.server, self.fake = make_server(0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.app = PanelApp(SimScreen(Path(self.tmp.name) / "p.png"), SimTouch(io.StringIO("")),
                            f"http://127.0.0.1:{self.server.server_address[1]}", net_backend=_NoNet())
        self.thread = threading.Thread(target=self.app.run, daemon=True)
        self.thread.start()
        self.assertTrue(wait_for(lambda: self.app.online and self.app._view().has_track))

    def tearDown(self):
        self.app.shutdown()
        self.thread.join(3)
        self.server.closing = True
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _vote(self, cid, vote):
        v = self.app._view()
        key = self.fake.song_key(v.artist, v.title)
        with self.fake.lock:
            self.fake.votes.setdefault(("song", key), {})[cid] = {
                "vote": vote, "who": cid, "at": time.time(), "artist": v.artist, "title": v.title, "video_id": ""}

    def test_votes_of_the_current_track_reach_the_player(self):
        self.assertEqual(self.app._view().vote_status, "neutral")
        self._vote("web-a", 1)
        self._vote("web-b", 1)
        self.assertTrue(wait_for(lambda: (self.app._view().vote_up, self.app._view().vote_status) == (2, "favourite")))
        self.assertTrue(wait_for(lambda: "♥ 2" in str(self.app.renderer._sigs.get("track"))))
        self._vote("web-a", -1)
        self._vote("web-b", -1)
        self.assertTrue(wait_for(lambda: self.app._view().vote_status == "banned"))


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
