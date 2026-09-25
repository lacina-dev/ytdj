"""A fake `codex app-server` (JSON-RPC over stdio) for tests.

Usage: fake_app_server.py app-server   (argv like the real binary)
Behaviour by env FAKE_MODE: ok | crash | hang | ask | fail
FAKE_LOG: file where every received message is appended (JSON lines).
"""

import json
import os
import sys
import time

MODE = os.environ.get("FAKE_MODE", "ok")
LOG = os.environ.get("FAKE_LOG")
DECISION = {"action": "nothing", "seeds": [], "requested": [], "focus_artists": [],
            "after_current": False, "avoid": [], "mood": "", "volume": 0,
            "remember": "", "reply": "ok"}
threads = 0
turns = 0


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    msg = json.loads(line)
    if LOG:
        with open(LOG, "a") as f:
            f.write(json.dumps(msg) + "\n")
    method, rid = msg.get("method"), msg.get("id")
    if method == "initialize":
        out({"id": rid, "result": {"userAgent": "fake"}})
    elif method == "thread/start":
        threads += 1
        out({"id": rid, "result": {"thread": {"id": f"thread-{threads}"}}})
    elif method == "turn/start":
        turns += 1
        tid = msg["params"]["threadId"]
        turn_id = f"turn-{turns}"
        out({"id": rid, "result": {"turn": {"id": turn_id, "status": "inProgress"}}})
        if MODE == "crash":
            sys.exit(3)
        if MODE == "hang":
            time.sleep(60)
        if MODE == "ask":
            # the server asks for approval; ytdj must refuse, then we go on
            out({"id": 777, "method": "item/commandExecution/requestApproval",
                 "params": {"threadId": tid}})
            reply = json.loads(sys.stdin.readline())
            if LOG:
                with open(LOG, "a") as f:
                    f.write(json.dumps(reply) + "\n")
        if MODE == "fail":
            out({"method": "error", "params": {"threadId": tid, "turnId": turn_id,
                                               "willRetry": False,
                                               "error": {"message": "usage limit"}}})
            continue
        d = dict(DECISION, reply=f"turn {turns} in {tid}")
        out({"method": "item/completed", "params": {
            "threadId": tid, "turnId": turn_id, "completedAtMs": 0,
            "item": {"type": "agentMessage", "id": "m", "text": json.dumps(d)}}})
        out({"method": "turn/completed", "params": {
            "threadId": tid, "turn": {"id": turn_id, "status": "completed"}}})
