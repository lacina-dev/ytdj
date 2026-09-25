"""Režim interpreta: jen ten interpret a žádné opakování, dokud pool nedojde —
i přes více tahů ("pusť Kabát" znovu, automatický tah) a restart.

    python -m unittest tests.test_radio_artist -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ytdj-radio-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ytdj.music.catalog import Track  # noqa: E402
from ytdj.music.radio import RadioPools  # noqa: E402
from ytdj.state import PlayRecord  # noqa: E402


class Store:
    """Jako ytdj.state.Store: historie přehrání, nejnovější první."""

    def __init__(self) -> None:
        self.plays: list[str] = []  # nejstarší první

    def play(self, tracks: list[Track]) -> None:
        self.plays += [t.id for t in tracks]

    def recent_history(self, limit: int = 40) -> list[PlayRecord]:
        return [PlayRecord(v, "", "", "finished") for v in reversed(self.plays)][:limit]

    def recently_played(self, days: int) -> set[str]:
        return set(self.plays)

    def blacklisted(self) -> set[str]:
        return set()

    def record_seed(self, video_id: str, mood: str) -> None:
        pass


class Catalog:
    radio_calls = 0

    async def radio(self, video_id: str, limit: int = 50) -> list[Track]:
        Catalog.radio_calls += 1
        return [Track(f"r{n:010d}", f"R{n}", "Wanastowi Vjecy", None, 200) for n in range(20)]


CFG = types.SimpleNamespace(
    repeat_days=30, max_duration=600, max_duration_request=5400,
    min_duration=60, pool_low=10, radio_limit=50,
)
KABAT = [Track(f"k{n:010d}", f"Song {n}", "Kabát", None, 200) for n in range(12)]


def run(coro):
    return asyncio.run(coro)


class ArtistRotation(unittest.TestCase):
    def test_new_turn_continues_where_the_last_one_stopped(self) -> None:
        """Pi 25. 9.: tři tahy "Kabát" → třikrát Malá dáma, Burlaci, Bára."""
        store = Store()
        heard: list[str] = []
        for turn in range(3):  # tah posluchače, automatický tah, restart ytdj
            pools = RadioPools(Catalog(), store, CFG)  # restart = nová session
            run(pools.set_artist("Kabát", KABAT))
            got = run(pools.next_tracks(4))
            self.assertTrue(all(t.artist == "Kabát" for t in got))
            store.play(got)
            heard += [t.id for t in got]
        self.assertEqual(len(heard), 12)
        self.assertEqual(len(set(heard)), 12, heard)  # celý pool bez opakování
        # nejznámější jdou první, dokud nezazněly
        self.assertEqual(heard[:4], [t.id for t in KABAT[:4]])

    def test_after_whole_pool_the_least_recent_comes_first(self) -> None:
        store = Store()
        store.play(KABAT[5:])  # hrály dávno
        store.play(KABAT[:5])  # hrály nedávno
        pools = RadioPools(Catalog(), store, CFG)
        run(pools.set_artist("Kabát", KABAT))
        got = [t.id for t in run(pools.next_tracks(7))]
        self.assertEqual(got, [t.id for t in KABAT[5:12]])

    def test_restart_inside_session_also_rotates(self) -> None:
        store = Store()
        pools = RadioPools(Catalog(), store, CFG)
        run(pools.set_artist("Kabát", KABAT))
        first = run(pools.next_tracks(12))
        store.play(first)
        again = run(pools.next_tracks(3))  # pool dohrán → jede znovu
        self.assertEqual([t.id for t in again], [t.id for t in first[:3]])
        self.assertEqual(Catalog.radio_calls, 0)  # nikdy rádio z cizích kapel

    def test_given_back_tracks_are_not_lost(self) -> None:
        """Plnič zahodí dávku, protože mezitím přišel tah — skladby se vrátí."""
        store = Store()
        pools = RadioPools(Catalog(), store, CFG)
        run(pools.set_artist("Kabát", KABAT))
        dropped = run(pools.next_tracks(3))
        pools.give_back(dropped)
        run(pools.set_artist("Kabát", KABAT))
        got = run(pools.next_tracks(3))
        self.assertEqual([t.id for t in got], [t.id for t in dropped])


if __name__ == "__main__":
    unittest.main()
