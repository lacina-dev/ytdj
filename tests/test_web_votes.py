"""Hlasování z pohledu webu: Odehráno nese id a hlasy, hlas ze seznamu Odehráno
míří na tu skladbu (ne na hrající) a rychlá tlačítka posílají texty, kterým
rozumí DJ bez modelu.

    YTDJ_EVENTS_FILE=/tmp/ev.jsonl .venv/bin/python -m unittest tests.test_web_votes -v
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_TMP = tempfile.mkdtemp(prefix="ytdj-web-votes-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)
os.environ.setdefault("YTDJ_EVENTS_FILE", str(Path(_TMP) / "events.jsonl"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_wishes as tw  # noqa: E402
from test_votes import JANA, PETR, FakePlayer, request  # noqa: E402
from ytdj import votes as V  # noqa: E402
from ytdj.agent.intent import favourites_request  # noqa: E402
from ytdj.config import DEFAULTS, Config  # noqa: E402
from ytdj.nicks import tag_of  # noqa: E402
from ytdj.state import PlayRecord  # noqa: E402
from ytdj.web import server as web  # noqa: E402

INDEX = Path(__file__).resolve().parents[1] / "ytdj/web/static/index.html"
POHODA = PlayRecord("pohoda00001", "Pohoda", "Kabát", "skipped")


def make():
    player = FakePlayer()
    wq = tw.WishQueue(SimpleNamespace(), player, SimpleNamespace(), None, Config(**DEFAULTS))
    wq.nicks.set(PETR, "Petr")
    wq.nicks.set(JANA, "Jana")
    app = SimpleNamespace(player=player, wishes=wq, cfg=Config(**DEFAULTS),
                          store=SimpleNamespace(recent_history=lambda n: [POHODA]),
                          pools=SimpleNamespace(describe=lambda: "", mood="", artist=""), dj=None)
    V.wire(app)
    srv = web.WebServer(app)
    h = {(r.path, m): r.endpoint for r in srv._starlette.routes for m in (getattr(r, "methods", None) or ())}
    return srv, app, h


class History(unittest.TestCase):
    def test_vote_from_history_hits_that_song_not_the_playing_one(self):
        async def go():
            srv, app, h = make()
            post = h[("/api/votes", "POST")]
            snap = await srv._snapshot()
            self.assertEqual(snap["history"][0]["id"], "pohoda00001")
            self.assertNotIn("votes", snap["history"][0])  # bez hlasů nic navíc
            for who in (PETR, JANA):  # web posílá id + interpreta + název
                resp = await post(request({"target": "song", "vote": -1, "client": who,
                                           "video_id": "pohoda00001", "artist": "Kabát",
                                           "title": "Pohoda"}))
                self.assertEqual(resp.status_code, 200, resp.body)
            out = json.loads(resp.body)
            self.assertEqual(out["item"]["title"], "Pohoda")
            self.assertEqual(out["changed"], "ban")
            snap = await srv._snapshot()
            hv = snap["history"][0]["votes"]
            self.assertEqual((hv["status"], hv["down"]), ("banned", 2))
            self.assertEqual(set(hv["down_by"]), {tag_of(PETR), tag_of(JANA)})
            self.assertEqual(snap["current"]["votes"]["down"], 0)  # hrající netknutá
            # jen jméno a název, bez id: pořád ta skladba, ne hrající
            resp = await post(request({"target": "song", "vote": 1, "client": PETR,
                                       "artist": "Olympic", "title": "Jasná zpráva"}))
            self.assertEqual(json.loads(resp.body)["item"]["title"], "Jasná zpráva")
            self.assertEqual((await srv._snapshot())["current"]["votes"]["up"], 0)

        tw.run(go())


class Page(unittest.TestCase):
    def test_quick_buttons_are_understood_without_the_model(self):
        html = INDEX.read_text()
        texts = re.findall(r'data-fav="([^"]+)"', html)
        self.assertEqual([favourites_request(t) for t in texts], ["office", "mine"])

    def test_page_uses_the_contract(self):
        html = INDEX.read_text()
        for needle in ('"/api/votes"', "/api/votes/track", "up_by", "down_by", "#hlasovani",
                       "Jen tuhle písničku", "Celého interpreta", "Celý interpret "):
            self.assertIn(needle, html)
        # 👍 celému interpretovi: vlastní hlas pozná z up_by, 👎 ze staršího "by" i down_by
        self.assertIn("(a.up_by || []).indexOf(S.tag)", html)
        self.assertIn("(a.down_by || a.by || [])", html)
        self.assertIn('b.artist_status === "favourite"', html)

    def test_page_edits_imported_playlists(self):
        """Úpravy importu (F-HLASY-21…23): stránka volá API úprav, tlačítka
        úprav dává jen k vlastnímu playlistu a názvy z YouTube i vlastní
        název vkládá jen přes esc()."""
        html = INDEX.read_text()
        for needle in ('"/songs?client="', '"/songs"', '"/rename"', 'data-imp="songs:',
                       "removed: !r.removed", "Písničky", "Vyřadit", "Vrátit", "Přejmenovat",
                       "Vyřazené z playlistu"):
            self.assertIn(needle, html)
        # upravovat jen vlastní: tlačítka jen s it.mine / mine
        self.assertIn("(it.mine ? '<button class=\"ibtn\" type=\"button\" data-imp=\"rename:'", html)
        self.assertIn('var act = !mine ? ""', html)
        # nic od YouTube ani od lidí bez esc()
        for raw in ("esc(r.title)", "esc(r.artist)", 'esc(r.via || "")', "esc(it.title)",
                    "esc(it.yt_title || it.title)", "esc(it.who)"):
            self.assertIn(raw, html)
        body = html[html.index("function impSongRow"):html.index("async function loadImpSongs")]
        for m in re.finditer(r"\+\s*((?:r|it)\.(?:title|artist|via|who|yt_title|key)\b)", body):
            self.fail("bez esc(): " + m.group(1))
        # rozepsaný název nesmaže pravidelné obnovení stránky
        self.assertIn('if (V.impRename && !force && vel.imp.querySelector(".impren")) return;', html)
        # nápověda o úpravách ví
        manual = (INDEX.parent / "napoveda.html").read_text()
        for needle in ("Vyřadit", "Vrátit", "Přejmenovat", "Obnovit</b> ji nevrátí"):
            self.assertIn(needle, manual)

    def test_artist_thumbs_up_from_history_row(self):
        async def go():
            srv, app, h = make()
            post = h[("/api/votes", "POST")]
            # řádek Odehráno: web posílá id + interpreta; cíl = interpret té skladby
            resp = await post(request({"target": "artist", "vote": 1, "client": JANA,
                                       "video_id": "pohoda00001", "artist": "Kabát",
                                       "title": "Pohoda"}))
            out = json.loads(resp.body)
            # jeden 👍 celého interpreta z něj oblíbeného neudělá (PLAN H3)
            self.assertEqual((out["item"]["key"], out["item"]["status"]), ("kabat", "neutral"))
            app.votes.cast("artist", PETR, 1, "Petr", artist="Kabát")  # druhý člověk
            snap = await srv._snapshot()
            hv = snap["history"][0]["votes"]
            self.assertEqual(hv["artist_status"], "favourite")
            self.assertEqual(set(hv["artists"][0]["up_by"]), {tag_of(JANA), tag_of(PETR)})
            cur = snap["current"]["votes"]["artists"][0]  # hrající je taky od Kabátu
            self.assertEqual((cur["name"], cur["up"], cur["status"]), ("Kabát", 2, "favourite"))

        tw.run(go())


ARTIST_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const cases = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const f = new Function(src + "\nreturn {artistUpNote, artistUpBadge};")();
const out = cases.map((c) => c.badge
  ? f.artistUpBadge(c.arts, c.favA, (a) => (a.mine || 0))
  : f.artistUpNote(c.a, c.favA));
process.stdout.write(JSON.stringify(out));
"""


