"""Start služby: přehrávač první, zbytek aplikace souběžně (F-RESTART-08),
a událost `app.start` s časy fází (F-PROVOZ-07).

    YTDJ_EVENTS_FILE=/tmp/x.jsonl .venv/bin/python -m unittest tests.test_startup -v

Pi 26. 9.: mpv se spouštělo až ~2,7 s po startu procesu (import celé aplikace
včetně ytmusicapi, webu a terminálu), navázaná skladba zazněla ~8 s po konci
starého mpv.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="ytdj-startup-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)
os.environ.setdefault("XDG_RUNTIME_DIR", _TMP)
os.environ.setdefault("YTDJ_EVENTS_FILE", str(Path(_TMP) / "events.jsonl"))

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_wishes as tw  # noqa: E402
from fake_mpv import FakeMpv  # noqa: E402
from ytdj import __main__ as main  # noqa: E402
from ytdj import telemetry, wishes  # noqa: E402
from ytdj.config import DATA_DIR, DEFAULTS, Config  # noqa: E402
from ytdj.music import catalog as catalog_mod  # noqa: E402
from ytdj.player.mpv import MpvPlayer  # noqa: E402

HEAVY = ("ytmusicapi", "prompt_toolkit", "starlette", "uvicorn")


class StubWeb:
    def __init__(self, app, host, port) -> None:
        self.url = "http://stub"

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def poke(self) -> None: ...


def make_player_class(fake: FakeMpv, playback: Path, order: list[str]):
    class StartPlayer(MpvPlayer):
        """Opravdový MpvPlayer; start() se místo spuštění mpv připojí k falešnému."""

        starts = 0
        stopped = False
        connected = threading.Event()

        async def start(self) -> None:
            type(self).starts += 1
            order.append("player.start")
            r, w = await asyncio.open_unix_connection(str(fake.path))
            self.connected.set()

            async def resolver_call(req: dict) -> dict:
                return {"ok": True}

            async def wait_ready(video_id: str, timeout: float) -> bool:
                return True

            self._resolver_call = resolver_call  # type: ignore[method-assign]
            self._resolver = SimpleNamespace(returncode=0)  # type: ignore[assignment]
            self.wait_ready = wait_ready  # type: ignore[method-assign]
            self.playback_file = playback
            try:
                await asyncio.sleep(0.2)  # mpv startuje (na Pi ~1,4 s)
            except asyncio.CancelledError:
                w.close()
                raise
            await self.attach(r, w)
            order.append("player.ready")

        async def stop(self) -> None:
            type(self).stopped = True
            await super().stop()

    return StartPlayer


async def interrupted_session(tmp: Path) -> tuple[str, Path]:
    """Hrálo přání, služba se restartuje: session.json do DATA_DIR a playback.json."""
    async with tw.Rig() as rig:
        await rig.background()
        j = rig.wq.submit("Holky z naší školky", "Jana")
        await rig.until(lambda: j.state == "playing")
        playing = rig.fake.current_vid()
        await rig.wq.refresh_playing()
        rig.wq.state_file = DATA_DIR / "session.json"
        rig.wq.save()
        pb = tmp / "playback.json"
        rig.player.playback_file = pb
        rig.player._time_pos = 42.0
        rig.player._save_playback()
    return playing, pb


def fake_catalog(cfg):
    cat = tw.Catalog()
    cat.authenticated = False
    cat.warmed = 0

    def warm() -> None:
        cat.warmed += 1

    cat.warm = warm
    return cat


def day_resume(orig=wishes.should_resume):
    def fixed(saved, now=None, wall=None, boot=None):
        return orig(saved, datetime(2026, 9, 25, 10, 0), wall, boot)
    return fixed


class StartOrder(unittest.TestCase):
    def test_player_starts_before_the_rest_of_the_app_and_resumes(self):
        """mpv se spustí dřív, než se importuje DJ a fronta přání; App.run na
        běžící start jen počká (žádný druhý start) a přerušená skladba hraje
        dál od místa. Web a katalog (ytmusicapi) až potom."""
        order: list[str] = []
        events: list[tuple[str, dict]] = []

        async def go():
            tmp = Path(tempfile.mkdtemp(dir=_TMP))
            playing, pb = await interrupted_session(tmp)
            fake = FakeMpv(tmp / "mpv.sock")
            await fake.start()
            Player = make_player_class(fake, pb, order)

            def import_app() -> None:
                order.append("imports")
                main._import_app()

            cfg = Config(**DEFAULTS)
            with mock.patch.object(main, "MpvPlayer", Player), \
                    mock.patch("ytdj.music.Catalog", fake_catalog), \
                    mock.patch("ytdj.web.WebServer", StubWeb), \
                    mock.patch.object(main, "yt_dlp_warning", mock.AsyncMock(return_value=None)), \
                    mock.patch.object(wishes, "should_resume", day_resume()), \
                    mock.patch.object(telemetry, "event",
                                      lambda kind, **f: events.append((kind, f))):
                clock = main.StartClock()
                app = await main.start_app(cfg, clock, import_app=import_app)
                self.assertIsInstance(app.player, Player)
                self.assertIsNone(app.web)  # web až po rozjezdu přehrávače
                run = asyncio.create_task(app.run(repl=False))
                loop = asyncio.get_running_loop()
                end = loop.time() + 5
                while playing not in fake.started:
                    self.assertLess(loop.time(), end, "přerušená skladba nezačala")
                    self.assertFalse(run.done(), run)
                    await asyncio.sleep(0.02)
                self.assertEqual(fake.started[0], playing)
                self.assertEqual(list(fake.loadfile_options.values()), [{"start": "40.0"}])
                app.restart_requested.set()
                rc = await asyncio.wait_for(run, 10)
            await fake.close()
            return rc, Player

        rc, Player = asyncio.run(go())
        self.assertEqual(rc, 1)
        self.assertEqual(order[:2], ["player.start", "imports"])
        self.assertEqual(Player.starts, 1)
        self.assertTrue(Player.stopped)
        start = [f for k, f in events if k == "app.start"]
        self.assertEqual(len(start), 1, [k for k, _ in events])
        f = start[0]
        for key in ("player_call_ms", "app_imports_ms", "app_ms", "player_ms",
                    "resume_ms", "web_ms", "proc_start"):
            self.assertIn(key, f)
        self.assertTrue(f["web"])
        self.assertLessEqual(f["player_call_ms"], f["app_imports_ms"])
        self.assertLessEqual(f["player_ms"], f["resume_ms"])
        self.assertLessEqual(f["resume_ms"], f["web_ms"])
        resumed = [k for k, _ in events]
        self.assertIn("player.resume_track", resumed)

    def test_failed_import_stops_the_started_player(self):
        async def go():
            tmp = Path(tempfile.mkdtemp(dir=_TMP))
            fake = FakeMpv(tmp / "mpv.sock")
            await fake.start()
            Player = make_player_class(fake, tmp / "playback.json", [])

            def broken() -> None:
                Player.connected.wait(2)  # import chvíli trvá; mpv už startuje
                raise ImportError("rozbitý modul")

            with mock.patch.object(main, "MpvPlayer", Player):
                with self.assertRaises(ImportError):
                    await main.start_app(Config(**DEFAULTS), import_app=broken)
            await fake.close()
            return Player

        Player = asyncio.run(go())
        self.assertEqual(Player.starts, 1)
        self.assertTrue(Player.stopped)


class LightStart(unittest.TestCase):
    def test_player_path_does_not_import_the_heavy_parts(self):
        """Co je potřeba ke startu přehrávače (a k aplikaci bez webu a
        terminálu), nenačte ytmusicapi, prompt_toolkit ani web; YTMusic
        vznikne až voláním katalogu nebo `warm()`."""
        code = f"""
