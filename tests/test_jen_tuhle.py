"""„Jen tuhle" (F-FRONTA-27) a čip „Bizár" (F-PRANI-36).

Hlášení z jukeboxu #41 (Radek, 10. 11. 2026): „Přidat možnost přehrát pouze
jednu písničku bez narušení rádia / co zrovna hraje." Hlášení #42 (Michael,
12. 11.): „Chybí mi tlačítko ‚pusť tam bizár'". Vlastník 13. 11.: „ano".

    YTDJ_EVENTS_FILE=/tmp/x .venv/bin/python -m unittest tests.test_jen_tuhle -v
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_wishes as tw  # noqa: E402
from test_wishes import KABAT, SCRIPT, Rig, WebApp, body, decision, req, run, web  # noqa: E402
from ytdj.agent.offline import BIZARRE, BIZARRE_OFFLINE, CHIPS, CodexOffline  # noqa: E402
from ytdj.wishes import Wish  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
BIZ_TEXT = "pusť jednu bizarní písničku"  # text čipu na webu začíná takhle


def bizarre_text() -> str:
    src = (ROOT / "ytdj" / "web" / "static" / "index.html").read_text(encoding="utf-8")
    i = src.index('data-chip="bizarre"')
    return src[i:].split('data-wish="', 1)[1].split('"', 1)[0]


def setup_script() -> None:
    SCRIPT[bizarre_text()] = ("codex", decision(
        action="play_next", requested=[{"artist": "Ivan Mládek", "title": "Jozin"}]))
    SCRIPT["něco od Olympicu"] = ("codex", decision(
        action="start_radio", focus_artists=["Olympic"]))
    SCRIPT["Holky a Jasná zpráva"] = ("codex", decision(
        action="play_next", requested=[{"artist": "Olympic", "title": "Holky"},
                                       {"artist": "Olympic", "title": "Jasna"}]))


class OnlyThis(unittest.TestCase):
    def setUp(self) -> None:
        setup_script()

    @staticmethod
    def bg_upcoming(rig: Rig) -> list[str]:
        """„Hraje dál" bez skladeb přání — pořadí podkresu."""
        return [v for v in rig.upcoming() if v not in rig.wq.owner]

    @staticmethod
    def bg_state(rig: Rig) -> tuple:
        pools = rig.pools
        reason = {k: v for k, v in rig.wq.bg_reason.items()}
        return (pools.mood, [p.seed.id for p in pools.pools], pools.artist,
                getattr(pools, "album", ""), reason, list(rig.wq.mix), list(rig.dj.avoid))

    def test_song_plays_once_then_background_exactly_as_before(self):
        """Jedna skladba hned po hrající; podkres pak pokračuje stejnou řadou,
        stejný režim, semínka i důvod — žádné „Olympic a podobné"."""
        async def go():
            async with Rig() as rig:
                await rig.background()
                cur = rig.fake.current_vid()
                before = self.bg_state(rig)
                bg_before = self.bg_upcoming(rig)
                last_wish = rig.dj.wish
                w = rig.wq.submit("Holky z naší školky", "Radek", play_next=True,
                                  only_this=True)
                await rig.until(lambda: w.state == "queued")
                await rig.settle(0.2)
                self.assertTrue(w.only_this)
                self.assertEqual(len(w.tracks), 1)
                self.assertEqual(rig.fake.current_vid(), cur)  # „Hned po téhle" nic neutne
                self.assertEqual(rig.upcoming()[0], w.tracks[0].id)  # hned po hrající
                self.assertEqual(self.bg_upcoming(rig)[:len(bg_before)], bg_before)
                self.assertEqual(self.bg_state(rig), before)
                self.assertIn("Jen tahle: „song s-Holky“ (Olympic).", w.reply)
                self.assertIn("Potom rádio hraje dál, jak hrálo.", w.reply)
                self.assertNotIn("podobná", w.reply)
                rig.fake.finish_current()  # skladba rádia dohraje → přání
                await rig.until(lambda: w.state == "playing")
                rig.fake.finish_current()  # přání dohraje
                await rig.until(lambda: w.state == "done")
                await rig.settle(0.4)  # podkres po přání (_background_after) by se přeladil teď
                self.assertEqual(self.bg_state(rig), before)
                self.assertNotEqual(rig.pools.mood, "Olympic a podobné")
                # další skladba je ta, která byla v rotaci na řadě před přáním
                self.assertEqual(rig.fake.current_vid(), bg_before[0])
                self.assertEqual(self.bg_upcoming(rig)[:len(bg_before) - 1], bg_before[1:])
                self.assertIs(rig.dj.wish, last_wish)  # automatické tahy se drží starého směru
                kinds = rig.kinds()
                self.assertNotIn("request.background", kinds)
                self.assertNotIn("request.background_follow", kinds)
                created = [f for k, f in rig.events if k == "request.created"][-1]
                self.assertIs(created.get("only_this"), True)
                queued = [f for k, f in rig.events if k == "request.queued"][-1]
                self.assertIs(queued.get("only_this"), True)

        run(go())

    def test_without_the_box_the_song_still_steers(self):
        """Bez zaškrtnutí beze změny (F-PRANI-04, F-FRONTA-13)."""
        async def go():
            async with Rig() as rig:
                await rig.background()
                w = rig.wq.submit("Holky z naší školky", "Radek")
                await rig.until(lambda: w.state == "playing")
                rig.fake.finish_current()
                await rig.until(lambda: w.state == "done")
                await rig.until(lambda: rig.wq.bg_reason.get("id") == w.id)
                self.assertEqual(rig.pools.mood, "Olympic a podobné")
                self.assertFalse(w.only_this)

        run(go())

    def test_artist_wish_is_one_song_and_reply_says_which(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                before = self.bg_state(rig)
                w = rig.wq.submit("pusť Kabát", "Radek", only_this=True)
                await rig.until(lambda: w.state in ("queued", "playing"))
                await rig.settle(0.3)
                self.assertEqual([t.id for t in w.tracks], [KABAT[0].id])  # nejznámější
                self.assertEqual(w.rest, [])
                self.assertFalse(w.handed)
                self.assertEqual(rig.pools.artist, "")  # žádný režim interpreta
                self.assertEqual(self.bg_state(rig), before)
                self.assertTrue(w.reply.startswith("Jen jedna — od interpreta Kabát: „song kab0“."),
                                w.reply)
                owned = [v for v in rig.upcoming() if rig.wq.owner.get(v) == w.id]
                self.assertLessEqual(len(owned), 1)
                if w.state == "queued":
                    rig.fake.finish_current()
                    await rig.until(lambda: w.state == "playing")
                rig.fake.finish_current()
                await rig.until(lambda: w.state == "done")
                await rig.settle(0.4)
                self.assertEqual(self.bg_state(rig), before)
                self.assertEqual(w.played, 1)

        run(go())

    def test_mood_wish_is_the_djs_single_pick(self):
        """Nálada bez konkrétní skladby: jedna, kterou DJ vybral (první z jeho
        výběru), a nálada podkresu se nemění."""
        async def go():
            async with Rig() as rig:
                await rig.background()
                before = self.bg_state(rig)
                w = rig.wq.submit("něco klidnějšího", "Radek", only_this=True)
                await rig.until(lambda: w.state in ("queued", "playing"))
                await rig.settle(0.2)
                self.assertEqual(len(w.tracks), 1)
                self.assertEqual(w.tracks[0].artist, "Calm")
                self.assertEqual(self.bg_state(rig), before)
                self.assertEqual(rig.pools.mood, "podkres")
                self.assertIn("Jen jedna v náladě „klidný pop“: „song s-c0“ (Calm).", w.reply)
                # model dostal pokyn „Jen tuhle"
                self.assertTrue(any(tw.wishes.ONLY_HINT in t for t, _ in rig.asked))

        run(go())

    def test_two_named_songs_play_just_the_first(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                w = rig.wq.submit("Holky a Jasná zpráva", "Radek", only_this=True)
                await rig.until(lambda: w.state in ("queued", "playing"))
                self.assertEqual([t.title for t in w.tracks], ["song s-Holky"])

        run(go())

    def test_enrich_and_only_this_exclude_each_other(self):
        """Server/fronta: když přijde obojí, platí „Jen tuhle" (mix nevznikne)."""
        async def go():
            async with Rig() as rig:
                await rig.background()
                w = rig.wq.submit("Holky z naší školky", "Radek", only_this=True, enrich=True)
                await rig.until(lambda: w.state in ("queued", "playing"))
                await rig.settle(0.2)
                self.assertTrue(w.only_this)
                self.assertFalse(w.enrich)
                self.assertEqual(rig.wq.mix, [])
                self.assertEqual(rig.pools.mood, "podkres")

        run(go())

    def test_others_waiting_fairness_unchanged(self):
        """Kola a „hned po téhle" jako u každého přání; po všech podkres jde za
        posledním přáním BEZ zaškrtnutí (F-FRONTA-13)."""
        async def go():
            async with Rig() as rig:
                await rig.background()
                p = rig.wq.submit("pusť Kabát", "Petr")
                await rig.until(lambda: p.state == "playing")
                w = rig.wq.submit("Holky z naší školky", "Radek", only_this=True)
                await rig.until(lambda: w.state == "queued")
                await rig.settle(0.2)
                # Radkova jedna skladba ve střídání po Petrově kole (F-FRONTA-01)
                self.assertIn("Radek", rig.owners())
                self.assertEqual(len(w.tracks), 1)

        run(go())

    def test_flag_survives_restart(self):
        w = Wish(id="a", token="t", who="R", source="web", text="x", created=1.0, mono=1.0,
                 only_this=True, chip=BIZARRE)
        back = Wish.from_json(w.to_json())
        self.assertTrue(back.only_this)
        self.assertEqual(back.chip, BIZARRE)
        w.only_this = False
        self.assertFalse(Wish.from_json(w.to_json()).only_this)

    def test_queued_wish_restored_after_restart_keeps_background(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                w = rig.wq.submit("Holky z naší školky", "Radek", play_next=True, only_this=True)
                await rig.until(lambda: w.state == "queued")
                await rig.settle(0.2)
                saved = rig.wq.session_state()
            async with Rig() as rig2:
                await rig2.wq.resume(saved, now=datetime(2026, 11, 13, 10, 0))
                await rig2.settle(0.3)
                x = rig2.wq.by_id(w.id)
                self.assertIsNotNone(x)
                self.assertTrue(x.only_this)
                mood = rig2.pools.mood
                self.assertEqual(mood, "podkres")
                for _ in range(3):
                    if x.state == "done":
                        break
                    rig2.fake.finish_current()
                    await rig2.settle(0.3)
                await rig2.until(lambda: x.state == "done")
                await rig2.settle(0.4)
                self.assertEqual(rig2.pools.mood, "podkres")
                self.assertNotEqual(rig2.wq.bg_reason.get("id"), x.id)

        run(go())


class Bizarre(unittest.TestCase):
    def setUp(self) -> None:
        setup_script()

    def test_chip_is_one_song_from_the_model_background_untouched(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                before = OnlyThis.bg_state(rig)
                fast: list[str] = []
                orig = rig.dj.fast_plan

                async def spy(text):
                    fast.append(text)
                    return await orig(text)

                rig.dj.fast_plan = spy
                w = rig.wq.submit(bizarre_text(), "Michael", chip=BIZARRE)
                await rig.until(lambda: w.state in ("queued", "playing"))
                await rig.settle(0.2)
                self.assertTrue(w.only_this)  # Bizár je vždycky jedna skladba
                self.assertEqual(fast, [])  # rychlá cesta ne — vybírá model
                self.assertEqual(len(w.tracks), 1)
                t = w.tracks[0]
                self.assertEqual(t.artist, "Ivan Mládek")
                self.assertTrue(w.reply.startswith(f"Bizár: „{t.title}“ (Ivan Mládek)."), w.reply)
                self.assertEqual(OnlyThis.bg_state(rig), before)
                asked = [t for t, _ in rig.asked if t.startswith(BIZ_TEXT)]
                self.assertEqual(len(asked), 1)
                self.assertIn(tw.wishes.ONLY_HINT, asked[0])

        run(go())

    def test_offline_says_honestly_it_cannot(self):
        """Mozek DJe nejede (limit 3. 11.): žádná náhoda pod cizím jménem —
        poctivá věta a hudba hraje dál beze změny."""
        async def go():
            async with Rig() as rig:
                await rig.background()
                before = OnlyThis.bg_state(rig)
                cur = rig.fake.current_vid()

                async def down(text, auto=False):
                    raise CodexOffline("limit")

                rig.dj.interpret = down
                w = rig.wq.submit(bizarre_text(), "Michael", chip=BIZARRE)
                await rig.until(lambda: w.state == "error")
                self.assertEqual(w.reply, BIZARRE_OFFLINE)
                self.assertEqual(w.tracks, [])
                self.assertEqual(rig.fake.current_vid(), cur)
                self.assertEqual(OnlyThis.bg_state(rig), before)
                self.assertIn("request.bizarre_offline", rig.kinds())

        run(go())

    def test_chip_and_text_in_the_page(self):
        src = (ROOT / "ytdj" / "web" / "static" / "index.html").read_text(encoding="utf-8")
        chips = src[src.index('id="chips"'):]
        chips = chips[:chips.index("</div>")]
        self.assertIn('data-chip="bizarre"', chips)
        self.assertIn(">Bizár</button>", chips)
        self.assertTrue(bizarre_text().startswith(BIZ_TEXT))
        self.assertIn("kancel", bizarre_text())  # do kanceláře
        self.assertIn(BIZARRE, CHIPS)
        self.assertIn(BIZARRE, web.CHIP_KEYS)

    def test_prompt_mentions_bizarre_and_office(self):
        from ytdj.agent.prompts import ROLE
        i = ROLE.index("Bizár")
        part = ROLE[i - 80:i + 600]
        self.assertIn("JEDNA", part)
        self.assertIn("explicit_ok = false", part)
        self.assertIn("kanceláře", part)


class WebBox(unittest.TestCase):
    def setUp(self) -> None:
        setup_script()

    def test_checkboxes_in_the_page(self):
        src = (ROOT / "ytdj" / "web" / "static" / "index.html").read_text(encoding="utf-8")
        nxt, enr, only = (src.index('id="playNext"'), src.index('id="enrich"'),
                          src.index('id="onlyThis"'))
        self.assertLess(nxt, enr)
        self.assertLess(enr, only)
        self.assertLess(only - nxt, 1200)  # vedle sebe
        self.assertIn("Jen tuhle", src[only:only + 80])
        self.assertIn("only_this: !!el.onlyThis.checked", src)
        self.assertIn("el.onlyThis.checked = false", src)  # nepamatuje se
        # vzájemně se vylučují: zaškrtnutí jednoho odškrtne druhé
        self.assertIn("if (el.onlyThis.checked) el.enrich.checked = false;", src)
        self.assertIn("if (el.enrich.checked) el.onlyThis.checked = false;", src)

    def test_server_passes_only_this_and_bizarre_chip(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                srv = web.WebServer(WebApp(rig))
                resp = await srv._prompt(req({"text": "Holky z naší školky", "who": "Radek",
                                              "source": "web", "only_this": True,
                                              "enrich": True, "client": "c-radek"}))
                self.assertEqual(resp.status_code, 202)
                w = rig.wq.by_id(body(resp)["id"])
                self.assertTrue(w.only_this)
                self.assertFalse(w.enrich)
                self.assertTrue(body(resp)["request"].get("only_this"))
                resp = await srv._prompt(req({"text": bizarre_text(), "who": "Michael",
                                              "source": "web", "chip": BIZARRE,
                                              "client": "c-michael"}))
                w2 = rig.wq.by_id(body(resp)["id"])
                self.assertEqual(w2.chip, BIZARRE)
                self.assertTrue(w2.only_this)
                resp = await srv._prompt(req({"text": "Jasná zpráva", "who": "Jana",
                                              "only_this": "yes", "client": "c-jana"}))
                self.assertFalse(rig.wq.by_id(body(resp)["id"]).only_this)  # jen true

        run(go())


if __name__ == "__main__":
    unittest.main()
