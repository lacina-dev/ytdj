"""Přání "i s obrazem": DJ dodá oficiální klipy pro telku (POZADAVKY #72).

    .venv/bin/python -m unittest tests.test_wish_video -v

Vlastník 6. 10. 2026: „Chci ověřit video, ale asi by to chtělo, aby agent
reagoval na to, když chci video, a dodal video." Na Pi měl klipy zapnuté,
proklikal 20+ skladeb (i Rammstein Sonne, Du hast, Deutschland) a všechny
hrály jako písničky bez obrazu — katalog hledá písně, klip bývá jiné video.

Že posluchač chce obraz, pozná model (pole `want_video`, podle smyslu); tady
se zkouší, co s tím aplikace udělá. Bez sítě a bez modelu: falešný katalog,
falešné mpv, falešná telka. Skutečný model měří tests/model_wish_check.py.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_wishes as tw  # noqa: E402
from test_wishes import Rig, T, run  # noqa: E402
from test_wish_understanding import _YT, real_catalog  # noqa: E402

from ytdj.agent import fastpath  # noqa: E402
from ytdj.agent.codex import DECISION_SCHEMA  # noqa: E402
from ytdj.agent.intent import build_intent  # noqa: E402
from ytdj.agent.prompts import ROLE, render_state  # noqa: E402
from ytdj.music.catalog import OMV, Probe, Track, to_track  # noqa: E402

ATV = "MUSIC_VIDEO_TYPE_ATV"
UGC = "MUSIC_VIDEO_TYPE_UGC"
SONG = Track("sonnesong00", "Sonne", "Rammstein", "Mutter", 273)
CLIP = Track("sonneclip00", "Sonne", "Rammstein", None, 253, video_type=OMV)
LONG_SONG = Track("enjoysong00", "Enjoy the Silence", "Depeche Mode", "Violator", 373)
LONG_CLIP = Track("enjoyclip00", "Enjoy the Silence", "Depeche Mode", None, 280, video_type=OMV)
NOCLIP = Track("nerdicisong", "Nerdíci v neklidu", "Pondělníci", None, 81)
BAND_SONGS = [Track(f"ramsong{i:04d}", f"Píseň {i}", "Rammstein", None, 240) for i in range(8)]
BAND_CLIPS = [Track(f"ramclip{i:04d}", f"Klip {i}", "Rammstein", None, 240, video_type=OMV)
              for i in range(8)]


def decision(**kw) -> dict:
    base = tw.decision(albums=[], explicit_ok=False, want_video=False, question="", options=[])
    base.update(kw)
    return base


def P(artist: str, title: str) -> dict:
    return {"artist": artist, "title": title}


class Tv:
    """Vypínač klipů na telce (ytdj/tvvideo.TvVideo) — jen to, co DJ používá."""

    def __init__(self, enabled: bool = False, can: bool = True, why: str = "") -> None:
        self.enabled, self._can, self._why = enabled, can, why
        self.calls: list[tuple] = []

    def can(self) -> tuple[bool, str]:
        return self._can, self._why

    async def set(self, on: bool, who: str = "", client=None) -> dict:
        self.calls.append((on, who))
        self.enabled = on
        return {"on": on}


def teach(rig: Rig) -> None:
    cat = rig.catalog
    songs = {"sonne": SONG, "enjoy the silence": LONG_SONG, "nerdici v neklidu": NOCLIP}
    clips = {"sonne": CLIP, "enjoy the silence": LONG_CLIP}
    plain_song, plain_artist = cat.search_song, cat.artist_tracks
    cat.video_calls = []

    async def search_song(artist: str, title: str, strict: bool = False):
        return songs.get(tw.wishes.norm(title)) or await plain_song(artist, title)

    async def artist_tracks(name: str, limit: int = 50):
        if name == "Rammstein":
            return list(BAND_SONGS)
        return await plain_artist(name, limit)

    async def official_video(artist: str, title: str):
        cat.video_calls.append(("official_video", artist, title))
        return clips.get(tw.wishes.norm(title))

    async def artist_videos(name: str, limit: int = 50):
        cat.video_calls.append(("artist_videos", name))
        return list(BAND_CLIPS) if name == "Rammstein" else []

    async def radio(video_id: str, limit: int = 50):
        # rádio: napůl klipy, napůl písničky
        out = []
        for i in range(12):
            out.append(Track(f"radclip{i:04d}", f"Rádio klip {i}", f"Kapela {i}", None, 200, video_type=OMV))
            out.append(Track(f"radsong{i:04d}", f"Rádio píseň {i}", f"Skupina {i}", None, 200, video_type=ATV))
        return out

    cat.search_song, cat.artist_tracks, cat.radio = search_song, artist_tracks, radio
    cat.official_video, cat.artist_videos = official_video, artist_videos


class Decision(unittest.TestCase):
    def test_schema_and_prompt_describe_it_by_meaning(self):
        self.assertEqual(DECISION_SCHEMA["properties"]["want_video"], {"type": "boolean"})
        self.assertIn("want_video", DECISION_SCHEMA["required"])
        self.assertIn("Pole `want_video`", ROLE)
        self.assertIn("NIKDY true, když si o obraz neřekl", ROLE)

    def test_only_listeners_music_wishes_carry_it(self):
        yes = build_intent("Sonne i s videem", decision(
            action="play_next", requested=[P("Rammstein", "Sonne")], want_video=True))
        self.assertTrue(yes.want_video)
        self.assertFalse(build_intent("Sonne", decision(
            action="play_next", requested=[P("Rammstein", "Sonne")])).want_video)
        self.assertFalse(build_intent("jde tu pustit video?", decision(
            action="nothing", want_video=True, reply="Jde.")).want_video)
        # automatický tah (přeseedování) o obraz žádat nemůže
        self.assertFalse(build_intent("x", decision(
            action="start_radio", seeds=[P("A", "B")], want_video=True), auto=True).want_video)

    def test_catalogue_title_with_guests_is_still_the_named_song(self):
        """Skutečný model 6. 10.: k „pusť klip Uptown Funk od Marka Ronsona" vrátil
        název přesně z katalogu, „Uptown Funk (feat. Bruno Mars)" — a pravidlo
        „název v přání nepadl" z toho udělalo režim interpreta."""
        intent = build_intent("pusť klip Uptown Funk od Marka Ronsona", decision(
            action="play_next", want_video=True,
            requested=[P("Mark Ronson", "Uptown Funk (feat. Bruno Mars)")]))
        self.assertEqual((intent.kind, intent.artists), ("songs", []))
        # skladba, kterou nejmenoval, je dál přání interpreta
        intent = build_intent("pusť Marka Ronsona", decision(
            action="play_next", requested=[P("Mark Ronson", "Uptown Funk (feat. Bruno Mars)")]))
        self.assertEqual(intent.kind, "artist")

    def test_model_knows_whether_clips_are_possible(self):
        rig = Rig()
        self.assertEqual(rig.dj._tv_line(), "")  # jukebox bez telky: nic netvrdí
        rig.dj.tv = Tv(enabled=False)
        self.assertIn("vypnuté", rig.dj._tv_line())
        rig.dj.tv = Tv(enabled=True)
        self.assertEqual(rig.dj._tv_line(), "zapnuté")
        rig.dj.tv = Tv(can=False, why="Telka není připojená.")
        self.assertIn("Telka není připojená", rig.dj._tv_line())
        self.assertIn("Klipy na telce: zapnuté", render_state("", [], "x", [], "", tv="zapnuté"))
        self.assertNotIn("Klipy na telce", render_state("", [], "x", [], ""))


