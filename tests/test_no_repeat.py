"""Pravidlo neopakování (hlášení vlastníka z jukeboxu #32, 12. 10. 2026):

    „V nastavení je, že písničky se nemají opakovat během 5 dní. Zjevně se
    opakují. … oprav tak, aby agent neopakoval písničky, co nebyly hrané před
    nastavenou lhůtou. Jediná výjimka může být výslovné přání na konkrétní
    písničku nebo na opakování již hraného."

Změřeno na Pi (8.–12. 10., 386 přehrání, 46 opakování do 5 dní; cesty viz
FUNKCE.md, tabulka změn): přání interpreta začínalo pokaždé stejnými čtyřmi
skladbami (Horkýže Slíže 12. 10. v 10:33, 11:16, 13:05), podkres po přání je
zahrál znovu (12:05–12:15), oblíbené směly „i dřív" (9. 10. 19:08–19:12 po
restartu za 0,3–0,6 h) a režim oblíbených jel dokola.

    YTDJ_EVENTS_FILE=/tmp/ev.jsonl .venv/bin/python -m unittest tests.test_no_repeat -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

_TMP = tempfile.mkdtemp(prefix="ytdj-norepeat-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)
os.environ.setdefault("YTDJ_EVENTS_FILE", str(Path(_TMP) / "events.jsonl"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_controls as tc  # noqa: E402  (as_app: App._on_event nad Rigem)
import test_wishes as tw  # noqa: E402  (Rig: opravdový MpvPlayer nad falešným mpv)
from test_votes import book  # noqa: E402
from ytdj.agent import fastpath  # noqa: E402
from ytdj.agent.codex import DECISION_SCHEMA, Plan  # noqa: E402
from ytdj.agent.intent import Intent, build_intent, option_intent  # noqa: E402
from ytdj.agent.prompts import ROLE  # noqa: E402
from ytdj.config import DEFAULTS, Config  # noqa: E402
from ytdj.music.catalog import Track  # noqa: E402
from ytdj.music.radio import RadioPools  # noqa: E402
from ytdj.music.repeats import RepeatGuard, one_per_song, period_text  # noqa: E402
from ytdj.state import Store  # noqa: E402
from ytdj.votes import SONG  # noqa: E402
from ytdj.web.server import FIELD_META  # noqa: E402
from ytdj.wishes import REPEAT_HINT, Wish  # noqa: E402

DAY = 86400.0
PETR = "client-petr"


def run(coro):
    return asyncio.run(coro)


def T(vid: str, title: str, artist: str) -> Track:
    return Track(vid.ljust(11, "0")[:11], title, artist, duration=200)


# čtyři skladby, které 12. 10. hrály v 10:33, 11:16, 12:05 a 13:05
HS = [T("hs1", "Silný refrén", "Horkýže Slíže"), T("hs2", "Mám v p... na lehátku", "Horkýže Slíže"),
      T("hs3", "Malá Žužu", "Horkýže Slíže"), T("hs4", "L.A.G. Song", "Horkýže Slíže")]
HS_MORE = [T(f"hsm{i}", f"Další věc {i}", "Horkýže Slíže") for i in range(8)]
OTHERS = [T(f"oth{i}", f"Cizí píseň {i}", f"Jiná kapela {i}") for i in range(30)]


def new_store(background: bool = False) -> Store:
    return Store(Path(tempfile.mkdtemp(dir=_TMP)) / "state.db", background=background)


def cfg(**kw) -> Config:
    return Config(**{**DEFAULTS, "repeat_days": 5, **kw})


def played(store: Store, tracks, ago_s: float = 600.0, outcome: str = "finished") -> None:
    """Zapíše do historie, že skladby začaly hrát před `ago_s` vteřinami."""
    for t in tracks:
        store.db.execute(
            "INSERT INTO plays(video_id,title,artist,ts,seed_id,outcome) VALUES(?,?,?,?,?,?)",
            (t.id, t.title, t.artist, time.time() - ago_s, "test", outcome))
    store.play_rev += 1  # psáno mimo record_start → paměť pravidla načíst znovu


def pools_for(store: Store, radio=None, config: Config | None = None, votes=None) -> RadioPools:
    async def default_radio(video_id, limit=50):
        return list(radio if radio is not None else OTHERS)

    pools = RadioPools(SimpleNamespace(radio=default_radio), store, config or cfg())
    pools.votes = votes
    if votes is not None:
        votes.pools = pools
    return pools


# --------------------------------------------------------------------------
# co se počítá jako "hrála" — jedna paměť pro všechny zdroje skladeb
# --------------------------------------------------------------------------


class Guard(unittest.TestCase):
    def test_everything_that_started_counts_however_it_ended(self) -> None:
        """Dohraná, přeskočená po 3 s i odsunutá přáním — všechno začalo hrát.
        Jen skladba, kterou se přehrát nepovedlo, nehrála."""
        store = new_store()
        tracks = [T(f"t{i}", f"Píseň {i}", f"Kapela {i}") for i in range(5)]
        for t, how in zip(tracks, ("finished", "skipped", "replaced", "started", "error")):
            store.record_start(t.id, t.title, t.artist, "nálada")
            if how != "started":
                store.record_outcome(t.id, how)
        guard = RepeatGuard(store, cfg())
        run(guard.refresh())
        self.assertEqual([guard.recent(t) for t in tracks], [True, True, True, True, False])
        fresh, old = guard.split(tracks)
        self.assertEqual(([t.id for t in fresh], len(old)), ([tracks[4].id], 4))

    def test_another_upload_of_the_same_song_is_the_same_song(self) -> None:
        """Pi 12. 10.: „Horkýze Slíze — Osemnast" v 11:05 a jiné video téže
        písně „Horkýže Slíže — Osemnásť" v 11:22."""
        store = new_store()
        played(store, [T("3ITWdseTIQc", "Osemnast", "Horkýze Slíze")])
        guard = RepeatGuard(store, cfg())
        run(guard.refresh())
        self.assertTrue(guard.recent(T("Gc2if_4DkzI", "Osemnásť", "Horkýže Slíže")))
        self.assertTrue(guard.recent(T("livevideo01", "Osemnásť (Live)", "Horkýže Slíže - Topic")))
        self.assertFalse(guard.recent(T("jinapisen01", "Devatenásť", "Horkýže Slíže")))
        self.assertFalse(guard.recent(T("jinakapel01", "Osemnásť", "Jiná kapela")))
        # sada interpreta: každá píseň jednou, ne studiová verze a pak živák
        both = [T("a1", "Malá Žužu", "Horkýže Slíže"), T("a2", "Malá Žužu (Live)", "Horkýže Slíže"),
                T("a3", "Líza a Wendy", "Horkýže Slíže")]
        self.assertEqual([t.id for t in one_per_song(both)], [both[0].id, both[2].id])

    def test_the_period_is_the_setting_and_zero_switches_it_off(self) -> None:
        store = new_store()
        old, new = T("old", "Stará", "Kapela"), T("new", "Nová", "Kapela")
        played(store, [old], ago_s=6 * DAY)
        played(store, [new], ago_s=4 * DAY)
        config = cfg(repeat_days=5)
        guard = RepeatGuard(store, config)
        run(guard.refresh())
        self.assertEqual((guard.recent(old), guard.recent(new)), (False, True))
        config.repeat_days = 7  # lhůta se v nastavení prodloužila — platí hned
        run(guard.refresh())
        self.assertEqual((guard.recent(old), guard.recent(new)), (True, True))
        config.repeat_days = 0
        run(guard.refresh())
        self.assertEqual((guard.recent(old), guard.recent(new)), (False, False))
        self.assertEqual((period_text(5), period_text(1)), ("v posledních 5 dnech", "za poslední den"))

    def test_history_is_read_once_in_a_thread_and_then_kept_in_memory(self) -> None:
        """SD karta na Pi je pomalá (2600+ řádků historie): databáze se čte
        jednou, mimo event loop; další starty skladeb jdou z paměti."""
        store = new_store(background=True)
        first = T("first", "První", "Kapela")
        store.record_start(first.id, first.title, first.artist, "x")
        store.flush()
        reads: list[str] = []
        real = store.played_since

        def spy(cutoff):
            reads.append(threading.current_thread().name)
            return real(cutoff)

        store.played_since = spy  # type: ignore[method-assign]
        guard = RepeatGuard(store, cfg())

        async def go():
            await guard.refresh()
            later = T("later", "Druhá", "Kapela")
            self.assertFalse(guard.recent(later))
            store.record_start(later.id, later.title, later.artist, "x")  # zápis jde frontou
            for _ in range(5):
                await guard.refresh()
            return guard.recent(first), guard.recent(later)

        self.assertEqual(run(go()), (True, True))
        self.assertEqual(len(reads), 1, reads)
        self.assertTrue(reads[0].startswith("ytdj-store-read"), reads)

    def test_the_setting_says_what_the_rule_does(self) -> None:
        label, text, bounds = FIELD_META["repeat_days"]
        self.assertEqual((label, bounds), ("Neopakovat N dní", (0, 3650)))
        for must in ("přání", "konkrétní", "0 ="):
            self.assertIn(must, text)


