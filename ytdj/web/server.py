"""Local web interface on top of a running ytdj instance.

Runs in the same asyncio loop as the player — uvicorn is started as a task,
not via `uvicorn.run()`. Nothing here may block the loop: everything that
touches mpv or Codex is awaited, and a Codex query (tens of seconds) holds
only its own `busy` flag, not a lock over the whole server.

Listens exclusively on 127.0.0.1 and has no authentication — it controls
your own player, it is not a public service.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shlex
import socket
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import uvicorn

from .. import config as cfgmod

if TYPE_CHECKING:  # circular import — we pull in App for typing only
    from ..__main__ import App

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
INDEX_FILE = STATIC_DIR / "index.html"

# how often the state is recomputed for SSE and how long silence may last
TICK = 1.0
KEEPALIVE = 15.0

NO_INDEX_HTML = """<!doctype html><meta charset="utf-8">
<title>ytdj</title>
<body style="font:16px/1.5 system-ui;margin:3rem auto;max-width:32rem">
<h1>Frontend zatím chybí</h1>
<p>Soubor <code>ytdj/web/static/index.html</code> neexistuje, takže není co
zobrazit. Samotné API na <code>/api/…</code> ale funguje.</p>
"""


# --------------------------------------------------------------------------
# settings description for the web interface
# --------------------------------------------------------------------------

# Keys that take effect only after a restart (both mpv and ytmusicapi are
# constructed at startup).
RESTART_KEYS = frozenset(
    {
        # web se zvedá jednou při startu, takže adresa ani port za běhu nejdou
        "web_enabled",
        "web_host",
        "web_port",
        "language",
        "location",
        "ytdl_format",
        "cookies_browser",
        "cookies_file",
        "player_client",
        "js_runtimes",
        "remote_components",
        "mpv_extra_args",
    }
)

# Keys the settings form doesn't show. Volume has its own slider next to the
# controls; a second field for it would be a second source of truth.
HIDDEN_KEYS = frozenset({"volume"})

# Keys we can switch at runtime — they are also set directly on app.cfg.
LIVE_KEYS = (
    "codex_model",
    "queue_target",
    "queue_low",
    "pool_low",
    "radio_limit",
    "min_duration",
    "max_duration",
    "repeat_days",
    "artist_window",
)

CODEX_MODELS = [
    "",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.5",
    "gpt-5.6-luna",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
]

# key -> (label, help text, range for numbers)
FIELD_META: dict[str, tuple[str, str, tuple[int, int] | None]] = {
    "codex_model": (
        "Model Codexu",
        "Prázdné = nechat výchozí model Codexu; mini varianty odpovídají rychleji.",
        None,
    ),
    "language": (
        "Jazyk katalogu",
        "Jazyk, ve kterém YouTube Music vrací názvy a popisky (např. cs).",
        None,
    ),
    "location": (
        "Země katalogu",
        "Dvoupísmenný kód země, podle které se vybírají dostupné skladby (např. CZ).",
        None,
    ),
    "queue_target": (
        "Cílová hloubka fronty",
        "Kolik skladeb držet nachystaných za tou právě hrající.",
        (1, 50),
    ),
    "queue_low": (
        "Práh pro doplnění fronty",
        "Klesne-li fronta pod tenhle počet, začne se dolévat z poolů.",
        (0, 50),
    ),
    "pool_low": (
        "Práh pro doplnění poolu",
        "Když v poolu zbývá míň skladeb, dotáhne se z rádia další dávka.",
        (0, 500),
    ),
    "radio_limit": (
        "Velikost rádia ze seedu",
        "Kolik skladeb si vyžádat z rádia jedné seed skladby.",
        (1, 200),
    ),
    "min_duration": (
        "Minimální délka skladby (s)",
        "Kratší skladby se do fronty nepustí — odfiltruje to znělky a skeče.",
        (0, 3600),
    ),
    "max_duration": (
        "Maximální délka skladby (s)",
        "Delší skladby se přeskočí — typicky hodinové mixy a livesety.",
        (10, 7200),
    ),
    "repeat_days": (
        "Neopakovat N dní",
        "Skladba, která hrála během posledních N dní, se znovu nenabídne.",
        (0, 3650),
    ),
    "artist_window": (
        "Okno pro interpreta",
        "Na kolika po sobě jdoucích skladbách hlídat, aby se interpret neopakoval.",
        (0, 100),
    ),
    "ytdl_format": (
        "Preference formátů (yt-dlp)",
        "Pořadí formátů pro mpv: 774 a 141 jsou Premium, 251 a 140 běžné.",
        None,
    ),
    "cookies_browser": (
        "Cookies z prohlížeče",
        "Profil, ze kterého yt-dlp bere přihlášení; bez cookies hraje jen 128 kb/s.",
        None,
    ),
    "cookies_file": (
        "Cookies ze souboru",
        "Cesta k vyexportovanému cookies.txt; má přednost před prohlížečem a "
        "jako jediné funguje bez přihlášené plochy.",
        None,
    ),
    "player_client": (
        "Klient yt-dlp",
        "Prázdné = výběr nechat na yt-dlp (anonymní klienti, bez Premium). "
        "Klient s přihlášením, např. web_music, potřebuje PO token.",
        None,
    ),
    "js_runtimes": (
        "JS runtime pro yt-dlp",
        "Bez funkčního runtime (node) neprojde řešení signatur a Premium formáty zmizí.",
        None,
    ),
    "remote_components": (
        "Vzdálené komponenty yt-dlp",
        "Odkud si yt-dlp stahuje pomocný JS, standardně ejs:github.",
        None,
    ),
    "mpv_extra_args": (
        "Další argumenty mpv",
        "Volitelné přepínače navíc, zapsané jako na příkazové řádce.",
        None,
    ),
}


def _cookie_choices(current: str) -> list[str]:
    """Choices for cookies_browser — empty, none and detected Chrome profiles."""
    choices = ["", "none"]
    try:
        profiles = cfgmod._chrome_profiles_with_youtube_login()
    except Exception:  # detection touches SQLite in the profile, must not kill the request
        log.debug("detekce Chrome profilů selhala", exc_info=True)
        profiles = []
    choices += [f"chrome:{p}" for p in profiles]
    if current and current not in choices:
        choices.append(current)
    return choices


def _field_type(key: str) -> str:
    if key in ("cookies_browser", "codex_model"):
        return "choice"
    default = cfgmod.DEFAULTS[key]
    if isinstance(default, bool):
        return "bool"
    if isinstance(default, int):
        return "int"
    return "str"


POT_PING = ("127.0.0.1", 4416)  # výchozí adresa bgutil provideru


async def _pot_status() -> str:
    """Běží razítko PO tokenů? Bez něj nejsou Premium formáty."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(*POT_PING), timeout=1.0
        )
    except (OSError, asyncio.TimeoutError):
        return "neběží"
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return "běží"