class Resolve(unittest.TestCase):
    def plan(self, text: str, data: dict):
        rig = Rig()
        teach(rig)
        return rig, run(rig.dj.resolve(build_intent(text, data)))

    def test_song_becomes_its_official_video(self):
        """Pi 6. 10. 20:49: „Sonne" hrála jako písnička, klip se nespustil."""
        rig, plan = self.plan("Sonne i s videem", decision(
            action="play_next", requested=[P("Rammstein", "Sonne")], want_video=True))
        self.assertEqual([t.id for t in plan.requested], [CLIP.id])
        self.assertEqual(plan.requested[0].video_type, OMV)
        self.assertEqual((plan.video, plan.notes), (1, []))

    def test_not_asked_never_a_video_version(self):
        rig, plan = self.plan("Sonne", decision(
            action="play_next", requested=[P("Rammstein", "Sonne")]))
        self.assertEqual([t.id for t in plan.requested], [SONG.id])  # přesně ta písnička
        self.assertEqual(plan.video, 0)
        self.assertEqual(rig.catalog.video_calls, [])  # klipy se ani nehledaly
        rig, plan = self.plan("pusť Rammstein", decision(
            action="start_radio", focus_artists=["Rammstein"]))
        self.assertEqual({t.id for t in plan.artist_tracks}, {t.id for t in BAND_SONGS})
        self.assertEqual(rig.catalog.video_calls, [])

    def test_no_official_video_plays_the_song_and_says_so(self):
        rig, plan = self.plan("nerdíci v neklidu s videem", decision(
            action="play_next", requested=[P("Pondělníci", "Nerdíci v neklidu")], want_video=True))
        self.assertEqual([t.id for t in plan.requested], [NOCLIP.id])
        self.assertEqual(plan.video, 0)
        self.assertEqual(plan.notes, ["Oficiální klip k Pondělníci — Nerdíci v neklidu nemám — "
                                      "hraju písničku (na telce bez obrazu)."])

    def test_different_recording_is_mentioned_only_when_it_differs_a_lot(self):
        rig, plan = self.plan("Enjoy the Silence klip", decision(
            action="play_next", requested=[P("Depeche Mode", "Enjoy the Silence")], want_video=True))
        self.assertEqual([t.id for t in plan.requested], [LONG_CLIP.id])  # 280 s místo 373 s
        self.assertEqual(plan.notes, ["Klip je jiná nahrávka než písnička z alba (jiná délka)."])

    def test_artist_plays_his_official_videos_most_known_first(self):
        rig, plan = self.plan("zahraj mi videa od Rammstein", decision(
            action="start_radio", focus_artists=["Rammstein"], want_video=True))
        self.assertEqual(plan.intent.kind, "artist")
        self.assertEqual([t.id for t in plan.artist_tracks], [t.id for t in BAND_CLIPS])
        self.assertEqual(plan.video, len(BAND_CLIPS))
        # interpret bez klipů: jeho písničky a poctivá poznámka
        rig, plan = self.plan("klipy od Kabátu", decision(
            action="start_radio", focus_artists=["Kabát"], want_video=True))
        self.assertTrue(plan.artist_tracks and all(t.artist == "Kabát" for t in plan.artist_tracks))
        self.assertEqual(plan.video, 0)
        self.assertIn("Oficiální klipy od Kabát nemám — hraju písničky", " ".join(plan.notes))

    def test_mood_seeds_become_videos(self):
        rig, plan = self.plan("dej tam nějaké klipy", decision(
            action="start_radio", mood="hity", want_video=True,
            seeds=[P("Rammstein", "Sonne"), P("Depeche Mode", "Enjoy the Silence"),
                   P("Pondělníci", "Nerdíci v neklidu")]))
        self.assertEqual([t.id for t in plan.seeds], [CLIP.id, LONG_CLIP.id])  # jen ty s klipem
        self.assertEqual(plan.video, 2)

    def test_video_lookups_run_alongside_the_song_lookups(self):
        import asyncio
        import time

        async def go():
            rig = Rig()
            teach(rig)
            rig.catalog.delay = 0.3
            find = rig.catalog.official_video

            async def slow_video(artist, title):
                await asyncio.to_thread(time.sleep, 0.3)
                return await find(artist, title)

            rig.catalog.official_video = slow_video
            t0 = time.monotonic()
            plan = await rig.dj.resolve(build_intent("Sonne i s videem", decision(
                action="play_next", requested=[P("Rammstein", "Sonne")], want_video=True)))
            self.assertEqual(plan.requested[0].id, CLIP.id)
            self.assertLess(time.monotonic() - t0, 0.55)  # souběžně, ne 0,3 + 0,3

        run(go())