# --------------------------------------------------------------------------
# podkres: rádio, interpret, album, oblíbené
# --------------------------------------------------------------------------


class Background(unittest.TestCase):
    def test_artist_mode_over_the_songs_just_played_goes_similar_instead(self) -> None:
        """Pi 12. 10. 12:02: přání dohrálo své 4 skladby, podkres dostal režim
        interpreta jen z nich, „jel od začátku" a ve 12:05–12:15 je zahrál znovu
        (a přitom se 440× protočil: radio.pool attempts 440, 1,5 s)."""
        store = new_store()
        played(store, HS, ago_s=300)
        pools = pools_for(store, radio=HS + OTHERS)
        events: list[tuple[str, dict]] = []
        with tw.mock.patch.object(tw.telemetry, "event", lambda k, **f: events.append((k, f))):
            run(pools.set_artist("Horkýže Slíže", HS, max_tracks=10))
            got = run(pools.next_tracks(11))
        self.assertTrue(got)  # ticho ne
        self.assertFalse({t.id for t in got} & {t.id for t in HS}, [t.label() for t in got])
        self.assertEqual((pools.artist, pools.mood), ("", "Horkýže Slíže a podobné"))
        attempts = [f["attempts"] for k, f in events if k == "radio.pool"]
        self.assertLess(max(attempts), 100, attempts)
        self.assertIn(("radio.artist_mode", "all_played"),
                      [(k, f.get("ended")) for k, f in events])
        # čtyři skladby, které ještě nehrály (čekají ve frontě): vydá je jednou,
        # zůstane u interpreta a taky se neprotáčí
        events.clear()
        fresh_store = new_store()
        pools = pools_for(fresh_store, radio=OTHERS)
        with tw.mock.patch.object(tw.telemetry, "event", lambda k, **f: events.append((k, f))):
            run(pools.set_artist("Horkýže Slíže", HS))
            got = run(pools.next_tracks(11))
            again = run(pools.next_tracks(11))  # nic nezačalo hrát — smí je vydat znovu
        self.assertEqual([t.id for t in got], [t.id for t in HS])
        self.assertEqual({t.id for t in again}, {t.id for t in HS})
        self.assertEqual(pools.artist, "Horkýže Slíže")
        self.assertLess(max(f["attempts"] for k, f in events if k == "radio.pool"), 30)

    def test_artist_mode_plays_only_what_was_not_played_within_the_period(self) -> None:
        store = new_store()
        played(store, HS, ago_s=3 * 3600)  # ráno
        pools = pools_for(store)
        run(pools.set_artist("Horkýže Slíže", HS + HS_MORE))
        got = run(pools.next_tracks(8))
        self.assertEqual([t.id for t in got], [t.id for t in HS_MORE])
        store2 = new_store()
        played(store2, HS, ago_s=6 * DAY)  # před lhůtou — smí
        pools2 = pools_for(store2)
        run(pools2.set_artist("Horkýže Slíže", HS + HS_MORE))
        self.assertEqual({t.id for t in HS} - {t.id for t in run(pools2.next_tracks(12))}, set())

    def test_radio_does_not_bring_back_a_favourite_after_a_restart(self) -> None:
        """Pi 9. 10. 19:06 restart: oblíbené Happy, Get Lucky a Killer Queen
        hrály znovu za 0,3–4,4 h — pooly si po startu nic nepamatovaly a
        oblíbená směla „i dřív než po repeat_days"."""
        store = new_store()
        b = book()
        hits = [T("happy", "Happy", "Pharrell Williams"), T("lucky", "Get Lucky", "Daft Punk"),
                T("queen", "Killer Queen", "Queen")]
        for t in hits:
            b._rate.clear()
            b.cast(SONG, PETR, 1, "Petr", track=t)
        played(store, hits, ago_s=20 * 60)
        pools = pools_for(store, radio=hits + OTHERS, votes=b)  # nový proces = nové pooly
        run(pools.set_seeds([T("seed", "Seed", "Seed")], mood="veselá hudba k práci"))
        got = run(pools.next_tracks(11))
        self.assertEqual(len(got), 11)
        self.assertFalse({t.id for t in got} & {t.id for t in hits})

    def test_radio_skips_another_upload_of_a_song_already_played(self) -> None:
        store = new_store()
        played(store, [T("3ITWdseTIQc", "Osemnast", "Horkýze Slíze")], ago_s=17 * 60)
        other = T("Gc2if_4DkzI", "Osemnásť", "Horkýže Slíže")
        twice = [T("dup1", "Stejná píseň", "Kapela"), T("dup2", "Stejná píseň (Remastered)", "Kapela")]
        pools = pools_for(store, radio=[other] + twice + OTHERS)
        run(pools.set_seeds([T("seed", "Seed", "Seed")], mood="rádio"))
        got = [t.id for t in run(pools.next_tracks(11))]
        self.assertNotIn(other.id, got)
        self.assertEqual(len({twice[0].id, twice[1].id} & set(got)), 1)  # v dávce jen jednou

    def test_favourites_mode_plays_the_unplayed_first_then_goes_round(self) -> None:
        """Pi 9. 10. 14:18–15:36: režim oblíbených zahrál 8 písniček hraných před
        44–71 h. Vlastník 16. 10.: nejdřív nehrané, pak dokola od nejdéle
        nehrané — nikdy „už všechno hrálo"."""
        store = new_store()
        b = book()
        favs = [T(f"fav{i}", f"Oblíbená {i}", f"Kapela {i}") for i in range(6)]
        for t in favs:
            b._rate.clear()
            b.cast(SONG, PETR, 1, "Petr", track=t)
        for i, t in enumerate(favs[:4]):
            played(store, [t], ago_s=(3 - i * 0.5) * DAY)  # fav0 nejdávněji
        pools = pools_for(store, votes=b)
        res = run(pools.set_favourites("office"))
        self.assertEqual(res["pool_size"], 2)  # nejdřív dvě, které ve lhůtě nehrály
        got = run(pools.next_tracks(2))
        self.assertEqual({t.id for t in got}, {favs[4].id, favs[5].id})
        for t in got:  # začaly hrát (až po vydání)
            store.record_start(t.id, t.title, t.artist, "oblíbené")
            time.sleep(0.001)
        more = run(pools.next_tracks(6))
        self.assertEqual([t.id for t in more],
                         [t.id for t in favs[:4]] + [t.id for t in got])  # dokola, nejdéle nehraná první
        self.assertEqual((pools.favourites, pools.mood), ("office", "oblíbené kanceláře"))
        # všechno už hrálo: režim se zapne rovnou dokola (ne „a podobné")
        self.assertEqual(run(pools_for(store, votes=b).set_favourites("office"))["pool_size"], 6)

    def test_album_in_the_background_leaves_out_what_was_played(self) -> None:
        store = new_store()
        album = [T(f"alb{i}", f"Skladba alba {i}", "Kapela") for i in range(8)]
        played(store, [album[5], album[6]], ago_s=DAY)
        pools = pools_for(store)
        run(pools.set_album("Kapela — Deska", album, done={album[0].id}))
        got = [t.id for t in run(pools.next_tracks(8))]
        self.assertEqual(got[:5], [album[i].id for i in (1, 2, 3, 4, 7)])

    def test_only_a_wish_that_asked_for_it_lets_the_background_repeat(self) -> None:
        store = new_store()
        played(store, HS, ago_s=300)
        album = list(HS)
        b = book()
        for t in HS:
            b._rate.clear()
            b.cast(SONG, PETR, 1, "Petr", track=t)
        for setup in ("artist", "album", "favourites"):
            with self.subTest(mode=setup):
                pools = pools_for(store, votes=b)
                if setup == "artist":
                    run(pools.set_artist("Horkýže Slíže", HS, repeat_ok=True))
                elif setup == "album":
                    run(pools.set_album("HS — Deska", album, repeat_ok=True))
                else:
                    run(pools.set_favourites("office", repeat_ok=True))
                self.assertTrue(pools.repeat_ok)
                self.assertEqual({t.id for t in run(pools.next_tracks(4))}, {t.id for t in HS})
                # další naplnění poolů (rádio) výjimku ruší
                run(pools.set_seeds([T("seed", "Seed", "Seed")], mood="rádio"))
                self.assertFalse(pools.repeat_ok)


