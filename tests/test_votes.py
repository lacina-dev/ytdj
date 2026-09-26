"""Hlasování kanceláře (PLAN H): prahy, kanonické klíče, filtr podkresu,
výslovné přání s poznámkou, úklid fronty, oblíbené, API a stavový snímek.

    YTDJ_EVENTS_FILE=/tmp/ev.jsonl .venv/bin/python -m unittest tests.test_votes -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_TMP = tempfile.mkdtemp(prefix="ytdj-votes-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)
os.environ.setdefault("YTDJ_EVENTS_FILE", str(Path(_TMP) / "events.jsonl"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_wishes as tw  # noqa: E402  (Rig: opravdový MpvPlayer nad falešným mpv)
from ytdj import votes as V  # noqa: E402
from ytdj.agent.codex import Plan  # noqa: E402
from ytdj.agent.context import start_context, start_instruction  # noqa: E402
from ytdj.agent.intent import Intent, favourites_request  # noqa: E402
from ytdj.config import DEFAULTS, Config  # noqa: E402
from ytdj.display import Censor  # noqa: E402
from ytdj.music.catalog import Track  # noqa: E402
from ytdj.music.radio import RadioPools  # noqa: E402
from ytdj.nicks import tag_of  # noqa: E402
from ytdj.state import Store  # noqa: E402
from ytdj.votes import (ARTIST, BANNED, DOWN, FAVOURITE, NEUTRAL, PENDING, SONG,  # noqa: E402
                        VoteBook, VoteError, enforce)
from ytdj.web import server as web  # noqa: E402
from ytdj.web import votes_api  # noqa: E402

PETR, JANA, KAREL, EVA = "client-petr", "client-jana", "client-karel", "client-eva"
DAMA = Track("dama0000001", "Malá dáma (Official Video)", "Kabát")
DAMA_LIVE = Track("dama0000002", "Malá dáma [Live]", "Kabát - Topic")
DAMA_VIDEO = Track("dama0000003", "Kabát - Malá dáma", "Kabát")
POHODA = Track("pohoda00001", "Pohoda", "Kabát")
ZPRAVA_DUO = Track("zprava00001", "Jasná zpráva - Remastered 2011", "Olympic & Petr Janda")
ZPRAVA = Track("zprava00002", "Jasná zpráva", "Olympic")


def book(**cfg) -> VoteBook:
    c = Config(**{**DEFAULTS, **cfg})
    clock = {"t": 1000.0}

    def mono() -> float:
        clock["t"] += 1.0
        return clock["t"]

    b = VoteBook(cfg=c, censor=Censor(), rng=lambda: 0.0, mono=mono)
    b._clock = clock  # type: ignore[attr-defined]
    return b


class Keys(unittest.TestCase):
    def test_other_upload_or_version_is_the_same_song(self):
        k = V.song_key(DAMA.artist, DAMA.title)
        self.assertEqual(k, "kabat|mala dama")
        self.assertIn(k, V.song_keys(DAMA_LIVE.artist, DAMA_LIVE.title))
        self.assertIn(k, V.song_keys(DAMA_VIDEO.artist, DAMA_VIDEO.title))
        self.assertNotIn(k, V.song_keys(POHODA.artist, POHODA.title))
        # spolupráce: "Olympic & Petr Janda" i samotný "Olympic"
        self.assertEqual(V.song_key(ZPRAVA_DUO.artist, ZPRAVA_DUO.title),
                         V.song_key(ZPRAVA.artist, ZPRAVA.title))

    def test_credits(self):
        self.assertEqual(V.credits("A, B & C feat. D"), ("A", "B & C", "B", "C", "D"))
        self.assertEqual(V.credits("Simon & Garfunkel")[0], "Simon & Garfunkel")
        self.assertEqual(V.credits("Kabát - Topic"), ("Kabát",))
        self.assertEqual(V.artist_key("The Beatles"), V.artist_key("Beatles"))


class Thresholds(unittest.TestCase):
    def test_song_statuses(self):
        b = book()
        b.cast(SONG, PETR, -1, "Petr", track=DAMA)
        self.assertEqual(b.tally(SONG, "kabat|mala dama").status, DOWN)
        self.assertIsNone(b.blocked(DAMA))
        r = b.cast(SONG, JANA, -1, "Jana", track=DAMA_LIVE)  # jiná verze → týž cíl
        self.assertEqual(r.key, "kabat|mala dama")
        self.assertEqual(r.changed, "ban")
        self.assertEqual(b.blocked(DAMA_VIDEO), SONG)
        # dva 👍 proti dvěma 👎: 👎 není víc → zpátky
        b.cast(SONG, KAREL, 1, "Karel", track=DAMA)
        r = b.cast(SONG, EVA, 1, "Eva", track=DAMA)
        self.assertEqual(r.changed, "unban")
        self.assertEqual(r.after, NEUTRAL)
        self.assertIsNone(b.blocked(DAMA))
        # 👍 a víc 👍 než 👎 = oblíbená
        b.cast(SONG, PETR, 1, "Petr", track=POHODA)
        self.assertEqual(b.tally(SONG, V.song_key(POHODA.artist, POHODA.title)).status, FAVOURITE)
        self.assertTrue(b.is_favourite(POHODA))

    def test_one_voter_cannot_ban_and_threshold_is_config(self):
        b = book(ban_song_votes=3, ban_artist_votes=2)
        b.cast(SONG, PETR, -1, track=DAMA)
        b.cast(SONG, PETR, -1, track=DAMA_LIVE)  # týž člověk znovu = pořád jeden hlas
        self.assertEqual(b.tally(SONG, "kabat|mala dama").down, 1)
        b.cast(SONG, JANA, -1, track=DAMA)
        self.assertIsNone(b.blocked(DAMA))  # práh 3
        self.assertEqual(b.item(SONG, "kabat|mala dama")["need"], 1)
        b.cast(ARTIST, PETR, -1, artist="Olympic")
        self.assertEqual(b.tally(ARTIST, "olympic").status, PENDING)
        b.cast(ARTIST, JANA, -1, artist="Olympic")
        self.assertEqual(b.tally(ARTIST, "olympic").status, BANNED)

    def test_artist_thumbs_up_and_down(self):
        b = book()
        r = b.cast(ARTIST, PETR, 1, artist="Kabát")
        # jeden 👍 celého interpreta z něj oblíbeného neudělá (favourite_artist_votes 2)
        self.assertEqual((r.after, r.item["up"]), (NEUTRAL, 1))
        self.assertFalse(b.is_favourite(POHODA))
        r = b.cast(ARTIST, KAREL, 1, artist="Kabát")
        self.assertEqual((r.after, r.item["up"]), (FAVOURITE, 2))
        b.cast(ARTIST, KAREL, 0, artist="Kabát")  # jako předtím: jen Petr
        b.cast(ARTIST, EVA, 1, artist="Kabát")
        self.assertTrue(b.is_favourite(POHODA))  # skladba oblíbeného interpreta
        self.assertTrue(b.is_favourite(Track("x1", "Song", "Tomáš Klus feat. Kabát")))
        for v in (JANA, KAREL):
            b.cast(ARTIST, v, -1, artist="Kabát")
        self.assertEqual(b.tally(ARTIST, "kabat").status, PENDING)  # 2 👎 pod prahem 3
        r = b.cast(ARTIST, EVA, -1, artist="Kabát")
        self.assertEqual((r.after, r.changed), (BANNED, "ban"))  # 3 👎 > 1 👍
        self.assertFalse(b.is_favourite(POHODA))
        b.cast(ARTIST, "client-ota", 1, artist="Kabát")
        r = b.cast(ARTIST, "client-iva", 1, artist="Kabát")  # 3 👍 : 3 👎 — už ne víc 👎
        self.assertEqual((r.after, r.changed), (PENDING, "unban"))
        r = b.cast(ARTIST, "client-ema", 1, artist="Kabát")  # 4 : 3
        self.assertEqual(r.after, FAVOURITE)
        with self.assertRaises(VoteError):
            b.cast(ARTIST, PETR, 2, artist="Kabát")


class ArtistCredits(unittest.TestCase):
    def test_ban_matches_any_credited_artist(self):
        b = book()
        for v in (PETR, JANA, KAREL):
            b.cast(ARTIST, v, -1, track=POHODA)  # bez jména = první uvedený (Kabát)
        self.assertEqual(b.tally(ARTIST, "kabat").status, BANNED)
        self.assertEqual(b.blocked(Track("x1", "Song", "Tomáš Klus, Kabát")), ARTIST)
        self.assertEqual(b.blocked(Track("x2", "Song", "Tomáš Klus feat. Kabát")), ARTIST)
        self.assertEqual(b.blocked(Track("x3", "Song", "Kabát & Olympic")), ARTIST)
        self.assertIsNone(b.blocked(Track("x4", "Song", "Kabátová")))
        self.assertIsNone(b.blocked(Track("x5", "Song", "Olympic")))
        # režim interpreta, který si někdo vyžádal: jeho vlastní vyřazení neplatí
        self.assertIsNone(b.blocked(POHODA, exempt=["Kabát"]))


class ChangeAndWithdraw(unittest.TestCase):
    def test_change_withdraw_and_persist(self):
        d = Path(tempfile.mkdtemp(dir=_TMP))
        store = Store(d / "state.db")
        b = VoteBook(store, Config(**DEFAULTS))
        b.cast(SONG, PETR, -1, "Petr", track=DAMA)
        r = b.cast(SONG, JANA, -1, "Jana", track=DAMA)
        self.assertEqual(r.after, BANNED)
        r = b.cast(SONG, JANA, 1, "Jana", track=DAMA)  # změna názoru
        self.assertEqual((r.prev, r.after, r.changed), (-1, NEUTRAL, "unban"))
        r = b.cast(SONG, PETR, 0, "Petr", key=r.key)  # stažení ze seznamu (podle klíče)
        self.assertEqual((r.item["up"], r.item["down"], r.item["status"]), (1, 0, FAVOURITE))
        # kdo a kdy zůstává i u staženého hlasu (vote 0)
        rows = store.all_votes()
        self.assertEqual({(row[2], row[3]) for row in rows}, {(PETR, 0), (JANA, 1)})
        # po restartu stejný stav
        again = VoteBook(store, Config(**DEFAULTS)).load()
        self.assertEqual(again.tally(SONG, "kabat|mala dama").status, FAVOURITE)
        self.assertEqual(again.item(SONG, "kabat|mala dama")["voters"][0]["nick"], "Jana")
        store.close()


class RateLimit(unittest.TestCase):
    def test_30_per_10_minutes(self):
        b = book()
        for i in range(V.RATE_MAX):
            b.cast(SONG, PETR, 1 if i % 2 else -1, track=Track(f"v{i}", f"Song {i}", "X"))
        with self.assertRaises(VoteError) as cm:
            b.cast(SONG, PETR, 1, track=DAMA)
        self.assertEqual(cm.exception.status, 429)
        b.cast(SONG, JANA, 1, track=DAMA)  # ostatní hlasují dál
        b._clock["t"] += V.RATE_WINDOW + 1  # type: ignore[attr-defined]
        b.cast(SONG, PETR, 1, track=DAMA)


class Nicks(unittest.TestCase):
    def test_names_go_through_office_filter_and_registry(self):
        b = book()
        b.cast(SONG, PETR, -1, "kokot", track=DAMA)
        voters = b.item(SONG, "kabat|mala dama")["voters"]
        self.assertNotIn("kokot", json.dumps(voters, ensure_ascii=False))
        self.assertEqual(voters[0]["tag"], tag_of(PETR))
        # zapojené do aplikace: přezdívka z registru má přednost před "who"
        wq = tw.WishQueue(SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), None,
                          Config(**DEFAULTS))
        wq.nicks.set(JANA, "Jana K.")
        app = SimpleNamespace(store=None, cfg=Config(**DEFAULTS), wishes=wq, pools=None, dj=None)
        wired = V.wire(app)
        wired.cast(SONG, JANA, -1, "whatever", track=DAMA)
        wired.cast(SONG, KAREL, -1, "kokot", track=DAMA)
        names = [v["nick"] for v in wired.item(SONG, "kabat|mala dama")["voters"]]
        self.assertEqual(names[0], "Jana K.")
        self.assertNotIn("kokot", names[1])
        self.assertIn("(pozn.: vyřazená hlasováním — Jana K., ", wired.ban_note(DAMA_LIVE))


class Pools(unittest.TestCase):
    def _pools(self, tracks: list[Track], b: VoteBook, recent: set[str] = frozenset()):
        async def radio(video_id, limit=50):
            return list(tracks)

        store = SimpleNamespace(blacklisted=lambda: set(), recently_played=lambda d: set(recent),
                                record_seed=lambda *a: None, recent_history=lambda n: [])
        pools = RadioPools(SimpleNamespace(radio=radio), store, Config(**DEFAULTS))
        pools.votes = b
        return pools

    def test_banned_never_downweighted_half_favourite_again(self):
        b = book()
        tracks = [DAMA, POHODA, ZPRAVA, Track("fav00000001", "Fav", "Nobody", duration=200)]
        for t in tracks:
            t.duration = 200
        b.cast(SONG, PETR, -1, track=DAMA)
        b.cast(SONG, JANA, -1, track=DAMA)  # vyřazená
        b.cast(SONG, PETR, -1, track=POHODA)  # upozaděná
        b.cast(SONG, JANA, 1, track=tracks[3])  # oblíbená

        async def go(rng):
            b.rng = rng
            pools = self._pools(tracks, b, recent={tracks[3].id})
            await pools.set_seeds([Track("seed0000001", "S", "Seed")])
            return [t.id for t in await pools.next_tracks(10)]

        got = asyncio.run(go(lambda: 0.9))  # upozaděná neprošla
        self.assertNotIn(DAMA.id, got)
        self.assertNotIn(POHODA.id, got)
        self.assertIn(ZPRAVA.id, got)
        self.assertIn(tracks[3].id, got)  # oblíbená i přes repeat_days
        got = asyncio.run(go(lambda: 0.1))  # upozaděná prošla
        self.assertIn(POHODA.id, got)
        self.assertNotIn(DAMA.id, got)


class FavouriteArtists(unittest.TestCase):
    def test_pool_boost_and_repeat_days(self):
        b = book()
        radio = [Track(f"r{i:010d}", f"Song {i}", f"Band {i}", duration=200) for i in range(20)]
        kab = Track("kab0000000x", "Burlaci", "Kabát", duration=200)
        tracks = radio[:15] + [kab] + radio[15:]
        b.cast(ARTIST, PETR, 1, artist="Kabát")
        b.cast(ARTIST, JANA, 1, artist="Kabát")  # oblíbený až od dvou lidí
        pools = Pools._pools(self, tracks, b, recent={kab.id})
        b.pools = pools  # jako wire(): pool_reject posune oblíbené dopředu

        async def go():
            await pools.set_seeds([Track("seed0000001", "S", "Seed")])
            return [t.id for t in await pools.next_tracks(10)]

        got = asyncio.run(go())
        # 16. v rádiu, po posunu o BOOST_LIFT (8) míst mezi prvními deseti —
        # a i když hrál v posledních repeat_days
        self.assertIn(kab.id, got)
        self.assertLessEqual(got.index(kab.id), 15 - V.BOOST_LIFT)
        # podruhé se už neposouvá (každá skladba jednou)
        self.assertEqual(b.boost_pools(pools.pools), 0)

    def test_favourite_mix_adds_artist_songs(self):
        b = book()
        fav_song = Track("fav00000001", "Fav", "Nobody")
        b.cast(SONG, JANA, 1, track=fav_song)
        b.cast(ARTIST, JANA, 1, artist="Olympic")
        b.cast(ARTIST, EVA, 1, artist="Olympic")
        b.cast(ARTIST, PETR, 1, artist="Kabát")
        b.cast(ARTIST, EVA, 1, artist="Kabát")
        for v in (PETR, KAREL):  # vyřazená skladba oblíbeného interpreta se nepustí
            b.cast(SONG, v, -1, track=tw.OLYMPIC[0])

        class Cat:
            async def artist_tracks(self, name, limit=50):
                return list(tw.ARTISTS.get(name.lower(), []))[:limit]

        office = asyncio.run(b.favourite_mix(Cat()))
        self.assertEqual(office[0].id, fav_song.id)
        arts = [t.artist for t in office]
        self.assertEqual(arts.count("Olympic"), V.ARTIST_MIX)
        self.assertEqual(arts.count("Kabát"), V.ARTIST_MIX)
        self.assertNotIn(tw.OLYMPIC[0].id, [t.id for t in office])
        mine = asyncio.run(b.favourite_mix(Cat(), PETR))  # Petr: jen Kabát
        self.assertEqual({t.artist for t in mine}, {"Kabát"})
        # jeden 👍 ("moje oblíbené" ano, oblíbené kanceláře ne)
        b.cast(ARTIST, KAREL, 1, artist="Queen")
        self.assertNotIn("Queen", b.favourite_artists())
        self.assertEqual(b.favourite_artists(KAREL), ["Queen"])

        class Slow:
            async def artist_tracks(self, name, limit=50):
                await asyncio.sleep(5)
                return []

        only = asyncio.run(b.favourite_mix(Slow(), timeout=0.05))  # katalog nestihl
        self.assertEqual([t.id for t in only], [fav_song.id])

    def test_dj_context_names_favourite_artists(self):
        b = book()
        b.cast(ARTIST, PETR, 1, artist="Olympic")
        self.assertNotIn("Oblíbení interpreti kanceláře", b.describe())  # jeden 👍 nestačí
        b.cast(ARTIST, JANA, 1, artist="Olympic")
        self.assertIn("Oblíbení interpreti kanceláře (👍): Olympic", b.describe())
        self.assertIn("interpret Olympic", b.summary()[0])


class ContextForDJ(unittest.TestCase):
    def test_describe_and_start_context(self):
        d = Path(tempfile.mkdtemp(dir=_TMP))
        store = Store(d / "state.db")
        b = VoteBook(store, Config(**DEFAULTS))
        b.cast(SONG, PETR, 1, "Petr", track=ZPRAVA)
        for v in (PETR, JANA, KAREL):
            b.cast(ARTIST, v, -1, artist="Kabát")
        text = b.describe()
        self.assertIn("Oblíbené kanceláře (👍): Olympic — Jasná zpráva", text)
        self.assertIn("interpreti Kabát", text)
        self.assertLess(len(text), 600)
        ctx = start_context(store, cfg=Config(**DEFAULTS))
        instr = start_instruction(ctx)
        self.assertIn("Olympic — Jasná zpráva", instr)
        self.assertIn("Vyřazené hlasováním", instr)
        self.assertLessEqual(len(instr), 800)
        store.close()

    def test_favourites_phrases(self):
        self.assertEqual(favourites_request("pusť moje oblíbené"), "mine")
        self.assertEqual(favourites_request("Pusť oblíbené"), "office")
        self.assertEqual(favourites_request("hraj oblíbené kanceláře"), "office")
        self.assertEqual(favourites_request("něco z mých oblíbených"), "mine")
        self.assertIsNone(favourites_request("pusť oblíbené od Kabátu"))
        self.assertIsNone(favourites_request("pusť Kabát"))


# --------------------------------------------------------------------------
# s frontou přání a přehrávačem (tests/test_wishes.Rig)
# --------------------------------------------------------------------------


def _wire(rig) -> tuple[VoteBook, SimpleNamespace]:
    app = SimpleNamespace(store=rig.store, cfg=rig.cfg, wishes=rig.wq, pools=rig.pools,
                          dj=rig.dj, player=rig.player)
    return V.wire(app), app


class WithQueue(unittest.TestCase):
    def test_explicit_wish_for_banned_song_is_honoured_with_note(self):
        async def go():
            async with tw.Rig() as rig:
                b, _ = _wire(rig)
                holky = tw.T("s-Holky", "Olympic")  # co najde katalog na "Holky"
                b.cast(SONG, PETR, -1, "Petr", track=Track("other000001", holky.title + " (Live)",
                                                             "Olympic"))
                b.cast(SONG, JANA, -1, "Jana", track=holky)
                self.assertEqual(b.blocked(holky), SONG)
                w = rig.wq.submit("Holky z naší školky", "Karel")
                await rig.until(lambda: w.state in ("queued", "playing") and w.reply)
                self.assertIn("(pozn.: vyřazená hlasováním — Petr, Jana)", w.reply)
                self.assertEqual(w.tracks[0].id, holky.id)
                self.assertIn("vote.honoured", rig.kinds())

        tw.run(go())

    def test_banned_artist_wish_note_and_set_drops_banned_songs(self):
        b = book()
        for v in (PETR, JANA, KAREL):
            b.cast(ARTIST, v, -1, "X", artist="Kabát")
        b.cast(SONG, PETR, -1, track=tw.KABAT[1])
        b.cast(SONG, JANA, -1, track=tw.KABAT[1])
        dj = SimpleNamespace(votes=b)
        plan = Plan(intent=Intent(kind="artist", text="pusť Kabát", artists=["Kabát"]),
                    artist_tracks=list(tw.KABAT[:5]))
        from ytdj.agent.codex import CodexDJ

        CodexDJ._office_votes(dj, plan)  # type: ignore[arg-type]
        self.assertEqual([t.id for t in plan.artist_tracks],
                         [t.id for t in tw.KABAT[:5] if t is not tw.KABAT[1]])
        self.assertTrue(any("Kabát je vyřazený hlasováním" in n for n in plan.notes), plan.notes)

    def test_ban_cleans_background_but_keeps_wishes(self):
        async def go():
            async with tw.Rig() as rig:
                b, app = _wire(rig)
                await rig.background()
                tw.SCRIPT["pusť rádiový hit"] = ("codex", tw.decision(
                    action="play_next", requested=[{"artist": "Radio 2", "title": "Hit"}],
                    reply="Zařazuju Hit."))
                w = rig.wq.submit("pusť rádiový hit", "Jana")
                await rig.until(lambda: w.state in ("queued", "playing"))
                await rig.settle(0.2)
                wish_id = w.tracks[0].id
                before = [v for v in rig.upcoming() + [rig.fake.current_vid()]
                          if v != wish_id and rig.pools.known[v].artist == "Radio 2"]
                self.assertTrue(before, "podkres má mít skladbu Radio 2")
                for v in (PETR, KAREL, EVA):
                    r = b.cast(ARTIST, v, -1, artist="Radio 2")
                self.assertEqual(r.changed, "ban")
                await enforce(app, "test")
                await rig.settle(0.2)
                left = rig.upcoming() + [rig.fake.current_vid()]
                self.assertIn(wish_id, left)  # Janino přání zůstává
                for v in before:
                    self.assertNotIn(v, rig.upcoming())
                self.assertIn("vote.cleanup", rig.kinds())

        tw.run(go())

    def test_banned_background_playing_is_skipped_wish_is_not(self):
        async def go():
            async with tw.Rig() as rig:
                b, app = _wire(rig)
                await rig.background()
                cur = rig.fake.current_vid()
                artist = rig.pools.known[cur].artist
                for v in (PETR, JANA, KAREL):
                    b.cast(ARTIST, v, -1, artist=artist)
                eff = await enforce(app, "test")
                await rig.settle(0.2)
                self.assertTrue(eff["skipped"])
                self.assertNotEqual(rig.fake.current_vid(), cur)
                self.assertIn("vote.skip", rig.kinds())
                # přání, které hraje, se nepřeskočí, i když je vyřazené
                w = rig.wq.submit("pusť Kabát", "Jana")
                await rig.until(lambda: w.state == "playing" or
                                rig.fake.current_vid() == tw.KABAT[0].id)
                for v in (PETR, JANA, KAREL):
                    b.cast(ARTIST, v, -1, artist="Kabát")
                eff = await enforce(app, "test")
                self.assertFalse(eff["skipped"])
                self.assertEqual(rig.fake.current_vid(), tw.KABAT[0].id)

        tw.run(go())

    def test_play_favourites_office_and_mine(self):
        async def go():
            async with tw.Rig() as rig:
                b, _ = _wire(rig)
                fav = [tw.T(f"fav{i}", f"Fav {i}") for i in range(3)]
                b.cast(SONG, JANA, 1, "Jana", track=fav[0])
                b.cast(SONG, KAREL, 1, "Karel", track=fav[1])
                b.cast(SONG, KAREL, 1, "Karel", track=fav[2])
                b.cast(SONG, PETR, -1, "Petr", track=fav[2])  # 1:1 — už ne oblíbená kanceláře
                w = rig.wq.submit("pusť oblíbené", "Petr")
                await rig.until(lambda: w.state in ("queued", "playing") and w.reply)
                self.assertEqual({t.id for t in w.tracks}, {fav[0].id, fav[1].id})
                self.assertIn("oblíbené kanceláře", w.reply)
                self.assertEqual(rig.store.top_requested(), [])  # nejsou to "vyžádané jménem"
                m = rig.wq.submit("pusť moje oblíbené", "Karel")
                await rig.until(lambda: m.state in ("queued", "playing"))
                # co právě hraje, do přání nepatří (wishes: request.already_playing)
                self.assertEqual({t.id for t in m.tracks} | {rig.fake.current_vid()},
                                 {fav[1].id, fav[2].id} | {rig.fake.current_vid()})
                self.assertIn(fav[2].id, {t.id for t in m.tracks})
                # oblíbený interpret: "pusť oblíbené" hraje i jeho známé skladby
                b.cast(ARTIST, EVA, 1, "Eva", artist="Olympic")
                b.cast(ARTIST, JANA, 1, "Jana", artist="Olympic")  # oblíbený od dvou lidí
                o = rig.wq.submit("pusť oblíbené", "Jana")
                await rig.until(lambda: o.state in ("queued", "playing") and o.reply)
                self.assertTrue(any(t.artist == "Olympic" for t in o.tracks), o.tracks)
                self.assertIn(fav[0].id, {t.id for t in o.tracks} | {rig.fake.current_vid()})
                n = rig.wq.submit("pusť moje oblíbené", "Ota")
                await rig.until(lambda: n.state == "notfound")
                self.assertIn("nemáš žádné oblíbené", n.reply)

        tw.run(go())


# --------------------------------------------------------------------------
# HTTP API (bez sítě — obslužné funkce přímo)
# --------------------------------------------------------------------------


class FakePlayer:
    def __init__(self) -> None:
        self.current = Track("cur00000001", "Malá dáma", "Kabát, Tomáš Klus", duration=200)
        self.queue = [DAMA_LIVE, ZPRAVA]
        self.skipped = 0

    async def status(self):
        return SimpleNamespace(playing=True, paused=False, buffering=False, current=self.current,
                               position=1.0, duration=200.0, queue=list(self.queue), volume=50,
                               quality="")

    async def remove_upcoming(self, ids):
        n = len([t for t in self.queue if t.id in ids])
        self.queue = [t for t in self.queue if t.id not in ids]
        return n

    async def skip(self, by_user=True):
        self.skipped += 1


def request(body: dict | None = None, query: dict | None = None):
    async def read_body():
        return json.dumps(body or {}).encode()

    return SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={"user-agent": "test"},
                           body=read_body, query_params=query or {},
                           url=SimpleNamespace(path="/api/votes"), method="POST")


class Api(unittest.TestCase):
    def _server(self):
        player = FakePlayer()
        wq = tw.WishQueue(SimpleNamespace(), player, SimpleNamespace(), None, Config(**DEFAULTS))
        wq.nicks.set(PETR, "Petr")
        app = SimpleNamespace(player=player, wishes=wq, store=None, cfg=Config(**DEFAULTS),
                              pools=SimpleNamespace(describe=lambda: "", mood="", artist=""),
                              dj=None)
        V.wire(app)
        srv = web.WebServer(app)
        handlers = {}
        for r in srv._starlette.routes:
            for m in getattr(r, "methods", None) or ():
                handlers[(r.path, m)] = r.endpoint
        return srv, app, handlers

    def test_vote_flow_and_status_block(self):
        async def go():
            srv, app, h = self._server()
            post, get = h[("/api/votes", "POST")], h[("/api/votes", "GET")]
            # bez id klienta nic
            resp = await post(request({"target": "song", "vote": -1}))
            self.assertEqual(resp.status_code, 400)
            # 👎 hrající (bez video_id), Petr podle přezdívky
            resp = await post(request({"target": "song", "vote": -1, "client": PETR, "who": "X"}))
            out = json.loads(resp.body)
            self.assertEqual(out["item"]["voters"][0]["nick"], "Petr")
            self.assertEqual(out["item"]["status"], DOWN)
            self.assertIn("chybí ještě 1×", out["message"])
            # bez přezdívky hlasovat nejde (vždy musí být jasné, kdo hlasoval)
            resp = await post(request({"target": "song", "vote": -1, "client": JANA, "who": "Jana"}))
            self.assertEqual(resp.status_code, 403)
            app.wishes.nicks.set(JANA, "Jana")
            resp = await post(request({"target": "song", "vote": -1, "client": JANA, "who": "Jana"}))
            out = json.loads(resp.body)
            self.assertEqual(out["changed"], "ban")
            # hrající byl podkres → přeskočen; DAMA_LIVE (jiná verze) z fronty pryč
            self.assertEqual(app.player.skipped, 1)
            self.assertEqual([t.id for t in app.player.queue], [ZPRAVA.id])
            self.assertEqual(out["effect"], {"removed": 1, "skipped": True})
            # interpret: 👍 hrajícímu (první uvedený = Kabát), 👎 jinému po jménu
            resp = await post(request({"target": "artist", "vote": 1, "client": PETR}))
            out = json.loads(resp.body)
            self.assertEqual((out["item"]["key"], out["item"]["status"], out["item"]["up"]),
                             ("kabat", NEUTRAL, 1))  # jeden 👍 celého interpreta nestačí
            self.assertIn("až mu 👍 dají aspoň 2 lidé", out["message"])
            resp = await post(request({"target": "artist", "vote": 1, "client": JANA,
                                       "artist": "Kabát"}))
            out = json.loads(resp.body)
            self.assertEqual((out["item"]["key"], out["item"]["status"], out["item"]["up"]),
                             ("kabat", FAVOURITE, 2))
            self.assertIn("oblíbenými interprety", out["message"])
            resp = await post(request({"target": "artist", "vote": 1, "client": PETR,
                                       "artist": "Olympic"}))
            resp = await post(request({"target": "artist", "vote": 1, "client": JANA,
                                       "artist": "Olympic"}))
            self.assertEqual(json.loads(resp.body)["item"]["status"], FAVOURITE)
            resp = await post(request({"target": "artist", "vote": -1, "client": PETR,
                                       "artist": "Tomáš Klus"}))
            self.assertEqual(json.loads(resp.body)["item"]["status"], PENDING)
            # seznamy
            lists = json.loads((await get(request(query={"client": JANA}))).body)
            self.assertEqual([i["key"] for i in lists["banned"]], ["kabat|mala dama"])
            self.assertEqual({i["key"] for i in lists["favourites"]}, {"kabat", "olympic"})
            self.assertEqual({i["target"] for i in lists["favourites"]}, {ARTIST})
            self.assertEqual(lists["banned"][0]["mine"], -1)
            self.assertEqual([i["key"] for i in lists["pending"]], ["tomasklus"])
            self.assertEqual(lists["rules"], {"ban_song_votes": 2, "ban_artist_votes": 3,
                                              "favourite_artist_votes": 2})
            self.assertEqual(len(lists["mine"]), 3)  # Mala dáma 👎, Kabát 👍, Olympic 👍
            # detail hrající skladby: skladba + každý uvedený interpret
            det = json.loads((await h[("/api/votes/track", "GET")](
                request(query={"client": PETR}))).body)
            self.assertEqual([a["name"] for a in det["artists"]], ["Kabát", "Tomáš Klus"])
            self.assertEqual(det["artists"][1]["mine"], -1)
            self.assertEqual((det["artists"][0]["mine"], det["artists"][0]["up"]), (1, 2))
            # stavový snímek: malý blok, značky místo jmen
            snap = await srv._snapshot()
            cur = snap["current"]["votes"]
            self.assertEqual((cur["down"], cur["status"]), (2, BANNED))
            self.assertEqual(set(cur["down_by"]), {tag_of(PETR), tag_of(JANA)})
            self.assertEqual(cur["artists"][1]["status"], PENDING)
            self.assertEqual(cur["artists"][1]["by"], [tag_of(PETR)])  # starší čtenáři: "by" = 👎
            self.assertEqual(cur["artists"][1]["down_by"], [tag_of(PETR)])
            k = cur["artists"][0]
            self.assertEqual((k["status"], k["up"], set(k["up_by"])),
                             (FAVOURITE, 2, {tag_of(PETR), tag_of(JANA)}))
            self.assertNotIn("by", k)
            # Jasná zpráva: o skladbě se nehlasovalo, ale Olympic je oblíbený
            qv = snap["queue"][0]["votes"]
            self.assertEqual((qv["status"], qv["artist_status"]), (NEUTRAL, FAVOURITE))
            # stažení
            resp = await post(request({"target": "song", "vote": 0, "client": JANA,
                                       "key": "kabat|mala dama"}))
            out = json.loads(resp.body)
            self.assertEqual((out["changed"], out["item"]["status"]), ("unban", DOWN))

        tw.run(go())

    def test_rate_limit_is_429(self):
        async def go():
            srv, app, h = self._server()
            post = h[("/api/votes", "POST")]
            codes = []
            for i in range(V.RATE_MAX + 1):
                resp = await post(request({"target": "song", "vote": 1 - 2 * (i % 2),
                                           "client": PETR}))
                codes.append(resp.status_code)
            self.assertEqual(codes[-1], 429)
            self.assertEqual(set(codes[:-1]), {200})

        tw.run(go())

    def test_status_without_votes_module(self):
        # starší / testovací aplikace bez hlasování: snímek beze změny
        cur = {"id": "a", "title": "t", "artist": "x"}
        votes_api.annotate(SimpleNamespace(), cur, [])
        self.assertNotIn("votes", cur)


if __name__ == "__main__":
    unittest.main()
