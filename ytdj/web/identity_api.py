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
    POST /api/identity/report  {"client", "event": "<druh>", …} → 204
        Co se dělo v prohlížeči (které adresy odpověděly, cesta začala / skončila…) —
        jen kvůli logu; bere jen hodnoty z výčtů a vlastní adresy, s brzdou.
    GET  /api/identity/admin/accounts?nick=   (PIN správce) přehled účtů: konec id, přezdívka,
        první a poslední setkání, počty hlasů, playlistů a písniček — nikdy celé id
    POST /api/identity/admin/join  {"accounts": ["<konec id>", …], "confirm": false|true}
        (PIN správce) spojí jmenované účty do nejstaršího stejnou cestou jako sync / redeem;
        bez "confirm" jen vrátí plán a nic nezmění (F-NICK-12, `python -m ytdj.identity`)
    GET  /identity             stránka, přes kterou si adresy účet předají
    GET  /identity.js          společná logika stránek

Požadavky, které mění stav, bere jen od stránky téže adresy (JSON, hlavička
Origin musí sedět s Host) — cizí stránka v síti tak nic nepřepíše ani nevyláká.
Všechno je v paměti, nic tu nečeká na kartu.

Do provozního logu (identity.*) jde každý krok, ale nikdy id klienta, kód ani
cookie — jen veřejná značka člověka (F-BEZP-04):
    identity.origins   seznam vlastních adres se změnil
    identity.sync      how: new | same | cookie | moved | merged (new/same nejvýš 1× za 30 min)
    identity.offer     adresa vydala kód (account: jestli na ní účet byl)
    identity.redeem    kódy vyzvednuty: given / ok / expired / unknown, from, note
    identity.merge     dva účty spojeny: older / younger, votes, conflicts, imports, wishes
    identity.refused   odmítnutý požadavek: why = not_json | cross_site | origin | unknown_address | no_client
    identity.page      hlášení stránky: event = probe | trip_start | hop | trip_done | trip_lost | skip | moved
    identity.stale     požadavek přišel se starým (spojeným) id — server ho vyřídil za platný účet
    identity.admin_list  správce si vypsal účty (kolik)
    identity.admin_join  správce spojil účty (dry = jen plán): kept / joined = konce id, počty
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .. import identity, telemetry
from ..nicks import tag_of
from ..wishes import clean_cid

if TYPE_CHECKING:
    from .server import WebServer

log = logging.getLogger(__name__)

JOIN_MAX = 12  # účtů v jednom spojení správcem

REFUSALS = {
    "not_json": "Čekám JSON.",
    "cross_site": "Tohle smí jen stránka jukeboxu.",
    "origin": "Tohle smí jen stránka jukeboxu.",
    "unknown_address": "Tahle adresa mezi adresy jukeboxu nepatří.",
    "no_client": "Chybí id prohlížeče — obnov prosím stránku.",
}


