"""Lokální webové rozhraní nad běžící instancí ytdj.

Běží ve stejné asyncio smyčce jako přehrávač — uvicorn se pouští jako úloha,
ne přes `uvicorn.run()`. Nic tady nesmí smyčku blokovat: všechno, co sahá na
mpv nebo na Codex, se awaituje, a dotaz na Codex (desítky sekund) drží jen
vlastní příznak `busy`, ne zámek nad celým serverem.

Poslouchá výhradně na 127.0.0.1 a nemá autentizaci — je to ovládání vlastního
přehrávače, ne veřejná služba.
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

if TYPE_CHECKING:  # kruhový import — App si taháme jen pro typy
    from ..__main__ import App

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
INDEX_FILE = STATIC_DIR / "index.html"

# jak často se přepočítává stav pro SSE a jak dlouho smí být ticho
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
# popis nastavení pro webové rozhraní
# --------------------------------------------------------------------------

# Klíče, které se projeví až po restartu (mpv i ytmusicapi se staví při startu).
RESTART_KEYS = frozenset(
    {
        "language",
        "location",
        "ytdl_format",
        "cookies_browser",
        "js_runtimes",
        "remote_components",
        "mpv_extra_args",
    }
)

# Klíče, které umíme přepnout za běhu — nastaví se rovnou i na app.cfg.
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

# key -> (label, nápověda, rozsah pro čísla)
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
    """Volby pro cookies_browser — prázdno, none a nalezené Chrome profily."""
    choices = ["", "none"]
    try:
        profiles = cfgmod._chrome_profiles_with_youtube_login()
    except Exception:  # detekce sahá na SQLite v profilu, nesmí shodit request
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


# --------------------------------------------------------------------------
# zápis do config.toml se zachováním komentářů
# --------------------------------------------------------------------------


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_scalar(str(v)) for v in value) + "]"
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _key_of(line: str) -> str | None:
    """Vrátí klíč, pokud řádek je přiřazení na nejvyšší úrovni."""
    stripped = line.lstrip()
    if not stripped or stripped.startswith("#") or stripped.startswith("["):
        return None
    key, sep, _ = stripped.partition("=")
    key = key.strip()
    if not sep or not key.replace("_", "").isalnum():
        return None
    return key


def _open_brackets(text: str) -> int:
    """Kolik hranatých závorek zůstalo neuzavřených (řetězce se ignorují)."""
    depth = 0
    in_str: str | None = None
    escaped = False
    for ch in text:
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_str:
                in_str = None
            continue
        if ch in "\"'":
            in_str = ch
        elif ch == "#":
            break
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
    return depth


def rewrite_config_text(text: str, changes: dict[str, Any]) -> str:
    """Přepíše jen řádky dotčených klíčů; komentáře a pořadí zůstanou.

    Nové klíče se přidají na konec kořenové části (tedy před první `[tabulku]`,
    aby nespadly dovnitř cizí sekce).
    """
    lines = text.splitlines()
    out: list[str] = []
    pending = dict(changes)
    first_table = -1
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("[") and first_table < 0:
            first_table = len(out)
        key = _key_of(line) if first_table < 0 else None
        if key in pending:
            # víceřádkové pole spolknout celé, ať po přepsání nezůstane ocas
            chunk = line
            while _open_brackets(chunk) > 0 and i + 1 < len(lines):
                i += 1
                chunk += "\n" + lines[i]
            out.append(f"{key} = {_toml_scalar(pending.pop(key))}")
            i += 1
            continue
        out.append(line)
        i += 1

    if pending:
        block = [f"{k} = {_toml_scalar(v)}" for k, v in pending.items()]
        at = first_table if first_table >= 0 else len(out)
        if at > 0 and out[at - 1].strip():
            block.insert(0, "")
        out[at:at] = block

    return "\n".join(out).rstrip("\n") + "\n"


class BadValue(ValueError):
    """Neplatná hodnota v POST /api/config — vrací se jako 400."""


def coerce_value(key: str, raw: Any) -> Any:
    """Ověří a převede jednu hodnotu podle typu v DEFAULTS."""
    if key not in cfgmod.DEFAULTS:
        raise BadValue(f"Neznámý klíč nastavení: {key}")
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
    """Pád jednoho requestu nesmí položit server ani smyčku."""

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
    """Webové ovládání nad instancí `ytdj.__main__.App`."""

    def __init__(self, app: "App", host: str = "127.0.0.1", port: int = 8765) -> None:
        self.app = app
        self.host = host
        self.port = port

        # true po dobu tahu Codexu — /api/status i SSE to hlásí dál
        self.busy = False

        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None
        self._sock: socket.socket | None = None
        self._closing = asyncio.Event()
        self._starlette = self._build()

    # ---- routy ----

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
            # frontend si může sáhnout i na /app.js — bereme to ze stejné složky
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

    # ---- stav ----

    async def _snapshot(self) -> dict:
        """Kompletní stav pro /api/status i pro SSE.

        Nesmí vyhodit výjimku kvůli tomu, že mpv zrovna neodpovídá — raději
        vrátí prázdný stav, aby se frontend neodpojoval.
        """
        current = None
        queue: list[dict] = []
        playing = paused = False
        position = duration = 0.0
        volume = 100
        try:
            st = await self.app.player.status()
            playing = bool(st.playing)
            paused = bool(st.paused)
            current = _track_dict(st.current)
            position = float(st.position or 0.0)
            duration = float(st.duration or 0.0)
            queue = [_track_dict(t) for t in st.queue]
            volume = int(st.volume)
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

                    # probudí se dřív, když se server vypíná
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

    # ---- povely ----

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

        # Serializace bez zámku: mezi testem a nastavením není žádný await,
        # takže v jedné smyčce se dva tahy nikdy nepotkají.
        # `app.ask()` navíc sdílí zámek s REPL a s automatickým přeseedováním,
        # aby si dva souběžné tahy nepřepsaly pooly.
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

    # ---- nastavení ----

    async def _config_get(self, request: Request) -> Response:
        cfg = self.app.cfg
        values: dict[str, Any] = {}
        fields: list[dict] = []

        for key in cfgmod.DEFAULTS:
            value = getattr(cfg, key, cfgmod.DEFAULTS[key])
            if isinstance(value, list):
                # do formuláře jde jeden řádek argumentů, ne pole
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
            await asyncio.to_thread(_write_config, changes)
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

    # ---- životní cyklus ----

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def start(self) -> None:
        """Nastartuje uvicorn jako úlohu a vrátí se, až port poslouchá."""
        if self._task is not None:
            return
        self._closing.clear()

        # Socket si otevřeme sami: chyba při bindu (obsazený port) tak přijde
        # jako obyčejná OSError sem, a ne jako SystemExit uvnitř úlohy, kde by
        # shodila celou smyčku i s přehrávačem.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.host, self.port))
        except OSError:
            sock.close()
            raise
        sock.set_inheritable(True)
        self.port = sock.getsockname()[1]  # kvůli portu 0 (test / náhodný port)
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
        # bez tohohle si uvicorn zabere SIGINT a Ctrl+C by nešlo do REPLu
        server.capture_signals = contextlib.nullcontext  # type: ignore[assignment]
        self._server = server

        self._task = asyncio.create_task(server.serve(sockets=[sock]), name="ytdj-web")

        deadline = asyncio.get_running_loop().time() + 10
        while not server.started:
            if self._task.done():
                self._task.result()  # propustí původní chybu
                raise RuntimeError("webový server se nespustil")
            if asyncio.get_running_loop().time() > deadline:
                raise RuntimeError("webový server nenaběhl do 10 s")
            await asyncio.sleep(0.02)

        log.info("webové rozhraní běží na %s", self.url)

    async def stop(self) -> None:
        self._closing.set()  # SSE smyčky se ukončí samy
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


def _write_config(changes: dict[str, Any]) -> None:
    """Přepíše config.toml. Běží ve vlákně — sahá na disk."""
    path = cfgmod.CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text() if path.exists() else "# ytdj — konfigurace\n"
    new_text = rewrite_config_text(text, changes)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(new_text)
    tmp.replace(path)  # atomicky, ať výpadek uprostřed zápisu config nezničí