class Catalogue(unittest.TestCase):
    def test_official_video_is_omv_by_that_artist_only(self):
        """Skutečný katalog 6. 10.: mezi „oficiálními" jsou i cizí nahrávky pod
        cizím jménem a zpomalené verze; první výsledek bývá video fanouška."""
        def v(vid, kind, artist, title, views="1M", dur="4:13"):
            return {"videoId": vid, "videoType": kind, "artists": [{"name": artist}], "title": title,
                    "views": views, "duration": dur, "duration_seconds": 253}

        yt = _YT(mixed=[
            v("fanloop0001", UGC, "looped", "Rammstein - Sonne {slowed + reverb}"),
            v("StZcUAPRRac", OMV, "Rammstein", "Sonne", "309M"),
            v("stranger001", OMV, "Někdo Cizí", "Rammstein - Sonne", "43K"),
            v("lyrics00001", UGC, "Jack", "Rammstein Sonne Lyrics (German)", "3.2M"),
            v("ramlive0001", OMV, "Rammstein", "Sonne (Live)", "9M"),
        ])
        cat = real_catalog(yt)
        hit = run(cat.official_video("Rammstein", "Sonne"))
        self.assertEqual(yt.calls, [("search", "Rammstein Sonne", "videos")])
        self.assertEqual((hit.id, hit.video_type, hit.artist), ("StZcUAPRRac", OMV, "Rammstein"))
        # nic oficiálního od něj → None (žádné video fanouška ani s textem)
        only_fans = _YT(mixed=[x for x in yt.mixed if x["videoType"] == UGC or x["artists"][0]["name"] != "Rammstein"])
        self.assertIsNone(run(real_catalog(only_fans).official_video("Rammstein", "Sonne")))
        self.assertIsNone(run(cat.official_video("Rammstein", "Úplně jiná píseň")))

    def test_artist_videos_are_official_and_most_known_first(self):
        class YT:
            def search(self, query, filter=None, limit=20):
                return [{"artist": "Rammstein", "browseId": "UCram"}]

            def get_artist(self, browse_id):
                return {"videos": {"browseId": "VLvideos", "results": [
                    {"videoId": "sonne000001", "title": "Sonne"}, {"videoId": "duhast00001", "title": "Du hast"}]}}

            def get_playlist(self, playlist_id, limit=50):
                def t(vid, title, kind=OMV):
                    return {"videoId": vid, "title": title, "videoType": kind,
                            "artists": [{"name": "Rammstein"}], "duration_seconds": 240}
                return {"tracks": [t("newest00001", "Nový živák"), t("duhast00001", "Du hast"),
                                   t("fanupload01", "Fan cut", UGC), t("sonne000001", "Sonne"),
                                   {**t("gone0000001", "Nedostupné"), "isAvailable": False}]}

        cat = real_catalog(_YT())
        cat.yt = YT()
        vids = run(cat.artist_videos("Rammstein"))
        self.assertEqual([t.id for t in vids], ["sonne000001", "duhast00001", "newest00001"])
        self.assertTrue(all(t.video_type == OMV for t in vids))

    def test_tracks_and_probe_carry_the_kind(self):
        t = to_track({"videoId": "abcdefghijk", "title": "X", "artists": [{"name": "A"}],
                      "videoType": OMV})
        self.assertEqual(t.video_type, OMV)
        self.assertEqual(t, Track("abcdefghijk", "X", "A"))  # rovnost bez ohledu na druh
        yt = _YT(mixed=[{"resultType": "video", "title": "Sonne", "videoType": OMV,
                         "artists": [{"name": "Rammstein"}]},
                        {"resultType": "video", "title": "Sonne lyrics", "videoType": UGC,
                         "artists": [{"name": "Jack"}]}])
        probe = run(real_catalog(yt).probe("Sonne klip"))
        self.assertEqual(probe.videos, ["Rammstein — Sonne [oficiální klip]", "Jack — Sonne lyrics"])
        self.assertIsInstance(probe, Probe)