class BadValue(ValueError):
    """Invalid value in POST /api/config — returned as a 400."""


def coerce_value(key: str, raw: Any) -> Any:
    """Validates and converts one value according to its type in DEFAULTS."""
    if key not in cfgmod.DEFAULTS:
        raise BadValue(f"Neznámý klíč nastavení: {key}")
    if key in HIDDEN_KEYS:
        # Zapisuje ho přehrávač, když se hlasitost změní. Kdyby šla i tudy,
        # přepsala by se hodnota, kterou zrovna drží mpv.
        raise BadValue(f"{key} se nastavuje přehrávačem (POST /api/control)")
    default = cfgmod.DEFAULTS[key]
    label = FIELD_META.get(key, (key,))[0]

    if isinstance(default, bool):
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.lower() in ("true", "false", "1", "0"):
            return raw.lower() in ("true", "1")
        raise BadValue(f"{label}: očekávám ano/ne, přišlo {raw!r}")

    if isinstance(default, int):
        if isinstance(raw, bool):
            raise BadValue(f"{label}: očekávám celé číslo, přišlo {raw!r}")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise BadValue(f"{label}: očekávám celé číslo, přišlo {raw!r}") from None
        bounds = FIELD_META.get(key, (None, None, None))[2]
        if bounds and not (bounds[0] <= value <= bounds[1]):
            raise BadValue(
                f"{label}: hodnota musí být mezi {bounds[0]} a {bounds[1]}, "
                f"přišlo {value}"
            )
        return value

    if isinstance(default, list):
        if isinstance(raw, str):
            try:
                return shlex.split(raw)
            except ValueError as exc:
                raise BadValue(f"{label}: nejde rozdělit na argumenty ({exc})") from None
        if isinstance(raw, list) and all(isinstance(v, str) for v in raw):
            return list(raw)
        raise BadValue(f"{label}: očekávám seznam textů nebo řádek argumentů")

    if not isinstance(raw, str):
        raise BadValue(f"{label}: očekávám text, přišlo {raw!r}")
    return raw


