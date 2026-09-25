"""DJ fast path: "pusť Kabát" without Codex — accept only certainty.

    python -m unittest tests.test_dj_fastpath -v
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_dj_apply import FakePlayer, T  # noqa: E402

from ytdj.agent.codex import CodexDJ  # noqa: E402
from ytdj.agent.fastpath import (  # noqa: E402
    declined,
    find_artists,
    find_song,
    parse_song,
    name_matches,
    parse,
    variants,
)
from ytdj.agent.intent import ListenerIntent, norm  # noqa: E402
from ytdj.config import DEFAULTS, Config  # noqa: E402
from ytdj.music.catalog import Artist  # noqa: E402
from ytdj.music.radio import RadioPools  # noqa: E402
from ytdj.state import Store  # noqa: E402

# what the (fuzzy) catalog knows: artist name → their tracks
CATALOG = {
    "Kabát": [T(f"k{i}", "Kabát") for i in range(10)],
    "David Stypka": [T(f"s{i}", "David Stypka, Bandjeez") for i in range(10)],
    "Ewa Farná": [T(f"e{i}", "Ewa Farná") for i in range(10)],
    "Midi Lidi": [T(f"m{i}", "Midi Lidi") for i in range(10)],
    "Tata Bojs": [T(f"t{i}", "Tata Bojs") for i in range(10)],
    "Mňága a Žďorp": [T(f"z{i}", "Mňága a Žďorp") for i in range(10)],
    "Queen": [T(f"q{i}", "Queen") for i in range(10)],
    # traps: a band called like a famous song, a soundtrack "artist"
    "Wonderwall": [T(f"w{i}", "Wonderwall") for i in range(3)],
    "Pelíšky": [T("p0", "Pelíšky")],
    "Kabaret Kalich": [T("kk", "Kabaret Kalich")],
    "Znouzectnost": [T(f"zn{i}", "Znouzectnost") for i in range(5)],
    "Wanastowi Vjecy": [T(f"wv{i}", "Wanastowi Vjecy") for i in range(5)],
    # a different band one letter away — "pusť Olympic" must never land here
    "Olympica": [T("oa0", "Olympica")],
}
SONGS = {
    "wonderwall": [T("oa", "Oasis", "Wonderwall"), T("w0", "Wonderwall", "Witchcraft")],
    "pelisky": [T("ph", "Jan Hammer", "Pelíšky"), T("p0", "Pelíšky", "Pelíšky")],
}


# (artist, title) as the catalog has them
SONGBOOK = [
    T("jz", "Olympic", "Jasná zpráva"),
    T("ol2", "Olympic", "Dávno"),
    T("ww", "Oasis", "Wonderwall (Remastered)"),
    T("br", "Queen", "Bohemian Rhapsody"),
    T("km", "Jaromir Nohavica", "Kometa"),
    T("md", "Kabát", "Malá dáma"),
    T("po", "Kabát", "Pohoda"),
]


class FuzzyCatalog:
    """Like YouTube Music: returns the closest artist even for a declined name."""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.calls: list[tuple] = []

    async def find_artist(self, name: str):
        self.calls.append(("find_artist", name))
        await asyncio.sleep(self.delay)
        q = norm(name)
        best = None
        for artist in CATALOG:
            full = norm(artist)
            for a in [full] + full.split():  # a surname alone finds the artist too
                common = 0
                while common < min(len(a), len(q)) and a[common] == q[common]:
                    common += 1
                if common >= 4 and (best is None or common > best[0]):
                    best = (common, artist)
        return Artist(best[1], "UC" + best[1]) if best else None

    async def search(self, query: str, limit: int = 8):
        self.calls.append(("search", query))
        await asyncio.sleep(self.delay)
        return SONGS.get(norm(query), [])[:limit]

    async def search_song(self, artist: str, title: str):
        """Like the real one: fuzzy on both, and when the title isn't found it
        still returns *some* track by that artist — the fast path must not bite."""
        self.calls.append(("search_song", artist, title))
        await asyncio.sleep(self.delay)
        a, t = norm(artist)[:5], norm(title)[:5]
        for s in SONGBOOK:
            if a in norm(s.artist) and norm(s.title).startswith(t):
                return s
        for s in SONGBOOK:
            if a in norm(s.artist):
                return s  # "his track at least"
        return None

    async def radio(self, video_id: str, limit: int = 50):
        return [T(f"r{i}", f"Similar {i}") for i in range(20)]

    async def artist_tracks(self, name: str, limit: int = 50):
        self.calls.append(("artist_tracks", name))
        await asyncio.sleep(self.delay)
        return list(CATALOG.get(name, []))[:limit]


def run(coro):
    return asyncio.run(coro)


class Pure(unittest.TestCase):
    def test_declined(self):
        self.assertTrue(declined("stypka", "stypku"))
        self.assertTrue(declined("farna", "farnou"))
        self.assertTrue(declined("kabat", "kabatu"))
        self.assertTrue(declined("ewa", "ewu"))
        self.assertTrue(declined("lucie", "lucii"))
        self.assertFalse(declined("kabat", "kabaret"))
        self.assertFalse(declined("queen", "queens"))

    def test_name_matches(self):
        self.assertTrue(name_matches(["davida", "stypku"], "David Stypka"))
        # surname alone is ambiguous (real catalog: "Nohavicu" → Petr, not Jaromír)
        self.assertFalse(name_matches(["stypku"], "David Stypka"))
        self.assertFalse(name_matches(["nohavicu"], "Petr Nohavica"))
        self.assertTrue(name_matches(["mnaga", "a", "zdorp"], "Mňága a Žďorp"))
        self.assertTrue(name_matches(["beatles"], "The Beatles"))
        self.assertFalse(name_matches(["kabaret"], "Kabát"))
        self.assertFalse(name_matches(["kabat"], "Kabaret Kalich"))
        self.assertFalse(name_matches(["david"], "David Stypka"))  # first name only: no

    def test_variants(self):
        self.assertIn("david stypka", variants(["davida", "stypku"]))
        self.assertIn("kabat", variants(["kabatu"]))
        self.assertIn("ewa farna", variants(["ewu", "farnou"]))

    def test_parse_rejects(self):
        for text in (
            "pusť něco klidného", "dej to nahlas", "hlasitěji", "hraj", "pusť rock",
            "hraj české hity z devadesátek", "zahraj jednu od Chinaski",
            "něco jako Kabát", "Kabát, ale ne Pohodu", "rock, třeba Kabát",
            "pusť něco klidného od Kabátu", "hlasitost 70", "pusť rádio",
            "dej něco jiného", "pusť Kabát prosím a pak něco veselého",
        ):
            self.assertIsNone(parse(text), text)

    def test_parse_keeps_real_names(self):
        for text in ("pusť Metallicu", "pusť Radiohead", "hraj davida Stypku",
                     "dej tam Kryštof", "pusť The Beatles"):
            self.assertIsNotNone(parse(text), text)


class SpacedAndMisspelled(unittest.TestCase):
    """Pi 23:02: "Hraj z nouze cnost" meant the band Znouzectnost."""

    def test_name_matches_fuzzy(self):
        self.assertTrue(name_matches(["znouzecnost"], "Znouzectnost"))
        self.assertTrue(name_matches(["z", "nouze", "cnost"], "Znouzectnost"))
        self.assertTrue(name_matches(["tata", "boys"], "Tata Bojs"))
        self.assertTrue(name_matches(["wanastowi", "vjeci"], "Wanastowi Vjecy"))
        # but not a different band that differs where Czech declines
        self.assertFalse(name_matches(["olympic"], "Olympica"))
        self.assertFalse(name_matches(["kabaret"], "Kabát"))
        self.assertFalse(name_matches(["nouze", "cnost"], "Znouzectnost"))  # 2 edits, 12 chars

    def test_found_in_catalog(self):
        cases = [
            ("Hraj z nouze cnost", ["Znouzectnost"]),
            ("hraj znouzecnost", ["Znouzectnost"]),
            ("pusť tata boys", ["Tata Bojs"]),
            ("pusť mnaga a zdorp", ["Mňága a Žďorp"]),
            ("pusť wanastowi vjeci", ["Wanastowi Vjecy"]),
        ]
        for text, want in cases:
            res = run(find_artists(FuzzyCatalog(), text))
            self.assertEqual((res.artists, res.reason), (want, ""), text)

    def test_one_letter_neighbour_is_not_accepted(self):
        cat = FuzzyCatalog()
        cat_find = cat.find_artist

        async def only_olympica(name):
            await cat_find(name)
            return Artist("Olympica", "UColympica")

        cat.find_artist = only_olympica
        res = run(find_artists(cat, "pusť Olympic"))
        self.assertEqual(res.artists, [])


class WithCatalog(unittest.TestCase):
    def artists(self, text, cat=None):
        return run(find_artists(cat or FuzzyCatalog(), text))

    def test_accepted(self):
        cases = [
            ("pusť Kabát", ["Kabát"]),
            ("dej tam Kabátu", ["Kabát"]),
            ("hraj davida Stypku", ["David Stypka"]),
            ("hraj Ewu Farnou", ["Ewa Farná"]),
            ("chci slyšet Queen", ["Queen"]),
            ("pusť Mňága a Žďorp", ["Mňága a Žďorp"]),
            ("pusť Midi Lidi a pak Tata Bojs", ["Midi Lidi", "Tata Bojs"]),
            ("hoď tam Kabát a Queen", ["Kabát", "Queen"]),
        ]
        for text, want in cases:
            res = self.artists(text)
            self.assertEqual((res.artists, res.reason), (want, ""), text)
            self.assertEqual(len(res.tracks), len(want))

    def test_fallbacks(self):
        cases = [
            ("pusť Wonderwall", "song_title"),  # an artist exists, but it's Oasis' song
            ("pusť Pelíšky", "song_title"),
            ("pusť něco klidného", "not_artist_phrase"),
            ("dej to nahlas", "not_artist_phrase"),
            ("pusť Kabaret", "no_strict_match"),  # catalog says Kabaret Kalich / Kabát
            ("pusť Nikdoneznámý", "no_strict_match"),
            ("pusť Kabát a Nikdoneznámý", "no_strict_match"),  # all or nothing
            ("pusť Stypku", "no_strict_match"),  # surname only → the model decides
        ]
        for text, reason in cases:
            res = self.artists(text)
            self.assertEqual(res.artists, [], text)
            self.assertTrue(res.reason.startswith(reason), (text, res.reason))

    def test_decision_time(self):
        # 0.3 s per catalog call (Pi-like) — the decision itself must stay small
        cat = FuzzyCatalog(delay=0.3)
        t0 = time.monotonic()
        res = self.artists("hraj davida Stypku", cat)
        took = time.monotonic() - t0
        self.assertEqual(res.artists, ["David Stypka"])
        calls = len(cat.calls)
        self.assertLess(took - 0.3 * calls, 0.5)  # own CPU time besides the network
        self.assertLess(took, 3.0)


class Songs(unittest.TestCase):
    def song(self, text, cat=None, **kw):
        return run(find_song(cat or FuzzyCatalog(), text, **kw))

    def test_parse(self):
        self.assertEqual(parse_song("pusť Jasnou zprávu od Olympicu").pairs,
                         [("Olympicu", "Jasnou zprávu")])
        self.assertEqual(parse_song("Oasis - Wonderwall").pairs[0], ("Oasis", "Wonderwall"))
        self.assertEqual(parse_song("zahraj Bohemian Rhapsody od Queen prosím").pairs,
                         [("Queen", "Bohemian Rhapsody")])
        for text in ("pusť něco klidného od Kabátu", "zahraj jednu od Chinaski",
                     "pusť něco od Kabátu", "pusť Kabát", "hraj písničky od Midi Lidi",
                     "pusť hit od Olympicu", "něco jako Wonderwall od Oasis"):
            self.assertIsNone(parse_song(text), text)

    def test_accepted(self):
        cases = [
            ("pusť Jasnou zprávu od Olympicu", "jz"),  # both declined
            ("zahraj Wonderwall od Oasis", "ww"),      # English, version tag in catalog
            ("Oasis - Wonderwall", "ww"),
            ("pusť Queen – Bohemian Rhapsody", "br"),
            ("Bohemian Rhapsody - Queen", "br"),       # reversed order
            ("pusť Kometu od Nohavici", "km"),         # surname is enough with a title
            ("zahraj Malou dámu od Kabátu", "md"),
        ]
        for text, want in cases:
            res = self.song(text)
            self.assertEqual((res.track and res.track.id, res.reason), (want, ""), text)

    def test_traps(self):
        cases = [
            "pusť Olympic od Kabátu",               # a title that is an artist's name
            "pusť Holky z naší školky od Olympicu",  # catalog answers with another song
            "pusť Kabát - Olympic",
        ]
        for text in cases:
            res = self.song(text)
            self.assertIsNone(res.track, text)
            self.assertTrue(res.reason.startswith("no_strict_match"), (text, res.reason))

    def _vagner_only(self):
        # Pi 23:46: the catalog's only "Holky z naší školky" is by Vágner & co.
        cat = FuzzyCatalog()
        vagner = T("hv", "Karel Vágner, Stanislav Hložek, Petr Kotvald", "Holky z naší školky")

        async def search_song(artist, title, strict=False):
            return vagner

        cat.search_song = search_song
        return cat

    def test_named_artist_lacks_it_other_version_with_note(self):
        res = self.song("pusť Holky z naší školky od Olympicu", self._vagner_only())
        self.assertEqual(res.track.id, "hv")
        self.assertEqual(
            res.note,
            "Od Olympicu ji nemám — hraju verzi Karel Vágner, Stanislav Hložek, Petr Kotvald.",
        )

    def test_insisting_on_the_artist_means_not_found(self):
        for text in ("pusť Holky z naší školky jen od Olympicu",
                     "pusť Holky z naší školky od Olympicu, ne jinou verzi",
                     "pusť originál Holky z naší školky od Olympicu"):
            res = self.song(text, self._vagner_only())
            self.assertIsNone(res.track, text)

    def test_right_title_wrong_artist_plays_real_one_with_note(self):
        res = self.song("pusť Wonderwall od Kabátu")
        self.assertEqual(res.track.id, "ww")
        self.assertEqual(res.note, "Od Kabátu ji nemám — hraju verzi Oasis.")

    def test_surname_prefers_named_artist_over_other_version(self):
        # real catalog: strict song search vetoes "Nohavici" vs "Jaromir Nohavica"
        # and the most popular "Kometa" is by JONY — the named one must win
        cat = FuzzyCatalog()
        jony = T("jo", "JONY", "Kometa")
        jarek = T("km", "Jaromir Nohavica", "Kometa")

        async def search_song(artist, title, strict=False):
            return None if artist else jony

        async def search(query, limit=8):
            return [jony, jarek]

        cat.search_song, cat.search = search_song, search
        res = self.song("pusť Kometu od Nohavici", cat)
        self.assertEqual((res.track.id, res.note), ("km", ""))

    def test_weak_title_match_is_not_another_version(self):
        async def search_song(artist, title, strict=False):
            return T("x", "Someone", "Holky z naší školky a jiné písně (live)")

        cat = FuzzyCatalog()
        cat.search_song = search_song
        res = self.song("pusť Holky od Olympicu", cat)
        self.assertIsNone(res.track)

    def test_lookups_run_in_parallel(self):
        # Pi: ~1.5–2 s per catalog call; the declined forms must not add up
        res = self.song("pusť Jasnou zprávu od Olympicu", FuzzyCatalog(delay=1.0), budget=2.5)
        self.assertEqual((res.track and res.track.id, res.reason), ("jz", ""))

    def test_time_budget(self):
        t0 = time.monotonic()
        res = self.song("pusť Jasnou zprávu od Olympicu", FuzzyCatalog(delay=1.0), budget=0.3)
        self.assertEqual(res.reason, "timeout")
        self.assertLess(time.monotonic() - t0, 0.8)


def make(current=None, player=None):
    cfg = Config(**DEFAULTS)
    store = Store(Path(tempfile.mkdtemp(dir=_TMP)) / "state.db")
    catalog = FuzzyCatalog()
    pools = RadioPools(catalog, store, cfg)
    player = player or FakePlayer(current)
    dj = CodexDJ(cfg, catalog, pools, player, store)
    dj._wish_file = Path(tempfile.mkdtemp(dir=_TMP)) / "intent.json"
    dj.wish = ListenerIntent()
    return dj, player


class FastTurn(unittest.TestCase):
    def test_plays_artist_and_records_wish(self):
        dj, player = make(current=T("c", "Someone"))
        reply = run(dj.fast_turn("pusť Kabát"))
        self.assertEqual(reply, "Hraju Kabát — jen to, dokud neřekneš jinak.")
        self.assertEqual(player.current.artist, "Kabát")
        self.assertTrue(all(t.artist == "Kabát" for t in player.queue))
        self.assertEqual(dj.focus, "Kabát")
        self.assertEqual(dj.wish.focus_artists, ["Kabát"])
        self.assertIn("pusť Kabát", dj.wish.describe())

    def test_uncertain_leaves_everything_alone(self):
        dj, player = make(current=T("c", "Someone"))
        self.assertIsNone(run(dj.fast_turn("pusť Wonderwall")))
        self.assertEqual(player.log, [])
        self.assertEqual(dj.focus, "")


class FastSong(unittest.TestCase):
    def test_other_version_reply_is_honest(self):
        dj, player = make(current=T("c", "Someone"))
        reply = run(dj.fast_turn("pusť Wonderwall od Kabátu"))
        self.assertEqual(reply, "Od Kabátu ji nemám — hraju verzi Oasis. Pak podobné.")
        self.assertEqual(player.current.id, "ww")

    def test_plays_song_now_then_similar(self):
        dj, player = make(current=T("c", "Someone"))
        reply = run(dj.fast_turn("pusť Jasnou zprávu od Olympicu"))
        self.assertEqual(reply, "Hraju Olympic — Jasná zpráva, pak podobné.")
        self.assertEqual(player.current.id, "jz")  # now, not after the current one
        self.assertEqual(dj.focus, "")  # a song, not artist mode
        self.assertEqual([p.seed.id for p in dj.pools.pools], ["jz"])  # radio from it
        self.assertTrue(player.queue and player.queue[0].artist.startswith("Similar"))
        self.assertIn("Jasnou zprávu", dj.wish.describe())

    def test_mood_with_od_goes_to_model(self):
        dj, player = make(current=T("c", "Someone"))
        self.assertIsNone(run(dj.fast_turn("pusť něco klidného od Kabátu")))
        self.assertEqual(player.log, [])


class ReadyPlayer(FakePlayer):
    """A player that can say when a track is resolved (proposed API)."""

    def __init__(self, current, ready_after: float):
        super().__init__(current)
        self.ready_after = ready_after
        self.waited: list[str] = []

    async def wait_ready(self, video_id: str, timeout: float) -> bool:
        self.waited.append(video_id)
        await asyncio.sleep(min(self.ready_after, timeout))
        return self.ready_after <= timeout


class Switch(unittest.TestCase):
    def test_old_track_plays_until_new_is_ready(self):
        async def scenario():
            player = ReadyPlayer(T("c", "Someone"), ready_after=0.2)
            dj, _ = make(player=player)
            await dj.fast_turn("pusť Kabát")
            still_old = player.current.id  # right after the turn
            await asyncio.sleep(0.3)
            return still_old, player
        still_old, player = run(scenario())
        self.assertEqual(still_old, "c")  # no silence while resolving
        self.assertEqual(player.current.artist, "Kabát")  # then switched
        self.assertEqual(player.waited, ["k0"])

    def test_no_double_skip_when_track_ended_meanwhile(self):
        async def scenario():
            player = ReadyPlayer(T("c", "Someone"), ready_after=0.2)
            dj, _ = make(player=player)
            await dj.fast_turn("pusť Kabát")
            player.current = player.queue.pop(0)  # old one finished by itself
            await asyncio.sleep(0.3)
            return player
        player = run(scenario())
        self.assertEqual(player.current.id, "k0")
        self.assertNotIn(("skip", False), player.log)


if __name__ == "__main__":
    unittest.main()
