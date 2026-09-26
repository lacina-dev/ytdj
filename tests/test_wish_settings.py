"""Počty skladeb přání v nastavení (F-FRONTA-18) a odkud je rádio (F-FRONTA-19).

Vlastník 26. 9.: „Když mám přání a DJ mi dá nějaké skladby, teď jsou tři.
Proč jen tři? Jde to nastavit v nastavení? A co je to, co hraje dál a nemá
to už u sebe moje jméno?"
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import test_wishes as tw  # noqa: E402
from test_wishes import Rig, W, WebApp, body, fair_order, req, run, whos  # noqa: E402
from ytdj.config import DEFAULTS, Config  # noqa: E402
from ytdj.nicks import tag_of  # noqa: E402
from ytdj.web import server as web  # noqa: E402
from ytdj.wishes import Amounts, Turns  # noqa: E402

INDEX = Path(__file__).resolve().parents[1] / "ytdj/web/static/index.html"
KEYS = ("wish_block", "wish_shared_block", "wish_budget", "wish_budget_panel", "wish_artist_max")


class WishAmounts(unittest.TestCase):
    """F-FRONTA-18: počty skladeb přání jdou změnit v nastavení; výchozí = F-FRONTA-01/02."""

    def test_defaults_are_the_rules(self):
        a = Amounts.of(Config(**DEFAULTS))
        # F-FRONTA-01: kolo 3, když čekají jiní 2; F-FRONTA-02: 4 skladby, z displeje 3
        self.assertEqual((a.block, a.shared, a.budget, a.panel_budget, a.artist_max), (3, 2, 4, 3, 12))
        self.assertEqual(Amounts.of(object()), Amounts())  # bez klíčů (starý config) taky

    def test_bad_values_are_clamped(self):
        cfg = Config(**{**DEFAULTS, "wish_block": 2, "wish_shared_block": 5, "wish_budget": 0,
                        "wish_budget_panel": 99, "wish_artist_max": "x"})
        a = Amounts.of(cfg)
        self.assertEqual((a.block, a.shared, a.budget, a.panel_budget, a.artist_max), (2, 2, 1, 10, 12))

    def test_fair_order_follows_the_amounts(self):
        petr = W("P", 20, 1, kind="artist")
        jana, karel = W("J", 1, 2), W("K", 1, 3)
        self.assertEqual(whos(fair_order([petr, jana, karel], Turns()))[:4], "PPJK")
        self.assertEqual(whos(fair_order([petr, jana, karel], Turns(), amounts=Amounts(shared=1)))[:3], "PJK")
        self.assertEqual(whos(fair_order([petr, jana, karel], Turns(),
                                         amounts=Amounts(block=4, shared=3)))[:5], "PPPJK")
        # rozpočet: displej má 3, s nastavením 5 hraje dál před Robertem
        disp = W("D", 12, 1, kind="artist")
        disp.source = "panel"
        disp.played = 3
        rob = W("R", 2, 2)
        self.assertEqual(whos(fair_order([disp, rob], Turns()))[:3], "RRD")
        self.assertEqual(whos(fair_order([disp, rob], Turns(), amounts=Amounts(panel_budget=5)))[:2], "DD")

    def test_settings_change_the_queue_live(self):
        """Nastavení z webu platí hned: přání nálady má tolik skladeb, kolik je v kole."""
        async def go():
            async with Rig() as rig:
                srv = web.WebServer(WebApp(rig))
                with mock.patch.object(web.cfgmod, "save_values"):
                    resp = await srv._config_post(req({"wish_block": 5, "wish_shared_block": 3,
                                                       "wish_budget": 6, "wish_budget_panel": 2}))
                self.assertEqual(resp.status_code, 200, resp.body)
                self.assertEqual(body(resp)["restart_required"], [])
                self.assertEqual((rig.cfg.wish_block, rig.cfg.wish_shared_block), (5, 3))
                await rig.background()
                rig.catalog.radio_base = 300
                k = rig.wq.submit("něco klidnějšího", "Karel")
                await rig.until(lambda: k.state in ("queued", "playing"))
                self.assertEqual(len(k.tracks), 5)
                # odpověď na stížnost říká nastavená čísla, ne výchozí
                q = rig.wq.submit("Proč se nestřídáme? To není fér.", "Robert")
                await rig.until(lambda: q.state == "done")
                self.assertIn("střídáme se po 3 skladbách", q.reply)
                self.assertIn("nejvýš 6 skladeb (z displeje 2 skladby)", q.reply)

        run(go())

    def test_settings_form_offers_the_keys_with_bounds(self):
        async def go():
            async with Rig() as rig:
                srv = web.WebServer(WebApp(rig))
                data = body(await srv._config_get(req(method="GET")))
                fields = {f["key"]: f for f in data["fields"]}
                for key in KEYS:
                    self.assertIn(key, web.LIVE_KEYS)
                    self.assertEqual(fields[key]["type"], "int")
                    self.assertFalse(fields[key]["restart"])
                    self.assertTrue(fields[key]["label"] and fields[key]["help"])
                    self.assertEqual(data["values"][key], DEFAULTS[key])
                with mock.patch.object(web.cfgmod, "save_values") as save:
                    bad = await srv._config_post(req({"wish_block": 11}))
                    self.assertEqual(bad.status_code, 400)
                    # kolo, když čekají jiní, nesmí být delší než kolo
                    bad = await srv._config_post(req({"wish_shared_block": 4}))
                    self.assertEqual(bad.status_code, 400)
                    self.assertIn("nejvýš 3", body(bad)["error"])
                    bad = await srv._config_post(req({"wish_block": 1}))
                    self.assertEqual(bad.status_code, 400)
                    save.assert_not_called()
                    ok = await srv._config_post(req({"wish_block": 1, "wish_shared_block": 1}))
                    self.assertEqual(ok.status_code, 200)

        run(go())


class RadioOrigin(unittest.TestCase):
    """F-FRONTA-19: rádio po přání říká, podle čího přání hraje — a není to jmenovka přání."""

    def test_radio_after_a_wish_says_whose_wish_it_follows(self):
        async def go():
            async with Rig() as rig:
                srv = web.WebServer(WebApp(rig))
                cid = "web-karel001"
                await srv._me_post(req({"client": cid, "nick": "Karel"}))
                await rig.background()
                k = rig.wq.submit("něco klidnějšího", "Karel", client={"id": cid})
                await rig.until(lambda: k.state in ("queued", "playing"))
                for _ in range(10):  # blok nálady dohraje
                    if not k.active:
                        break
                    rig.fake.finish_current()
                    await rig.settle(0.15)
                self.assertFalse(k.active)
                reason = rig.wq.reason_for(rig.fake.current_vid())
                self.assertEqual((reason["kind"], reason["who"]), ("radio", ""))  # ničí přání to není
                self.assertEqual(reason["from_who"], "Karel")
                self.assertEqual(reason["from_key"], tag_of(cid))
                self.assertEqual(reason["text"], "klidný pop")
                st = body(await srv._status(req(method="GET")))
                self.assertEqual(st["current"]["reason"], reason)
                self.assertLessEqual(len(json.dumps(reason, ensure_ascii=False)), 160)  # malý stav
                self.assertNotIn(cid, str(st))
                # přejmenování platí i pro rádio
                await srv._me_post(req({"client": cid, "nick": "Karlos"}))
                self.assertEqual(rig.wq.reason_for(rig.fake.current_vid())["from_who"], "Karlos")

        run(go())

    def test_background_nobody_asked_for_has_no_name(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                reason = rig.wq.reason_for(rig.fake.current_vid())
                self.assertEqual(reason["kind"], "radio")
                self.assertNotIn("from_who", reason)

        run(go())

    def test_name_goes_through_the_office_filter(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                rig.wq.bg_reason = {"kind": "radio", "who": "kurva", "text": "punk", "id": ""}
                reason = rig.wq.reason_for(rig.fake.current_vid())
                self.assertNotIn("kurva", json.dumps(reason, ensure_ascii=False))
                self.assertTrue(reason["from_who"])

        run(go())


def render_reason(reason: dict, tag: str = "") -> str:
    """Spustí renderReason z index.html v node a vrátí HTML řádku pod interpretem."""
    src = INDEX.read_text()
    fn = re.search(r"\n  function renderReason\(r\) \{.*?\n  \}\n", src, re.S).group(0)
    esc = re.search(r"\n  function esc\(.*?\n  \}\n", src, re.S)
    script = """