def _err(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def from_own_page(request: Request) -> str | None:
    """None = v pořádku; jinak kód důvodu (REFUSALS). Prohlížeč posílá u POSTu Origin —
    musí být stejný jako adresa, na kterou požadavek přišel; formulář cizí stránky
    neumí poslat JSON a `fetch` cizí stránky s JSON neprojde bez svolení (CORS)."""
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/json":
        return "not_json"
    site = request.headers.get("sec-fetch-site", "")
    if site and site not in ("same-origin", "none"):
        return "cross_site"
    origin = request.headers.get("origin")
    if origin is not None:
        host = identity.origin_from_host_header(request.headers.get("host"))
        mine = identity.origin_from_host_header(origin[7:]) if origin.startswith("http://") else ""
        if not mine or mine != host:
            return "origin"
    return None


def current(app: object, data: dict, path: str = "") -> None:
    """Požadavek se starým id (účet se mezitím spojil jinde, stránka ještě běží)
    vyřídit za platný účet — ať z jednoho člověka nejsou dva. Původní id nechá
    v `_client_was` (sync podle něj člověku řekne, co se stalo)."""
    idn = getattr(app, "identity", None)
    cid = data.get("client")
    if idn is None or not isinstance(cid, str) or not cid:
        return
    now = idn.resolve(cid)
    if now != cid:
        data["_client_was"], data["client"] = cid, now
        if not idn.quiet("stale:" + cid, 300.0):
            telemetry.event("identity.stale", tag=tag_of(now), was=tag_of(cid), path=path or None)


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

    def refuse(request: Request, why: str, status: int = 403) -> JSONResponse:
        c = _client(request)
        if ident().allow("refused:" + c["ip"], 10):
            telemetry.event("identity.refused", path=request.url.path, why=why,
                            host=telemetry.clip(request.headers.get("host", ""), 80), **c)
        return _err(REFUSALS[why], status)

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
            return refuse(request, bad)
        idn = ident()
        idn.origins.ensure_started()
        data = await srv._body(request)
        local = clean_cid(data.get("_client_was") or data.get("client"))
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
        kind = (note or {}).get("kind")
        if kind == "merged":
            srv.poke()
        if winner:
            known = idn.known(winner)
            how = ("moved" if data.get("_client_was") else "merged" if kind == "merged" else
                   "cookie" if kind == "adopted" else "same" if known else "new")
            if how not in ("same", "new") or not idn.quiet(f"sync:{winner}:{here}"):
                telemetry.event("identity.sync", how=how, tag=tag_of(winner), here=here or None,
                                cookie=bool(cookie), known=known, linked=len(idn.linked.get(winner, [])),
                                **_client(request))
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
            return refuse(request, bad)
        idn = ident()
        here = here_of(request, idn.origins.list())
        if not here:
            return refuse(request, "unknown_address")
        data = await srv._body(request)
        cid = clean_cid(data.get("client")) or clean_cid(request.cookies.get(identity.COOKIE))
        code = idn.offer(cid, here, identity.clean_extras(data))
        account = bool(idn.codes[code]["cid"])
        telemetry.event("identity.offer", here=here, account=account,
                        tag=tag_of(idn.codes[code]["cid"]) if account else None, **_client(request))
        return JSONResponse({"code": code}, headers={"Cache-Control": "no-store"})

    async def redeem(request: Request) -> Response:
        bad = from_own_page(request)
        if bad:
            return refuse(request, bad)
        idn = ident()
        here = here_of(request, idn.origins.list())
        if not here:
            return refuse(request, "unknown_address")
        data = await srv._body(request)
        local = clean_cid(data.get("_client_was") or data.get("client"))
        if not local:
            return refuse(request, "no_client", 400)
        codes = data.get("codes")
        out = idn.redeem(local, codes if isinstance(codes, list) else [], here=here)
        stats = out.pop("stats")
        kind = (out.get("note") or {}).get("kind")
        telemetry.event("identity.redeem", tag=tag_of(out["client"]), here=here, note=kind,
                        known=idn.known(out["client"]), **stats, **_client(request))
        if kind:
            srv.poke()
        return reply(idn, request, out, out["client"])

    async def report(request: Request) -> Response:
        bad = from_own_page(request)
        if bad:
            return refuse(request, bad)
        idn = ident()
        c = _client(request)
        if not idn.allow("page:" + c["ip"]):
            return Response(status_code=429)
        data = await srv._body(request)
        own = idn.origins.list()
        clean = identity.clean_report(data, own)
        if clean is None:
            return Response(status_code=400)
        cid = clean_cid(data.get("client"))
        telemetry.event("identity.page", tag=tag_of(cid) if cid else None, here=here_of(request, own) or None,
                        **clean, **c)
        return Response(status_code=204)

    # ---- správce: přehled účtů a spojení jmenovaných účtů (F-NICK-12) ----

    async def admin_accounts(request: Request) -> Response:
        idn = ident()
        rows = idn.accounts(str(request.query_params.get("nick") or "")[:40])
        telemetry.event("identity.admin_list", n=len(rows), **_client(request))
        return JSONResponse({"accounts": rows}, headers={"Cache-Control": "no-store"})

    async def admin_join(request: Request) -> Response:
        idn = ident()
        try:
            data = await srv._body(request)
        except ValueError as exc:
            return _err(str(exc), 400)
        given = data.get("accounts")
        if not isinstance(given, list) or not 2 <= len(given) <= JOIN_MAX:
            return _err(f"Zadej dva až {JOIN_MAX} účtů (konce jejich id z výpisu).", 400)
        cids: list[str] = []
        for raw in given:
            suffix = str(raw or "").strip().lstrip("…. ")[:64]
            if len(suffix) < identity.SUFFIX_MIN:
                return _err(f"‚{suffix}‘ je moc krátké — zadej aspoň {identity.SUFFIX_MIN} "
                            "poslední znaky id z výpisu.", 400)
            hits = idn.find(suffix)
            if not hits:
                return _err(f"Účet, jehož id končí na ‚{suffix}‘, tu není.", 404)
            if len(hits) > 1:
                rows = sorted((idn.account_row(c) for c in hits), key=lambda r: r["first"])
                return JSONResponse({"error": f"Na ‚{suffix}‘ končí víc účtů — zadej delší konec id.",
                                     "candidates": rows}, status_code=409)
            if hits[0] not in cids:
                cids.append(hits[0])
        if len(cids) < 2:
            return JSONResponse({"error": "Zadané konce id vedou na jeden a tentýž účet — není co spojovat.",
                                 "candidates": [idn.account_row(c) for c in cids]}, status_code=409)
        confirm = data.get("confirm") is True
        out = idn.join(cids) if confirm else idn.plan(cids)
        out["done"] = bool(out.get("done"))
        telemetry.event("identity.admin_join", dry=not out["done"], kept=out["kept"]["id"],
                        joined=[r["id"] for r in out["joined"]],
                        votes=sum(r["moved"] for r in out["joined"]),
                        conflicts=sum(r["conflicts"] for r in out["joined"]),
                        imports=sum(r["imports"] for r in out["joined"]),
                        wishes=sum(r["wishes"] for r in out["joined"]), **_client(request))
        if out["done"]:
            log.info("správce spojil účty: %s → %s", ", ".join(r["id"] for r in out["joined"]),
                     out["kept"]["id"])
            srv.poke()
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    return [
        Route("/api/identity/admin/accounts", _safe(srv._admin_only(admin_accounts)), methods=["GET"]),
        Route("/api/identity/admin/join", _safe(srv._admin_only(admin_join)), methods=["POST"]),
        Route("/api/identity/sync", _safe(sync), methods=["POST"]),
        Route("/api/identity/hello", _safe(hello), methods=["GET"]),
        Route("/api/identity/offer", _safe(offer), methods=["POST"]),
        Route("/api/identity/redeem", _safe(redeem), methods=["POST"]),
        Route("/api/identity/report", _safe(report), methods=["POST"]),
        Route("/identity", _safe(srv._manual), methods=["GET"]),
        Route("/identity.js", _safe(srv._manual), methods=["GET"]),
    ]
