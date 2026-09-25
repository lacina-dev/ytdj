"""Event loop se nesmí zaseknout: pomalá síť ani pomalá SD karta nesmí zastavit
web, panel a frontu přání (Pi 25. 9.: 10,7 s v handleru startu skladby).

    .venv/bin/python -m unittest tests.test_loop -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="ytdj-loop-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)
os.environ.setdefault("YTDJ_EVENTS_FILE", str(Path(_TMP) / "events.jsonl"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_wishes as tw  # noqa: E402
from ytdj import loopwatch, telemetry  # noqa: E402
from ytdj.__main__ import App  # noqa: E402
from ytdj.agent.intent import SkipWatch  # noqa: E402
from ytdj.state import Store  # noqa: E402


def blocking_disk_write() -> None:
    time.sleep(0.8)  # jako INSERT, který čeká na přetíženou SD kartu


class Watchdog(unittest.TestCase):
    def test_blocked_loop_is_reported_with_the_culprit(self):
        events: list[tuple[str, dict]] = []

        async def go():
            with mock.patch.object(telemetry, "event", lambda k, **f: events.append((k, f))):
                w = loopwatch.LoopWatch(threshold=0.3, tick=0.1)
                w.start()
                await asyncio.sleep(0.3)
                blocking_disk_write()
                await asyncio.sleep(0.3)
                await w.stop()

        asyncio.run(go())
        lags = [f for k, f in events if k == "sys.loop_lag"]
        self.assertEqual(len(lags), 1, lags)
        self.assertGreaterEqual(lags[0]["lag_ms"], 500)
        self.assertTrue(any("blocking_disk_write" in fr for fr in lags[0]["stack"]), lags[0])
        self.assertLessEqual(len(lags[0]["stack"]), loopwatch.FRAMES)

    def test_quiet_loop_reports_nothing(self):
        events: list = []

        async def go():
            with mock.patch.object(telemetry, "event", lambda k, **f: events.append(k)):
                w = loopwatch.LoopWatch(threshold=0.3, tick=0.05)
                w.start()
                for _ in range(20):
                    await asyncio.sleep(0.02)
                await w.stop()

        asyncio.run(go())
        self.assertNotIn("sys.loop_lag", events)

    def test_stuck_loop_is_reported_while_stuck(self):
        events: list[tuple[str, dict]] = []

        async def go():
            with mock.patch.object(telemetry, "event", lambda k, **f: events.append((k, f))):
                w = loopwatch.LoopWatch(threshold=0.2, tick=0.05, stuck=0.5)
                w.start()
                await asyncio.sleep(0.1)
                time.sleep(0.9)
                ongoing = [f for k, f in events if k == "sys.loop_lag" and f.get("ongoing")]
                self.assertTrue(ongoing)  # zapsáno, ještě než se loop rozběhl
                await w.stop()

        asyncio.run(go())


class StoreWrites(unittest.TestCase):
    def test_writes_do_not_wait_for_the_disk(self):
        store = Store(Path(tempfile.mkdtemp(dir=_TMP)) / "state.db", background=True)
        store._later(lambda: time.sleep(1.0))  # SD karta právě nestíhá
        t0 = time.monotonic()
        store.record_start("vid00000001", "Malá dáma", "Kabát", "Kabát")
        store.record_outcome("vid00000001", "finished")
        self.assertLess(time.monotonic() - t0, 0.05)
        store.flush()
        self.assertEqual([(r.video_id, r.outcome) for r in store.recent_history(5)],
                         [("vid00000001", "finished")])
        store.close()

    def test_reads_can_run_off_the_loop(self):
        store = Store(Path(tempfile.mkdtemp(dir=_TMP)) / "state.db", background=True)
        store.record_start("vid00000002", "T", "A", None)
        store.flush()

        async def go():
            return await store.aread("recent_history", 5)

        self.assertEqual(asyncio.run(go())[0].video_id, "vid00000002")
        store.close()


class ThreePeopleOnASlowPi(unittest.TestCase):
    """3 lidé naráz, katalog odpovídá 2 s a SD karta se zasekne: /api/status
    musí odpovídat hned (< 200 ms) a hlídač nesmí nic nahlásit."""

    def test_status_stays_responsive(self):
        async def go():
            async with tw.Rig(codex_delay=0.5, background_store=True) as rig:
                rig.catalog.delay = 2.0
                await rig.pools.set_seeds([tw.T("seed0", "Seed")], mood="podkres")
                await rig.player.enqueue(await rig.dj.next_tracks(4))
                await rig.settle()
                # handler startu skladby jako v ytdj (zápis do state.db)
                app = SimpleNamespace(store=rig.store, dj=rig.dj, pools=rig.pools,
                                      wishes=rig.wq, skips=SkipWatch(), repl=None)
                app._set_status = lambda text: None
                app._now = App._now
                rig.player._handlers.insert(0, App._on_event.__get__(app))
                srv = tw.web.WebServer(tw.WebApp(rig))
                watch = loopwatch.LoopWatch(threshold=0.2, tick=0.05)
                watch.start()
                # SD karta se zasekne na 1,5 s, zrovna když startují přání
                rig.store._later(lambda: time.sleep(1.5))
                wishes = [rig.wq.submit("pusť Kabát", "Petr"),
                          rig.wq.submit("Holky z naší školky", "Jana"),
                          rig.wq.submit("něco klidnějšího", "Karel")]
                worst = 0.0
                loop = asyncio.get_running_loop()
                end = loop.time() + 25
                while loop.time() < end and not all(w.state in ("queued", "playing") for w in wishes):
                    t0 = time.monotonic()
                    await srv._status(tw.req(method="GET"))
                    worst = max(worst, time.monotonic() - t0)
                    await asyncio.sleep(0.05)
                await watch.stop()
                return worst, wishes, watch

        worst, wishes, watch = asyncio.run(go())
        self.assertTrue(all(w.state in ("queued", "playing") for w in wishes),
                        [(w.who, w.state, w.reply) for w in wishes])
        self.assertLess(worst, 0.2, f"/api/status trvalo {worst * 1000:.0f} ms")
        self.assertEqual(watch.events, 0, f"loop stál {watch.max_lag_ms} ms")


if __name__ == "__main__":
    unittest.main()
