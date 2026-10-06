"""Chyby a nápady kolegů (POZADAVKY #52, FUNKCE F-HLASENI-01…).

Vlastník: „Udělat tam issues, aby kolegové mohli ty věci reportovat a
navrhovat, aniž bych to musel přepisovat… všechno tam dej a pak k tomu piš,
jak to bylo vyřešeno."

    YTDJ_EVENTS_FILE=/tmp/ev.jsonl .venv/bin/python -m unittest tests.test_issues -v
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="ytdj-issues-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)
os.environ.setdefault("YTDJ_EVENTS_FILE", str(Path(_TMP) / "events.jsonl"))

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_wishes as tw  # noqa: E402
from ytdj import adminpin, issues, loopwatch, telemetry  # noqa: E402
from ytdj.config import DEFAULTS, Config  # noqa: E402
from ytdj.state import Store  # noqa: E402
from ytdj.web import issues_api  # noqa: E402
from ytdj.web import server as web  # noqa: E402

STATIC = ROOT / "ytdj/web/static"
PAGE = STATIC / "hlaseni.html"
INDEX = STATIC / "index.html"
NAPOVEDA = STATIC / "napoveda.html"
PETR, JANA, KAREL = "client-petr", "client-jana", "client-karel"
PIN = "246813"


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp(dir=_TMP))


def make(tmp: Path | None = None, seed: list[dict] | None = None, store: Store | None = None,
         background: bool = False, seed_path: Path | None = None):
    """WebServer nad malou aplikací: skutečná fronta přání (přezdívky), skutečný
    Store v dočasné složce, PIN správce v ruce testu. Hudba žádná."""
    tmp = tmp or tmpdir()
    if store is None:
        store = Store(tmp / "state.db", background=background)
    wq = tw.WishQueue(SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), None, Config(**DEFAULTS))
    wq.nicks.set(PETR, "Petr")
    wq.nicks.set(JANA, "Jana")
    app = SimpleNamespace(cfg=Config(**DEFAULTS), wishes=wq, store=store,
                          restart_requested=asyncio.Event())
    mono = Clock()
    book = issues.wire(app, seed_path=seed_path, mono=mono)
    book.load(seed=issues.check_seed(seed) if seed is not None else None)
    srv = web.WebServer(app)
    (tmp / "admin-pin").write_text(PIN + "\n", encoding="ascii")
    srv.admin = adminpin.AdminGuard(tmp / "admin-pin", clock=Clock())
    return SimpleNamespace(srv=srv, app=app, book=book, store=store, mono=mono, tmp=tmp)


async def acall(srv, method: str, path: str, body=None, pin: str | None = None, ip: str = "10.0.0.7",
                query: str = "", raw: bytes | None = None, gz: bool = False, etag: str = ""):
    """Jeden požadavek celou aplikací Starlette (směrování, PIN, _safe) bez sítě."""
    headers = [(b"user-agent", b"Mozilla/5.0 (X11; Linux) Chrome/130")]
    if pin is not None:
        headers.append((b"x-ytdj-pin", pin.encode()))
    if gz:
        headers.append((b"accept-encoding", b"gzip"))
    if etag:
        headers.append((b"if-none-match", etag.encode()))
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
    if data:
        headers.append((b"content-type", b"application/json"))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "http", "path": path, "raw_path": path.encode(), "root_path": "",
             "query_string": query.encode(), "headers": headers, "client": (ip, 40000),
             "server": ("test", 80)}
    sent = False
    out = SimpleNamespace(status=0, headers={}, body=b"")

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": data, "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out.status = msg["status"]
            out.headers = {k.decode().lower(): v.decode() for k, v in msg.get("headers", [])}
        elif msg["type"] == "http.response.body":
            out.body += msg.get("body", b"")

    await srv._starlette(scope, receive, send)
    out.json = (lambda: json.loads(out.body))
    return out


def call(rig, method: str, path: str, body=None, **kw):
    return asyncio.run(acall(rig.srv, method, path, body, **kw))


def new(rig, who: str = PETR, kind: str = "chyba", title: str = "Nenašlo to písničku",
        body: str = "Napsal jsem název a nic.", **kw):
    return call(rig, "POST", "/api/issues", {"client": who, "kind": kind, "title": title, "body": body}, **kw)


class Api(unittest.TestCase):
    """F-HLASENI-01, 03, 04: přidat, seznam, filtr, stránky, komentář, +1."""

    def test_create_list_and_detail(self):
        rig = make()
        r = new(rig)
        self.assertEqual(r.status, 201, r.body)
        item = r.json()["item"]
        self.assertEqual((item["id"], item["kind"], item["state"], item["open"]), (1, "chyba", "nove", True))
        self.assertEqual((item["title"], item["body"], item["who"], item["mine"]),
                         ("Nenašlo to písničku", "Napsal jsem název a nic.", "Petr", True))
        self.assertEqual((item["resolution"], item["comment_list"], item["plus"]), ("", [], 0))
        new(rig, JANA, "napad", "Posouvání v písničce", "")
        lst = call(rig, "GET", "/api/issues", query="client=" + JANA).json()
        self.assertEqual([(i["id"], i["who"], i["mine"]) for i in lst["items"]],
                         [(2, "Jana", True), (1, "Petr", False)])
        self.assertEqual(lst["items"][1]["excerpt"], "Napsal jsem název a nic.")
        self.assertNotIn("body", lst["items"][0])  # seznam je lehký, text až v detailu
        self.assertEqual((lst["total"], lst["counts"]["open"], lst["counts"]["kinds"]),
                         (2, 2, {"chyba": 1, "napad": 1}))
        one = call(rig, "GET", "/api/issues/1", query="client=" + JANA)
        self.assertEqual(one.status, 200)
        self.assertEqual((one.json()["item"]["title"], one.json()["item"]["mine"]), ("Nenašlo to písničku", False))
        self.assertEqual(call(rig, "GET", "/api/issues/99").status, 404)
        self.assertEqual(call(rig, "GET", "/api/issues/abc").status, 404)

    def test_identity_is_the_browser_id_and_nick(self):
        rig = make()
        # bez id prohlížeče se nepíše; bez přezdívky jde psát jako Host (nebo pod poslaným jménem)
        r = call(rig, "POST", "/api/issues", {"kind": "chyba", "title": "Bez id"})
        self.assertEqual(r.status, 400)
        self.assertIn("id prohlížeče", r.json()["error"])
        self.assertEqual(new(rig, KAREL).json()["item"]["who"], "Host")
        r = call(rig, "POST", "/api/issues", {"client": "client-eva", "who": "Eva", "kind": "napad",
                                              "title": "Den a noc"})
        self.assertEqual(r.json()["item"]["who"], "Eva")
        # jméno určuje registr přezdívek, ne to, co pošle stránka; změna přezdívky se projeví všude
        r = call(rig, "POST", "/api/issues", {"client": PETR, "who": "Někdo jiný", "kind": "chyba",
                                              "title": "Čí to je"})
        self.assertEqual(r.json()["item"]["who"], "Petr")
        rig.app.wishes.nicks.set(PETR, "Péťa")
        self.assertEqual(call(rig, "GET", "/api/issues/3").json()["item"]["who"], "Péťa")
        # id klienta ven nejde nikdy
        dump = call(rig, "GET", "/api/issues").body.decode() + call(rig, "GET", "/api/issues/3").body.decode()
        self.assertNotIn(PETR, dump)
        self.assertNotIn(KAREL, dump)

    def test_empty_and_too_long_are_rejected(self):
        rig = make()
        for payload, word in (
                ({"kind": "chyba", "title": ""}, "název"),
                ({"kind": "chyba", "title": "  \n "}, "název"),
                ({"kind": "chyba", "title": "ab"}, "název"),
                ({"kind": "chyba", "title": "x" * (issues.TITLE_MAX + 1)}, str(issues.TITLE_MAX)),
                ({"kind": "chyba", "title": "V pořádku", "body": "x" * (issues.BODY_MAX + 1)},
                 str(issues.BODY_MAX)),
                ({"kind": "prani", "title": "V pořádku"}, "Chyba"),
                ({"title": "V pořádku"}, "Chyba"),
                ({"kind": ["chyba"], "title": "V pořádku"}, "Chyba")):
            r = call(rig, "POST", "/api/issues", {"client": PETR, **payload})
            self.assertEqual(r.status, 400, payload)
            self.assertIn(word, r.json()["error"], payload)
        self.assertEqual(call(rig, "POST", "/api/issues", raw=b"{nope").status, 400)
        self.assertEqual(call(rig, "POST", "/api/issues", raw=b"[1]").status, 400)
        self.assertEqual(rig.book.items, {})
        # přesně na hranici projde
        ok = call(rig, "POST", "/api/issues", {"client": PETR, "kind": "napad", "title": "x" * issues.TITLE_MAX,
                                               "body": "y" * issues.BODY_MAX})
        self.assertEqual(ok.status, 201)
        new(rig)
        for body in ("", "   \n\n ", "x" * (issues.COMMENT_MAX + 1)):
            r = call(rig, "POST", "/api/issues/1/comments", {"client": PETR, "body": body})
            self.assertEqual(r.status, 400, body[:5])
        self.assertEqual(call(rig, "POST", "/api/issues/1/comments", {"body": "bez id"}).status, 400)
        self.assertEqual(call(rig, "POST", "/api/issues/77/comments", {"client": PETR, "body": "x"}).status, 404)

    def test_request_body_size_is_limited(self):
        rig = make()
        big = json.dumps({"client": PETR, "kind": "chyba", "title": "Velké",
                          "body": "x" * (issues_api.BODY_LIMIT + 10)}).encode()
        self.assertEqual(call(rig, "POST", "/api/issues", raw=big).status, 413)
        self.assertEqual(rig.book.items, {})

    def test_filter_by_kind_and_state(self):
        rig = make()
        new(rig, PETR, "chyba", "Chyba jedna")
        new(rig, JANA, "napad", "Nápad jedna")
        new(rig, PETR, "napad", "Nápad dva")
        new(rig, JANA, "chyba", "Chyba dva")
        rig.book.admin_update(1, state="vyreseno", resolution="Opraveno.")
        rig.book.admin_update(2, state="resi_se")
        rig.book.admin_update(3, state="zamitnuto")

        def ids(query: str) -> list[int]:
            r = call(rig, "GET", "/api/issues", query=query)
            self.assertEqual(r.status, 200, r.body)
            return [i["id"] for i in r.json()["items"]]

        self.assertEqual(ids(""), [4, 3, 2, 1])
        self.assertEqual(ids("kind=chyba"), [4, 1])
        self.assertEqual(ids("kind=napad"), [3, 2])
        self.assertEqual(ids("state=open"), [4, 2])
        self.assertEqual(ids("state=closed"), [3, 1])
        self.assertEqual(ids("state=vyreseno"), [1])
        self.assertEqual(ids("state=resi_se"), [2])
        self.assertEqual(ids("state=nove"), [4])
        self.assertEqual(ids("kind=napad&state=open"), [2])
        self.assertEqual(ids("kind=chyba&state=zamitnuto"), [])
        self.assertEqual(call(rig, "GET", "/api/issues", query="kind=jine").status, 400)
        self.assertEqual(call(rig, "GET", "/api/issues", query="state=hotovo").status, 400)
        c = call(rig, "GET", "/api/issues", query="kind=chyba").json()["counts"]
        self.assertEqual((c["all"], c["open"], c["closed"]), (4, 2, 2))  # počty jsou vždy přes všechno
        self.assertEqual(c["states"], {"nove": 1, "resi_se": 1, "nasazeni": 0, "vyreseno": 1, "zamitnuto": 1})

    def test_pagination_newest_first(self):
        rig = make()
        clock = Clock(1_790_000_000.0)
        rig.book.clock = clock
        rig.book._rates["create"].limit = 1000
        for i in range(1, 56):
            clock.t += 60
            self.assertEqual(new(rig, PETR, "napad", f"Nápad {i}").status, 201)
        first = call(rig, "GET", "/api/issues").json()
        self.assertEqual(len(first["items"]), issues.PAGE_DEFAULT)
        self.assertEqual((first["items"][0]["id"], first["total"], first["more"]), (55, 55, True))
        seen, offset = [], 0
        while True:
            page = call(rig, "GET", "/api/issues", query=f"offset={offset}&limit=20").json()
            seen += [i["id"] for i in page["items"]]
            offset += len(page["items"])
            if not page["more"]:
                break
        self.assertEqual(seen, list(range(55, 0, -1)))
        # strop stránky a jen počty (pro odznak na hlavní stránce)
        self.assertEqual(len(call(rig, "GET", "/api/issues", query="limit=999").json()["items"]), issues.PAGE_MAX)
        zero = call(rig, "GET", "/api/issues", query="limit=0").json()
        self.assertEqual((zero["items"], zero["counts"]["open"]), ([], 55))
        self.assertEqual(call(rig, "GET", "/api/issues", query="offset=x").status, 400)
        self.assertEqual(call(rig, "GET", "/api/issues", query="offset=500").json()["items"], [])

    def test_comment(self):
        rig = make()
        new(rig)
        r = call(rig, "POST", "/api/issues/1/comments", {"client": JANA, "body": "Mně taky,\nráno v 8."})
        self.assertEqual(r.status, 201, r.body)
        c = r.json()["item"]["comment_list"]
        self.assertEqual([(x["who"], x["body"], x["mine"]) for x in c], [("Jana", "Mně taky,\nráno v 8.", True)])
        call(rig, "POST", "/api/issues/1/comments", {"client": PETR, "body": "Díky."})
        one = call(rig, "GET", "/api/issues/1", query="client=" + PETR).json()["item"]
        self.assertEqual([(x["who"], x["mine"]) for x in one["comment_list"]], [("Jana", False), ("Petr", True)])
        self.assertEqual(call(rig, "GET", "/api/issues").json()["items"][0]["comments"], 2)

    def test_plus_is_one_per_person_and_toggles(self):
        rig = make()
        new(rig)
        plus = lambda who, **b: call(rig, "POST", "/api/issues/1/plus", {"client": who, **b})  # noqa: E731
        r = plus(JANA)
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual((r.json()["item"]["plus"], r.json()["item"]["plus_mine"]), (1, True))
        self.assertEqual(plus(JANA, on=True).json()["item"]["plus"], 1)  # podruhé totéž = pořád jedno
        self.assertEqual(plus(PETR).json()["item"]["plus_by"], ["Jana", "Petr"])
        lst = call(rig, "GET", "/api/issues", query="client=" + KAREL).json()["items"][0]
        self.assertEqual((lst["plus"], lst["plus_mine"]), (2, False))
        r = plus(JANA)  # přepnutí = stažení
        self.assertEqual((r.json()["item"]["plus"], r.json()["item"]["plus_mine"], r.json()["item"]["plus_by"]),
                         (1, False, ["Petr"]))
        self.assertEqual(plus(JANA, on=False).json()["item"]["plus"], 1)
        self.assertEqual(plus(JANA, on=True).json()["item"]["plus"], 2)
        self.assertEqual(call(rig, "POST", "/api/issues/1/plus", {"on": True}).status, 400)  # bez id
        self.assertEqual(call(rig, "POST", "/api/issues/9/plus", {"client": JANA}).status, 404)


class Limits(unittest.TestCase):
    """F-HLASENI-02: brzda na člověka a strop celku."""

    def test_rate_limit_per_client_and_address(self):
        rig = make()
        for i in range(issues.RATE_CREATE):
            self.assertEqual(new(rig, PETR, title=f"Položka {i}").status, 201)
        r = new(rig, PETR, title="Šestá")
        self.assertEqual(r.status, 429)
        self.assertIn("za pár minut", r.json()["error"])
        self.assertEqual(new(rig, JANA, title="Jana smí").status, 201)  # brzda je na člověka
        # komentáře mají vlastní brzdu
        for i in range(issues.RATE_COMMENT):
            self.assertEqual(call(rig, "POST", "/api/issues/1/comments",
                                  {"client": PETR, "body": f"k{i}"}).status, 201)
        self.assertEqual(call(rig, "POST", "/api/issues/1/comments", {"client": PETR, "body": "x"}).status, 429)
        # po okně zase jde
        rig.mono.t += issues.RATE_WINDOW + 1
        self.assertEqual(new(rig, PETR, title="Po pauze").status, 201)
        self.assertEqual(call(rig, "POST", "/api/issues/1/comments", {"client": PETR, "body": "x"}).status, 201)
        # střídání id z jedné adresy brzdu neobejde (strop adresy je 4× vyšší)
        rig.mono.t += issues.RATE_WINDOW + 1
        codes = [new(rig, f"client-bot{i:03d}", title=f"Bot {i}", ip="10.0.0.66").status
                 for i in range(issues.RATE_CREATE * issues.RATE_IP_FACTOR + 1)]
        self.assertEqual(codes[:-1], [201] * (issues.RATE_CREATE * issues.RATE_IP_FACTOR))
        self.assertEqual(codes[-1], 429)
        self.assertEqual(new(rig, KAREL, title="Jiná adresa", ip="10.0.0.9").status, 201)

    def test_plus_has_a_brake_too(self):
        rig = make()
        new(rig)
        codes = [call(rig, "POST", "/api/issues/1/plus", {"client": JANA}).status
                 for _ in range(issues.RATE_PLUS + 1)]
        self.assertEqual(codes, [200] * issues.RATE_PLUS + [429])

    def test_rejected_input_does_not_use_up_the_limit(self):
        rig = make()
        for _ in range(issues.RATE_CREATE + 3):
            self.assertEqual(new(rig, PETR, title="").status, 400)
        self.assertEqual(new(rig, PETR).status, 201)

    def test_total_cap_drops_oldest_closed_never_open_or_seed(self):
        seed = [{"key": "s1", "kind": "napad", "title": "Ze seedu", "state": "vyreseno",
                 "resolution": "Hotovo."}]
        rig = make(seed=seed)
        book = rig.book
        book._rates["create"].limit = 10_000
        clock = Clock(1_790_000_000.0)
        book.clock = clock
        with mock.patch.object(issues, "MAX_ITEMS", 6):
            for i in range(5):
                clock.t += 10
                self.assertEqual(new(rig, PETR, title=f"Položka {i}").status, 201)
            self.assertEqual(len(book.items), 6)
            # plno a nic uzavřeného od kolegů: další se nevejde (seed se nevyhazuje)
            r = new(rig, JANA, title="Sedmá")
            self.assertEqual(r.status, 429)
            self.assertIn("plný", r.json()["error"])
            clock.t += 10
            book.admin_update(3, state="vyreseno")
            clock.t += 10
            book.admin_update(2, state="zamitnuto")
            self.assertEqual(new(rig, JANA, title="Sedmá").status, 201)
            self.assertEqual(len(book.items), 6)
            self.assertNotIn(3, book.items)  # nejdéle neměněná uzavřená
            self.assertIn(2, book.items)
            self.assertIn(1, book.items)  # seed zůstává
        # i na disku je pryč
        rig.store.flush()
        self.assertNotIn(3, [row[0] for row in rig.store.all_issues()[0]])

    def test_comment_caps(self):
        rig = make()
        new(rig)
        rig.book._rates["comment"].limit = 10_000
        with mock.patch.object(issues, "MAX_COMMENTS", 3):
            codes = [call(rig, "POST", "/api/issues/1/comments", {"client": JANA, "body": "x"}).status
                     for _ in range(4)]
        self.assertEqual(codes, [201, 201, 201, 429])
        new(rig, title="Druhá")
        with mock.patch.object(issues, "MAX_COMMENTS_TOTAL", 3):
            self.assertEqual(call(rig, "POST", "/api/issues/2/comments",
                                  {"client": JANA, "body": "x"}).status, 429)

    def test_worst_case_fits_in_memory(self):
        """Strop × největší text = pár MB, ne stovky (Pi má 1 GB)."""
        worst = (issues.MAX_ITEMS * (issues.TITLE_MAX + issues.BODY_MAX + issues.RESOLUTION_MAX)
                 + issues.MAX_COMMENTS_TOTAL * issues.COMMENT_MAX)
        self.assertLess(worst * 4, 20 * 2**20)
        self.assertLessEqual(issues.PAGE_MAX, 50)


class Admin(unittest.TestCase):
    """F-HLASENI-05: stav, řešení a mazání jen s PINem správce."""

    def test_state_and_resolution_need_the_pin(self):
        rig = make()
        new(rig)
        body = {"state": "vyreseno", "resolution": "Opraveno: hledání bere i překlepy."}
        r = call(rig, "POST", "/api/issues/1/resolve", body)
        self.assertEqual((r.status, r.json()["pin"]), (401, "required"))
        r = call(rig, "POST", "/api/issues/1/resolve", body, pin="000000")
        self.assertEqual((r.status, r.json()["pin"]), (403, "wrong"))
        self.assertEqual(rig.book.items[1].state, "nove")
        self.assertEqual(rig.book.items[1].resolution, "")
        r = call(rig, "POST", "/api/issues/1/resolve", body, pin=PIN)
        self.assertEqual(r.status, 200, r.body)
        item = r.json()["item"]
        self.assertEqual((item["state"], item["open"], item["resolution"]),
                         ("vyreseno", False, "Opraveno: hledání bere i překlepy."))
        # psát, komentovat a dávat +1 jde dál bez PINu, 127.0.0.1 výjimku nemá
        self.assertEqual(call(rig, "POST", "/api/issues/1/comments", {"client": JANA, "body": "Díky"}).status, 201)
        self.assertEqual(call(rig, "POST", "/api/issues/1/resolve", body, ip="127.0.0.1").status, 401)

    def test_states_and_bad_input(self):
        rig = make()
        new(rig)
        for state in issues.STATES:
            r = call(rig, "POST", "/api/issues/1/resolve", {"state": state}, pin=PIN)
            self.assertEqual((r.status, r.json()["item"]["state"]), (200, state))
        self.assertEqual(set(issues.STATES), {"nove", "resi_se", "nasazeni", "vyreseno", "zamitnuto"})
        for bad in ({"state": "hotovo"}, {"state": ["nove"]}, {}, {"resolution": 5},
                    {"resolution": "x" * (issues.RESOLUTION_MAX + 1)}):
            self.assertEqual(call(rig, "POST", "/api/issues/1/resolve", bad, pin=PIN).status, 400, bad)
        self.assertEqual(call(rig, "POST", "/api/issues/5/resolve", {"state": "nove"}, pin=PIN).status, 404)

    def test_resolution_is_dated_and_shown(self):
        rig = make()
        clock = Clock(1_790_000_000.0)
        rig.book.clock = clock
        new(rig)
        self.assertIsNone(call(rig, "GET", "/api/issues/1").json()["item"]["resolved_at"])
        clock.t += 3600
        call(rig, "POST", "/api/issues/1/resolve", {"state": "nasazeni", "resolution": "Opraveno, čeká na večer."},
             pin=PIN)
        item = call(rig, "GET", "/api/issues/1").json()["item"]
        self.assertEqual((item["resolution"], item["resolved_at"], item["state"]),
                         ("Opraveno, čeká na večer.", clock.t, "nasazeni"))
        brief = call(rig, "GET", "/api/issues").json()["items"][0]
        self.assertEqual((brief["resolved"], brief["resolved_at"]), (True, clock.t))
        # změna jen stavu datum řešení nemění; nový text ano; prázdný text řešení smaže
        when = clock.t
        clock.t += 86400
        call(rig, "POST", "/api/issues/1/resolve", {"state": "vyreseno"}, pin=PIN)
        self.assertEqual(call(rig, "GET", "/api/issues/1").json()["item"]["resolved_at"], when)
        call(rig, "POST", "/api/issues/1/resolve", {"resolution": "Nasazeno."}, pin=PIN)
        self.assertEqual(call(rig, "GET", "/api/issues/1").json()["item"]["resolved_at"], clock.t)
        call(rig, "POST", "/api/issues/1/resolve", {"resolution": ""}, pin=PIN)
        item = call(rig, "GET", "/api/issues/1").json()["item"]
        self.assertEqual((item["resolution"], item["resolved"], item["resolved_at"]), ("", False, None))

    def test_delete_needs_the_pin(self):
        rig = make(seed=[{"key": "s1", "kind": "napad", "title": "Ze seedu"}])
        new(rig)
        call(rig, "POST", "/api/issues/2/comments", {"client": JANA, "body": "spam"})
        call(rig, "POST", "/api/issues/2/comments", {"client": JANA, "body": "k věci"})
        call(rig, "POST", "/api/issues/2/plus", {"client": JANA})
        self.assertEqual(call(rig, "DELETE", "/api/issues/2").status, 401)
        self.assertEqual(call(rig, "DELETE", "/api/issues/2", pin="111111").status, 403)
        self.assertEqual(call(rig, "DELETE", "/api/issues/2/comments/1").status, 401)
        self.assertIn(2, rig.book.items)
        r = call(rig, "DELETE", "/api/issues/2/comments/1", pin=PIN)
        self.assertEqual([c["body"] for c in r.json()["item"]["comment_list"]], ["k věci"])
        self.assertEqual(call(rig, "DELETE", "/api/issues/2/comments/1", pin=PIN).status, 404)
        r = call(rig, "DELETE", "/api/issues/2", pin=PIN)
        self.assertEqual((r.status, r.json()["deleted"]), (200, 2))
        self.assertEqual(call(rig, "GET", "/api/issues/2").status, 404)
        self.assertEqual(call(rig, "DELETE", "/api/issues/2", pin=PIN).status, 404)
        rig.store.flush()
        items, comments, plus = rig.store.all_issues()
        self.assertEqual(([r[0] for r in items], comments, plus), ([1], [], []))
        # položka ze seedu by se po startu založila znovu — mazání odmítne a řekne proč
        r = call(rig, "DELETE", "/api/issues/1", pin=PIN)
        self.assertEqual(r.status, 409)
        self.assertIn("seed", r.json()["error"])

    def test_brake_is_shared_with_settings(self):
        """Pět špatných PINů u hlášení zavře na 5 minut i nastavení — a naopak."""
        rig = make()
        new(rig)
        for _ in range(adminpin.MAX_FAILURES - 1):
            self.assertEqual(call(rig, "POST", "/api/issues/1/resolve", {"state": "vyreseno"},
                                  pin="999999").status, 403)
        r = call(rig, "DELETE", "/api/issues/1", pin="999999")
        self.assertEqual((r.status, r.json()["pin"]), (429, "locked"))
        self.assertEqual(r.headers["retry-after"], str(adminpin.LOCK_SECONDS))
        # zavřeno i pro správný PIN, i pro nastavení
        self.assertEqual(call(rig, "POST", "/api/issues/1/resolve", {"state": "vyreseno"}, pin=PIN).status, 429)
        self.assertEqual(call(rig, "GET", "/api/config", pin=PIN).status, 429)
        self.assertEqual(rig.book.items[1].state, "nove")
        # kolegové píšou dál
        self.assertEqual(call(rig, "POST", "/api/issues/1/plus", {"client": JANA}).status, 200)
        rig.srv.admin.clock.t += adminpin.LOCK_SECONDS + 1
        self.assertEqual(call(rig, "POST", "/api/issues/1/resolve", {"state": "vyreseno"}, pin=PIN).status, 200)

    def test_only_admin_routes_are_guarded(self):
        rig = make()
        routes = {(r.path, m): r.endpoint for r in rig.srv._starlette.routes
                  for m in (getattr(r, "methods", None) or ())}
        guarded = {k for k, ep in routes.items() if k[0].startswith("/api/issues") and getattr(ep, "admin_only", False)}
        self.assertEqual(guarded, {("/api/issues/{iid}/resolve", "POST"), ("/api/issues/{iid}", "DELETE"),
                                   ("/api/issues/{iid}/comments/{cid}", "DELETE")})
        for key in (("/api/issues", "GET"), ("/api/issues", "POST"), ("/api/issues/{iid}", "GET"),
                    ("/api/issues/{iid}/comments", "POST"), ("/api/issues/{iid}/plus", "POST")):
            self.assertFalse(getattr(routes[key], "admin_only", True), key)

    def test_pin_never_in_telemetry(self):
        rig = make()
        new(rig)
        events = []
        with mock.patch.object(telemetry, "event", lambda name, **kw: events.append((name, kw))):
            call(rig, "POST", "/api/issues/1/resolve", {"state": "vyreseno", "resolution": "Hotovo"}, pin=PIN)
            call(rig, "POST", "/api/issues/1/resolve", {"state": "nove"}, pin="135790")
        self.assertIn("issue.resolved", [n for n, _ in events])
        dump = json.dumps(events)
        self.assertNotIn(PIN, dump)
        self.assertNotIn("135790", dump)


class Persistence(unittest.TestCase):
    """F-HLASENI-06: přežije restart; zápisy jdou přes vlákno zapisovače."""

    def test_survives_a_restart_of_the_store(self):
        tmp = tmpdir()
        rig = make(tmp, background=True)
        new(rig, PETR, "chyba", "Nenašlo to písničku", "Podrobnosti\nna dva řádky")
        new(rig, JANA, "napad", "Posouvání v písničce")
        call(rig, "POST", "/api/issues/1/comments", {"client": JANA, "body": "Mně taky"})
        call(rig, "POST", "/api/issues/1/comments", {"client": PETR, "body": "Díky"})
        call(rig, "POST", "/api/issues/1/plus", {"client": JANA})
        call(rig, "POST", "/api/issues/1/plus", {"client": KAREL})
        call(rig, "POST", "/api/issues/1/plus", {"client": KAREL})  # stažené +1 se neobnoví
        call(rig, "POST", "/api/issues/2/resolve", {"state": "vyreseno", "resolution": "Přidáno."}, pin=PIN)
        before = [call(rig, "GET", f"/api/issues/{i}", query="client=" + JANA).json() for i in (1, 2)]
        rig.store.close()  # ukončení služby: zapisovač dopíše frontu

        again = make(tmp, background=True)  # nový proces: nový Store nad stejným souborem
        after = [call(again, "GET", f"/api/issues/{i}", query="client=" + JANA).json() for i in (1, 2)]
        self.assertEqual(after, before)
        self.assertEqual(after[0]["item"]["plus_by"], ["Jana"])
        self.assertEqual([c["body"] for c in after[0]["item"]["comment_list"]], ["Mně taky", "Díky"])
        self.assertEqual((after[1]["item"]["state"], after[1]["item"]["resolution"]), ("vyreseno", "Přidáno."))
        # čísla pokračují, nic se nepřepíše
        self.assertEqual(new(again, PETR, title="Třetí").json()["item"]["id"], 3)
        r = call(again, "POST", "/api/issues/1/comments", {"client": PETR, "body": "Po restartu"})
        self.assertEqual([c["id"] for c in r.json()["item"]["comment_list"]], [1, 2, 3])
        again.store.close()

    def test_lives_in_state_db_next_to_the_other_state(self):
        from ytdj import config

        self.assertEqual(Store.__init__.__defaults__[0], config.STATE_DB)
        self.assertEqual(config.STATE_DB.parent, config.DATA_DIR)
        src = (ROOT / "ytdj/__main__.py").read_text(encoding="utf-8")
        self.assertIn("self.issues = wire_issues(self)", src)
        self.assertIn("await aload_issues(self.issues)", src)
        app = SimpleNamespace(store=object(), wishes=None)
        self.assertIs(issues.wire(app, seed_path=None).store, app.store)

    def test_not_ready_until_loaded(self):
        book = issues.IssueBook(None, seed_path=None)
        with self.assertRaises(issues.IssueError) as cm:
            book.create(PETR, "Petr", "chyba", "Moc brzy")
        self.assertEqual(cm.exception.status, 503)
        app = SimpleNamespace(cfg=Config(**DEFAULTS), issues=book, restart_requested=asyncio.Event())
        srv = web.WebServer(app)
        self.assertEqual(asyncio.run(acall(srv, "GET", "/api/issues")).status, 503)
        app.issues = None  # aplikace bez hlášení (staré testy, nástroje)
        self.assertEqual(asyncio.run(acall(srv, "GET", "/api/issues")).status, 404)

    def test_broken_load_starts_empty_instead_of_crashing(self):
        class Bad:
            async def aread(self, name):
                raise OSError("karta")

        book = issues.IssueBook(Bad(), seed_path=None)
        with self.assertLogs("ytdj.issues", "ERROR"):
            asyncio.run(issues.aload_quietly(book))
        self.assertTrue(book.loaded)
        self.assertEqual(book.items, {})


class Loop(unittest.TestCase):
    """F-HLASENI-06: hlavní smyčka na SD kartu nikdy nečeká."""

    def test_writes_do_not_wait_for_the_disk(self):
        rig = make(background=True)
        events: list[tuple[str, dict]] = []

        async def go():
            with mock.patch.object(telemetry, "event", lambda k, **f: events.append((k, f))):
                watch = loopwatch.LoopWatch(threshold=0.2, tick=0.05)
                watch.start()
                rig.store._later(lambda: time.sleep(1.2))  # SD karta právě nestíhá
                t0 = time.monotonic()
                codes = [(await acall(rig.srv, "POST", "/api/issues",
                                      {"client": PETR, "kind": "chyba", "title": "Zaseklá karta"})).status,
                         (await acall(rig.srv, "POST", "/api/issues/1/comments",
                                      {"client": JANA, "body": "Taky"})).status,
                         (await acall(rig.srv, "POST", "/api/issues/1/plus", {"client": JANA})).status,
                         (await acall(rig.srv, "POST", "/api/issues/1/resolve",
                                      {"state": "resi_se", "resolution": "Dívám se na to."}, pin=PIN)).status,
                         (await acall(rig.srv, "GET", "/api/issues")).status,
                         (await acall(rig.srv, "GET", "/api/issues/1")).status]
                took = time.monotonic() - t0
                await asyncio.sleep(0.3)
                await watch.stop()
                return codes, took, watch

        codes, took, watch = asyncio.run(go())
        self.assertEqual(codes, [201, 201, 200, 200, 200, 200])
        self.assertLess(took, 0.3, f"požadavky čekaly {took * 1000:.0f} ms")
        self.assertEqual(watch.events, 0, f"loop stál {watch.max_lag_ms} ms")
        rig.store.flush()
        items, comments, plus = rig.store.all_issues()
        self.assertEqual([(r[0], r[6], r[7]) for r in items], [(1, "resi_se", "Dívám se na to.")])
        self.assertEqual((len(comments), len(plus)), (1, 1))
        rig.store.close()

    def test_load_runs_off_the_event_loop(self):
        tmp = tmpdir()
        seed_file = tmp / "seed.json"
        seed_file.write_text(json.dumps({"items": [{"key": "a", "kind": "napad", "title": "Ze souboru"}]}),
                             encoding="utf-8")
        store = Store(tmp / "state.db", background=True)
        where: dict[str, str] = {}
        real_all, real_seed = store.all_issues, issues.read_seed

        def all_issues():
            where["db"] = threading.current_thread().name
            return real_all()

        def read_seed(path):
            where["seed"] = threading.current_thread().name
            return real_seed(path)

        store.all_issues = all_issues
        book = issues.IssueBook(store, seed_path=seed_file)

        async def go():
            where["loop"] = threading.current_thread().name
            with mock.patch.object(issues, "read_seed", read_seed):
                await issues.aload_quietly(book)

        asyncio.run(go())
        self.assertTrue(book.loaded)
        self.assertEqual([it.title for it in book.items.values()], ["Ze souboru"])
        self.assertNotEqual(where["db"], where["loop"])
        self.assertNotEqual(where["seed"], where["loop"])
        store.close()

    def test_handlers_never_touch_the_disk(self):
        """API pracuje jen s pamětí: žádné sqlite, open ani čtení souboru v modulu API,
        kniha sahá na Store jen přes zápisy do fronty zapisovače."""
        api = Path(issues_api.__file__).read_text(encoding="utf-8")
        for word in ("sqlite", "open(", "read_text", "write_text", "all_issues", "to_thread"):
            self.assertNotIn(word, api, word)
        src = Path(issues.__file__).read_text(encoding="utf-8")
        book = src[src.index("class IssueBook"): src.index("def wire(")]
        used = set(re.findall(r"self\.store\.(\w+)", book))
        self.assertEqual(used, {"all_issues", "save_issue", "save_issue_comment", "set_issue_plus",
                                "delete_issue", "delete_issue_comment"})
        state = (ROOT / "ytdj/state.py").read_text(encoding="utf-8")
        for name in sorted(used - {"all_issues"}):
            body = state[state.index(f"def {name}("):]
            body = body[: body.index("\n    def ", 10)]
            self.assertTrue("self._write(" in body or "self._write_tx(" in body, name)
            self.assertNotIn("self.db.", body, name)
        # čtení celé tabulky jen při načtení (load / aload), ne za provozu
        self.assertEqual(len(re.findall(r"self\.store\.all_issues\(", book)), 1)
        self.assertIn("rows = self.store.all_issues() if self.store is not None", book)  # jen v load()
        self.assertIn('await aread("all_issues")', book)


SEED = [
    {"key": "pozadavek-1", "kind": "chyba", "title": "Nenašlo to písničku", "body": "Původní text.",
     "state": "nove", "created": "2026-10-06", "who": "kolega"},
    {"key": "pozadavek-2", "kind": "napad", "title": "Posouvání v písničce", "body": "Tam a zpět.",
     "state": "nove", "created": "2026-10-06", "who": "kolega"},
]


class Seed(unittest.TestCase):
    """F-HLASENI-07: položky a řešení, které jdou s kódem."""

    def test_seed_creates_items_by_key(self):
        rig = make(seed=SEED)
        lst = call(rig, "GET", "/api/issues").json()["items"]
        self.assertEqual([(i["id"], i["title"], i["who"], i["seed"], i["state"]) for i in lst],
                         [(2, "Posouvání v písničce", "kolega", True, "nove"),
                          (1, "Nenašlo to písničku", "kolega", True, "nove")])
        self.assertEqual(time.strftime("%Y-%m-%d", time.localtime(lst[0]["created"])), "2026-10-06")
        # druhý start se stejným seedem nic nezaloží ani nepřepíše
        self.assertEqual(rig.book.apply_seed(issues.check_seed(SEED)), 0)
        self.assertEqual(len(rig.book.items), 2)

    def test_changed_seed_updates_state_and_resolution_keeps_comments_and_plus(self):
        tmp = tmpdir()
        rig = make(tmp, seed=SEED)
        call(rig, "POST", "/api/issues/1/comments", {"client": JANA, "body": "Mně taky nenašlo"})
        call(rig, "POST", "/api/issues/1/plus", {"client": JANA})
        call(rig, "POST", "/api/issues/1/plus", {"client": PETR})
        new(rig, KAREL, "napad", "Od kolegy")  # položka od člověka mezi tím
        rig.store.close()

        later = [dict(SEED[0], state="vyreseno", title="Nenašlo to písničku (opraveno)",
                      resolution="Hledání teď bere i názvy s překlepem.", resolved="2026-10-07"),
                 SEED[1],
                 {"key": "pozadavek-3", "kind": "napad", "title": "Nová ze seedu"}]
        again = make(tmp, seed=later)  # nasazení: nový kód, stejná databáze
        one = call(again, "GET", "/api/issues/1", query="client=" + JANA).json()["item"]
        self.assertEqual((one["state"], one["title"], one["resolution"]),
                         ("vyreseno", "Nenašlo to písničku (opraveno)", "Hledání teď bere i názvy s překlepem."))
        self.assertEqual(time.strftime("%Y-%m-%d", time.localtime(one["resolved_at"])), "2026-10-07")
        self.assertEqual([c["body"] for c in one["comment_list"]], ["Mně taky nenašlo"])
        self.assertEqual((one["plus"], one["plus_mine"], one["plus_by"]), (2, True, ["Jana", "Petr"]))
        # ostatní zůstaly, nová ze seedu dostala další číslo (ne číslo položky od kolegy)
        ids = {i["id"]: i["title"] for i in call(again, "GET", "/api/issues").json()["items"]}
        self.assertEqual(ids, {1: "Nenašlo to písničku (opraveno)", 2: "Posouvání v písničce",
                               3: "Od kolegy", 4: "Nová ze seedu"})
        again.store.close()
        # a drží to i po dalším startu (zapsáno na disk)
        third = make(tmp, seed=later)
        self.assertEqual(call(third, "GET", "/api/issues/1").json()["item"]["state"], "vyreseno")
        self.assertEqual(len(third.book.items), 4)
        third.store.close()

    def test_unchanged_seed_does_not_overwrite_admin_changes(self):
        tmp = tmpdir()
        rig = make(tmp, seed=SEED)
        call(rig, "POST", "/api/issues/2/resolve", {"state": "resi_se", "resolution": "Zkouším to."}, pin=PIN)
        rig.store.close()
        again = make(tmp, seed=SEED)  # restart se stejným seedem: zápis správce platí dál
        one = call(again, "GET", "/api/issues/2").json()["item"]
        self.assertEqual((one["state"], one["resolution"]), ("resi_se", "Zkouším to."))
        again.store.close()
        # až změna v seedu ho přepíše
        third = make(tmp, seed=[SEED[0], dict(SEED[1], state="vyreseno", resolution="Hotovo.")])
        one = call(third, "GET", "/api/issues/2").json()["item"]
        self.assertEqual((one["state"], one["resolution"]), ("vyreseno", "Hotovo."))
        third.store.close()

    def test_bad_seed_entries_are_skipped_not_fatal(self):
        raw = {"items": [
            {"key": "ok", "kind": "napad", "title": "V pořádku"},
            {"key": "ok", "kind": "napad", "title": "Stejný klíč podruhé"},
            {"kind": "napad", "title": "Bez klíče"},
            {"key": "k2", "kind": "prani", "title": "Špatný druh"},
            {"key": "k3", "kind": "chyba", "title": "Špatný stav", "state": "hotovo"},
            {"key": "k4", "kind": "chyba", "title": "x"},
            "nesmysl"]}
        with self.assertLogs("ytdj.issues", "WARNING"):
            good = issues.check_seed(raw)
        self.assertEqual([e["key"] for e in good], ["ok"])
        tmp = tmpdir()
        (tmp / "seed.json").write_text("{rozbité", encoding="utf-8")
        with self.assertLogs("ytdj.issues", "WARNING"):
            self.assertEqual(issues.read_seed(tmp / "seed.json"), [])
        self.assertEqual(issues.read_seed(tmp / "neni.json"), [])

    def test_repo_seed_is_valid_and_has_the_owner_requests(self):
        raw = json.loads(issues.SEED_FILE.read_text(encoding="utf-8"))
        good = issues.check_seed(raw)
        self.assertEqual(len(good), len(raw["items"]), "každá položka seedu musí projít kontrolou")
        keys = [e["key"] for e in good]
        # the first eleven are the owner's requests of 6. 10.; later ones are appended, never renumbered
        self.assertEqual(keys[:11], [f"pozadavek-{n}" for n in range(52, 63)])
        self.assertEqual(len(keys), len(set(keys)))
        pozadavky = (ROOT / "docs/POZADAVKY.md").read_text(encoding="utf-8")
        for e in good:
            n = e["key"].split("-")[1]
            self.assertIn(f"POZADAVKY #{n}", e["body"], e["key"])
            self.assertIn(f"| {n} |", pozadavky)
        book = issues.IssueBook(None).load()  # skutečný soubor z repozitáře
        self.assertEqual(len(book.items), len(good))
        self.assertEqual(issues.SEED_FILE, ROOT / "ytdj/data/issues_seed.json")

    def test_repo_seed_has_no_personal_data(self):
        text = issues.SEED_FILE.read_text(encoding="utf-8")
        self.assertNotIn("@", text)
        self.assertIsNone(re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text))
        raw = json.loads(text)
        self.assertLessEqual({e.get("who") for e in raw["items"]}, {"kolega", "vlastník"})


EVIL = '<script>alert(1)</script><img src=x onerror="alert(2)"> & "uvozovky" \'apostrof\''


class Escaping(unittest.TestCase):
    """F-HLASENI-08: text od lidí je vždy jen text."""

    def test_html_comes_back_as_plain_json_text(self):
        rig = make()
        rig.app.wishes.nicks.data["client-zly"] = {"nick": "<b>Zlý</b>", "at": time.time()}
        r = call(rig, "POST", "/api/issues", {"client": "client-zly", "kind": "chyba", "title": EVIL[:100],
                                              "body": EVIL})
        self.assertEqual(r.status, 201, r.body)
        call(rig, "POST", "/api/issues/1/comments", {"client": "client-zly", "body": EVIL})
        call(rig, "POST", "/api/issues/1/resolve", {"resolution": EVIL}, pin=PIN)
        for path in ("/api/issues", "/api/issues/1"):
            r = call(rig, "GET", path)
            self.assertTrue(r.headers["content-type"].startswith("application/json"), r.headers)
            # JSON nese text přesně, jak přišel — nic se z něj nestane značkou ani se neztratí
            json.loads(r.body)
        item = call(rig, "GET", "/api/issues/1").json()["item"]
        self.assertEqual((item["body"], item["resolution"], item["comment_list"][0]["body"]), (EVIL, EVIL, EVIL))
        self.assertEqual(item["who"], "<b>Zlý</b>")

    def test_page_never_builds_html_from_text(self):
        page = PAGE.read_text(encoding="utf-8")
        script = page[page.index("<script>"):]
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(",
                     "new Function", "DOMParser", "srcdoc", "javascript:"):
            self.assertNotIn(sink, script, sink)
        # jediná cesta textu do stránky je createTextNode / textContent / value
        self.assertIn("document.createTextNode(c)", script)
        self.assertNotIn("setAttribute(\"href\"", script)
        # atributy se z dat od lidí neskládají: href / src / style nedostane nic z položky
        for m in re.finditer(r"\b(href|src|style|class|id)\s*:\s*([^,}]+)", script):
            value = m.group(2)
            if m.group(1) == "class":
                continue  # jen třídy podle druhu a stavu (server je hlídá výčtem)
            self.assertNotRegex(value, r"\b(it|c|r)\.(title|body|who|resolution|excerpt)\b", m.group(0))

    def test_class_names_come_only_from_checked_enums(self):
        """Druh a stav jdou do názvu třídy — server pustí jen hodnoty z výčtu."""
        rig = make()
        r = call(rig, "POST", "/api/issues", {"client": PETR, "kind": 'chyba" onclick="x', "title": "Zkouška"})
        self.assertEqual(r.status, 400)
        new(rig)
        r = call(rig, "POST", "/api/issues/1/resolve", {"state": 'nove" x="'}, pin=PIN)
        self.assertEqual(r.status, 400)
        for it in rig.book.items.values():
            self.assertIn(it.kind, issues.KINDS)
            self.assertIn(it.state, issues.STATES)

    def test_control_characters_are_removed(self):
        rig = make()
        r = call(rig, "POST", "/api/issues", {
            "client": PETR, "kind": "chyba", "title": "Název\x00 s\x07 řídicími‮ znaky\n a řádkem",
            "body": "řádek 1\r\n\r\n\r\n\r\nřádek 2\x00\x1b[31m\t konec  \n\n"})
        item = r.json()["item"]
        self.assertEqual(item["title"], "Název s řídicími znaky a řádkem")
        self.assertEqual(item["body"], "řádek 1\n\nřádek 2[31m  konec")
        self.assertEqual(issues.clean_text(None), "")
        self.assertEqual(issues.clean_line(12), "12")


class Page(unittest.TestCase):
    """F-HLASENI-09: stránka, odkazy na ni a její váha."""

    def test_served_gzipped_with_etag(self):
        rig = make()
        r = call(rig, "GET", "/hlaseni", gz=True)
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers.get("content-encoding"), "gzip")
        self.assertEqual(r.headers.get("cache-control"), "no-cache")
        self.assertEqual(gzip.decompress(r.body), PAGE.read_bytes())
        self.assertEqual(call(rig, "GET", "/hlaseni", gz=True, etag=r.headers["etag"]).status, 304)
        plain = call(rig, "GET", "/hlaseni")
        self.assertEqual(plain.body, PAGE.read_bytes())
        self.assertTrue(plain.headers["content-type"].startswith("text/html"))

    def test_page_is_built_off_the_event_loop(self):
        src = Path(web.__file__).read_text(encoding="utf-8")
        self.assertIn('Route("/hlaseni", _safe(self._manual), methods=["GET"])', src)
        body = src[src.index("async def _manual"): src.index("async def _static_fallback")]
        self.assertIn("asyncio.to_thread(self._pages.get", body)

    def test_page_is_light_and_self_contained(self):
        raw = PAGE.read_bytes()
        self.assertLess(len(raw), 32 * 1024, "stránka má zůstat lehká (Pi 3)")
        self.assertLess(len(gzip.compress(raw, 6)), 9 * 1024)
        page = raw.decode("utf-8")
        self.assertEqual(re.findall(r"""(?:src|href)=["'](https?:)?//""", page), [])  # nic zvenku
        # žádné knihovny — jen vlastní malé skripty společné stránkám: vzhled (F-WEB-09)
        # a tentýž člověk na všech adresách jukeboxu (F-NICK-07)
        self.assertEqual(re.findall(r"<script[^>]+src=[\"']?([^\"' >]*)", page), ["/theme.js", "/identity.js"])
        self.assertIn('<link rel="stylesheet" href="/manual.css">', page)  # barvy den / noc jako jinde
        self.assertIn('name="viewport" content="width=device-width, initial-scale=1', page)
        self.assertIn('<meta name="color-scheme" content="dark light">', page)
        self.assertNotRegex(page[page.index("<style>"): page.index("</style>")], r"#[0-9a-fA-F]{3,8}\b")

    def test_page_uses_the_contract(self):
        page = PAGE.read_text(encoding="utf-8")
        for needle in ('"/api/issues"', '"/comments"', '"/plus"', '"/resolve"', "Jak to bylo vyřešeno",
                       "taky se mi to děje", "taky to chci", 'data-kind="chyba"', 'data-kind="napad"',
                       'id="stateSel"', "Načíst další", 'load("ytdj.client")', 'load("ytdj.nick")'):
            self.assertIn(needle, page, needle)
        for state, label in issues.STATES.items():
            self.assertIn(f'<option value="{state}">{label}</option>', page)
            self.assertIn(f'{state}: "{label}"', page)
        self.assertIn(f'maxlength="{issues.TITLE_MAX}"', page)
        self.assertIn(f'maxlength="{issues.BODY_MAX}"', page)
        self.assertIn(f'maxlength: "{issues.COMMENT_MAX}"', page)
        self.assertIn(f'maxlength: "{issues.RESOLUTION_MAX}"', page)

    def test_page_asks_for_the_pin_like_settings(self):
        """Stejná paměť PINu, stejná hlavička a stejné hlášky jako Nastavení jukeboxu."""
        page, index = PAGE.read_text(encoding="utf-8"), INDEX.read_text(encoding="utf-8")
        key = re.search(r'var PIN_KEY = "([^"]+)"', index).group(1)
        self.assertIn(f'var PIN_KEY = "{key}"', page)
        for same in ('"X-YTDJ-PIN"', "PIN je na displeji jukeboxu: Síť.",
                     "Špatný PIN. Správný je na displeji jukeboxu: Síť.", "Moc špatných PINů — zkus to znovu za ",
                     "PIN má 6 číslic.", 'if (res.status === 403) { forgetPin();'.replace("res.", "r."),
                     'inputmode'):
            self.assertIn(same, page, same)
        for same in ("PIN je na displeji jukeboxu: Síť.", "Špatný PIN. Správný je na displeji jukeboxu: Síť.",
                     "Moc špatných PINů — zkus to znovu za ", "PIN má 6 číslic."):
            self.assertIn(same, index, same)
        # PIN jde jen s požadavky správce
        self.assertIn("if (admin && adminPin()) opts.headers", page)
        self.assertEqual(page.count(", true);"), 1)

    def test_link_in_header_and_help(self):
        index = INDEX.read_text(encoding="utf-8")
        header = index[index.index('<header class="top">'): index.index("</header>")]
        self.assertIn('id="issuesBtn" href="/hlaseni"', header)
        self.assertLess(header.index('id="issuesBtn"'), header.index('id="helpBtn"'))
        self.assertIn("Chyby a nápady", header)
        self.assertIn('id="issuesCnt" hidden', header)
        self.assertIn('fetch("/api/issues?limit=0"', index)  # počet otevřených: jeden malý dotaz
        help_ = NAPOVEDA.read_text(encoding="utf-8")
        self.assertIn('<a href="/hlaseni">Chyby a nápady</a>', help_)
        self.assertIn("Jak to bylo vyřešeno", help_)
        page = PAGE.read_text(encoding="utf-8")
        self.assertIn('<a class="back" href="/">', page)
        self.assertIn('href="/napoveda"', page)

    def test_open_count_for_the_header_badge(self):
        rig = make(seed=SEED)
        new(rig)
        rig.book.admin_update(1, state="vyreseno")
        r = call(rig, "GET", "/api/issues", query="limit=0")
        self.assertEqual((r.json()["counts"]["open"], r.json()["items"]), (2, []))
        self.assertLess(len(r.body), 400)


