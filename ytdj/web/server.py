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
import gzip
import hashlib
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
from .. import telemetry
from . import votes_api

if TYPE_CHECKING:  # circular import — we pull in App for typing only
    from ..__main__ import App

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
INDEX_FILE = STATIC_DIR / "index.html"
VOLUME_MAX = 100  # strop hlasitosti — stejný jako fronta přání a displej
# čipy nálad (web, displej) → klíče pro DJ bez modelu (agent/offline.py)
CHIP_KEYS = frozenset({"calmer", "livelier", "czech", "more", "other", "surprise"})
PACKAGE_DIR = Path(__file__).resolve().parents[1]

# Tlačítko ▶ staré stránky (před frontou přání) posílalo rozjezd jako přání.
LEGACY_START_PROMPT = "Nic nehraje. Pusť hudbu a navaž na to, co jsem poslouchal naposledy."


def _digest(paths) -> str:
    h = hashlib.sha1()
    for p in paths:
        try:
            h.update(p.read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:10]


def build_ids() -> dict[str, str]:
    """"ui" = otisk index.html (stránka se podle něj sama znovu načte, když
    běží stará verze z cache — 25. 9. taková poslala "stop" všem); "app" =
    otisk celého balíčku ytdj (panel ho loguje)."""
    return {
        "ui": _digest([INDEX_FILE]),
        "app": _digest(sorted(PACKAGE_DIR.rglob("*.py")) + [INDEX_FILE]),
    }

HIST_TTL = 5.0  # s — historie pro snímek se čte ze state.db nejvýš takhle často

