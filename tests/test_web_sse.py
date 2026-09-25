"""The web server's status stream: one snapshot per tick for all clients.

    .venv/bin/python -m unittest tests.test_web_sse -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if not os.environ.get("YTDJ_EVENTS_FILE"):
    _EVENTS_TMP = tempfile.TemporaryDirectory(prefix="ytdj-web-tests-")
    os.environ["YTDJ_EVENTS_FILE"] = str(Path(_EVENTS_TMP.name) / "events.jsonl")

from ytdj.web import server as web  # noqa: E402


class FakePlayer:
    def __init__(self) -> None:
        self.calls = 0
        self.position = 0.0
        self.paused = False

    async def status(self):
        self.calls += 1
        if not self.paused:
            self.position += 0.5
        track = SimpleNamespace(id="a1", title="Holky z naší školky", artist="Olympic", album=None, duration=214)
        return SimpleNamespace(playing=not self.paused, paused=self.paused, buffering=False, current=track,
                               position=self.position, duration=214.0, queue=[], volume=50, quality="")

    async def skip(self):
        pass


class FakeApp:
    def __init__(self) -> None:
        self.player = FakePlayer()
        self.pools = SimpleNamespace(describe=lambda: "", mood="")
        self.store = SimpleNamespace(recent_history=lambda n: [])
        self.codex_busy = False
        self.asked: list[str] = []

    async def ask(self, text: str) -> str:
        self.asked.append(text)
        await asyncio.sleep(0.2)
        return f"Hraju {text}."


def request(body: dict | None = None):
    async def is_disconnected():
        return False

    async def read_body():
        return json.dumps(body or {}).encode()

    return SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={"user-agent": "test"},
                           is_disconnected=is_disconnected, body=read_body, url=SimpleNamespace(path="/x"),
                           method="GET")


async def collect(srv: web.WebServer, seconds: float) -> list[str]:
    resp = await srv._events(request())
    out: list[str] = []

    async def pump():
        async for chunk in resp.body_iterator:
            out.append(chunk if isinstance(chunk, str) else chunk.decode())

    task = asyncio.create_task(pump())
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return out


class SseTest(unittest.TestCase):
    def test_one_snapshot_per_tick_for_many_clients(self):
        async def main():
            app = FakeApp()
            srv = web.WebServer(app)
            streams = await asyncio.gather(*(collect(srv, 3.2) for _ in range(8)))
            return app, streams

        app, streams = asyncio.run(main())
        # 8 clients × ~3 ticks used to be ~24+ snapshots (5 mpv calls each)
        self.assertLessEqual(app.player.calls, 6)
        for chunks in streams:
            data = [c for c in chunks if c.startswith("data: ")]
            self.assertGreaterEqual(len(data), 3)
            state = json.loads(data[-1][6:])
            self.assertEqual(state["current"]["title"], "Holky z naší školky")
            self.assertIn("dj", state)

    def test_paused_player_gets_visible_pings(self):
        async def main():
            app = FakeApp()
            app.player.paused = True
            srv = web.WebServer(app)
            old = web.KEEPALIVE
            web.KEEPALIVE = 1.0
            try:
                return await collect(srv, 2.6)
            finally:
                web.KEEPALIVE = old

        chunks = asyncio.run(main())
        self.assertTrue(chunks[0].startswith("data: "))
        self.assertIn(web.PING, chunks)  # an event the page sees, not a comment

    def test_wish_is_visible_to_everybody(self):
        async def main():
            app = FakeApp()
            srv = web.WebServer(app)
            watcher = asyncio.create_task(collect(srv, 1.5))
            await asyncio.sleep(0.1)
            resp = await srv._prompt(request({"text": "písničky od Olympicu", "source": "panel"}))
            chunks = await watcher
            after = json.loads((await srv._status(request())).body)
            return resp, chunks, after

        resp, chunks, after = asyncio.run(main())
        self.assertEqual(resp.status_code, 200)
        states = [json.loads(c[6:]) for c in chunks if c.startswith("data: ")]
        busy = [s for s in states if s["dj"]["busy"]]
        self.assertTrue(busy, "nobody saw the DJ working")
        self.assertEqual(busy[0]["dj"]["text"], "písničky od Olympicu")
        self.assertEqual(busy[0]["dj"]["source"], "panel")
        last = after["dj"]["last"]
        self.assertTrue(last["ok"])
        self.assertEqual(last["reply"], "Hraju písničky od Olympicu.")
        self.assertFalse(after["dj"]["busy"])

    def test_unknown_source_is_web(self):
        async def main():
            app = FakeApp()
            srv = web.WebServer(app)
            await srv._prompt(request({"text": "Kabát", "source": "<script>"}))
            return json.loads((await srv._status(request())).body)

        self.assertEqual(asyncio.run(main())["dj"]["last"]["source"], "web")


if __name__ == "__main__":
    unittest.main()
