"""HTTP API seznamu AI tvorby (hlášení #37) — logika je v ytdj/aitvorba.py.

    GET  /api/ai?client=   {"ai": [...], "suggested": [...], "not_ai": [...],
                            "counts": {...}, "now": {...} | null}
         položka: {key, name, state, source, reason, by, at, video_id, title,
                   nick?, tag?, mine?}
         now: hrající skladba {video_id, title, artist, name, key, wish, flag}
    /api/status: current.ai = [{name, key, state}] — každý uvedený interpret hrající
    POST /api/ai           {"client", "state": "ai" | "neni_ai" | "navrzeno",
                            "key"? (ze seznamu) | "artist"? | nic = hlavní
                            interpret hrající skladby, "reason"?}
         200 {"ok", "item", "message", "effect"?}   400/403/404/429 {"error"}

Rozhodnout smí kdokoli s přezdívkou (jako hlasovat); kdo a kdy, je vidět.
Označení `ai` hned uklidí podkres z fronty (votes.enforce) — přání ne.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .. import telemetry
from ..aitvorba import AI, NOT_AI, SUGGESTED
from ..votes import VoteError, artist_key, credits, enforce
from ..wishes import clean_cid
from . import identity_api

if TYPE_CHECKING:
    from .server import WebServer

log = logging.getLogger(__name__)


def _err(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _message(flag: Any, prev: Any) -> str:
    if flag.state == AI:
        return f"{flag.name} je na seznamu AI tvorby — jukebox ho sám nenabídne."
    if flag.state == NOT_AI:
        return (f"{flag.name} není AI — jukebox ho nabízí jako dřív."
                if prev is not None and prev.state == AI else f"Zapsáno: {flag.name} není AI.")
    return f"{flag.name} je mezi návrhy — hraje dál, dokud to někdo nepotvrdí."


def annotate(app: Any, current: dict | None) -> None:
    """AI stav interpretů hrající skladby do sdíleného snímku (tlačítko 🤖 AI na
    kartě hrající, F-AI-06) — malé, bez jmen toho, kdo označil:

        current.ai = [{"name": "Kraken89", "key": "kraken89", "state": "ai"},
                      {"name": "Rendys", "key": "rendys", "state": ""}]
    """
    book = getattr(app, "ai", None)
    if book is None or current is None:
        return
    flags = getattr(book, "flags", {})
    out = []
    for name in credits(str(current.get("artist") or "")):
        key = artist_key(name)
        f = flags.get(key)
        out.append({"name": name, "key": key, "state": f.state if f is not None else ""})
    if out:
        current["ai"] = out


def routes(srv: "WebServer") -> list[Route]:
    from .server import _client, _safe

    app = srv.app

    def book() -> Any:
        return getattr(app, "ai", None)

    async def now(viewer: str) -> dict | None:
        player = getattr(app, "player", None)
        if player is None:
            return None
        try:
            st = await player.status()
        except Exception:
            return None
        cur = st.current
        if cur is None:
            return None
        cr = credits(cur.artist or "")
        wq = getattr(app, "wishes", None)
        is_wish = getattr(wq, "is_request_track", None)
        return {"video_id": cur.id, "title": cur.title, "artist": cur.artist,
                "name": cr[0] if cr else "", "wish": bool(is_wish and is_wish(cur.id)),
                "flag": book().state_of(cur)}

    async def lists(request: Request) -> Response:
        ai = book()
        if ai is None:
            return _err("Seznam AI tvorby tu není.", 404)
        who = {"client": request.query_params.get("client")}
        identity_api.current(app, who, request.url.path)
        viewer = clean_cid(who["client"])
        out = ai.lists(viewer)
        out["now"] = await now(viewer)
        return JSONResponse(out)

    async def decide(request: Request) -> Response:
        ai = book()
        if ai is None:
            return _err("Seznam AI tvorby tu není.", 404)
        try:
            data = await srv._body(request)
        except ValueError as exc:
            return _err(str(exc), 400)
        cid = clean_cid(data.get("client"))
        rec = {**_client(request), "state": data.get("state")}
        if not cid:
            return _err("Chybí id prohlížeče.", 400)
        wq = getattr(app, "wishes", None)
        if wq is not None and not wq.name_for(cid):
            telemetry.event("ai.rejected", reason="no_nick", **rec)
            return _err("Označovat jde s přezdívkou — nastav si ji nahoře vpravo.", 403)
        who = (wq.name_for(cid) if wq is not None else "") or "Host"
        state = data.get("state")
        if state not in (AI, NOT_AI, SUGGESTED):
            return _err("Stav je „ai“, „neni_ai“, nebo „navrzeno“.", 400)
        key = str(data.get("key") or "")[:200]
        artist = str(data.get("artist") or "")[:200]
        track = None
        if not key and not artist:
            st = await app.player.status()
            track = st.current
            if track is None:
                return _err("Teď nic nehraje.", 404)
        try:
            flag, prev = ai.decide(state, cid, who, key=key, name=artist, track=track,
                                   reason=str(data.get("reason") or ""))
        except VoteError as exc:
            telemetry.event("ai.rejected", error=str(exc), status=exc.status, **rec)
            return _err(str(exc), exc.status)
        out: dict[str, Any] = {"ok": True, "item": ai.item(flag, cid), "message": _message(flag, prev)}
        if flag.state == AI:
            try:
                out["effect"] = await enforce(app, reason=f"ai:{flag.key}")
            except Exception:
                log.exception("označení AI se nepodařilo promítnout do fronty")
        srv.poke()
        srv.poke(later=0.4)
        return JSONResponse(out)

    return [
        Route("/api/ai", _safe(lists), methods=["GET"]),
        Route("/api/ai", _safe(decide), methods=["POST"]),
    ]
