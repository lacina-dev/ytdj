"""Start přehrávače: priorita procesů bez preexec_fn (obal nice, všechna vlákna),
fallback yt-dlp ze shimu s absolutní prioritou, resolver souběžně s mpv a
rozpad času `player.start`.

    YTDJ_EVENTS_FILE=/tmp/x.jsonl python -m unittest tests.test_player_start

Snížit nice (zvýšit přednost) smí jen privilegovaný proces, takže se tu
priorita ověřuje směrem dolů (vlastní nice + 2 / + 3) — mechanismus je stejný
jako u -11 na Pi.
"""

from __future__ import annotations

import asyncio
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="ytdj-start-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ytdj import telemetry  # noqa: E402
from ytdj.config import DEFAULTS, Config  # noqa: E402
from ytdj.player import mpv as mpvmod  # noqa: E402
from ytdj.player import ytdl_cache  # noqa: E402
from ytdj.player.mpv import MpvPlayer  # noqa: E402

OWN = os.getpriority(os.PRIO_PROCESS, 0)

# Program se třemi vlákny, která vzniknou po startu; vypíše nice všech vláken.
THREADS = r"""
import os, sys, threading, time
ev = threading.Event()
ts = [threading.Thread(target=ev.wait, daemon=True) for _ in range(3)]
for t in ts: t.start()
tids = os.listdir(f"/proc/{os.getpid()}/task")
print(sorted(os.getpriority(os.PRIO_PROCESS, int(t)) for t in tids), len(tids))
"""

# Falešné mpv jako samostatný proces: IPC socket (tests/fake_mpv.py) a pár vláken.
FAKE_MPV = r"""
import asyncio, sys, threading
sys.path.insert(0, sys.argv[2])
from fake_mpv import FakeMpv
ev = threading.Event()
for _ in range(3):
    threading.Thread(target=ev.wait, daemon=True).start()
async def main():
    await asyncio.sleep(float(sys.argv[3]))  # "start mpv" (knihovny, init)
    fake = FakeMpv(__import__("pathlib").Path(sys.argv[1]))
    await fake.start()
    await asyncio.Event().wait()
asyncio.run(main())
"""


class NicedWrapper(unittest.TestCase):
    def test_absolute_priority_for_all_threads(self) -> None:
        """Obal nastaví prioritu před exec — dědí ji i vlákna vzniklá později."""
        target = OWN + 3
        argv = mpvmod.niced([sys.executable, "-c", THREADS], target)
        self.assertEqual(argv[:3], [mpvmod.NICE_BIN, "-n", "3"])
        out = subprocess.run(argv, capture_output=True, text=True, check=True).stdout
        self.assertEqual(out.split("]")[0] + "]", str([target] * 4))

    def test_no_wrapper_when_already_there(self) -> None:
        self.assertEqual(mpvmod.niced(["x"], OWN), ["x"])
        with mock.patch.object(mpvmod, "NICE_BIN", None):
            self.assertEqual(mpvmod.niced(["x"], OWN + 1), ["x"])

    def test_relative_delta_is_computed_from_own_nice(self) -> None:
        """-11 na Pi (ytdj 0) i v sandboxu s nice 5: vždy absolutní cíl."""
        with mock.patch.object(mpvmod.os, "getpriority", return_value=5):
            self.assertEqual(mpvmod.niced(["mpv"], -11)[1:3], ["-n", "-16"])
        with mock.patch.object(mpvmod.os, "getpriority", return_value=0):
            self.assertEqual(mpvmod.niced(["mpv"], -11)[1:3], ["-n", "-11"])

    def test_renice_all_tasks_fallback(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c",
                                 "import threading,time\n"
                                 "for _ in range(3): threading.Thread(target=time.sleep, "
                                 "args=(30,), daemon=True).start()\n"
                                 "print('up', flush=True); time.sleep(30)"],
                                stdout=subprocess.PIPE, text=True)
        try:
            proc.stdout.readline()
            self.assertEqual(set(mpvmod.task_nices(proc.pid)), {OWN})
            self.assertEqual(mpvmod.renice_tasks(proc.pid, OWN + 2), 4)
            self.assertEqual(mpvmod.task_nices(proc.pid), [OWN + 2] * 4)
            self.assertEqual(mpvmod.renice_tasks(proc.pid, OWN + 2), 0)
        finally:
            proc.kill()
            proc.wait()
            proc.stdout.close()

    def test_no_preexec_fn_left(self) -> None:
        """preexec_fn ve vícevláknovém procesu může uváznout (review P3)."""
        src = (ROOT / "ytdj" / "player" / "mpv.py").read_text()
        self.assertNotIn("preexec_fn=", src)


