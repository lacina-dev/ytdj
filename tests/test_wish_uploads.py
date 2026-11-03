"""Kapela, která na YouTube Music není — hraje se z nahrávek posluchačů.

    YTDJ_EVENTS_FILE=/tmp/x.jsonl .venv/bin/python -m unittest tests.test_wish_uploads -v

Pi 3. 11. 2026: Michal „Zahraj kapelu Tleskač". YouTube Music Tleskač nezná
(hledání interpretů i skladeb 0), na YouTube jsou jen videa, která nahráli
posluchači („Tleskač - Zelená je tráva", kanál 1bua1). DJ zahrál „Tomáš
Herudek — Tleskac" (cizí skladba toho jména, F-PRANI-20). Vlastník: „ano,
myslel jsem, že to tak už dělá" — kapela se má hrát z těch nahrávek a říct to.

Nahrávky v tests/fixtures/ytm_uploads/*.json jsou skutečné odpovědi
ytmusicapi (anglický katalog, location=CZ, anonymně) z 3. 11. 2026, jen
s poli, která katalog čte. Bez sítě a bez modelu (rozhodnutí modelu je
skriptované tak, jak ho model na Pi vrátil).
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_wishes as tw  # noqa: E402
from test_music_match import replay_catalog  # noqa: E402
from test_wishes import Rig, run  # noqa: E402

from ytdj.agent.fastpath import called_band  # noqa: E402
from ytdj.agent.intent import build_intent  # noqa: E402
from ytdj.music import match  # noqa: E402
from ytdj.music.catalog import UGC, Track  # noqa: E402
from ytdj.votes import song_keys  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ytm_uploads"
WISH = "Zahraj kapelu Tleskač"  # doslova z provozního logu 3. 11.


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def decision(name: str, **kw) -> dict:
    """Jak model na Pi rozhodl: start_radio, focus_artists [jméno]."""
    base = tw.decision(action="start_radio", focus_artists=[name], mood=name,
                       reply=f"Pouštím kapelu {name}.", albums=[], explicit_ok=False)
    base.update(kw)
    return base


def resolve(fixture: str, text: str, name: str, **kw):
    """DJ.resolve nad opravdovým Catalogem a nahranými odpověďmi YouTube Music."""
    rig = Rig()
    cat, replay = replay_catalog(load(fixture)["calls"])
    rig.dj.catalog = cat
    plan = run(rig.dj.resolve(build_intent(text, decision(name, **kw))))
    return plan, replay


def ugc(vid: str, song: str, artist: str = "Tleskač") -> Track:
    return Track(vid, song, artist, duration=200, video_type=UGC)


def video(title: str, channel: str = "1bua1", views: str = "10K", length: str = "3:30",
          vid: str = "x") -> dict:
    return {"resultType": "video", "title": title, "videoId": vid, "views": views,
            "duration": length, "videoType": UGC, "artists": [{"name": channel}]}


class Matching(unittest.TestCase):
    """match.upload_credit / match.uploads — čisté funkce nad názvy videí."""

    def test_title_must_start_with_the_name_and_a_separator(self):
        self.assertEqual(match.upload_credit("Tleskač - Zelená je tráva", "Tleskač"),
                         "Zelená je tráva")
        self.assertEqual(match.upload_credit("TleSKAč - krámy,,", "Tleskač"), "krámy")
        self.assertEqual(match.upload_credit("Tleskač-Klasy", "Tleskač"), "Klasy")
        self.assertEqual(match.upload_credit("Tleskač: Pokoj v suterénu", "tleskac"),
                         "Pokoj v suterénu")
        self.assertEqual(match.upload_credit("Tleskač | Jihlava (Official Video)", "Tleskač"),
                         "Jihlava")
        self.assertEqual(match.upload_credit("Tleskač - Zelená je tráva.avi", "Tleskač"),
                         "Zelená je tráva")
        self.assertEqual(match.upload_credit("Ska-Pra - Song", "Ska-Pra"), "Song")
        # bez oddělovače nejde poznat, kde jméno končí; cizí kapela vepředu nesedí
        self.assertIsNone(match.upload_credit("Tleskač Doktorka Práv", "Tleskač"))
        self.assertIsNone(match.upload_credit("TLESKAČ feat RANGO Muchacho", "Tleskač"))
        self.assertIsNone(match.upload_credit("Záhada hlavolamu - Deník Jana Tleskače", "Tleskač"))
        self.assertIsNone(match.upload_credit("Daniel Landa - Vlkodlakům", "Vlkodlak"))

    def test_one_song_once_most_viewed_studio_take_wins(self):
        items = [
            video("Tleskač - Zelená je tráva", views="91K", vid="pso"),
            video("Tleskač - Zelená je Tráva, DWBH live (Léto s Rychtářem 08)", views="3.5M",
                  vid="live"),
            video("Tleskač - Zelená je tráva", views="2M", vid="1bua1"),
            video("tleskač - zelená je tráva,  mělnické vinobraní", views="386K", vid="melnik"),
        ]
        got = match.uploads(items, "Tleskač")
        self.assertEqual([c.id for c in got], ["1bua1"])
        self.assertEqual(got[0].artists, ("Tleskač",))  # interpret je kapela, ne kanál

    def test_what_is_not_a_song_of_the_band_stays_out(self):
        items = [
            video("Tleskač - Zelená je tráva (cover)", vid="cover"),
            video("Tleskač - Zelená je tráva karaoke", vid="karaoke"),
            video("Tleskač - Celý koncert Lucerna", length="1:27:55", vid="koncert"),
            video("Tleskač - Jihlava", length="0:41", vid="short"),
            video("Tleskač - Best of", vid="best"),
            video("Tleskač - live 2005 2/4", vid="dil"),
            video("Tleskač - Sprengisandi", views="2", vid="nobody"),  # nikdo to neviděl
            video("Tleskač Doktorka Práv", vid="nosep"),
            {"resultType": "playlist", "title": "Tleskač - Never Die Songs"},
            {"resultType": "episode", "title": "Tleskač - podcast", "videoId": "pod",
             "videoType": "MUSIC_VIDEO_TYPE_PODCAST_EPISODE"},
            video("Tleskač - Lesní manekýn", vid="ok"),
        ]
        self.assertEqual([c.id for c in match.uploads(items, "Tleskač")], ["ok"])

    def test_the_bands_own_channel_counts_even_without_the_name_in_the_title(self):
        items = [video("Doktorka práv (Official Video)", channel="Tleskač Official", vid="own",
                       views="300")]
        got = match.uploads(items, "Tleskač")
        self.assertEqual([(c.id, c.title) for c in got], [("own", "Doktorka práv")])

    def test_live_recordings_after_the_rest(self):
        items = [video("Tleskač - Mrtvá holka (živě)", views="9M", vid="zive"),
                 video("Tleskač - Holá řiť, Masters of Rock 2011", views="8M", vid="rok"),
                 video("Tleskač - Krutej John", views="1K", vid="studio")]
        self.assertEqual([c.id for c in match.uploads(items, "Tleskač")],
                         ["studio", "zive", "rok"])

    def test_band_word_in_the_wish(self):
        self.assertTrue(called_band(WISH, "Tleskač"))
        self.assertTrue(called_band("něco od skupiny Tleskače", "Tleskač"))
        self.assertTrue(called_band("pusť kapelu Hlava 22", "Hlava 22"))
        self.assertFalse(called_band("Zahraj Tleskač", "Tleskač"))
        self.assertFalse(called_band("Nerdici v neklidu od kapely Polednici", "Nerdíci v neklidu"))


class RealCatalog(unittest.TestCase):
    """Catalog nad nahranými odpověďmi YouTube Music z 3. 11."""

    def test_tleskac_songs_from_listener_uploads(self):
        data = load("tleskac")
        cat, replay = replay_catalog(data["calls"])
        self.assertEqual(run(cat.artist_tracks("Tleskač", limit=100)), [])  # YTM ho nezná
        got = run(cat.artist_uploads("Tleskač"))
        self.assertEqual([[t.id, t.title] for t in got], data["uploads"])
        self.assertEqual([t.title for t in got[:4]], [
            "Zelená je tráva", "Nafoukanej chlapeček", "Příběh Jana Tleskače",
            "Hrozný Hroznýš z Hrozenkova"])
        self.assertTrue(all(t.artist == "Tleskač" and t.video_type == UGC for t in got))
        self.assertTrue(all(60 <= (t.duration or 0) <= 900 for t in got))
        keys = [match.upload_song_key(t.title) for t in got]
        self.assertEqual(len(keys), len(set(keys)))  # každá píseň jednou
        ids = {t.id for t in got}
        self.assertNotIn("vmhTUxCe6Is", ids)  # „Tleskač Doktorka Práv" — bez oddělovače
        self.assertNotIn("DXYlSiCBwL4", ids)  # cizí skladba „Tleskac" (Tomáš Herudek)
        self.assertNotIn("SBRGtlizbOc", ids)  # „tleskač live 2005 2/4" — díl záznamu
        # dost písní z hledání videí → playlisty se nehledají (o dotaz míň)
        self.assertNotIn(("search", "Tleskač", "community_playlists"), replay.log)

    def test_playlist_named_like_the_band_only_when_it_is_theirs(self):
        """Vlkodlak: v hledání nic, playlist „vlkodlak" je směs všeho — nepočítá se."""
        cat, replay = replay_catalog(load("vlkodlak")["calls"])
        self.assertEqual(run(cat.artist_uploads("Vlkodlak")), [])
        self.assertIn(("search", "Vlkodlak", "community_playlists"), replay.log)
        self.assertTrue(any(m == "get_playlist" for m, _, _ in replay.log))

        class FewVideos:
            calls: list = []

            def search(self, query, filter=None, limit=20):
                if filter == "videos":
                    return [video("Tleskač - Krutej John", vid="v1")]
                return [{"resultType": "playlist", "title": "TleSKAč", "playlistId": "PLok"},
                        {"resultType": "playlist", "title": "SKA z ČeSKA", "playlistId": "PLmix"}]

            def get_playlist(self, pid, limit=100):
                self.calls.append(pid)
                return {"tracks": [video(f"Tleskač - Píseň {i}", vid=f"p{i}", views=None)
                                   for i in range(4)] + [video("FOXIUM - Mantra", vid="cizi")]}

        yt = FewVideos()
        cat.yt = yt
        got = run(cat.artist_uploads("Tleskač"))
        self.assertEqual(yt.calls, ["PLok"])  # jen playlist pojmenovaný po kapele
        self.assertEqual({t.id for t in got}, {"v1", "p0", "p1", "p2", "p3"})

    def test_radio_from_an_upload_names_the_band(self):
        """„Tleskač a podobné": rádio z nahrávky je plné dalších nahrávek
        posluchačů — interpret je kapela, ne kanál, aby neopakování a hlasování
        poznaly tutéž píseň."""
        data = load("tleskac")
        cat, _ = replay_catalog(data["calls"])
        first = run(cat.artist_uploads("Tleskač"))[0]
        radio = run(cat.radio(first.id))
        labels = [t.label() for t in radio]
        self.assertEqual(labels[:10], data["radio"])
        self.assertIn("Tleskač — Zelená je tráva", labels)
        # „Tleskač - …" už v rádiu není (bez oddělovače nejde poznat, kde je název)
        self.assertFalse([t for t in radio if match.upload_credit(t.title, "Tleskač")])
        same = next(t for t in radio if t.id == first.id)
        self.assertTrue(song_keys(same.artist, same.title) & song_keys("Tleskač", first.title))
        # katalogové skladby (ATV/OMV) se nepřepisují
        self.assertIn("Sto zvířat — Nikdy nic nebylo", labels)


