"""Displej: DJ se ptá, co bylo přáním myšleno — odpověď jedním ťuknutím.

    python3 -m unittest tests.test_panel_ask -v      (systémový python3, jako ostatní testy panelu)

POZADAVKY #64: přání poslané z displeje, které čeká na upřesnění, ukáže na
obrazovce odpovědi otázku a 2–3 velká tlačítka; cizí přání (z webu) jen čeká.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if not os.environ.get("YTDJ_EVENTS_FILE"):
    _EVENTS_TMP = tempfile.TemporaryDirectory()
    os.environ["YTDJ_EVENTS_FILE"] = str(Path(_EVENTS_TMP.name) / "events.jsonl")
os.environ.setdefault("YTDJ_PANEL_ART_URL", "")

from ytdj.panel.ui import H, W  # noqa: E402
from ytdj.panel.wishapp import WishController  # noqa: E402
from ytdj.panel.wishui import ACTIVE, S_MAIN, WishRenderer, ask_box, targets  # noqa: E402

OPTIONS = ["písnička Lucie — Wanastowi Vjecy", "kapela Lucie", "album Lucie (živě)"]


def asking(rid: str = "r1", options=OPTIONS, state: str = "asking") -> dict:
    return {"requests": [{"id": rid, "state": state, "state_cs": "čeká na upřesnění",
                          "text": "Lucie", "who": "displej", "question": "Myslíš písničku, nebo kapelu?",
                          "options": list(options), "ask_s": 20,
                          "reply": "Myslíš písničku, nebo kapelu?"}]}


def controller(mine: bool = True) -> tuple[WishController, list]:
    sent: list = []
    c = WishController(None, lambda m: None)
    c._action = lambda rid, action, **extra: sent.append((rid, action, extra))  # žádná síť
    c.open(0.0)
    c.page, c.phase, c.req_id, c.wish = "sent", "busy", "r1", "Lucie"
    if mine:
        c.mine["r1"] = "token"
    return c, sent


class AskOnThePanel(unittest.TestCase):
    def test_question_comes_up_with_tappable_options(self):
        c, sent = controller()
        self.assertTrue(c.on_state(asking()))  # ukázat — jako odpověď DJe
        self.assertEqual(c.phase, "ask")
        v = c.view(1.0)
        self.assertEqual(v.question, "Myslíš písničku, nebo kapelu?")
        self.assertEqual(v.options, tuple(OPTIONS))
        t = targets(v)
        self.assertEqual(sorted(t), ["leave", "opt0", "opt1", "opt2"])
        self.assertFalse(c.on_state(asking()))  # tentýž stav znovu nic nedělá
        self.assertEqual(c._close_at(), float("inf"))  # otázka nezmizí sama (ukončí ji server)
        c._fire("opt1", 2.0)
        self.assertEqual(sent, [("r1", "answer", {"choice": 1})])
        self.assertEqual(c.phase, "busy")  # DJ teď řadí vybraný výklad
        self.assertEqual(c.view(2.0).options, ())
        # server potvrdí: zařazeno
        self.assertTrue(c.on_state({"requests": [{"id": "r1", "state": "queued", "reply": "Zařadil jsem…"}]}))
        self.assertEqual(c.phase, "queued")

    def test_no_tap_and_the_dj_decides_itself(self):
        c, sent = controller()
        c.on_state(asking())
        c._fire("leave", 2.0)  # "Nevím — ať vybere DJ": zpět k přehrávači, přání běží dál
        self.assertEqual(sent, [])
        # po čase server vybral sám a rovnou zařadil → odpověď se ukáže jako vždy
        self.assertTrue(c.on_state({"requests": [{"id": "r1", "state": "queued",
                                                  "reply": "Nevěděl jsem, jestli myslíš…"}]}))
        self.assertEqual(c.phase, "queued")
        self.assertIn("Nevěděl jsem", c.reply)

    def test_somebody_elses_waiting_wish_cannot_be_answered_here(self):
        c, sent = controller(mine=False)
        self.assertFalse(c.on_state(asking()))
        self.assertEqual(c.phase, "busy")
        self.assertNotIn("opt0", targets(c.view(1.0)))
        self.assertIn("asking", ACTIVE)  # ve frontě je vidět jako rozpracované ("čeká na upřesnění")
        c.page = "queue"
        self.assertEqual(c.queue_rows()[0].label, "čeká na upřesnění")

    def test_buttons_are_finger_sized_and_inside_the_answer_area(self):
        for n in (2, 3):
            boxes = [ask_box(i, n) for i in range(n)]
            for l, t, r, b in boxes:
                self.assertTrue(0 <= l < r <= W and S_MAIN[1] <= t < b <= S_MAIN[3] <= H)
                self.assertGreaterEqual(b - t, 32)
                self.assertGreaterEqual(r - l, 400)
            for a, c in zip(boxes, boxes[1:]):
                self.assertLessEqual(a[3], c[1])  # nepřekrývají se

    def test_screen_draws_the_question_and_the_pressed_option(self):
        c, _ = controller()
        c.on_state(asking(options=OPTIONS[:2]))
        r = WishRenderer()
        r.render(c.view(1.0), full=True)
        plain = r.frame.crop(S_MAIN).tobytes()
        c.pressed, c.inside = "opt0", True
        r.render(c.view(1.0), full=True)
        self.assertNotEqual(r.frame.crop(S_MAIN).tobytes(), plain)
        # a bez otázky vypadá obrazovka jinak (DJ vybírá)
        c2, _ = controller()
        r.render(c2.view(1.0), full=True)
        self.assertNotEqual(r.frame.crop(S_MAIN).tobytes(), plain)


if __name__ == "__main__":
    unittest.main()
