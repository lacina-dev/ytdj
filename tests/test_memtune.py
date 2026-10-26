"""Paměť: glibc malloc bez děravých arén v ytdj a resolveru (ytdj/memtune.py).

    YTDJ_EVENTS_FILE=/tmp/x.jsonl .venv/bin/python -m unittest tests.test_memtune
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if not os.environ.get("YTDJ_EVENTS_FILE"):
    _EVENTS_TMP = tempfile.TemporaryDirectory(prefix="ytdj-memtune-tests-")
    os.environ["YTDJ_EVENTS_FILE"] = str(Path(_EVENTS_TMP.name) / "events.jsonl")

from ytdj import memtune  # noqa: E402
from ytdj.config import DEFAULTS, Config  # noqa: E402
from ytdj.player import mpv as mpvmod  # noqa: E402

GLIBC = sys.platform.startswith("linux") and os.confstr("CS_GNU_LIBC_VERSION") is not None


class FakeLibc:
    def __init__(self, ok=1):
        self.calls, self.ok = [], ok

    def mallopt(self, param, value):
        self.calls.append((param, value))
        return self.ok


class Tune(unittest.TestCase):
    def test_two_arenas_and_a_fixed_mmap_threshold_unless_switched_off(self):
        lib = FakeLibc()
        with mock.patch.dict(os.environ, {"YTDJ_MALLOC": "1"}):
            self.assertTrue(memtune.tune_malloc(lib))
        # M_ARENA_MAX = -8, M_MMAP_THRESHOLD = -3 (mallopt(3))
        self.assertEqual(lib.calls, [(-8, 2), (-3, 128 * 1024)])
        lib = FakeLibc()
        with mock.patch.dict(os.environ, {"YTDJ_MALLOC": "0"}):
            self.assertFalse(memtune.tune_malloc(lib))
        self.assertEqual(lib.calls, [])
        self.assertFalse(memtune.tune_malloc(FakeLibc(ok=0)))  # glibc odmítla

        class NoLibc:
            def mallopt(self, *a):
                raise OSError("jiná libc")

        self.assertFalse(memtune.tune_malloc(NoLibc()))

    @unittest.skipUnless(GLIBC, "jen glibc")
    def test_real_glibc_accepts_it(self):
        out = subprocess.run([sys.executable, "-c",
                              "import sys; sys.path.insert(0, sys.argv[1]);"
                              "from ytdj import memtune; print(memtune.tune_malloc())", str(ROOT)],
                             capture_output=True, text=True, check=True,
                             env={**os.environ, "YTDJ_MALLOC": "1"}).stdout
        self.assertEqual(out.strip(), "True")

    def test_child_env_keeps_own_settings(self):
        with mock.patch.dict(os.environ, {"YTDJ_MALLOC": "1"}):
            env = memtune.malloc_env({"PATH": "/bin"})
            self.assertEqual((env["MALLOC_ARENA_MAX"], env["MALLOC_MMAP_THRESHOLD_"]), ("2", "131072"))
            self.assertEqual(memtune.malloc_env({"MALLOC_ARENA_MAX": "4"})["MALLOC_ARENA_MAX"], "4")
        with mock.patch.dict(os.environ, {"YTDJ_MALLOC": "0"}):
            self.assertNotIn("MALLOC_ARENA_MAX", memtune.malloc_env({}))


class Wiring(unittest.TestCase):
    def test_resolver_gets_it_and_music_mpv_does_not(self):
        """Resolver (yt-dlp v Pythonu) dostane totéž prostředím; hudební mpv
        ne — u něj to změřeno nepomohlo (51 → 83 MB), jeho prostředí se nemění."""
        tmp = Path(tempfile.mkdtemp(prefix="ytdj-memtune-"))
        shim = tmp / "yt-dlp"
        shim.write_text("#!/usr/bin/python3\n")
        cfg = Config(**{**DEFAULTS, "yt_dlp_path": str(shim)})
        player = mpvmod.MpvPlayer(cfg)
        seen = {}

        class Proc:
            stderr = None
            returncode = None

            async def wait(self):
                return 0

        async def fake_exec(*argv, env=None, **kw):
            seen["argv"], seen["env"] = argv, env
            return Proc()

        async def go():
            with mock.patch.object(mpvmod.asyncio, "create_subprocess_exec", fake_exec), \
                    mock.patch.object(mpvmod, "RESOLVER_SOCKET", tmp / "r.sock"), \
                    mock.patch.object(mpvmod, "runtime_file", lambda name: None), \
                    mock.patch.object(player, "_watch_resolver", lambda p: asyncio.sleep(0)), \
                    mock.patch.dict(os.environ, {"YTDJ_MALLOC": "1"}):
                await player._start_resolver()
                await asyncio.sleep(0)

        asyncio.run(go())
        self.assertTrue(any("ytdl_resolver.py" in str(a) for a in seen["argv"]))
        self.assertEqual(seen["env"]["MALLOC_ARENA_MAX"], "2")
        self.assertEqual(seen["env"]["MALLOC_MMAP_THRESHOLD_"], "131072")
        with mock.patch.dict(os.environ, {"YTDJ_MALLOC": "1"}):
            self.assertNotIn("MALLOC_ARENA_MAX", cfg.child_env())  # tohle dostává mpv

    def test_token_server_gets_a_smaller_young_heap_from_deploy(self):
        """Pi: node 187 MB špička; na PC 171 → 142 MB s menší mladou generací.
        Strop staré generace ne — server by při růstu spadl."""
        conf = (ROOT / "packaging/rpi/pot-memory.conf").read_text(encoding="utf-8")
        self.assertIn("Environment=NODE_OPTIONS=--max-semi-space-size=2", conf)
        self.assertNotIn("Environment=NODE_OPTIONS=--max-old-space-size", conf)
        deploy = (ROOT / "packaging/rpi/deploy.sh").read_text(encoding="utf-8")
        self.assertIn('install -m 644 packaging/rpi/pot-memory.conf "$pot_d/60-memory.conf"', deploy)
        self.assertIn('pot_d="$HOME/.config/systemd/user/ytdj-pot.service.d"', deploy)
        # po změně daemon-reload a restart serveru (bez roota, uživatelská služba)
        after = deploy[deploy.index("pot-memory.conf"):]
        self.assertIn("systemctl --user restart ytdj-pot.service", after)
        self.assertNotIn("sudo", deploy[deploy.index("pot_d="):deploy.index("pipewire.conf.d")])

    def test_main_process_tunes_itself_before_any_thread(self):
        src = (ROOT / "ytdj" / "__main__.py").read_text(encoding="utf-8")
        tune = src.index("memtune.tune_malloc()")
        self.assertLess(tune, src.index("import asyncio"))
        self.assertIn("malloc_tuned=MALLOC_TUNED", src)


if __name__ == "__main__":
    unittest.main()