class Cli(unittest.TestCase):
    """F-HLASENI-10: python -m ytdj.issues proti běžícímu ytdj."""

    def run_cli(self, rig, *args: str, pin_file: Path | None = None):
        async def go():
            await rig.srv.start()
            try:
                out, err = io.StringIO(), io.StringIO()

                def work():
                    with mock.patch.object(sys, "stderr", err), \
                            mock.patch.object(adminpin, "PIN_FILE", pin_file or rig.tmp / "nic"):
                        return issues.main(["--url", rig.srv.url, *args], out=out)

                code = await asyncio.to_thread(work)
                return code, out.getvalue(), err.getvalue()
            finally:
                await rig.srv.stop()

        rig.srv.host, rig.srv.port = "127.0.0.1", 0
        return asyncio.run(go())

    def test_list_show_resolve_against_a_running_server(self):
        rig = make(seed=SEED)
        new(rig, PETR, "chyba", "Od kolegy z webu", "Popis\nna dva řádky")
        call(rig, "POST", "/api/issues/3/comments", {"client": JANA, "body": "Mně taky"})
        call(rig, "POST", "/api/issues/3/plus", {"client": JANA})

        code, out, err = self.run_cli(rig, "list")
        self.assertEqual((code, err), (0, ""))
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 4)
        self.assertRegex(lines[0], r"^#3\s+Chyba\s+nové\s+\+1\s+Od kolegy z webu$")
        self.assertEqual(lines[-1], "— 3 z 3 (otevřených 3)")
        code, out, _ = self.run_cli(rig, "list", "--kind", "napad", "--state", "open")
        self.assertEqual([ln.split()[0] for ln in out.strip().splitlines()[:-1]], ["#2"])

        code, out, err = self.run_cli(rig, "show", "3")
        self.assertEqual((code, err), (0, ""))
        for part in ("#3  [Chyba · nové]  Od kolegy z webu", "Petr", "    na dva řádky", "— Jana", "Mně taky",
                     "+1: 1"):
            self.assertIn(part, out)
        code, _, err = self.run_cli(rig, "show", "99")
        self.assertEqual(code, 1)
        self.assertIn("404", err)

        # PIN ze souboru správce na tomhle stroji
        code, out, err = self.run_cli(rig, "resolve", "3", "--state", "vyreseno", "--text",
                                      "Opraveno v hledání.\nNasazeno večer.", pin_file=rig.tmp / "admin-pin")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("[Chyba · vyřešeno]", out)
        self.assertIn("Jak to bylo vyřešeno", out)
        it = rig.book.items[3]
        self.assertEqual((it.state, it.resolution), ("vyreseno", "Opraveno v hledání.\nNasazeno večer."))
        # …nebo z --pin; jen stav, text zůstane
        code, out, err = self.run_cli(rig, "--pin", PIN, "resolve", "3", "--state", "nasazeni")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual((it.state, it.resolution), ("nasazeni", "Opraveno v hledání.\nNasazeno večer."))
        code, out, _ = self.run_cli(rig, "list", "--state", "nasazeni")
        self.assertIn("✔", out.splitlines()[0])

    def test_resolve_without_or_with_wrong_pin_is_refused(self):
        rig = make(seed=SEED)
        code, out, err = self.run_cli(rig, "resolve", "1", "--state", "vyreseno")  # soubor s PINem není
        self.assertEqual((code, out), (2, ""))
        self.assertIn("--pin", err)
        code, out, err = self.run_cli(rig, "--pin", "000000", "resolve", "1", "--state", "vyreseno")
        self.assertEqual(code, 1)
        self.assertIn("403", err)
        self.assertNotIn("000000", err)
        code, _, err = self.run_cli(rig, "--pin", PIN, "resolve", "1")
        self.assertEqual(code, 2)
        self.assertEqual(rig.book.items[1].state, "nove")

    def test_unreachable_server_is_a_sentence_not_a_traceback(self):
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            code = issues.main(["--url", "http://127.0.0.1:9", "list"], out=io.StringIO())
        self.assertEqual(code, 1)
        self.assertIn("Nedovolal jsem se", err.getvalue())

    def test_cli_needs_only_the_standard_library(self):
        src = Path(issues.__file__).read_text(encoding="utf-8")
        for name in ("starlette", "uvicorn", "ytmusicapi", "prompt_toolkit"):
            self.assertNotIn(name, src)
        self.assertIn('if __name__ == "__main__":', src)


if __name__ == "__main__":
    unittest.main()
