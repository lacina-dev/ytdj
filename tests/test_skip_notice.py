"""Kdo přeskočil — hláška pro všechny (hlášení z jukeboxu #23, POZADAVKY #76,
F-PRESKOK-10 až F-PRESKOK-12): fronta přání, web a stránka, bez sítě.

    .venv/bin/python -m unittest tests.test_skip_notice -v
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ytdj-skipnotice-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)
os.environ.setdefault("YTDJ_EVENTS_FILE", str(Path(_TMP) / "events.jsonl"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_controls as tc  # noqa: E402
import test_wishes as tw  # noqa: E402
from ytdj import wishes  # noqa: E402
from ytdj.config import DEFAULTS  # noqa: E402
from ytdj.nicks import tag_of  # noqa: E402
from ytdj.web import server as web  # noqa: E402

run = tw.run
PETR, JANA, KAREL = "client-petr", "client-jana", "client-karel"
INDEX = Path(__file__).resolve().parents[1] / "ytdj/web/static/index.html"


def post(body: dict, **kw):
    return tw.req(body, **kw)


async def skipped(rig, srv, body: dict, **kw) -> tuple[str, str]:
    """Pošle Další a počká, až přehrávač přeskočí. Vrací (přeskočená, nová)."""
    before = rig.fake.current_vid()
    r = await srv._control(post({"action": "next", **body}, **kw))
    assert r.status_code == 200, r.body
    await rig.until(lambda: rig.fake.current_vid() != before and rig.wq.current_vid == rig.fake.current_vid())
    await rig.settle(0.05)
    return before, rig.fake.current_vid()


def label(rig, vid_: str) -> str:
    t = rig.player._tracks[vid_]
    return f"{t.title} ({t.artist})"


class WhoSkipped(unittest.TestCase):
    """F-PRESKOK-10: kdo přeskočil hrající skladbu, se dozví všichni."""

    def test_next_on_the_web_names_the_person_and_the_song(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                await rig.background()
                srv = web.WebServer(tw.WebApp(rig))
                self.assertEqual((await srv._snapshot())["notices"], [])
                old, _ = await skipped(rig, srv, {"client": KAREL, "who": "Karel"})
                snap = await srv._snapshot()
                (n,) = snap["notices"]
                self.assertEqual(n["text"], f"Karel · přeskočeno: {label(rig, old)}")
                self.assertEqual((n["kind"], n["who"], n["who_key"], n["src"], n["n"]),
                                 ("skip", "Karel", tag_of(KAREL), "web", 1))
                self.assertEqual((n["title"], n["artist"]),
                                 (rig.player._tracks[old].title, rig.player._tracks[old].artist))
                self.assertTrue(isinstance(n["id"], int) and n["at"] > 0)
                # ven jde přezdívka a veřejná značka, nikdy id klienta (F-BEZP-04)
                self.assertNotIn(KAREL, json.dumps(snap))
                ev = [f for k, f in rig.events if k == "skip.notice"]
                self.assertEqual((ev[0]["who"], ev[0]["src"], ev[0]["video_id"]), ("Karel", "web", old))

        run(go())

    def test_registered_nick_wins_and_goes_through_the_office_filter(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                await rig.background()
                srv = web.WebServer(tw.WebApp(rig))
                rig.wq.set_nick(JANA, "Jana")
                await skipped(rig, srv, {"client": JANA, "who": "Někdo Jiný"})
                self.assertEqual(rig.wq.notices[-1]["who"], "Jana")  # jméno z požadavku nerozhoduje
                await skipped(rig, srv, {"client": "client-rude", "who": "kurva"})
                self.assertNotIn("kurva", json.dumps(await srv._snapshot(), ensure_ascii=False))

        run(go())

    def test_display_speaker_key_and_no_nick(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                await rig.background()
                srv = web.WebServer(tw.WebApp(rig))
                old, _ = await skipped(rig, srv, {}, ua="ytdj-panel")
                n = rig.wq.notices[-1]
                self.assertEqual((n["text"], n["src"], n["who_key"]),
                                 (f"Displej · přeskočeno: {label(rig, old)}", "panel", ""))
                old, _ = await skipped(rig, srv, {"source": "key"}, ua="ytdj-panel")
                n = rig.wq.notices[-1]
                self.assertEqual((n["text"], n["src"]),
                                 (f"Tlačítko na repráku · přeskočeno: {label(rig, old)}", "key"))
                # z webu se za tlačítko na repráku vydávat nejde; bez přezdívky poctivě "někdo"
                old, _ = await skipped(rig, srv, {"source": "key", "client": "client-anon"})
                n = rig.wq.notices[-1]
                self.assertEqual((n["text"], n["src"]),
                                 (f"Někdo bez přezdívky · přeskočeno: {label(rig, old)}", "web"))
                self.assertEqual(n["who_key"], tag_of("client-anon"))

        run(go())

    def test_skip_by_text_command_and_by_click_into_the_queue(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                await rig.background()
                srv = web.WebServer(tw.WebApp(rig))
                old = rig.fake.current_vid()
                r = await srv._prompt(post({"text": "další", "who": "Petr", "client": PETR}))
                self.assertEqual(tw.body(r)["reply"], "Přeskakuju.")
                await rig.until(lambda: rig.fake.current_vid() != old and rig.wq.notices)
                n = rig.wq.notices[-1]
                self.assertEqual((n["text"], n["src"]), (f"Petr · přeskočeno: {label(rig, old)}", "text"))
                # klik do fronty, který hrající skladbu utne
                await rig.settle(0.1)
                up, cut = rig.upcoming(), rig.fake.current_vid()
                rig.player._res_ready.update(up)
                ok, _, info = await rig.wq.play_queued(up[2], by="Jana", client=JANA)
                self.assertEqual((ok, info["mode"]), (True, "now"))
                await rig.until(lambda: rig.fake.current_vid() == up[2] and rig.wq.notices[-1]["who"] == "Jana")
                n = rig.wq.notices[-1]
                self.assertEqual((n["text"], n["src"]), (f"Jana · přeskočeno: {label(rig, cut)}", "jump"))
                # klik, který nic neutne ("jako další"), hláška není
                count = len(rig.wq.notices)
                rig.player._res_ready.clear()
                ok, _, info = await rig.wq.play_queued(rig.upcoming()[2], by="Karel", client=KAREL)
                self.assertEqual((ok, info["mode"]), (True, "next"))
                await rig.settle(0.2)
                self.assertEqual(len(rig.wq.notices), count)
                self.assertIsNone(rig.wq._skip_who)

        run(go())

    def test_skip_asked_for_by_a_wish_carries_the_askers_name(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                await rig.background()
                old = rig.fake.current_vid()
                w = tw.Wish(id="w1", token="t", who="Jana", source="web", text="přeskoč to prosím",
                            created=0.0, mono=0.0, cid=JANA)
                await rig.wq._control(tw.Intent(kind="control", text=w.text, control="skip"), w)
                await rig.until(lambda: rig.fake.current_vid() != old and rig.wq.notices)
                n = rig.wq.notices[-1]
                self.assertEqual((n["text"], n["src"], n["who_key"]),
                                 (f"Jana · přeskočeno: {label(rig, old)}", "wish", tag_of(JANA)))
                # bez člověka (automatický tah DJe) hláška není
                old = rig.fake.current_vid()
                await rig.wq._control(tw.Intent(kind="control", text="", control="skip"))
                await rig.until(lambda: rig.fake.current_vid() != old)
                await rig.settle(0.1)
                self.assertEqual(len(rig.wq.notices), 1)

        run(go())

    def test_what_is_not_a_persons_skip_says_nothing(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                await rig.background()
                srv = web.WebServer(tw.WebApp(rig))
                wq, p = rig.wq, rig.player

                async def changed(action) -> None:
                    before = rig.fake.current_vid()
                    res = action()
                    if asyncio.iscoroutine(res):
                        await res
                    await rig.until(lambda: rig.fake.current_vid() != before
                                    and wq.current_vid == rig.fake.current_vid())
                    await rig.settle(0.05)

                await changed(rig.fake.finish_current)  # dohrála sama
                await changed(lambda: p.skip(by_user=False))  # vyměnil ji DJ (nové přání, hlasování)
                await changed(lambda: p.skip())  # přeskočení, u kterého není člověk (terminál)
                rig.fake.fail_once.add(rig.upcoming()[0])
                await changed(rig.fake.finish_current)  # další skladba se nedala přehrát
                await tc.sounds(rig, 200.0, 10.0)
                await srv._control(post({"action": "seek", "value": 5000, "client": KAREL}))  # posun na konec
                await srv._control(post({"action": "mute", "value": True, "client": KAREL}))
                await srv._control(post({"action": "pause", "client": KAREL}))
                await srv._control(post({"action": "play", "client": KAREL}))
                await srv._control(post({"action": "volume", "value": 30, "client": KAREL}))
                await rig.settle(0.2)
                self.assertEqual(wq.notices, [])
                self.assertEqual((await srv._snapshot())["notices"], [])
                self.assertEqual(wq.skipped_by, {})
                # Další při výpadku jen posune místo navázání — nic se nepřeskočilo
                p._outage = {"reason": "network", "since": 0, "detail": "", "resume_entry": None}
                await srv._control(post({"action": "next", "client": KAREL, "who": "Karel"}))
                p._outage = None
                self.assertIsNone(wq._skip_who)
                # a když po něm skladbu vymění DJ, Karlovo jméno u toho není
                await changed(lambda: p.skip(by_user=False))
                self.assertEqual(wq.notices, [])

        run(go())

    def test_double_tap_is_one_notice(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                await rig.background()
                srv = web.WebServer(tw.WebApp(rig))
                body = {"action": "next", "client": KAREL, "who": "Karel"}
                old = rig.fake.current_vid()
                await srv._control(post(body))
                await srv._control(post(body))  # dvojí ťuknutí (F-PRESKOK-06)
                await rig.until(lambda: rig.fake.current_vid() != old)
                await rig.settle(0.2)
                self.assertEqual([(n["n"], n["title"]) for n in rig.wq.notices],
                                 [(1, rig.player._tracks[old].title)])

        run(go())


class Coalescing(unittest.TestCase):
    """F-PRESKOK-11: série přeskočení je jedna hláška; hlášky samy zmizí."""

    def test_a_run_of_skips_is_one_notice(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                await rig.background(10)
                srv = web.WebServer(tw.WebApp(rig))
                ids = []
                last = ""
                for i in range(5):
                    last, _ = await skipped(rig, srv, {"client": KAREL, "who": "Karel"})
                    pub = (await srv._snapshot())["notices"]
                    self.assertEqual(len(pub), 1)  # nikdy se nevrství
                    self.assertEqual(pub[0]["n"], i + 1)
                    ids.append(pub[0]["id"])
                self.assertEqual(ids, sorted(set(ids)))  # každá změna má nové id — stránka ji ukáže jednou
                texts = {2: "2 skladby", 5: "5 skladeb"}
                self.assertEqual(pub[0]["text"],
                                 f"Karel · přeskočeno {texts[5]}, naposledy {label(rig, last)}")
                self.assertEqual(wishes._skipped_count(2), texts[2])
                # někdo jiný: nová hláška; pak zase Karel: nová série od jedné
                await skipped(rig, srv, {"client": JANA, "who": "Jana"})
                await skipped(rig, srv, {"client": KAREL, "who": "Karel"})
                self.assertEqual([(n["who"], n["n"]) for n in rig.wq.notices],
                                 [("Karel", 5), ("Jana", 1), ("Karel", 1)])

        run(go())

    def test_notices_expire_and_only_a_few_are_kept(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                await rig.background(12)
                srv = web.WebServer(tw.WebApp(rig))
                await skipped(rig, srv, {"client": KAREL, "who": "Karel"})
                first = rig.wq.notices[-1]["id"]
                self.assertEqual(len((await srv._snapshot())["notices"]), 1)
                rig.wq.notices[-1]["at"] -= wishes.NOTICE_TTL + 1  # o 21 s později
                self.assertEqual((await srv._snapshot())["notices"], [])  # stará se znovu neukáže
                # po vypršení už se neslučuje: další přeskočení je nová hláška od jedné
                await skipped(rig, srv, {"client": KAREL, "who": "Karel"})
                (n,) = (await srv._snapshot())["notices"]
                self.assertEqual(n["n"], 1)
                self.assertGreater(n["id"], first)
                for i in range(8):  # střídají se dva lidé: v paměti jen pár posledních
                    who = ("Jana", JANA) if i % 2 else ("Petr", PETR)
                    await skipped(rig, srv, {"client": who[1], "who": who[0]})
                self.assertEqual(len(rig.wq.notices), wishes.NOTICE_KEEP)
                self.assertEqual((wishes.NOTICE_TTL, wishes.NOTICE_KEEP), (20.0, 5))

        run(go())

    def test_everybody_gets_it_live(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                await rig.background()
                srv = web.WebServer(tw.WebApp(rig))
                rig.wq.on_change = srv.poke
                watcher = asyncio.create_task(tw.sse(srv, 0.8))
                await asyncio.sleep(0.25)
                old, _ = await skipped(rig, srv, {"client": KAREL, "who": "Karel"})
                chunks = await watcher
                states = [json.loads(c[6:]) for c in chunks if c.startswith("data: ")]
                self.assertEqual(states[0]["notices"], [])
                got = [s["notices"][-1]["text"] for s in states if s["notices"]]
                self.assertEqual(got[-1], f"Karel · přeskočeno: {label(rig, old)}")
                self.assertNotIn(KAREL, "".join(chunks))

        run(go())


class HistoryAndSwitch(unittest.TestCase):
    """F-PRESKOK-12: jméno u přeskočené skladby v Odehráno; vypínač v nastavení."""

    def test_history_row_says_who_skipped_it(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                tc.as_app(rig)
                await rig.background()
                srv = web.WebServer(tw.WebApp(rig))
                played = rig.fake.current_vid()
                rig.fake.finish_current()
                await rig.until(lambda: rig.fake.current_vid() != played)
                await rig.settle(0.1)
                old, _ = await skipped(rig, srv, {"client": KAREL, "who": "Karel"})
                rig.store.flush()
                srv._hist = None
                rows = {h["id"]: h for h in (await srv._snapshot())["history"]}
                self.assertEqual((rows[old]["outcome"], rows[old]["skipped_by"]), ("skipped", "Karel"))
                self.assertNotIn("skipped_by", rows[played])  # dohraná nemá nic
                self.assertNotIn("skipped_by", rows[rig.fake.current_vid()])
                # v databázi se nic nemění: jen "přeskočeno" jako dřív (jméno je v paměti)
                self.assertEqual({r.video_id: r.outcome for r in rig.store.recent_history(5)}[old],
                                 "skipped")
                self.assertLessEqual(len(rig.wq.skipped_by), wishes.SKIP_LABEL_KEEP)

        run(go())

    def test_switched_off_nobody_sees_anything(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                tc.as_app(rig)
                await rig.background()
                srv = web.WebServer(tw.WebApp(rig))
                rig.cfg.skip_notices = False
                old, _ = await skipped(rig, srv, {"client": KAREL, "who": "Karel"})
                rig.store.flush()
                srv._hist = None
                snap = await srv._snapshot()
                self.assertEqual(snap["notices"], [])
                self.assertNotIn("skipped_by", json.dumps(snap))
                self.assertNotIn("skip.notice", rig.kinds())
                rig.cfg.skip_notices = True  # platí hned, bez restartu
                await skipped(rig, srv, {"client": KAREL, "who": "Karel"})
                self.assertEqual(len((await srv._snapshot())["notices"]), 1)

        run(go())

    def test_setting_is_on_by_default_and_in_the_admin_form(self) -> None:
        self.assertIs(DEFAULTS["skip_notices"], True)
        self.assertIn("skip_notices", web.LIVE_KEYS)
        self.assertNotIn("skip_notices", web.RESTART_KEYS)
        self.assertEqual(web.FIELD_META["skip_notices"][0], "Hlásit všem, kdo přeskočil")
        self.assertIs(web.coerce_value("skip_notices", False), False)
        # nastavení jde jen s PINem správce jako všechno ostatní (F-BEZP-09)
        src = Path(web.__file__).read_text()
        self.assertIn('Route("/api/config", _safe(self._admin_only(self._config_post)), methods=["POST"])', src)

    def test_skip_accounting_is_unchanged(self) -> None:
        """Hláška nic nemění na tom, komu a jak se přeskočení počítá (F-PRESKOK-03 až 05)."""
        async def go():
            async with tw.Rig() as rig:
                await rig.background()
                srv = web.WebServer(tw.WebApp(rig))
                p = rig.wq.submit("pusť Kabát", "Petr")
                await rig.until(lambda: p.state == "playing")
                await rig.settle(0.2)
                await skipped(rig, srv, {"client": PETR, "who": "Petr"})  # autor: jen další z mých
                self.assertEqual((p.skips_others, p.skipped_by), (0, "Petr"))
                await asyncio.sleep(0.05)
                await skipped(rig, srv, {"client": JANA, "who": "Jana"})  # cizí: počítá se jako dřív
                self.assertEqual((p.skips_others, p.skipped_by), (1, "Jana"))
                self.assertEqual([(n["who"], n["n"]) for n in rig.wq.notices], [("Petr", 1), ("Jana", 1)])

        run(go())


def next_notice(notices: list[dict], seen: dict, tag: str) -> tuple[dict | None, dict]:
    """Spustí nextNotice z index.html v node: (hláška k ukázání, co už stránka viděla)."""
    src = INDEX.read_text()
    fn = re.search(r"\n  function nextNotice\(.*?\n  \}\n", src, re.S).group(0)
    script = "%s\nvar seen = %s;\nvar n = nextNotice(%s, seen, %s);\nprocess.stdout.write(JSON.stringify([n, seen]));" % (
        fn, json.dumps(seen), json.dumps(notices, ensure_ascii=False), json.dumps(tag))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True, timeout=20).stdout
    n, seen = json.loads(out)
    return n, seen


class Page(unittest.TestCase):
    def test_page_uses_the_one_toast_and_labels_history(self) -> None:
        html = INDEX.read_text()
        self.assertIn("showNotices(s.notices);", html)
        show = re.search(r"\n  function showNotices\(list\) \{.*?\n  \}\n", html, re.S).group(0)
        self.assertIn("toast(n.text)", show)  # stávající hláška dole, žádný druhý systém
        self.assertEqual(html.count('id="toast"'), 1)
        self.assertIn("""(t.skipped_by ? '<span class="a skby">přeskočeno · ' + esc(t.skipped_by) + "</span>" : "")""", html)

    @unittest.skipUnless(shutil.which("node"), "node není")
    def test_each_notice_once_and_not_to_the_person_who_skipped(self) -> None:
        def n(i, who, key, count=1):
            return {"id": i, "kind": "skip", "who": who, "who_key": key, "n": count,
                    "text": f"{who} · přeskočeno: X"}

        first, seen = next_notice([n(1, "Karel", "tk")], {}, "tj")
        self.assertEqual(first["text"], "Karel · přeskočeno: X")
        again, seen = next_notice([n(1, "Karel", "tk")], seen, "tj")  # týž stav znovu (další tik, reconnect)
        self.assertIsNone(again)
        merged, seen = next_notice([n(2, "Karel", "tk", 3)], seen, "tj")  # sloučená má nové id
        self.assertEqual(merged["n"], 3)
        own, seen = next_notice([n(3, "Jana", "tj")], seen, "tj")  # vlastní přeskočení se neukazuje
        self.assertIsNone(own)
        self.assertEqual(sorted(seen), ["1", "2", "3"])
        # stránka otevřená chvíli po přeskočení: ukáže tu nejnovější, ne tři za sebou
        late, _ = next_notice([n(4, "Petr", "tp"), n(5, "Karel", "tk")], {}, "tj")
        self.assertEqual(late["id"], 5)
        # bez přezdívky (prázdná značka) se nic neschová omylem
        anon, _ = next_notice([n(6, "displej", "")], {}, "")
        self.assertEqual(anon["id"], 6)
        nothing, _ = next_notice([], {}, "tj")
        self.assertIsNone(nothing)


if __name__ == "__main__":
    unittest.main()