class PlayingArtistThumbsUp(unittest.TestCase):
    """F-HLASY-25 (hlášení #33): 👍 celému interpretovi jde dát i u hrající
    skladby, stav je vidět a jeden 👍 se poctivě ukáže jako „1 z 2 potřebných"."""

    def test_playing_song_has_an_artist_thumbs_up_like_the_rows(self):
        html = INDEX.read_text()
        row = html[html.index('id="vrow"'):html.index('id="vInfo"')]
        self.assertIn('id="vArt"', row)
        self.assertIn('aria-haspopup="dialog"', row[row.index('id="vArt"') - 60:])
        # tlačítko otevře týž list jako řádky fronty a Odehráno, jen s 👍 interpretům
        self.assertIn('votes: cur.votes }, "artistUp");', html)
        sheet = html[html.index("function renderSheet()"):html.index("function opt(")]
        self.assertIn("if (!sh.downOnly && !sh.artistUp) {", sheet)   # bez 👍 písničce
        self.assertIn("if (!sh.artistUp) html += opt(\"song:", sheet)  # bez 👎 písničce
        self.assertIn("if (sh.artistUp) return;", sheet)               # bez 👎 interpretovi
        self.assertIn("artistUpNote(a, favA)", sheet)
        # stav u hrající: tlačítko „on" s mým 👍, počet „1/2" nebo ♥
        now = html[html.index("function renderNowVotes"):html.index("async function loadDetail")]
        self.assertIn("artistUpBadge(b.artists, b.fav_artist_votes, myArtistVote)", now)
        self.assertIn('vel.art.classList.toggle("on", ab.on)', now)
        # 👎 u hrající svítí jen pro můj 👎 (písničce nebo interpretovi), ne pro můj 👍 interpretovi
        self.assertIn("some(function (a) { return myArtistVote(a) === -1; })", now)
        manual = (INDEX.parent / "napoveda.html").read_text()
        self.assertIn("<b>👍 interpret</b>", manual)
        self.assertIn('data-cfg="favourite_artist_votes"', manual)

    @unittest.skipUnless(shutil.which("node"), "texty se zkouší v Node (není nainstalovaný)")
    def test_honest_counts_in_node(self):
        html = INDEX.read_text()
        m = re.search(r"/\* hlasy-interpret:start.*?\*/(.*?)/\* hlasy-interpret:end \*/", html, re.S)
        self.assertIsNotNone(m)
        src = Path(_TMP) / "artist.js"
        src.write_text(m.group(1), encoding="utf-8")
        harness = Path(_TMP) / "artist_harness.js"
        harness.write_text(ARTIST_HARNESS, encoding="utf-8")
        cases = [
            {"a": {"up": 1, "down": 0, "status": "neutral", "mine": 1}, "favA": 2},
            {"a": {"up": 1, "down": 0, "status": "neutral", "mine": 0}, "favA": 2},
            {"a": {"up": 0, "down": 0, "status": "neutral"}, "favA": 2},
            {"a": {"up": 2, "down": 2, "status": "pending", "mine": 1}, "favA": 2},  # 👎 zvedají práh
            {"a": {"up": 2, "down": 0, "status": "favourite"}, "favA": 2},
            {"badge": True, "arts": [{"name": "Kabát", "up": 1, "down": 0, "status": "neutral", "mine": 1},
                                     {"name": "Tomáš Klus", "up": 0, "down": 0, "status": "neutral"}], "favA": 2},
            {"badge": True, "arts": [{"name": "Kabát", "up": 2, "status": "favourite"}], "favA": 2},
            {"badge": True, "arts": [{"name": "Kabát", "up": 0, "status": "neutral"}], "favA": 3},
        ]
        f = Path(_TMP) / "artist_cases.json"
        f.write_text(json.dumps(cases), encoding="utf-8")
        r = subprocess.run([shutil.which("node"), str(harness), str(src), str(f)],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out[0], "Tvůj 👍 platí: 1 z 2 potřebných, chybí ještě 1× 👍 od dalšího člověka")
        self.assertEqual(out[1], "1 z 2 potřebných 👍, chybí ještě 1× 👍 od dalšího člověka")
        self.assertIn("jeden 👍 z něj oblíbeného neudělá", out[2])
        self.assertEqual(out[3], "Tvůj 👍 platí: 2 z 3 potřebných, chybí ještě 1× 👍 od dalšího člověka")
        self.assertEqual(out[4], "♥ oblíbený interpret kanceláře — 2× 👍")
        self.assertEqual(out[5], {"on": True, "n": "1/2"})
        self.assertEqual(out[6], {"on": False, "n": "♥"})
        self.assertEqual(out[7], {"on": False, "n": ""})

    def test_playing_song_carries_the_threshold_and_the_answer_is_honest(self):
        async def go():
            srv, app, h = make()
            post = h[("/api/votes", "POST")]
            snap = await srv._snapshot()
            self.assertEqual(snap["current"]["votes"]["fav_artist_votes"], 2)
            # 👍 celému interpretovi hrající skladby: jen video_id + jméno interpreta
            resp = await post(request({"target": "artist", "vote": 1, "client": JANA,
                                       "video_id": "cur00000001", "artist": "Kabát"}))
            out = json.loads(resp.body)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual((out["item"]["key"], out["item"]["status"]), ("kabat", "neutral"))
            self.assertEqual(out["message"], "Hlas zapsán — Kabát má 1 z 2 potřebných 👍. "
                                             "Oblíbený interpret bude, až mu 👍 dá i další člověk.")
            cur = (await srv._snapshot())["current"]["votes"]
            kabat = next(a for a in cur["artists"] if a["name"] == "Kabát")
            self.assertEqual((kabat["up"], kabat["status"], kabat["up_by"]), (1, "neutral", [tag_of(JANA)]))
            resp = await post(request({"target": "artist", "vote": 1, "client": PETR,
                                       "video_id": "cur00000001", "artist": "Kabát"}))
            self.assertIn("mezi oblíbenými interprety", json.loads(resp.body)["message"])

        tw.run(go())