import sys
HEAVY = {HEAVY!r}
def loaded():
    return sorted({{m.split('.')[0] for m in sys.modules}} & set(HEAVY)
                  | {{m for m in sys.modules if m.startswith(('ytdj.agent', 'ytdj.wishes',
                                                             'ytdj.web', 'ytdj.ui'))}})
import ytdj.__main__ as main
print("main", loaded())
main._import_app()
from ytdj.config import DEFAULTS, Config
from ytdj.music.catalog import Catalog
cat = Catalog(Config(**DEFAULTS))
bound = cat.yt.search  # v event loopu jen atribut, YTMusic nevzniká
print("app", sorted({{m.split('.')[0] for m in sys.modules}} & set(HEAVY)))
cat.warm()
print("warm", "ytmusicapi" in sys.modules)
"""
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             env=env, cwd=str(ROOT), timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = dict(line.split(" ", 1) for line in out.stdout.strip().splitlines())
        self.assertEqual(lines["main"], "[]")
        self.assertEqual(lines["app"], "[]")
        self.assertEqual(lines["warm"], "True")

    def test_lazy_ytmusic_is_created_once_in_the_calling_thread(self):
        made: list[str] = []
        gate = threading.Event()

        def factory():
            made.append(threading.current_thread().name)
            gate.wait(1)
            return SimpleNamespace(search=lambda q, **kw: [q, kw])

        lazy = catalog_mod.LazyYT(factory)
        fn = lazy.search  # atribut: nic nevytváří
        self.assertEqual(made, [])

        async def go():
            calls = [asyncio.to_thread(fn, "a", limit=1) for _ in range(4)]
            gate.set()
            return await asyncio.gather(*calls)

        res = asyncio.run(go())
        self.assertEqual(res, [["a", {"limit": 1}]] * 4)
        self.assertEqual(len(made), 1)
        self.assertNotEqual(made[0], threading.main_thread().name)
        self.assertEqual(lazy.search("b"), ["b", {}])  # už vytvořený: přímo


class StartEvent(unittest.TestCase):
    def test_clock_measures_from_process_start(self):
        age = main._proc_age_s()
        self.assertIsNotNone(age)
        self.assertGreater(age, 0)
        clock = main.StartClock()
        self.assertTrue(clock.exact)
        clock.mark("x")
        clock.mark("x")  # první značka platí
        f = clock.fields()
        self.assertGreaterEqual(f["x_ms"], 0)
        self.assertAlmostEqual(f["proc_start"], __import__("time").time() - age, delta=1.0)


if __name__ == "__main__":
    unittest.main()