# how often the state is recomputed for SSE and how long silence may last
TICK = 1.0
KEEPALIVE = 15.0
# A keepalive the page can see: an SSE comment (": ping") never reaches
# JavaScript, so a paused player looked like a dead connection to the web UI.
# The panel's reader ignores it (the data isn't a JSON object).
PING = "event: ping\ndata: 1\n\n"
SOURCES = frozenset({"web", "panel", "repl"})  # who sent a wish (POST /api/prompt "source")

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
    "max_duration_request",
    "repeat_days",
    "artist_window",
    "display_filter",
    "display_blocklist",
    "ban_song_votes",
    "ban_artist_votes",
    "favourite_artist_votes",
    "wish_block",
    "wish_shared_block",
    "wish_budget",
    "wish_budget_panel",
    "wish_artist_max",
    "prefetch_first",
    "prefetch_max",
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
    "web_enabled": (
        "Webové ovládání",
        "Vypnutím zmizí i tahle stránka — pak zbude jen terminál.",
        None,
    ),
    "web_host": (
        "Adresa webu",
        "127.0.0.1 = jen z tohohle stroje. 0.0.0.0 zpřístupní ovládání celé síti, "
        "a to bez jakéhokoli hesla.",
        None,
    ),
    "web_port": (
        "Port webu",
        "0 = vybrat volný port při startu.",
        (0, 65535),
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
    "prefetch_first": (
        "Připravit hned",
        "Kolik nejbližších skladeb fronty se připraví (adresa streamu) přednostně.",
        (1, 6),
    ),
    "prefetch_max": (
        "Připravit postupně až",
        "Po nejbližších se dopředu přidává po jedné až do tolika skladeb "
        "(rychlé série Další); vždy až po skladbách, na které se čeká.",
        (3, 15),
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
    "max_duration_request": (
        "Maximální délka u vyžádaného interpreta (s)",
        "Když si řekneš o konkrétního interpreta a kratších skladeb od něj "
        "není dost, sáhne se i po delších — až do téhle hranice.",
        (10, 21600),
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
    "display_filter": (
        "Skrývat sprostá slova",
        "Jména a texty přání na displeji a webu bez sprostých slov (DJ je dostane beze změny).",
        None,
    ),
    "display_blocklist": (
        "Slova navíc ke skrytí",
        "Kořeny slov oddělené mezerou, např. „blbec trouba“ — skryje se každé slovo, které jimi začíná.",
        None,
    ),
    "ban_song_votes": (
        "Vyřazení skladby: kolik lidí 👎",
        "Skladba zmizí z podkresu, když jí dá 👎 aspoň tolik lidí a 👎 je víc než 👍. "
        "Na výslovné přání hraje dál.",
        (1, 50),
    ),
    "ban_artist_votes": (
        "Vyřazení interpreta: kolik lidí 👎",
        "Interpret zmizí z podkresu, když mu dá 👎 aspoň tolik lidí.",
        (1, 50),
    ),
    "favourite_artist_votes": (
        "Oblíbený interpret: kolik lidí 👍",
        "Celý interpret je oblíbený (podkres ho hraje častěji), když mu dá 👍 aspoň tolik "
        "lidí a 👍 je víc než 👎. Písničce stačí jeden 👍.",
        (1, 50),
    ),
    "wish_block": (
        "Přání: skladeb v jednom kole",
        "Kolik skladeb jednoho přání zazní za sebou, když nikdo jiný nečeká. "
        "Tolik skladeb má i přání nálady. Výchozí 3.",
        (1, 10),
    ),
    "wish_shared_block": (
        "Přání: skladeb v kole, když čekají jiní",
        "Po kolika skladbách se přání střídají, když čekají i kolegové. "
        "Nesmí být víc než skladeb v jednom kole. Výchozí 2.",
        (1, 10),
    ),
    "wish_budget": (
        "Přání z webu: skladeb celkem, když čekají jiní",
        "Přání s víc skladbami (třeba interpret) zahraje, dokud mají jiní co hrát, "
        "nejvýš tolik skladeb; pak jde za ostatní a pokračuje, až nikdo nečeká. Výchozí 4.",
        (1, 10),
    ),
    "wish_budget_panel": (
        "Přání z displeje: skladeb celkem, když čekají jiní",
        "Totéž pro přání z dotykového displeje. Výchozí 3.",
        (1, 10),
    ),
    "wish_artist_max": (
        "Přání interpreta: skladeb v přání",
        "Kolik skladeb interpreta patří k přání; zbytek hraje podkres (rádio). Výchozí 12.",
        (1, 50),
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


def check_together(changes: dict[str, Any], cfg: Any) -> None:
    """Pravidla mezi klíči: kolo, když čekají jiní, nesmí být delší než kolo."""
    if not {"wish_block", "wish_shared_block"} & set(changes):
        return

    def val(key: str) -> Any:
        return changes.get(key, getattr(cfg, key, cfgmod.DEFAULTS[key]))

    block, shared = val("wish_block"), val("wish_shared_block")
    if isinstance(block, int) and isinstance(shared, int) and shared > block:
        raise BadValue(
            f"{FIELD_META['wish_shared_block'][0]}: nejvýš {block} "
            f"(tolik je skladeb v jednom kole), přišlo {shared}"
        )


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


def short_ua(ua: str) -> str:
    """User-Agent zkrácený na "prohlížeč/systém" — stačí odlišit panel, mobil a PC."""
    ua = ua or ""
    low = ua.lower()
    if not ua:
        return "?"
    for key, name in (("ytdj-panel", "panel"), ("curl/", "curl"), ("python", "python"),
                      ("wget", "wget")):
        if key in low:
            return name
    browser = "jiný"
    for key, name in (("edg/", "Edge"), ("firefox/", "Firefox"), ("chrome/", "Chrome"),
                      ("safari/", "Safari")):
        if key in low:
            browser = name
            break
    system = ""
    for key, name in (("android", "Android"), ("iphone", "iPhone"), ("ipad", "iPad"),
                      ("windows", "Windows"), ("mac os", "Mac"), ("linux", "Linux")):
        if key in low:
            system = name
            break
    return f"{browser}/{system}" if system else browser


def _client(request: Request) -> dict[str, str]:
    try:
        ip = request.client.host if request.client else "?"
    except Exception:
        ip = "?"
    return {"ip": ip, "ua": short_ua(request.headers.get("user-agent", ""))}


def _took(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _safe(handler: Callable) -> Callable:
    """A single request crashing must not take down the server or the loop."""

    async def wrapper(request: Request) -> Response:
        try:
            return await handler(request)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("požadavek %s %s selhal", request.method, request.url.path)
            telemetry.event("web.error", path=request.url.path, method=request.method,
                            error=telemetry.clip(f"{type(exc).__name__}: {exc}", 300))
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
        self.build = build_ids()
        self._hist: tuple | None = None  # (skladba, kdy, historie)
        self._sse_clients = 0
        # what the DJ is working on and how the last wish went — every client
        # sees it, not only the one that asked (status "dj")
        self._dj_text = ""
        self._dj_source = ""
        self._dj_last: dict | None = None

        # One snapshot per tick for all SSE clients: each client used to ask
        # mpv (5 IPC calls) and SQLite on its own, every second.
        self._payload: str | None = None
        self._seq = 0
        # Pozice se mění každou vteřinu, zbytek stavu zřídka: celý stav jen při
        # změně, pozice zvlášť jako drobná událost "pos" (~25 B/s místo ~2 kB/s).
        self._sig: str | None = None
        self._pos_payload: str | None = None
        self._pos_seq = 0
        self._last_pos: float | None = None
        self._changed: asyncio.Condition | None = None
        self._poke: asyncio.Event | None = None
        self._bcast: asyncio.Task | None = None

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
            Route("/api/requests", _safe(self._requests), methods=["GET"]),
            Route("/api/requests/{rid}", _safe(self._request_action), methods=["POST", "DELETE"]),
            Route("/api/control", _safe(self._control), methods=["POST"]),
            Route("/api/dj/warm", _safe(self._dj_warm), methods=["POST"]),
            Route("/api/me", _safe(self._me_get), methods=["GET"]),
            Route("/api/me", _safe(self._me_post), methods=["POST"]),
            Route("/api/config", _safe(self._config_get), methods=["GET"]),
            Route("/api/config", _safe(self._config_post), methods=["POST"]),
            Route("/api/about", _safe(self._about), methods=["GET"]),
            Route("/api/restart", _safe(self._restart), methods=["POST"]),
            *votes_api.routes(self),  # hlasování kanceláře (PLAN H)
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
        # no-cache + ETag: prohlížeč se vždycky zeptá, jestli se stránka
        # nezměnila (304), a starou verzi z cache nepoužije. Gzip jednou
        # v paměti: 110 kB → ~25 kB pro každý telefon v kanceláři.
        st = INDEX_FILE.stat()
        stamp = (st.st_mtime_ns, st.st_size)
        if getattr(self, "_index_cache", (None,))[0] != stamp:
            raw = INDEX_FILE.read_bytes()
            etag = '"%s"' % hashlib.sha1(raw).hexdigest()[:16]
            self._index_cache = (stamp, raw, gzip.compress(raw, 6), etag)
        _, raw, packed, etag = self._index_cache
        headers = {"Cache-Control": "no-cache", "ETag": etag, "Vary": "Accept-Encoding",
                   "X-Ytdj-Build": self.build["ui"]}
        if etag in request.headers.get("if-none-match", ""):
            return Response(status_code=304, headers=headers)
        if "gzip" in request.headers.get("accept-encoding", ""):
            headers["Content-Encoding"] = "gzip"
            return Response(packed, media_type="text/html; charset=utf-8", headers=headers)
        return Response(raw, media_type="text/html; charset=utf-8", headers=headers)

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
        playing = paused = buffering = False
        position = duration = 0.0
        volume = 100
        quality = ""
        outage = None
        try:
            st = await self.app.player.status()
            playing = bool(st.playing)
            paused = bool(st.paused)
            buffering = bool(getattr(st, "buffering", False))
            current = _track_dict(st.current)
            position = float(st.position or 0.0)
            duration = float(st.duration or 0.0)
            queue = [_track_dict(t) for t in st.queue]
            volume = int(st.volume)
            quality = st.quality
            # výpadek YouTube / sítě: {reason, since, detail} — fronta čeká
            outage = getattr(st, "outage", None) or None
        except Exception:
            log.debug("stav přehrávače se nepodařilo přečíst", exc_info=True)

        try:
            pools = self.app.pools.describe()
            mood = self.app.pools.mood or ""
        except Exception:
            pools, mood = "", ""

        wq = getattr(self.app, "wishes", None)
        if wq is not None:
            # proč hraje, co hraje, a čí je co ve frontě
            try:
                if current is not None:
                    current["reason"] = wq.reason_for(current.get("id"))
                for item in queue:
                    tag = wq.queue_tag(item.get("id"))
                    if tag:
                        item["req"] = tag
            except Exception:
                log.debug("přání ke frontě se nepodařilo přiřadit", exc_info=True)
        try:
            votes_api.annotate(self.app, current, queue)  # 👍/👎 u skladeb (PLAN H)
        except Exception:
            log.debug("hlasy ke skladbám se nepodařilo přiřadit", exc_info=True)

        history: list[dict] = []
        try:
            for rec in await self._history():
                history.append(
                    {
                        "id": getattr(rec, "video_id", "") or "",  # hlas ze seznamu Odehráno
                        "artist": rec.artist or "",
                        "title": rec.title or "",
                        "outcome": rec.outcome,
                    }
                )
        except Exception:
            log.debug("historii se nepodařilo přečíst", exc_info=True)
        try:
            votes_api.annotate_list(self.app, history)  # 👍/👎 i u odehraných
        except Exception:
            log.debug("hlasy k historii se nepodařilo přiřadit", exc_info=True)

        busy = self.busy
        dj_text, dj_source = self._dj_text, self._dj_source
        extra: dict[str, Any] = {}
        if wq is not None:
            busy = busy or wq.busy
            thinking = next((w for w in wq.wishes if w.state == "thinking"), None)
            if thinking is not None:
                dj_text, dj_source = telemetry.clip(thinking.text, 200), thinking.source
            elif wq.starting:
                dj_text, dj_source = "rozjezd podle času a dne", "start"
            extra = {
                "requests": wq.public(),
                "starting": wq.starting,
                "people": wq.people(),
            }
        last = self._dj_last
        wq_last = getattr(wq, "last", None) if wq is not None else None
        if wq_last and (last is None or (wq_last.get("at") or 0) >= (last.get("at") or 0)):
            last = wq_last
        brain = self._brain()
        extra["dj_offline"] = brain is not None and not brain.get("online", True)
        if brain is not None:
            extra["dj_brain"] = brain
        return {
            **extra,
            "playing": playing,
            "paused": paused,
            "buffering": buffering,
            "current": current,
            "position": position,
            "duration": duration,
            "queue": queue,
            "pools": pools,
            "volume": volume,
            "quality": quality,
            "mood": mood,
            "busy": busy,
            "history": history,
            "outage": outage,
            "build": self.build["ui"],
            "version": self.build["app"],
            "dj": {
                "busy": busy,
                "text": dj_text if busy else "",
                "source": dj_source if busy else "",
                "last": last,
            },
        }

    def _brain(self) -> dict | None:
        """Stav mozku DJe (jistič Codexu) — {"online", "reason", "retry_in_s", "message"}."""
        status = getattr(getattr(self.app, "dj", None), "status", None)
        if not callable(status):
            return None
        try:
            brain = status().get("brain")
        except Exception:
            log.debug("stav DJe se nepodařilo zjistit", exc_info=True)
            return None
        return brain if isinstance(brain, dict) else None

    async def _history(self) -> list:
        """Posledních 20 přehrání — ze state.db mimo event loop, na chvíli v cache.

        Snímek se počítá každou vteřinu; čtení z SD karty v event loopu umí
        při zatížené kartě stát vteřiny (a s ním web i panel)."""
        store = self.app.store
        now = time.monotonic()
        key = self._hist_key()
        if self._hist is not None and self._hist[0] == key and now - self._hist[1] < HIST_TTL:
            return self._hist[2]
        aread = getattr(store, "aread", None)
        rows = await aread("recent_history", 20) if aread else store.recent_history(20)
        self._hist = (key, now, rows)
        return rows

    def _hist_key(self):
        try:
            st = self.app.player
            return getattr(st, "_current_id", None)
        except Exception:
            return None

    async def _status(self, request: Request) -> Response:
        return JSONResponse(await self._snapshot())

    # ---- one snapshot for everybody ----

    def poke(self, later: float | None = None) -> None:
        """Something changed (a command, a wish): push the state now, not at the next tick."""
        if self._poke is None:
            return
        if later:
            asyncio.get_running_loop().call_later(later, self._poke.set)
        else:
            self._poke.set()

    def _ensure_broadcast(self) -> None:
        if self._changed is None:
            self._changed = asyncio.Condition()
            self._poke = asyncio.Event()
        if self._bcast is None or self._bcast.done():
            self._bcast = asyncio.create_task(self._broadcast(), name="ytdj-web-sse")

    async def _broadcast(self) -> None:
        """Recomputes the state once per tick while anybody listens; wakes the streams on a change."""
        assert self._changed is not None and self._poke is not None
        try:
            while self._sse_clients > 0 and not self._closing.is_set():
                self._poke.clear()
                try:
                    snap = await self._snapshot()
                    pos = round(float(snap.get("position") or 0.0), 1)
                    sig = json.dumps({k: v for k, v in snap.items() if k != "position"},
                                     ensure_ascii=False)
                except Exception:
                    log.exception("snímek stavu pro SSE selhal")
                    snap, sig = None, None
                notify = False
                if snap is not None and (sig != self._sig or self._payload is None):
                    self._sig = sig
                    self._payload = json.dumps(snap, ensure_ascii=False)
                    self._seq += 1
                    self._last_pos = pos
                    notify = True
                elif snap is not None and pos != self._last_pos:
                    self._last_pos = pos
                    # pole, ne objekt: starší čtečky (panel) ho přeskočí jako ping
                    self._pos_payload = f"event: pos\ndata: [{pos}]\n\n"
                    self._pos_seq += 1
                    notify = True
                if notify:
                    async with self._changed:
                        self._changed.notify_all()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._poke.wait(), timeout=TICK)
        finally:
            # nobody listens (or shutting down): the next listener starts
            # from a fresh snapshot, not from one that may be minutes old
            self._payload = None
            self._sig = None

    async def _events(self, request: Request) -> Response:
        who = _client(request)

        async def stream():
            seen = 0
            pos_seen = self._pos_seq
            last_sent = time.monotonic()
            t_open = last_sent
            self._sse_clients += 1
            telemetry.event("web.sse_open", clients=self._sse_clients, **who)
            try:
                self._ensure_broadcast()
                changed = self._changed
                assert changed is not None
                while not self._closing.is_set():
                    if await request.is_disconnected():
                        break
                    # the broadcaster may have just wound down as we joined
                    self._ensure_broadcast()
                    now = time.monotonic()
                    if self._seq != seen and self._payload is not None:
                        seen = self._seq
                        pos_seen = self._pos_seq  # celý stav pozici obsahuje
                        last_sent = now
                        yield f"data: {self._payload}\n\n"
                    elif self._pos_seq != pos_seen and self._pos_payload is not None:
                        pos_seen = self._pos_seq
                        last_sent = now
                        yield self._pos_payload
                    elif now - last_sent >= KEEPALIVE:
                        last_sent = now
                        yield PING
                    if (self._seq == seen or self._payload is None) and self._pos_seq == pos_seen:
                        wait = max(0.05, min(TICK * 2, KEEPALIVE - (time.monotonic() - last_sent)))
                        async with changed:
                            with contextlib.suppress(asyncio.TimeoutError):
                                await asyncio.wait_for(changed.wait(), timeout=wait)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("SSE stream skončil chybou")
            finally:
                self._sse_clients -= 1
                telemetry.event("web.sse_close", clients=self._sse_clients,
                                duration_s=int(time.monotonic() - t_open), **who)

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
        t0 = time.monotonic()
        rec: dict[str, Any] = _client(request)
        resp = await self._prompt_inner(request, rec)
        rec["status"] = resp.status_code
        telemetry.event("web.prompt", took_ms=_took(t0), **rec)
        return resp

    async def _prompt_inner(self, request: Request, rec: dict[str, Any]) -> Response:
        try:
            data = await self._body(request)
        except BadValue as exc:
            rec["error"] = str(exc)
            return _json_error(str(exc), 400)

        text = (data.get("text") or "").strip()
        # Text přání se zapisuje (zkrácený): je to hlavní signál pro ladění
        # toho, jestli DJ vyhověl.
        rec["text"] = telemetry.clip(text, 300)
        rec["len"] = len(text)
        source = data.get("source")
        source = source if isinstance(source, str) and source in SOURCES else "web"
        rec["source"] = source
        if not text:
            rec["error"] = "prázdný text"
            return _json_error("Chybí text požadavku.", 400)

        wq = getattr(self.app, "wishes", None)
        if wq is not None:
            return await self._wish(data, text, source, rec, request)

        # Lock-free serialization: there is no await between the test and the
        # assignment, so two turns can never meet within a single loop.
        # On top of that, `app.ask()` shares a lock with the REPL and with
        # automatic reseeding, so two concurrent turns can't overwrite each
        # other's pools.
        # Automatické přeseedování (po sérii přeskočení) posluchače nezdržuje:
        # app.ask() ho zruší a vezme požadavek hned. Odmítá se jen souběh dvou
        # požadavků posluchače.
        auto = getattr(self.app, "_reseeding", False)
        if self.busy or (getattr(self.app, "codex_busy", False) and not auto):
            rec["error"] = "Codex právě pracuje"
            return _json_error("Codex právě pracuje", 409)
        if auto:
            rec["preempted_auto"] = True
        self.busy = True
        self._dj_text, self._dj_source = telemetry.clip(text, 200), source
        self.poke()  # everybody's "DJ přemýšlí" lights up now, not in a second
        last: dict[str, Any] = {"text": self._dj_text, "source": source, "ok": False, "reply": ""}
        try:
            ask = getattr(self.app, "ask", None)
            reply = await (ask(text) if ask else self.app.dj.turn(text))
        except Exception as exc:
            log.exception("tah Codexu selhal")
            rec["error"] = telemetry.clip(f"{type(exc).__name__}: {exc}", 300)
            # text výjimky posluchači nepatří (klíče, URL, interní hlášky)
            return _json_error("DJ teď neodpověděl — zkus to prosím za chvíli znovu.", 500)
        else:
            last.update(ok=True, reply=telemetry.clip(reply or "", 400))
        finally:
            self.busy = False
            last["at"] = time.time()
            self._dj_last = last
            self.poke()
            self.poke(later=0.4)  # the new track is usually loaded by then
        rec["reply"] = telemetry.clip(reply or "", 300)
        return JSONResponse({"reply": reply or ""})

    async def _wish(self, data: dict, text: str, source: str, rec: dict[str, Any],
                    request: Request) -> Response:
        """Přání do fronty: hned 202 + id; průběh a odpověď chodí stavem všem.

        Starší klienti (bez "who" i "wait" v těle — stará stránka v cache,
        starý panel) dostanou odpověď jako dřív: požadavek počká, až DJ
        přání vyřídí (zařadí nebo zamítne), a vrátí {"reply"}.
        """
        wq = self.app.wishes
        legacy = "who" not in data and "wait" not in data
        wait = legacy or data.get("wait") is True
        rec["legacy"] = legacy or None
        start = getattr(self.app, "play_or_start", None)
        if text == LEGACY_START_PROMPT and start is not None:
            # ▶ ze staré stránky v cache: rozjezd, ne přání cizího "hosta"
            rec["legacy_start"] = True
            did = await start("web")
            self.poke()
            return JSONResponse({"reply": "DJ vybírá hudbu podle času a dne." if did == "starting"
                                 else "Hraju dál.", "local": True})
        # povely jako "další" nebo "hlasitěji" hned, bez fronty
        by = wq.name_for(data.get("client"), " ".join(str(data.get("who") or "").split())[:24]) or (
            "displej" if source == "panel" else "někdo")
        local = await wq.try_local(text, by=by, client=data.get("client"))
        if local is not None:
            rec["local"] = True
            rec["reply"] = local
            self.poke()
            self.poke(later=0.3)
            return JSONResponse({"reply": local, "local": True})
        play_next = data.get("play_next") is True
        try:
            w = wq.submit(text, data.get("who"), source, play_next=play_next,
                          client={"ip": rec.get("ip"), "ua": rec.get("ua"),
                                  "id": data.get("client")})
            chip = data.get("chip")
            if isinstance(chip, str) and chip in CHIP_KEYS:
                w.chip = chip  # tlačítko nálady — funguje i bez mozku DJe
        except Exception as exc:  # TooMany
            if type(exc).__name__ != "TooMany":
                raise
            rec["error"] = "too_many"
            return _json_error(str(exc), 429)
        rec["id"], rec["who"] = w.id, w.who
        self.poke()
        if not wait:
            return JSONResponse(
                {"id": w.id, "token": w.token, "who": w.who, "state": w.state,
                 "request": w.public(), "reply": ""},
                status_code=202,
            )
        await wq.wait(w)
        rec["reply"] = telemetry.clip(w.reply or "", 300)
        rec["state"] = w.state
        self.poke()
        if w.state == "error":
            # odpověď je už srozumitelná věta (bez "Codex selhal: …" dvakrát)
            return _json_error(w.reply or "DJ teď neodpověděl.", 500)
        if w.state in ("waiting", "thinking"):
            return JSONResponse({"reply": "DJ na přání ještě pracuje — odpověď uvidíš ve frontě.",
                                 "id": w.id, "state": w.state}, status_code=202)
        return JSONResponse({"reply": w.reply or "Hotovo.", "id": w.id, "state": w.state})

    async def _requests(self, request: Request) -> Response:
        wq = getattr(self.app, "wishes", None)
        return JSONResponse({"requests": wq.public() if wq else []})

    async def _request_action(self, request: Request) -> Response:
        """Autor s tokenem: odebrat (DELETE / action=remove) nebo zařadit hned."""
        wq = getattr(self.app, "wishes", None)
        if wq is None:
            return _json_error("Fronta přání tu není.", 404)
        rid = request.path_params.get("rid", "")
        try:
            data = await self._body(request)
        except BadValue as exc:
            return _json_error(str(exc), 400)
        token = data.get("token") or request.headers.get("x-wish-token") or ""
        action = "remove" if request.method == "DELETE" else str(data.get("action") or "")
        if action == "remove":
            ok, msg = await wq.remove(rid, token)
        elif action == "next":
            ok, msg = await wq.set_play_next(rid, token)
        else:
            return _json_error(f"Neznámá akce: {action!r}", 400)
        telemetry.event("web.request_action", action=action, id=rid[:16], ok=ok, **_client(request))
        self.poke()
        if not ok:
            status = 404 if msg.startswith("Takové") else 403 if "nepatří" in msg else 409
            return _json_error(msg, status)
        return JSONResponse({"ok": True, "message": msg})

    # ---- přezdívka (kdo jsem) ----

    async def _me_get(self, request: Request) -> Response:
        """GET /api/me?client=<id> → {"client", "nick" ("" = ještě nemá), "tag"}."""
        wq = getattr(self.app, "wishes", None)
        if wq is None:
            return _json_error("Přezdívky tu nejsou.", 404)
        try:
            return JSONResponse(wq.me(request.query_params.get("client")))
        except ValueError as exc:  # NickError
            return _json_error(str(exc), 400)

    async def _me_post(self, request: Request) -> Response:
        """POST /api/me {"client", "nick"} → 200 {"client", "nick", "tag", "shared"?} | 400 {"error"}."""
        wq = getattr(self.app, "wishes", None)
        if wq is None:
            return _json_error("Přezdívky tu nejsou.", 404)
        try:
            data = await self._body(request)
            out = wq.set_nick(data.get("client"), data.get("nick"))
        except ValueError as exc:  # BadValue, NickError — věta pro člověka
            telemetry.event("web.nick_rejected", error=str(exc), **_client(request))
            return _json_error(str(exc), 400)
        self.poke()
        return JSONResponse(out)

    async def _dj_warm(self, request: Request) -> Response:
        """Někdo začal psát přání — ať Codex startuje už teď (CodexDJ.warm_ahead).

        Nic nepřehrává ani nezařazuje; tělo požadavku se nečte. Displej (panel)
        může volat totéž při otevření obrazovky přání.
        """
        warm = getattr(self.app.dj, "warm_ahead", None)
        ok = bool(warm("web")) if warm is not None else False
        return JSONResponse({"ok": ok})

    async def _control(self, request: Request) -> Response:
        t0 = time.monotonic()
        rec: dict[str, Any] = _client(request)
        resp = await self._control_inner(request, rec)
        rec["status"] = resp.status_code
        telemetry.event("web.control", took_ms=_took(t0), **rec)
        return resp

    async def _control_inner(self, request: Request, rec: dict[str, Any]) -> Response:
        try:
            data = await self._body(request)
        except BadValue as exc:
            rec["error"] = str(exc)
            return _json_error(str(exc), 400)

        action = data.get("action")
        value = data.get("value")
        rec["action"] = telemetry.clip(action, 40) if action is not None else None
        if value is not None:
            rec["value"] = value if isinstance(value, (int, float, bool)) else telemetry.clip(value, 40)
        player = self.app.player
        wq = getattr(self.app, "wishes", None)

        try:
            if action == "play":
                if wq is not None:
                    wq.note_pause(False)
                start = getattr(self.app, "play_or_start", None)
                if start is not None:
                    # nic nehraje ani nečeká → chytrý rozjezd podle situace (C7)
                    did = await start("panel" if rec.get("ua") == "panel" else "web")
                    rec["did"] = did
                    if did == "starting":
                        self.poke()
                        return JSONResponse({"ok": True, "starting": True})
                else:
                    await player.toggle_pause(False)
            elif action == "pause":
                if wq is not None:
                    wq.note_pause(True)  # úmyslná pauza — přání ji pár minut nezruší
                await player.toggle_pause(True)
            elif action == "next":
                if wq is not None:
                    # kdo přeskočil — vlastník přeskočeného přání to uvidí
                    by = wq.name_for(data.get("client"),
                                     " ".join(str(data.get("who") or "").split())[:24])
                    # přeskočení cizího přání ukončí jeho kolo ještě před skokem
                    await wq.skip_current(by or ("displej" if rec.get("ua") == "panel" else "někdo"),
                                          data.get("client"))
                else:
                    await player.skip()
            elif action == "stop":
                if wq is not None:
                    await wq.stop_all()  # jen pauza — cizí přání se nemažou
                else:
                    await player.clear_queue()
                    await player.toggle_pause(True)
            elif action == "volume":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    return _json_error("Hlasitost musí být číslo 0–100.", 400)
                if not (0 <= int(value) <= 130):
                    return _json_error("Hlasitost musí být v rozsahu 0–100.", 400)
                # strop 100 všude (web, displej, přání, povely); 101–130 od
                # starších klientů se ořízne, ne odmítne
                await player.set_volume(min(VOLUME_MAX, int(value)))
            else:
                rec["error"] = "neznámý povel"
                return _json_error(f"Neznámý povel: {action!r}", 400)
        except Exception as exc:
            log.exception("povel %r selhal", action)
            rec["error"] = telemetry.clip(f"{type(exc).__name__}: {exc}", 300)
            return _json_error("Povel se nepodařilo provést — podrobnosti v logu.", 500)

        # the other screens (panel, phones) see it now; mpv settles a moment later
        self.poke()
        self.poke(later=0.3)
        return JSONResponse({"ok": True})

    # ---- co je pod kapotou ----

    async def _about(self, request: Request) -> Response:
        cfg = self.app.cfg

        account = ""
        if cfg.cookies_browser.startswith("chrome:"):
            account = await asyncio.to_thread(
                cfgmod.chrome_profile_account, cfg.cookies_browser.split(":", 1)[1]
            )
        if cfg.cookies_file:
            account = "ze souboru s cookies"

        try:
            quality = (await self.app.player.status()).quality
        except Exception:
            quality = ""
        brain = self._brain() or {}

        return JSONResponse(
            {
                "youtube": {
                    "cookies": cfg.cookie_source(),
                    "account": account,
                    # ytmusicapi je druhé, nezávislé přihlášení: katalog a rádia
                    "library": "přihlášen" if self.app.catalog.authenticated else "anonymně",
                    "client": cfg.player_client or "výběr yt-dlp",
                    "pot": await _pot_status(),
                    "quality": quality,
                },
                "backend": {
                    "engine": "codex CLI",
                    "model": cfg.codex_model or "výchozí",
                    # "předplatné" jen když mozek opravdu jede
                    "auth": ("předplatné (bez API klíče)" if brain.get("online", True)
                             else (brain.get("message") or "DJ teď nejede")),
                    "online": bool(brain.get("online", True)),
                },
                # pod systemd nás znovu spustí on; bez něj se proces po
                # čistém úklidu vymění sám přes execv (viz __main__)
                "restartable": True,
            }
        )

    async def _restart(self, request: Request) -> Response:
        log.info("restart na vyžádání z webu")
        telemetry.event("web.restart", **_client(request))
        self.app.restart_requested.set()
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
            check_together(changes, self.app.cfg)
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
        # jen klíče a čísla/přepínače — cesty a texty (cookies…) se nepíšou
        telemetry.event(
            "web.config", keys=sorted(changes), restart=restart,
            values={k: v for k, v in changes.items() if isinstance(v, (bool, int, float))},
            **_client(request),
        )

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
        if self._poke is not None:
            self._poke.set()
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


