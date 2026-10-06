"""Tentýž člověk na všech adresách jukeboxu (F-NICK-07 až F-NICK-10, POZADAVKY #69).

Server: seznam vlastních adres bere ze stroje (ne z hlavičky Host), cookie
přenese účet mezi porty, jednorázové kódy mezi jmény, cizí stránky nic
nepřepíšou, dva účty se spojí do staršího i s hlasy. Stránka (identity.js
spuštěná v Node nad malým modelem prohlížeče s několika adresami): stejné
jméno a jiný port, jiná adresa dosažitelná a nedosažitelná, dva dřívější
účty, zavřené úložiště, prohlížeč, který most nepustí — a nikdy navigace na
adresu, která neodpověděla. Skutečný prohlížeč: tests/identity_shots.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

_TMP = tempfile.mkdtemp(prefix="ytdj-identity-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)
os.environ.setdefault("YTDJ_EVENTS_FILE", str(Path(_TMP) / "events.jsonl"))

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_issues as ti  # noqa: E402  (WebServer nad malou aplikací, bez hudby)
from ytdj import identity, votes  # noqa: E402
from ytdj.music.catalog import Track  # noqa: E402
from ytdj.votes import SONG, ImportItem, PlaylistImport  # noqa: E402

NODE = shutil.which("node")
STATIC = ROOT / "ytdj" / "web" / "static"
A, A80, B, IP = "http://ytdj.local:8765", "http://ytdj.local", "http://jukebox.local", "http://10.0.0.5:8765"
OWN = (A, A80, B, IP)
OLD, NEW, BLANK = "web-old-account-1", "web-new-account-2", "web-blank-000003"
DAMA, POHODA, ZPRAVA = (Track("dama0000001", "Malá dáma", "Kabát"), Track("pohoda00001", "Pohoda", "Kabát"),
                        Track("zprava00002", "Jasná zpráva", "Olympic"))


class Mono:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def make(tmp: Path | None = None):
    rig = ti.make(tmp)
    rig.votes = votes.wire(rig.app)
    rig.votes.load([])
    rig.mono2 = Mono()
    nicks_path = rig.app.wishes.nicks.path
    idn = identity.Identity(rig.app, Path(tmp) / "identity.json" if tmp else None, mono=rig.mono2)
    idn.origins = identity.OwnOrigins(8765, alias="", hostname="", own_ips=lambda: [], extra=OWN)
    idn.origins.ensure_started = lambda: None  # v testech žádné vlákno na pozadí
    rig.app.identity = rig.idn = idn
    del nicks_path
    return rig


def account(rig, cid: str, nick: str, days_ago: float, *cast) -> None:
    """Účet s přezdívkou a hlasy, první hlas před `days_ago` dny."""
    rig.app.wishes.nicks.set(cid, nick)
    now = time.time()
    rig.votes.clock = lambda: now - days_ago * 86400
    for track, vote in cast:
        rig.votes.cast(SONG, cid, vote, nick, track=track)
    rig.votes.clock = time.time


def call(rig, method: str, path: str, body=None, host: str = "ytdj.local:8765", origin: str | None = "",
         cookie: str = "", ctype: str = "application/json", extra: dict | None = None):
    """Jeden požadavek celou aplikací; `origin=""` = jako stránka téže adresy, None = bez hlavičky."""
    headers = [(b"host", host.encode()), (b"user-agent", b"Mozilla/5.0 (X11; Linux) Chrome/130")]
    if origin == "":
        origin = "http://" + host
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if cookie:
        headers.append((b"cookie", f"{identity.COOKIE}={cookie}".encode()))
    data = json.dumps(body).encode() if body is not None else b""
    if data:
        headers.append((b"content-type", ctype.encode()))
    for k, v in (extra or {}).items():
        headers.append((k.encode(), v.encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "http", "path": path, "raw_path": path.encode(), "root_path": "", "query_string": b"",
             "headers": headers, "client": ("10.0.0.7", 40000), "server": ("test", 8765)}
    out = SimpleNamespace(status=0, headers={}, cookies=[], body=b"")
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": data, "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out.status = msg["status"]
            for k, v in msg["headers"]:
                if k == b"set-cookie":
                    out.cookies.append(v.decode())
                out.headers[k.decode()] = v.decode()
        elif msg["type"] == "http.response.body":
            out.body += msg.get("body", b"")

    asyncio.run(rig.srv._starlette(scope, receive, send))
    try:
        out.json = json.loads(out.body) if out.body else {}
    except ValueError:
        out.json = {}
    return out


class OwnAddresses(unittest.TestCase):
    """F-NICK-08: mezi kterými adresami se účet smí předat, určuje stroj, ne návštěvník."""

    def origins(self, port80: bool):
        return identity.OwnOrigins(
            8765, alias="jukebox.local", hostname="box", own_ips=lambda: ["10.0.0.5", "10.0.1.9"],
            points_here=lambda name, ips: name in ("jukebox.local", "box.local"),
            answers=lambda host, port: port80 and port == 80)

    def test_list_is_built_from_the_machine(self):
        own = self.origins(port80=True).look()
        self.assertEqual(own[:4], ("http://jukebox.local", "http://jukebox.local:8765",
                                   "http://box.local", "http://box.local:8765"))
        self.assertIn("http://10.0.0.5", own)
        self.assertIn("http://10.0.1.9:8765", own)
        # bez přesměrování portu 80 jen adresy s portem webu
        plain = self.origins(port80=False).look()
        self.assertEqual([o for o in plain if not o.endswith(":8765")], [])
        # jméno, které na tomhle stroji nevede zpátky na něj, do seznamu nepatří
        other = identity.OwnOrigins(8765, alias="jukebox.local", hostname="box", own_ips=lambda: ["10.0.0.5"],
                                    points_here=lambda name, ips: name == "box.local",
                                    answers=lambda h, p: False).look()
        self.assertEqual(other, ("http://box.local:8765", "http://10.0.0.5:8765"))

    def test_a_name_must_resolve_to_this_machine(self):
        import socket
        real = socket.getaddrinfo
        table = {"mine.local": "10.0.0.5", "loop.local": "127.0.1.1", "other.local": "10.0.0.99"}

        def fake(name, *a, **kw):
            if name not in table:
                raise OSError("neznámé jméno")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[name], 0))]

        socket.getaddrinfo = fake
        try:
            self.assertTrue(identity.points_here("mine.local", ["10.0.0.5"]))
            self.assertTrue(identity.points_here("loop.local", ["10.0.0.5"]))
            self.assertFalse(identity.points_here("other.local", ["10.0.0.5"]))  # jiný stroj v síti
            self.assertFalse(identity.points_here("nikde.local", ["10.0.0.5"]))
        finally:
            socket.getaddrinfo = real

    def test_host_header_alone_proves_nothing(self):
        rig = make()
        account(rig, OLD, "Robert", 20, (DAMA, 1))
        # adresa, kterou stroj nezná (cizí jméno ukazující na jukebox): žádné předávání
        r = call(rig, "POST", "/api/identity/sync", {"client": OLD}, host="evil.example:8765")
        self.assertEqual((r.status, r.json["here"], r.json["origins"]), (200, "", []))
        for path, body in (("/api/identity/offer", {"client": OLD}),
                           ("/api/identity/redeem", {"client": BLANK, "codes": []})):
            self.assertEqual(call(rig, "POST", path, body, host="evil.example:8765").status, 403, path)
        # vlastní adresa: seznam dostane a ví, která z nich je tahle
        r = call(rig, "POST", "/api/identity/sync", {"client": OLD}, host="jukebox.local")
        self.assertEqual((r.json["here"], tuple(r.json["origins"])), (B, OWN))
        self.assertEqual(identity.origin_from_host_header("JUKEBOX.local:80"), B)
        self.assertEqual(identity.origin_from_host_header("a/b"), "")

    def test_looking_never_blocks_the_caller(self):
        slow = identity.OwnOrigins(8765, alias="", hostname="", own_ips=lambda: (time.sleep(0.4), ["10.0.0.5"])[1],
                                   answers=lambda h, p: False)
        identity.LOOK_SCHEDULE, keep = (0.0,), identity.LOOK_SCHEDULE
        try:
            t0 = time.perf_counter()
            slow.ensure_started()
            slow.ensure_started()
            self.assertLess(time.perf_counter() - t0, 0.1)
            self.assertEqual(slow.list(), ())  # zatím nic — seznam se doplní sám
            for _ in range(60):
                if slow.list():
                    break
                time.sleep(0.05)
            self.assertEqual(slow.list(), ("http://10.0.0.5:8765",))
        finally:
            identity.LOOK_SCHEDULE = keep
        src = (ROOT / "ytdj/web/identity_api.py").read_text(encoding="utf-8")
        self.assertIn("idn.origins.ensure_started()", src)
        self.assertNotIn(".look(", src)  # zjišťování adres nikdy v obsluze požadavku


class SamePlaceOtherPort(unittest.TestCase):
    """F-NICK-07: stejné jméno, jiný port — účet nese cookie."""

    def test_cookie_carries_the_account_to_another_port(self):
        rig = make()
        account(rig, OLD, "Robert", 20, (DAMA, 1))
        r = call(rig, "POST", "/api/identity/sync", {"client": OLD})
        self.assertEqual((r.status, r.json["client"], r.json["nick"], r.json["known"]), (200, OLD, "Robert", True))
        cookie = r.cookies[0]
        self.assertTrue(cookie.startswith(f"{identity.COOKIE}={OLD};"))
        low = cookie.lower()
        for part in ("httponly", "samesite=lax", "path=/", f"max-age={identity.COOKIE_AGE}"):
            self.assertIn(part, low)
        self.assertNotIn("secure", low)  # web je http — Secure by cookie zahodilo
        self.assertNotIn("domain", low)  # jen pro tohle jméno
        # tentýž prohlížeč na portu 80: úložiště prázdné (nové id), cookie má
        r = call(rig, "POST", "/api/identity/sync", {"client": BLANK}, host="ytdj.local", cookie=OLD)
        self.assertEqual((r.json["client"], r.json["nick"]), (OLD, "Robert"))
        self.assertEqual(r.json["note"], {"kind": "adopted", "kept": "Robert"})
        self.assertEqual(rig.app.wishes.nicks.get(BLANK), "")  # z prázdného id účet nevznikl

    def test_new_browser_gets_no_account_from_nothing(self):
        rig = make()
        r = call(rig, "POST", "/api/identity/sync", {"client": BLANK})
        self.assertEqual((r.json["client"], r.json["nick"], r.json["known"], r.json["note"]), (BLANK, "", False, None))
        r = call(rig, "POST", "/api/identity/sync", {"client": ""})  # mezistránka na adrese bez účtu
        self.assertEqual((r.json["client"], r.cookies), ("", []))


class ForeignPages(unittest.TestCase):
    """F-NICK-08: cizí stránka v síti nic nepřepíše ani nevyláká."""

    def test_foreign_pages_are_refused(self):
        rig = make()
        account(rig, OLD, "Robert", 20, (DAMA, 1))
        bodies = {"/api/identity/sync": {"client": NEW}, "/api/identity/offer": {"client": OLD},
                  "/api/identity/redeem": {"client": NEW, "codes": []}}
        for path, body in bodies.items():
            with self.subTest(path=path):
                self.assertEqual(call(rig, "POST", path, body, origin="http://evil.example").status, 403)
                self.assertEqual(call(rig, "POST", path, body, origin=B).status, 403)  # i jiná vlastní adresa
                self.assertEqual(call(rig, "POST", path, body, ctype="text/plain").status, 403)  # formulář
                self.assertEqual(call(rig, "POST", path, body, extra={"sec-fetch-site": "cross-site"}).status, 403)
                self.assertEqual(call(rig, "POST", path, body).status, 200)
        self.assertEqual(rig.idn.alias, {})
        # ověření dosažitelnosti smí číst jen stránka z vlastní adresy
        ok = call(rig, "GET", "/api/identity/hello", origin=B)
        self.assertEqual(ok.headers.get("access-control-allow-origin"), B)
        self.assertEqual(ok.json, {"jukebox": rig.idn.instance})
        self.assertNotIn("access-control-allow-origin",
                         call(rig, "GET", "/api/identity/hello", origin="http://evil.example").headers)

    def test_codes_are_one_time_short_lived_and_hide_the_id(self):
        rig = make()
        account(rig, OLD, "Robert", 20, (DAMA, 1))
        code = call(rig, "POST", "/api/identity/offer", {"client": OLD}).json["code"]
        self.assertGreaterEqual(len(code), 20)
        self.assertNotIn(OLD, code)
        got = call(rig, "POST", "/api/identity/redeem", {"client": BLANK, "codes": [code]}, host="jukebox.local")
        self.assertEqual((got.status, got.json["client"], got.json["nick"]), (200, OLD, "Robert"))
        self.assertIn(f"{identity.COOKIE}={OLD}", got.cookies[0])
        self.assertEqual(sorted(got.json["linked"]), sorted([A, B]))
        # podruhé už kód nic nedá
        again = call(rig, "POST", "/api/identity/redeem", {"client": "web-somebody-else", "codes": [code]},
                     host="jukebox.local")
        self.assertEqual((again.json["client"], again.json["nick"]), ("web-somebody-else", ""))
        # po dvou minutách také ne
        code = call(rig, "POST", "/api/identity/offer", {"client": OLD}).json["code"]
        rig.mono2.t += identity.CODE_TTL + 1
        late = call(rig, "POST", "/api/identity/redeem", {"client": "web-third-00001", "codes": [code]},
                    host="jukebox.local")
        self.assertEqual(late.json["client"], "web-third-00001")
        # vymyšlený kód nic neudělá a účty ostatních lidí zůstávají, jak byly
        call(rig, "POST", "/api/identity/redeem", {"client": "web-third-00001", "codes": ["x" * 24, 7, None]})
        self.assertEqual(rig.app.wishes.nicks.get(ti.PETR), "Petr")
        self.assertEqual(rig.app.wishes.nicks.get(OLD), "Robert")

    def test_what_travels_with_the_account(self):
        raw = {"theme": "dark", "tokens": {"w1": {"token": "abc", "at": 5}, "w2": "x", "w3": {"token": 5}},
               "pin": "123456", "client": OLD}
        self.assertEqual(identity.clean_extras(raw), {"theme": "dark", "tokens": {"w1": {"token": "abc", "at": 5}}})
        self.assertEqual(identity.clean_extras({"theme": "pink"}), {})
        src = (STATIC / "identity.js").read_text(encoding="utf-8")
        self.assertNotIn("adminPin", src)  # PIN správce se mezi adresami nenosí


class TwoAccounts(unittest.TestCase):
    """F-NICK-09: dva účty jednoho prohlížeče → platí starší, nic se neztratí."""

    def rig(self):
        rig = make()
        account(rig, OLD, "Robert", 20, (DAMA, 1), (ZPRAVA, -1))
        account(rig, NEW, "Robert 2", 0.1, (DAMA, -1), (POHODA, 1), (ZPRAVA, -1))
        account(rig, ti.JANA, "Jana", 5, (ZPRAVA, -1))
        return rig

    def mine(self, rig, cid):
        return {key: b.vote for (target, key), ballots in rig.votes.items.items()
                for voter, b in ballots.items() if voter == cid and b.vote}

    def test_older_account_wins_and_votes_move(self):
        rig = self.rig()
        zprava = rig.votes.song_key_for(ZPRAVA)
        self.assertEqual(rig.votes.tally(SONG, zprava).down, 3)
        imp = PlaylistImport("imp1", NEW, "Robert 2", "PL1", "Můj playlist", time.time() - 100, time.time())
        imp.items["olympic|okno"] = ImportItem("olympic|okno", "okno0000001", "Olympic", "Okno", time.time() - 100)
        rig.votes.put_import(imp)
        wish = SimpleNamespace(cid=NEW, who="Robert 2")
        rig.app.wishes.wishes.append(wish)
        code = call(rig, "POST", "/api/identity/offer", {"client": OLD}).json["code"]
        got = call(rig, "POST", "/api/identity/redeem", {"client": NEW, "codes": [code]}, host="jukebox.local")
        self.assertEqual((got.json["client"], got.json["nick"]), (OLD, "Robert"))
        self.assertEqual(got.json["note"], {"kind": "merged", "dropped": "Robert 2", "kept": "Robert"})
        mine = self.mine(rig, OLD)
        self.assertEqual(mine[rig.votes.song_key_for(POHODA)], 1)   # hlas mladšího účtu přešel
        self.assertEqual(mine[rig.votes.song_key_for(DAMA)], 1)     # hlasovaly oba → platí starší
        self.assertEqual(mine["olympic|okno"], 1)                   # 👍 z playlistu mladšího účtu
        self.assertEqual(self.mine(rig, NEW), {})
        self.assertEqual(rig.votes.imports["imp1"].client, OLD)
        # jeden člověk = jeden hlas: ze tří 👎 (dva byly od téhož člověka) jsou dva
        self.assertEqual(rig.votes.tally(SONG, zprava).down, 2)
        self.assertEqual((wish.cid, wish.who), (OLD, "Robert"))     # čekající přání patří staršímu účtu
        self.assertEqual(rig.app.wishes.nicks.get(NEW), "")
        self.assertEqual(rig.idn.resolve(NEW), OLD)
        self.assertEqual(self.mine(rig, ti.JANA), {zprava: -1})     # cizího účtu se nic nedotklo
        # zápis do databáze šel přes Store (hlasy mladšího účtu staženy, staršímu připsány)
        rig.store.flush() if hasattr(rig.store, "flush") else None
        rows = {(key, voter): vote for target, key, voter, vote, *_ in rig.store.all_votes()}
        self.assertEqual(rows[(rig.votes.song_key_for(POHODA), OLD)], 1)
        self.assertEqual(rows[(rig.votes.song_key_for(POHODA), NEW)], 0)

    def test_same_name_other_port_merges_too(self):
        rig = self.rig()
        r = call(rig, "POST", "/api/identity/sync", {"client": NEW}, host="ytdj.local", cookie=OLD)
        self.assertEqual((r.json["client"], r.json["note"]["kind"], r.json["note"]["dropped"]),
                         (OLD, "merged", "Robert 2"))
        self.assertIn(f"{identity.COOKIE}={OLD}", r.cookies[0])

    def test_blank_id_never_wins_and_order_does_not_matter(self):
        for order in ((BLANK, OLD, NEW), (NEW, BLANK, OLD), (OLD, NEW)):
            with self.subTest(order=order):
                rig = self.rig()
                winner, _ = rig.idn.merge(order)
                self.assertEqual(winner, OLD)
        rig = self.rig()
        self.assertEqual(rig.idn.merge([BLANK, "web-blank-000004"]), (BLANK, None))  # dvě prázdná id: žádný účet
        self.assertEqual(rig.idn.merge([]), ("", None))

    def test_an_address_still_holding_the_dropped_id_learns_it_once(self):
        rig = self.rig()
        rig.idn.merge([OLD, NEW])  # spojeno jinde (třeba na druhé adrese)
        r = call(rig, "POST", "/api/identity/sync", {"client": NEW}, host="jukebox.local")
        self.assertEqual((r.json["client"], r.json["nick"]), (OLD, "Robert"))
        self.assertEqual(r.json["note"], {"kind": "merged", "dropped": "Robert 2", "kept": "Robert"})
        r = call(rig, "POST", "/api/identity/sync", {"client": OLD}, host="jukebox.local", cookie=OLD)
        self.assertIsNone(r.json["note"])

    def test_kept_on_the_card_by_a_thread(self):
        tmp = ti.tmpdir()
        rig = make(tmp)
        account(rig, OLD, "Robert", 20, (DAMA, 1))
        account(rig, NEW, "Robert 2", 1, (POHODA, 1))
        main = threading.current_thread().name
        seen = []
        real = rig.idn._write
        rig.idn._write = lambda gen, text: (seen.append(threading.current_thread().name), real(gen, text))
        self.assertTrue(rig.idn.wait_loaded())
        rig.idn.merge([NEW, OLD])
        rig.idn.flush()
        self.assertTrue(seen and main not in seen)
        again = identity.Identity(rig.app, tmp / "identity.json")
        self.assertTrue(again.wait_loaded())  # i čtení jde ve vlákně; smyčka ho jen převezme
        self.assertEqual((again.resolve(NEW), again.gone), (OLD, {NEW: "Robert 2"}))


# Malý model prohlížeče s několika adresami (každá má vlastní úložiště, jména sdílejí cookie
# mezi porty) a serveru se stejnými odpověďmi jako ytdj/web/identity_api.py. Scénář jde jako JSON.
HARNESS = r"""
const fs = require("fs"), vm = require("vm"), { webcrypto } = require("crypto");
const src = fs.readFileSync(process.argv[2], "utf8"), sc = JSON.parse(process.argv[3]);
const own = sc.own, accounts = sc.accounts || {}, alias = {}, linked = sc.linked || {}, cookies = sc.cookies || {};
const store = sc.storage || {}, session = {}, codes = {}, calls = [], trace = [], results = [];
let nCode = 0;
const hostOf = (o) => o.replace(/^http:\/\//, "").replace(/:\d+$/, "");
const resolve = (c) => { while (alias[c]) c = alias[c]; return c; };
function merge(ids) {
  const raw = ids[0], order = [];
  ids.map(resolve).forEach((c) => { if (c && order.indexOf(c) < 0) order.push(c); });
  if (!order.length) return ["", null];
  const acc = order.filter((c) => accounts[c]);
  const winner = acc.length ? acc.slice().sort((x, y) => accounts[x].age - accounts[y].age)[0] : order[0];
  let note = null;
  order.forEach((c) => {
    if (c === winner) return;
    if (accounts[c] && c === order[0]) note = { kind: "merged", dropped: accounts[c].nick, kept: accounts[winner].nick };
    alias[c] = winner;
  });
  if (!note && accounts[winner] && raw !== winner) note = { kind: "adopted", kept: accounts[winner].nick };
  return [winner, note];
}
function me(origin, cid, note, extra) {
  const here = own.indexOf(origin) >= 0 ? origin : "";
  return Object.assign({ client: cid, nick: (accounts[cid] || {}).nick || "", tag: cid ? "tag-" + cid : "",
    known: !!accounts[cid], note: note, here: here, origins: here ? own : [], linked: linked[cid] || [],
    jukebox: "J1" }, extra || {});
}
function serve(origin, path, body) {
  calls.push({ origin: origin, path: path, body: body });
  const host = hostOf(origin);
  if (path === "/api/identity/sync") {
    const [w, note] = merge([body.client, cookies[host]].filter(Boolean));
    if (w) cookies[host] = w;
    return me(origin, w, note);
  }
  if (path === "/api/identity/offer") {
    const code = "code" + String(++nCode).padStart(20, "0");
    const cid = resolve(body.client || "");
    codes[code] = { cid: accounts[cid] ? cid : "", origin: origin, extras: { theme: body.theme, tokens: body.tokens } };
    return { code: code };
  }
  if (path === "/api/identity/redeem") {
    const ids = [body.client], from = [origin], extras = {};
    (body.codes || []).forEach((c) => { const r = codes[c]; delete codes[c]; if (!r) return; from.push(r.origin);
      if (r.cid) { ids.push(r.cid); if (r.extras.theme) extras.theme = r.extras.theme;
        if (r.extras.tokens) extras.tokens = r.extras.tokens; } });
    const [w, note] = merge(ids);
    linked[w] = (linked[w] || []).concat(from.filter((o) => (linked[w] || []).indexOf(o) < 0));
    cookies[host] = w;
    return me(origin, w, note, { extras: extras });
  }
  throw new Error("neznámá cesta " + path);
}
function storage(bag, broken) {
  if (broken) { const boom = () => { throw new Error("SecurityError"); }; return { getItem: boom, setItem: boom, removeItem: boom }; }
  return { getItem: (k) => (k in bag ? bag[k] : null), setItem: (k, v) => { bag[k] = String(v); }, removeItem: (k) => { delete bag[k]; } };
}
async function load(url) {
  const m = /^(http:\/\/[^\/#]+)([^#]*)(#.*)?$/.exec(url), origin = m[1], path = m[2] || "/", hash = m[3] || "";
  if (!sc.reachable[origin]) { trace.push("UNREACHABLE " + url); return null; }
  trace.push(origin + path + (hash ? "#" + hash.slice(1).split("&")[0] : ""));
  const broken = (sc.brokenStorage || []).indexOf(origin) >= 0;
  store[origin] = store[origin] || {}; session[origin] = session[origin] || {};
  let leave = null, left;
  const gone = new Promise((r) => { left = r; });
  const listeners = [];
  const ctx = {
    location: { origin: origin, href: url, pathname: path, hash: hash,
                replace: (u) => { leave = /^http:/.test(u) ? u : origin + u; left("left"); } },
    localStorage: storage(store[origin], broken), sessionStorage: storage(session[origin], broken),
    document: { getElementById: () => ({ textContent: "", hidden: true }), visibilityState: "visible" },
    crypto: webcrypto, setTimeout: setTimeout, clearTimeout: clearTimeout, AbortController: AbortController,
    fetch: (u, opts) => new Promise((ok, fail) => {
      const cross = /^http:/.test(u), target = cross ? /^(http:\/\/[^\/]+)/.exec(u)[1] : origin;
      const p = cross ? u.slice(target.length) : u;
      if (cross && (sc.bridgeBlocked || !sc.reachable[target])) return fail(new TypeError("Failed to fetch"));
      if (cross && ((sc.deadFrom || {})[origin] || []).indexOf(target) >= 0) return fail(new TypeError("Failed to fetch"));
      if (sc.serverDown) return fail(new TypeError("Failed to fetch"));
      if (p === "/api/identity/hello") { calls.push({ origin: origin, path: "hello " + target });
        return ok({ ok: true, json: async () => ({ jukebox: (sc.otherJukebox || []).indexOf(target) >= 0 ? "J2" : "J1" }) }); }
      try { const d = serve(target, p, JSON.parse(opts.body)); ok({ ok: true, json: async () => d }); }
      catch (e) { ok({ ok: false, status: 500, json: async () => ({}) }); }
    }),
  };
  ctx.window = { addEventListener: (name, fn) => listeners.push(fn) };
  vm.runInNewContext(src, ctx);
  if (sc.interact) listeners.forEach((fn) => fn({}));
  const api = ctx.window.ytdjIdentity;
  const res = await Promise.race([api.ready || api.done, gone]);
  if (res !== "left") results.push({ url: origin + path, res: res, note: api.ready ? api.note(res) : undefined });
  await new Promise((r) => setTimeout(r, 30));  // doběhnout cestu na pozadí (účet tu je)
  return leave;
}
(async () => {
  for (const start of sc.visits) {
    let url = start, hops = 0;
    while (url && hops++ < 25) url = await load(url);
    trace.push("--");
  }
  console.log(JSON.stringify({ trace: trace, results: results, storage: store, calls: calls, cookies: cookies, linked: linked }));
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(2); });
"""


@unittest.skipUnless(NODE, "identity.js se zkouší v Node (není nainstalovaný)")
class PageLogic(unittest.TestCase):
    """identity.js opravdu spuštěná: kdy a kudy se vydá, a kdy se nevydá nikam."""

    PETR = {"web-petr-0000001": {"nick": "Petr", "age": 100}}

    @classmethod
    def setUpClass(cls):
        cls.harness = Path(_TMP) / "identity_harness.js"
        cls.harness.write_text(HARNESS, encoding="utf-8")

    def run_js(self, **sc) -> dict:
        sc.setdefault("own", list(OWN))
        sc.setdefault("reachable", {o: True for o in OWN})
        r = subprocess.run([NODE, str(self.harness), str(STATIC / "identity.js"), json.dumps(sc)],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual([t for t in out["trace"] if t.startswith("UNREACHABLE")], [], out["trace"])
        return out

    def petr_at(self, origin: str) -> dict:
        return {origin: {"ytdj.client": "web-petr-0000001", "ytdj.nick": "Petr", "ytdj.theme": "dark",
                         "ytdj.tokens": json.dumps({"w7": {"token": "tok", "at": 1}})}}

    def test_same_name_other_port_needs_no_trip(self):
        out = self.run_js(accounts=self.PETR, cookies={"ytdj.local": "web-petr-0000001"},
                          linked={"web-petr-0000001": list(OWN)}, visits=[A80 + "/"])
        self.assertEqual(out["trace"], [A80 + "/", "--"])
        res = out["results"][0]["res"]
        self.assertEqual((res["client"], res["nick"], res["changed"]), ("web-petr-0000001", "Petr", True))
        self.assertEqual(out["storage"][A80]["ytdj.nick"], "Petr")
        self.assertIn("Poznali jsme tě", out["results"][0]["note"])

    def test_other_address_reachable_brings_the_account(self):
        out = self.run_js(accounts=self.PETR, storage=self.petr_at(A), visits=[B + "/", B + "/", IP + "/"],
                          reachable={A: True, B: True, IP: True, A80: False})
        first = out["trace"][: out["trace"].index("--")]
        self.assertEqual(first[0], B + "/")
        self.assertEqual(first[-2:], [B + "/identity#take", B + "/"])
        self.assertIn(A + "/identity#give", first)
        got = out["storage"][B]
        self.assertEqual((got["ytdj.client"], got["ytdj.nick"], got["ytdj.who"]), ("web-petr-0000001", "Petr", "Petr"))
        self.assertEqual(got["ytdj.theme"], "dark")                    # vzhled jde s účtem
        self.assertEqual(json.loads(got["ytdj.tokens"])["w7"]["token"], "tok")  # i značky čekajících přání
        back = [r for r in out["results"] if r["url"] == B + "/"][0]
        self.assertEqual(back["res"]["nick"], "Petr")                  # stránka se na přezdívku neptá
        self.assertIn("Poznali jsme tě", back["note"])
        # id klienta nikdy v adrese; kódy jednorázové
        offers = [c for c in out["calls"] if c["path"] == "/api/identity/offer"]
        self.assertTrue(offers)
        # druhá návštěva: žádná další cesta; třetí adresa (IP) si účet vyzvedne sama jednou
        second = out["trace"][out["trace"].index("--") + 1:]
        self.assertEqual(second[:2], [B + "/", "--"])
        self.assertEqual(out["storage"][IP]["ytdj.client"], "web-petr-0000001")

    def test_other_address_unreachable_is_never_visited(self):
        out = self.run_js(accounts=self.PETR, storage=self.petr_at(A), visits=[B + "/", B + "/"],
                          reachable={A: False, A80: False, B: True, IP: False})
        self.assertEqual(out["trace"], [B + "/", "--", B + "/", "--"])
        res = out["results"][0]["res"]
        self.assertEqual((res["nick"], res["changed"]), ("", False))   # nový prohlížeč → stránka se zeptá
        probes = [c["path"] for c in out["calls"] if c["path"].startswith("hello")]
        self.assertEqual(probes, [])  # nedosažitelné adresy ani neodpověděly; podruhé se hned neptá znovu
        self.assertEqual(sorted(json.loads(out["storage"][B]["ytdj.id.miss"])), sorted([A, A80, IP]))

    def test_address_that_dies_mid_way_is_skipped(self):
        # IP odpoví stránce B, ale z mezistránky na A už ne → cesta ji vynechá a vrátí se
        sc = dict(accounts=self.PETR, storage=self.petr_at(A), visits=[B + "/"],
                  reachable={A: True, B: True, IP: True, A80: False})
        ok = self.run_js(**sc)
        self.assertIn(IP + "/identity#give", ok["trace"])
        out = self.run_js(**sc, deadFrom={A: [IP]})
        self.assertNotIn(IP + "/identity#give", out["trace"])
        self.assertEqual(out["trace"][-3:], [B + "/identity#take", B + "/", "--"])
        self.assertEqual(out["storage"][B]["ytdj.client"], "web-petr-0000001")
        # na adrese odpovídá něco jiného než tenhle jukebox → jako by neodpovídala
        out = self.run_js(**sc, otherJukebox=[IP])
        self.assertNotIn(IP + "/identity#give", out["trace"])
        self.assertEqual(out["storage"][B]["ytdj.client"], "web-petr-0000001")

    def test_two_existing_accounts_end_as_the_older_one(self):
        accounts = {"web-old-0000001": {"nick": "Robert", "age": 1}, "web-new-0000002": {"nick": "Robert 2", "age": 9}}
        storage = {A: {"ytdj.client": "web-old-0000001", "ytdj.nick": "Robert"},
                   B: {"ytdj.client": "web-new-0000002", "ytdj.nick": "Robert 2"}}
        out = self.run_js(accounts=accounts, storage=storage, visits=[B + "/", A + "/"],
                          reachable={A: True, B: True, IP: False, A80: False})
        self.assertEqual(out["storage"][B]["ytdj.client"], "web-old-0000001")
        self.assertEqual(out["storage"][B]["ytdj.nick"], "Robert")
        self.assertEqual(out["storage"][A]["ytdj.client"], "web-old-0000001")
        notes = [r["note"] for r in out["results"] if r.get("note")]
        self.assertEqual(len(notes), 1)  # řekne to jednou, jednou větou
        self.assertIn("druhý účet „Robert 2“", notes[0])
        self.assertIn("starším účtem „Robert“", notes[0])
        second = out["trace"][out["trace"].index("--") + 1:]
        self.assertEqual(second, [A + "/", "--"])  # adresa staršího účtu už nikam nechodí

    def test_storage_blocked_falls_back_quietly(self):
        out = self.run_js(accounts=self.PETR, storage=self.petr_at(A), visits=[B + "/"], brokenStorage=[B])
        self.assertEqual(out["trace"], [B + "/", "--"])
        self.assertIsNotNone(out["results"][0]["res"])  # stránka jede dál (id jen v paměti)
        # cookie funguje i bez úložiště: stejné jméno, jiný port
        out = self.run_js(accounts=self.PETR, cookies={"ytdj.local": "web-petr-0000001"}, visits=[A80 + "/"],
                          brokenStorage=[A80])
        self.assertEqual(out["trace"], [A80 + "/", "--"])
        self.assertEqual(out["results"][0]["res"]["nick"], "Petr")

    def test_bridge_blocked_by_the_browser_falls_back(self):
        out = self.run_js(accounts=self.PETR, storage=self.petr_at(A), visits=[B + "/"], bridgeBlocked=True)
        self.assertEqual(out["trace"], [B + "/", "--"])
        self.assertEqual(out["results"][0]["res"]["nick"], "")  # nic nenašla → zeptá se jako dřív
        # server bez odpovědi (nebo starší verze): výsledek null, stránka jede jako dřív
        out = self.run_js(accounts=self.PETR, visits=[B + "/"], serverDown=True)
        self.assertEqual((out["trace"], out["results"][0]["res"]), ([B + "/", "--"], None))

    def test_only_the_page_that_started_accepts_and_only_own_addresses(self):
        # podstrčený návrat s cizím kódem: nic nevyzvedne, jde na hlavní stránku
        forged = B + "/identity#take&n=" + "ab" * 16 + "&c=" + "code" + "0" * 20
        out = self.run_js(accounts=self.PETR, storage=self.petr_at(B), visits=[forged],
                          linked={"web-petr-0000001": list(OWN)})
        self.assertEqual([c for c in out["calls"] if c["path"] == "/api/identity/redeem"], [])
        self.assertEqual(out["trace"][:2], [B + "/identity#take", B + "/"])
        self.assertEqual(out["storage"][B]["ytdj.client"], "web-petr-0000001")
        # podstrčené předání na cizí adresu: žádný kód, žádná navigace
        for to in ("http://evil.example", "http://evil.example:8765", B):
            lure = B + "/identity#give&to=" + to.replace(":", "%3A").replace("/", "%2F") + "&n=" + "ab" * 16 + "&r=&c="
            out = self.run_js(accounts=self.PETR, storage=self.petr_at(B), visits=[lure],
                              reachable={**{o: True for o in OWN}, "http://evil.example": True,
                                         "http://evil.example:8765": True})
            self.assertEqual(out["trace"], [B + "/identity#give", "--"], to)
            self.assertEqual([c for c in out["calls"] if c["path"] == "/api/identity/offer"], [], to)
            self.assertEqual(out["results"][0]["res"], "stopped")
        # cizí adresa uprostřed cesty také ne
        lure = B + "/identity#give&to=" + A.replace(":", "%3A").replace("/", "%2F") + "&n=" + "ab" * 16 + \
            "&r=http%3A%2F%2Fevil.example&c="
        out = self.run_js(accounts=self.PETR, storage=self.petr_at(B), visits=[lure],
                          reachable={**{o: True for o in OWN}, "http://evil.example": True})
        self.assertEqual(out["trace"], [B + "/identity#give", "--"])
        src = (STATIC / "identity.js").read_text(encoding="utf-8")
        self.assertNotIn("K.client +", src)  # id klienta se do adresy neskládá

    def test_no_loops_and_no_interrupting(self):
        # účet tu je a člověk už na stránku sáhl → nikam se neodbíhá
        out = self.run_js(accounts=self.PETR, storage=self.petr_at(B), visits=[B + "/"], interact=True)
        self.assertEqual(out["trace"], [B + "/", "--"])
        # po cestě se 10 minut další nezačne, ani kdyby server o srovnání nevěděl
        out = self.run_js(accounts=self.PETR, storage=self.petr_at(B), visits=[B + "/", B + "/", B + "/"])
        first = out["trace"].index("--")
        self.assertGreater(first, 2)
        self.assertEqual(out["trace"][first + 1:], [B + "/", "--", B + "/", "--"])


class Pages(unittest.TestCase):
    """F-NICK-07 / F-NICK-10: stránky skript opravdu používají a bez něj jedou jako dřív."""

    def test_pages_wait_for_the_account_before_asking(self):
        idx = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertEqual(idx.count('<script src="/identity.js"></script>'), 1)
        self.assertLess(idx.index('<script src="/identity.js"></script>'), idx.index('S.client = load("ytdj.client")'))
        start = idx[idx.index("function startMe()"): idx.index("/* connection: SSE")]
        self.assertIn("idReady.then", start)
        self.assertLess(start.index("setNick(r.nick"), start.index("openNick(false"))  # nejdřív účet, pak dotaz
        self.assertIn("|| Promise.resolve(null)", idx)  # bez skriptu jako dřív
        # dotaz na přezdívku při načtení už jen přes startMe (po srovnání účtu)
        self.assertEqual(idx.count('if (!S.nick) openNick(false, load("ytdj.who") || "");'), 2)
        self.assertNotRegex(idx, r"\n  initMe\(\);\n\}\)\(\);")
        hl = (STATIC / "hlaseni.html").read_text(encoding="utf-8")
        self.assertEqual(hl.count('<script src="/identity.js"></script>'), 1)
        self.assertIn("window.ytdjIdentity.ready.then", hl)
        shell = (STATIC / "identity.html").read_text(encoding="utf-8")
        self.assertIn('<script src="/identity.js"></script>', shell)
        self.assertIn('href="/"', shell)  # kdyby se cesta zastavila, vede zpátky na jukebox

    def test_served_packed_with_etag(self):
        rig = make()
        for path, needle in (("/identity.js", "ytdjIdentity"), ("/identity", "identity.js")):
            r = call(rig, "GET", path, origin=None)
            self.assertEqual(r.status, 200, path)
            self.assertIn(needle, r.body.decode())
            self.assertEqual(r.headers["cache-control"], "no-cache")
            again = call(rig, "GET", path, origin=None, extra={"if-none-match": r.headers["etag"]})
            self.assertEqual(again.status, 304)
        self.assertLess(len((STATIC / "identity.js").read_bytes()), 16 * 1024)

    def test_help_page_says_it(self):
        nap = (STATIC / "napoveda.html").read_text(encoding="utf-8")
        sec = nap[nap.index('id="prezdivka"'): nap.index('id="displej"')]
        self.assertIn("jinou adres", sec)
        self.assertIn("tentýž člověk", sec)


if __name__ == "__main__":
    unittest.main()
