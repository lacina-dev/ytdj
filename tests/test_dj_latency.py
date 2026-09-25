"""DJ latency: don't wait for Codex to exit, resolve seeds in parallel.

    python -m unittest tests.test_dj_latency -v

On the Pi (25 Sep) Codex kept running 5–8 s after `turn.completed`, and the
seeds were looked up one after another (10–12 s between the turn and the reply).
"""

from __future__ import annotations

import asyncio
import json
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

from test_dj_apply import T, make  # noqa: E402

DECISION = {"action": "nothing", "seeds": [], "requested": [], "focus_artists": [],
            "after_current": False, "avoid": [], "mood": "", "volume": 0,
            "remember": "", "reply": "ok"}

# a fake `codex exec`: answers, says turn.completed, then lingers like the real one
LINGERING = f"""
import json, sys, time
print(json.dumps({{"type": "thread.started", "thread_id": "t1"}}))
print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message",
      "text": {json.dumps(json.dumps(DECISION))}}}}}))
print(json.dumps({{"type": "turn.completed", "usage": {{}}}}), flush=True)
time.sleep(3)
"""


class NoWaitForExit(unittest.TestCase):
    def test_returns_at_turn_completed(self):
        dj, _, _ = make()

        async def go():
            t0 = time.monotonic()
            data = await dj._run([sys.executable, "-c", LINGERING], "prompt")
            return data, time.monotonic() - t0

        data, took = asyncio.run(go())
        self.assertEqual(data["reply"], "ok")
        self.assertLess(took, 2.0)  # not the 3 s the process keeps running
        self.assertEqual(dj.thread_id, "t1")


class SlowCatalog:
    async def search_song(self, artist, title):
        await asyncio.sleep(0.3)
        return T(f"{artist}-{title}", artist, title)


class ParallelSeeds(unittest.TestCase):
    def test_five_seeds_take_one_lookup_time(self):
        dj, _, _ = make()
        dj.catalog = SlowCatalog()
        pairs = [("A", "a"), ("B", "b"), ("C", "c"), ("D", "d"), ("E", "e")]

        async def go():
            t0 = time.monotonic()
            found, missing = await dj._resolve_pairs(pairs)
            return found, missing, time.monotonic() - t0

        found, missing, took = asyncio.run(go())
        self.assertEqual([t.artist for t in found], ["A", "B", "C", "D", "E"])  # order kept
        self.assertEqual(missing, [])
        self.assertLess(took, 0.9)  # 5 × 0.3 s serially would be 1.5 s


if __name__ == "__main__":
    unittest.main()