# --------------------------------------------------------------------------
# server
# --------------------------------------------------------------------------


def _track_dict(track: Any) -> dict | None:
    if track is None:
        return None
    return {
        "id": getattr(track, "id", ""),
        "title": getattr(track, "title", ""),
        "artist": getattr(track, "artist", ""),
        "album": getattr(track, "album", None),
        "duration": getattr(track, "duration", None),
    }


def _json_error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _safe(handler: Callable) -> Callable:
    """A single request crashing must not take down the server or the loop."""

    async def wrapper(request: Request) -> Response:
        try:
            return await handler(request)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("požadavek %s %s selhal", request.method, request.url.path)
            return _json_error("Vnitřní chyba serveru — podrobnosti v logu.", 500)

    wrapper.__name__ = getattr(handler, "__name__", "handler")
    return wrapper


class WebServer:
    """Web control interface on top of a `ytdj.__main__.App` instance."""

    def __init__(self, app: "App", host: str = "127.0.0.1", port: int = 8765) -> None:
        self.app = app
        self.host = host
        self.port = port

        # true for the duration of a Codex turn — /api/status and SSE pass it on
        self.busy = False

        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None
        self._sock: socket.socket | None = None
        self._closing = asyncio.Event()
        self._starlette = self._build()

    # ---- routes ----

    def _build(self) -> Starlette:
        routes = [
            Route("/", _safe(self._index), methods=["GET"]),
            Route("/api/status", _safe(self._status), methods=["GET"]),
            Route("/api/events", _safe(self._events), methods=["GET"]),
            Route("/api/prompt", _safe(self._prompt), methods=["POST"]),
            Route("/api/control", _safe(self._control), methods=["POST"]),
            Route("/api/config", _safe(self._config_get), methods=["GET"]),
            Route("/api/config", _safe(self._config_post), methods=["POST"]),
            Mount(
                "/static",
                StaticFiles(directory=str(STATIC_DIR), check_dir=False),
                name="static",
            ),
            # the frontend may also reach for /app.js — we serve it from the same folder
            Route("/{path:path}", _safe(self._static_fallback), methods=["GET"]),
        ]
        return Starlette(routes=routes)

    async def _index(self, request: Request) -> Response:
        if not INDEX_FILE.is_file():
            return HTMLResponse(NO_INDEX_HTML, status_code=503)
        return FileResponse(INDEX_FILE, headers={"Cache-Control": "no-cache"})

    async def _static_fallback(self, request: Request) -> Response:
        rel = request.path_params.get("path", "")
        target = (STATIC_DIR / rel).resolve()
        try:
            inside = target.is_relative_to(STATIC_DIR.resolve())
        except (OSError, ValueError):
            inside = False
        if not rel or not inside or not target.is_file():
            return _json_error("Nenalezeno.", 404)
        return FileResponse(target)

    # ---- state ----

    async def _snapshot(self) -> dict:
        """Complete state for both /api/status and SSE.

        Must not raise an exception just because mpv happens to be
        unresponsive — it rather returns an empty state so the frontend
        doesn't disconnect.
        """
        current = None
        queue: list[dict] = []
        playing = paused = False
        position = duration = 0.0
        volume = 100
        quality = ""
        try:
            st = await self.app.player.status()
            playing = bool(st.playing)
            paused = bool(st.paused)
            current = _track_dict(st.current)
            position = float(st.position or 0.0)
            duration = float(st.duration or 0.0)
            queue = [_track_dict(t) for t in st.queue]
            volume = int(st.volume)
            quality = st.quality
        except Exception:
            log.debug("stav přehrávače se nepodařilo přečíst", exc_info=True)

        try:
            pools = self.app.pools.describe()
            mood = self.app.pools.mood or ""
        except Exception:
            pools, mood = "", ""

        history: list[dict] = []
        try:
            for rec in self.app.store.recent_history(20):
                history.append(
                    {
                        "artist": rec.artist or "",
                        "title": rec.title or "",
                        "outcome": rec.outcome,
                    }
                )
        except Exception:
            log.debug("historii se nepodařilo přečíst", exc_info=True)

        return {
            "playing": playing,
            "paused": paused,
            "current": current,
            "position": position,
            "duration": duration,
            "queue": queue,
            "pools": pools,
            "volume": volume,
            "quality": quality,
            "mood": mood,
            "busy": self.busy,
            "history": history,
        }

    async def _status(self, request: Request) -> Response:
        return JSONResponse(await self._snapshot())

    async def _events(self, request: Request) -> Response:
        async def stream():
            last_payload: str | None = None
            last_sent = 0.0
            try:
                while not self._closing.is_set():
                    if await request.is_disconnected():
                        break
                    try:
                        payload = json.dumps(
                            await self._snapshot(), ensure_ascii=False
                        )
                    except Exception:
                        log.exception("snímek stavu pro SSE selhal")
                        payload = last_payload or "{}"

                    now = time.monotonic()
                    if payload != last_payload:
                        last_payload = payload
                        last_sent = now
                        yield f"data: {payload}\n\n"
                    elif now - last_sent >= KEEPALIVE:
                        last_sent = now
                        yield ": ping\n\n"

                    # wakes up earlier when the server is shutting down
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self._closing.wait(), timeout=TICK)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("SSE stream skončil chybou")

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ---- commands ----

    async def _body(self, request: Request) -> dict:
        raw = await request.body()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise BadValue("Tělo požadavku není platný JSON.") from None
        if not isinstance(data, dict):
            raise BadValue("Očekávám JSON objekt.")
        return data

    async def _prompt(self, request: Request) -> Response:
        try:
            data = await self._body(request)
        except BadValue as exc:
            return _json_error(str(exc), 400)

        text = (data.get("text") or "").strip()
        if not text:
            return _json_error("Chybí text požadavku.", 400)

        # Lock-free serialization: there is no await between the test and the
        # assignment, so two turns can never meet within a single loop.
        # On top of that, `app.ask()` shares a lock with the REPL and with
        # automatic reseeding, so two concurrent turns can't overwrite each
        # other's pools.
        if self.busy or getattr(self.app, "codex_busy", False):
            return _json_error("Codex právě pracuje", 409)
        self.busy = True
        try:
            ask = getattr(self.app, "ask", None)
            reply = await (ask(text) if ask else self.app.dj.turn(text))
        except Exception as exc:
            log.exception("tah Codexu selhal")
            return _json_error(f"Codex selhal: {exc}", 500)
        finally:
            self.busy = False
        return JSONResponse({"reply": reply or ""})

    async def _control(self, request: Request) -> Response:
        try:
            data = await self._body(request)
        except BadValue as exc:
            return _json_error(str(exc), 400)

        action = data.get("action")
        value = data.get("value")
        player = self.app.player

        try:
            if action == "play":
                await player.toggle_pause(False)
            elif action == "pause":
                await player.toggle_pause(True)
            elif action == "next":
                await player.skip()
            elif action == "stop":
                await player.clear_queue()
                await player.toggle_pause(True)
            elif action == "volume":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    return _json_error("Hlasitost musí být číslo 0–130.", 400)
                if not (0 <= int(value) <= 130):
                    return _json_error("Hlasitost musí být v rozsahu 0–130.", 400)
                await player.set_volume(int(value))
            else:
                return _json_error(f"Neznámý povel: {action!r}", 400)
        except Exception as exc:
            log.exception("povel %r selhal", action)
            return _json_error(f"Povel se nepodařilo provést: {exc}", 500)

        return JSONResponse({"ok": True})

    # ---- settings ----

    async def _config_get(self, request: Request) -> Response:
        cfg = self.app.cfg
        values: dict[str, Any] = {}
        fields: list[dict] = []

        for key in cfgmod.DEFAULTS:
            if key in HIDDEN_KEYS:
                continue
            value = getattr(cfg, key, cfgmod.DEFAULTS[key])
            if isinstance(value, list):
                # the form gets a single line of arguments, not an array
                value = shlex.join(str(v) for v in value)
            values[key] = value

            label, help_text, _ = FIELD_META.get(key, (key, "", None))
            field: dict[str, Any] = {
                "key": key,
                "label": label,
                "help": help_text,
                "type": _field_type(key),
                "restart": key in RESTART_KEYS,
            }
            if key == "cookies_browser":
                field["choices"] = _cookie_choices(str(values[key]))
            elif key == "codex_model":
                choices = list(CODEX_MODELS)
                if values[key] and values[key] not in choices:
                    choices.append(str(values[key]))
                field["choices"] = choices
            fields.append(field)

        return JSONResponse({"values": values, "fields": fields})

    async def _config_post(self, request: Request) -> Response:
        try:
            data = await self._body(request)
        except BadValue as exc:
            return _json_error(str(exc), 400)
        if not data:
            return JSONResponse({"ok": True, "restart_required": []})

        try:
            changes = {key: coerce_value(key, raw) for key, raw in data.items()}
        except BadValue as exc:
            return _json_error(str(exc), 400)

        try:
            await asyncio.to_thread(cfgmod.save_values, changes)
        except OSError as exc:
            log.exception("zápis konfigurace selhal")
            return _json_error(f"Nepodařilo se zapsat konfiguraci: {exc}", 500)

        restart: list[str] = []
        for key, value in changes.items():
            if key in LIVE_KEYS:
                setattr(self.app.cfg, key, value)
            elif getattr(self.app.cfg, key, None) != value:
                restart.append(key)

        return JSONResponse({"ok": True, "restart_required": restart})

    # ---- lifecycle ----

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def start(self) -> None:
        """Starts uvicorn as a task and returns once the port is listening."""
        if self._task is not None:
            return
        self._closing.clear()

        # We open the socket ourselves: a bind error (port already in use)
        # then arrives here as a plain OSError, not as a SystemExit inside the
        # task, where it would take down the whole loop, player included.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.host, self.port))
        except OSError:
            sock.close()
            raise
        sock.set_inheritable(True)
        self.port = sock.getsockname()[1]  # because of port 0 (test / random port)
        self._sock = sock

        config = uvicorn.Config(
            self._starlette,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
            lifespan="off",
            timeout_graceful_shutdown=3,
        )
        server = uvicorn.Server(config)
        # without this, uvicorn grabs SIGINT and Ctrl+C wouldn't reach the REPL
        server.capture_signals = contextlib.nullcontext  # type: ignore[assignment]
        self._server = server

        self._task = asyncio.create_task(server.serve(sockets=[sock]), name="ytdj-web")

        deadline = asyncio.get_running_loop().time() + 10
        while not server.started:
            if self._task.done():
                self._task.result()  # lets the original error through
                raise RuntimeError("webový server se nespustil")
            if asyncio.get_running_loop().time() > deadline:
                raise RuntimeError("webový server nenaběhl do 10 s")
            await asyncio.sleep(0.02)

        log.info("webové rozhraní běží na %s", self.url)

    async def stop(self) -> None:
        self._closing.set()  # SSE loops terminate on their own
        server, task = self._server, self._task
        self._server = self._task = None

        if server is not None:
            server.should_exit = True
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                if server is not None:
                    server.force_exit = True
                task.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await task
            except Exception:
                log.exception("webový server skončil chybou")

        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None