class Queue(unittest.TestCase):
    def test_video_wish_plays_the_clip_and_switches_clips_on(self):
        async def go():
            text = "Sonne i s videem"
            tw.SCRIPT[text] = ("codex", decision(
                action="start_radio", requested=[P("Rammstein", "Sonne")], want_video=True))
            try:
                async with Rig() as rig:
                    teach(rig)
                    rig.wq.tv = tv = Tv(enabled=False)
                    await rig.background()
                    w = rig.wq.submit(text, "kolega")
                    await rig.until(lambda: w.state in ("queued", "playing") and bool(w.reply))
                    await rig.settle(0.2)
                    self.assertEqual([t.id for t in w.tracks], [CLIP.id])
                    self.assertTrue(w.want_video)
                    self.assertEqual(tv.calls, [(True, "kolega (přáním)")])  # je vidět, kdo je zapnul
                    self.assertIn("Zapnul jsem Klipy na telce.", w.reply)
                    ev = [f for k, f in rig.events if k == "request.video"]
                    self.assertEqual((ev[0]["ok"], ev[0]["videos"], ev[0]["switched_on"]), (True, 1, True))
                    # podkres tohohle přání: jen skladby, které samy jsou klip
                    self.assertTrue(rig.pools.video_only)
                    nxt = await rig.dj.next_tracks(5)
                    self.assertTrue(nxt and all(t.video_type == OMV for t in nxt), nxt)
                    # další přání (bez obrazu) → podkres zase jako dřív; vypínač zůstává, jak je
                    j = rig.wq.submit("Holky z naší školky", "kolegyně")
                    await rig.until(lambda: j.state in ("queued", "playing"))
                    self.assertFalse(rig.pools.video_only)
                    self.assertFalse(j.want_video)
                    self.assertEqual(len(tv.calls), 1)
                    rig.pools.session_seen.clear()
                    self.assertTrue(any(t.video_type != OMV for t in await rig.dj.next_tracks(6)))
            finally:
                tw.SCRIPT.pop(text, None)

        run(go())

    def test_clips_already_on_are_left_alone(self):
        async def go():
            text = "zahraj mi videa od Rammstein"
            tw.SCRIPT[text] = ("codex", decision(
                action="start_radio", focus_artists=["Rammstein"], want_video=True))
            try:
                async with Rig() as rig:
                    teach(rig)
                    rig.wq.tv = tv = Tv(enabled=True)
                    w = rig.wq.submit(text, "kolega")
                    await rig.until(lambda: w.state in ("queued", "playing") and bool(w.reply))
                    self.assertEqual(tv.calls, [])
                    self.assertNotIn("Zapnul jsem", w.reply)
                    await rig.settle(0.2)
                    self.assertEqual(len(w.tracks), 4)  # rozpočet přání, zbytek je podkres
                    self.assertTrue(all(t.video_type == OMV for t in w.tracks))
                    self.assertEqual(rig.pools.artist, "Rammstein")  # podkres: jeho klipy
                    self.assertTrue(all(t.video_type == OMV for t in await rig.dj.next_tracks(3)))
            finally:
                tw.SCRIPT.pop(text, None)

        run(go())

    def test_tv_cannot_show_video_song_stays_exact_and_reply_says_why(self):
        async def go():
            text = "Sonne i s videem"
            tw.SCRIPT[text] = ("codex", decision(
                action="play_next", requested=[P("Rammstein", "Sonne")], want_video=True))
            try:
                for tv in (Tv(can=False, why="Telka není připojená."), None):
                    async with Rig() as rig:
                        teach(rig)
                        rig.wq.tv = tv
                        w = rig.wq.submit(text, "kolega")
                        await rig.until(lambda: w.state in ("queued", "playing") and bool(w.reply))
                        self.assertEqual([t.id for t in w.tracks], [SONG.id])  # písnička, ne klip
                        self.assertFalse(w.want_video)
                        self.assertIn("S obrazem to teď nejde", w.reply)
                        self.assertEqual(rig.catalog.video_calls, [])
                        if tv is not None:
                            self.assertEqual(tv.calls, [])
                            self.assertIn("Telka není připojená", w.reply)
            finally:
                tw.SCRIPT.pop(text, None)

        run(go())

    def test_no_clip_found_does_not_touch_the_switch(self):
        async def go():
            text = "nerdíci v neklidu s videem"
            tw.SCRIPT[text] = ("codex", decision(
                action="play_next", requested=[P("Pondělníci", "Nerdíci v neklidu")], want_video=True))
            try:
                async with Rig() as rig:
                    teach(rig)
                    rig.wq.tv = tv = Tv(enabled=False)
                    w = rig.wq.submit(text, "kolega")
                    await rig.until(lambda: w.state in ("queued", "playing") and bool(w.reply))
                    self.assertEqual([t.id for t in w.tracks], [NOCLIP.id])
                    self.assertIn("Oficiální klip k Pondělníci — Nerdíci v neklidu nemám", w.reply)
                    self.assertEqual(tv.calls, [])
            finally:
                tw.SCRIPT.pop(text, None)

        run(go())


