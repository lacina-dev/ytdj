"""HTTP API stránky „Chyby a nápady" (POZADAVKY #52) — logika je v ytdj/issues.py.

Kolegové bez přihlášení (kdo = `client`, id prohlížeče jako u přání; jméno
bere server z registru přezdívek, jinak z `who`, jinak „Host"):

    GET    /api/issues?client=&kind=&state=&offset=&limit=
               kind: chyba | napad; state: open | closed | nove | resi_se |
               nasazeni | vyreseno | zamitnuto; limit ≤ 50 (0 = jen počty)
               200 {"items": [položka…], "total", "offset", "limit", "more",
                    "counts": {"all", "open", "closed", "states": {…}, "kinds": {…}}}
    POST   /api/issues                  {"client", "who"?, "kind", "title", "body"?}
               201 {"ok": true, "item": {…celá položka…}}
    GET    /api/issues/{id}?client=     200 {"item": {…celá položka…}}
    POST   /api/issues/{id}/comments    {"client", "who"?, "body"}   201 {"ok", "item"}
    POST   /api/issues/{id}/plus        {"client", "who"?, "on"?: bool}  (bez "on" přepne)
               200 {"ok", "item"}

Správce — jen s PINem v hlavičce X-YTDJ-PIN (stejná brzda jako nastavení):

    POST   /api/issues/{id}/resolve     {"state"?, "resolution"?}    200 {"ok", "item"}
    DELETE /api/issues/{id}                                          200 {"ok", "deleted"}
    DELETE /api/issues/{id}/comments/{cid}                           200 {"ok", "item"}

Položka v seznamu: {"id", "kind", "title", "state", "open", "who", "created",
"updated", "plus", "plus_mine", "comments" (počet), "resolved", "resolved_at",
"mine", "seed", "excerpt"}; celá má místo "excerpt" navíc "body",
"resolution", "comment_list": [{"id", "who", "body", "at", "mine"}], "plus_by".
Chyby: 400 / 404 / 409 / 413 / 429 / 503 {"error": "věta pro člověka"}.

Texty jdou ven jen jako JSON; stránka je vkládá jako text (textContent),
nikdy jako HTML. Všechno tu čte a mění jen paměť — na kartu zapisuje vlákno
`Store`, hlavní smyčka na disk nečeká.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .. import telemetry
from ..issues import WHO_MAX, IssueError
from ..wishes import clean_cid

if TYPE_CHECKING:
    from .server import WebServer

log = logging.getLogger(__name__)

BODY_LIMIT = 16 * 1024  # bajtů v těle požadavku — víc žádný text nepotřebuje


def _err(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def routes(srv: "WebServer") -> list[Route]:
    """Cesty hlášení pro WebServer._build (před statickým fallbackem)."""
    from .server import _client, _safe  # server importuje nás — až tady

    app = srv.app

    def book() -> Any:
        return getattr(app, "issues", None)

    async def body_of(request: Request) -> dict:
        raw = await request.body()
        if len(raw) > BODY_LIMIT:
            raise IssueError("Tohle je moc dlouhé.", 413)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise IssueError("Tělo požadavku není platný JSON.", 400) from None
        if not isinstance(data, dict):
            raise IssueError("Očekávám JSON objekt.", 400)
        return data

    def who_of(data: dict) -> tuple[str, str]:
        """(id klienta, jméno) — bez id prohlížeče se nepíše."""
        from . import identity_api  # staré id spojeného účtu → platný účet (F-NICK-09)
        identity_api.current(app, data, "/api/issues")
        cid = clean_cid(data.get("client"))
        if not cid:
            raise IssueError("Chybí id prohlížeče — obnov prosím stránku.", 400)
        wq = getattr(app, "wishes", None)
        raw = " ".join(str(data.get("who") or "").split())[:WHO_MAX]
        return cid, (wq.name_for(cid, raw) if wq is not None else raw) or "Host"

    def guard(fn: Any, kind: str) -> Any:
        """Společné: bez knihy 404, IssueError → věta a kód, zápis do provozního logu."""

        async def handler(request: Request) -> Response:
            issues = book()
            if issues is None:
                return _err("Chyby a nápady tu nejsou.", 404)
            try:
                return await fn(request, issues)
            except IssueError as exc:
                if exc.status in (413, 429):
                    telemetry.event("issue.rejected", action=kind, status=exc.status,
                                    **_client(request))
                return _err(str(exc), exc.status)

        handler.__name__ = fn.__name__
        return handler

    async def listing(request: Request, issues: Any) -> Response:
        q = request.query_params
        return JSONResponse(issues.listing(
            clean_cid(q.get("client")), kind=q.get("kind") or "", state=q.get("state") or "",
            offset=q.get("offset") or 0, limit=q.get("limit") if q.get("limit") is not None else 20))

    async def detail(request: Request, issues: Any) -> Response:
        return JSONResponse({"item": issues.detail(request.path_params.get("iid"),
                                                   clean_cid(request.query_params.get("client")))})

    async def create(request: Request, issues: Any) -> Response:
        data = await body_of(request)
        cid, who = who_of(data)
        c = _client(request)
        item = issues.create(cid, who, data.get("kind"), data.get("title"), data.get("body") or "",
                             ip=c["ip"])
        telemetry.event("issue.created", id=item["id"], type=item["kind"],
                        title=telemetry.clip(item["title"], 120), who=who, **c)
        return JSONResponse({"ok": True, "item": item}, status_code=201)

    async def comment(request: Request, issues: Any) -> Response:
        data = await body_of(request)
        cid, who = who_of(data)
        c = _client(request)
        item = issues.comment(request.path_params.get("iid"), cid, who, data.get("body"), ip=c["ip"])
        telemetry.event("issue.comment", id=item["id"], who=who, **c)
        return JSONResponse({"ok": True, "item": item}, status_code=201)

    async def plus(request: Request, issues: Any) -> Response:
        data = await body_of(request)
        cid, who = who_of(data)
        on = data.get("on")
        item = issues.plus(request.path_params.get("iid"), cid, who,
                           on if isinstance(on, bool) else None, ip=_client(request)["ip"])
        return JSONResponse({"ok": True, "item": item})

    # ---- správce (PIN) ----

    async def resolve(request: Request, issues: Any) -> Response:
        data = await body_of(request)
        state, text = data.get("state"), data.get("resolution")
        if text is not None and not isinstance(text, str):
            raise IssueError("Text řešení má být text.", 400)
        item = issues.admin_update(request.path_params.get("iid"), state=state, resolution=text)
        telemetry.event("issue.resolved", id=item["id"], state=item["state"],
                        has_text=bool(item["resolution"]), **_client(request))
        return JSONResponse({"ok": True, "item": item})

    async def remove(request: Request, issues: Any) -> Response:
        gone = issues.delete(request.path_params.get("iid"))
        telemetry.event("issue.deleted", id=gone["id"], **_client(request))
        return JSONResponse({"ok": True, "deleted": gone["id"]})

    async def remove_comment(request: Request, issues: Any) -> Response:
        item = issues.delete_comment(request.path_params.get("iid"), request.path_params.get("cid"))
        telemetry.event("issue.comment_deleted", id=item["id"], **_client(request))
        return JSONResponse({"ok": True, "item": item})

    def admin(fn: Any, kind: str) -> Any:
        return _safe(srv._admin_only(guard(fn, kind)))

    return [
        Route("/api/issues", _safe(guard(listing, "list")), methods=["GET"]),
        Route("/api/issues", _safe(guard(create, "create")), methods=["POST"]),
        Route("/api/issues/{iid}", _safe(guard(detail, "detail")), methods=["GET"]),
        Route("/api/issues/{iid}/comments", _safe(guard(comment, "comment")), methods=["POST"]),
        Route("/api/issues/{iid}/plus", _safe(guard(plus, "plus")), methods=["POST"]),
        # stav, řešení a mazání jen s PINem správce (F-HLASENI-05)
        Route("/api/issues/{iid}/resolve", admin(resolve, "resolve"), methods=["POST"]),
        Route("/api/issues/{iid}", admin(remove, "delete"), methods=["DELETE"]),
        Route("/api/issues/{iid}/comments/{cid}", admin(remove_comment, "delete_comment"),
              methods=["DELETE"]),
    ]
