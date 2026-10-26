"""AI tvorba se nenabízí (hlášení z jukeboxu #37, FUNKCE F-AI-…).

    YTDJ_EVENTS_FILE=/tmp/ev.jsonl .venv/bin/python -m unittest tests.test_ai -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="ytdj-ai-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)
os.environ.setdefault("YTDJ_EVENTS_FILE", str(Path(_TMP) / "events.jsonl"))

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_wishes as tw  # noqa: E402
from ytdj import aitvorba as A  # noqa: E402
from ytdj import telemetry  # noqa: E402
from ytdj import votes as V  # noqa: E402
from ytdj.agent.codex import CodexDJ, Plan  # noqa: E402
from ytdj.agent.intent import Intent  # noqa: E402
from ytdj.config import DEFAULTS, Config  # noqa: E402
from ytdj.display import Censor  # noqa: E402
from ytdj.music.catalog import Track  # noqa: E402
from ytdj.music.radio import RadioPools  # noqa: E402
from ytdj.state import Store  # noqa: E402
from ytdj.web import server as web  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
PETR, JANA = "client-petr", "client-jana"
KRAKEN = Track("kraken00001", "NEČUM A PRACUJ", "Kraken89", duration=200)
KRAKEN_FEAT = Track("kraken00002", "Povstaň", "Rendys, Kraken89", duration=200)
KABAT = Track("kabat000001", "Pohoda", "Kabát", duration=200)
OLYMPIC = Track("olymp000001", "Jasná zpráva", "Olympic", duration=200)


def books(seed=None):
    b = V.VoteBook(cfg=Config(**DEFAULTS), censor=Censor(), rng=lambda: 0.0)
    ai = A.AiBook(seed=seed or (lambda: []))
    b.ai = ai
    return b, ai


class Events:
    def __init__(self):
        self.items: list[tuple[str, dict]] = []
        self.p = mock.patch.object(telemetry, "event", lambda kind, **f: self.items.append((kind, f)))

    def __enter__(self):
        self.p.start()
        return self

    def __exit__(self, *a):
        self.p.stop()

    def kinds(self):
        return [k for k, _ in self.items]


class FakePlayer:
    def __init__(self, current, queue):
        self.current, self.queue, self.skipped = current, list(queue), 0

    async def status(self):
        return SimpleNamespace(playing=True, paused=False, buffering=False, current=self.current,
                               position=1.0, duration=200.0, queue=list(self.queue), volume=50, quality="")

    async def remove_upcoming(self, ids):
        n = len([t for t in self.queue if t.id in ids])
        self.queue = [t for t in self.queue if t.id not in ids]
        return n

    async def skip(self, by_user=True):
        self.skipped += 1


class Flagging(unittest.TestCase):
    """F-AI-01: kdokoli s přezdívkou označí / vrátí, vždy kdo a kdy; návrh nic nefiltruje."""

    def test_one_person_flags_anyone_undoes_suggestion_does_not_filter(self):
        b, ai = books(seed=lambda: [A.Flag("kraken89", "Kraken89", A.SUGGESTED, A.LIST, "štítek")])
        asyncio.run(ai.aload())
        self.assertEqual(ai.flags["kraken89"].state, A.SUGGESTED)
        self.assertIsNone(b.blocked(KRAKEN))  # návrh hraje dál
        with Events() as ev:
            flag, prev = ai.decide(A.AI, PETR, "Petr", key="kraken89")
        self.assertEqual((flag.state, flag.who, prev.state), (A.AI, "Petr", A.SUGGESTED))
        self.assertEqual(b.blocked(KRAKEN), "ai")
        self.assertEqual(b.blocked(KRAKEN_FEAT), "ai")  # sedí na každého uvedeného
        self.assertIsNone(b.blocked(KABAT))
        self.assertEqual(ev.items[0][0], "ai.flag")
        self.assertEqual(ev.items[0][1]["voter"], PETR[-6:])  # jen konec id
        item = ai.lists(PETR)["ai"][0]
        self.assertEqual((item["nick"], item["mine"], item["by"]), ("Petr", True, "označil(a) Petr"))
        ai.decide(A.NOT_AI, JANA, "Jana", key="kraken89")
        self.assertIsNone(b.blocked(KRAKEN))
        self.assertEqual(ai.lists()["not_ai"][0]["nick"], "Jana")
        # z hrající skladby: hlavní interpret
        flag, _ = ai.decide(A.AI, PETR, "Petr", track=KRAKEN_FEAT)
        self.assertEqual((flag.key, flag.name, flag.video_id), ("rendys", "Rendys", KRAKEN_FEAT.id))

    def test_rate_limit_and_bad_input(self):
        _, ai = books()
        with self.assertRaises(V.VoteError) as e:
            ai.decide("smazat", PETR, "Petr", name="X")
        self.assertEqual(e.exception.status, 400)
        with self.assertRaises(V.VoteError) as e:
            ai.decide(A.AI, PETR, "Petr")
        self.assertEqual(e.exception.status, 404)
        for i in range(A.RATE_MAX):
            ai.decide(A.AI, PETR, "Petr", name=f"Umělec {i}")
        with self.assertRaises(V.VoteError) as e:
            ai.decide(A.AI, PETR, "Petr", name="Ještě jeden")
        self.assertEqual(e.exception.status, 429)

    def test_seed_file_is_only_suggestions_with_reasons(self):
        seeds = A.load_seed()
        self.assertGreaterEqual(len(seeds), 1)
        for s in seeds:
            self.assertEqual(s.state, A.SUGGESTED)
            self.assertGreater(len(s.reason), 20, s.name)


class Background(unittest.TestCase):
    """F-AI-02: podkres, rádio a oblíbené AI tvorbu nenabídnou; rádio přitom nezmlkne."""

    def _pools(self, tracks, b):
        async def radio(video_id, limit=50):
            return list(tracks)

        store = SimpleNamespace(blacklisted=lambda: set(), recently_played=lambda d: set(),
                                record_seed=lambda *a: None, recent_history=lambda n: [])
        pools = RadioPools(SimpleNamespace(radio=radio), store, Config(**DEFAULTS))
        pools.votes = b
        return pools

    def test_radio_skips_ai_and_still_plays_the_rest(self):
        b, ai = books()
        ai.decide(A.AI, PETR, "Petr", name="Kraken89")
        ais = [Track(f"krak{i:07d}", f"AI {i}", "Kraken89", duration=200) for i in range(8)]
        tracks = ais + [KABAT, OLYMPIC]

        async def go():
            pools = self._pools(tracks, b)
            await pools.set_seeds([Track("seed0000001", "S", "Seed")])
            with Events() as ev:
                got = await pools.next_tracks(6)
            return got, ev

        got, ev = asyncio.run(go())
        ids = [t.id for t in got]
        self.assertTrue(ids)  # nikdy ticho: zbytek rádia hraje
        self.assertTrue({KABAT.id, OLYMPIC.id} <= set(ids))
        self.assertFalse(any(t.artist == "Kraken89" for t in got))
        pool = [f for k, f in ev.items if k == "radio.pool"][-1]
        self.assertGreaterEqual(pool["rejected"].get("ai", 0), 8)

    def test_artist_asked_by_name_is_exempt_and_favourites_skip_ai(self):
        b, ai = books()
        ai.decide(A.AI, PETR, "Petr", name="Kraken89")
        self.assertIsNone(b.pool_reject(KRAKEN, ["Kraken89"]))  # režim interpreta: někdo si ho řekl
        self.assertEqual(b.pool_reject(KRAKEN), "ai")
        b.cast(V.SONG, JANA, 1, "Jana", track=KRAKEN)  # oblíbená, ale AI
        b.cast(V.SONG, JANA, 1, "Jana", track=KABAT)
        office = [t.id for t in b.favourite_tracks(shuffle=False)]
        self.assertEqual(office, [KABAT.id])  # „pusť oblíbené kanceláře" AI nepustí
        mine = {t.id for t in b.favourite_tracks(JANA, shuffle=False)}
        self.assertEqual(mine, {KRAKEN.id, KABAT.id})  # vlastní 👍 = vlastní přání
        self.assertIn("AI tvorba — nenavrhuj ani jako seedy", b.describe())
        self.assertIn("Kraken89", b.describe())


class Queue(unittest.TestCase):
    """F-AI-03: označení hned uklidí podkres z fronty, přání nechá; výslovné přání s poznámkou."""

    def test_flag_cleans_background_skips_playing_keeps_wishes(self):
        b, ai = books()
        wish = Track("wishkrak001", "Na přání", "Kraken89", duration=200)
        player = FakePlayer(KRAKEN, [KABAT, wish, Track("krak0000009", "X", "Kraken89")])
        app = SimpleNamespace(votes=b, player=player, pools=None,
                              wishes=SimpleNamespace(is_request_track=lambda vid: vid == wish.id))
        ai.decide(A.AI, PETR, "Petr", name="Kraken89")
        with Events() as ev:
            eff = asyncio.run(V.enforce(app, reason="ai:kraken89"))
        self.assertEqual(eff, {"removed": 1, "skipped": True})
        self.assertEqual([t.id for t in player.queue], [KABAT.id, wish.id])
        self.assertEqual([f["why"] for k, f in ev.items if k == "vote.skip"], ["ai"])

    def test_explicit_wish_plays_with_note(self):
        b, ai = books()
        ai.decide(A.AI, PETR, "Petr", name="Kraken89")
        dj = SimpleNamespace(votes=b)
        plan = Plan(intent=Intent(kind="song", text="pusť Nečum a pracuj"), requested=[KRAKEN])
        CodexDJ._office_votes(dj, plan)  # type: ignore[arg-type]
        self.assertEqual(plan.requested, [KRAKEN])
        self.assertTrue(any("Kraken89 je na seznamu AI tvorby — označil(a) Petr" in n
                            for n in plan.notes), plan.notes)
        plan = Plan(intent=Intent(kind="artist", text="pusť Kraken89", artists=["Kraken89"]),
                    artist_tracks=[KRAKEN, KRAKEN_FEAT])
        CodexDJ._office_votes(dj, plan)  # type: ignore[arg-type]
        self.assertEqual(len(plan.artist_tracks), 2)
        self.assertTrue(any("na seznamu AI tvorby" in n for n in plan.notes), plan.notes)
        # Zahrát znovu (replay) odmítá jen vyřazené hlasováním, AI ne
        self.assertEqual(b.ban_note(KRAKEN), "")


class YouTubeLabel(unittest.TestCase):
    """F-AI-04: štítek „Made with AI" u podkresu ve frontě → na seznam s důvodem, mimo smyčku."""

    def test_parse_real_responses(self):
        self.assertTrue(A.parse_label((FIX / "ai_next_made_with_ai.txt").read_text()))
        self.assertFalse(A.parse_label((FIX / "ai_next_no_label.txt").read_text()))
        # jméno typu v seznamu rendererů (bez obsahu) štítek není
        self.assertFalse(A.parse_label('["howThisWasMadeSectionViewModel","x"] "Made with AI"'))

    def _app(self, current, queue, wish_ids=()):
        b, ai = books()
        player = FakePlayer(current, queue)
        app = SimpleNamespace(votes=b, ai=ai, player=player, pools=None, web=None,
                              wishes=SimpleNamespace(is_request_track=lambda vid: vid in wish_ids))
        return b, ai, app

    def test_label_flags_background_with_reason_once_per_track(self):
        human = Track("human000001", "Human", "Real Band", duration=200)
        wish = Track("wish0000001", "Na přání", "Beskydo", duration=200)
        bes = Track("besky000001", "Sousedka", "Beskydo", duration=200)
        no = Track("nolab000001", "Ne", "Lidi", duration=200)
        b, ai, app = self._app(human, [wish, bes, no], wish_ids={wish.id})
        asked = []

        def fetch(vid):
            asked.append(vid)
            return vid == bes.id

        watch = A.LabelWatch(app, ai, fetch=fetch)
        with Events() as ev:
            added = asyncio.run(watch.round())
        self.assertEqual(added, ["beskydo"])
        self.assertNotIn(wish.id, asked)  # přání se neptá
        f = ai.flags["beskydo"]
        self.assertEqual((f.state, f.source, f.reason, f.video_id), (A.AI, A.YOUTUBE, A.LABEL_REASON, bes.id))
        self.assertEqual([t.id for t in app.player.queue], [wish.id, no.id])  # podkres pryč, přání zůstává
        self.assertIn("ai.label", ev.kinds())
        self.assertIn("ai.flag", ev.kinds())
        asked.clear()
        asyncio.run(watch.round())
        self.assertEqual(asked, [])  # každá skladba jednou

    def test_human_not_ai_wins_and_errors_are_unknown(self):
        bes = Track("besky000002", "Tanec", "Beskydo", duration=200)
        b, ai, app = self._app(None, [bes])
        ai.decide(A.NOT_AI, PETR, "Petr", name="Beskydo")
        asked = []
        watch = A.LabelWatch(app, ai, fetch=lambda vid: asked.append(vid) or True)
        asyncio.run(watch.round())
        self.assertEqual(asked, [])  # o Beskydo rozhodl člověk
        self.assertEqual(ai.flags["beskydo"].state, A.NOT_AI)

        other = Track("other000001", "X", "Někdo", duration=200)
        b, ai, app = self._app(None, [other])

        def boom(vid):
            raise OSError("síť")

        watch = A.LabelWatch(app, ai, fetch=boom)
        with Events() as ev:
            asyncio.run(watch.round())
        self.assertNotIn(other.id, watch.checked)  # nevím = zkusí se příště
        self.assertEqual([f["ok"] for k, f in ev.items if k == "ai.label"], [False])
        self.assertFalse(ai.flags)

    def test_slow_youtube_does_not_block_the_loop(self):
        slow = Track("slow0000001", "S", "Pomalý", duration=200)
        b, ai, app = self._app(None, [slow])

        def fetch(vid):
            time.sleep(0.4)
            return False

        async def go():
            watch = A.LabelWatch(app, ai, fetch=fetch)
            ticks = 0
            done = False

            async def ticker():
                nonlocal ticks
                while not done:
                    await asyncio.sleep(0.02)
                    ticks += 1

            t = asyncio.create_task(ticker())
            await watch.round()
            done = True
            await t
            return ticks

        self.assertGreater(asyncio.run(go()), 10)

    def test_fetch_uses_fixed_youtube_endpoint(self):
        seen = {}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return (FIX / "ai_next_made_with_ai.txt").read_bytes()

        def urlopen(req, timeout):
            seen["url"], seen["timeout"] = req.full_url, timeout
            seen["body"] = json.loads(req.data)
            return Resp()

        with mock.patch.object(A.urllib.request, "urlopen", urlopen):
            self.assertTrue(A.fetch_label("abc"))
        self.assertTrue(seen["url"].startswith("https://www.youtube.com/youtubei/v1/next"))
        self.assertEqual(seen["body"]["videoId"], "abc")
        self.assertEqual(seen["body"]["context"]["client"]["clientName"], "MWEB")
        self.assertLessEqual(seen["timeout"], 10)


