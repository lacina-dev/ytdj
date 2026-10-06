"""Upřesnění přání: když si DJ opravdu není jistý, zeptá se autora (POZADAVKY #64).

    .venv/bin/python -m unittest tests.test_wish_clarify -v

Vlastník 6. 10. 2026: „…kdyby se DJ, pokud si není jistý, co user myslí, mohl
usera zeptat nebo ho to nechat upřesnit, třeba jestli myslí kapelu nebo
písničku" — a „ptát se jen pokud je to nezbytné, tedy minimálně".

Otázka = rozhodnutí modelu `ask` se 2–3 hotovými výklady. Autor odpoví jedním
ťuknutím; bez odpovědi vezme DJ po čase první (nejpravděpodobnější) možnost
a řekne to. Fronta ani hudba na otázku nečekají. Bez sítě a bez modelu
(rozhodnutí skriptovaná); jak často se ptá skutečný model, měří
tests/model_wish_check.py.
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_wishes as tw  # noqa: E402
from test_wishes import Rig, run  # noqa: E402

from ytdj.agent.codex import DECISION_SCHEMA  # noqa: E402
from ytdj.agent.intent import build_intent, clean_options, option_intent  # noqa: E402
from ytdj.agent.prompts import ROLE  # noqa: E402
from ytdj.config import DEFAULTS, Config  # noqa: E402
from ytdj.wishes import ACTIVE, ASK_TIMEOUT, STATE_CS, Wish  # noqa: E402

WISH = "Lucie"  # kapela Lucie, nebo písnička Lucie od Wanastowek?
SONG = {"label": "písnička Lucie — Wanastowi Vjecy", "kind": "song",
        "artist": "Wanastowi Vjecy", "title": "Lucie"}
BAND = {"label": "kapela Olympic", "kind": "artist", "artist": "Olympic", "title": ""}
ALBUM = {"label": "album Load — Metallica", "kind": "album", "artist": "Metallica", "title": "Load"}


def decision(**kw) -> dict:
    base = tw.decision(albums=[], explicit_ok=False, question="", options=[])
    base.update(kw)
    return base


def ask(*options, question: str = "Myslíš písničku, nebo kapelu?") -> dict:
    return decision(action="ask", question=question, options=list(options))


class Asked:
    """Skript modelu na dobu testu: text přání → rozhodnutí."""

    def __init__(self, **script) -> None:
        self.script = script

    def __enter__(self):
        for text, data in self.script.items():
            tw.SCRIPT[text] = ("codex", data)
        return self

    def __exit__(self, *exc) -> None:
        for text in self.script:
            tw.SCRIPT.pop(text, None)


def kinds(rig: Rig, kind: str) -> list[dict]:
    return [f for k, f in rig.events if k == kind]


class Decision(unittest.TestCase):
    def test_schema_and_prompt_ask_only_when_necessary(self):
        self.assertIn("ask", DECISION_SCHEMA["properties"]["action"]["enum"])
        self.assertEqual(DECISION_SCHEMA["properties"]["options"]["items"]["properties"]["kind"]["enum"],
                         ["song", "artist", "album"])
        for key in ("question", "options"):
            self.assertIn(key, DECISION_SCHEMA["required"])
        self.assertIn("VÝCHOZÍ JE ROZHODNOUT", ROLE)
        self.assertIn("jen tehdy, když je to nezbytné", ROLE)
        self.assertIn("PRVNÍ možnost je ta nejpravděpodobnější", ROLE)

    def test_ask_needs_two_complete_readings(self):
        intent = build_intent(WISH, ask(SONG, BAND))
        self.assertEqual(intent.kind, "ask")
        self.assertFalse(intent.changes_music)
        self.assertEqual([o["label"] for o in intent.options], [SONG["label"], BAND["label"]])
        # jediná možnost = není na co se ptát → rovnou se hraje
        one = build_intent(WISH, ask(SONG))
        self.assertEqual((one.kind, one.tracks), ("song", [("Wanastowi Vjecy", "Lucie")]))
        # neúplné a zdvojené možnosti se zahodí, víc než tři taky
        opts = clean_options([SONG, dict(SONG), {"kind": "song", "artist": "X", "title": ""},
                              {"kind": "nonsense", "artist": "X", "title": "Y"}, BAND, ALBUM,
                              {"label": "", "kind": "artist", "artist": "Čtvrtá", "title": ""}])
        self.assertEqual([o["kind"] for o in opts], ["song", "artist", "album"])
        self.assertEqual(build_intent(WISH, ask()).kind, "none")
        # zadání od aplikace (přeseedování) se neptá nikdy
        self.assertEqual(build_intent(WISH, ask(SONG, BAND), auto=True).kind, "song")

    def test_each_option_is_a_ready_to_run_reading(self):
        song = option_intent(WISH, SONG)
        self.assertEqual((song.kind, song.tracks), ("song", [("Wanastowi Vjecy", "Lucie")]))
        band = option_intent(WISH, BAND)
        self.assertEqual((band.kind, band.artists), ("artist", ["Olympic"]))
        album = option_intent(WISH, ALBUM)
        self.assertEqual((album.kind, album.albums), ("album", [("Metallica", "Load")]))
        self.assertFalse(song.auto)

    def test_dj_takes_the_first_reading_when_it_may_not_ask(self):
        """Mimo frontu přání (a u druhé otázky) se model ptát nesmí."""
        rig = Rig()
        self.assertFalse(rig.dj.may_ask)
        intent = rig.dj._decision_intent(WISH, ask(SONG, BAND), auto=False)
        self.assertEqual(intent.kind, "song")
        rig.dj.may_ask = True
        self.assertEqual(rig.dj._decision_intent(WISH, ask(SONG, BAND), auto=False).kind, "ask")
        self.assertEqual(rig.dj._decision_intent(WISH, ask(SONG, BAND), auto=True).kind, "song")

    def test_offline_dj_never_asks(self):
        """Bez modelu (jistič, limit) rozhoduje DJ sám jako dřív — otázka nevznikne."""
        async def go():
            rig = Rig()
            rig.dj.may_ask = True
            rig.dj.note_wish = lambda: None
            rig.dj.breaker.allow = lambda: False
            seen = []

            async def offline(text, auto, exc):
                seen.append(text)
                return build_intent(text, decision(action="start_radio",
                                                   seeds=[{"artist": "A", "title": "B"}]))

            rig.dj._offline_intent = offline
            intent = await rig.dj.interpret(WISH)
            self.assertEqual((intent.kind, seen), ("mood", [WISH]))

        run(go())

    def test_default_timeout_is_25_seconds(self):
        self.assertEqual(DEFAULTS["wish_clarify_timeout"], 25)
        self.assertEqual(ASK_TIMEOUT, 25)
        self.assertEqual(Config(**DEFAULTS).wish_clarify_timeout, 25)
        self.assertIn("asking", ACTIVE)
        self.assertEqual(STATE_CS["asking"], "čeká na upřesnění")


class Queue(unittest.TestCase):
    def test_author_answers_with_one_tap_and_that_reading_plays(self):
        async def go():
            with Asked(**{WISH: ask(SONG, BAND)}):
                async with Rig() as rig:
                    await rig.background()
                    w = rig.wq.submit(WISH, "kolega")
                    await rig.until(lambda: w.state == "asking")
                    shown = next(r for r in rig.wq.public() if r["id"] == w.id)
                    self.assertEqual(shown["state_cs"], "čeká na upřesnění")
                    self.assertEqual(shown["question"], "Myslíš písničku, nebo kapelu?")
                    self.assertEqual(shown["options"], [SONG["label"], BAND["label"]])
                    self.assertTrue(0 < shown["ask_s"] <= 25)
                    self.assertEqual(w.tracks, [])  # nic se nezařadilo naslepo
                    # cizí člověk odpovědět nemůže; jen vidí, že přání čeká
                    self.assertEqual(await rig.wq.answer(w.id, "cizi-token", 1),
                                     (False, "Tohle přání ti nepatří."))
                    self.assertEqual(w.state, "asking")
                    self.assertEqual((await rig.wq.answer(w.id, w.token, 7))[0], False)
                    ok, msg = await rig.wq.answer(w.id, w.token, 1)
                    self.assertEqual((ok, msg), (True, "Beru: kapela Olympic."))
                    await rig.until(lambda: "Zařadil jsem" in w.reply)
                    self.assertEqual(w.kind, "artist")
                    self.assertTrue(w.tracks and all(t.artist == "Olympic" for t in w.tracks))
                    self.assertNotIn("Nevěděl jsem", w.reply)  # rozhodl autor, ne DJ
                    # podruhé už není na co odpovídat
                    self.assertEqual(await rig.wq.answer(w.id, w.token, 0),
                                     (False, "Tohle přání už na upřesnění nečeká."))
                    asked = kinds(rig, "request.ask")
                    self.assertEqual(asked[0]["options"], [SONG["label"], BAND["label"]])
                    self.assertEqual(asked[0]["timeout_s"], 25)
                    ans = kinds(rig, "request.answer")
                    self.assertEqual((ans[0]["by"], ans[0]["choice"], ans[0]["label"]),
                                     ("author", 1, "kapela Olympic"))
                    self.assertIsInstance(ans[0]["waited_ms"], int)

        run(go())

    def test_no_answer_dj_takes_the_first_reading_and_says_so(self):
        async def go():
            with Asked(**{WISH: ask(SONG, BAND)}):
                async with Rig() as rig:
                    rig.cfg.wish_clarify_timeout = 0.3
                    await rig.background()
                    w = rig.wq.submit(WISH, "kolega")
                    await rig.until(lambda: w.state == "asking")
                    await rig.until(lambda: "Zařadil jsem" in w.reply)
                    self.assertEqual(w.kind, "song")
                    self.assertEqual((w.tracks[0].artist, w.tracks[0].title),
                                     ("Wanastowi Vjecy", "song s-Lucie"))
                    self.assertTrue(w.reply.startswith(
                        "Nevěděl jsem, jestli myslíš písnička Lucie — Wanastowi Vjecy nebo kapela "
                        "Olympic; vzal jsem písnička Lucie — Wanastowi Vjecy — jestli jinak, napiš "
                        "to znovu."), w.reply)
                    self.assertIn("Zařadil jsem", w.reply)
                    ans = kinds(rig, "request.answer")
                    self.assertEqual((ans[0]["by"], ans[0]["choice"]), ("timeout", 0))

        run(go())

    def test_question_never_blocks_the_queue_or_the_music(self):
        async def go():
            with Asked(**{WISH: ask(SONG, BAND)}):
                async with Rig() as rig:
                    await rig.background()
                    playing = rig.fake.current_vid()
                    w = rig.wq.submit(WISH, "kolega")
                    await rig.until(lambda: w.state == "asking")
                    self.assertEqual(rig.fake.current_vid(), playing)  # hudba hraje dál
                    # přání někoho jiného (přes model i rychlou cestou) projde hned
                    j = rig.wq.submit("Holky z naší školky", "kolegyně")
                    k = rig.wq.submit("pusť Kabát", "třetí kolega")
                    await rig.until(lambda: j.state in ("queued", "playing"))
                    await rig.until(lambda: k.state in ("queued", "playing"))
                    self.assertEqual(w.state, "asking")
                    self.assertFalse(rig.wq.lock.locked())  # tah modelu nikdo nedrží
                    rig.fake.finish_current()
                    await rig.settle(0.3)
                    self.assertIn(rig.fake.current_vid(), {t.id for t in j.tracks + k.tracks})

        run(go())

    def test_typing_again_is_the_answer_and_no_second_question(self):
        async def go():
            with Asked(**{WISH: ask(SONG, BAND), "tu písničku": ask(SONG, BAND)}):
                async with Rig() as rig:
                    await rig.background()
                    told: list[str] = []
                    scripted = rig.dj.interpret

                    async def interpret(text, auto=False):
                        told.append(text)  # celé zadání pro model, i s dovětkem fronty
                        return await scripted(text.split(" (Tohle je odpověď")[0], auto)

                    rig.dj.interpret = interpret
                    w = rig.wq.submit(WISH, "kolega")
                    await rig.until(lambda: w.state == "asking")
                    w2 = rig.wq.submit("tu písničku", "kolega")
                    self.assertEqual(w.state, "replaced")
                    self.assertTrue(w2.asked)
                    await rig.until(lambda: w2.state in ("queued", "playing"))
                    # model dostal, na co se DJ ptal — a i když se zeptal znovu,
                    # druhá otázka nepadla: vzala se první možnost
                    said = [t for t in told if t.startswith("tu písničku")][0]
                    self.assertIn("odpověď posluchače na tvou otázku", said)
                    self.assertIn("písnička Lucie — Wanastowi Vjecy; kapela Olympic", said)
                    self.assertEqual(w2.kind, "song")
                    self.assertEqual(len(kinds(rig, "request.ask")), 1)
                    self.assertEqual(kinds(rig, "request.answer")[0]["by"], "new_wish")
                    await rig.settle(0.2)
                    self.assertEqual(w.state, "replaced")  # časovač staré otázky už nic neudělá

        run(go())

    def test_author_can_remove_a_wish_that_waits(self):
        async def go():
            with Asked(**{WISH: ask(SONG, BAND)}):
                async with Rig() as rig:
                    rig.cfg.wish_clarify_timeout = 0.3
                    w = rig.wq.submit(WISH, "kolega")
                    await rig.until(lambda: w.state == "asking")
                    self.assertEqual((await rig.wq.remove(w.id, w.token))[0], True)
                    await rig.settle(0.5)
                    self.assertEqual(w.state, "removed")  # ani po čase se nic nezařadí
                    self.assertEqual(w.tracks, [])

        run(go())

    def test_asking_can_be_switched_off(self):
        async def go():
            with Asked(**{WISH: ask(SONG, BAND)}):
                async with Rig() as rig:
                    rig.cfg.wish_clarify_timeout = 0  # 0 = nikdy se neptat
                    w = rig.wq.submit(WISH, "kolega")
                    await rig.until(lambda: w.state in ("queued", "playing"))
                    self.assertEqual(w.kind, "song")
                    self.assertEqual(kinds(rig, "request.ask"), [])

        run(go())

    def test_one_question_per_wish_survives_restart(self):
        w = Wish(id="a", token="t", who="kolega", source="web", text=WISH, created=1.0, mono=1.0,
                 state="asking", asked=True, options=[SONG, BAND], question="?")
        back = Wish.from_json(json.loads(json.dumps(w.to_json())))
        self.assertTrue(back.asked)  # po restartu se zpracuje znovu, ale už bez otázky
        self.assertEqual(back.options, [])


class Web(unittest.TestCase):
    def test_answer_goes_through_the_wish_action_endpoint(self):
        from test_votes import FakePlayer, request
        from ytdj import votes as V
        from ytdj.web import server as web

        async def go():
            calls = []

            async def answer(rid, token, choice):
                calls.append((rid, token, choice))
                return (True, "Beru: kapela Olympic.") if token == "tok" else (False, "Tohle přání ti nepatří.")

            player = FakePlayer()
            wq = tw.WishQueue(SimpleNamespace(), player, SimpleNamespace(), None, Config(**DEFAULTS))
            wq.answer = answer
            app = SimpleNamespace(player=player, wishes=wq, cfg=Config(**DEFAULTS), store=None,
                                  pools=SimpleNamespace(describe=lambda: "", mood="", artist=""), dj=None)
            V.wire(app)
            srv = web.WebServer(app)
            post = {(r.path, m): r.endpoint for r in srv._starlette.routes
                    for m in (getattr(r, "methods", None) or ())}[("/api/requests/{rid}", "POST")]

            def req(body):
                r = request(body)
                r.path_params = {"rid": "abcd"}
                return r

            ok = await post(req({"action": "answer", "choice": 1, "token": "tok"}))
            self.assertEqual((ok.status_code, json.loads(ok.body)["message"]), (200, "Beru: kapela Olympic."))
            bad = await post(req({"action": "answer", "choice": 1, "token": "cizí"}))
            self.assertEqual(bad.status_code, 403)
            self.assertEqual(calls, [("abcd", "tok", 1), ("abcd", "cizí", 1)])

        run(go())

    def test_page_offers_the_options_as_buttons_to_the_author_only(self):
        src = (Path(__file__).resolve().parents[1] / "ytdj/web/static/index.html").read_text()
        self.assertIn('data-ans="', src)
        self.assertIn('action: "answer"', src)
        self.assertIn("asking: 1", src)
        self.assertIn("Odpovědět může jen ten, kdo přání poslal", src)


if __name__ == "__main__":
    unittest.main()