class Wishes(unittest.TestCase):
    """DJ.resolve: kdy kapela z nahrávek, kdy skladba (F-PRANI-20), kdy nic."""

    def test_kapelu_tleskac_plays_the_band_not_somebody_elses_song(self):
        plan, replay = resolve("tleskac", WISH, "Tleskač")
        self.assertEqual(plan.failed, "")
        self.assertEqual(plan.intent.kind, "artist")
        self.assertEqual(plan.intent.artists, ["Tleskač"])
        self.assertEqual(plan.requested, [])  # žádný Tomáš Herudek
        self.assertEqual([t.title for t in plan.artist_tracks[:3]],
                         ["Zelená je tráva", "Nafoukanej chlapeček", "Příběh Jana Tleskače"])
        self.assertEqual(plan.uploads, ["Tleskač"])
        self.assertIn("Tleskač na YouTube Music jako interpret není — hraju nahrávky, které na "
                      "YouTube nahráli posluchači.", plan.notes)
        self.assertFalse(any("je to skladba" in n or "nenašel" in n for n in plan.notes))
        self.assertEqual(plan.intent.reply, "")  # odpověď modelu slibovala interpreta z katalogu
        # „kapelu X" se jako název skladby nezkouší vůbec
        self.assertNotIn(("search", "Tleskač", "songs"), replay.log)

    def test_bare_name_with_many_uploads_is_still_the_band(self):
        plan, replay = resolve("tleskac", "Zahraj Tleskač", "Tleskač")
        self.assertEqual(plan.intent.kind, "artist")
        self.assertEqual(plan.requested, [])
        self.assertGreaterEqual(len(plan.artist_tracks), 10)
        # skladba toho jména se hledala (souběžně), ale proti 20 písním kapely neobstojí
        self.assertIn(("search", "Tleskač", "songs"), replay.log)

    def test_bare_song_title_still_plays_the_song(self):
        """F-PRANI-20 platí dál: „zahraj Vlkodlak" je skladba Wanastowi Vjecy."""
        plan, _ = resolve("vlkodlak", "zahraj Vlkodlak", "Vlkodlak")
        self.assertEqual(plan.failed, "")
        self.assertEqual(plan.intent.kind, "song")
        self.assertEqual([t.label() for t in plan.requested], ["Wanastowi Vjecy — Vlkodlak"])
        self.assertTrue(any("je to skladba" in n for n in plan.notes), plan.notes)
        self.assertEqual(plan.uploads, [])

    def test_named_band_with_nothing_is_not_replaced_by_a_song(self):
        """„chci skupinu Vlkodlak": skladbu Vlkodlak od jiné kapely nehrát — říct, že není."""
        plan, replay = resolve("vlkodlak", "chci skupinu Vlkodlak", "Vlkodlak")
        self.assertIn("Od Vlkodlak jsem v katalogu nic nenašel.", plan.failed)
        self.assertEqual(plan.requested, [])
        self.assertNotIn(("search", "Vlkodlak", "songs"), replay.log)

    def test_artist_in_the_catalogue_never_looks_for_uploads(self):
        rig = Rig()
        asked: list[str] = []

        async def artist_uploads(name, limit=50):
            asked.append(name)
            return [ugc("u1", "x", name)]

        rig.catalog.artist_uploads = artist_uploads
        plan = run(rig.dj.resolve(build_intent("pusť Kabát", decision("Kabát"))))
        self.assertEqual(asked, [])
        self.assertEqual(plan.uploads, [])
        self.assertTrue(plan.artist_tracks)
        self.assertTrue(all(t.artist == "Kabát" for t in plan.artist_tracks))

    def test_few_uploads_lose_to_a_real_song_of_that_name(self):
        """Dvě nahrávky „X - …" nestačí přebít skladbu X (F-PRANI-20)."""
        rig = Rig()
        song = Track("songvlkodl", "Vlkodlak", "Wanastowi Vjecy", duration=240)

        async def artist_uploads(name, limit=50):
            return [ugc("u1", "Píseň A", name), ugc("u2", "Píseň B", name)]

        async def search_song(artist, title, strict=False):
            return song if match.norm(title) == "vlkodlak" else None

        rig.catalog.artist_uploads = artist_uploads
        rig.catalog.search_song = search_song
        plan = run(rig.dj.resolve(build_intent("zahraj Vlkodlak", decision("Vlkodlak"))))
        self.assertEqual([t.id for t in plan.requested], ["songvlkodl"])
        self.assertEqual(plan.uploads, [])
        # bez té skladby by se hrály ty dvě nahrávky
        rig.catalog.search_song = lambda *a, **k: _none()
        plan = run(rig.dj.resolve(build_intent("zahraj Vlkodlak", decision("Vlkodlak"))))
        self.assertEqual([t.id for t in plan.artist_tracks], ["u1", "u2"])
        self.assertEqual(plan.uploads, ["Vlkodlak"])

    def test_video_wish_does_not_claim_there_is_no_picture(self):
        """Přání s obrazem: nahrávky posluchačů jsou videa — „na telce bez
        obrazu" by nebyla pravda (F-TV-09 je u přání s obrazem ukáže)."""
        rig = Rig()

        async def artist_uploads(name, limit=50):
            return [ugc("u1", "Zelená je tráva"), ugc("u2", "Krutej John")]

        async def none(*a, **k):
            return None

        async def no_videos(name, limit=50):
            return []

        rig.catalog.artist_uploads = artist_uploads
        rig.catalog.official_video = none
        rig.catalog.artist_videos = no_videos
        plan = run(rig.dj.resolve(build_intent(WISH + " i s klipem", decision(
            "Tleskač", want_video=True))))
        self.assertEqual([t.id for t in plan.artist_tracks], ["u1", "u2"])
        self.assertIn("Oficiální klipy od Tleskač nejsou — hraju videa, která nahráli "
                      "posluchači.", plan.notes)
        self.assertFalse(any("bez obrazu" in n for n in plan.notes), plan.notes)