class Persist(unittest.TestCase):
    """F-AI-05: přežije restart; stará state.db se doplní bez ztráty; zápis přes vlákno zápisů."""

    def test_old_database_migrates_and_flags_survive_restart(self):
        d = Path(tempfile.mkdtemp(dir=_TMP))
        db = d / "state.db"
        old = sqlite3.connect(db)
        old.executescript("""
            CREATE TABLE votes (target TEXT NOT NULL, key TEXT NOT NULL, voter TEXT NOT NULL,
                vote INTEGER NOT NULL, who TEXT, video_id TEXT, artist TEXT, title TEXT,
                ts REAL NOT NULL, PRIMARY KEY (target, key, voter));
            CREATE TABLE plays (video_id TEXT NOT NULL, title TEXT, artist TEXT, ts REAL NOT NULL,
                seed_id TEXT, outcome TEXT NOT NULL DEFAULT 'started');
            INSERT INTO votes VALUES ('artist','papizon','c1',-1,'Radek','','Papizon','',1.0);
            INSERT INTO plays VALUES ('x','t','a',1.0,NULL,'finished');
        """)
        old.commit()
        old.close()
        store = Store(db, background=True)
        self.assertEqual(len(store.all_votes()), 1)
        self.assertEqual(store.all_ai_flags(), [])
        _, ai = books()
        ai.store = store
        main = threading.get_ident()
        ai.decide(A.AI, PETR, "Petr", name="Papizon", reason="debut 2026")
        ai.decide(A.NOT_AI, JANA, "Jana", name="Kabát")
        store.flush()
        self.assertTrue(store._writer.is_alive() and store._writer.ident != main)
        store.close()
        again = Store(db)
        _, ai2 = books()
        ai2.load(again.all_ai_flags())
        self.assertEqual(ai2.flags["papizon"].state, A.AI)
        self.assertEqual(ai2.flags["papizon"].reason, "debut 2026")
        self.assertEqual(ai2.flags["kabat"].who, "Jana")
        self.assertEqual(len(again.all_votes()), 1)
        # rozhodnutí člověka má přednost před návrhem ze seedu
        _, ai3 = books()
        ai3.load(again.all_ai_flags(), [A.Flag("papizon", "Papizon", A.SUGGESTED, A.LIST)])
        self.assertEqual(ai3.flags["papizon"].state, A.AI)
        again.close()