class ShimFallbackNice(unittest.TestCase):
    def test_exec_real_sets_absolute_priority(self) -> None:
        with mock.patch.object(ytdl_cache.os, "setpriority") as setp, \
                mock.patch.object(ytdl_cache.os, "nice") as nice, \
                mock.patch.object(ytdl_cache.os, "execv") as execv, \
                mock.patch.dict(os.environ, {ytdl_cache.ENV_REAL: "/x/yt-dlp"}):
            ytdl_cache._exec_real(["-J", "u"])
        setp.assert_called_once_with(os.PRIO_PROCESS, 0, ytdl_cache.REAL_NICE)
        nice.assert_not_called()
        # (skutečné execv se nevrátí; mock ano, proto se bere první volání)
        self.assertEqual(execv.call_args_list[0],
                         mock.call("/x/yt-dlp", ["/x/yt-dlp", "-J", "u"]))

    def test_real_ytdlp_does_not_inherit_parent_offset(self) -> None:
        """Shim je potomek mpv (Pi: -11). Relativní +5 dávalo yt-dlp -6; teď
        max(5, nice rodiče) — níž než rodič jít bez oprávnění nejde."""
        d = Path(tempfile.mkdtemp(dir=_TMP))
        fake = d / "yt-dlp"
        fake.write_text("#!/bin/sh\nps -o ni= -p $$\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        env = dict(os.environ, **{ytdl_cache.ENV_REAL: str(fake), "YTDJ_TELEMETRY": "0"})
        env.pop(ytdl_cache.ENV_SOCKET, None)
        parent = OWN + 2  # "mpv"
        out = subprocess.run(
            ["nice", "-n", "2", sys.executable, ytdl_cache.__file__, "-J", "--",
             "https://music.youtube.com/watch?v=dQw4w9WgXcQ"],
            env=env, capture_output=True, text=True, check=True).stdout
        self.assertEqual(int(out.strip()), max(ytdl_cache.REAL_NICE, parent))
        self.assertNotEqual(int(out.strip()), parent + 5)


class _NoSampler:
    def __init__(self, *a, **k) -> None:
        pass

    def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class StartTest(unittest.TestCase):
    def run_start(self, delay: float = 0.3, stale: bool = False):
        d = Path(tempfile.mkdtemp(dir=_TMP))
        sock = d / "mpv.sock"
        events: list[tuple[str, dict]] = []
        order: list[str] = []
        if stale:
            s = socket.socket(socket.AF_UNIX)
            s.bind(str(sock))  # soubor zůstane, nikdo neposlouchá
            s.close()

        async def go():
            player = MpvPlayer(Config(**DEFAULTS))

            async def fake_resolver(self_=player):
                order.append("resolver" if player.writer is None else "resolver-late")
                player._resolver = None

            args = [sys.executable, "-c", FAKE_MPV, str(sock), str(ROOT / "tests"), str(delay)]
            with mock.patch.object(mpvmod, "MPV_SOCKET", sock), \
                    mock.patch.object(mpvmod, "MPV_NICE", OWN + 3), \
                    mock.patch.object(mpvmod, "runtime_file", lambda name: None), \
                    mock.patch.object(mpvmod, "SystemSampler", _NoSampler), \
                    mock.patch.object(MpvPlayer, "_args", lambda self: args), \
                    mock.patch.object(player, "_start_resolver", fake_resolver), \
                    mock.patch.object(telemetry, "event",
                                      lambda kind, **f: events.append((kind, f))):
                t0 = time.monotonic()
                await player.start()
                took = time.monotonic() - t0
                await player.stop()
            return took

        took = asyncio.run(go())
        return took, events, order

    def test_start_breakdown_priority_and_resolver_first(self) -> None:
        took, events, order = self.run_start(delay=0.3)
        self.assertEqual(order, ["resolver"])  # před spojením s mpv, ne až po něm
        start = [f for k, f in events if k == "player.start"][-1]
        for key in ("spawn_ms", "sock_ms", "seen_ms", "connect_ms", "gap_ms", "took_ms"):
            self.assertIsInstance(start[key], int, key)
        # socket vznikl až po "startu mpv" (0,3 s); uvidíme ho do pár desítek ms
        self.assertGreaterEqual(start["sock_ms"], 250)
        self.assertLess(start["seen_ms"] - start["sock_ms"], 150)
        self.assertLessEqual(start["seen_ms"], start["connect_ms"])
        self.assertIsNone(start.get("stale_sock"))
        self.assertIn("mpv_cpu_ms", start)
        self.assertIn("mpv_majflt", start)
        self.assertEqual(start["nice"], OWN + 3)
        prio = [f for k, f in events if k == "player.priority"][-1]
        self.assertEqual((prio["mpv"], prio["mpv_min"], prio["mpv_max"]),
                         (OWN + 3, OWN + 3, OWN + 3))
        self.assertGreaterEqual(prio["mpv_tasks"], 4)

    def test_stale_socket_is_reported_and_not_connected_to(self) -> None:
        took, events, order = self.run_start(delay=0.2, stale=True)
        start = [f for k, f in events if k == "player.start"][-1]
        self.assertTrue(start["stale_sock"])
        self.assertGreaterEqual(start["connect_ms"], 150)

    def test_mpv_that_exits_fails_fast(self) -> None:
        d = Path(tempfile.mkdtemp(dir=_TMP))

        async def go():
            player = MpvPlayer(Config(**DEFAULTS))

            async def no_resolver():
                player._resolver = None

            with mock.patch.object(mpvmod, "MPV_SOCKET", d / "mpv.sock"), \
                    mock.patch.object(mpvmod, "runtime_file", lambda name: None), \
                    mock.patch.object(MpvPlayer, "_args",
                                      lambda self: [sys.executable, "-c", "raise SystemExit(2)"]), \
                    mock.patch.object(player, "_start_resolver", no_resolver), \
                    mock.patch.object(telemetry, "event", lambda kind, **f: None):
                t0 = time.monotonic()
                with self.assertRaises(RuntimeError):
                    await player.start()
                return time.monotonic() - t0

        self.assertLess(asyncio.run(go()), 5)  # dřív čekalo 20 s na socket


class PriorityTelemetry(unittest.TestCase):
    def test_priority_waits_for_mpv_and_resolver_listen(self) -> None:
        async def go():
            events: list[tuple[str, dict]] = []
            player = MpvPlayer(Config(**DEFAULTS))
            player._resolver = object()  # type: ignore[assignment]  # "běží"
            with mock.patch.object(telemetry, "event",
                                   lambda kind, **f: events.append((kind, f))):
                player._t_priority()
                self.assertFalse([k for k, _ in events if k == "player.priority"])
                player._mpv_up = True
                player._on_resolver_event("resolver.listen", {"disk": 0, "template": True})
            prio = [f for k, f in events if k == "player.priority"]
            self.assertEqual(len(prio), 1)
            self.assertEqual((prio[0]["want_mpv"], prio[0]["want_resolver"]),
                             (mpvmod.MPV_NICE, mpvmod.RESOLVER_NICE))
            listen = [f for k, f in events if k == "resolver.listen"][-1]
            self.assertIn("listen_ms", listen)
            for t in asyncio.all_tasks() - {asyncio.current_task()}:
                t.cancel()
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
