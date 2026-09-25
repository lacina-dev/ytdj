"""Výpadek vs. vadná skladba: černá listina jen na obsah a na čas, při výpadku
se stojí (fronta i přání zůstanou) a po obnově spojení se pokračuje.

    python -m unittest tests.test_outage -v
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="ytdj-outage-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_player_queue import Harness, T, vid  # noqa: E402
from ytdj.music.catalog import Track, to_track  # noqa: E402
from ytdj.music.radio import RadioPools  # noqa: E402
from ytdj.player import mpv as mpvmod  # noqa: E402
from ytdj.player.outage import classify_error, outage_reason  # noqa: E402
from ytdj.state import Store  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class Classify(unittest.TestCase):
    def test_content_vs_transient(self) -> None:
        cases = {
            "ERROR: [youtube] abc: Private video. Sign in if you've been granted access": "content",
            "ERROR: [youtube] abc: Video unavailable. This video has been removed by the uploader": "removed",
            "ERROR: [youtube] abc: Sign in to confirm your age": "content",
            "The uploader has not made this video available in your country": "content",
            "Video unavailable. This content isn't available, try again later.": "transient",
            "Sign in to confirm you're not a bot": "transient",
            "Unable to download API page: <urlopen error [Errno -3] Temporary failure in name resolution>": "transient",
            "loading failed": "transient",
            "": "transient",
            "vypršel čas": "transient",
        }
        for text, want in cases.items():
            self.assertEqual(classify_error(text), want, text)

    def test_outage_reason(self) -> None:
        self.assertEqual(outage_reason("Temporary failure in name resolution"), "dns")
        self.assertEqual(outage_reason("Sign in to confirm you're not a bot"), "youtube_login")
        self.assertEqual(outage_reason("loading failed"), "playback")


class BlacklistTtl(unittest.TestCase):
    def test_ttl_and_permanent(self) -> None:
        st = Store(Path(tempfile.mkdtemp(dir=_TMP)) / "state.db")
        st.blacklist("aaaaaaaaaaa", "x")  # 7 dní
        st.blacklist("bbbbbbbbbbb", "x", days=None)  # smazané: natrvalo
        st.blacklist("ccccccccccc", "x", days=-1)  # už prošlé
        self.assertEqual(st.blacklisted(), {"aaaaaaaaaaa", "bbbbbbbbbbb"})
        with mock.patch("ytdj.state.time.time", return_value=time.time() + 8 * 86400):
            self.assertEqual(st.blacklisted(), {"bbbbbbbbbbb"})

    def test_legacy_rows_get_a_ttl(self) -> None:
        """Staré "nepřehratelné" (i z výpadků) nesmí zůstat navždy."""
        path = Path(tempfile.mkdtemp(dir=_TMP)) / "state.db"
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE blacklist (video_id TEXT PRIMARY KEY, reason TEXT, ts REAL NOT NULL)")
        db.execute("INSERT INTO blacklist VALUES ('ddddddddddd', 'nepřehratelné', ?)",
                   (time.time() - 10 * 86400,))
        db.execute("INSERT INTO blacklist VALUES ('eeeeeeeeeee', 'nepřehratelné', ?)", (time.time(),))
        db.commit()
        db.close()
        st = Store(path)
        self.assertEqual(st.blacklisted(), {"eeeeeeeeeee"})


class Outage(unittest.TestCase):
    def setUp(self) -> None:
        self._d = mock.patch.object(mpvmod, "OUTAGE_FIRST_DELAY", 0.05)
        self._d.start()

    def tearDown(self) -> None:
        self._d.stop()

    def _events(self, p):
        got: list[tuple[str, str | None, str]] = []

        async def handler(ev):
            got.append((ev.kind, ev.track.id if ev.track else None, ev.detail))

        p.on_event(handler)
        return got

    def test_network_outage_holds_queue_and_resumes(self) -> None:
        async def go():
            async with Harness(jitter=0) as h:
                p = h.player
                online = {"ok": False}
                p.probe = lambda: (online["ok"], "" if online["ok"] else "dns: down")
                got = self._events(p)
                await p.enqueue([T(i) for i in range(6)])
                await h.settle()
                # Wi-Fi spadla: nic se nedá otevřít
                h.fake.fail = {vid(i) for i in range(1, 6)}
                await p.skip()
                await h.settle(0.2)
                st = await p.status()
                self.assertIsNotNone(st.outage)
                self.assertFalse(st.playing)
                self.assertIsNone(h.fake.cur)  # stojí (stop keep-playlist)
                # fronta drží od první chybné skladby — nic se neztratilo
                self.assertEqual([t.id for t in st.queue][:5], [vid(i) for i in range(1, 6)])
                self.assertEqual(p.queue_depth, 5)
                kinds = [k for k, _, _ in got]
                self.assertNotIn("error", kinds)  # přání ani černá listina nic nedostanou
                self.assertIn("outage", kinds)
                self.assertIn(("player.outage"), [k for k, _ in h.events])
                # plnič během výpadku nic nespouští (append bez -play)
                await p.enqueue([T(9)])
                await h.settle()
                self.assertIsNone(h.fake.cur)
                # spojení je zpátky
                h.fake.fail = set()
                online["ok"] = True
                await asyncio.sleep(0.5)
                self.assertIsNone(p.outage)
                self.assertEqual(h.fake.current_vid(), vid(1))  # od první chybné
                self.assertIn("outage_end", [k for k, _, _ in got])
                phases = [f.get("phase") for k, f in h.events if k == "player.outage"]
                self.assertEqual(phases[0], "start")
                self.assertIn("probe", phases)
                self.assertEqual(phases[-1], "end")

        run(go())

    def test_single_failure_is_not_an_outage(self) -> None:
        async def go():
            async with Harness(jitter=0) as h:
                p = h.player
                got = self._events(p)
                await p.enqueue([T(i) for i in range(4)])
                await h.settle()
                h.fake.fail = {vid(1)}
                await p.skip()
                await h.settle(0.2)
                self.assertIsNone(p.outage)
                self.assertEqual(h.fake.current_vid(), vid(2))
                self.assertIn(("unavailable", vid(1)), [(k, v) for k, v, _ in got])

        run(go())

    def test_content_error_is_error_not_outage(self) -> None:
        async def go():
            async with Harness(jitter=0) as h:
                p = h.player
                got = self._events(p)
                await p.enqueue([T(i) for i in range(5)])
                await h.settle()
                for i in (1, 2):
                    p._on_resolver_event("resolver.get", {"video_id": vid(i), "ok": False,
                                                          "error": "Private video"})
                h.fake.fail = {vid(1), vid(2)}
                await p.skip()
                await h.settle(0.2)
                self.assertIsNone(p.outage)
                self.assertEqual(h.fake.current_vid(), vid(3))
                errors = [(v, d) for k, v, d in got if k == "error"]
                self.assertEqual([v for v, _ in errors], [vid(1), vid(2)])
                self.assertTrue(all(d.startswith("content|") for _, d in errors))

        run(go())

    def test_track_that_keeps_failing_online_is_given_up(self) -> None:
        """Spojení je, ale skladba padá pořád — po pár obnoveních se přeskočí."""
        async def go():
            async with Harness(jitter=0) as h:
                p = h.player
                p.probe = lambda: (True, "")
                got = self._events(p)
                await p.enqueue([T(i) for i in range(5)])
                await h.settle()
                h.fake.fail = {vid(1), vid(2)}
                await p.skip()
                await asyncio.sleep(1.5)
                self.assertIn(vid(3), h.fake.started)
                gave = [(v, d) for k, v, d in got if k == "error"]
                self.assertTrue(gave and gave[0][1].startswith("retry_failed|"))

        run(go())

    def test_next_during_outage_moves_resume_point(self) -> None:
        async def go():
            async with Harness(jitter=0) as h:
                p = h.player
                online = {"ok": False}
                p.probe = lambda: (online["ok"], "down")
                await p.enqueue([T(i) for i in range(6)])
                await h.settle()
                h.fake.fail = {vid(i) for i in range(1, 6)}
                await p.skip()
                await h.settle(0.2)
                self.assertIsNotNone(p.outage)
                await p.skip()  # posluchač nechce vid(1)
                self.assertEqual(p.upcoming_ids()[0], vid(2))
                h.fake.fail = set()
                online["ok"] = True
                await asyncio.sleep(0.5)
                self.assertEqual(h.fake.current_vid(), vid(2))

        run(go())


class Volume(unittest.TestCase):
    def test_ceiling_100(self) -> None:
        self.assertEqual(mpvmod._clamp_volume(130), 100)
        self.assertEqual(mpvmod._clamp_volume(-5), 0)


class _Store:
    def blacklisted(self):
        return set()

    def recently_played(self, days):
        return set()

    def record_seed(self, *a):
        pass


CFG = types.SimpleNamespace(repeat_days=30, max_duration=600, max_duration_request=5400,
                            min_duration=60, pool_low=10, radio_limit=50)


class RadioResilience(unittest.TestCase):
    def test_pool_survives_radio_failure(self) -> None:
        class Cat:
            fail = True
            calls = 0

            async def radio(self, video_id, limit=50):
                Cat.calls += 1
                if Cat.fail:
                    raise OSError("network down")
                return [Track(f"r{n:010d}", f"R{n}", f"A{n}", None, 200) for n in range(5)]

        pools = RadioPools(Cat(), _Store(), CFG)
        run(pools.set_seeds([Track("seed0000000", "S", "A", None, 200)], mood="klid"))
        self.assertEqual(len(pools.pools), 1)
        self.assertEqual(run(pools.next_tracks(3)), [])
        self.assertEqual(len(pools.pools), 1)  # dřív se smazal i s náladou
        self.assertTrue(pools.retrying())
        Cat.fail = False
        pools.pools[0].retry_at = 0.0  # uplynulo RADIO_RETRY
        got = run(pools.next_tracks(3))
        self.assertEqual(len(got), 3)
        self.assertEqual(pools.mood, "klid")

    def test_explicit_kept_out_of_background_only(self) -> None:
        clean = [Track(f"c{n:010d}", f"C{n}", f"A{n}", None, 200) for n in range(3)]
        dirty = [Track(f"x{n:010d}", f"X{n}", f"B{n}", None, 200, explicit=True) for n in range(3)]

        class Cat:
            async def radio(self, video_id, limit=50):
                return dirty + clean

        pools = RadioPools(Cat(), _Store(), CFG)
        run(pools.set_seeds([Track("seed0000000", "S", "A", None, 200)]))
        got = run(pools.next_tracks(6))
        self.assertTrue(got and not any(t.explicit for t in got))
        # vyžádaný interpret: jeho skladby ano, i explicitní
        run(pools.set_artist("B", dirty))
        self.assertEqual(len(run(pools.next_tracks(3))), 3)

    def test_to_track_reads_explicit(self) -> None:
        t = to_track({"videoId": "abcdefghijk", "title": "X", "artists": [{"name": "A"}],
                      "isExplicit": True})
        self.assertTrue(t.explicit)
        self.assertEqual(t, Track("abcdefghijk", "X", "A"))  # rovnost bez ohledu na odznak


if __name__ == "__main__":
    unittest.main()