def request(body=None, query=None):
    async def read_body():
        return json.dumps(body or {}).encode()

    return SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={"user-agent": "test"},
                           body=read_body, query_params=query or {},
                           url=SimpleNamespace(path="/api/ai"), method="POST")


class Api(unittest.TestCase):
    """F-AI-06: stránka Hlasování — seznam, hraje teď, označit/vrátit jen s přezdívkou."""

    def _server(self):
        player = FakePlayer(Track("cur00000001", "NEČUM <b>A</b> PRACUJ", "Kraken89, Rendys", duration=200),
                            [KRAKEN, KABAT])
        wq = tw.WishQueue(SimpleNamespace(), player, SimpleNamespace(), None, Config(**DEFAULTS))
        wq.nicks.set(PETR, "Petr")
        app = SimpleNamespace(player=player, wishes=wq, store=None, cfg=Config(**DEFAULTS),
                              pools=SimpleNamespace(describe=lambda: "", mood="", artist=""), dj=None)
        V.wire(app)
        srv = web.WebServer(app)
        h = {}
        for r in srv._starlette.routes:
            for m in getattr(r, "methods", None) or ():
                h[(r.path, m)] = r.endpoint
        return srv, app, h

    def test_flag_now_playing_lists_and_undo(self):
        async def go():
            srv, app, h = self._server()
            post, get = h[("/api/ai", "POST")], h[("/api/ai", "GET")]
            r = await post(request({"state": "ai"}))
            self.assertEqual(r.status_code, 400)  # bez id prohlížeče
            r = await post(request({"state": "ai", "client": JANA}))
            self.assertEqual(r.status_code, 403)  # bez přezdívky
            r = await post(request({"state": "ai", "client": PETR}))
            out = json.loads(r.body)
            self.assertEqual(out["item"]["name"], "Kraken89")
            self.assertEqual(out["effect"], {"removed": 1, "skipped": True})
            self.assertIn("nenabídne", out["message"])
            lists = json.loads((await get(request(query={"client": PETR}))).body)
            self.assertEqual([i["key"] for i in lists["ai"]], ["kraken89"])
            self.assertTrue(lists["ai"][0]["mine"])
            self.assertEqual(lists["now"]["name"], "Kraken89")
            self.assertEqual(lists["now"]["flag"]["state"], "ai")
            app.wishes.nicks.set(JANA, "Jana")
            r = await post(request({"state": "neni_ai", "client": JANA, "key": "kraken89"}))
            self.assertIn("nabízí jako dřív", json.loads(r.body)["message"])
            self.assertIsNone(app.votes.blocked(KRAKEN))

        tw.run(go())

    def test_page_script_escapes_and_is_loaded(self):
        index = (ROOT / "ytdj/web/static/index.html").read_text()
        self.assertIn('<script src="/ai.js?v=3"></script>', index)
        js = (ROOT / "ytdj/web/static/ai.js").read_text()
        # každé pole ze serveru jde do HTML přes esc()
        for field in ("it.name", "it.reason", "n.name", "n.title"):
            self.assertIn(f"esc({field})", js)
        self.assertNotIn("#fff", js.lower())  # barvy jen z proměnných stránky (Den i Noc)


if __name__ == "__main__":
    unittest.main()
