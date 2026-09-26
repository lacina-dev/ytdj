"""Nálezy nezávislé revize fronty přání (26. 9.) — každý s reprodukcí:

  P1.1 identita = id klienta, ne jméno ani IP
  P1.5 po restartu služby se přání obnoví i bez hudby (i rozpracovaná)
  P2.6 "hned teď" neutne cizí přání; kdo přeskočil, vidí vlastník
  P2.7 spam jednoho člověka nezdrží ostatní
  P2.8 rozjezd ustoupí přání; jeho výsledek vidí všichni
  P2.9 sprostá slova se na displeji a webu nezobrazí; hlasitost max 100
  P3   srozumitelné odpovědi (ETA v minutách, úmyslná pauza, žádný text výjimky)

    .venv/bin/python -m unittest tests.test_review -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ytdj-review-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)
os.environ.setdefault("YTDJ_EVENTS_FILE", str(Path(_TMP) / "events.jsonl"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_wishes as tw  # noqa: E402
from test_wishes import SCRIPT, Rig, body, req, run  # noqa: E402
from ytdj import wishes  # noqa: E402
from ytdj.__main__ import App  # noqa: E402
from ytdj.agent.codex import CodexUnavailable  # noqa: E402
from ytdj.agent.intent import SkipWatch  # noqa: E402
from ytdj.display import Censor  # noqa: E402
from ytdj.web import server as web  # noqa: E402


def raw(rig: Rig):
    """submit bez automatického id klienta (Rig ho jinak dává podle jména)."""
    return wishes.WishQueue.submit.__get__(rig.wq)


class Identity(unittest.TestCase):
    def test_anonymous_colleagues_behind_one_nat_are_two_people(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                sub = raw(rig)
                a = sub("pusť Kabát", "", "web", client={"ip": "10.8.0.1", "id": "web-aaaaaaaa1"})
                await rig.settle(0.3)
                b = sub("Olympic prosím", "", "web", client={"ip": "10.8.0.1", "id": "web-bbbbbbbb2"})
                await rig.until(lambda: b.state in ("queued", "playing"))
                self.assertIn(a.state, ("queued", "playing"))  # nenahrazeno
                # popisek je lidské "Host" (žádné "host ·aa1" na displeji);
                # rozliší je who_key (barva jmenovky), ne jméno
                self.assertEqual((a.who, b.who), ("Host", "Host"))
                self.assertNotEqual(a.public()["who_key"], b.public()["who_key"])

        run(go())

    def test_two_people_at_the_panel_and_a_name_thief(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                sub = raw(rig)
                p1 = sub("pusť Kabát", "", "panel", client={"id": "panel-111111"})
                await rig.settle(0.3)
                p2 = sub("Olympic prosím", "", "panel", client={"id": "panel-222222"})
                await rig.until(lambda: p2.state in ("queued", "playing"))
                self.assertIn(p1.state, ("queued", "playing"))
                self.assertEqual((p1.who, p2.who), ("displej", "displej"))
                # cizí člověk napíše jméno "jana" — Janino přání nenahradí
                j = sub("Holky z naší školky", "Jana", "web", client={"id": "web-jana0001"})
                await rig.until(lambda: j.state in ("queued", "playing"))
                x = sub("Dancing Queen", "jana", "web", client={"id": "web-thief0001"})
                await rig.until(lambda: x.state in ("queued", "playing"))
                self.assertIn(j.state, ("queued", "playing"))
                # tentýž prohlížeč: nové přání jde před jeho starší, to zůstává…
                y = sub("Jasná zpráva", "Jana", "web", client={"id": "web-jana0001"})
                await rig.until(lambda: y.state in ("queued", "playing"))
                self.assertIn(j.state, ("queued", "playing"))
                self.assertLess(y.rank, j.rank)
                # …a oprava ("ne, radši…") nahradí to poslední
                SCRIPT["ne, radši Dancing Queen"] = SCRIPT["Dancing Queen"]
                z = sub("ne, radši Dancing Queen", "Jana", "web", client={"id": "web-jana0001"})
                await rig.until(lambda: z.state in ("queued", "playing"))
                self.assertEqual(y.state, "replaced")
                self.assertIn(j.state, ("queued", "playing"))

        run(go())

    def test_client_without_id_never_merges(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                sub = raw(rig)
                a = sub("pusť Kabát", "Petr", "web")
                await rig.settle(0.3)
                b = sub("Olympic prosím", "Petr", "web")
                await rig.until(lambda: b.state in ("queued", "playing"))
                self.assertIn(a.state, ("queued", "playing"))

        run(go())


class Restart(unittest.TestCase):
    def test_paused_restart_keeps_wishes_and_tokens(self):
        async def go():
            async with Rig(codex_delay=5.0) as rig:
                await rig.background()
                a = rig.wq.submit("pusť Kabát", "Petr")
                await rig.until(lambda: a.state in ("queued", "playing"))
                e = rig.wq.submit("Dancing Queen", "Eva")  # DJ ho ještě nestihl
                await rig.until(lambda: e.state == "thinking")
                await rig.player.toggle_pause(True)  # porada
                await rig.wq.refresh_playing()
                saved = rig.wq.session_state()
            self.assertFalse(saved["playing"])
            self.assertEqual({w["who"] for w in saved["wishes"]}, {"Petr", "Eva"})
            async with Rig() as rig2:
                why = await rig2.wq.resume(saved, now=datetime(2026, 9, 28, 10, 0))
                self.assertEqual(why, "was_not_playing")
                eva = rig2.wq.by_id(e.id)
                await rig2.until(lambda: eva.state in ("queued", "playing"))  # DJ ho dořeší
                self.assertTrue(rig2.player._paused)  # hudba se sama nerozjela
                ok, _ = await rig2.wq.remove(a.id, a.token)  # token z prohlížeče platí dál
                self.assertTrue(ok)
                pub = {r["id"]: r for r in rig2.wq.public()}
                self.assertTrue(pub[e.id]["restored"])

        run(go())


class CutAndSkip(unittest.TestCase):
    def test_skip_is_attributed_to_whoever_skipped(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                s = rig.wq.submit("Holky z naší školky", "Petr")
                await rig.until(lambda: s.state == "playing")
                rig.wq.note_skip("Jana")
                await rig.player.skip()
                await rig.until(lambda: s.state == "skipped")
                self.assertEqual(s.skipped_by, "Jana")
                self.assertIn("Přeskočil Jana", s.reply)
                pub = [r for r in rig.wq.public() if r["id"] == s.id][0]
                self.assertEqual((pub["state_cs"], pub["skipped_by"]), ("přeskočeno", "Jana"))

        run(go())


class Spam(unittest.TestCase):
    def test_one_persons_burst_does_not_delay_others(self):
        async def go():
            async with Rig(codex_delay=0.3) as rig:
                await rig.background()
                spam = [rig.wq.submit(f"něco klidnějšího {i}", "Spammer") for i in range(5)]
                jana = rig.wq.submit("Dancing Queen", "Jana")
                await rig.until(lambda: jana.state not in ("waiting", "thinking"), timeout=5)
                asked = [t for t, _ in rig.asked]
                # Janino přání nejpozději jako druhý tah (dřív šesté)
                self.assertLessEqual(asked.index("Dancing Queen"), 1, asked)
                self.assertGreaterEqual(sum(w.state == "replaced" for w in spam), 3)

        run(go())

    def test_additive_burst_takes_turns_with_others(self):
        async def go():
            async with Rig(codex_delay=0.3) as rig:
                await rig.background()
                for i in range(3):
                    SCRIPT[f"přidej Holky {i}"] = SCRIPT["Holky z naší školky"]
                    rig.wq.submit(f"přidej Holky {i}", "Spammer")
                jana = rig.wq.submit("Dancing Queen", "Jana")
                await rig.until(lambda: jana.state not in ("waiting", "thinking"), timeout=5)
                asked = [t for t, _ in rig.asked]
                self.assertLessEqual(asked.index("Dancing Queen"), 1, asked)

        run(go())


class Texts(unittest.TestCase):
    def test_codex_errors_are_never_shown_raw(self):
        async def go():
            async with Rig() as rig:
                async def boom(text, auto=False):
                    raise CodexUnavailable("Codex není přihlášen: 401 Incorrect API key sk-svcac****fvMA "
                                           "https://platform.openai.com/account/api-keys")

                rig.dj.interpret = boom
                await rig.background()
                w = rig.wq.submit("něco klidnějšího", "Eva")
                await rig.until(lambda: w.state == "error")
                self.assertEqual(w.reply, wishes.DJ_OFFLINE_TEXT)
                self.assertNotIn("sk-", str(rig.wq.public()))

                async def other(text, auto=False):
                    raise RuntimeError("Traceback … /home/lacina/.codex/auth.json")

                rig.dj.interpret = other
                w2 = rig.wq.submit("něco živějšího", "Eva2")
                await rig.until(lambda: w2.state == "error")
                self.assertEqual(w2.reply, wishes.DJ_FAILED_TEXT)
                # legacy klient: jedna srozumitelná věta, ne "Codex selhal: (Codex selhal: …)"
                srv = web.WebServer(tw.WebApp(rig))
                resp = await srv._prompt(req({"text": "něco pomalejšího"}))
                self.assertEqual(resp.status_code, 500)
                self.assertEqual(body(resp)["error"], wishes.DJ_FAILED_TEXT)

        run(go())

    def test_artist_reply_mentions_turns_only_when_others_wait(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                p = rig.wq.submit("pusť Kabát", "Petr")
                await rig.until(lambda: bool(p.reply))
                self.assertNotIn("střídáme", p.reply)
                self.assertIn("Kabát", p.reply)
            async with Rig(codex_delay=0.5) as rig:
                await rig.background()
                j = rig.wq.submit("Holky z naší školky", "Jana")
                await rig.until(lambda: j.state == "thinking")
                p = rig.wq.submit("pusť Kabát", "Petr")
                await rig.until(lambda: bool(p.reply))
                self.assertIn("střídáme", p.reply)

        run(go())

    def test_eta_in_minutes(self):
        self.assertEqual(wishes.eta_text(2, 400), "za ~2 skladby (~7 min)")
        self.assertEqual(wishes.eta_text(0, 20), "hned po téhle")

    def test_deliberate_pause_is_respected(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                rig.wq.note_pause(True)
                await rig.player.toggle_pause(True)
                w = rig.wq.submit("Holky z naší školky", "Jana")
                await rig.until(lambda: bool(w.reply))
                self.assertTrue(rig.player._paused)
                self.assertIn("pozastavená", w.reply)

        run(go())


class Display(unittest.TestCase):
    def test_rude_names_and_texts_are_masked(self):
        c = Censor()
        self.assertEqual(c.clean("píča Jana"), "••• Jana")
        self.assertEqual(c.clean("pusť Kabát"), "pusť Kabát")
        self.assertEqual(c.clean("picnic"), "picnic")
        self.assertEqual(Censor(["blbec"]).clean("ty blbče, blbec"), "ty blbče, •••")
        self.assertEqual(Censor(enabled=False).clean("kurva"), "kurva")

        async def go():
            async with Rig() as rig:
                await rig.background()
                w = rig.wq.submit("Holky z naší školky kurva", "Píča")
                await rig.until(lambda: w.state == "playing")
                pub = [r for r in rig.wq.public() if r["id"] == w.id][0]
                self.assertEqual((pub["who"], pub["text"]), ("•••", "Holky z naší školky •••"))
                self.assertNotIn("Píča", rig.wq.people())
                self.assertEqual(rig.wq.reason_for(rig.fake.current_vid())["who"], "•••")

        SCRIPT["Holky z naší školky kurva"] = SCRIPT["Holky z naší školky"]
        run(go())

    def test_volume_ceiling_is_100_everywhere(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                self.assertEqual(await rig.wq.try_local("hlasitost 120"), "Hlasitost 100.")
                srv = web.WebServer(tw.WebApp(rig))
                resp = await srv._control(req({"action": "volume", "value": 125}))
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(rig.player._volume, 100)

        run(go())


class StartYields(unittest.TestCase):
    def test_wish_cancels_the_idle_start(self):
        async def go():
            async with Rig(codex_delay=1.0) as rig:
                app = tw.WebApp(rig)
                app.skips = SkipWatch()
                app._reseed_task = None
                app._now = App._now
                app._poke_web = lambda: None
                app._listener_spoke = App._listener_spoke.__get__(app)
                rig.wq.on_listener = app._listener_spoke
                self.assertEqual(await app.play_or_start(), "starting")
                await rig.until(lambda: rig.wq.lock.locked())  # rozjezd čeká na model
                w = rig.wq.submit("Holky z naší školky", "Jana")
                await rig.until(lambda: w.state in ("queued", "playing"), timeout=3)
                self.assertFalse(rig.wq.starting)
                self.assertTrue(app._start_task.done())

        run(go())

    def test_start_result_is_in_status(self):
        async def go():
            async with Rig() as rig:
                srv = web.WebServer(tw.WebApp(rig))
                await srv._control(req({"action": "play"}))
                await rig.until(lambda: rig.fake.current_vid() is not None)
                await rig.settle(0.1)
                st = body(await srv._status(req(method="GET")))
                self.assertEqual(st["dj"]["last"]["source"], "start")
                self.assertTrue(st["dj"]["last"]["ok"])

        run(go())

    def test_status_carries_dj_brain(self):
        async def go():
            async with Rig() as rig:
                rig.dj.status = lambda: {"brain": {"online": False, "reason": "limit",
                                                   "retry_in_s": 300, "message": "Došel limit."}}
                srv = web.WebServer(tw.WebApp(rig))
                st = body(await srv._status(req(method="GET")))
                self.assertTrue(st["dj_offline"])
                self.assertEqual(st["dj_brain"]["reason"], "limit")

        run(go())


if __name__ == "__main__":
    unittest.main()