class FastPath(unittest.TestCase):
    """Rychlá cesta přijímá jen jistotu; přání, které chce i obraz, jde k modelu."""

    TEXTS = ("pusť klip Uptown Funk od Marka Ronsona", "Uptown Funk i s videem",
             "zahraj mi videa od Rammstein", "chci to vidět na telce", "dej tam nějaké klipy",
             "Rammstein klipy", "Sonne od Rammstein na telce")

    def test_wishes_for_picture_are_never_taken_as_a_plain_song_or_artist(self):
        from test_dj_fastpath import FuzzyCatalog
        from ytdj.music.catalog import Artist

        funk = Track("uf", "Uptown Funk", "Mark Ronson")
        sonne = Track("so", "Sonne", "Rammstein")

        class Cat(FuzzyCatalog):
            async def find_artist(self, name):
                q = tw.wishes.norm(name)
                for a in ("Rammstein", "Mark Ronson"):
                    if tw.wishes.norm(a)[:5] in q:
                        return Artist(a, "UC" + a)
                return None

            async def search(self, query, limit=8):
                return [funk, sonne]  # obyčejné hledání skladbu najde

            async def search_song(self, artist, title, strict=False):
                t = tw.wishes.norm(title)
                return funk if "uptown funk" in t else sonne if "sonne" in t else None

            async def artist_tracks(self, name, limit=50):
                return [sonne] if name == "Rammstein" else [funk]

        for text in self.TEXTS:
            cat = Cat()
            self.assertTrue(run(fastpath.find_artists(cat, text)).reason, text)
            self.assertIsNone(run(fastpath.find_song(cat, text)).track, text)
        # bez zmínky o obrazu jde táž skladba rychlou cestou dál
        self.assertEqual(run(fastpath.find_song(Cat(), "Uptown Funk od Marka Ronsona")).track.id, "uf")


if __name__ == "__main__":
    unittest.main()
