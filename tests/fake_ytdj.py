"""A tiny stand-in for ytdj's web API, for trying the panel without mpv.

    python tests/fake_ytdj.py [--port 8765]

Serves /api/status, /api/events (SSE, a snapshot per second when it changed,
": ping" otherwise) and /api/control with the same validation as the real
server. Playback is simulated: the position advances while playing and the
next track comes on when one ends (or on "next").
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
        self.controls: list[tuple[str, object]] = []  # what the panel sent

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
            self._advance()
            cur = TRACKS[self.index]
            return {
                "playing": not self.paused,
                "paused": self.paused,
                "current": cur,
                "position": round(self._position(), 3),
                "duration": float(cur["duration"]),
                "queue": [TRACKS[(self.index + i) % len(TRACKS)] for i in (1, 2)],
                "pools": "",
                "volume": self.volume,
                "quality": "opus 251 kb/s",
                "mood": self.mood,
                "busy": self.busy,
                "history": [],
            }

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
            elif self.path == "/api/events" and sse:
                self._events()
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
                        chunk(": ping\n\n")
                    time.sleep(1.0)
                self.wfile.write(b"0\r\n\r\n")
            except OSError:
                pass

        def do_POST(self) -> None:
            if self.path != "/api/control":
                self._json(404, {"error": "Nenalezeno."})
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._json(400, {"error": "Tělo požadavku není platný JSON."})
                return
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
