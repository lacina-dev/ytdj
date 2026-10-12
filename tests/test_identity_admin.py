"""Správce spojuje jmenované účty jednoho člověka z více zařízení (F-NICK-12).

Skutečný web (Starlette), skutečná kniha hlasů, Importer nad falešným YouTube,
Store a identity.json v dočasné složce; příkaz `python -m ytdj.identity` proti
běžícímu serveru na 127.0.0.1. Tři prohlížeče se stejnou přezdívkou: nejstarší
má hlasy, prostřední importoval playlist (vyřadil písničku, přejmenoval ho),
nejmladší má hlasy, z nichž jeden koliduje s nejstarším.

    YTDJ_EVENTS_FILE=/tmp/ev.jsonl .venv/bin/python -m unittest tests.test_identity_admin -v
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="ytdj-identity-admin-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)
os.environ.setdefault("YTDJ_EVENTS_FILE", str(Path(_TMP) / "events.jsonl"))

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_identity as T  # noqa: E402
import test_issues as ti  # noqa: E402
import test_playlist_import as P  # noqa: E402
from ytdj import adminpin, identity  # noqa: E402
from ytdj.config import DEFAULTS, Config  # noqa: E402
from ytdj.votes import SONG, VoteBook  # noqa: E402

# tři prohlížeče jednoho člověka + cizí lidé; konce id jako ve výpisu (6 znaků)
FIRST, MIDDLE, LAST = "web-aaaa-robert-0c662c", "web-bbbb-robert-792b05", "web-cccc-robert-bbffeb"
TWIN = "web-dddd-eva-xx-99b05"  # končí taky na „b05“ → krátký konec id je nejednoznačný
PIN = {"x-ytdj-pin": ti.PIN}
ACCOUNTS, JOIN = "/api/identity/admin/accounts", "/api/identity/admin/join"


def make():
    rig = T.make(ti.tmpdir())
    rig.yt = P.FakeYT({P.PL: P.playlist("Takový normální playlist", P.songs("a", 5))})
    rig.app.imports.catalog = SimpleNamespace(yt=rig.yt)
    T.account(rig, FIRST, "Robert", 17, (T.DAMA, 1), (T.ZPRAVA, -1))
    T.account(rig, MIDDLE, "Robert", 12)
    T.account(rig, LAST, "robert", 0.2, (T.DAMA, -1), (T.POHODA, 1))
    T.account(rig, TWIN, "Eva", 3, (T.ZPRAVA, 1))
    T.account(rig, ti.JANA, "Jana", 9, (T.ZPRAVA, -1))
    # prostřední účet: playlist, jedna písnička vyřazená, vlastní název
    out = T.call(rig, "POST", "/api/votes/imports", {"client": MIDDLE, "url": P.PL})
    assert out.status == 200, out.body
    rig.iid = out.json["import"]["id"]
    rig.k1 = P.key_of("a", 1)
    T.call(rig, "POST", f"/api/votes/imports/{rig.iid}/songs", {"client": MIDDLE, "key": rig.k1, "removed": True})
    T.call(rig, "POST", f"/api/votes/imports/{rig.iid}/rename", {"client": MIDDLE, "label": "Do práce"})
    rig.votes.imports[rig.iid].created -= 12 * 86400  # import z doby, kdy účet vznikl
    return rig


def admin(rig, method: str, path: str, body=None, query: str = "", pin: dict | None = PIN):
    return T.call(rig, method, path, body, origin=None, query=query, extra=pin or {})


def state(rig) -> str:
    """Všechno, co spojení mění — pro „plán nic nezměnil“."""
    if hasattr(rig.store, "flush"):
        rig.store.flush()
    rig.idn.flush()
    votes = sorted((t, k, v, b.vote, b.src) for (t, k), bs in rig.votes.items.items() for v, b in bs.items())
    imports = sorted((i.id, i.client, i.label, tuple(i.items), tuple(i.removed)) for i in rig.votes.imports.values())
    nicks = sorted((c, r["nick"]) for c, r in rig.app.wishes.nicks.data.items())
    disk = (rig.tmp / "identity.json").read_text(encoding="utf-8") if (rig.tmp / "identity.json").exists() else ""
    return json.dumps([votes, imports, nicks, rig.idn.alias, rig.idn.since, rig.idn.gone, disk,
                       sorted(map(tuple, rig.store.all_votes())), sorted(map(tuple, rig.store.all_imports()[0]))],
                      ensure_ascii=False, default=str)


def mine(rig, cid: str) -> dict:
    return {key: b.vote for (_t, key), ballots in rig.votes.items.items()
            for voter, b in ballots.items() if voter == cid and b.vote and not b.src}


class Guarded(unittest.TestCase):
    def test_pin_is_required_and_wrong_pin_is_refused(self):
        rig = make()
        before = state(rig)
        for method, path, body in (("GET", ACCOUNTS, None),
                                   ("POST", JOIN, {"accounts": ["0c662c", "792b05"], "confirm": True})):
            with self.subTest(path=path):
                r = admin(rig, method, path, body, pin=None)
                self.assertEqual((r.status, r.json["pin"]), (401, "required"))
                r = admin(rig, method, path, body, pin={"x-ytdj-pin": "000000"})
                self.assertEqual((r.status, r.json["pin"]), (403, "wrong"))
                self.assertNotIn(b"0c662c", r.body)
                self.assertNotIn("accounts", r.json)
        self.assertEqual(state(rig), before)
        # stejná brzda jako nastavení (F-BEZP-10): po 5 špatných PINech je zavřeno i pro správný
        for _ in range(3):
            admin(rig, "GET", ACCOUNTS, pin={"x-ytdj-pin": "000000"})
        r = admin(rig, "POST", JOIN, {"accounts": ["0c662c", "792b05"], "confirm": True})
        self.assertEqual((r.status, r.json["pin"]), (429, "locked"))
        self.assertEqual(state(rig), before)
        routes = {(r.path, m): r.endpoint for r in rig.srv._starlette.routes
                  for m in getattr(r, "methods", None) or ()}
        self.assertTrue(routes[(ACCOUNTS, "GET")].admin_only)
        self.assertTrue(routes[(JOIN, "POST")].admin_only)

    def test_list_shows_only_the_end_of_the_id(self):
        rig = make()
        r = admin(rig, "GET", ACCOUNTS)
        self.assertEqual(r.status, 200)
        rows = r.json["accounts"]
        # všichni, nejstarší první; Petr z rigu má jen přezdívku
        self.assertEqual([a["id"] for a in rows if a["nick"].lower() == "robert"], ["0c662c", "792b05", "bbffeb"])
        self.assertEqual({a["nick"] for a in rows}, {"Robert", "robert", "Eva", "Jana", "Petr"})
        for cid in (FIRST, MIDDLE, LAST, TWIN, ti.JANA, ti.PETR):
            self.assertNotIn(cid.encode(), r.body)
        # přezdívka bez ohledu na velikost písmen a diakritiku
        r = admin(rig, "GET", ACCOUNTS, query="nick=R%C3%B3BERT")
        rows = r.json["accounts"]
        self.assertEqual([(a["id"], a["nick"], a["votes"], a["imports"], a["songs"], a["removed"]) for a in rows],
                         [("0c662c", "Robert", 2, 0, 0, 0), ("792b05", "Robert", 0, 1, 4, 1),
                          ("bbffeb", "robert", 2, 0, 0, 0)])
        self.assertLess(rows[0]["first"], rows[1]["first"])
        self.assertLess(rows[1]["first"], rows[2]["first"])
        self.assertTrue(all(a["last"] >= a["first"] and a["browsers"] == 1 for a in rows))
        self.assertEqual(admin(rig, "GET", ACCOUNTS, query="nick=Nikdo").json["accounts"], [])

    def test_suffix_must_name_exactly_one_account(self):
        rig = make()
        before = state(rig)
        for accounts, status, text in (
                (["0c662c"], 400, "Zadej dva až 12 účtů"),
                ("0c662c", 400, "Zadej dva až 12 účtů"),
                (["0c662c", "b05"], 400, "je moc krátké"),
                (["0c662c", "zzzzzz"], 404, "končí na ‚zzzzzz‘, tu není"),
                (["0c662c", "9b05"], 200, ""),          # delší konec už je jednoznačný (Eva)
                (["0c662c", "…0c662c"], 409, "jeden a tentýž účet"),
                (["0c662c", "2b05", "bbffeb"], 200, "")):
            with self.subTest(accounts=accounts):
                r = admin(rig, "POST", JOIN, {"accounts": accounts, "confirm": False})
                self.assertEqual(r.status, status, r.body)
                self.assertIn(text, r.json.get("error", ""))
        # nejednoznačný konec: odmítne a vyjmenuje kandidáty (zase jen konce id)
        twin2 = "web-eeee-ota-yy-7792b05"
        T.account(rig, twin2, "Ota", 1, (T.POHODA, -1))
        before = state(rig)
        r = admin(rig, "POST", JOIN, {"accounts": ["0c662c", "792b05"], "confirm": True})
        self.assertEqual(r.status, 409)
        self.assertIn("končí víc účtů", r.json["error"])
        self.assertEqual([(c["id"], c["nick"]) for c in r.json["candidates"]], [("792b05", "Robert"), ("792b05", "Ota")])
        self.assertNotIn(MIDDLE.encode(), r.body)
        self.assertNotIn(twin2.encode(), r.body)
        self.assertEqual(state(rig), before)
        r = admin(rig, "POST", JOIN, {"accounts": ["0c662c", "robert-792b05"], "confirm": False})
        self.assertEqual((r.status, r.json["joined"][0]["nick"]), (200, "Robert"))


class Join(unittest.TestCase):
    def test_dry_run_says_what_would_happen_and_changes_nothing(self):
        rig = make()
        before = state(rig)
        with T.Events() as ev:
            r = admin(rig, "POST", JOIN, {"accounts": ["bbffeb", "792b05", "0c662c"]})
        self.assertEqual((r.status, r.json["done"]), (200, False))
        plan = r.json
        # vyhrává nejstarší účet, ať ho správce napíše kdekoli — vybrat si ho nemůže
        self.assertEqual(plan["kept"]["id"], "0c662c")
        self.assertEqual([(j["id"], j["moved"], j["conflicts"], j["imports"], j["songs"], j["removed"])
                          for j in plan["joined"]], [("bbffeb", 1, 1, 0, 0, 0), ("792b05", 0, 0, 1, 4, 1)])
        self.assertEqual((plan["nick"], plan["nicks"]), ("Robert", ["Robert", "robert"]))
        self.assertEqual(plan["after"], {"votes": 3, "imports": 1, "songs": 4})
        self.assertEqual(state(rig), before)
        self.assertEqual(rig.idn.resolve(LAST), LAST)
        self.assertEqual(ev.kinds(), ["identity.admin_join"])
        self.assertTrue(ev.got[0]["dry"])
        # plán říká totéž, co pak spojení udělá
        with T.Events() as ev:
            done = admin(rig, "POST", JOIN, {"accounts": ["bbffeb", "792b05", "0c662c"], "confirm": True}).json
        self.assertTrue(done["done"])
        self.assertEqual(done["joined"], plan["joined"])
        merges = {e["younger"]: e for e in ev.of("identity.merge")}
        self.assertEqual({(e["votes"], e["imports"]) for e in merges.values()}, {(1, 0), (0, 1)})
        self.assertEqual({e["older"] for e in merges.values()}, {identity.tag_of(FIRST)})
        self.assertEqual((done["kept"]["votes"], done["kept"]["imports"], done["kept"]["songs"],
                          done["kept"]["browsers"]), (3, 1, 4, 3))

    def test_three_accounts_become_the_oldest_one_in_every_browser(self):
        rig = make()
        zprava, dama, pohoda = (rig.votes.song_key_for(t) for t in (T.ZPRAVA, T.DAMA, T.POHODA))
        self.assertEqual((rig.votes.tally(SONG, dama).up, rig.votes.tally(SONG, dama).down), (1, 1))
        with T.Events() as ev:
            r = admin(rig, "POST", JOIN, {"accounts": ["792b05", "bbffeb", "0c662c"], "confirm": True})
        self.assertEqual((r.status, r.json["done"], r.json["kept"]["id"]), (200, True, "0c662c"))
        for cid in (FIRST, MIDDLE, LAST, TWIN):
            self.assertNotIn(cid.encode(), r.body)
        # stejná pravidla jako F-NICK-09: hlasy přešly, kde hlasovaly oba, platí starší
        self.assertEqual(mine(rig, FIRST), {dama: 1, zprava: -1, pohoda: 1})
        self.assertEqual((mine(rig, MIDDLE), mine(rig, LAST)), ({}, {}))
        self.assertEqual((rig.votes.tally(SONG, dama).up, rig.votes.tally(SONG, dama).down), (1, 0))
        self.assertEqual(mine(rig, TWIN), {zprava: 1})  # cizích účtů se nic nedotklo
        self.assertEqual(mine(rig, ti.JANA), {zprava: -1})
        nicks = rig.app.wishes.nicks
        self.assertEqual((nicks.get(FIRST), nicks.get(MIDDLE), nicks.get(LAST), nicks.get(TWIN)),
                         ("Robert", "", "", "Eva"))
        imp = rig.votes.imports[rig.iid]
        self.assertEqual((imp.client, imp.name, list(imp.removed)), (FIRST, "Do práce", [rig.k1]))
        # každý ze tří prohlížečů je teď ten jeden účet: playlist je „můj“ a jde upravovat
        songs = f"/api/votes/imports/{rig.iid}/songs"
        k2 = P.key_of("a", 2)
        for cid in (FIRST, MIDDLE, LAST):
            with self.subTest(browser=cid[-6:]):
                me = T.call(rig, "GET", "/api/me", origin=None, query=f"client={cid}").json
                self.assertEqual((me["client"], me["nick"]), (FIRST, "Robert"))
                lst = T.call(rig, "GET", "/api/votes/imports", origin=None, query=f"client={cid}").json
                self.assertEqual([(i["mine"], i["title"], i["songs"], i["removed"]) for i in lst["imports"]],
                                 [(True, "Do práce", 4, 1)])
                votes = T.call(rig, "GET", "/api/votes", origin=None, query=f"client={cid}").json
                self.assertEqual(len(votes["mine"]), 3)
                r = T.call(rig, "POST", songs, {"client": cid, "key": k2, "removed": True})
                self.assertEqual((r.status, r.json["import"]["songs"]), (200, 3))
                r = T.call(rig, "POST", songs, {"client": cid, "key": k2, "removed": False})
                self.assertEqual(r.status, 200)
                r = T.call(rig, "POST", f"/api/votes/imports/{rig.iid}/rename", {"client": cid, "label": "Do práce"})
                self.assertEqual(r.status, 200)
        # stránka se starým id se při srovnání dozví jednou větou, co se stalo (jako F-NICK-09)
        r = T.call(rig, "POST", "/api/identity/sync", {"client": LAST})
        self.assertEqual((r.json["client"], r.json["note"]),
                         (FIRST, {"kind": "merged", "dropped": "robert", "kept": "Robert"}))
        # cizí člověk dál nesmí nic
        self.assertEqual(T.call(rig, "POST", songs, {"client": TWIN, "key": k2, "removed": True}).status, 403)
        # v provozním logu jen konce id a veřejné značky, nikdy celé id
        self.assertEqual(ev.kinds(), ["identity.merge", "identity.merge", "identity.admin_join"])
        j = ev.of("identity.admin_join")[0]
        self.assertEqual((j["dry"], j["kept"], j["joined"], j["votes"], j["conflicts"], j["imports"]),
                         (False, "0c662c", ["792b05", "bbffeb"], 1, 1, 1))
        logged = json.dumps(ev.got, ensure_ascii=False)
        for cid in (FIRST, MIDDLE, LAST):
            self.assertNotIn(cid, logged)
        self.assertNotIn(ti.PIN, logged)
        # podruhé už není co spojovat
        r = admin(rig, "POST", JOIN, {"accounts": ["792b05", "bbffeb", "0c662c"], "confirm": True})
        self.assertEqual(r.status, 409)
        rows = admin(rig, "GET", ACCOUNTS, query="nick=robert").json["accounts"]
        self.assertEqual([(a["id"], a["votes"], a["imports"], a["browsers"]) for a in rows], [("0c662c", 3, 1, 3)])

    def test_join_survives_a_restart(self):
        rig = make()
        admin(rig, "POST", JOIN, {"accounts": ["0c662c", "792b05", "bbffeb"], "confirm": True})
        if hasattr(rig.store, "flush"):
            rig.store.flush()
        rig.idn.flush()
        rig.app.wishes.nicks.flush()
        book = asyncio.run(VoteBook(rig.store, Config(**DEFAULTS)).aload())
        imp = book.imports[rig.iid]
        self.assertEqual((imp.client, imp.name, list(imp.removed), len(imp.items)), (FIRST, "Do práce", [rig.k1], 4))
        self.assertEqual((len(P.up_of(book, FIRST)), P.up_of(book, MIDDLE), P.up_of(book, LAST)), (2 + 4, set(), set()))
        again = identity.Identity(rig.app, rig.tmp / "identity.json")
        self.assertTrue(again.wait_loaded())
        self.assertEqual((again.resolve(MIDDLE), again.resolve(LAST), again.resolve(FIRST)), (FIRST, FIRST, FIRST))
        # nový běh: tři prohlížeče jsou pořád jeden účet a playlist upravují
        rig.app.votes = rig.app.imports.book = book
        again.origins = rig.idn.origins
        rig.app.identity = again
        for cid in (MIDDLE, LAST, FIRST):
            lst = T.call(rig, "GET", "/api/votes/imports", origin=None, query=f"client={cid}").json
            self.assertTrue(lst["imports"][0]["mine"], cid[-6:])
            r = T.call(rig, "POST", f"/api/votes/imports/{rig.iid}/songs",
                       {"client": cid, "key": rig.k1, "removed": cid != MIDDLE})
            self.assertEqual(r.status, 200, r.body)
        rows = T.call(rig, "GET", ACCOUNTS, origin=None, query="nick=robert", extra=PIN).json["accounts"]
        self.assertEqual([(a["id"], a["browsers"], a["imports"]) for a in rows], [("0c662c", 3, 1)])

    def test_account_without_a_nick_or_older_alias_can_be_named(self):
        """Konec dřívějšího (už spojeného) id vede na platný účet; účet jen s hlasy jde spojit taky."""
        rig = make()
        admin(rig, "POST", JOIN, {"accounts": ["0c662c", "bbffeb"], "confirm": True})
        r = admin(rig, "POST", JOIN, {"accounts": ["bbffeb", "792b05"]})
        self.assertEqual((r.status, r.json["kept"]["id"], [j["id"] for j in r.json["joined"]]),
                         (200, "0c662c", ["792b05"]))
        self.assertEqual(admin(rig, "POST", JOIN, {"accounts": ["bbffeb", "0c662c"]}).status, 409)


class Cli(unittest.TestCase):
    """`python -m ytdj.identity` proti běžícímu jukeboxu (jako `python -m ytdj.issues`)."""

    def run_cli(self, rig, *args: str, pin_file: Path | None = None):
        async def go():
            await rig.srv.start()
            try:
                out, err = io.StringIO(), io.StringIO()

                def work():
                    with mock.patch.object(sys, "stderr", err), \
                            mock.patch.object(adminpin, "PIN_FILE", pin_file or rig.tmp / "nic"):
                        return identity.main(["--url", rig.srv.url, *args], out=out)

                code = await asyncio.to_thread(work)
                return code, out.getvalue(), err.getvalue()
            finally:
                await rig.srv.stop()

        rig.srv.host, rig.srv.port = "127.0.0.1", 0
        return asyncio.run(go())

    def test_list_plan_and_join_against_a_running_server(self):
        rig = make()
        pin_file = rig.tmp / "admin-pin"
        code, out, err = self.run_cli(rig, "list", "--nick", "Robert", pin_file=pin_file)
        self.assertEqual((code, err), (0, ""))
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 4)
        self.assertRegex(lines[0], r"^…0c662c  Robert\s+poprvé \d+\. \d+\. \d{4}\s+naposledy \d+\. \d+\. \d{4}\s+"
                                   r"2 hlasy, 0 playlistů$")
        self.assertRegex(lines[1], r"^…792b05  Robert .* 0 hlasů, 1 playlist \(4 písničky, 1 vyřazeno\)$")
        self.assertEqual(lines[3], "— 3 účty s přezdívkou Robert")
        # bez --yes jen plán
        before = state(rig)
        code, out, err = self.run_cli(rig, "join", "bbffeb", "792b05", "0c662c", pin_file=pin_file)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(state(rig), before)
        self.assertIn("Spojil bych: 3 účty do nejstaršího z nich.", out)
        self.assertRegex(out, r"připojí se  …bbffeb  robert  \(poprvé [\d. ]+\) — přejde: 1 hlas "
                              r"\(1× hlasovaly oba účty — platí hlas staršího\), 0 playlistů")
        self.assertRegex(out, r"připojí se  …792b05  Robert  \(poprvé [\d. ]+\) — přejde: 0 hlasů, 1 playlist "
                              r"\(4 písničky, 1 vyřazeno a zůstane vyřazeno\)")
        self.assertRegex(out, r"zůstane     …0c662c  Robert  \(poprvé [\d. ]+\) — má 2 hlasy, 0 playlistů")
        self.assertIn("POZOR: účty mají různé přezdívky (Robert, robert)", out)
        self.assertIn("Přezdívka spojeného účtu: Robert.", out)
        self.assertIn("Po spojení bude mít 3 hlasy a 1 playlist (4 písničky).", out)
        self.assertTrue(out.rstrip().endswith("Zatím se nic nezměnilo. Spojení nejde vrátit — provedeš ho "
                                              "stejným příkazem s --yes."))
        # s --yes spojí (PIN tentokrát z --pin)
        code, out, err = self.run_cli(rig, "--pin", ti.PIN, "join", "bbffeb", "792b05", "0c662c", "--yes")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Spojeno: 3 účty do nejstaršího z nich.", out)
        self.assertRegex(out, r"zůstal      …0c662c  Robert  \(poprvé [\d. ]+\) — teď má 3 hlasy, 1 playlist "
                              r"\(4 písničky, 1 vyřazeno\), 3 prohlížeče")
        self.assertEqual((rig.idn.resolve(MIDDLE), rig.idn.resolve(LAST)), (FIRST, FIRST))
        self.assertEqual(rig.votes.imports[rig.iid].client, FIRST)
        for text in (out,):
            for cid in (FIRST, MIDDLE, LAST):
                self.assertNotIn(cid, text)
        code, out, err = self.run_cli(rig, "list", "--nick", "robert", pin_file=pin_file)
        self.assertEqual(out.strip().splitlines()[-1], "— 1 účet s přezdívkou robert")

    def test_refusals_are_sentences(self):
        rig = make()
        T.account(rig, "web-eeee-ota-yy-7792b05", "Ota", 1, (T.POHODA, -1))
        before = state(rig)
        code, out, err = self.run_cli(rig, "list")  # soubor s PINem není
        self.assertEqual((code, out), (2, ""))
        self.assertIn("--pin", err)
        code, out, err = self.run_cli(rig, "--pin", "000000", "join", "0c662c", "bbffeb", "--yes")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("403", err)
        self.assertNotIn("000000", err)
        code, out, err = self.run_cli(rig, "--pin", ti.PIN, "join", "0c662c", "792b05", "--yes")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("končí víc účtů", err)
        self.assertRegex(err, r"…792b05  Robert")
        self.assertRegex(err, r"…792b05  Ota")
        code, out, err = self.run_cli(rig, "--pin", ti.PIN, "join", "0c662c")
        self.assertEqual(code, 2)
        self.assertEqual(state(rig), before)
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            code = identity.main(["--url", "http://127.0.0.1:9", "--pin", ti.PIN, "list"], out=io.StringIO())
        self.assertEqual(code, 1)
        self.assertIn("Nedovolal jsem se", err.getvalue())


if __name__ == "__main__":
    unittest.main()
