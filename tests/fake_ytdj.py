"""A tiny stand-in for ytdj's web API, for trying the panel without mpv.

    python tests/fake_ytdj.py [--port 8765]

Serves /api/status, /api/events (SSE, a snapshot per second when it changed,
a "ping" event otherwise), /api/control with the same validation as the real
server, /api/prompt (a DJ turn that takes `prompt_delay` seconds, 409 while
another one runs) and the web UI itself on /, so the page can be tried
without a Pi. Playback is simulated: the position advances while playing and
the next track comes on when one ends (or on "next").

POST /fake/state {"idle": true, "busy": true, "paused": true, "prompt_delay": 2,
"prompt_status": 500, "sse": false} flips the fake into states that are hard
to reach on the real thing.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

INDEX = Path(__file__).resolve().parents[1] / "ytdj" / "web" / "static" / "index.html"

TRACKS = [
    {"id": "a1", "title": "Holky z naší školky", "artist": "Olympic", "album": None, "duration": 214},
    {
        "id": "a2",
        "title": "Příliš žluťoučký kůň úpěl ďábelské ódy — živě ze Šťastného Žďáru (remaster 2024)",
        "artist": "Žďárští řezníci & Ústečtí ďáblové",
        "album": None,
        "duration": 5400,
    },
    {"id": "a3", "title": "Ruty šuty", "artist": "Lucie", "album": None, "duration": 188},
]


class FakeYtdj:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.index = 0
        self.paused = False
        self.volume = 65
        self.pos = 12.0
        self.pos_at = time.monotonic()
        self.mood = "klidný večer, český rock"
        self.busy = False
        self.idle = False  # nothing loaded (after a restart, after Stop)
        self.controls: list[tuple[str, object]] = []  # what the panel sent
        # the DJ
        self.prompt_delay = 2.0
        self.prompt_status = 200  # anything else: the turn fails with that status
        self.prompts: list[dict] = []  # bodies of POST /api/prompt
        self.dj_text = ""
        self.dj_source = ""
        self.last: dict | None = None

    def _position(self) -> float:
        if self.paused:
            return self.pos
        return self.pos + time.monotonic() - self.pos_at

    def _advance(self) -> None:
        cur = TRACKS[self.index]
        if self._position() >= cur["duration"]:
            self._next()

    def _next(self) -> None:
        self.index = (self.index + 1) % len(TRACKS)
        self.pos, self.pos_at = 0.0, time.monotonic()

    def snapshot(self) -> dict:
        with self.lock:
            if self.idle:
                return {
                    "playing": False, "paused": False, "buffering": False, "current": None,
                    "position": 0.0, "duration": 0.0, "queue": [], "pools": "",
                    "volume": self.volume, "quality": "", "mood": "", "busy": self.busy,
                    "history": [], "dj": self._dj(),
                }
            self._advance()
            cur = TRACKS[self.index]
            return {
                "playing": not self.paused,
                "paused": self.paused,
                "buffering": False,
                "current": cur,
                "position": round(self._position(), 3),
                "duration": float(cur["duration"]),
                "queue": [TRACKS[(self.index + i) % len(TRACKS)] for i in (1, 2)],
                "pools": "",
                "volume": self.volume,
                "quality": "opus 251 kb/s",
                "mood": self.mood,
                "busy": self.busy,
                "history": [
                    {"artist": "Lucie", "title": "Amerika", "outcome": "finished"},
                    {"artist": "Kabát", "title": "Pohoda", "outcome": "skipped"},
                ],
                "dj": self._dj(),
            }

    def _dj(self) -> dict:
        return {
            "busy": self.busy,
            "text": self.dj_text if self.busy else "",
            "source": self.dj_source if self.busy else "",
            "last": self.last,
        }

    def prompt(self, data: dict) -> tuple[int, dict]:
        text = str(data.get("text") or "").strip()
        if not text:
            return 400, {"error": "Chybí text požadavku."}
        with self.lock:
            if self.busy:
                return 409, {"error": "Codex právě pracuje"}
            self.busy, self.dj_text = True, text
            self.dj_source = str(data.get("source") or "web")
            self.prompts.append(dict(data))
        time.sleep(self.prompt_delay)
        with self.lock:
            self.busy = False
            status = self.prompt_status
            if status != 200:
                self.last = {"text": text, "reply": "", "ok": False, "source": self.dj_source, "at": time.time()}
                return status, {"error": "Codex selhal: timeout"}
            reply = f"Jasně — pouštím: {text}. Nejdřív Olympic, pak podobné české kytary."
            self.last = {"text": text, "reply": reply, "ok": True, "source": self.dj_source, "at": time.time()}
            self.idle = False
            return 200, {"reply": reply}

    def control(self, data: dict) -> tuple[int, dict]:
        action, value = data.get("action"), data.get("value")
        with self.lock:
            self.controls.append((action, value))
            if action in ("play", "pause"):
                want = action == "pause"
                if want != self.paused:
                    self.pos, self.pos_at = self._position(), time.monotonic()
                    self.paused = want
            elif action == "next":
                self._next()
            elif action == "stop":
                self.idle = True
            elif action == "volume":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    return 400, {"error": "Hlasitost musí být číslo 0–130."}
                if not 0 <= int(value) <= 130:
                    return 400, {"error": "Hlasitost musí být v rozsahu 0–130."}
                self.volume = int(value)
            else:
                return 400, {"error": f"Neznámý povel: {action!r}"}
        return 200, {"ok": True}


def make_server(port: int = 0, fake: FakeYtdj | None = None, sse: bool = True) -> tuple[ThreadingHTTPServer, FakeYtdj]:
    fake = fake or FakeYtdj()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:  # quiet
            pass

        def _json(self, status: int, body: dict) -> None:
            raw = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            if self.path == "/api/status":
                self._json(200, fake.snapshot())
            elif self.path == "/api/events" and sse and getattr(fake, "sse", True):
                self._events()
            elif self.path == "/" and INDEX.is_file():
                raw = INDEX.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            elif self.path == "/api/about":
                self._json(200, {"youtube": {"cookies": "soubor", "library": "přihlášen", "pot": "běží",
                                             "quality": "opus 251 kb/s"},
                                 "backend": {"engine": "codex CLI", "model": "výchozí"}, "restartable": False})
            elif self.path == "/api/config":
                self._json(200, {"values": {}, "fields": []})
            else:
                self._json(404, {"error": "Nenalezeno."})

        def _events(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            last, last_sent = None, 0.0

            def chunk(text: str) -> None:
                raw = text.encode()
                self.wfile.write(f"{len(raw):x}\r\n".encode() + raw + b"\r\n")
                self.wfile.flush()

            try:
                while not getattr(self.server, "closing", False):
                    payload = json.dumps(fake.snapshot(), ensure_ascii=False)
                    now = time.monotonic()
                    if payload != last:
                        last, last_sent = payload, now
                        chunk(f"data: {payload}\n\n")
                    elif now - last_sent >= 15:
                        last_sent = now
                        chunk("event: ping\ndata: 1\n\n")
                    time.sleep(1.0)
                self.wfile.write(b"0\r\n\r\n")
            except OSError:
                pass

        def do_POST(self) -> None:
            if self.path not in ("/api/control", "/api/prompt", "/fake/state"):
                self._json(404, {"error": "Nenalezeno."})
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._json(400, {"error": "Tělo požadavku není platný JSON."})
                return
            if self.path == "/api/prompt":
                self._json(*fake.prompt(data))
            elif self.path == "/fake/state":
                with fake.lock:
                    for key in ("idle", "busy", "paused", "prompt_delay", "prompt_status", "sse", "dj_text", "mood"):
                        if key in data:
                            setattr(fake, key, data[key])
                self._json(200, {"ok": True})
            else:
                self._json(*fake.control(data))

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server, fake


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8765)
    args = p.parse_args()
    server, _ = make_server(args.port)
    print(f"fake ytdj on http://127.0.0.1:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
