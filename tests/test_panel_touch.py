"""Touch on the resistive glass: the driver's filtering, the press rules, calibration.

    python3 -m unittest tests.test_panel_touch -v

The sample sequences imitate what the Pi's telemetry showed (25.–26. 9.):
landings with scattered samples (spread_rejected 565 / 347 downs), converter
samples that come back empty while the finger is down (invalid_samples 481),
and presses lost as "slid out" on release.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import test_panel  # noqa: E402,F401  (sets YTDJ_EVENTS_FILE / no cover art before ytdj.panel loads)
from fake_ytdj import make_server  # noqa: E402
from test_panel import EventLog, _FakeDevice, _NoNet, center, wait_for  # noqa: E402

from ytdj.panel import kedei  # noqa: E402
from ytdj.panel.app import PanelApp  # noqa: E402
from ytdj.panel.calib import POINTS  # noqa: E402
from ytdj.panel.sim import SimScreen, SimTouch  # noqa: E402
from ytdj.panel.touchpress import CANCEL, GRAB, STICKY, Press, closest, nearest  # noqa: E402
from ytdj.panel.ui import NEXT, PLAY, TARGETS, VOL_TRACK_W, VOL_UP, knob_x  # noqa: E402

P = 4000  # pressure well above MIN_PRESSURE


class PressRulesTest(unittest.TestCase):
    BOX = NEXT  # (244, 204, 472, 256)

    def test_jitter_and_a_lift_drift_keep_the_press(self):
        # a resting finger near the button's lower edge, ±12 px, then one drifting lift sample
        p = Press("next", self.BOX)
        for x, y in ((300, 250), (310, 262), (296, 258), (305, 268), (300, 255), (320, 300)):
            p.move(x, y)
        self.assertTrue(p.inside)  # 300 is 44 px below: one sample far away is not a drag

    def test_sustained_drag_away_cancels(self):
        p = Press("next", self.BOX)
        p.move(300, 270)
        p.move(300, 256 + CANCEL + 5)
        self.assertTrue(p.inside)
        p.move(300, 256 + CANCEL + 20)
        self.assertFalse(p.inside)

    def test_coming_back_rearms(self):
        p = Press("next", self.BOX)
        p.move(300, 330)
        p.move(300, 340)
        self.assertFalse(p.inside)
        p.move(300, 256 + (STICKY + CANCEL) // 2)  # between the margins: no flicker, still out
        self.assertFalse(p.inside)
        p.move(300, 256 + STICKY - 2)
        self.assertTrue(p.inside)

    def test_nearest_target_within_grab(self):
        tg = {"a": (0, 0, 100, 50), "b": (110, 0, 200, 50)}
        self.assertEqual(nearest(tg, 50, 20)[0], "a")
        self.assertEqual(nearest(tg, 104, 20)[0], "a")  # in the gap, nearer to a
        self.assertEqual(nearest(tg, 107, 20)[0], "b")
        self.assertEqual(nearest(tg, 50, 50 + GRAB - 1)[0], "a")
        self.assertEqual(nearest(tg, 50, 50 + GRAB + 5)[0], None)
        name, dist, dx, dy = closest(tg, 50, 80)
        self.assertEqual((name, dx, dy), ("a", 0, 55))
        self.assertGreater(dist, GRAB)

    def test_player_touch_areas_cover_the_gaps(self):
        # between Hrát/Další, between the buttons and the volume row, the screen edges
        for x, y in ((240, 230), (120, 258), (2, 230), (478, 300), (84, 300), (396, 300)):
            self.assertIsNotNone(nearest(TARGETS, x, y)[0], (x, y))


def replay(samples):
    """Runs the driver over scripted raw samples (raw = pixels); the events it produces."""
    kedei._Device._instance = _FakeDevice(samples)
    t = kedei.KedeiTouch(calibration=kedei.Calibration(1, 0, 0, 0, 1, 0))
    t.DOWN_INTERVAL = t.IDLE_INTERVAL = 0.0
    events = []
    for _ in range(len(samples) + 3):
        ev = t.poll(0.0)
        if ev:
            events.append(ev)
    return events, t.take_stats()


class DriverReplayTest(unittest.TestCase):
    def setUp(self):
        self.saved = kedei._Device._instance

    def tearDown(self):
        kedei._Device._instance = self.saved

    def test_scattered_landing_with_a_noisy_sample_still_presses(self):
        # the old driver: 3 samples within 12 px in a row, any bad sample started over → nothing
        samples = [(300, 230, P), "invalid", (318, 236, P), (309, 228, P), (305, 233, P),
                   None, None, None]
        events, st = replay(samples)
        self.assertEqual([e.kind for e in events][:1], ["down"])
        self.assertEqual(events[-1].kind, "up")
        down = events[0]
        self.assertTrue(abs(down.x - 309) <= 10 and abs(down.y - 232) <= 6, down)
        self.assertEqual(st.get("aborted_press", 0), 0)

    def test_noise_while_held_is_not_a_release(self):
        samples = [(300, 230, P), (302, 231, P)] + ["invalid", (301, 229, 50)] * 4 + [(301, 230, P),
                                                                                     None, None, None]
        events, _ = replay(samples)
        self.assertEqual([e.kind for e in events], ["down", "up"])  # one press, not four

    def test_stuck_penirq_releases_eventually(self):
        samples = [(300, 230, P), (301, 230, P)] + ["invalid"] * (kedei.KedeiTouch.NOISY_RELEASE + 1)
        events, st = replay(samples)
        self.assertEqual([e.kind for e in events], ["down", "up"])
        self.assertEqual(st.get("noisy_release"), 1)

    def test_one_wild_sample_is_dropped_moves_are_smoothed(self):
        samples = [(300, 230, P), (301, 230, P), (450, 60, P), (302, 231, P), (303, 230, P),
                   None, None, None]
        events, st = replay(samples)
        self.assertEqual([e.kind for e in events], ["down", "up"])
        self.assertTrue(all(abs(e.x - 301) < 5 for e in events))

    def test_slide_away_is_reported_as_moves(self):
        samples = [(300, 230, P), (301, 230, P)] + [(300, 230 + 12 * i, P) for i in range(1, 9)] + [None] * 3
        events, _ = replay(samples)
        kinds = [e.kind for e in events]
        self.assertEqual(kinds[0], "down")
        self.assertIn("move", kinds)
        # the finger ended far below (the last LIFT_SKIP samples before a lift are not reported)
        self.assertGreater(events[-1].y, 270)

    def test_a_single_touchy_sample_is_not_a_press(self):
        events, st = replay([(300, 230, P), None, None, None])
        self.assertEqual(events, [])
        self.assertEqual(st.get("aborted_press"), 1)


class _AppBase(unittest.TestCase):
    """The whole panel against a fake ytdj, fed with scripted touch events."""

    def setUp(self):
        self.log = EventLog()
        self.server, self.fake = make_server(0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.screen = SimScreen(Path(self.tmp.name) / "p.png")
        self.touch = SimTouch(io.StringIO(""))
        self.app = PanelApp(self.screen, self.touch, f"http://127.0.0.1:{self.server.server_address[1]}",
                            net_backend=_NoNet())
        self.app.page_guard = 0.0
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
        self.log.close()

    def _gesture(self, points, dt=0.02):
        self.touch.feed("down", *points[0])
        for p in points[1:]:
            time.sleep(dt)
            self.touch.feed("move", *p)
        time.sleep(dt)
        self.touch.feed("up", *points[-1])


class AppTouchTest(_AppBase):
    """Real-looking event sequences through the whole panel."""

    def test_jittery_press_on_next_counts(self):
        before = self.fake.index
        # landed reading into the volume row, then rested on Další ±10 px, the last sample drifted at lift
        self._gesture([(300, 266), (310, 248), (296, 236), (305, 244), (301, 232), (299, 241), (330, 300)])
        self.assertTrue(wait_for(lambda: self.fake.index != before))
        self.assertEqual(self.fake.volume, 65)  # the landing on the volume row did nothing

    def test_owner_case_finger_on_volume_minus_reading_high(self):
        """"Prst mám přes celé −volume, ale když jsem moc nahoře, aktivuje se Play" (26. 9.)."""
        vol = self.fake.volume
        # the logged vol_down hits: dy −30…+3 around the centre (289); the landing read in Hrát
        self._gesture([(50, 255), (52, 262), (47, 270), (51, 266), (49, 275), (50, 268), (48, 290)])
        self.assertTrue(wait_for(lambda: self.fake.volume == vol - 5))
        time.sleep(0.3)
        self.assertFalse(self.fake.paused)  # Hrát was not pressed
        e = self.log.wait("panel.action", button="vol_down")
        self.assertTrue(e)
        self.assertEqual(e[0].get("landed"), "play")

    def test_top_strip_reading_above_the_screen_edge_still_hits(self):
        from ytdj.panel.ui import NET_TARGET

        x = (NET_TARGET[0] + NET_TARGET[2]) // 2
        self._gesture([(x, 0), (x, 1), (x, 0)])
        self.assertTrue(wait_for(lambda: self.app._shown_page == "overview"))

    def test_press_in_the_gap_between_play_and_next(self):
        self._gesture([(238, 230), (239, 231)])  # the 8 px gap, a hair nearer to Hrát
        self.assertTrue(wait_for(lambda: self.fake.paused))

    def test_drag_away_does_not_skip(self):
        before = self.fake.index
        self._gesture([(300, 230), (300, 200), (300, 150), (300, 110), (300, 90)])
        time.sleep(0.5)
        self.assertEqual(self.fake.index, before)

    def test_feedback_ring_shows_before_release(self):
        x, y = center(NEXT)
        self.touch.feed("down", x, y)
        edge, inner = (NEXT[0] + 1, (NEXT[1] + NEXT[3]) // 2), (NEXT[0] + 8, (NEXT[1] + NEXT[3]) // 2)
        # on the glass while the finger is still down: the ring differs from the button's fill
        self.assertTrue(wait_for(lambda: self.screen.glass.getpixel(edge) != self.screen.glass.getpixel(inner)))
        self.assertEqual(self.app.pressed, "next")
        self.touch.feed("move", x + 400, y)  # off screen far: slide away, twice
        self.touch.feed("move", x + 400, y + 1)
        self.touch.feed("up", x + 400, y + 1)

    def test_miss_is_logged_with_the_nearest_button(self):
        self.touch.tap(100, 180)  # the empty time row under the cover
        e = self.log.wait("panel.touch_miss")
        self.assertTrue(e)
        self.assertEqual(e[0]["page"], "player")
        self.assertEqual((e[0]["x"], e[0]["y"]), (100, 180))
        self.assertIn(e[0]["near"], ("play", "phone"))
        self.assertIn("dx", e[0])

    def test_hits_log_their_offset_from_the_centre(self):
        x, y = center(VOL_UP)
        self.touch.tap(x + 7, y - 5)
        e = self.log.wait("panel.action", button="vol_up")
        self.assertTrue(e)
        # centre of the vol_up *touch area* (it fills the gaps), not of the drawn button
        tx = (TARGETS["vol_up"][0] + TARGETS["vol_up"][2]) // 2
        self.assertEqual(e[0]["off"][0], x + 7 - tx)


class VolumeNeverJumpsTest(_AppBase):
    """P1 26. 9.: a touch on "+" or "Další" set the volume to 100 (Pi journal 20:35:29, 21:55:51).
    The owner: the volume must never change by a jump from the display."""

    def _volume_after(self, seconds=0.7):
        time.sleep(seconds)
        return self.fake.volume

    def test_pi_20_35_29_next_press_sliding_onto_the_bar(self):
        # dotyk: next na 333,252; panel.volume_drag x 333 y 252 x_end 405 moves 11 → was 35 → 100
        vol = self.fake.volume
        pts = [(333, 252), (336, 256), (340, 260), (346, 262), (352, 263), (360, 264),
               (368, 264), (378, 265), (388, 265), (396, 266), (402, 266), (405, 266)]
        self._gesture(pts, dt=0.318 / 11)
        self.assertEqual(self._volume_after(), vol)
        self.assertNotIn("volume", [c[0] for c in self.fake.controls])

    def test_pi_21_55_51_49ms_tap_on_the_bar_top_edge(self):
        # panel.volume_drag x 368 y 262 moves 0 press_ms 49 → was 45 → 100
        vol = self.fake.volume
        self.touch.feed("down", 368, 262)
        time.sleep(0.049)
        self.touch.feed("up", 368, 262)
        after = self._volume_after()
        self.assertLessEqual(abs(after - vol), 5)  # at most one step, like −/+
        self.assertNotEqual(after, 100)
        e = self.log.wait("panel.volume_drag")
        self.assertTrue(e)
        self.assertEqual(e[0]["mode"], "tap")

    def test_plus_pressed_at_its_left_edge_is_plus(self):
        vol = self.fake.volume
        self._gesture([(401, 290), (393, 291), (391, 290), (392, 289), (390, 290), (391, 291)], dt=0.03)
        self.assertEqual(self._volume_after(), vol + 5)

    def test_minus_pressed_at_its_right_edge_is_minus(self):
        vol = self.fake.volume
        self._gesture([(82, 290), (90, 291), (92, 290), (91, 289), (93, 290), (91, 291)], dt=0.03)
        self.assertEqual(self._volume_after(), vol - 5)

    def test_a_tap_on_the_bar_is_one_step_towards_the_finger(self):
        vol = self.fake.volume  # 65: the knob in the middle-right
        self.touch.tap(380, 290)  # the right end of the bar (would have been 100)
        self.assertEqual(self._volume_after(), vol + 5)
        self.touch.tap(180, 290)
        self.assertEqual(self._volume_after(), vol)

    def test_drag_away_from_the_knob_is_relative(self):
        vol = self.fake.volume
        x0 = knob_x(vol, 100) - 100  # far left of the knob
        self._gesture([(x0 + i * 5, 290) for i in range(9)], dt=0.03)  # 40 px to the right
        after = self._volume_after()
        self.assertGreater(after, vol)
        self.assertLessEqual(after, vol + round(40 * 100 / VOL_TRACK_W) + 1)  # as much as the finger moved

    def test_small_wobble_on_the_bar_changes_nothing_but_a_step(self):
        vol = self.fake.volume
        x = knob_x(vol, 100) + 60
        self._gesture([(x, 290), (x + 6, 291), (x - 5, 289), (x + 4, 290), (x - 3, 290)], dt=0.03)
        self.assertEqual(self._volume_after(), vol + 5)


class CalibrationFileTest(unittest.TestCase):
    """P2: a broken /etc/ytdj/panel-touch.json must not kill the panel or touch (Pi reports undervoltage)."""

    def _load(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cal.json"
            path.write_text(text)
            return kedei.Calibration.load(path)

    def test_broken_files_fall_back_to_the_default(self):
        for text in ("", '{"coef": [1, 0, 0, 0,', "[]", '{"coef": [1, 2]}', '{"coef": ["x", 0, 0, 0, 1, 0]}',
                     '{"coef": [1, 0, 0, 0, 1, NaN]}'):
            cal = self._load(text)
            self.assertEqual(cal.coef, kedei.Calibration.default().coef, text)
            self.assertEqual(cal.source, "default:corrupt", text)
            cal.map(2000, 2000)

    def test_bad_grid_keeps_the_affine_part(self):
        for grid in ('{"xs": [1, 2], "ys": [1, 2, 3], "d": []}',
                     '{"xs": [5, 5, 5], "ys": [1, 150, 300], "d": [[0, 0]]}',
                     '{"xs": [1, 200, 400], "ys": [1, 150, 300], "d": [[0, 0]]}',
                     '{"xs": [1, 200, 400], "ys": [1, 150, 300], "d": [[0, "a"], [0, 0], [0, 0], [0, 0], '
                     '[0, 0], [0, 0], [0, 0], [0, 0], [0, 0]]}',
                     '"grid"'):
            cal = self._load('{"coef": [1, 0, 3, 0, 1, 0], "grid": %s}' % grid)
            self.assertEqual(cal.coef, (1, 0, 3, 0, 1, 0), grid)
            self.assertIsNone(cal.grid, grid)
            self.assertEqual(cal.map(100, 100), (103, 100))

    def test_save_is_atomic_and_leaves_no_temp_file(self):
        cal, _ = kedei.recalibrate(kedei.Calibration(1, 0, 0, 0, 1, 0),
                                   [((round(bent_glass(x, y)[0]), round(bent_glass(x, y)[1])), (x, y))
                                    for x, y in POINTS])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cal.json"
            path.write_text("old")
            cal.save(path)
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["cal.json"])
            back = kedei.Calibration.load(path)
            self.assertTrue(back.source.startswith("file:"))
            self.assertEqual(back.grid, cal.grid)

    def test_touch_driver_starts_with_a_broken_file(self):
        saved = kedei._Device._instance
        try:
            kedei._Device._instance = _FakeDevice([])
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "cal.json"
                path.write_text("")
                t = kedei.KedeiTouch(calibration=kedei.Calibration.load(path))
                self.assertEqual(t.calibration_source, "default:corrupt")
        finally:
            kedei._Device._instance = saved


class CalibrationTest(unittest.TestCase):
    def setUp(self):
        self.saved = kedei._Device._instance

    def tearDown(self):
        kedei._Device._instance = self.saved

    def test_composes_on_top_of_the_current_calibration_and_saves(self):
        kedei._Device._instance = _FakeDevice([])
        base = kedei.Calibration(1, 0, 12, 0, 1, -9)  # the glass reads 12 px right, 9 px up
        t = kedei.KedeiTouch(calibration=base)
        pairs = [((x + 12, y - 9), (x, y)) for x, y in POINTS]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cal.json"
            err = t.apply_correction(pairs, path)
            self.assertLess(err, 1)
            self.assertEqual(t._cal.map(100, 100), (100, 100))
            self.assertTrue(json.loads(path.read_text())["coef"])
            self.assertEqual(kedei.Calibration.load(path).map(100, 100), (100, 100))

    def test_rotated_panel(self):
        kedei._Device._instance = _FakeDevice([])
        t = kedei.KedeiTouch(rotate=180, calibration=kedei.Calibration(1, 0, 12, 0, 1, -9))
        # what the panel sees (rotated) is 12 px left and 9 px down
        pairs = [((x - 12, y + 9), (x, y)) for x, y in POINTS]
        with tempfile.TemporaryDirectory() as tmp:
            t.apply_correction(pairs, Path(tmp) / "cal.json")
        x, y = t._cal.map(200, 100)
        self.assertEqual((kedei.WIDTH - 1 - x, kedei.HEIGHT - 1 - y), (kedei.WIDTH - 1 - 200, kedei.HEIGHT - 1 - 100))

    def test_points_that_dont_fit_change_nothing(self):
        kedei._Device._instance = _FakeDevice([])
        t = kedei.KedeiTouch(calibration=kedei.Calibration(1, 0, 0, 0, 1, 0))
        pairs = [((x, y), (x, y)) for x, y in POINTS[:4]] + [((400, 40), POINTS[4])]  # a slipped finger
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                t.apply_correction(pairs, Path(tmp) / "cal.json")
            self.assertFalse((Path(tmp) / "cal.json").exists())
        self.assertEqual(t._cal.coef, (1, 0, 0, 0, 1, 0))


def bent_glass(x, y):
    """A position-dependent error like the Pi's after the 5-point calibration (26. 9.):
    bottom-left reads high and right, bottom-right reads low and left."""
    fx, fy = x / 480, y / 320
    return x + 14 * (1 - fx) * fy - 12 * fx * fy, y - 26 * (1 - fx) * fy + 10 * fx * fy


class GridCalibrationTest(unittest.TestCase):
    """9 crosses: an affine correction plus a 3×3 grid of what's left."""

    CHECK = ((50, 289), (436, 289), (122, 230), (358, 230), (240, 100), (300, 20), (60, 20))

    def _pairs(self, points):
        return [((round(bent_glass(x, y)[0]), round(bent_glass(x, y)[1])), (x, y)) for x, y in points]

    def _worst(self, cal):
        return max(max(abs(a - b) for a, b in zip(cal.map_float(*bent_glass(x, y)), (x, y))) for x, y in self.CHECK)

    def test_nine_points_fix_what_five_cannot(self):
        ident = kedei.Calibration(1, 0, 0, 0, 1, 0)
        five = [(40, 40), (440, 40), (440, 280), (40, 280), (240, 160)]
        cal5, info5 = kedei.recalibrate(ident, self._pairs(five))
        cal9, info9 = kedei.recalibrate(ident, self._pairs(POINTS))
        self.assertEqual(len(POINTS), 9)
        self.assertIsNone(cal5.grid)
        self.assertIsNotNone(cal9.grid)
        self.assertGreater(self._worst(cal5), 5)  # an affine fit can't follow the bend
        self.assertLess(self._worst(cal9), 3.5)
        self.assertLess(info9["error"], 1.0)
        self.assertGreater(info9["affine_error"], 3)  # the bend is reported

    def test_grid_survives_save_and_load(self):
        cal, _ = kedei.recalibrate(kedei.Calibration(1, 0, 0, 0, 1, 0), self._pairs(POINTS))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cal.json"
            cal.save(path)
            back = kedei.Calibration.load(path)
        self.assertEqual(back.grid, cal.grid)
        self.assertEqual(back.map(50, 289), cal.map(50, 289))

    def test_recalibrating_again_does_not_stack_grids(self):
        ident = kedei.Calibration(1, 0, 0, 0, 1, 0)
        cal, _ = kedei.recalibrate(ident, self._pairs(POINTS))
        # measured with the new calibration, the points are already right
        again = [((round(cal.map_float(*bent_glass(x, y))[0]), round(cal.map_float(*bent_glass(x, y))[1])), (x, y))
                 for x, y in POINTS]
        cal2, info = kedei.recalibrate(cal, again)
        self.assertLess(self._worst(cal2), 3.5)

    def test_a_slipped_point_among_nine_is_refused(self):
        pairs = self._pairs(POINTS)
        (mx, my), t = pairs[4]
        pairs[4] = ((mx + 60, my - 50), t)
        with self.assertRaises(ValueError):
            kedei.recalibrate(kedei.Calibration(1, 0, 0, 0, 1, 0), pairs)