var S = {tag: %s, sig: {}, status: {requests: []}};
var out = {innerHTML: ""};
var el = {reason: out};
function setHidden() {}
function isMine() { return false; }
function hue() { return 0; }
function whoChip(name, you, key) { return '<span class="who">' + name + '</span>'; }
%s
%s
renderReason(%s);
process.stdout.write(out.innerHTML);
""" % (json.dumps(tag), esc.group(0) if esc else "function esc(s) { return String(s); }", fn,
       json.dumps(reason, ensure_ascii=False))
    return subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True,
                          timeout=20).stdout


@unittest.skipUnless(shutil.which("node"), "node není")
class WebShowsRadioOrigin(unittest.TestCase):
    """F-FRONTA-19 na webu: tlumený text, ne barevná jmenovka přání."""

    def test_radio_after_a_wish(self):
        html = render_reason({"kind": "radio", "who": "", "text": "veselý český punk",
                              "from_who": "Robert", "from_key": "abc"})
        self.assertIn("Rádio podle přání Robert", html)
        self.assertIn("veselý český punk", html)
        self.assertNotIn('class="who"', html)  # žádná jmenovka jako u přání
        mine = render_reason({"kind": "radio", "who": "", "text": "punk", "from_who": "Robert",
                              "from_key": "abc"}, tag="abc")
        self.assertIn("Rádio podle tvého přání", mine)

    def test_other_origins(self):
        self.assertIn("Rádio podle času a dne", render_reason({"kind": "start", "who": "", "text": "ráno"}))
        self.assertIn("vybral DJ", render_reason({"kind": "radio", "who": "", "text": "klidný pop"}))
        wish = render_reason({"kind": "wish", "who": "Jana", "text": "Holky", "id": "1", "who_key": "k"})
        self.assertIn('class="who"', wish)  # přání dál s jmenovkou


if __name__ == "__main__":
    unittest.main()
