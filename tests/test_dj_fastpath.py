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
}
SONGS = {
    "wonderwall": [T("oa", "Oasis", "Wonderwall"), T("w0", "Wonderwall", "Witchcraft")],
    "pelisky": [T("ph", "Jan Hammer", "Pelíšky"), T("p0", "Pelíšky", "Pelíšky")],
}


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
