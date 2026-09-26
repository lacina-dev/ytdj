"""DJ when Codex is down: circuit breaker, friendly messages, local chips, budget.

    python -m unittest tests.test_dj_offline -v

Pi 26 Sep: Codex answered 401 and listeners saw
"(Codex selhal: Codex není přihlášen: unexpected status 401 Unauthorized:
Incorrect API key provided: sk-svcac…)"; a 429 could hold a turn for 240 s.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ytdj-test-")
os.environ["XDG_DATA_HOME"] = _TMP
os.environ["XDG_CONFIG_HOME"] = _TMP
os.environ["YTDJ_EVENTS_FILE"] = str(Path(_TMP) / "events.jsonl")
os.environ["YTDJ_CODEX_APP_SERVER"] = "0"

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from test_dj_apply import T, make  # noqa: E402

import ytdj.agent.codex as codex_mod  # noqa: E402
from ytdj.agent.intent import local_command  # noqa: E402
from ytdj.agent.offline import (  # noqa: E402
    CALMER,
    CZECH,
    LIVELIER,
    MORE,
    OTHER,
    SURPRISE,
    Breaker,
    CodexOffline,
    classify,
    local_mood,
)

PI_401 = ("unexpected status 401 Unauthorized: Incorrect API key provided: "
          "sk-svcac*****fvMA. You can find your API key at https://platform.openai.com")


def run(coro):
    return asyncio.run(coro)


class Classify(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(classify(PI_401), "login")
        self.assertEqual(classify("Codex není přihlášen: …"), "login")
        self.assertEqual(classify("unexpected status 429 Too Many Requests"), "limit")
        self.assertEqual(classify("You've hit your usage limit"), "limit")
        # náš vlastní strop tahu není síť (Pi 26. 9. 9:23: studený start + model
        # přetáhl 25 s a posluchač četl "síť")
        self.assertEqual(classify(TimeoutError("x")), "slow")
        self.assertEqual(classify("Codex neodpověděl do 240 s, ukončen"), "slow")
        self.assertEqual(classify("stream disconnected before completion"), "network")
        self.assertIsNone(classify("JSONDecodeError: Expecting value"))


class BreakerTest(unittest.TestCase):
    def test_opens_backs_off_and_recovers(self):
        b = Breaker(auth_file=Path(_TMP) / "nope.json")
        self.assertTrue(b.allow(now=0))
        self.assertEqual(b.failure("429 Too Many Requests", now=0), "limit")
        self.assertFalse(b.allow(now=10))
        self.assertTrue(b.allow(now=301))  # probe after 5 min
        b.failure("429", now=301)
        self.assertFalse(b.allow(now=301 + 600))  # 15 min the second time
        st = b.status(now=400)
        self.assertEqual((st["online"], st["reason"]), (False, "limit"))
        self.assertGreater(st["retry_in_s"], 0)
        self.assertNotIn("429", st["message"])
        b.success()
        self.assertEqual(b.status()["online"], True)

    def test_single_odd_error_does_not_open(self):
        b = Breaker(auth_file=Path(_TMP) / "nope.json")
        self.assertIsNone(b.failure("JSONDecodeError", now=0))
        self.assertTrue(b.allow(now=1))
        self.assertEqual(b.failure("JSONDecodeError", now=1), "error")
        self.assertFalse(b.allow(now=2))

    def test_new_login_retries_at_once(self):
        auth = Path(tempfile.mkdtemp(dir=_TMP)) / "auth.json"
        auth.write_text("{}")
        b = Breaker(auth_file=auth)
        b.failure(PI_401, now=time.time())
        self.assertFalse(b.allow())
        os.utime(auth, (time.time() + 5, time.time() + 5))  # the owner logged in again
        self.assertTrue(b.allow())


class Chips(unittest.TestCase):
    def test_local_moods(self):
        cases = {
            "klidnější": CALMER, "něco klidnějšího prosím": CALMER, "zpomal to": CALMER,
            "živější": LIVELIER, "něco veselejšího": LIVELIER,
            "jen česky": CZECH, "něco českého": CZECH,
            "víc takového": MORE, "ještě něco podobného": MORE,
            "něco jiného": OTHER, "překvap mě": SURPRISE,
        }
        for text, key in cases.items():
            self.assertEqual(local_mood(text), key, text)
        self.assertIsNone(local_mood("pusť Kabát"))
        self.assertIsNone(local_mood("něco klidného na odpoledne, ale bez jazzu a bez saxofonu"))

    def test_commands_with_fillers(self):
        cases = {
            "hlasitěji prosím": "louder", "trochu hlasitěji": "louder",
            "dej to hlasitěji": "louder", "další prosím": "skip",
            "ztiš to trochu": "quieter", "další písničku": "skip", "pauza prosím": "pause",
        }
        for text, action in cases.items():
            self.assertEqual(local_command(text), (action, 0), text)
        for text in ("další od Kabátu", "pusť něco hlasitějšího", "pusť Kabát", "něco jiného"):
            self.assertIsNone(local_command(text), text)


class OfflineDJ(unittest.TestCase):
    def dj_401(self, current=None):
        dj, player, store = make(current=current)
        dj.breaker.auth_file = Path(_TMP) / "nope.json"
        calls = []

        async def model_401(prompt, auto):
            calls.append(prompt)
            raise codex_mod.CodexUnavailable(f"Codex není přihlášen: {PI_401}")

        dj._model_decision = model_401
        return dj, player, store, calls

    def test_raw_error_never_reaches_the_listener(self):
        dj, _, _, calls = self.dj_401()
        with self.assertRaises(CodexOffline) as cm:
            run(dj.interpret("něco na zlepšení nálady"))
        msg = str(cm.exception)
        self.assertNotIn("sk-", msg)
        self.assertNotIn("401", msg)
        self.assertIn("není přihlášený", msg)
        self.assertEqual(cm.exception.reason, "login")
        # the breaker is open: the next wish doesn't wait for the model at all
        with self.assertRaises(CodexOffline):
            run(dj.interpret("něco na zlepšení nálady"))
        self.assertEqual(len(calls), 1)
        self.assertFalse(dj.status()["brain"]["online"])
        self.assertEqual(dj.status()["brain"]["reason"], "login")

    def test_turn_reply_is_friendly(self):
        dj, _, _, _ = self.dj_401()
        reply = run(dj.turn("něco na zlepšení nálady"))
        self.assertNotIn("sk-", reply)
        self.assertNotIn("Codex selhal", reply)

    def test_more_like_this_works_without_the_model(self):
        cur = T("cur", "Tata Bojs", "Opakování")
        dj, player, store, _ = self.dj_401(current=cur)
        for i in range(5):
            store.record_start(f"h{i}", f"Song {i}", f"Artist {i}", None)
            store.record_outcome(f"h{i}", "finished")
        intent = run(dj.interpret("víc takového"))
        self.assertEqual(intent.kind, "mood")
        self.assertEqual(intent.seed_tracks[0].id, "cur")
        self.assertIn("bez mozku", intent.reply)
        plan = run(dj.resolve(intent))
        self.assertEqual(plan.seeds[0].id, "cur")  # no catalog search needed

    def test_czech_from_history(self):
        dj, _, store, _ = self.dj_401()
        store.record_start("c1", "Pramínek vlasů", "Jiří Suchý", None)
        store.record_outcome("c1", "finished")
        store.record_start("c2", "Černí andělé", "Lucie", None)
        store.record_outcome("c2", "finished")
        store.record_start("e1", "Wonderwall", "Oasis", None)
        store.record_outcome("e1", "finished")
        intent = run(dj.interpret("jen česky"))
        self.assertTrue(intent.seed_tracks)
        self.assertNotIn("e1", [t.id for t in intent.seed_tracks])

    def test_calmer_uses_youtube_moods(self):
        dj, _, _, _ = self.dj_401()

        async def cats():
            return {"Moods & moments": [{"title": "Energize", "params": "E"},
                                         {"title": "Chill", "params": "C"}]}

        async def lists(params):
            return [{"playlistId": f"PL-{params}"}]

        async def tracks(pid, limit=50):
            return [T(f"{pid}-{i}", f"Calm {i}") for i in range(6)]

        dj.catalog.mood_categories = cats
        dj.catalog.mood_playlists = lists
        dj.catalog.playlist_tracks = tracks
        intent = run(dj.interpret("klidnější"))
        self.assertTrue(all(t.id.startswith("PL-C") for t in intent.seed_tracks))

    def test_listener_budget(self):
        dj, _, _ = make()
        dj.breaker.auth_file = Path(_TMP) / "nope.json"

        async def slow(prompt, auto):
            await asyncio.sleep(5)  # a 429 with willRetry would sit here for minutes

        dj._model_decision = slow
        old = codex_mod.LISTENER_BUDGET
        codex_mod.LISTENER_BUDGET = 0.3
        try:
            t0 = time.monotonic()
            with self.assertRaises(CodexOffline) as cm:
                run(dj.interpret("něco na zlepšení nálady"))
            self.assertLess(time.monotonic() - t0, 2)
        finally:
            codex_mod.LISTENER_BUDGET = old
        # vypršel NÁŠ rozpočet → "DJ nestihl odpovědět", ne "síť"
        self.assertEqual(cm.exception.reason, "slow")
        self.assertIn("nestihl", str(cm.exception))
        self.assertNotIn("síť", str(cm.exception))
        self.assertIsNone(dj.breaker.reason)  # jeden pomalý tah jistič neotevře
        codex_mod.LISTENER_BUDGET = 0.3
        try:
            with self.assertRaises(CodexOffline):
                run(dj.interpret("něco na zlepšení nálady"))
        finally:
            codex_mod.LISTENER_BUDGET = old
        self.assertEqual(dj.breaker.reason, "slow")  # druhý za sebou už ano

    def test_cold_start_gets_extra_budget(self):
        """Studený app-server (proces + vlákno) se do rozpočtu posluchače
        nepočítá — 9:57 start 8.8 s + model 14 s."""
        dj, _, _ = make()
        dj.breaker.auth_file = Path(_TMP) / "nope.json"

        async def slowish(prompt, auto):
            await asyncio.sleep(0.5)
            return {"action": "nothing", "reply": "stihl jsem to"}

        dj._model_decision = slowish
        old = (codex_mod.LISTENER_BUDGET, codex_mod.COLD_START_BUDGET,
               os.environ.get("YTDJ_CODEX_APP_SERVER"))
        codex_mod.LISTENER_BUDGET, codex_mod.COLD_START_BUDGET = 0.3, 1.0
        os.environ["YTDJ_CODEX_APP_SERVER"] = "1"
        try:
            dj.app = None  # neběží → studený start
            self.assertEqual(run(dj.interpret("ahoj")).reply, "stihl jsem to")

            class Warm:
                ready = True

            dj.app = Warm()  # běží → jen rozpočet modelu
            with self.assertRaises(CodexOffline) as cm:
                run(dj.interpret("ahoj"))
            self.assertEqual(cm.exception.reason, "slow")
        finally:
            codex_mod.LISTENER_BUDGET, codex_mod.COLD_START_BUDGET = old[0], old[1]
            os.environ["YTDJ_CODEX_APP_SERVER"] = old[2] or "0"
            dj.app = None

    def test_recovers_after_probe(self):
        dj, _, _, _ = self.dj_401()
        with self.assertRaises(CodexOffline):
            run(dj.interpret("něco na zlepšení nálady"))
        dj.breaker.retry_at = 0  # time for a probe

        async def ok(prompt, auto):
            return {"action": "nothing", "reply": "jsem zpět"}

        dj._model_decision = ok
        self.assertEqual(run(dj.interpret("ahoj")).reply, "jsem zpět")
        self.assertTrue(dj.status()["brain"]["online"])


class ExecFallbackStopsOnFatal(unittest.TestCase):
    def test_no_fresh_exec_after_login_error(self):
        dj, _, _ = make()
        calls = []

        async def exec_401(args, prompt):
            calls.append(args)
            raise RuntimeError(PI_401)

        dj._run = exec_401
        dj.thread_id = "t"  # a resume would be tried first
        with self.assertRaises(RuntimeError):
            run(dj._model_decision("p", False))
        self.assertEqual(len(calls), 1)  # not resume + fresh


if __name__ == "__main__":
    unittest.main()
