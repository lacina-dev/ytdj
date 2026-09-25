"""DJ: the persistent `codex app-server` client, against a fake server.

    python -m unittest tests.test_dj_appserver -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ytdj-test-")
os.environ["XDG_DATA_HOME"] = _TMP
os.environ["XDG_CONFIG_HOME"] = _TMP
os.environ["YTDJ_EVENTS_FILE"] = str(Path(_TMP) / "events.jsonl")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from ytdj.agent.appserver import (  # noqa: E402
    AppServer,
    AppServerAuthError,
    AppServerError,
    AppServerFatal,
    native_codex,
)
from ytdj.agent.codex import DECISION_SCHEMA  # noqa: E402

FAKE = Path(_TMP) / "codex"
FAKE.write_text(f"#!/bin/sh\nexec {sys.executable} {HERE / 'fake_app_server.py'} \"$@\"\n")
FAKE.chmod(0o755)


def run(coro):
    return asyncio.run(coro)


class Fake(unittest.TestCase):
    def setUp(self):
        self.log = Path(tempfile.mkdtemp(dir=_TMP)) / "msgs.jsonl"
        os.environ["FAKE_LOG"] = str(self.log)
        os.environ["FAKE_MODE"] = "ok"

    def sent(self):
        return [json.loads(x) for x in self.log.read_text().splitlines()]

    def test_warm_turns_reuse_process_and_rotate_threads(self):
        async def go():
            app = AppServer(str(FAKE), _TMP, model="m1", max_turns_per_thread=2)
            results = [await app.turn(f"p{i}", DECISION_SCHEMA, timeout=5) for i in range(3)]
            pid = app.proc.pid
            await app.close()
            return results, pid

        results, _ = run(go())
        self.assertGreater(results[0].startup_ms, 0)
        self.assertTrue(results[0].new_thread)
        self.assertFalse(results[1].new_thread)  # same thread, warm
        self.assertTrue(results[2].new_thread)  # rotated after 2 turns
        self.assertIn("thread-1", json.loads(results[1].text)["reply"])
        self.assertIn("thread-2", json.loads(results[2].text)["reply"])
        msgs = self.sent()
        self.assertEqual([m.get("method") for m in msgs].count("initialize"), 1)
        start = next(m for m in msgs if m.get("method") == "thread/start")["params"]
        self.assertEqual((start["sandbox"], start["approvalPolicy"], start["model"]),
                         ("read-only", "never", "m1"))
        turn = next(m for m in msgs if m.get("method") == "turn/start")["params"]
        self.assertEqual(turn["outputSchema"], DECISION_SCHEMA)

    def test_prewarm_hides_startup_and_disables_plugins(self):
        os.environ["FAKE_START_DELAY"] = "0.5"

        async def go():
            app = AppServer(str(FAKE), _TMP)
            app.prewarm()
            await asyncio.sleep(1.0)  # the fast path runs meanwhile
            res = await app.turn("p", DECISION_SCHEMA, timeout=5)
            await app.close()
            return res

        try:
            res = run(go())
        finally:
            del os.environ["FAKE_START_DELAY"]
        self.assertLess(res.startup_ms, 300)  # the 0.5 s start was paid in advance
        self.assertTrue(res.new_thread)
        msgs = self.sent()
        argv = msgs[0]["argv"]
        self.assertEqual(argv[0], "app-server")
        for feature in ("plugins", "remote_plugin", "apps", "shell_tool", "unified_exec"):
            self.assertIn(f"features.{feature}=false", argv)
        self.assertEqual([m.get("method") for m in msgs].count("initialize"), 1)

    def test_crash_mid_turn_raises_and_next_turn_restarts(self):
        async def go():
            app = AppServer(str(FAKE), _TMP)
            os.environ["FAKE_MODE"] = "crash"
            with self.assertRaises(AppServerError):
                await app.turn("p", DECISION_SCHEMA, timeout=5)
            await app.close()
            os.environ["FAKE_MODE"] = "ok"
            res = await app.turn("p", DECISION_SCHEMA, timeout=5)
            await app.close()
            return res

        self.assertGreater(run(go()).startup_ms, 0)  # a fresh process

    def test_hang_times_out(self):
        os.environ["FAKE_MODE"] = "hang"

        async def go():
            app = AppServer(str(FAKE), _TMP)
            try:
                with self.assertRaises(TimeoutError):
                    await app.turn("p", DECISION_SCHEMA, timeout=0.5)
            finally:
                await app.close()
            return app

        app = run(go())
        self.assertFalse(app.alive)

    def test_server_requests_are_refused(self):
        os.environ["FAKE_MODE"] = "ask"

        async def go():
            app = AppServer(str(FAKE), _TMP)
            res = await app.turn("p", DECISION_SCHEMA, timeout=5)
            await app.close()
            return res

        res = run(go())
        self.assertEqual(json.loads(res.text)["action"], "nothing")
        reply = next(m for m in self.sent() if m.get("id") == 777)
        self.assertIn("error", reply)  # no approval, ever

    def test_error_notification_fails_the_turn(self):
        os.environ["FAKE_MODE"] = "fail"

        async def go():
            app = AppServer(str(FAKE), _TMP)
            try:
                with self.assertRaises(AppServerError) as cm:
                    await app.turn("p", DECISION_SCHEMA, timeout=5)
            finally:
                await app.close()
            return str(cm.exception)

        self.assertIn("usage limit", run(go()))

    def test_auth_error_fails_fast_even_while_codex_retries(self):
        os.environ["FAKE_MODE"] = "auth"

        async def go():
            app = AppServer(str(FAKE), _TMP)
            try:
                with self.assertRaises(AppServerAuthError):
                    await app.turn("p", DECISION_SCHEMA, timeout=10)
            finally:
                await app.close()

        import time as _t
        t0 = _t.monotonic()
        run(go())
        self.assertLess(_t.monotonic() - t0, 5)  # not the minute of retries

    def test_rate_limit_with_retry_fails_fast(self):
        # a 429 marked willRetry used to keep the turn waiting up to 240 s
        os.environ["FAKE_MODE"] = "limit"

        async def go():
            app = AppServer(str(FAKE), _TMP)
            try:
                with self.assertRaises(AppServerFatal) as cm:
                    await app.turn("p", DECISION_SCHEMA, timeout=10)
            finally:
                await app.close()
            return cm.exception.reason

        import time as _t
        t0 = _t.monotonic()
        self.assertEqual(run(go()), "limit")
        self.assertLess(_t.monotonic() - t0, 5)

    def test_idle_process_is_closed(self):
        async def go():
            app = AppServer(str(FAKE), _TMP, idle_ttl=0.3)
            await app.turn("p", DECISION_SCHEMA, timeout=5)
            alive_after_turn = app.alive
            await asyncio.sleep(0.8)
            return alive_after_turn, app.alive

        self.assertEqual(run(go()), (True, False))


class DJFallback(unittest.TestCase):
    """CodexDJ: app-server failure → `codex exec` path, and telemetry says how."""

    def setUp(self):
        import test_dj_apply  # noqa: F401 — its import sets YTDJ_CODEX_APP_SERVER=0; do it before we override

        self._env = os.environ.get("YTDJ_CODEX_APP_SERVER")
        os.environ["YTDJ_CODEX_APP_SERVER"] = "1"

    def tearDown(self):
        os.environ["YTDJ_CODEX_APP_SERVER"] = self._env or "0"

    def test_falls_back_to_exec(self):
        from test_dj_apply import make

        os.environ["FAKE_MODE"] = "crash"
        os.environ["FAKE_LOG"] = str(Path(_TMP) / "x.jsonl")
        dj, _, _ = make()
        dj.app = AppServer(str(FAKE), _TMP)
        exec_calls = []

        async def fake_run(args, prompt):
            exec_calls.append(args)
            return {"action": "nothing", "reply": "from exec"}

        dj._run = fake_run
        intent = run(dj.interpret("ahoj"))
        self.assertEqual(intent.reply, "from exec")
        self.assertEqual(len(exec_calls), 1)
        events = [json.loads(x) for x in Path(os.environ["YTDJ_EVENTS_FILE"]).read_text().splitlines()]
        turns = [e for e in events if e["kind"] == "dj.turn"]
        self.assertEqual([(e["how"], e.get("ok")) for e in turns[-2:]],
                         [("app_server", False), ("exec", True)])

    def test_app_server_answers(self):
        from test_dj_apply import make

        os.environ["FAKE_MODE"] = "ok"
        os.environ["FAKE_LOG"] = str(Path(_TMP) / "y.jsonl")
        dj, _, _ = make()
        dj.app = AppServer(str(FAKE), _TMP)

        async def no_exec(args, prompt):
            raise AssertionError("exec must not run")

        dj._run = no_exec

        async def go():
            try:
                return await dj.interpret("ahoj")
            finally:
                await dj.close()

        self.assertIn("turn 1", run(go()).reply)


class NativeBinary(unittest.TestCase):
    def test_finds_vendor_binary_next_to_npm_wrapper(self):
        root = Path(tempfile.mkdtemp(dir=_TMP)) / "@openai/codex"
        (root / "bin").mkdir(parents=True)
        js = root / "bin/codex.js"
        js.write_text("")
        native = root / "node_modules/@openai/codex-linux-arm64/vendor/aarch64/bin/codex"
        native.parent.mkdir(parents=True)
        native.write_text("")
        link = Path(tempfile.mkdtemp(dir=_TMP)) / "codex"
        link.symlink_to(js)
        self.assertEqual(native_codex(str(link)), str(native))
        self.assertIsNone(native_codex(str(Path(_TMP) / "nothing")))


if __name__ == "__main__":
    unittest.main()
