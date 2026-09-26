"""HTTP API hlasování kanceláře (PLAN H) — logika je v ytdj/votes.py.

    POST /api/votes        hlas 👍 / 👎 / stáhnout
    GET  /api/votes        seznamy: oblíbené, vyřazené, rozhoduje se (+ moje)
    GET  /api/votes/track  jedna skladba a každý její interpret (detail)
    GET  /api/status       current.votes / queue[].votes (viz annotate)

Kdo hlasuje = `client` (id prohlížeče, jako u přání); jméno bere server
z registru přezdívek (`/api/me`), jinak z `who`. Všechno čte a mění jen
paměť — zápis do state.db jde přes vlákno Store, event loop na SD kartu
nečeká.

POST /api/votes
    {"target": "song" | "artist", "vote": 1 | -1 | 0, "client": "<id>",
     "who": "Petr",                       # jen když nemá přezdívku
     "video_id": "abc"?,                  # bez něj = co právě hraje
     "artist": "Kabát"?, "title": "…"?,   # skladba mimo frontu; jméno interpreta
     "key": "kabat|mala dama"?}           # z GET /api/votes (hlas ze seznamu)
    200 {"ok": true, "item": {…položka…}, "changed": "ban" | "unban" | null,
         "message": "…", "effect": {"removed": 1, "skipped": false}?}
    400 / 404 / 429 {"error": "věta pro člověka"}

Položka (item) — stejná v POST, GET /api/votes i v detailu:
    {"target": "song", "key": "kabat|mala dama", "label": "Kabát — Malá dáma",
     "artist": "Kabát", "title": "Malá dáma", "video_id": "abc",
     "up": 0, "down": 2, "status": "banned", "need": 0,
     "voters": [{"nick": "Petr", "vote": -1, "at": 1790000000.0, "tag": "3f2a…"}],
     "updated": 1790000000.0, "mine": -1}
    status skladby: banned | favourite | downweighted | neutral
    status interpreta: banned | favourite | pending | neutral   (👍 i 👎 jako u skladby)
    need = kolik 👎 ještě chybí k vyřazení; mine jen s ?client= / "client".
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .. import telemetry
from ..votes import ARTIST, BANNED, FAVOURITE, SONG, VoteError, enforce
from ..wishes import clean_cid

if TYPE_CHECKING:
    from .server import WebServer

log = logging.getLogger(__name__)

WHO_MAX = 24


def _err(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def _track(app: Any, video_id: str) -> Any:
    """Skladba podle videoId: hrající, ve frontě, známá z poolů; "" = hrající."""
    st = await app.player.status()
    if not video_id:
        return st.current
    for t in [st.current, *st.queue]:
        if t is not None and t.id == video_id:
            return t
    pools = getattr(app, "pools", None)
    known = getattr(pools, "session_track", None)
    return known(video_id) if known is not None else None


def _message(target: str, res: Any, fav_artist: int = 2) -> str:
    item = res.item
    name = item.get("label") or item.get("key")
    if res.changed == "ban":
        return (f"{name} je vyřazen{'ý' if target == ARTIST else 'á'} hlasováním — "
                "podkres ho už nepustí.")
    if res.changed == "unban":
        return f"{name} je zpátky v nabídce."
    if res.vote == 0:
        return "Hlas stažen."
    if item["status"] == BANNED:
        return f"Hlas zapsán (už je vyřazen{'ý' if target == ARTIST else 'á'})."
    if res.vote > 0 and item["status"] == FAVOURITE and target == ARTIST:
        return f"Hlas zapsán — {name} je mezi oblíbenými interprety kanceláře."
    if res.vote > 0 and target == ARTIST and item.get("up", 0) < fav_artist:
        # jeden 👍 celého interpreta z něj oblíbeného neudělá (PLAN H3)
        who = f"dají aspoň {fav_artist} lidé" if fav_artist <= 4 else f"dá aspoň {fav_artist} lidí"
        return f"Hlas zapsán — oblíbený interpret bude, až mu 👍 {who}."
    need = item.get("need") or 0
    if res.vote < 0 and need:
        return f"Hlas zapsán — k vyřazení chybí ještě {need}× 👎 od dalších."
    return "Hlas zapsán."


def routes(srv: "WebServer") -> list[Route]:
    """Cesty hlasování pro WebServer._build (před statickým fallbackem)."""
    from .server import _client, _safe  # server importuje nás — až tady

    app = srv.app

    def book() -> Any:
        return getattr(app, "votes", None)

    async def cast(request: Request) -> Response:
        votes = book()
        if votes is None:
            return _err("Hlasování tu není.", 404)
        try:
            data = await srv._body(request)
        except ValueError as exc:
            return _err(str(exc), 400)
        cid = clean_cid(data.get("client"))
        rec: dict[str, Any] = {**_client(request), "target": data.get("target"),
                               "vote": data.get("vote")}
        wq = getattr(app, "wishes", None)
        raw_who = " ".join(str(data.get("who") or "").split())[:WHO_MAX]
        if wq is not None and cid and not wq.name_for(cid):
            # hlasování je veřejné: vždy musí být jasné, kdo hlasoval
            rec["status"] = 403
            telemetry.event("vote.rejected", reason="no_nick", **rec)
            return _err("Hlasovat jde s přezdívkou — nastav si ji nahoře vpravo.", 403)
        who = (wq.name_for(cid, raw_who) if wq is not None else raw_who) or "Host"
        key = str(data.get("key") or "")[:200]
        video_id = str(data.get("video_id") or "")[:64]
        target = data.get("target")
        if target not in (SONG, ARTIST):
            return _err("Hlasovat jde o skladbu (\"song\"), nebo o interpreta (\"artist\").", 400)
        track = None
        if not key:
            # skladba mimo frontu (Odehráno) bez videoId: podle jména, ne "co hraje"
            by_name = not video_id and target == SONG and bool(data.get("title"))
            track = None if by_name else await _track(app, video_id)
            if track is None and target == SONG and data.get("title"):
                from ..music.catalog import Track

                track = Track(video_id, str(data.get("title"))[:200],
                              str(data.get("artist") or "")[:200])
            if track is None and not (target == ARTIST and data.get("artist")):
                msg = "Teď nic nehraje." if not video_id else "Tuhle skladbu neznám."
                telemetry.event("vote.rejected", error=msg, **rec)
                return _err(msg, 404)
        try:
            res = votes.cast(target, cid, data.get("vote"), who, track=track, key=key,
                             artist=str(data.get("artist") or "")[:200])
        except VoteError as exc:
            telemetry.event("vote.rejected", error=str(exc), status=exc.status, **rec)
            return _err(str(exc), exc.status)
        out: dict[str, Any] = {"ok": True, "item": res.item, "changed": res.changed,
                               "message": _message(res.target, res,
                                                   getattr(votes, "fav_artist_threshold", lambda: 2)())}
        if res.changed == "ban":
            try:
                out["effect"] = await enforce(app, reason=f"{res.target}:{res.key}")
            except Exception:
                log.exception("vyřazení se nepodařilo promítnout do fronty")
        srv.poke()
        srv.poke(later=0.4)  # mpv po přeskočení / odebrání
        return JSONResponse(out)

    async def lists(request: Request) -> Response:
        votes = book()
        if votes is None:
            return _err("Hlasování tu není.", 404)
        return JSONResponse(votes.lists(clean_cid(request.query_params.get("client"))))

    async def detail(request: Request) -> Response:
        votes = book()
        if votes is None:
            return _err("Hlasování tu není.", 404)
        video_id = (request.query_params.get("video_id") or "")[:64]
        track = await _track(app, video_id)
        if track is None:
            return _err("Teď nic nehraje." if not video_id else "Tuhle skladbu neznám.", 404)
        return JSONResponse(votes.detail(track, clean_cid(request.query_params.get("client"))))

    return [
        Route("/api/votes", _safe(cast), methods=["POST"]),
        Route("/api/votes", _safe(lists), methods=["GET"]),
        Route("/api/votes/track", _safe(detail), methods=["GET"]),
    ]


def annotate_list(app: Any, items: list[dict]) -> None:
    """Jako queue[i].votes u annotate — pro seznam Odehráno."""
    votes = getattr(app, "votes", None)
    if votes is None or not getattr(votes, "items", None):
        return
    for item in items:
        b = votes.brief(item)
        if b:
            item["votes"] = b


def annotate(app: Any, current: dict | None, queue: list[dict]) -> None:
    """Hlasy do sdíleného snímku /api/status (a SSE) — malé, bez jmen:

        current.votes = {"up": 1, "down": 0, "status": "favourite",
                         "up_by": ["<tag>"], "down_by": [],
                         "artists": [{"name": "Kabát", "up": 2, "down": 1,
                                      "status": "favourite", "up_by": ["<tag>", …],
                                      "by": ["<tag>"], "down_by": ["<tag>"]}]}
        queue[i].votes = {"up", "down", "status", "up_by"?, "down_by"?,
                          "artist_status"?, "artists"?}   — jen u položek s hlasy
        artists[].by = down_by (👎; starší web a displej čtou "by");
        artist_status = banned | pending | favourite (nejhorší z uvedených)

    `tag` je značka klienta z /api/me: "můj hlas" = moje značka v up_by / down_by.
    """
    votes = getattr(app, "votes", None)
    if votes is None:
        return
    if current is not None:
        current["votes"] = votes.brief(current, full=True)
    if not getattr(votes, "items", None):
        return
    for item in queue:
        b = votes.brief(item)
        if b:
            item["votes"] = b