class IndexServing(unittest.TestCase):
    """Stránka jde do telefonů zabalená (110 kB → ~25 kB) a nezměněná jen jako 304."""

    def _get(self, h, **headers):
        import asyncio

        from starlette.requests import Request
        scope = {"type": "http", "method": "GET", "path": "/", "query_string": b"",
                 "headers": [(k.replace("_", "-").encode(), v.encode()) for k, v in headers.items()]}
        return asyncio.run(h[("/", "GET")](Request(scope)))

    def test_gzip_etag_and_304(self):
        import gzip

        srv, app, h = make()
        raw = INDEX.read_bytes()
        plain = self._get(h)
        self.assertEqual(plain.body, raw)
        self.assertNotIn("content-encoding", plain.headers)
        packed = self._get(h, accept_encoding="gzip, deflate")
        self.assertEqual(packed.headers["content-encoding"], "gzip")
        self.assertEqual(gzip.decompress(packed.body), raw)
        self.assertLess(len(packed.body), len(raw) // 3)
        etag = packed.headers["etag"]
        self.assertEqual(etag, plain.headers["etag"])
        again = self._get(h, if_none_match=etag, accept_encoding="gzip")
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again.body, b"")
        self.assertIn('rel="icon"', raw.decode())  # bez /favicon.ico → žádná 404


if __name__ == "__main__":
    unittest.main()
