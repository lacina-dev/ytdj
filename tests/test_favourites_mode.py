"""Režim oblíbených (POZADAVKY #49, FUNKCE F-FRONTA-20, F-HLASY-19/20).

Vlastník 27. 9. 0:59: "Já mu řeknu, ať hraje to, co máme rádi, napřeskáčku
interprety. Tam je hromada umělců a on hraje to, co je teď ve frontě … Navíc
jsem chtěl, aby to hrál pořád, ale on naplánoval jen pár." A hned potom:
"nechci, aby se učil konkrétní fráze … chci, aby chápal, co mu user napíše".

Přání tedy vykládá model (akce `favourites` s `favourites_scope`,
`continuous`, `alternate_artists`); tady se testuje, že aplikace jeho
rozhodnutí provede: rozhodnutí z (falešného) app-serveru → Intent →
blok přání napřeskáčku → podkres v režimu oblíbených, pořád, bez opakování,
po lidech na střídačku. Porozumění skutečného modelu měří
.claude/scratch/fav_model_check.py na Pi (zpráva v předávce).

    YTDJ_EVENTS_FILE=/tmp/ev.jsonl .venv/bin/python -m unittest tests.test_favourites_mode -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from collections import Counter
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

_TMP = tempfile.mkdtemp(prefix="ytdj-favmode-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)
os.environ.setdefault("YTDJ_EVENTS_FILE", str(Path(_TMP) / "events.jsonl"))

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import test_wishes as tw  # noqa: E402
from ytdj import votes as V  # noqa: E402
from ytdj.agent.appserver import AppServer  # noqa: E402
from ytdj.agent.codex import DECISION_SCHEMA  # noqa: E402
from ytdj.agent.intent import build_intent, favourites_request  # noqa: E402
from ytdj.config import DEFAULTS, Config  # noqa: E402
from ytdj.music.catalog import Track  # noqa: E402
from ytdj.music.radio import FAV_KEEP, RadioPools  # noqa: E402
from ytdj.votes import SONG, VoteBook, artist_keys  # noqa: E402

FAKE = Path(_TMP) / "codex"
FAKE.write_text(f"#!/bin/sh\nexec {sys.executable} {HERE / 'fake_app_server.py'} \"$@\"\n")
FAKE.chmod(0o755)

OWNER_1 = "Hraj to co mame radi napreskacku"
OWNER_2 = "Hraj pisnicky co mame radi stridej interprety a hraj porad"
PEOPLE = {"client-robert": "Robert", "client-jana": "Jana", "client-karel": "Karel"}


def fav_decision(scope: str = "office", continuous: bool = True, alternate: bool = True) -> dict:
    return {"action": "favourites", "seeds": [], "requested": [], "focus_artists": [],
            "after_current": False, "avoid": [], "mood": "", "volume": 0, "remember": "",
            "reply": "", "favourites_scope": scope, "continuous": continuous,
            "alternate_artists": alternate}


def office(book: VoteBook) -> dict[str, list[Track]]:
    """Kancelář jako na Pi: Robert má velký playlist (84 písniček, některé
    kapely vícekrát), Jana 12 vlastních 👍, Karel 5. Celkem ~100 od ~60 interpretů."""
    made: dict[str, list[Track]] = {}
    specs = {"client-robert": (84, 50), "client-jana": (12, 8), "client-karel": (5, 5)}
    for cid, (n, bands) in specs.items():
        tag = cid.split("-")[1][:3]
        made[cid] = [Track(f"{tag}{i:08d}", f"{tag} song {i}", f"{tag} band {i % bands}",
                           duration=200) for i in range(n)]
        for t in made[cid]:
            book._rate.clear()
            book.cast(SONG, cid, 1, PEOPLE[cid], track=t)
    return made


def pools_with(book: VoteBook, recent: set[str] = frozenset()) -> RadioPools:
    store = SimpleNamespace(blacklisted=lambda: set(), recently_played=lambda d: set(recent),
                            record_seed=lambda *a: None, recent_history=lambda n: [])

    async def radio(video_id, limit=50):
        return [Track(f"radio{i:05d}x", f"Radio {i}", f"Radio band {i}", duration=200)
                for i in range(20)]

    pools = RadioPools(SimpleNamespace(radio=radio), store, Config(**DEFAULTS))
    pools.votes = book
    book.pools = pools
    return pools


def owner_of(t: Track) -> str:
    return {"rob": "Robert", "jan": "Jana", "kar": "Karel"}.get(t.id[:3], "?")


def no_neighbours(tracks: list[Track]) -> bool:
    return all(not (artist_keys(a.artist) & artist_keys(b.artist)) for a, b in zip(tracks, tracks[1:]))


# --------------------------------------------------------------------------
# rozhodnutí modelu → přání oblíbených
# --------------------------------------------------------------------------


class ModelDecision(unittest.TestCase):
    def test_schema_lets_the_model_say_scope_continuous_and_alternation(self):
        props = DECISION_SCHEMA["properties"]
        self.assertIn("favourites", props["action"]["enum"])
        self.assertEqual(props["favourites_scope"]["enum"], ["office", "mine", ""])
        for f in ("favourites_scope", "continuous", "alternate_artists"):
            self.assertIn(f, DECISION_SCHEMA["required"])  # structured outputs: vše povinné

    def test_app_server_decision_becomes_a_favourites_intent(self):
        os.environ["FAKE_MODE"] = "ok"
        os.environ.pop("FAKE_LOG", None)
        cases = [(OWNER_1, fav_decision("office", True, True), ("office", True, True)),
                 ("pusť pár mých oblíbených", fav_decision("mine", False, True), ("mine", False, True))]
        for text, dec, want in cases:
            os.environ["FAKE_DECISION"] = json.dumps(dec)

            async def go():
                app = AppServer(str(FAKE), _TMP)
                try:
                    return await app.turn(f"Uživatel říká: {text}", DECISION_SCHEMA, timeout=5)
                finally:
                    await app.close()

            data = json.loads(asyncio.run(go()).text)
            intent = build_intent(text, data)
            with self.subTest(text=text):
                self.assertEqual((intent.favourites, intent.fav_continuous, intent.fav_alternate), want)
                self.assertEqual((intent.kind, intent.artists, intent.seeds), ("song", [], []))
        os.environ.pop("FAKE_DECISION", None)

    def test_no_phrase_matching_only_the_exact_command(self):
        # vlastník: rozumět, ne učit se fráze — bez modelu jen přesný povel
        self.assertEqual(favourites_request("pusť oblíbené"), "office")
        self.assertEqual(favourites_request("pusť moje oblíbené"), "mine")
        for text in (OWNER_1, OWNER_2, "co máme rádi", "dej naše srdcovky a nepřestávej"):
            self.assertIsNone(favourites_request(text), text)
        # rozhodnutí modelu "start_radio" se podle textu nepřepisuje na oblíbené
        intent = build_intent(OWNER_1, {**fav_decision(), "action": "start_radio",
                                        "seeds": [{"artist": "X", "title": "Y"}]})
        self.assertEqual((intent.kind, intent.favourites), ("mood", ""))


class PromptKnowsFavourites(unittest.TestCase):
    def test_overview_line_counts_people_artists_and_the_askers_own(self):
        book = VoteBook(cfg=Config(**DEFAULTS), rng=lambda: 0.0)
        office(book)
        line = book.describe(asker="client-karel")
        self.assertIn("Oblíbené kanceláře (akce favourites): 101 skladeb od 63 interpretů, "
                      "👍 od 3 lidí; nejvíc jan band 0, jan band 1", line)
        self.assertIn("Kdo píše, má 5 vlastních oblíbených.", line)
        self.assertIn("Kdo píše, vlastní oblíbené nemá.", book.describe(asker="client-nikdo"))
        self.assertLess(len(line), 800)
        empty = VoteBook(cfg=Config(**DEFAULTS))
        self.assertEqual(empty.describe(), "")  # bez hlasů nic navíc
        self.assertIn("zatím žádné", empty.describe(asker="client-x"))

    def test_block_of_favourites_alternates_artists(self):
        book = VoteBook(cfg=Config(**DEFAULTS), rng=lambda: 0.0)
        # jeden člověk, 12 písniček od 3 kapel (dřív klidně 3× Kabát za sebou)
        for i in range(12):
            book._rate.clear()
            book.cast(SONG, "client-jana", 1, "Jana",
                      track=Track(f"jan{i:08d}", f"s{i}", ["Kabát", "Olympic", "Lucie"][i // 4]))
        for seed in range(20):
            book.prng.seed(seed)
            mix = asyncio.run(book.favourite_mix(None))
            self.assertEqual(len(mix), 12)
            self.assertTrue(no_neighbours(mix), [t.artist for t in mix])
        grouped = asyncio.run(book.favourite_mix(None, alternate=False))
        self.assertEqual(len(grouped), 12)  # alternate_artists = false: pořadí bez přeskládání


# --------------------------------------------------------------------------
# podkres v režimu oblíbených
# --------------------------------------------------------------------------


class Rotation(unittest.TestCase):
    def setUp(self):
        self.book = VoteBook(cfg=Config(**DEFAULTS), rng=lambda: 0.0)
        self.book.prng.seed(49)
        self.made = office(self.book)
        self.total = sum(len(v) for v in self.made.values())
        self.pools = pools_with(self.book)

    def pull(self, n: int, batch: int = 4) -> list[Track]:
        async def go():
            out: list[Track] = []
            while len(out) < n:
                got = await self.pools.next_tracks(batch)
                if not got:
                    break
                out += got
            return out[:n]
        return asyncio.run(go())

    def test_plays_every_favourite_once_alternating_then_a_new_round(self):
        res = asyncio.run(self.pools.set_favourites("office", mood="oblíbené kanceláře"))
        self.assertEqual((res["pool_size"], self.pools.favourites), (self.total, "office"))
        first = self.pull(self.total)
        ids = [t.id for t in first]
        self.assertEqual(len(ids), len(set(ids)))  # nic dvakrát, dokud nezazní všechny
        self.assertEqual(len(ids), self.total)
        self.assertTrue(no_neighbours(first), [t.artist for t in first])
        # po lidech na střídačku: v prvních 30 má každý svůj díl, Karel všech 5
        c = Counter(owner_of(t) for t in first[:30])
        self.assertEqual(c["Karel"], 5)
        self.assertGreaterEqual(c["Jana"], 10)
        self.assertLessEqual(c["Robert"], 15)
        # pořád: žádný limit 10 skladeb / 40 min jako u interpreta (F-FRONTA-14)
        more = self.pull(40)
        self.assertEqual(len(more), 40)
        self.assertEqual(self.pools.favourites, "office")
        self.assertFalse(set(t.id for t in more[:5]) & set(ids[-FAV_KEEP:]))  # konec kola se nevrací hned
        self.assertTrue(no_neighbours(more))

    def test_bans_apply_and_other_wishes_are_not_duplicated(self):
        asyncio.run(self.pools.set_favourites("office"))
        banned = self.made["client-jana"][0]
        queued = self.made["client-jana"][1]
        for cid in ("client-karel", "client-robert"):
            self.book.cast(SONG, cid, -1, PEOPLE[cid], track=banned)  # 2× 👎 > 1× 👍
        self.pools.session_seen.add(queued.id)  # čeká ve frontě jako přání
        got = {t.id for t in self.pull(self.total)}
        self.assertNotIn(banned.id, got)
        self.assertNotIn(queued.id, got)

    def test_personal_favourites_and_leaving_the_mode(self):
        asyncio.run(self.pools.set_favourites("mine", voter="client-karel"))
        got = self.pull(5)
        self.assertEqual({owner_of(t) for t in got}, {"Karel"})
        # jiné přání (nálada, interpret) režim ukončí
        asyncio.run(self.pools.set_seeds([Track("seed0000001", "S", "Seed")], mood="klid"))
        self.assertEqual(self.pools.favourites, "")
        self.assertNotIn("napřeskáčku", self.pools.describe())

    def test_boost_does_not_reorder_the_favourites_mode(self):
        asyncio.run(self.pools.set_favourites("office"))
        before = [t.id for t in self.pools.pools[0].tracks]
        self.book._maybe_boost()
        self.assertEqual([t.id for t in self.pools.pools[0].tracks], before)


# --------------------------------------------------------------------------
# celé přání: owner's texts → model (skript) → blok + podkres pořád
# --------------------------------------------------------------------------


class OwnerReplay(unittest.TestCase):
    def test_owner_texts_play_favourites_alternating_and_keep_going(self):
        async def go():
            async with tw.Rig() as rig:
                app = SimpleNamespace(store=rig.store, cfg=rig.cfg, wishes=rig.wq, pools=rig.pools,
                                      dj=rig.dj, player=rig.player)
                book = V.wire(app)
                book.prng.seed(27)
                made = office(book)
                asked = []

                async def interpret(text, auto=False):
                    asked.append(text)
                    return build_intent(text, fav_decision("office", True, True), auto=auto)

                rig.dj.interpret = interpret  # "model" rozhodne podle textu
                w = rig.wq.submit(OWNER_1, "Robert")
                await rig.until(lambda: w.state in ("queued", "playing") and w.reply)
                self.assertEqual(asked, [OWNER_1])  # rozhodl model, ne fráze
                block = list(w.tracks)
                self.assertGreaterEqual(len(block), 7)
                self.assertTrue(no_neighbours(block), [t.artist for t in block])
                self.assertEqual(rig.pools.favourites, "office")
                self.assertIn("dokud si nikdo nepřeje něco jiného", w.reply)
                reason = rig.wq.bg_reason
                self.assertEqual((reason.get("text"), reason.get("mode")),
                                 ("oblíbené kanceláře", "favourites"))
                # podkres: 30+ dalších, nic z bloku ani dvakrát, pořád oblíbené
                seen = {t.id for t in block}
                bg: list[Track] = []
                for _ in range(40):
                    got = await rig.pools.next_tracks(4)
                    bg += got
                    if len(bg) >= 32:
                        break
                self.assertGreaterEqual(len(bg), 32)
                ids = [t.id for t in bg]
                self.assertEqual(len(ids), len(set(ids)))
                self.assertFalse(seen & set(ids))
                fav_ids = {t.id for ts in made.values() for t in ts}
                self.assertTrue(set(ids) <= fav_ids)
                self.assertTrue(no_neighbours(bg))
                self.assertGreaterEqual(len({t.artist for t in block + bg}), 25)
                # druhý text vlastníka: totéž (nové přání téhož člověka)
                w2 = rig.wq.submit(OWNER_2, "Robert")
                await rig.until(lambda: w2.state in ("queued", "playing", "done") and w2.reply)
                self.assertTrue(w2.tracks)
                self.assertEqual(rig.pools.favourites, "office")
                # restart: režim oblíbených se obnoví i s tím, co už zaznělo
                rig.wq.save()
                saved = rig.wq.load_state()
            self.assertEqual(saved["bg"]["mode"], "favourites")
            async with tw.Rig() as rig2:
                app2 = SimpleNamespace(store=rig2.store, cfg=rig2.cfg, wishes=rig2.wq,
                                       pools=rig2.pools, dj=rig2.dj, player=rig2.player)
                book2 = V.wire(app2)
                office(book2)
                await rig2.wq.resume(saved, now=datetime(2026, 9, 25, 10, 0))
                self.assertEqual(rig2.pools.favourites, "office")
                self.assertTrue(set(saved["bg"]["played"]) <= rig2.pools._fav_played)

        tw.run(go())

    def test_not_continuous_is_a_block_then_similar_music(self):
        async def go():
            async with tw.Rig() as rig:
                app = SimpleNamespace(store=rig.store, cfg=rig.cfg, wishes=rig.wq, pools=rig.pools,
                                      dj=rig.dj, player=rig.player)
                book = V.wire(app)
                office(book)

                async def interpret(text, auto=False):
                    return build_intent(text, fav_decision("mine", False, True), auto=auto)

                rig.dj.interpret = interpret
                w = rig.wq.submit("pusť pár mých oblíbených", "Jana", client={"id": "client-jana"})
                await rig.until(lambda: w.state in ("queued", "playing") and w.reply)
                self.assertEqual({owner_of(t) for t in w.tracks}, {"Jana"})
                self.assertEqual(rig.pools.favourites, "")  # jen blok, pak podobná hudba
                self.assertEqual(w.fav, "")

        tw.run(go())


if __name__ == "__main__":
    unittest.main()
