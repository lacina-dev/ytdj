"""Displej: kdo přeskočil skladbu, se na pár vteřin ukáže v horním pruhu.

    python3 -m unittest tests.test_panel_skip -v      (systémový python3, jako ostatní testy panelu)

Hlášení z jukeboxu #23, POZADAVKY #76, F-PRESKOK-10.
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
from ytdj.panel.app import PanelApp  # noqa: E402
from ytdj.panel.sim import SimScreen, SimTouch  # noqa: E402
from ytdj.panel.ui import TOAST, Renderer, View  # noqa: E402

BASE = View(online=True, connecting=False, has_track=True, title="Holky z naší školky",
            artist="Olympic", running=True, elapsed=83, duration=214, volume=65, can_next=True)


def wait_for(cond, timeout: float = 5.0, step: float = 0.02) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return False


def notice(i: int, who: str = "Karel", src: str = "web", n: int = 1) -> dict:
    return {"id": i, "kind": "skip", "at": time.time(), "who": who, "who_key": "t-abc", "src": src,
            "n": n, "title": "Pohoda", "artist": "Kabát", "text": f"{who} · přeskočeno: Pohoda (Kabát)"}


class SkipBanner(unittest.TestCase):
    def test_banner_is_drawn_in_the_strip_and_only_there(self):
        r = Renderer()
        r.render(BASE, full=True)
        plain = r.frame.copy()
        boxes = r.render(replace(BASE, toast=("Karel", "Pohoda (Kabát)", "", " · přeskočeno: ")))
        self.assertTrue(boxes)
        for b in boxes:
            self.assertTrue(b[1] >= TOAST[1] and b[3] <= TOAST[3], b)
        self.assertNotEqual(plain.tobytes(), r.frame.tobytes())
        # jiný text než u přání ("si přeje:") — je to jiná kresba
        r2 = Renderer()
        r2.render(replace(BASE, toast=("Karel", "Pohoda (Kabát)", "")), full=True)
        self.assertNotEqual(r2.frame.tobytes(), r.frame.tobytes())
        r.render(BASE)
        self.assertEqual(r.frame.tobytes(), plain.tobytes())  # a zase zmizí beze stopy


class SkipEndToEnd(unittest.TestCase):
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

    def start(self):
        self.thread.start()
        self.assertTrue(wait_for(lambda: self.app.online), "panel se nepřipojil")

    def tearDown(self):
        self.app.shutdown()
        self.thread.join(3)
        self.server.closing = True
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def test_skip_from_the_web_shows_who_and_what_for_a_few_seconds(self):
        self.start()
        self.assertEqual(self.app._view().toast, ())
        self.fake.notices = [notice(1)]
        self.assertTrue(wait_for(lambda: self.app._view().toast), "displej hlášku neukázal")
        self.assertEqual(self.app._view().toast, ("Karel", "Pohoda (Kabát)", "", " · přeskočeno: "))
        # série: jedna hláška s počtem
        self.fake.notices = [notice(2, n=5)]
        self.assertTrue(wait_for(lambda: "5×" in (self.app._view().toast or ("",) * 4)[3]))
        self.assertEqual(self.app._view().toast[3], " · přeskočeno 5×: ")
        # sama zmizí, i když ji stav ještě nese
        self.assertTrue(wait_for(lambda: self.app._view().toast == (), timeout=8))
        time.sleep(1.2)
        self.assertEqual(self.app._view().toast, ())  # a tatáž se podruhé neukáže

    def test_old_notice_at_start_and_own_skips_are_not_announced(self):
        self.fake.notices = [notice(7)]  # visela ve stavu už před startem displeje
        self.start()
        time.sleep(1.2)
        self.assertEqual(self.app._view().toast, ())
        # přeskočení z displeje nebo tlačítkem na repráku: kdo u něj stojí, to ví
        self.fake.notices = [notice(8, who="displej", src="panel")]
        time.sleep(1.2)
        self.assertEqual(self.app._view().toast, ())
        self.fake.notices = [notice(9, who="tlačítko na repráku", src="key")]
        time.sleep(1.2)
        self.assertEqual(self.app._view().toast, ())
        self.fake.notices = [notice(10, who="Jana")]
        self.assertTrue(wait_for(lambda: self.app._view().toast[:1] == ("Jana",)))

    def test_speaker_key_tells_the_server_where_the_skip_came_from(self):
        self.start()
        self.app.events.put(("key", "next"))  # tlačítko Další na repráku
        self.assertTrue(wait_for(lambda: ("next", None) in self.fake.controls))
        body = [b for b in self.fake.control_bodies if b.get("action") == "next"][-1]
        self.assertEqual(body.get("source"), "key")
        time.sleep(0.5)
        # dotyk na displeji posílá Další bez toho
        from ytdj.panel.ui import NEXT
        self.touch.tap((NEXT[0] + NEXT[2]) // 2, (NEXT[1] + NEXT[3]) // 2)
        self.assertTrue(wait_for(lambda: len([c for c in self.fake.controls if c[0] == "next"]) == 2))
        self.assertNotIn("source", self.fake.control_bodies[-1])


if __name__ == "__main__":
    unittest.main()
