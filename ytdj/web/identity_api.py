"""HTTP API „jeden člověk na všech adresách jukeboxu" (POZADAVKY #69) — logika je v ytdj/identity.py.

    POST /api/identity/sync    {"client": "<id z úložiště | ''>"}
        Srovná id z úložiště stránky s id v cookie (cookie platí pro všechny
        porty téhož jména). → {"client", "nick", "tag", "known", "note",
        "origins": [vlastní adresy], "here": "<tahle adresa | ''>", "linked": […],
        "jukebox": "<běžící instance>"} + Set-Cookie (HttpOnly, SameSite=Lax).
    GET  /api/identity/hello   {"jukebox": "<instance>"} — stránka si tím z prohlížeče
        ověří, že jiná adresa jukeboxu opravdu odpovídá (CORS jen pro vlastní adresy).
    POST /api/identity/offer   {"client", "theme"?, "tokens"?} → {"code"}
        Jednorázový kód (2 min) za účet na téhle adrese; id klienta se do adresy nepíše.
    POST /api/identity/redeem  {"client", "codes": […]} → jako sync + "extras"
        Vyzvedne kódy z ostatních adres a spojí účty do nejstaršího.
    GET  /identity             stránka, přes kterou si adresy účet předají
    GET  /identity.js          společná logika stránek

Požadavky, které mění stav, bere jen od stránky téže adresy (JSON, hlavička
Origin musí sedět s Host) — cizí stránka v síti tak nic nepřepíše ani nevyláká.
Všechno je v paměti, nic tu nečeká na kartu.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .. import identity
from ..wishes import clean_cid

if TYPE_CHECKING:
    from .server import WebServer

log = logging.getLogger(__name__)


def _err(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def from_own_page(request: Request) -> str | None:
    """None = v pořádku; jinak důvod odmítnutí. Prohlížeč posílá u POSTu Origin —
    musí být stejný jako adresa, na kterou požadavek přišel; formulář cizí stránky
    neumí poslat JSON a `fetch` cizí stránky s JSON neprojde bez svolení (CORS)."""
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/json":
        return "Čekám JSON."
    site = request.headers.get("sec-fetch-site", "")
    if site and site not in ("same-origin", "none"):
        return "Tohle smí jen stránka jukeboxu."
    origin = request.headers.get("origin")
    if origin is not None:
        host = identity.origin_from_host_header(request.headers.get("host"))
        mine = identity.origin_from_host_header(origin[7:]) if origin.startswith("http://") else ""
        if not mine or mine != host:
            return "Tohle smí jen stránka jukeboxu."
    return None


def routes(srv: "WebServer") -> list[Route]:
    """Cesty identity pro WebServer._build (před statickým fallbackem)."""
    from .server import _client, _safe  # server importuje nás — až tady

    app = srv.app

    def ident() -> identity.Identity:
        got = getattr(app, "identity", None)
        if got is None:
            got = identity.wire(app, srv.port)
        got.origins.port = srv.port  # port 0 v testech se dozvíme až po startu
        return got

    ident()  # už při startu webu: soubor se spojenými účty se čte ve vlákně, ne u prvního požadavku

    def here_of(request: Request, own: tuple[str, ...]) -> str:
        host = identity.origin_from_host_header(request.headers.get("host"))
        return host if host in own else ""

    def reply(idn: identity.Identity, request: Request, data: dict, cid: str) -> JSONResponse:
        own = idn.origins.list()
        here = here_of(request, own)
        data.update(known=idn.known(cid), jukebox=idn.instance, here=here,
                    origins=list(own) if here else [],
                    linked=list(idn.linked.get(cid, [])))
        resp = JSONResponse(data, headers={"Cache-Control": "no-store"})
        if cid:
            # HttpOnly: stránka cookie nečte (id má v úložišti); bez Secure — web je http
            resp.set_cookie(identity.COOKIE, cid, max_age=identity.COOKIE_AGE, path="/",
                            httponly=True, samesite="lax")
        return resp

    async def sync(request: Request) -> Response:
        bad = from_own_page(request)
        if bad:
            return _err(bad, 403)
        idn = ident()
        idn.origins.ensure_started()
        data = await srv._body(request)
        local = clean_cid(data.get("client"))
        cookie = clean_cid(request.cookies.get(identity.COOKIE))
        here = here_of(request, idn.origins.list())
        winner, note = idn.merge([c for c in (local, cookie) if c], here=here)
        if winner:
            idn.note_seen(winner)
            nicks = idn.nicks
            if nicks is not None:
                nicks.touch(winner)
        out = idn.me(winner) if winner else {"client": "", "nick": "", "tag": ""}
        out["note"] = note
        if note and note.get("kind") == "merged":
            srv.poke()
        return reply(idn, request, out, winner)

    async def hello(request: Request) -> Response:
        idn = ident()
        headers = {"Cache-Control": "no-store", "Vary": "Origin"}
        origin = request.headers.get("origin", "")
        if origin and origin in idn.origins.list():
            headers["Access-Control-Allow-Origin"] = origin
        return JSONResponse({"jukebox": idn.instance}, headers=headers)

    async def offer(request: Request) -> Response:
        bad = from_own_page(request)
        if bad:
            return _err(bad, 403)
        idn = ident()
        here = here_of(request, idn.origins.list())
        if not here:
            return _err("Tahle adresa mezi adresy jukeboxu nepatří.", 403)
        data = await srv._body(request)
        cid = clean_cid(data.get("client")) or clean_cid(request.cookies.get(identity.COOKIE))
        code = idn.offer(cid, here, identity.clean_extras(data))
        return JSONResponse({"code": code}, headers={"Cache-Control": "no-store"})

    async def redeem(request: Request) -> Response:
        bad = from_own_page(request)
        if bad:
            return _err(bad, 403)
        idn = ident()
        here = here_of(request, idn.origins.list())
        if not here:
            return _err("Tahle adresa mezi adresy jukeboxu nepatří.", 403)
        data = await srv._body(request)
        local = clean_cid(data.get("client"))
        if not local:
            return _err("Chybí id prohlížeče — obnov prosím stránku.", 400)
        codes = data.get("codes")
        out = idn.redeem(local, codes if isinstance(codes, list) else [], here=here)
        if out.get("note"):
            srv.poke()
            log.info("účet předán mezi adresami (%s) z %s", out["note"].get("kind"), _client(request)["ip"])
        return reply(idn, request, out, out["client"])

    return [
        Route("/api/identity/sync", _safe(sync), methods=["POST"]),
        Route("/api/identity/hello", _safe(hello), methods=["GET"]),
        Route("/api/identity/offer", _safe(offer), methods=["POST"]),
        Route("/api/identity/redeem", _safe(redeem), methods=["POST"]),
        Route("/identity", _safe(srv._manual), methods=["GET"]),
        Route("/identity.js", _safe(srv._manual), methods=["GET"]),
    ]
