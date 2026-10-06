"""Web: vypínač „Klipy na telce“ (POZADAVKY #71).

    POST /api/tv {"video": true|false, "who": "…", "client": "…"}
        200 {"ok": true, "tv": {on, can, why, mode, …}} | 400 | 404 | 429

Přepnout smí kdokoli (jako hlasitost, bez PINu); stav je jeden pro celý
jukebox a chodí všem ve stavu (`tv` ve /api/status a v proudu událostí).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from .server import WebServer

# brzda proti cvakání: tolik přepnutí za tolik vteřin z jedné adresy
LIMIT, WINDOW = 10, 60.0


def public(app: Any) -> dict | None:
    """`tv` do stavu — None, když jukebox klipy vůbec nemá (testy, starý běh)."""
    tv = getattr(app, "tvvideo", None)
    if tv is None:
        return None
    try:
        return tv.public()
    except Exception:
        return None


def routes(srv: "WebServer") -> list[Route]:
    from .server import _client, _safe  # server importuje nás — až tady

    app = srv.app
    recent: dict[str, list[float]] = {}

    async def switch(request: Request) -> Response:
        tv = getattr(app, "tvvideo", None)
        if tv is None:
            return JSONResponse({"error": "Klipy na telce tu nejsou."}, status_code=404)
        try:
            data = await srv._body(request)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if not isinstance(data.get("video"), bool):
            return JSONResponse({"error": "Chybí video: true/false."}, status_code=400)
        who_info = _client(request)
        now = time.monotonic()
        hits = [t for t in recent.get(who_info["ip"], []) if now - t < WINDOW]
        if len(hits) >= LIMIT:
            return JSONResponse({"error": "Moc přepínání — chvilku počkej."}, status_code=429)
        recent[who_info["ip"]] = hits + [now]
        if len(recent) > 500:
            recent.clear()
        wq = getattr(app, "wishes", None)
        raw = " ".join(str(data.get("who") or "").split())[:24]
        who = raw
        if wq is not None:
            try:
                who = wq.name_for(data.get("client"), raw) or raw
            except Exception:
                who = raw
        state = await tv.set(data["video"], who, who_info)
        srv.poke()
        return JSONResponse({"ok": True, "tv": state})

    return [Route("/api/tv", _safe(switch), methods=["POST"])]
