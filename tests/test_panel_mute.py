"""Displej: ztlumeno z webu je na displeji vidět a sáhnutí na hlasitost zvuk vrátí.

    python3 -m unittest tests.test_panel_mute -v      (systémový python3, jako ostatní testy panelu)

POZADAVKY #70 („možnost ztlumit zvuk jedním kliknutím"), F-HLAS-09.
"""

from __future__ import annotations

import io
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

if not os.environ.get("YTDJ_EVENTS_FILE"):
    _EVENTS_TMP = tempfile.TemporaryDirectory()
    os.environ["YTDJ_EVENTS_FILE"] = str(Path(_EVENTS_TMP.name) / "events.jsonl")
os.environ.setdefault("YTDJ_PANEL_ART_URL", "")

from fake_ytdj import make_server  # noqa: E402
from ytdj.panel import keys  # noqa: E402
from ytdj.panel.app import PanelApp  # noqa: E402
from ytdj.panel.sim import SimScreen, SimTouch  # noqa: E402
from ytdj.panel.ui import VOL, VOL_DOWN, Renderer, View  # noqa: E402

BASE = View(online=True, connecting=False, has_track=True, title="Holky z naší školky",
            artist="Olympic", running=True, elapsed=83, duration=214, volume=65, can_next=True)


def wait_for(cond, timeout: float = 5.0, step: float = 0.02) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return False


class MutedOnTheGlass(unittest.TestCase):
    def test_muted_changes_only_the_volume_bar_and_keeps_the_level(self):
        r = Renderer()
        r.render(BASE, full=True)
        plain = r.frame.copy()
        boxes = r.render(replace(BASE, muted=True))
        self.assertTrue(boxes)  # je to vidět
        for b in boxes:  # a jen v pruhu hlasitosti
            self.assertTrue(VOL[0] <= b[0] and b[2] <= VOL[2] and VOL[1] <= b[1] and b[3] <= VOL[3], b)
        muted = r.frame.copy()
        self.assertNotEqual(plain.tobytes(), muted.tobytes())
        # knoflík stojí na stejném místě (úroveň 65 zůstává), jen zešedl: jiná
        # úroveň by lištu překreslila jinak než ztlumení téže úrovně
        r2 = Renderer()
        r2.render(replace(BASE, muted=True, volume=30), full=True)
        self.assertNotEqual(r2.frame.tobytes(), muted.tobytes())
        # zpátky se zvukem: přesně jako předtím
        r.render(BASE)
        self.assertEqual(r.frame.tobytes(), plain.tobytes())

    def test_offline_draws_as_before(self):
        a, b = Renderer(), Renderer()
        a.render(replace(BASE, online=False), full=True)
        b.render(replace(BASE, online=False, muted=True), full=True)
        self.assertEqual(a.frame.tobytes(), b.frame.tobytes())

    def test_hardware_mute_key_is_left_as_it_was(self):
        # tlačítko ztlumení na repráku dál pozastavuje (beze změny — o jeho
        # přepnutí na ztlumení rozhodne vlastník)
        self.assertEqual(keys.ACTIONS[keys.KEY_MUTE], "play")


class MutedEndToEnd(unittest.TestCase):
    def setUp(self):
        self.server, self.fake = make_server(0)
        port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.touch = SimTouch(io.StringIO(""))
        self.app = PanelApp(SimScreen(Path(self.tmp.name) / "panel.png"), self.touch,
                            f"http://127.0.0.1:{port}")
        self.app.page_guard = 0.0
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

    def test_mute_from_the_web_shows_and_a_volume_tap_brings_the_sound_back(self):
        self.assertFalse(self.app._view().muted)
        self.assertEqual(self.fake.control({"action": "mute", "value": True})[0], 200)  # jako z webu
        self.assertTrue(wait_for(lambda: self.app._view().muted), "displej neukázal ztlumení")
        self.assertEqual(self.app._view().volume, 65)  # úroveň zůstala
        self.assertTrue(self.app._view().running)  # hudba běží dál
        x, y = (VOL_DOWN[0] + VOL_DOWN[2]) // 2, (VOL_DOWN[1] + VOL_DOWN[3]) // 2
        self.touch.tap(x, y, hold=0.15)  # − na displeji: 65 → 60 a zvuk zpět
        self.assertTrue(wait_for(lambda: self.fake.volume == 60 and not self.fake.muted))
        self.assertTrue(wait_for(lambda: not self.app._view().muted))


if __name__ == "__main__":
    unittest.main()