class FavouritesRound(unittest.TestCase):
    """Vlastník 16. 10. 2026: „Oblíbených se to týká způsobem, že to nejdříve
    hraje ty, co to ve lhůtě ještě nehrálo, a pak klidně opakuje, ale zas
    nezahraje znovu, dokud nepřehraje všechny." Jako na Pi: ~160 oblíbených
    od tří lidí, lhůta 5 dní, většina už ve lhůtě hrála."""

    SPLIT = {"client-robert": (84, 10), "client-jana": (50, 6), "client-karel": (26, 4)}  # (všech, nehraných)

    def setUp(self) -> None:
        self.store = new_store()
        self.book = book()
        self.book.prng.seed(16)
        self.by_person: dict[str, list[Track]] = {}
        self.unplayed: set[str] = set()
        old: list[Track] = []
        for cid, (n, fresh) in self.SPLIT.items():
            tag = cid.split("-")[1][:3]
            tracks = [T(f"{tag}{i:05d}", f"{tag} píseň {i}", f"{tag} kapela {i % 40}") for i in range(n)]
            self.by_person[cid] = tracks
            for t in tracks:
                self.book._rate.clear()
                self.book.cast(SONG, cid, 1, cid, track=t)
            self.unplayed |= {t.id for t in tracks[:fresh]}
            old += tracks[fresh:]
        # 140 už hrálo, každá jindy během posledních 5 dnů (pořadí zamíchané)
        import random
        rnd = random.Random(5)
        rnd.shuffle(old)
        for i, t in enumerate(old):
            played(self.store, [t], ago_s=4.5 * DAY - i * 1800)
        self.old_order = [t.id for t in old]  # od nejdéle nehrané
        self.owner = {t.id: cid for cid, ts in self.by_person.items() for t in ts}
        self.pools = pools_for(self.store, votes=self.book)

    def pull(self, n: int) -> list[Track]:
        """Jako plnič: dávky po 4, co se vydá, začne hrát (zapíše se do historie)."""
        async def go():
            out: list[Track] = []
            while len(out) < n:
                got = await self.pools.next_tracks(min(4, n - len(out)))
                if not got:
                    break
                for t in got:
                    self.store.record_start(t.id, t.title, t.artist, "oblíbené")
                    await asyncio.sleep(0.001)  # každá začne jindy
                out += got
            return out
        return run(go())

    def test_unplayed_first_then_strictly_least_recently_played(self) -> None:
        res = run(self.pools.set_favourites("office"))
        self.assertEqual(res["pool_size"], 20)
        first = self.pull(20)
        self.assertEqual({t.id for t in first}, self.unplayed)  # 1. fáze: jen nehrané
        # po lidech na střídačku (F-HLASY-17): v prvních 12 má každý svůj díl
        share = {cid: sum(self.owner[t.id] == cid for t in first[:12]) for cid in self.SPLIT}
        self.assertGreaterEqual(min(share.values()), 3, share)
        self.assertLessEqual(max(share.values()), 5, share)
        # 2. fáze: dokola přesně od nejdéle nehrané, každá jednou
        cycle = self.pull(160)
        ids = [t.id for t in cycle]
        self.assertEqual(len(ids), 160)
        self.assertEqual(len(set(ids)), 160)  # žádná dvakrát, dokud nezazní všechny
        self.assertEqual(ids, self.old_order + [t.id for t in first])
        self.assertEqual((self.pools.favourites, self.pools.mood), ("office", "oblíbené kanceláře"))
        again = [t.id for t in self.pull(30)]
        self.assertEqual(again, ids[:30])  # a zase stejně: nejdéle nehraná první
        # hlídka: kolo dokola je dovolené opakování, ne díra
        wq = SimpleNamespace(_resumed_vid=None, pools=self.pools, wishes=[],
                             _owner_of=lambda vid: None)
        from ytdj.wishes import WishQueue

        self.assertEqual(WishQueue.repeat_reason(wq, again[0]), "favourites_rotation")
        self.pools.favourites = ""  # jiný režim podkresu: už ne
        self.assertEqual(WishQueue.repeat_reason(wq, again[0]), "")

    def test_no_favourite_twice_while_another_has_not_played_since(self) -> None:
        """Obecně, i s přerušením: mezi dvěma přehráními téže oblíbené zazněly
        všechny ostatní (z těch, které ve lhůtě hrály)."""
        run(self.pools.set_favourites("office"))
        seq = [t.id for t in self.pull(60)]
        # přání někoho jiného na chvíli přeruší oblíbené, pak zase oblíbené
        run(self.pools.set_seeds([T("seed", "Seed", "Seed")], mood="rádio"))
        self.pull(5)
        run(self.pools.set_favourites("office"))
        seq += [t.id for t in self.pull(300)]
        last: dict[str, int] = {}
        for i, vid in enumerate(seq):
            if vid in last:
                between = set(seq[last[vid] + 1:i])
                self.assertEqual(len(between), 159, (vid, i))
            last[vid] = i

    def test_the_wish_takes_the_unplayed_and_then_the_longest_unplayed(self) -> None:
        """„pusť oblíbené" (blok 8 skladeb): když nehrané nestačí, doplní ho
        nejdéle nehrané a DJ to řekne jednou krátkou větou."""
        async def go():
            async with tw.Rig() as rig:
                rig.cfg.repeat_days = 5
                every = [t for ts in self.by_person.values() for t in ts]
                rig.store = self.store
                rig.wq.repeats = RepeatGuard(self.store, rig.cfg)
                w = Wish(id="w", token="t", who="Marek", source="web", text="pusť oblíbené",
                         created=time.time(), mono=time.monotonic(), cid="client-marek")
                fresh = [t for t in every if t.id in self.unplayed][:3]
                stale = [t for t in every if t.id not in self.unplayed]
                plan = Plan(intent=Intent(kind="song", text=w.text, note="favourites"),
                            requested=(fresh + stale)[:8], candidates=fresh + stale)
                cut = await rig.wq._drop_repeats(w, plan.intent, plan)
                self.assertEqual([t.id for t in plan.requested],
                                 [t.id for t in fresh] + self.old_order[:5])
                self.assertEqual(w.again, set(self.old_order[:5]))
                self.assertEqual(cut.notes, ["Nehraných oblíbených bylo jen 3, pak jedu dokola — "
                                             "od těch, co nejdéle nehrály."])
                self.assertLess(len(" ".join(cut.notes)), 120)  # krátce (web ukáže ~400 znaků)

        run(go())


