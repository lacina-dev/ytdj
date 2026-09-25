"""A tiny stand-in for ytdj's web API, for trying the panel and the web UI without mpv.

    python tests/fake_ytdj.py [--port 8765] [--demo]

Serves /api/status, /api/events (SSE: the full state when anything but the
position changed, a tiny "pos" event otherwise, a "ping" when nothing moves),
/api/control with the same validation as the real server, the request queue
(POST /api/prompt → 202 + id + token, POST /api/requests/<id> to remove a
wish or put it next) and the web UI itself on /, so the page can be tried
without a Pi. A wish goes "thinking" → (after `prompt_delay`) "queued" with
the DJ's reply; the queued wishes play in order as tracks end (or on "next").
Clients that send neither "who" nor "wait" (the old panel and page) get the
old blocking answer.

POST /fake/state {"idle": true, "busy": true, "paused": true, "prompt_delay": 2,
"prompt_status": 500, "sse": false} flips the fake into states that are hard
to reach on the real thing; `--demo` starts with a few people's wishes queued.
"""

from __future__ import annotations

import argparse
import json
import secrets
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
STATE_CS = {"waiting": "čeká", "thinking": "DJ vybírá", "queued": "ve frontě", "playing": "hraje",
            "done": "hotovo", "notfound": "nenašel", "error": "chyba", "removed": "odebráno"}
ACTIVE = ("waiting", "thinking", "queued", "playing")


