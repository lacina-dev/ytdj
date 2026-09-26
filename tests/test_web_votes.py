"""Hlasování z pohledu webu: Odehráno nese id a hlasy, hlas ze seznamu Odehráno
míří na tu skladbu (ne na hrající) a rychlá tlačítka posílají texty, kterým
rozumí DJ bez modelu.

    YTDJ_EVENTS_FILE=/tmp/ev.jsonl .venv/bin/python -m unittest tests.test_web_votes -v
"""

from __future__ import annotations

import json
import os
import re
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