# --------------------------------------------------------------------------
# přání: sady se čistí a DJ říká pravdu
# --------------------------------------------------------------------------


def started(rig, vid: str) -> bool:
    return rig.fake.current_vid() == vid


class Wishes(unittest.TestCase):
    def test_the_same_artist_wish_again_plays_his_other_songs(self) -> None:
        """Pi 12. 10.: Petr si čtyřikrát řekl „horkýže slíže" a pokaždé dostal
        Silný refrén, Mám v p… na lehátku, Malou Žužu a L.A.G. Song."""
        async def go():
            async with tw.Rig() as rig:
                rig.cfg.repeat_days = 5
                await rig.background()
                played(rig.store, tw.KABAT[:4], ago_s=40 * 60)
                w = rig.wq.submit("pusť Kabát", "Petr")
                await rig.until(lambda: w.state in ("queued", "playing") and bool(w.reply))
                every = [t.id for t in w.tracks + w.rest]
                self.assertFalse(set(every) & {t.id for t in tw.KABAT[:4]})
                self.assertEqual(w.tracks[0].id, tw.KABAT[4].id)  # jeho další nejznámější
                self.assertIn("Bez toho, co tu v posledních 5 dnech už hrálo (4 skladby).", w.reply)
                self.assertIn("Zařadil jsem Kabát: ", w.reply)
                dropped = [f for k, f in rig.events if k == "request.repeats_dropped"]
                self.assertEqual((dropped[0]["dropped"], dropped[0]["days"]), (4, 5))
                # ani jeho podkres (zbytek interpreta) je nevrátí
                await rig.until(lambda: rig.pools.artist == "Kabát")
                more = await rig.dj.next_tracks(12)
                self.assertFalse({t.id for t in more} & {t.id for t in tw.KABAT[:4]})

        run(go())

    def test_nothing_unplayed_left_means_similar_music_and_the_truth(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                rig.cfg.repeat_days = 5
                await rig.background()
                played(rig.store, tw.OLYMPIC, ago_s=3 * 3600)  # všech 10, co katalog zná
                w = rig.wq.submit("Olympic prosím", "Petr")
                await rig.until(lambda: w.state in ("queued", "playing", "done") and bool(w.reply))
                self.assertTrue(w.tracks)  # nezůstane ticho
                self.assertFalse({t.id for t in w.tracks} & {t.id for t in tw.OLYMPIC})
                self.assertEqual(w.kind, "mood")
                self.assertIn("Od Olympic tu v posledních 5 dnech hrálo všechno, co znám (10 skladeb) "
                              "— neopakuju to, hraju podobnou hudbu.", w.reply)
                self.assertIn(REPEAT_HINT, w.reply)
                self.assertEqual(rig.pools.mood, "Olympic a podobné")
                self.assertEqual(rig.pools.artist, "")

        run(go())

    def test_the_background_after_an_artist_wish_does_not_replay_it(self) -> None:
        """Pi 12. 10. 12:02 a 9. 10. 19:56: podkres po přání interpreta dostal
        jen skladby toho přání a zahrál je znovu za 0,2–0,9 h."""
        async def go():
            async with tw.Rig() as rig:
                rig.cfg.repeat_days = 5
                tc.as_app(rig)  # historie se píše jako v aplikaci
                await rig.background()
                w = rig.wq.submit("pusť Kabát", "Petr")
                await rig.until(lambda: w.state in ("queued", "playing") and bool(w.reply))
                heard: list[str] = []
                for _ in range(40):
                    cur = rig.fake.current_vid()
                    if cur and (not heard or heard[-1] != cur):
                        heard.append(cur)
                    if w.state == "done":
                        break
                    rig.fake.finish_current()
                    await rig.settle(0.15)
                self.assertEqual(w.state, "done")
                mine = [v for v in heard if "kab" in v]
                self.assertGreaterEqual(len(mine), 4)
                # dál hraje podkres (režim interpreta, pak podobné) — nic z toho, co už znělo
                rig.store.flush()
                after: list[str] = []
                for _ in range(14):
                    cur = rig.fake.current_vid()
                    if cur and cur not in heard and (not after or after[-1] != cur):
                        after.append(cur)
                    self.assertIsNotNone(cur)  # hudba nikdy neumlkla
                    rig.fake.finish_current()
                    await rig.settle(0.15)
                    for t in await rig.dj.next_tracks(max(0, 4 - len(rig.upcoming()))):
                        await rig.player.enqueue([t])
                self.assertEqual(len(after), len(set(after)))
                self.assertFalse(set(after) & set(heard))
                self.assertEqual([k for k, _ in rig.events if k == "repeat.leak"], [])

        run(go())

    def test_album_and_favourites_and_a_playlist_link_drop_what_was_played(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                rig.cfg.repeat_days = 5
                album = [T(f"alb{i}", f"Skladba alba {i}", "Kapela") for i in range(6)]
                played(rig.store, album[:2], ago_s=DAY)

                def wish(text: str = "x") -> Wish:
                    return Wish(id="w1", token="t", who="Petr", source="web", text=text,
                                created=time.time(), mono=time.monotonic(), cid=PETR)

                # album: dvě skladby vynechá a řekne to
                plan = Plan(intent=Intent(kind="album", text="album Deska"),
                            album_tracks=list(album), album_label="Kapela — Deska")
                cut = await rig.wq._drop_repeats(wish(), plan.intent, plan)
                self.assertEqual([t.id for t in plan.album_tracks], [t.id for t in album[2:]])
                self.assertEqual(cut.notes, ["Bez toho, co z něj tu v posledních 5 dnech už hrálo "
                                             "(2 skladby)."])
                self.assertEqual(cut.empty, "")
                # celé album už hrálo: nic se nezařadí a odpověď řekne proč a co s tím
                plan = Plan(intent=Intent(kind="album", text="album Deska"),
                            album_tracks=list(album[:2]), album_label="Kapela — Deska")
                cut = await rig.wq._drop_repeats(wish(), plan.intent, plan)
                self.assertEqual(plan.album_tracks, [])
                self.assertEqual(cut.empty, "Album Kapela — Deska tu v posledních 5 dnech hrálo celé "
                                            "(2 skladby), tak ho neopakuju — nic jsem nezařadil. "
                                            + REPEAT_HINT)
                # oblíbené (rychlá cesta i model) a playlist z odkazu jsou taky sady
                plan = Plan(intent=Intent(kind="song", text="pusť", note="link_list"),
                            requested=list(album))
                cut = await rig.wq._drop_repeats(wish("pusť"), plan.intent, plan)
                self.assertEqual([t.id for t in plan.requested], [t.id for t in album[2:]])
                self.assertEqual(cut.notes, ["Bez toho, co tu v posledních 5 dnech už hrálo "
                                             "(2 skladby)."])
                # oblíbené nedojdou (vlastník 16. 10.): po nehraných dokola
                plan = Plan(intent=Intent(kind="song", text="pusť oblíbené", note="favourites"),
                            requested=list(album))
                w = wish("pusť oblíbené")
                cut = await rig.wq._drop_repeats(w, plan.intent, plan)
                self.assertEqual([t.id for t in plan.requested], [t.id for t in album[2:] + album[:2]])
                self.assertEqual(w.again, {album[0].id, album[1].id})
                self.assertEqual(cut.notes, ["Nehraných oblíbených bylo jen 4, pak jedu dokola — "
                                             "od těch, co nejdéle nehrály."])
                # blok přání je jen začátek sady: vybírá se z celé, ne z prvních pár
                plan = Plan(intent=Intent(kind="song", text="pusť oblíbené", note="favourites"),
                            requested=list(album[:3]), candidates=list(album))
                cut = await rig.wq._drop_repeats(wish("pusť oblíbené"), plan.intent, plan)
                self.assertEqual([t.id for t in plan.requested], [t.id for t in album[2:5]])
                plan = Plan(intent=Intent(kind="song", text="pusť oblíbené", note="favourites"),
                            requested=list(album[:1]))
                cut = await rig.wq._drop_repeats(wish("pusť oblíbené"), plan.intent, plan)
                self.assertEqual(([t.id for t in plan.requested], cut.empty), ([album[0].id], ""))
                self.assertEqual(cut.notes, ["Všechny oblíbené tu v posledních 5 dnech už hrály, tak "
                                             "jedu dokola — od těch, co nejdéle nehrály."])
                # výslovně i s tím, co už hrálo (rozhodl model): nic se nevynechá
                plan = Plan(intent=Intent(kind="album", text="znovu album Deska", repeat_ok=True),
                            album_tracks=list(album), album_label="Kapela — Deska")
                w = wish()
                cut = await rig.wq._drop_repeats(w, plan.intent, plan)
                self.assertEqual((len(plan.album_tracks), cut.notes, cut.empty, w.repeat_ok),
                                 (6, [], "", True))

        run(go())

    def test_a_song_the_dj_picked_himself_is_swapped_for_one_not_played(self) -> None:
        """„Jednu od Olympicu" — skladbu vybral DJ, ne posluchač: hrála-li tu,
        zahraje jinou od téhož interpreta a řekne to."""
        async def go():
            async with tw.Rig() as rig:
                rig.cfg.repeat_days = 5
                await rig.background()
                pick = T("pick", "Želva", "Olympic")

                async def search_song(artist, title):
                    return pick

                rig.catalog.search_song = search_song
                tw.SCRIPT["zahraj jednu od Olympicu"] = ("codex", tw.decision(
                    action="play_next", requested=[{"artist": "Olympic", "title": "Želva"}]))
                played(rig.store, [pick] + tw.OLYMPIC[:2], ago_s=2 * 3600)
                w = rig.wq.submit("zahraj jednu od Olympicu", "Petr")
                await rig.until(lambda: w.state in ("queued", "playing") and bool(w.reply))
                self.assertEqual([t.id for t in w.tracks], [tw.OLYMPIC[2].id])
                self.assertIn("„Želva“ tu v posledních 5 dnech už hrálo, tak místo toho hraju od "
                              "Olympic něco, co nehrálo.", w.reply)

        run(go())


# --------------------------------------------------------------------------
# jediná výjimka: výslovné přání konkrétní skladby nebo opakování
# --------------------------------------------------------------------------


class Explicit(unittest.TestCase):
    def test_replay_button_plays_the_song_again(self) -> None:
        """Pi 9. 10. 18:33: „Znovu: Meowlau — Ostnatý drôt" 35 min po dohrání."""
        async def go():
            async with tw.Rig() as rig:
                rig.cfg.repeat_days = 5
                tc.as_app(rig)
                await rig.background()
                old = T("GZ4_w3IVZZU", "Ostnatý drôt", "Meowlau")
                played(rig.store, [old], ago_s=35 * 60)
                await rig.pools.repeats.refresh()
                w = rig.wq.replay(old, "Robert", client={"id": "client-robert"})
                await rig.until(lambda: w.state in ("queued", "playing") and bool(w.reply))
                self.assertEqual([t.id for t in w.tracks], [old.id])
                self.assertTrue(w.repeat_ok)
                self.assertNotIn("hrálo", w.reply)
                await rig.until(lambda: started(rig, old.id))
                await rig.until(lambda: any(k == "repeat.allowed" for k, _ in rig.events))
                ev = [f for k, f in rig.events if k == "repeat.allowed"][0]
                self.assertEqual((ev["video_id"], ev["why"]), (old.id, "replay"))
                self.assertEqual([k for k, _ in rig.events if k == "repeat.leak"], [])

        run(go())

    def test_a_song_named_in_the_wish_plays_even_if_the_model_forgot_the_flag(self) -> None:
        """Pi 8. 10. 14:26 „enter sandman", 9. 10. 10:25 „Zahraj zpíčenec od
        řezníka" — obojí hrálo před 2–3 dny a má hrát znovu."""
        async def go():
            async with tw.Rig() as rig:
                rig.cfg.repeat_days = 5
                await rig.background()
                song = T("jasna", "Jasná zpráva", "Olympic")

                async def search_song(artist, title):
                    return song

                rig.catalog.search_song = search_song
                played(rig.store, [song], ago_s=2 * DAY)
                # model nastavil repeat_ok = False (zapomněl), název ale v přání padl
                tw.SCRIPT["pusť Jasnou zprávu od Olympicu"] = ("codex", tw.decision(
                    action="play_next", requested=[{"artist": "Olympic", "title": "Jasná zpráva"}],
                    repeat_ok=False))
                w = rig.wq.submit("pusť Jasnou zprávu od Olympicu", "Jana")
                await rig.until(lambda: w.state in ("queued", "playing") and bool(w.reply))
                self.assertEqual([t.id for t in w.tracks], [song.id])
                self.assertEqual(w.named, {song.id})
                self.assertEqual(rig.wq.repeat_reason(song.id), "named")
                self.assertNotIn("hrálo", w.reply)

        run(go())

    def test_a_wish_that_clearly_wants_the_played_ones_gets_them(self) -> None:
        """Co přání znamená, řekne model (`repeat_ok`), ne seznam obratů."""
        async def go():
            async with tw.Rig() as rig:
                rig.cfg.repeat_days = 5
                await rig.background()
                played(rig.store, tw.KABAT[:4], ago_s=3600)
                text = "pusť Kabát, klidně i to, co už dneska hrálo"
                tw.SCRIPT[text] = ("codex", tw.decision(
                    action="start_radio", focus_artists=["Kabát"], repeat_ok=True))
                w = rig.wq.submit(text, "Petr")
                await rig.until(lambda: w.state in ("queued", "playing") and bool(w.reply))
                self.assertEqual([t.id for t in w.tracks[:4]], [t.id for t in tw.KABAT[:4]])
                self.assertTrue(w.repeat_ok)
                self.assertNotIn("hrálo", w.reply)
                # a jeho podkres (režim interpreta) taky smí
                await rig.until(lambda: rig.pools.artist == "Kabát")
                self.assertTrue(rig.pools.repeat_ok)
                # další přání bez toho už zase ne
                o = rig.wq.submit("Olympic prosím", "Jana")
                await rig.until(lambda: o.state in ("queued", "playing") and bool(o.reply))
                self.assertFalse(o.repeat_ok)

        run(go())

    def test_fast_path_knows_only_the_unambiguous_cases(self) -> None:
        """Bez modelu jen konkrétní skladba, odkaz na skladbu a „Zahrát znovu".
        Přání, které může chtít opakování, rychlá cesta nechá modelu."""
        self.assertIsNotNone(fastpath.parse("pusť Kabát"))
        for text in ("pusť znovu Kabát", "zase Kabát", "Kabát ještě jednou", "zopakuj Kabát",
                     "pusť Kabát, klidně i to, co už hrálo", "Kabát opakovaně", "opět Kabát"):
            self.assertIsNone(fastpath.parse(text), text)
        self.assertIsNotNone(fastpath.parse("ještě Kabát"))  # "další od něj", ne "totéž znovu"

        async def go():
            async with tw.Rig() as rig:
                from test_dj_fastpath import FuzzyCatalog

                rig.dj.catalog = FuzzyCatalog()
                plan = await rig.dj._fast_song_plan("pusť Jasnou zprávu od Olympicu")
                self.assertTrue(plan.intent.repeat_ok)
                rig.wq.catalog = SimpleNamespace(resolve_link=self._link)
                w = Wish(id="w", token="t", who="P", source="web", created=0.0, mono=0.0,
                         text="https://music.youtube.com/watch?v=dQw4w9WgXcQ")
                one = await rig.wq._link_plan(w)
                self.assertTrue(one.intent.repeat_ok)
                w.text = "https://music.youtube.com/playlist?list=PLx"
                many = await rig.wq._link_plan(w)
                self.assertEqual((many.intent.repeat_ok, many.intent.note), (False, "link_list"))

        run(go())

    @staticmethod
    async def _link(url: str):
        if "playlist" in url:
            return SimpleNamespace(kind="playlist", label="Playlist",
                                   tracks=[T(f"pl{i}", f"Z playlistu {i}", "Kapela") for i in range(5)])
        return SimpleNamespace(kind="track", label="", tracks=[T("dQw4w9WgXcQ", "Never", "Rick")])


class Model(unittest.TestCase):
    def test_schema_and_prompt_know_repeat_ok(self) -> None:
        self.assertEqual(DECISION_SCHEMA["properties"]["repeat_ok"], {"type": "boolean"})
        self.assertIn("repeat_ok", DECISION_SCHEMA["required"])
        self.assertIn("Pole `repeat_ok`", ROLE)
        self.assertIn("Rozhoduje smysl, ne jednotlivá slova", ROLE)

    def test_decision_carries_repeat_ok_only_for_a_listeners_music_wish(self) -> None:
        P = lambda a, t: {"artist": a, "title": t}  # noqa: E731
        song = build_intent("pusť Wonderwall", tw.decision(
            action="play_next", requested=[P("Oasis", "Wonderwall")], repeat_ok=True))
        self.assertEqual((song.kind, song.repeat_ok), ("songs", True))
        artist = build_intent("hraj Kabát, klidně i to, co už bylo", tw.decision(
            action="start_radio", focus_artists=["Kabát"], repeat_ok=True))
        self.assertEqual((artist.kind, artist.repeat_ok), ("artist", True))
        album = build_intent("pusť znovu album Load", tw.decision(
            action="start_radio", albums=[P("Metallica", "Load")], repeat_ok=True))
        self.assertEqual((album.kind, album.repeat_ok), ("album", True))
        self.assertFalse(build_intent("pusť Kabát", tw.decision(
            action="start_radio", focus_artists=["Kabát"])).repeat_ok)
        # nálada bere skladby z rádia — tam se neopakuje nikdy
        self.assertFalse(build_intent("něco veselého", tw.decision(
            action="start_radio", seeds=[P("A", "B")], repeat_ok=True)).repeat_ok)
        # automatický tah (přeseedování, rozjezd) taky nikdy
        self.assertFalse(build_intent("pooly došly", tw.decision(
            action="play_next", requested=[P("Oasis", "Wonderwall")], repeat_ok=True),
            auto=True).repeat_ok)
        self.assertFalse(build_intent("ahoj", tw.decision(action="nothing", repeat_ok=True)).repeat_ok)
        fav = build_intent("oblíbené, klidně i ty, co už hrály", {
            **tw.decision(action="favourites", repeat_ok=True), "favourites_scope": "office"})
        self.assertEqual((fav.favourites, fav.repeat_ok), ("office", True))
        # posluchač si písničku vybral v upřesnění
        picked = option_intent("lucie", {"kind": "song", "artist": "Lucie", "title": "Amerika",
                                         "label": "písnička Lucie — Amerika"})
        self.assertTrue(picked.repeat_ok)
        self.assertFalse(option_intent("lucie", {"kind": "artist", "artist": "Lucie", "title": "",
                                                 "label": "kapela Lucie"}).repeat_ok)


# --------------------------------------------------------------------------
# hlídka a restart
# --------------------------------------------------------------------------


class Watch(unittest.TestCase):
    def test_a_repeat_nobody_asked_for_is_reported_as_a_leak(self) -> None:
        """Kdyby nějaký zdroj skladeb pravidlo obešel, je to vidět v provozním
        logu (`repeat.leak`) — ne až na hlášení z kanceláře."""
        async def go():
            async with tw.Rig() as rig:
                rig.cfg.repeat_days = 5
                tc.as_app(rig)
                stray = T("stray", "Zatoulaná", "Kapela")
                played(rig.store, [stray], ago_s=1800)
                await rig.pools.repeats.refresh()
                await rig.player.enqueue([stray])  # mimo pooly i přání
                await rig.until(lambda: any(k == "repeat.leak" for k, _ in rig.events))
                ev = [f for k, f in rig.events if k == "repeat.leak"][0]
                self.assertEqual(ev["video_id"], stray.id)
                self.assertTrue(1700 <= ev["ago_s"] <= 1900, ev)

        run(go())

    def test_the_track_resumed_after_a_restart_is_not_a_repeat(self) -> None:
        """Pi 9. 10. 19:06: Walking on Sunshine navázala po restartu od 109. s —
        v historii je dvakrát, opakování to není. Co hrálo před restartem, ale
        podkres po restartu nenabídne."""
        async def go():
            async with tw.Rig() as rig:
                rig.cfg.repeat_days = 5
                tc.as_app(rig)
                cur = T("XI7tTVpc67k", "Walking on Sunshine", "Katrina and the Waves")
                before = [T(f"bef{i}", f"Před restartem {i}", f"Kapela {i}") for i in range(3)]
                played(rig.store, before, ago_s=600)
                played(rig.store, [cur], ago_s=120, outcome="started")
                rig.player.saved_playback = lambda *a: (cur, 109.0)  # type: ignore[method-assign]

                async def resume_track(track, pos):
                    await rig.player.enqueue([track])
                    return True

                rig.player.resume_track = resume_track  # type: ignore[method-assign]
                await rig.pools.repeats.refresh()
                at = await rig.wq._resume_interrupted()
                self.assertEqual(at, (cur.id, 109.0))
                await rig.until(lambda: any(k == "repeat.allowed" for k, _ in rig.events))
                ev = [f for k, f in rig.events if k == "repeat.allowed"][0]
                self.assertEqual((ev["video_id"], ev["why"]), (cur.id, "resume"))
                self.assertEqual([k for k, _ in rig.events if k == "repeat.leak"], [])
                # podkres po restartu: rádio vrací i to, co hrálo před ním
                rig.catalog.radio = self._radio(before + [cur])
                await rig.pools.set_seeds([T("seed", "Seed", "Seed")], mood="jako před restartem")
                got = {t.id for t in await rig.dj.next_tracks(6)}
                self.assertTrue(got)
                self.assertFalse(got & {t.id for t in before + [cur]})

        run(go())

    @staticmethod
    def _radio(first):
        async def radio(video_id, limit=50):
            return list(first) + OTHERS

        return radio

    def test_wish_keeps_its_exception_over_a_restart(self) -> None:
        w = Wish(id="w", token="t", who="P", source="web", text="x", created=time.time(),
                 mono=time.monotonic(), repeat_ok=True, named={"abc"})
        back = Wish.from_json(w.to_json())
        self.assertEqual((back.repeat_ok, back.named), (True, {"abc"}))
        plain = Wish.from_json(Wish(id="v", token="t", who="P", source="web", text="x",
                                    created=time.time(), mono=time.monotonic()).to_json())
        self.assertEqual((plain.repeat_ok, plain.named), (False, set()))


if __name__ == "__main__":
    unittest.main()