async def _none():
    return None


class Queue(unittest.TestCase):
    """Celá fronta přání: odpověď, skladby přání a podkres po něm."""

    def test_reply_tracks_and_then_band_and_similar(self):
        uploads = [ugc(f"tleskac{i:03d}", s) for i, s in enumerate(
            ["Zelená je tráva", "Nafoukanej chlapeček", "Příběh Jana Tleskače",
             "Hrozný Hroznýš z Hrozenkova", "Krutej John", "Pavlačová story"])]

        async def go():
            async with Rig() as rig:
                async def artist_uploads(name, limit=50):
                    return list(uploads) if match.norm(name) == "tleskac" else []

                rig.catalog.artist_uploads = artist_uploads
                tw.SCRIPT[WISH] = ("codex", decision("Tleskač"))
                await rig.background()
                w = rig.wq.submit(WISH, "Michal")
                await rig.until(lambda: w.state in ("queued", "playing"))
                await rig.settle(0.3)
                self.assertTrue(w.reply.startswith(
                    "Zařadil jsem Tleskač: Zelená je tráva (Tleskač), Nafoukanej chlapeček "
                    "(Tleskač), Příběh Jana Tleskače (Tleskač)"), w.reply)
                self.assertIn("Tleskač na YouTube Music jako interpret není — hraju nahrávky, "
                              "které na YouTube nahráli posluchači.", w.reply)
                self.assertNotIn("Pouštím kapelu", w.reply)
                self.assertTrue(w.tracks and all(t.artist == "Tleskač" for t in w.tracks))
                self.assertEqual(w.tracks[0].id, uploads[0].id)
                # podkres po přání: další jejich nahrávky, pak „Tleskač a podobné"
                # z nahrávek jako seedů (rádio z videa jde i u nahrávek posluchačů)
                self.assertEqual(rig.pools.artist, "Tleskač")
                rig.catalog.calls.clear()
                await rig.pools.soften_artist("expired")
                self.assertEqual(rig.pools.mood, "Tleskač a podobné")
                seeds = [vid for kind, vid in rig.catalog.calls if kind == "radio"]
                self.assertTrue(seeds and all(s.startswith("tleskac") for s in seeds), seeds)

        try:
            run(go())
        finally:
            tw.SCRIPT.pop(WISH, None)


if __name__ == "__main__":
    unittest.main()
