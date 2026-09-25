"""Panel tests: layout helpers, and the whole thing against a fake ytdj.

    python -m unittest tests.test_panel -v
"""

from __future__ import annotations

import io
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

from ytdj.panel.app import PanelApp  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
