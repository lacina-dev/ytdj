"""Hlídací testy pro pravidla z docs/FUNKCE.md, která dřív žádný test neměla.

Jen chování, které aplikace už má (25.–26. 9.), proti existujícím
falešným součástem (tests/fake_mpv.py, Rig z tests/test_wishes.py):

    YTDJ_EVENTS_FILE=/tmp/ev.jsonl .venv/bin/python -m unittest tests.test_funkce_guards -v
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="ytdj-funkce-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)
os.environ.setdefault("YTDJ_EVENTS_FILE", str(Path(_TMP) / "events.jsonl"))

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_player_queue import Harness, T  # noqa: E402
from test_wishes import Rig  # noqa: E402
from test_wishes import T as WT  # noqa: E402
from ytdj.config import DEFAULTS, Config  # noqa: E402
from ytdj.music.catalog import LinkTarget, Track  # noqa: E402
from ytdj.music.radio import RadioPools  # noqa: E402
from ytdj.player import mpv as mpvmod  # noqa: E402
from ytdj.player.mpv import MpvPlayer  # noqa: E402

INDEX = ROOT / "ytdj/web/static/index.html"


def run(coro):
    return asyncio.run(coro)


def _args(player: MpvPlayer) -> list[str]:
    # shim pro resolver by se zapisoval mimo test — cesta k yt-dlp stačí
    with mock.patch.object(player, "_ytdl_shim", lambda: "yt-dlp"):
        return player._args()


class VolumeRemembered(unittest.TestCase):
    """F-HLAS-05: hlasitost přežije restart; mpv startuje rovnou s ní."""

    def test_mpv_starts_at_the_saved_volume_capped(self) -> None:
        args = _args(MpvPlayer(Config(**{**DEFAULTS, "volume": 40})))
        self.assertIn("--volume=40", args)
        self.assertIn("--volume-max=100", args)
        args = _args(MpvPlayer(Config(**{**DEFAULTS, "volume": 130})))
        self.assertIn("--volume=100", args)

    def test_set_volume_is_written_to_config_once_after_a_pause(self) -> None:
        saved: list[dict] = []

        async def go():
            async with Harness(jitter=0) as h:
                with mock.patch.object(mpvmod, "SAVE_DELAY", 0.05), \
                        mock.patch.object(mpvmod, "save_values", saved.append):
                    for v in (50, 55, 60, 130):  # tažení posuvníku
                        await h.player.set_volume(v)
                    self.assertEqual(saved, [])  # ne při každém pohybu
                    await asyncio.sleep(0.2)
                    self.assertEqual(saved, [{"volume": 100}])
                    self.assertEqual(h.player.cfg.volume, 100)

        run(go())


class StartsWithBufferedStream(unittest.TestCase):
    """F-ZVUK-03: skladba začne až se 2 s proudu v cache (bez lupnutí na začátku)."""

    def test_cache_pause_wait(self) -> None:
        args = _args(MpvPlayer(Config(**DEFAULTS)))
        self.assertIn("--cache-pause-initial=yes", args)
        self.assertIn("--cache-pause-wait=2", args)


class TimeStandsWhileLoading(unittest.TestCase):
    """F-ZVUK-04: dokud proud nezačal, stav říká 'buffering' a web drží čas."""

    def test_status_reports_buffering_only_while_really_waiting(self) -> None:
        async def go():
            async with Harness(jitter=0) as h:
                await h.player.enqueue([T(i) for i in range(3)])
                await h.settle()
                real = h.fake._prop
                idle = {"on": True}

                def prop(name):
                    if name == "core-idle":
                        return idle["on"]
                    return real(name)

                h.fake._prop = prop
                st = await h.player.status()
                self.assertTrue(st.buffering)
                idle["on"] = False  # zvuk teče
                self.assertFalse((await h.player.status()).buffering)
                idle["on"] = True
                await h.player.toggle_pause(True)  # pauza není načítání
                self.assertFalse((await h.player.status()).buffering)

        run(go())

    def test_web_page_holds_the_clock_while_loading(self) -> None:
        html = INDEX.read_text()
        self.assertIn("S.loading = S.running && !!s.buffering", html)
        self.assertIn("if (S.running && !S.loading) p +=", html)  # čas běží jen při zvuku
        self.assertIn("načítám…", html)


class LinkWish(unittest.TestCase):
    """F-PRANI-15: odkaz na YouTube je přání bez modelu."""

    def test_video_link_queues_that_track_without_the_model(self) -> None:
        song = WT("linksong", "Linkers")

        async def resolve_link(url: str):
            if "youtu.be/linksong000" in url:
                return LinkTarget("track", song.label(), [song])
            return None

        async def go():
            async with Rig() as rig:
                rig.catalog.resolve_link = resolve_link
                await rig.background()
                w = rig.wq.submit("https://youtu.be/linksong000", "Petr")
                await rig.until(lambda: w.state in ("queued", "playing"))
                self.assertEqual(w.via, "link")
                self.assertEqual([t.id for t in w.tracks], [song.id])
                self.assertEqual(rig.asked, [])  # žádný tah modelu
                bad = rig.wq.submit("https://youtu.be/nonsense000", "Jana")
                await rig.until(lambda: not bad.active)
                self.assertIn("odkaz jsem nerozluštil", bad.reply)
                self.assertEqual(rig.asked, [])

        run(go())


class ArtistCapInBackground(unittest.TestCase):
    """F-ZVUK-06: v podkresu nejvýš 2 skladby téhož interpreta na jedno doplnění."""

    def test_two_per_artist_unless_artist_mode(self) -> None:
        same = [Track(f"s{n:010d}", f"S{n}", "Same", None, 200) for n in range(6)]
        other = [Track(f"o{n:010d}", f"O{n}", f"Other {n}", None, 200) for n in range(6)]

        class Cat:
            async def radio(self, video_id, limit=50):
                return same + other

        class Store:
            def blacklisted(self):
                return set()

            def recently_played(self, days):
                return set()

            def record_seed(self, *a):
                pass

        cfg = types.SimpleNamespace(repeat_days=30, max_duration=600, max_duration_request=5400,
                                    min_duration=60, pool_low=10, radio_limit=50)
        pools = RadioPools(Cat(), Store(), cfg)
        run(pools.set_seeds([Track("seed0000000", "S", "X", None, 200)], mood="mix"))
        got = run(pools.next_tracks(6))
        self.assertEqual(len(got), 6)
        self.assertEqual(sum(t.artist == "Same" for t in got), 2)
        run(pools.set_artist("Same", list(same)))  # vyžádaný interpret: strop neplatí
        self.assertEqual({t.artist for t in run(pools.next_tracks(4))}, {"Same"})


class NickOnFirstOpen(unittest.TestCase):
    """F-NICK-01: web se při prvním otevření zeptá na přezdívku a pamatuje si ji."""

    def test_page_asks_for_a_nick_when_it_has_none(self) -> None:
        html = INDEX.read_text()
        self.assertIn('S.nick = load("ytdj.nick")', html)
        self.assertTrue(re.search(r'if \(!S\.nick\) openNick\(false', html))
        self.assertIn('store("ytdj.nick", nick)', html)
        self.assertIn('postJSON("/api/me"', html)  # server drží přezdívku podle prohlížeče


if __name__ == "__main__":
    unittest.main()