def eta(ahead: int) -> str:
    if ahead <= 0:
        return "hned po téhle"
    word = "skladbu" if ahead == 1 else "skladby" if ahead < 5 else "skladeb"
    return f"za ~{ahead} {word}"


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
        self.starting = False
        self.controls: list[tuple[str, object]] = []  # what the panel sent
        # the DJ
        self.prompt_delay = 2.0
        self.prompt_status = 200  # anything else: the turn fails with that status
        self.prompts: list[dict] = []  # bodies of POST /api/prompt
        self.dj_text = ""
        self.dj_source = ""
        self.last: dict | None = None
        # the request queue
        self.requests: list[dict] = []
        self.playing_req: dict | None = None  # the wish whose track plays now
        self.actions: list[dict] = []  # POST /api/requests/<id>
        self.build = "fake-1"  # the page reloads itself when this changes
        self.dj_offline = False  # the DJ's brain is down (Codex circuit breaker)
        self.outage = None  # {"reason": …} while YouTube / the network is down

    def _position(self) -> float:
        if self.paused:
            return self.pos
        return self.pos + time.monotonic() - self.pos_at

    def _advance(self) -> None:
        cur = self._current()
        if self._position() >= cur["duration"]:
            self._next()

    def _queued(self) -> list[dict]:
        order = sorted((r for r in self.requests if r["state"] == "queued"),
                       key=lambda r: (not r["play_next"], r["seq"]))
        return order

    def _current(self) -> dict:
        if self.playing_req is not None:
            return self.playing_req["track"]
        return TRACKS[self.index]

    def _next(self) -> None:
        if self.playing_req is not None:
            self.playing_req["state"] = "done"
            self.playing_req["done_at"] = time.time()
            self.playing_req = None
        queued = self._queued()
        if queued:
            self.playing_req = queued[0]
            queued[0]["state"] = "playing"
        else:
            self.index = (self.index + 1) % len(TRACKS)
        self.pos, self.pos_at = 0.0, time.monotonic()

    def _public(self, r: dict) -> dict:
        out = {k: r[k] for k in ("id", "who", "source", "text", "state", "reply", "play_next",
                                 "created", "summary", "kind")}
        out["state_cs"] = STATE_CS[r["state"]]
        out["n_tracks"], out["n_played"] = 1, int(r["state"] in ("playing", "done"))
        if r["state"] == "queued":
            ahead = self._queued().index(r)
            out["ahead"], out["eta"] = ahead, eta(ahead)
        return out

    def _requests(self) -> list[dict]:
        live = [r for r in self.requests if r["state"] in ACTIVE]
        rank = {"playing": 0, "queued": 1, "thinking": 2, "waiting": 3}
        live.sort(key=lambda r: (rank[r["state"]], self._queued().index(r) if r["state"] == "queued" else 0,
                                 r["seq"]))
        done = sorted((r for r in self.requests if r["state"] not in ACTIVE),
                      key=lambda r: -r.get("done_at", 0))[:6]
        return [self._public(r) for r in live + done]

    def snapshot(self) -> dict:
        with self.lock:
            if not self.idle:
                self._advance()  # first: the requests' states follow the track change
            busy = self.busy or self.starting or any(r["state"] == "thinking" for r in self.requests)
            base = {"requests": self._requests(), "starting": self.starting,
                    "dj_offline": self.dj_offline, "outage": self.outage,
                    "build": self.build, "version": self.build,
                    "people": sorted({r["who"] for r in self.requests if r["who"] != "displej"})}
            if self.idle:
                return {
                    **base,
                    "playing": False, "paused": False, "buffering": False, "current": None,
                    "position": 0.0, "duration": 0.0, "queue": [], "pools": "",
                    "volume": self.volume, "quality": "", "mood": "", "busy": busy,
                    "history": [], "dj": self._dj(busy),
                }
            cur = dict(self._current())
            if self.playing_req is not None:
                r = self.playing_req
                cur["reason"] = {"kind": "wish", "who": r["who"], "text": r["text"], "id": r["id"]}
            else:
                cur["reason"] = {"kind": "radio", "who": "", "text": self.mood}
            queue = []
            for r in self._queued():
                queue.append({**r["track"], "req": {"id": r["id"], "who": r["who"]}})
            queue += [TRACKS[(self.index + i) % len(TRACKS)] for i in (1, 2)]
            return {
                **base,
                "playing": not self.paused,
                "paused": self.paused,
                "buffering": False,
                "current": cur,
                "position": round(self._position(), 3),
                "duration": float(cur["duration"]),
                "queue": queue,
                "pools": "",
                "volume": self.volume,
                "quality": "opus 251 kb/s",
                "mood": self.mood,
                "busy": busy,
                "history": [
                    {"artist": "Lucie", "title": "Amerika", "outcome": "finished"},
                    {"artist": "Kabát", "title": "Pohoda", "outcome": "skipped"},
                ],
                "dj": self._dj(busy),
            }

    def _dj(self, busy: bool) -> dict:
        thinking = next((r for r in self.requests if r["state"] == "thinking"), None)
        text = thinking["text"] if thinking else self.dj_text
        source = thinking["source"] if thinking else self.dj_source
        return {"busy": busy, "text": text if busy else "", "source": source if busy else "",
                "last": self.last}

    # ---- the request queue ----

    def add_request(self, text: str, who: str, source: str = "web", play_next: bool = False,
                    state: str = "thinking", reply: str = "") -> dict:
        r = {
            "id": secrets.token_hex(4), "token": secrets.token_urlsafe(9), "who": who,
            "source": source, "text": text, "state": state, "reply": reply,
            "play_next": play_next, "created": time.time(), "seq": len(self.requests),
            "summary": "", "kind": "",
            "track": {"id": f"w{len(self.requests)}", "title": text[:60].capitalize(),
                      "artist": "vybral DJ", "album": None, "duration": 200},
        }
        self.requests.append(r)
        return r

    def _decide(self, r: dict) -> None:
        time.sleep(self.prompt_delay)
        with self.lock:
            if r["state"] != "thinking":
                return
            if self.prompt_status != 200:
                r["state"], r["reply"] = "error", "DJ narazil na chybu: Codex selhal: timeout"
                r["done_at"] = time.time()
                return
            r["state"] = "queued"
            r["kind"], r["summary"] = "songs", "skladba"
            ahead = self._queued().index(r)
            r["reply"] = f"Jasně — pouštím: {r['text']}. Na řadě {eta(ahead)}."
            if self.idle:
                self.idle = False
                self.playing_req = None
                self._next()

    def prompt(self, data: dict) -> tuple[int, dict]:
        text = str(data.get("text") or "").strip()
        if not text:
            return 400, {"error": "Chybí text požadavku."}
        source = str(data.get("source") or "web")
        legacy = "who" not in data and "wait" not in data
        with self.lock:
            if legacy and self.busy:
                return 409, {"error": "Codex právě pracuje"}
            self.prompts.append(dict(data))
            who = str(data.get("who") or "").strip() or {"panel": "displej"}.get(source, "host")
            r = self.add_request(text, who, source, play_next=data.get("play_next") is True)
        if not legacy and data.get("wait") is not True:
            threading.Thread(target=self._decide, args=(r,), daemon=True).start()
            with self.lock:
                pub = self._public(r)
            return 202, {"id": r["id"], "token": r["token"], "who": who, "state": r["state"],
                         "request": pub, "reply": ""}
        # the old clients: block until the DJ decided, like before
        self._decide(r)
        with self.lock:
            ok = r["state"] != "error"
            self.last = {"text": text, "reply": r["reply"], "ok": ok, "source": source,
                         "at": time.time()}
            if not ok:
                return self.prompt_status, {"error": "Codex selhal: timeout"}
            return 200, {"reply": r["reply"], "id": r["id"], "state": r["state"]}

    def request_action(self, rid: str, data: dict) -> tuple[int, dict]:
        with self.lock:
            self.actions.append({"id": rid, **data})
            r = next((x for x in self.requests if x["id"] == rid), None)
            if r is None:
                return 404, {"error": "Takové přání neznám."}
            if data.get("token") != r["token"]:
                return 403, {"error": "Tohle přání ti nepatří."}
            if r["state"] not in ACTIVE:
                return 409, {"error": "Přání už je vyřízené."}
            if data.get("action") == "remove":
                r["state"], r["done_at"] = "removed", time.time()
                if r is self.playing_req:
                    self.playing_req = None
                return 200, {"ok": True, "message": "Odebráno."}
            if data.get("action") == "next":
                r["play_next"] = True
                return 200, {"ok": True, "message": "Hraje hned po téhle skladbě."}
        return 400, {"error": "Neznámá akce."}

    def control(self, data: dict) -> tuple[int, dict]:
        action, value = data.get("action"), data.get("value")
        with self.lock:
            self.controls.append((action, value))
            if action == "play" and self.idle:
                # nothing to un-pause: the DJ starts by the time of day
                self.starting = True
                threading.Thread(target=self._start, daemon=True).start()
                return 200, {"ok": True, "starting": True}
            if action in ("play", "pause"):
                want = action == "pause"
                if want != self.paused:
                    self.pos, self.pos_at = self._position(), time.monotonic()
                    self.paused = want
            elif action == "next":
                self._next()
            elif action == "stop":
                # like the real server: only a pause, nobody's wishes are removed
                if not self.paused:
                    self.pos, self.pos_at = self._position(), time.monotonic()
                    self.paused = True
            elif action == "volume":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    return 400, {"error": "Hlasitost musí být číslo 0–100."}
                if not 0 <= int(value) <= 130:
                    return 400, {"error": "Hlasitost musí být v rozsahu 0–100."}
                self.volume = min(100, int(value))  # strop 100 jako skutečný server
            else:
                return 400, {"error": f"Neznámý povel: {action!r}"}
        return 200, {"ok": True}

    def _start(self) -> None:
        time.sleep(min(self.prompt_delay, 1.0))
        with self.lock:
            self.starting = False
            self.idle = False
            self.paused = False
            self.mood = "ranní klid, známé české i zahraniční"
            self.pos, self.pos_at = 0.0, time.monotonic()

    def demo(self) -> None:
        """A few people's wishes, as in the office on a busy afternoon."""
        with self.lock:
            p = self.add_request("písničky od Kabátu", "Petr", state="playing",
                                 reply="Hraju Kabát — po 3 skladbách, střídám s ostatními.")
            p["track"] = {"id": "k1", "title": "Malá dáma", "artist": "Kabát", "album": None,
                          "duration": 231}
            self.playing_req = p
            j = self.add_request("Holky z naší školky", "Jana", state="queued",
                                 reply="Zařazuju Holky z naší školky. Na řadě hned po téhle.")
            j["track"] = {"id": "o1", "title": "Holky z naší školky", "artist": "Olympic",
                          "album": None, "duration": 214}
            k = self.add_request("něco klidnějšího na odpoledne", "Karel", state="queued",
                                 reply="Zklidním to — akustický pop. Na řadě za ~1 skladbu.")
            k["track"] = {"id": "c1", "title": "Tichá noc v Praze", "artist": "Calm Trio",
                          "album": None, "duration": 199}
            self.add_request("Dancing Queen", "displej", source="panel", state="thinking")
            d = self.add_request("Jasná zpráva", "Jana", state="done",
                                 reply="Zařazuju Jasnou zprávu.")
            d["done_at"] = time.time() - 60
            self.pos, self.pos_at = 47.0, time.monotonic()


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
            elif self.path == "/api/requests":
                self._json(200, {"requests": fake.snapshot().get("requests", [])})
            else:
                self._json(404, {"error": "Nenalezeno."})

        def _events(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            last_sig, last_pos, last_sent = None, None, 0.0

            def chunk(text: str) -> None:
                raw = text.encode()
                self.wfile.write(f"{len(raw):x}\r\n".encode() + raw + b"\r\n")
                self.wfile.flush()

            try:
                while not getattr(self.server, "closing", False):
                    snap = fake.snapshot()
                    pos = round(float(snap.get("position") or 0), 1)
                    sig = json.dumps({k: v for k, v in snap.items() if k != "position"}, ensure_ascii=False)
                    now = time.monotonic()
                    if sig != last_sig:
                        last_sig, last_pos, last_sent = sig, pos, now
                        chunk(f"data: {json.dumps(snap, ensure_ascii=False)}\n\n")
                    elif pos != last_pos:
                        last_pos, last_sent = pos, now
                        chunk(f"event: pos\ndata: [{pos}]\n\n")
                    elif now - last_sent >= 15:
                        last_sent = now
                        chunk("event: ping\ndata: 1\n\n")
                    time.sleep(0.5)
                self.wfile.write(b"0\r\n\r\n")
            except OSError:
                pass

        def do_DELETE(self) -> None:
            if self.path.startswith("/api/requests/"):
                data = self._body()
                if data is not None:
                    self._json(*fake.request_action(self.path.rsplit("/", 1)[-1],
                                                    {**data, "action": "remove"}))
            else:
                self._json(404, {"error": "Nenalezeno."})

        def _body(self) -> dict | None:
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._json(400, {"error": "Tělo požadavku není platný JSON."})
                return None
            return data if isinstance(data, dict) else {}

        def do_POST(self) -> None:
            if self.path.startswith("/api/requests/"):
                data = self._body()
                if data is not None:
                    self._json(*fake.request_action(self.path.rsplit("/", 1)[-1], data))
                return
            if self.path not in ("/api/control", "/api/prompt", "/fake/state"):
                self._json(404, {"error": "Nenalezeno."})
                return
            data = self._body()
            if data is None:
                return
            if self.path == "/api/prompt":
                self._json(*fake.prompt(data))
            elif self.path == "/fake/state":
                with fake.lock:
                    for key in ("idle", "busy", "paused", "prompt_delay", "prompt_status", "sse", "dj_text", "mood", "build",
                                "dj_offline", "outage"):
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
    p.add_argument("--demo", action="store_true", help="start with a few people's wishes queued")
    args = p.parse_args()
    server, fake = make_server(args.port)
    if args.demo:
        fake.demo()
    print(f"fake ytdj on http://127.0.0.1:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