class TouchTestScreenTest(unittest.TestCase):
    def setUp(self):
        self.log = EventLog()
        self.server, self.fake = make_server(0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.screen = SimScreen(Path(self.tmp.name) / "p.png")
        self.touch = SimTouch(io.StringIO(""))
        self.app = PanelApp(self.screen, self.touch, f"http://127.0.0.1:{self.server.server_address[1]}",
                            net_backend=_NoNet())
        self.app.page_guard = 0.0
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
        self.log.close()

    def test_shows_the_reading_presses_nothing_and_goes_back(self):
        from ytdj.panel.netui import TEST_BTN
        from ytdj.panel.touchtest import BACK_BOX
        from ytdj.panel.ui import NET_TARGET

        self.touch.tap(*center(NET_TARGET))
        self.assertTrue(wait_for(lambda: self.app._shown_page == "overview"))
        self.touch.tap(*center(TEST_BTN))
        self.assertTrue(wait_for(lambda: self.app._shown_page == "touchtest"))
        before, paused = self.fake.index, self.fake.paused
        x, y = center(NEXT)
        self.touch.feed("down", x, y)
        self.assertTrue(wait_for(lambda: self.app.touchtest.view().cursor == (x, y)))
        px = self.screen.pushed_px
        self.touch.feed("move", x + 6, y + 3)
        self.assertTrue(wait_for(lambda: self.app.touchtest.view().cursor == (x + 6, y + 3)))
        time.sleep(0.1)
        # two crosshairs and the info line (≈ 10 000 px), not a frame (153 600)
        self.assertLess(self.screen.pushed_px - px, 12000)
        self.touch.feed("up", x + 6, y + 3)
        self.assertTrue(wait_for(lambda: self.app.touchtest.view().lit == "next"))
        self.assertIn("Další", self.app.touchtest.view().info)
        time.sleep(0.3)
        self.assertEqual((self.fake.index, self.fake.paused), (before, paused))  # nothing pressed
        self.touch.tap(*center(BACK_BOX))
        self.assertTrue(wait_for(lambda: self.app._shown_page == "player"))
        self.assertTrue(self.log.wait("panel.touch_test", phase="end"))


class HeaderReachTest(unittest.TestCase):
    def test_header_buttons_reach_the_top_edge(self):
        from ytdj.panel.netui import NetView
        from ytdj.panel.netui import targets as net_targets
        from ytdj.panel.wishui import WishView
        from ytdj.panel.wishui import targets as wish_targets

        for page in ("home", "queue"):
            t = wish_targets(WishView(page=page))
            self.assertEqual(nearest(t, 30, 0)[0], "back", page)
            self.assertEqual(nearest(t, 30, 49)[0], "back", page)
        self.assertEqual(nearest(wish_targets(WishView(page="home")), 420, 2)[0], "queue")
        for page in ("overview", "list"):
            self.assertEqual(nearest(net_targets(NetView(page=page, loaded=True)), 30, 1)[0], "back", page)
        for name in ("net", "wish", "phone"):
            box = TARGETS[name]
            self.assertEqual(nearest(TARGETS, (box[0] + box[2]) // 2, 0)[0], name)
            self.assertEqual(nearest(TARGETS, (box[0] + box[2]) // 2, 52)[0], name)


class CalibrationFlowTest(unittest.TestCase):
    """The whole flow on the panel: network screen → five crosses → taps land right."""

    def setUp(self):
        self.log = EventLog()
        self.server, self.fake = make_server(0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.touch = SimTouch(io.StringIO(""))
        self.touch.offset = (14, -11)  # a calibration that's off by this much
        self.app = PanelApp(SimScreen(Path(self.tmp.name) / "p.png"), self.touch,
                            f"http://127.0.0.1:{self.server.server_address[1]}", net_backend=_NoNet())
        self.app.page_guard = 0.0
        self.thread = threading.Thread(target=self.app.run, daemon=True)
        self.thread.start()
        self.assertTrue(wait_for(lambda: self.app.online))

    def tearDown(self):
        self.app.shutdown()
        self.thread.join(3)
        self.server.closing = True
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()
        self.log.close()

    def _hold(self, x, y, jitter=((0, 0), (2, -1), (-1, 2), (1, 1), (0, -2), (6, 5))):
        self.touch.feed("down", x + jitter[0][0], y + jitter[0][1])
        for dx, dy in jitter[1:]:
            time.sleep(0.01)
            self.touch.feed("move", x + dx, y + dy)
        time.sleep(0.01)
        self.touch.feed("up", x, y)

    def _open(self):
        from ytdj.panel.netui import CAL_BTN
        from ytdj.panel.ui import NET_TARGET

        self.touch.tap(*center(NET_TARGET))
        self.assertTrue(wait_for(lambda: self.app._shown_page == "overview"))
        self.touch.tap(*center(CAL_BTN))
        self.assertTrue(wait_for(lambda: self.app._shown_page == "calib"))

    def test_calibrate_then_taps_land_where_aimed(self):
        self._open()
        for i, (x, y) in enumerate(POINTS):
            self._hold(x, y)
            self.assertTrue(wait_for(lambda: self.app.calib.step == i + 1 or self.app.calib.phase != "points"))
        self.assertTrue(wait_for(lambda: self.app.calib.phase == "done"), self.app.calib.detail)
        saved = self.log.wait("panel.calibration", phase="saved")
        self.assertTrue(saved)
        self.assertGreaterEqual(saved[0]["shift"], 13)
        # the offset is gone: a tap aimed at Hrát's centre arrives there
        got = []
        self.touch.feed = self._spy(self.touch.feed, got)
        self.touch.tap(*center(PLAY))
        x, y = got[0]
        self.assertLessEqual(abs(x - center(PLAY)[0]) + abs(y - center(PLAY)[1]), 2)

    def _spy(self, feed, got):
        touch = self.touch

        def spy(kind, x, y):
            feed(kind, x, y)
            ev = touch.q.queue[-1]
            got.append((ev.x, ev.y))
        return spy

    def test_nine_crosses_straighten_a_bent_glass(self):
        self.touch.offset = (0, 0)
        self.touch.distort = bent_glass
        self._open()
        for i, (x, y) in enumerate(POINTS):
            self._hold(x, y)
            self.assertTrue(wait_for(lambda: self.app.calib.step == i + 1 or self.app.calib.phase != "points"))
        self.assertTrue(wait_for(lambda: self.app.calib.phase == "done"), self.app.calib.detail)
        saved = self.log.wait("panel.calibration", phase="saved")
        self.assertEqual(saved[0]["n"], 9)
        self.assertIsNotNone(saved[0]["grid_max"])
        # the owner's case: aiming at the top of −volume (y 270) reads there, not in Hrát
        got = []
        self.touch.feed = self._spy(self.touch.feed, got)
        self.touch.tap(44, 270)
        self.assertLessEqual(abs(got[0][1] - 270), 3)

    def test_a_slipped_point_changes_nothing_and_offers_again(self):
        self._open()
        for i, (x, y) in enumerate(POINTS):
            if i == 2:
                x, y = x - 150, y - 90  # slipped far off
            self._hold(x, y)
            self.assertTrue(wait_for(lambda: self.app.calib.step == i + 1 or self.app.calib.phase != "points"))
        self.assertTrue(wait_for(lambda: self.app.calib.phase == "failed"))
        self.assertIsNone(self.touch.correction)
        from ytdj.panel.calib import BTN_L

        self.touch.tap(*center(BTN_L))  # Zrušit
        self.assertTrue(wait_for(lambda: self.app._shown_page == "player"))

    def test_leaving_it_alone_gives_up(self):
        from ytdj.panel import calib

        self._open()
        old = calib.IDLE_CANCEL
        calib.IDLE_CANCEL = 0.3
        try:
            self.app.events.put(("wake",))
            self.assertTrue(wait_for(lambda: self.app._shown_page == "player"))
        finally:
            calib.IDLE_CANCEL = old
        self.assertIsNone(self.touch.correction)
        self.assertTrue(self.log.wait("panel.calibration", phase="abandoned"))


class _ExDevice:
    """The KeDei bus with kd_touch_ex: (code, (x, y, z1, z2, spread x, spread y)) per sample."""

    def __init__(self, samples):
        self.samples = list(samples)
        self.pen = False

    def init(self, madctl):
        pass

    def touch_ex(self):
        item = self.samples.pop(0) if self.samples else (0, (0, 0, 0, 0, 0, 0))
        code, v = item
        self.pen = code not in (0, -1)
        return code, v

    def touch_raw(self):  # not used when touch_ex exists
        raise AssertionError("touch_ex should be used")

    def pen_down(self):
        return self.pen


class DriverDiagnosticsTest(unittest.TestCase):
    """Why samples are refused (invalid_* counters), raw Z1/Z2 for the finger test, the prepared filter."""

    def setUp(self):
        self.saved = kedei._Device._instance

    def tearDown(self):
        kedei._Device._instance = self.saved

    def _touch(self, samples, env_filter=None):
        kedei._Device._instance = _ExDevice(samples)
        t = kedei.KedeiTouch(calibration=kedei.Calibration(1, 0, 0, 0, 1, 0))
        t.DOWN_INTERVAL = t.IDLE_INTERVAL = 0.0
        if env_filter is not None:
            t.contact_filter = kedei.contact_filter_from_env(env_filter)
        return t

    @staticmethod
    def _run(t, n):
        evs = []
        for _ in range(n):
            ev = t.poll(0.0)
            if ev:
                evs.append(ev)
        return evs

    def test_invalid_reasons_are_counted(self):
        ok = (1, (300, 230, 600, 3000, 20, 20))
        t = self._touch([ok, (-3, (300, 230, 600, 3000, 400, 20)), ok, (-2, (10, 4090, 20, 4000, 0, 0)),
                         ok, ok, ok, (0, (0,) * 6), (0, (0,) * 6), (0, (0,) * 6)])
        evs = self._run(t, 14)
        self.assertEqual([e.kind for e in evs], ["down", "up"])
        st = t.take_stats()
        self.assertEqual((st["invalid_samples"], st["invalid_spread"], st["invalid_rest"]), (2, 1, 1))

    def test_recording_keeps_raw_pressure_and_positions(self):
        ok = (1, (300, 230, 600, 3000, 20, 20))
        t = self._touch([ok, ok, (-3, (305, 231, 590, 3100, 300, 10)), ok] + [(0, (0,) * 6)] * 3)
        t.start_recording()
        self._run(t, 10)
        rec = t.take_recording()
        self.assertEqual([r["c"] for r in rec], [1, 1, -3, 1])
        self.assertEqual(rec[0]["raw"], [300, 230, 600, 3000, 20, 20])
        self.assertEqual((rec[0]["x"], rec[0]["y"]), (300, 230))
        self.assertNotIn("x", rec[2])
        t.stop_recording()
        self.assertEqual(t.take_recording(), [])

    def test_contact_filter_is_off_unless_configured(self):
        self.assertIsNone(kedei.contact_filter_from_env(""))
        self.assertIsNone(kedei.contact_filter_from_env("nonsense"))
        f = kedei.contact_filter_from_env("0.1:1.0")
        self.assertTrue(f((2048, 0, 1000, 2000, 0, 0)))  # R = 0.5·(2 − 1) = 0.5
        self.assertFalse(f((2048, 0, 1000, 9000, 0, 0)))  # R = 4.0

    def test_contact_filter_drops_samples_when_on(self):
        good = (1, (2048, 230, 1000, 2000, 10, 10))
        dual = (1, (2048, 900, 1000, 4000, 10, 10))  # R 1.5 (z still above MIN_PRESSURE)
        t = self._touch([good, good, dual, dual, good] + [(0, (0,) * 6)] * 3, env_filter="0.1:1.0")
        evs = self._run(t, 12)
        self.assertEqual([e.kind for e in evs], ["down", "up"])
        self.assertEqual(t.take_stats().get("dual_rejected"), 2)


class FingerTestFlowTest(_AppBase):
    def test_twelve_spots_log_a_probe_each(self):
        from ytdj.panel.fingertest import SPOTS
        from ytdj.panel.netui import PROBE_BTN
        from ytdj.panel.ui import NET_TARGET

        self.touch.tap(*center(NET_TARGET))
        self.assertTrue(wait_for(lambda: self.app._shown_page == "overview"))
        self.touch.tap(*center(PROBE_BTN))
        self.assertTrue(wait_for(lambda: self.app._shown_page == "fingertest"))
        before = self.fake.index
        for i, (x, y) in enumerate(SPOTS):
            # the owner's finger at the top edge reads lower: simulate it there
            ry = y + 60 if y <= 20 else y
            self._gesture([(x, ry), (x + 2, ry + 1), (x - 1, ry), (x, ry + 2), (x + 1, ry - 1)], dt=0.02)
            self.assertTrue(wait_for(lambda: self.app.fingertest.step == i + 1))
        self.assertTrue(wait_for(lambda: self.app.fingertest.phase == "done"))
        probes = self.log.wait("panel.touch_probe")
        self.assertEqual(len(probes), 12)
        top = probes[0]
        self.assertEqual(top["target"], list(SPOTS[0]))
        self.assertAlmostEqual(top["err"][1], 60, delta=2)
        self.assertIsNone(top.get("aim"))  # (40, 10) is the status text, no button (None isn't logged)
        self.assertEqual(probes[1]["aim"], "phone")
        self.assertEqual(probes[2]["aim"], "wish")
        self.assertEqual(probes[1]["button"], "queue")  # read 60 px lower: the owner's "Fronta"
        self.assertEqual(self.fake.index, before)  # nothing pressed meanwhile
        run = self.log.wait("panel.touch_probe_run", phase="done")
        self.assertAlmostEqual(run[0]["top_err"], 60, delta=2)
        from ytdj.panel.fingertest import BTN_DONE

        self.touch.tap(*center(BTN_DONE))
        self.assertTrue(wait_for(lambda: self.app._shown_page == "player"))


class ProbeReportTest(unittest.TestCase):
    def test_per_spot_error_spread_and_pressure(self):
        from ytdj.panel.touchreport import probe_report

        def probe(target, dy, r_z2):
            samples = [{"t": i * 15, "c": 1, "raw": [2048, 100, 1000, r_z2, 20, 20], "x": target[0],
                        "y": target[1] + dy + (i % 3)} for i in range(8)]
            samples.append({"t": 130, "c": -3, "raw": [2048, 100, 1000, r_z2, 400, 20]})
            return {"kind": "panel.touch_probe", "target": list(target), "err": [0, dy], "button": "x",
                    "samples": samples}

        text = probe_report([probe((282, 10), 70, 1400), probe((240, 160), 2, 3000)])
        self.assertIn("282,10", text)
        self.assertIn("+70", text)
        self.assertIn("rozptyl 1", text)
        self.assertIn("top edge: median dy +70 px, touch resistance R 0.20", text)
        self.assertIn("rest: median dy +2 px, touch resistance R 1.00", text)
        self.assertIn("no panel.touch_probe", probe_report([]))


class TallStripProposalTest(unittest.TestCase):
    def test_off_by_default_and_renders_when_on(self):
        import subprocess

        from ytdj.panel import ui

        self.assertEqual(ui.STRIP_H, 32)  # the look the owner has now
        code = ("import sys; sys.path.insert(0, %r); from ytdj.panel import ui; "
                "r = ui.Renderer(); r.render(ui.View(online=True, connecting=False, has_track=True, title='X', "
                "artist='Y', running=True, duration=100, can_next=True), full=True); "
                "print(ui.STRIP_H, ui.NET_BTN, ui.ART[1], ui.Renderer.LINE_Y)") % str(Path(__file__).resolve().parents[1])
        out = subprocess.run([sys.executable, "-c", code], env={"YTDJ_PANEL_TALL_STRIP": "1", "PATH": "/usr/bin"},
                             capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(out[0], "46")
        self.assertEqual(out[-2], "48")


class TouchReportTest(unittest.TestCase):
    def test_bias_and_rates(self):
        from ytdj.panel.touchreport import load, report

        ev = [{"kind": "panel.touch_driver", "ts": "2026-09-26T12:00:00+02:00", "downs": 20, "aborted_press": 5,
               "spread_rejected": 7, "period_s": 60}]
        ev += [{"kind": "panel.action", "ts": "2026-09-26T12:00:01+02:00", "source": "touch", "button": "next",
                "off": [11, 9], "press_ms": 150}] * 12
        ev += [{"kind": "panel.touch_miss", "ts": "2026-09-26T12:00:02+02:00", "page": "player", "near": "play",
                "dist": 14, "dx": 30, "dy": 40}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "e.jsonl"
            path.write_text("".join(json.dumps(e) + "\n" for e in ev) + "not json\n")
            text = report(load(str(path)))
        self.assertIn("20 presses from 25 landings (80 %)", text)
        self.assertIn("actions from touch: 12 (60 % of presses)", text)
        self.assertIn("median shift x +11 px, y +9 px", text)
        self.assertIn("Kalibrace dotyku", text)
        self.assertIn("closest to play 1", text)


class DriverRebuildTest(unittest.TestCase):
    """Pi 26. 9. 23:50: rsync přenesl starší čas zdroje, novější stará .so zůstala
    a dotyk padal na chybějícím kd_touch_ex. Překládá se podle obsahu, ne času."""

    def test_rebuilds_when_content_changes_even_if_source_is_older(self):
        import os
        import shutil
        from unittest import mock

        from ytdj.panel import kedei

        if not shutil.which("cc"):
            self.skipTest("cc není k dispozici")
        d = Path(tempfile.mkdtemp())
        src = d / "kedei.c"
        src.write_text("int kd_old(void) { return 1; }\n")
        with mock.patch.object(kedei, "SOURCE", src), mock.patch.object(kedei, "LIB_DIR", d / "lib"):
            so = kedei._build_lib()
            self.assertIn(b"kd_old", so.read_bytes())
            # nový obsah se starším časem než knihovna (jako po rsync -a)
            src.write_text("int kd_new(void) { return 2; }\n")
            past = so.stat().st_mtime - 3600
            os.utime(src, (past, past))
            so = kedei._build_lib()
            self.assertIn(b"kd_new", so.read_bytes())
            # beze změny obsahu se nic nepřekládá
            before = so.stat().st_mtime_ns
            kedei._build_lib()
            self.assertEqual(so.stat().st_mtime_ns, before)


if __name__ == "__main__":
    unittest.main()
