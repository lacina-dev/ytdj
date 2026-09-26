"""Přezdívky: kontrola, paměť na straně serveru, API /api/me a jméno všude stejné.

    YTDJ_EVENTS_FILE=/tmp/x.jsonl .venv/bin/python -m unittest tests.test_nicks -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_TMP = tempfile.mkdtemp(prefix="ytdj-nicks-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)
os.environ.setdefault("YTDJ_EVENTS_FILE", str(Path(_TMP) / "events.jsonl"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_wishes import Rig, WebApp, body, req, run  # noqa: E402
from ytdj import wishes  # noqa: E402
from ytdj.display import Censor  # noqa: E402
from ytdj.nicks import NICK_MAX, NickError, Nicks, clean_nick, tag_of  # noqa: E402
from ytdj.web import server as web  # noqa: E402


def get(**query):
    r = req(method="GET")
    r.query_params = query
    return r


def raw(rig: Rig):
    """submit bez automatického id klienta (Rig ho jinak dává podle jména)."""
    return wishes.WishQueue.submit.__get__(rig.wq)


class Validation(unittest.TestCase):
    def test_trimmed_and_collapsed(self):
        self.assertEqual(clean_nick("   Petr    K.  ", Censor()), "Petr K.")
        self.assertEqual(clean_nick("Zdeněk", Censor()), "Zdeněk")
        self.assertEqual(clean_nick("Jana-Marie", Censor()), "Jana-Marie")

    def test_length(self):
        with self.assertRaises(NickError):
            clean_nick("P", Censor())
        with self.assertRaises(NickError):
            clean_nick("x" * (NICK_MAX + 1), Censor())
        self.assertEqual(clean_nick("x" * NICK_MAX, Censor()), "x" * NICK_MAX)

    def test_junk_and_reserved(self):
        for bad in ("<b>hi</b>", "12345", "😀😀", "displej", "Host", "DJ", "   "):
            with self.assertRaises(NickError, msg=bad):
                clean_nick(bad, Censor())

    def test_office_filter_with_a_kind_message(self):
        for bad in ("kurva", "Debil Pepa", "píča"):
            with self.assertRaises(NickError) as cm:
                clean_nick(bad, Censor())
            self.assertIn("jinou", str(cm.exception))
        self.assertEqual(clean_nick("Picnic", Censor()), "Picnic")  # nevinné slovo projde
        # správce filtr vypnul → přezdívka projde
        self.assertEqual(clean_nick("debil", Censor(enabled=False)), "debil")
        # slova navíc z nastavení
        with self.assertRaises(NickError):
            clean_nick("Trouba", Censor(extra=["troub"]))


class Book(unittest.TestCase):
    def test_persisted_per_client(self):
        path = Path(tempfile.mkdtemp(dir=_TMP)) / "nicks.json"
        book = Nicks(path)
        self.assertFalse(book.set("web-aaaaaa", "Petr"))
        self.assertTrue(book.set("web-bbbbbb", "petr"))  # stejné jméno, jiný člověk
        self.assertFalse(book.shared("web-aaaaaa", "Karel"))  # sám se sebou se nepere
        self.assertFalse(Nicks(None).set("web-aaaaaa", "Petr") or False)
        again = Nicks(path)
        self.assertEqual(again.get("web-aaaaaa"), "Petr")
        self.assertEqual(again.get("web-bbbbbb"), "petr")
        self.assertEqual(again.get("web-cccccc"), "")

    def test_shared_ignores_case_and_diacritics(self):
        book = Nicks(None)
        book.set("web-aaaaaa", "Šárka")
        self.assertTrue(book.shared("web-bbbbbb", "sarka"))

    def test_broken_file_is_not_fatal(self):
        path = Path(tempfile.mkdtemp(dir=_TMP)) / "nicks.json"
        path.write_text("{nope")
        self.assertEqual(Nicks(path).get("web-aaaaaa"), "")

    def test_tag_does_not_reveal_the_client(self):
        self.assertNotIn("aaaaaa", tag_of("web-aaaaaa"))
        self.assertEqual(tag_of("web-aaaaaa"), tag_of("web-aaaaaa"))
        self.assertNotEqual(tag_of("web-aaaaaa"), tag_of("web-bbbbbb"))


class Api(unittest.TestCase):
    def test_register_read_and_reject(self):
        async def go():
            async with Rig() as rig:
                srv = web.WebServer(WebApp(rig))
                me = body(await srv._me_get(get(client="web-petr001")))
                self.assertEqual(me["nick"], "")
                self.assertEqual(me["tag"], tag_of("web-petr001"))

                resp = await srv._me_post(req({"client": "web-petr001", "nick": "  Petr  "}))
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(body(resp)["nick"], "Petr")
                self.assertNotIn("shared", body(resp))
                self.assertEqual(body(await srv._me_get(get(client="web-petr001")))["nick"], "Petr")

                rude = await srv._me_post(req({"client": "web-petr001", "nick": "kurva"}))
                self.assertEqual(rude.status_code, 400)
                self.assertIn("jinou", body(rude)["error"])
                self.assertNotIn("kurva", body(rude)["error"])
                # odmítnutá přezdívka nepřepíše tu dobrou
                self.assertEqual(rig.wq.nicks.get("web-petr001"), "Petr")

                short = await srv._me_post(req({"client": "web-petr001", "nick": "P"}))
                self.assertEqual(short.status_code, 400)
                nocid = await srv._me_post(req({"nick": "Karel"}))
                self.assertEqual(nocid.status_code, 400)
                self.assertEqual((await srv._me_get(get())).status_code, 400)

                other = body(await srv._me_post(req({"client": "web-petr002", "nick": "PETR"})))
                self.assertTrue(other.get("shared"))  # jen upozornění, uloží se
                self.assertEqual(rig.wq.nicks.get("web-petr002"), "PETR")

        run(go())

    def test_nick_is_the_label_everywhere(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                srv = web.WebServer(WebApp(rig))
                cid = "web-karel001"
                await srv._me_post(req({"client": cid, "nick": "Karel"}))
                # stránka poslala jiné (staré) jméno — platí zaregistrovaná přezdívka
                resp = await srv._prompt(req({"text": "Holky z naší školky", "who": "host ·001",
                                              "client": cid, "wait": False}))
                self.assertEqual(resp.status_code, 202)
                data = body(resp)
                self.assertEqual(data["who"], "Karel")
                w = rig.wq.by_id(data["id"])
                await rig.until(lambda: w.state in ("queued", "playing"))
                st = body(await srv._status(req(method="GET")))
                mine = [r for r in st["requests"] if r["id"] == w.id][0]
                self.assertEqual(mine["who"], "Karel")
                self.assertEqual(mine["who_key"], tag_of(cid))  # "tvoje" na webu, barva na displeji
                reason = st["current"]["reason"]
                if reason.get("kind") == "wish":  # hraje už jeho přání
                    self.assertEqual(reason["who_key"], tag_of(cid))
                self.assertEqual(rig.wq.reason_for(w.tracks[0].id)["who_key"], tag_of(cid))
                tagged = [q["req"] for q in st["queue"] if q.get("req")]
                self.assertTrue(all(t["who_key"] for t in tagged), st["queue"])
                self.assertNotIn(cid, str(st))  # id klienta ven nejde
                self.assertNotIn(data["token"], str(st))
                self.assertIn("Karel", st["people"])

                # přejmenování: rozpracovaná přání i důvod "hraje" hned s novým jménem
                await srv._me_post(req({"client": cid, "nick": "Karel V."}))
                self.assertEqual(w.who, "Karel V.")
                st = body(await srv._status(req(method="GET")))
                self.assertEqual([r for r in st["requests"] if r["id"] == w.id][0]["who"], "Karel V.")

                # kdo přeskočil — podle přezdívky, ne podle "who" z těla
                await srv._control(req({"action": "next", "who": "někdo jiný", "client": cid}))
                self.assertEqual(rig.wq._skip_note[1], "Karel V.")

                nick_events = [f for k, f in rig.events if k == "web.nick"]
                self.assertEqual(nick_events[-1]["nick"], "Karel V.")
                self.assertEqual(nick_events[-1]["was"], "Karel")

        run(go())

    def test_old_clients_without_nick_still_work(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                srv = web.WebServer(WebApp(rig))
                resp = await srv._prompt(req({"text": "Holky z naší školky", "who": "Jana",
                                              "client": "web-jana0001", "wait": False}))
                self.assertEqual(body(resp)["who"], "Jana")
                panel = await srv._prompt(req({"text": "Dancing Queen", "source": "panel"}, ua="ytdj-panel"))
                self.assertEqual(panel.status_code, 200)
                self.assertEqual(rig.wq.by_id(body(panel)["id"]).who, "displej")

        run(go())


class Telemetry(unittest.TestCase):
    def test_people_are_counted_by_client_not_by_label(self):
        """Pi 26. 9.: jeden prohlížeč poslal přání jako "host ·8e7" a pak "karel"
        a log hlásil tři lidi místo dvou."""
        async def go():
            async with Rig(codex_delay=0.5) as rig:
                await rig.background()
                sub = raw(rig)
                sub("pusť Kabát", "", "web", client={"id": "web-aaaaaa8e7"})
                sub("přidej Olympic", "karel", "web", client={"id": "web-aaaaaa8e7"})
                sub("Dancing Queen", "Jana", "web", client={"id": "web-bbbbbbbb1"})
                created = [f for k, f in rig.events if k == "request.created"]
                self.assertEqual(created[-1]["active_people"], 2)

        run(go())


class HonestNext(unittest.TestCase):
    def test_play_next_says_it_waits_for_someone_elses_wish(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                wq = rig.wq
                p = wq.submit("pusť Kabát", "Petr")
                await rig.until(lambda: p.state == "playing")
                cur = rig.fake.current_vid()
                j = wq.submit("Holky z naší školky", "Jana", play_next=True)
                await rig.until(lambda: j.state == "queued" and bool(j.reply))
                self.assertEqual(rig.fake.current_vid(), cur)  # nic se neutnulo
                self.assertIn("hned po téhle skladbě", j.reply)
                self.assertIn("co si přeje Petr", j.reply)  # poctivě: proč to není hned

        run(go())


if __name__ == "__main__":
    unittest.main()
