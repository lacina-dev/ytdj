"""Co se při spojení dvou účtů (F-NICK-09) stane s importovanými playlisty.

Skutečný web (Starlette bez sítě), skutečná kniha hlasů, Importer nad
falešným YouTube a Store v dočasné složce. Spojuje se cestou, kterou
jukebox používá (`Identity.merge`): mladší účet playlist importoval,
vyřadil z něj písničku a přejmenoval ho; po spojení ho musí umět upravovat
starší účet z každého svého prohlížeče — i z toho, který ještě drží staré id.

    YTDJ_EVENTS_FILE=/tmp/ev.jsonl .venv/bin/python -m unittest tests.test_merge_imports -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_TMP = tempfile.mkdtemp(prefix="ytdj-merge-imports-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)
os.environ.setdefault("YTDJ_EVENTS_FILE", str(Path(_TMP) / "events.jsonl"))

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_identity as T  # noqa: E402  (rig: web + přezdívky + hlasy + identita)
import test_issues as ti  # noqa: E402
import test_playlist_import as P  # noqa: E402  (falešný YouTube, písničky)
from ytdj.config import DEFAULTS, Config  # noqa: E402
from ytdj.votes import SONG, VoteBook  # noqa: E402

OLD, NEW = T.OLD, T.NEW
PL, PL2 = P.PL, "PLdruhy00000000000000000000000000000"


def make(cap: int = 1000):
    rig = T.make(ti.tmpdir())
    rig.app.cfg.playlist_import_max = cap
    rig.votes.cfg = rig.app.imports.cfg = rig.app.cfg
    rig.yt = P.FakeYT({PL: P.playlist("Takový normální playlist", P.songs("a", 5)),
                       PL2: P.playlist("Druhý", P.songs("b", 4))})
    rig.app.imports.catalog = SimpleNamespace(yt=rig.yt)
    T.account(rig, OLD, "Robert", 20, (T.DAMA, 1))      # starší účet: vyhraje
    T.account(rig, NEW, "Robert", 5, (T.POHODA, 1))     # mladší: ten importoval
    T.account(rig, ti.JANA, "Jana", 9, (T.ZPRAVA, 1))
    return rig


def post(rig, path: str, body: dict):
    return T.call(rig, "POST", path, body)


def get(rig, path: str, cid: str = ""):
    return T.call(rig, "GET", path, origin=None, query=f"client={cid}" if cid else "")


def add(rig, cid: str, url: str = PL) -> dict:
    out = post(rig, "/api/votes/imports", {"client": cid, "url": url})
    assert out.status == 200, out.body
    return out.json


def restart(rig) -> VoteBook:
    """Nový běh jukeboxu nad toutéž databází."""
    if hasattr(rig.store, "flush"):
        rig.store.flush()
    return asyncio.run(VoteBook(rig.store, Config(**DEFAULTS)).aload())


class MergedPlaylists(unittest.TestCase):
    def _edited(self):
        """Mladší účet importoval playlist, vyřadil písničku a přejmenoval ho."""
        rig = make()
        iid = add(rig, NEW)["import"]["id"]
        k1 = P.key_of("a", 1)
        self.assertEqual(post(rig, f"/api/votes/imports/{iid}/songs",
                              {"client": NEW, "key": k1, "removed": True}).status, 200)
        self.assertEqual(post(rig, f"/api/votes/imports/{iid}/rename",
                              {"client": NEW, "label": "Do práce"}).status, 200)
        return rig, iid, k1

    def test_surviving_account_edits_the_playlist_from_every_browser(self):
        rig, iid, k1 = self._edited()
        k2 = P.key_of("a", 2)
        songs = f"/api/votes/imports/{iid}/songs"
        # před spojením: druhý účet téhož člověka je pro jukebox cizí
        r = post(rig, songs, {"client": OLD, "key": k2, "removed": True})
        self.assertEqual((r.status, r.json["error"]), (403, "Upravovat jde jen vlastní playlist."))
        self.assertFalse(get(rig, "/api/votes/imports", OLD).json["imports"][0]["mine"])

        winner, note = rig.idn.merge([NEW, OLD])
        self.assertEqual((winner, note["kind"]), (OLD, "merged"))
        imp = rig.votes.imports[iid]
        # playlist patří staršímu účtu i s vlastním názvem a vyřazenou písničkou
        self.assertEqual((imp.client, imp.name, list(imp.removed)), (OLD, "Do práce", [k1]))
        self.assertNotIn(OLD, rig.votes.items.get((SONG, k1), {}))
        # prohlížeč s platným id i ten, který ještě drží staré (spojené) id:
        # playlist je „můj“, jeho písničky se počítají a úpravy projdou
        for cid in (OLD, NEW):
            with self.subTest(browser="platné id" if cid == OLD else "staré id"):
                lst = get(rig, "/api/votes/imports", cid).json
                row = lst["imports"][0]
                self.assertEqual((row["mine"], row["title"], row["songs"], row["removed"]),
                                 (True, "Do práce", 4, 1))
                self.assertEqual(lst["mine_songs"], 4)
                self.assertTrue(get(rig, songs, cid).json["import"]["mine"])
                # moje hlasy: Malá dáma (starší účet) i Pohoda (přešla z mladšího)
                mine = get(rig, "/api/votes", cid).json["mine"]
                self.assertEqual(sorted(i["label"] for i in mine), ["Kabát — Malá dáma", "Kabát — Pohoda"])
                r = post(rig, songs, {"client": cid, "key": k2, "removed": True})
                self.assertEqual((r.status, r.json["import"]["songs"], r.json["import"]["mine"]),
                                 (200, 3, True))
                r = post(rig, songs, {"client": cid, "key": k2, "removed": False})
                self.assertEqual((r.status, r.json["song"]["mine"]), (200, "playlist"))
                r = post(rig, f"/api/votes/imports/{iid}/rename", {"client": cid, "label": "Na ráno"})
                self.assertEqual((r.status, r.json["import"]["title"]), (200, "Na ráno"))
                r = post(rig, f"/api/votes/imports/{iid}/rename", {"client": cid, "label": "Do práce"})
                self.assertEqual(r.status, 200)
                r = post(rig, f"/api/votes/imports/{iid}/refresh", {"client": cid})
                self.assertEqual((r.status, r.json["songs"], r.json["kept_out"]), (200, 4, 1))
        # id klienta se ani po spojení nikomu neposílá
        body = get(rig, "/api/votes/imports", ti.JANA).body + get(rig, songs, ti.JANA).body
        self.assertNotIn(OLD.encode(), body)
        self.assertNotIn(NEW.encode(), body)
        # cizí člověk dál nesmí nic
        r = post(rig, songs, {"client": ti.JANA, "key": k2, "removed": True})
        self.assertEqual(r.status, 403)
        self.assertFalse(get(rig, "/api/votes/imports", ti.JANA).json["imports"][0]["mine"])

    def test_merge_survives_a_restart(self):
        rig, iid, k1 = self._edited()
        rig.idn.merge([NEW, OLD])
        rig.idn.flush()
        book = restart(rig)
        imp = book.imports[iid]
        self.assertEqual((imp.client, imp.name, imp.title, list(imp.removed)),
                         (OLD, "Do práce", "Takový normální playlist", [k1]))
        self.assertEqual([it.title for it in imp.items.values()],
                         ["a song 0", "a song 2", "a song 3", "a song 4"])
        # 👍 z playlistu i vlastní hlasy má starší účet, mladší nemá nic
        self.assertEqual(len(P.up_of(book, OLD)), 4 + 2)
        self.assertEqual(P.up_of(book, NEW), set())
        self.assertEqual(book.imports_of(NEW), [])
        self.assertNotIn(OLD, book.items.get((SONG, k1), {}))
        # i paměť „staré id → platné“ je na kartě
        again = T.identity.Identity(rig.app, rig.tmp / "identity.json")
        self.assertTrue(again.wait_loaded())
        self.assertEqual(again.resolve(NEW), OLD)
        # po restartu se dá upravovat dál (vrátit vyřazenou) a přežije to další restart
        rig.app.votes = rig.app.imports.book = book
        rig.app.imports.store = rig.store
        r = post(rig, f"/api/votes/imports/{iid}/songs", {"client": NEW, "key": k1, "removed": False})
        self.assertEqual((r.status, r.json["import"]["songs"]), (200, 5))
        self.assertEqual(restart(rig).imports[iid].removed, {})

    def test_same_playlist_imported_by_both_accounts_counts_once(self):
        rig = make()
        first = add(rig, OLD)["import"]["id"]
        second = add(rig, NEW)["import"]["id"]
        post(rig, "/api/votes", {"target": "song", "vote": 1, "client": ti.JANA, "key": P.key_of("a", 0)})
        keys = [P.key_of("a", i) for i in range(5)]
        self.assertEqual([rig.votes.tally(SONG, k).up for k in keys], [3, 2, 2, 2, 2])
        rig.idn.merge([NEW, OLD])
        # jeden člověk = jeden 👍 na písničku, i když ji má ve dvou playlistech
        self.assertEqual([rig.votes.tally(SONG, k).up for k in keys], [2, 1, 1, 1, 1])
        lst = get(rig, "/api/votes/imports", OLD).json
        self.assertEqual({i["id"]: (i["songs"], i["active"], i["mine"]) for i in lst["imports"]},
                         {first: (5, 5, True), second: (5, 0, True)})
        self.assertEqual(lst["mine_songs"], 5)  # do limitu se písnička počítá jednou
        # vyřazení z jednoho playlistu 👍 nevezme (drží ho druhý), odebrání obou ano
        r = post(rig, f"/api/votes/imports/{first}/songs",
                 {"client": OLD, "key": keys[1], "removed": True})
        self.assertIn("dál jí ho ale dává playlist", r.json["message"])
        self.assertEqual(rig.votes.tally(SONG, keys[1]).up, 1)
        book = restart(rig)
        self.assertEqual([book.tally(SONG, k).up for k in keys], [2, 1, 1, 1, 1])
        self.assertEqual(len(book.imports_of(OLD)), 2)
        T.call(rig, "DELETE", f"/api/votes/imports/{first}", {"client": OLD})
        self.assertEqual([rig.votes.tally(SONG, k).up for k in keys], [2, 1, 1, 1, 1])
        T.call(rig, "DELETE", f"/api/votes/imports/{second}", {"client": NEW})  # staré id = tentýž člověk
        self.assertEqual([rig.votes.tally(SONG, k).up for k in keys], [1, 0, 0, 0, 0])
        self.assertEqual(restart(rig).imports, {})

    def test_limit_per_person_after_a_merge(self):
        """Spojení nic nezahodí, ale limit F-HLASY-13 platí dál: nad něj nic nepřibude
        a Obnovit playlist do limitu srovná (a řekne to)."""
        rig = make(cap=6)
        a = add(rig, OLD)["import"]["id"]          # 5 písniček
        b = add(rig, NEW, PL2)["import"]["id"]     # 4 písničky jiného playlistu
        rig.idn.merge([NEW, OLD])
        self.assertEqual(len(rig.votes.import_keys(OLD)), 9)  # spojení samo nic nesmazalo
        lst = get(rig, "/api/votes/imports", OLD).json
        self.assertEqual((lst["mine_songs"], lst["max"]), (9, 6))
        # nový playlist se nevejde
        rig.yt.lists["PLtreti00000000000000000000000000000"] = P.playlist("Třetí", P.songs("c", 2))
        r = post(rig, "/api/votes/imports", {"client": OLD, "url": "PLtreti00000000000000000000000000000"})
        self.assertEqual(r.status, 409)
        self.assertIn("Z playlistů už máš 6 písniček", r.json["error"])
        # vyřazenou písničku nad limit vrátit nejde
        kb = P.key_of("b", 0)
        self.assertEqual(post(rig, f"/api/votes/imports/{b}/songs",
                              {"client": OLD, "key": kb, "removed": True}).status, 200)
        r = post(rig, f"/api/votes/imports/{b}/songs", {"client": OLD, "key": kb, "removed": False})
        self.assertEqual(r.status, 409)
        # Obnovit: playlist se vejde jen do zbytku limitu a hláška to řekne
        r = post(rig, f"/api/votes/imports/{b}/refresh", {"client": OLD})
        self.assertEqual((r.status, r.json["songs"], r.json["cut"]), (200, 1, 2))
        self.assertIn("limit je 6 písniček z playlistů na člověka", r.json["message"])
        self.assertEqual(len(rig.votes.import_keys(OLD)), 6)
        self.assertEqual(len(rig.votes.imports[a].items), 5)
        self.assertEqual(len(restart(rig).import_keys(OLD)), 6)


if __name__ == "__main__":
    unittest.main()
