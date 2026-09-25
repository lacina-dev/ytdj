"""Hledání skladeb: skórování (ytdj.music.match) a celý Catalog.search_song
nad nahranými odpověďmi YouTube Music — bez sítě.

    python -m unittest tests.test_music_match -v

Nahrávky v tests/fixtures/ytm/*.json jsou skutečné odpovědi ytmusicapi
(anglický katalog, location=CZ, anonymně) z 25. 9. 2026, bez náhledů
a feedbackTokenů. Případy jsou z logů jukeboxu, kde se zahrálo něco jiného,
než si model řekl (Jiří Suchý, Prince, Monkey Business, Eva Cassidy,
Jana Kirschner), plus běžné české a zahraniční žádosti.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tempfile  # noqa: E402

from ytdj import telemetry  # noqa: E402
from ytdj.music import catalog as catalog_mod  # noqa: E402
from ytdj.music import match  # noqa: E402

# Události z testů nesmí skončit v ~/.local/share/ytdj/events.jsonl.
_EVENTS_DIR = tempfile.TemporaryDirectory()
EVENTS = Path(_EVENTS_DIR.name) / "events.jsonl"
telemetry._path = EVENTS


def events(kind: str) -> list[dict]:
    if not EVENTS.exists():
        return []
    rows = [json.loads(line) for line in EVENTS.read_text(encoding="utf-8").splitlines()]
    return [r for r in rows if r["kind"] == kind]


def clear_events() -> None:
    EVENTS.unlink(missing_ok=True)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ytm"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def recorded(name: str, method: str = "search", filter: str | None = "songs") -> list:
    for call in load(name)["calls"]:
        if call["method"] == method and call["kwargs"].get("filter") == filter:
            return call["result"]
    raise KeyError(f"{name}: {method}/{filter} nenahráno")


def songs(name: str) -> list[match.Candidate]:
    raw = recorded(name)
    return [c for c in (match.from_song(i, n) for n, i in enumerate(raw)) if c]


# ---- čisté funkce ----


class NormTest(unittest.TestCase):
    def test_diacritics_and_punctuation(self):
        self.assertEqual(match.norm("Děda Mládek"), match.norm("Deda Mladek"))
        self.assertEqual(match.norm("Jiří Suchý"), "jiri suchy")
        self.assertEqual(match.norm("Łódź"), "lodz")

    def test_spacing_does_not_matter(self):
        self.assertEqual(match.similar("TribalNeed", "Tribal Need"), 1.0)
        # tak to YouTube Music opravdu má: Tomas Klus — Panubohudooken
        self.assertEqual(match.similar("Panubohudooken", "Pánu bohu do oken"), 1.0)

    def test_containment_only_on_whole_words(self):
        # dřív "a in b" → 0.9 i uprostřed slova
        self.assertLess(match.similar("Jez", "Jezebel"), match.TITLE_FLOOR)
        self.assertLess(match.similar("Love", "Lovesong"), match.TITLE_FLOOR)
        self.assertGreaterEqual(
            match.similar("Nuvole Bianche", "Einaudi: Nuvole Bianche"), match.TITLE_FLOOR
        )


class ArtistTest(unittest.TestCase):
    def test_different_person_with_same_surname_is_vetoed(self):
        # na "Deda Mladek — Jožin z bažin" dřív vyhrál Ivan Mládek (0.73)
        self.assertLess(
            match.artist_similar("Ivan Mládek", "Deda Mladek"), match.ARTIST_FLOOR
        )

    def test_short_first_name_and_surname_only(self):
        self.assertGreaterEqual(match.artist_similar("Miroslav Zbirka", "Miro Žbirka"), 0.9)
        self.assertGreaterEqual(match.artist_similar("Jaromir Nohavica", "Nohavica"), 0.75)
        self.assertEqual(match.artist_similar("The Beatles", "Beatles"), 1.0)

    def test_each_credited_artist_counts(self):
        score, primary = match.artist_score(("Prince & The Revolution",), "Prince")
        self.assertEqual(score, 1.0)
        self.assertTrue(primary)
        score, primary = match.artist_score(
            ("Vokální kvintet Divadla Semafor", "Jiří Suchý"), "Jiří Suchý"
        )
        self.assertEqual(score, 1.0)
        self.assertFalse(primary)
        score, _ = match.artist_score(("David Stypka", "Bandjeez"), "David Stypka, Bandjeez")
        self.assertEqual(score, 1.0)

    def test_band_with_ampersand_in_name(self):
        score, _ = match.artist_score(("Bob Marley & The Wailers",), "Bob Marley & The Wailers")
        self.assertEqual(score, 1.0)
        score, _ = match.artist_score(("Mňága a Žďorp",), "Mnaga a Zdorp")
        self.assertEqual(score, 1.0)


class TitleTest(unittest.TestCase):
    def test_split_title(self):
        self.assertEqual(
            match.split_title("Kiss (Live In Utrecht) [2020 Remaster]"),
            ("Kiss", "Live In Utrecht 2020 Remaster"),
        )
        self.assertEqual(match.split_title("Wonderwall - Remastered")[0], "Wonderwall")
        self.assertEqual(match.split_title("Teardrop feat. Elizabeth Fraser")[0], "Teardrop")
        # koncové "live" v zadání je značka, ne část názvu
        self.assertEqual(match.split_title("Bohemian Rhapsody live"), ("Bohemian Rhapsody", "live"))
        # "Live Forever" je název, ne živák
        self.assertEqual(match.split_title("Live Forever"), ("Live Forever", ""))

    def test_version_tags(self):
        tags = lambda t: match.version_tags(match.split_title(t)[1])  # noqa: E731
        self.assertEqual(tags("Kiss (Live In Utrecht) [2020 Remaster]"), {"live"})
        self.assertEqual(tags("Fields Of Gold (Live At Blues Alley)"), {"live"})
        self.assertEqual(tags("Černí andělé (sympho rmx 2022)"), {"remix"})
        self.assertEqual(tags("Praminek vlasu (Instrumental)"), {"instrumental"})
        self.assertEqual(tags("One More Time (12 Mix)"), {"remix", "extended"})
        self.assertEqual(tags("Song (Sped Up)"), {"speed"})
        # neutrální: rádiová verze, remaster, edit, album, feat.
        for neutral in (
            "Pokoj v dusi (Radio Version)",
            "Wonderwall (Remastered)",
            "Could You Be Loved (Edit)",
            "Fields Of Gold (Songbird 20)",
            "Teardrop (feat. Elizabeth Fraser)",
            "Strawberry Fields Forever (2009 Stereo Mix)",
            "Levels (Original Mix)",
        ):
            self.assertEqual(tags(neutral), set(), neutral)

    def test_parse_views(self):
        self.assertEqual(match.parse_views("2.5K"), 2500)
        self.assertEqual(match.parse_views("153M"), 153_000_000)
        self.assertEqual(match.parse_views("1.6B"), 1_600_000_000)
        self.assertEqual(match.parse_views("738"), 738)
        self.assertEqual(match.parse_views("1,234"), 1234)
        self.assertIsNone(match.parse_views(None))

    def test_asked_version_wins_and_unasked_loses(self):
        self.assertLess(match.tag_adjustment({"live"}, set()), 0)
        self.assertGreater(match.tag_adjustment({"live"}, {"live"}), 0)
        self.assertLess(match.tag_adjustment(set(), {"remix"}), 0)


class VideoTest(unittest.TestCase):
    def test_artist_and_title_from_video_title(self):
        item = {
            "videoId": "8e39rbKHC5o",
            "title": "Monkey Business - Piece Of My Life (Official Video)",
            "artists": [{"name": "Marie J.", "id": "x"}],
            "views": "2.7M",
            "duration_seconds": 354,
        }
        cands = match.from_video(item)
        self.assertEqual(cands[0].artists, ("Monkey Business",))
        self.assertEqual(cands[0].title, "Piece Of My Life")
        self.assertEqual(cands[0].views, 2_700_000)
        # kanál je druhý kandidát — a ten veto na interpreta neprojde
        self.assertEqual(cands[1].artists, ("Marie J.",))
        self.assertIsNone(match.score(cands[1], "Monkey Business", "Piece of My Life"))

    def test_czech_quotes(self):
        item = {"videoId": "abcdefghijk", "title": "Lucie „Černí andělé“", "artists": []}
        (c,) = match.from_video(item)
        self.assertEqual((c.artists, c.title), (("Lucie",), "Černí andělé"))


# ---- skutečné výsledky hledání ----


class RankingOnRecordedResultsTest(unittest.TestCase):
    def best(self, fixture, artist, title):
        c = match.best(songs(fixture), artist, title)
        self.assertIsNotNone(c, fixture)
        return c

    def test_praminek_vlasu_canonical_not_bargain_bin_compilation(self):
        # Log jukeboxu 19:04: seed "Jiří Suchý — Pramínek vlasů" se přeložil na
        # --NpoxKArd0 "Jiri Suchy — Praminek vlasu" z alba "Blues pro Tebe"
        # (5:45, 2,5 tis. přehrání) — první ve výsledcích, se shodou 1.0.
        raw = recorded("jiri_suchy_praminek_vlasu")
        self.assertEqual(raw[0]["videoId"], "--NpoxKArd0")
        c = self.best("jiri_suchy_praminek_vlasu", "Jiří Suchý", "Pramínek vlasů")
        self.assertNotEqual(c.id, "--NpoxKArd0")
        self.assertNotEqual(c.album, "Blues pro Tebe")
        self.assertEqual(c.title, "Pramínek vlasů")
        self.assertIn("Jiří Suchý", c.artists)

    def test_prince_kiss_studio_not_live(self):
        # Log 19:49: "Prince — Kiss" → mA6utM0R9Ac "Kiss (Live In Utrecht)".
        # Studiová verze je ve výsledcích první, jen jako "Prince & The
        # Revolution" — a ten "&" ji stál shodu interpreta.
        c = self.best("prince_kiss", "Prince", "Kiss")
        self.assertEqual(c.id, "rH9CZuuKpSg")

    def test_eva_cassidy_studio_not_live(self):
        # Log 18:12: "Fields of Gold" → z8E2PGjGWTg "(Live At Blues Alley)"
        c = self.best("eva_cassidy_fields_of_gold", "Eva Cassidy", "Fields of Gold")
        self.assertNotIn("live", c.title.lower())
        self.assertEqual(c.artists, ("Eva Cassidy",))  # ne Sting, ne duet

    def test_popular_radio_version_over_obscure_long_take(self):
        # Log 19:16: "Pokoj v duši" → sfrNON5ChoU (8:46, 4,1 tis.) místo
        # "Pokoj v dusi (Radio Version)" (11 mil.)
        c = self.best("jana_kirschner_pokoj_v_dusi", "Jana Kirschner", "Pokoj v duši")
        self.assertEqual(c.id, "xQKF06B3dZ4")

    def test_official_without_diacritics_beats_live_and_remix_with_them(self):
        c = self.best("lucie_cerni_andele", "Lucie", "Černí andělé")
        self.assertEqual(c.id, "3f5cfBYjI44")  # "Cerni andele", 9,8 mil.

    def test_named_band_not_the_more_famous_namesake(self):
        c = self.best("deda_mladek_jozin_z_bazin", "Deda Mladek", "Jožin z bažin")
        self.assertIn("Deda Mladek Illegal Band", c.artists)

    def test_asked_remix_gets_a_remix(self):
        c = self.best("daft_punk_one_more_time_remix", "Daft Punk", "One More Time (remix)")
        self.assertIn("remix", match.version_tags(match.split_title(c.title)[1]))
        self.assertEqual(c.artists, ("Daft Punk",))

    def test_asked_live_gets_live(self):
        c = self.best("queen_bohemian_rhapsody_live", "Queen", "Bohemian Rhapsody live")
        self.assertIn("live", c.title.lower())
        self.assertEqual(match.split_title(c.title)[0], "Bohemian Rhapsody")

    def test_wrong_song_by_right_artist_is_vetoed(self):
        # Log 18:25/19:49: "Monkey Business — Piece of My Life" → jejich
        # "Blue Light Baggie Bingo": interpret 1.0 přebil nesouvisející název.
        self.assertIsNone(
            match.best(songs("monkey_business_piece_of_my_life"), "Monkey Business", "Piece of My Life")
        )

    def test_right_title_by_wrong_artist_is_vetoed(self):
        # README: "TribalNeed — Tribal Need" nesmí skončit u Ballistic Noise
        cands = songs("tribalneed_tribal_need")
        self.assertTrue(any("Ballistic Noise" in c.artists for c in cands))
        self.assertIsNone(match.best(cands, "TribalNeed", "Tribal Need"))

    def test_common_requests(self):
        cases = [
            ("olympic_jasna_zprava", "Olympic", "Jasná zpráva", "D8dY9Vp1HX8"),
            ("tomas_klus_panu_bohu_do_oken", "Tomáš Klus", "Pánu bohu do oken", "Be0ElJ_pAVw"),
            ("miro_zbirka_atlantida", "Miro Žbirka", "Atlantída", "ENfil6UC8yk"),
            ("karel_plihal_akordy", "Karel Plíhal", "Akordy", "-WdBkGONisg"),
        ]
        for fixture, artist, title, vid in cases:
            with self.subTest(fixture):
                self.assertEqual(self.best(fixture, artist, title).id, vid)
        c = self.best("nirvana_come_as_you_are", "Nirvana", "Come As You Are")
        self.assertEqual((c.artists, c.title), (("Nirvana",), "Come As You Are"))


# ---- celý Catalog.search_song nad nahrávkou ----


class ReplayYT:
    """Místo YTMusic: vrací nahrané odpovědi, na nenahrané volání spadne."""

    def __init__(self, calls: list[dict]) -> None:
        self.calls = calls
        self.log: list[tuple] = []

    def _find(self, method: str, key, filter=None):
        self.log.append((method, key, filter))
        for c in self.calls:
            args = c["args"][0] if c["args"] else c["kwargs"].get("videoId")
            if c["method"] == method and args == key and c["kwargs"].get("filter") == filter:
                return c["result"]
        raise AssertionError(f"nenahrané volání {method}({key!r}, filter={filter!r})")

    def search(self, query, filter=None, limit=20, **_):
        return self._find("search", query, filter)

    def get_artist(self, browse_id):
        return self._find("get_artist", browse_id)

    def get_watch_playlist(self, videoId=None, **_):
        return self._find("get_watch_playlist", videoId)

    def get_playlist(self, playlistId, limit=100, **_):
        return self._find("get_playlist", playlistId)


def replay_catalog(calls: list[dict]):
    replay = ReplayYT(calls)
    cat = catalog_mod.Catalog.__new__(catalog_mod.Catalog)
    cat.yt = replay
    cat.authenticated = False
    cat._auth = None
    cat._cfg = types.SimpleNamespace(language="cs", location="CZ")
    return cat, replay


def resolve(fixture: str):
    data = load(fixture)
    cat, replay = replay_catalog(data["calls"])
    track = asyncio.run(cat.search_song(data["artist"], data["title"]))
    return track, replay


def artist_tracks(fixture: str):
    data = load(fixture)
    cat, replay = replay_catalog(data["calls"])
    return asyncio.run(cat.artist_tracks(data["artist"], limit=data["limit"])), replay


class SearchSongReplayTest(unittest.TestCase):
    def test_every_fixture_resolves_as_recorded(self):
        for path in sorted(FIXTURES.glob("*.json")):
            with self.subTest(path.stem):
                if path.stem.startswith("artist_"):
                    tracks, _ = artist_tracks(path.stem)
                    self.assertEqual([t.id for t in tracks], load(path.stem)["resolved"])
                    continue
                track, _ = resolve(path.stem)
                self.assertEqual(track.id if track else None, load(path.stem)["resolved"])

    def test_song_only_on_youtube_as_someone_elses_video(self):
        track, replay = resolve("monkey_business_piece_of_my_life")
        self.assertEqual(track.artist, "Monkey Business")
        self.assertEqual(match.norm(track.title), "piece of my life")
        # songs → profil interpreta → videa, v tomhle pořadí
        self.assertEqual(
            [(m, f) for m, _, f in replay.log],
            [
                ("search", "songs"),
                ("search", "artists"),
                ("get_artist", None),
                ("get_playlist", None),
                ("search", "videos"),
            ],
        )

    def test_artist_only_request_plays_their_top_song(self):
        for fixture, artist in (("tata_bojs", "Tata Bojs"), ("radiohead", "Radiohead")):
            with self.subTest(fixture):
                track, _ = resolve(fixture)
                self.assertEqual(track.artist, artist)

    def test_unknown_title_falls_back_to_the_artist_never_to_another_band(self):
        track, _ = resolve("tribalneed_tribal_need")
        self.assertEqual(track.artist, "TribalNeed")

    def test_unknown_title_by_known_artist_falls_back_to_their_best_known(self):
        # "Tata Bojs — E-mail" není ani mezi 100 skladbami profilu; dřív
        # vyhrála "Vesmírná" (413 přehrání) jen díky shodě interpreta
        track, _ = resolve("tata_bojs_e_mail")
        self.assertEqual((track.artist, track.title), ("Tata Bojs", "Novej člověk"))

    def test_catalog_client_is_english_even_with_czech_config(self):
        # ytmusicapi 1.12 s language="cs" vrací na filtrované hledání prázdno
        self.assertEqual(catalog_mod.SEARCH_LANGUAGE, "en")


class ArtistTracksTest(unittest.TestCase):
    """Catalog.artist_tracks — na "hraj Midi Lidi" celý interpret, ne jedna písnička."""

    def test_midi_lidi(self):
        tracks, replay = artist_tracks("artist_midi_lidi")
        self.assertEqual(len(tracks), 50)
        self.assertEqual(tracks[0].title, "Láska není švédský stůl")
        self.assertTrue(all(t.artist == "Midi Lidi" for t in tracks))
        titles = [match.norm(match.split_title(t.title)[0]) for t in tracks]
        self.assertEqual(len(titles), len(set(titles)))
        self.assertTrue(all(t.duration for t in tracks))  # kvůli max_duration
        self.assertEqual(
            [m for m, _, _ in replay.log], ["search", "get_artist", "get_playlist"]
        )

    def test_well_known_first_live_versions_last(self):
        tracks, _ = artist_tracks("artist_tata_bojs")
        self.assertEqual(tracks[0].title, "Novej člověk")
        # V playlistu profilu je "220 Travoltů (OnLive from DOX+)" druhý;
        # při padesáti skladbách se na živáky vůbec nedostane.
        raw = next(c for c in load("artist_tata_bojs")["calls"] if c["method"] == "get_playlist")
        self.assertIn("OnLive", raw["result"]["tracks"][1]["title"])
        self.assertFalse([t for t in tracks if match.version_tags(match.split_title(t.title)[1])])
        # "Opakování" i "Opakování (radio edit)" jsou jedna píseň
        self.assertEqual(
            sum(match.split_title(t.title)[0] == "Opakování" for t in tracks), 1
        )

    def test_only_the_artist_even_in_collaborations(self):
        for fixture, name in (("artist_olympic", "Olympic"), ("artist_radiohead", "Radiohead")):
            with self.subTest(fixture):
                tracks, _ = artist_tracks(fixture)
                self.assertGreaterEqual(len(tracks), 40)
                for t in tracks:
                    # "Olympic/Karel Mareš, Yvonne Přenosilová" je taky Olympic
                    self.assertIn(name, match.artist_parts(t.artist.split(", ")))

    def test_artist_without_music_profile_uses_channel_videos(self):
        tracks, _ = artist_tracks("artist_tribalneed")
        self.assertTrue(tracks)
        self.assertTrue(all(t.artist == "TribalNeed" for t in tracks))

    def test_artist_songs_dedupe_filter_and_order(self):
        me = {"name": "Midi Lidi", "id": "UC_me"}
        items = [
            {"videoId": "a" * 11, "title": "Lux (Live)", "artists": [me]},
            {"videoId": "b" * 11, "title": "Rád vařím", "artists": [me]},
            {"videoId": "c" * 11, "title": "Rád vařím (Radio Edit)", "artists": [me]},
            {"videoId": "d" * 11, "title": "Cizí", "artists": [{"name": "Kazety", "id": "UC_x"}]},
            {"videoId": "e" * 11, "title": "Host", "artists": [{"name": "X", "id": "UC_x"}, me]},
            {"videoId": "f" * 11, "title": "Pryč", "artists": [me], "isAvailable": False},
        ]
        out = match.artist_songs(items, "UC_me", "Midi Lidi")
        self.assertEqual([c.title for c in out], ["Rád vařím", "Host", "Lux (Live)"])


class _Store:
    def __init__(self, recent=()):
        self.recent = set(recent)
        self.seeds = []

    def blacklisted(self):
        return set()

    def recently_played(self, days):
        return self.recent

    def record_seed(self, video_id, mood):
        self.seeds.append(video_id)


class _RadioCatalog:
    """Rádio vrací cizí interprety — v režimu interpreta se nesmí použít."""

    def __init__(self):
        self.radio_calls = 0

    async def radio(self, video_id, limit=50):
        self.radio_calls += 1
        return [catalog_mod.Track(f"r{n:010d}", f"Radio {n}", "Someone Else", None, 200) for n in range(20)]


class ArtistModeTest(unittest.TestCase):
    def setUp(self):
        from ytdj.music.radio import RadioPools

        self.cfg = types.SimpleNamespace(
            repeat_days=30, max_duration=600, max_duration_request=5400,
            min_duration=60, pool_low=10, radio_limit=50,
        )
        self.tracks = [
            catalog_mod.Track(f"m{n:010d}", f"Song {n}", "Midi Lidi", None, 240) for n in range(12)
        ]
        # jedna "hrála včera" — vyžádaný interpret má přednost před neopakováním
        self.store = _Store(recent={self.tracks[1].id})
        self.catalog = _RadioCatalog()
        self.pools = RadioPools(self.catalog, self.store, self.cfg)

    def test_plays_only_the_artist_and_more_than_two_in_a_row(self):
        summary = asyncio.run(self.pools.set_artist("Midi Lidi", self.tracks))
        self.assertEqual(summary["pool_size"], 12)
        out = asyncio.run(self.pools.next_tracks(5))
        self.assertEqual([t.id for t in out], [t.id for t in self.tracks[:5]])
        self.assertEqual(self.catalog.radio_calls, 0)
        self.assertIn("Midi Lidi", self.pools.describe())

    def test_starts_over_when_everything_played(self):
        asyncio.run(self.pools.set_artist("Midi Lidi", self.tracks))
        played = []
        for _ in range(6):
            played += asyncio.run(self.pools.next_tracks(5))
        self.assertEqual(len(played), 30)
        self.assertTrue(all(t.artist == "Midi Lidi" for t in played))
        self.assertEqual(self.catalog.radio_calls, 0)
        self.assertFalse(self.pools.exhausted())

    def test_unknown_artist_keeps_current_pools(self):
        asyncio.run(self.pools.set_artist("Midi Lidi", self.tracks))
        summary = asyncio.run(self.pools.set_artist("Nikdo", []))
        self.assertEqual(summary["pool_size"], 0)
        self.assertEqual(self.pools.artist, "Midi Lidi")

    def test_set_seeds_ends_artist_mode(self):
        asyncio.run(self.pools.set_artist("Midi Lidi", self.tracks))
        asyncio.run(self.pools.set_seeds([self.tracks[0]], mood="cokoli"))
        self.assertEqual(self.pools.artist, "")
        out = asyncio.run(self.pools.next_tracks(5))
        # zpátky u rádia a u stropu dvou skladeb na interpreta
        self.assertLessEqual(sum(t.artist == "Someone Else" for t in out), 2)


class TelemetryTest(unittest.TestCase):
    """Co jde do events.jsonl — jedna událost na operaci, ne na kandidáta."""

    def setUp(self):
        clear_events()

    def test_search_records_choice_source_and_runner_up(self):
        resolve("prince_kiss")
        (ev,) = events("catalog.search")
        self.assertEqual((ev["artist"], ev["title"]), ("Prince", "Kiss"))
        self.assertEqual(ev["video_id"], "rH9CZuuKpSg")
        self.assertEqual(ev["chosen"], "Prince & The Revolution — Kiss")
        self.assertEqual(ev["source"], "songs")
        self.assertEqual(ev["lang"], "en")
        self.assertGreater(ev["score"], ev["runner_up"]["score"])
        self.assertEqual(ev["steps"], [{"source": "songs", "query": "Prince Kiss", "n": 20}])
        self.assertIsInstance(ev["took_ms"], int)
        self.assertEqual(events("catalog.match_fail"), [])

    def test_fallback_records_match_fail_with_reason(self):
        resolve("tata_bojs_e_mail")
        (fail,) = events("catalog.match_fail")
        self.assertEqual((fail["artist"], fail["title"]), ("Tata Bojs", "E-mail"))
        self.assertTrue(fail["rejected"])
        self.assertTrue(all(r["why"].startswith("title") for r in fail["rejected"]))
        (ev,) = events("catalog.search")
        self.assertEqual(ev["source"], "artist_top_fallback")
        self.assertEqual([s["source"] for s in ev["steps"]], ["songs", "profile", "videos"])
        self.assertEqual(len(events("catalog.artist_tracks")), 1)

    def test_artist_tracks_event(self):
        artist_tracks("artist_midi_lidi")
        (ev,) = events("catalog.artist_tracks")
        self.assertEqual(ev["artist"], "Midi Lidi")
        self.assertEqual(ev["source"], "profile")
        self.assertEqual(ev["n"], 50)
        self.assertEqual(ev["first"], "Láska není švédský stůl")
        self.assertIn("took_ms", ev)

    def test_radio_events(self):
        mode = ArtistModeTest()
        mode.setUp()
        asyncio.run(mode.pools.set_artist("Midi Lidi", mode.tracks))
        asyncio.run(mode.pools.next_tracks(5))
        (am,) = events("radio.artist_mode")
        self.assertEqual((am["artist"], am["n"]), ("Midi Lidi", 12))
        (pool,) = events("radio.pool")
        self.assertEqual((pool["wanted"], pool["got"], pool["artist_mode"]), (5, 5, "Midi Lidi"))

        clear_events()
        asyncio.run(mode.pools.set_seeds([mode.tracks[0]], mood="cokoli"))
        (fetch,) = events("radio.fetch")
        self.assertEqual((fetch["seed"], fetch["n"]), (mode.tracks[0].id, 20))
        (seeds,) = events("radio.seeds")
        self.assertEqual(seeds["pool_sizes"], [20])
        asyncio.run(mode.pools.next_tracks(5))
        (pool,) = events("radio.pool")
        # rádio vrací 20 skladeb jednoho interpreta — strop dvou je vidět
        self.assertGreater(pool["rejected"].get("artist_cap", 0), 0)


if __name__ == "__main__":
    unittest.main()
