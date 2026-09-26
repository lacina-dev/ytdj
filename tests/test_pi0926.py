"""Pi 26. 9. 9:00–10:10 — Robert z telefonu, přání "Parni Valjak" z displeje.

Každý test přehrává skutečnou posloupnost z provozního logu (request.*,
dj.*) proti opravdové frontě přání a falešnému mpv (Rig z test_wishes):

  1. přání interpreta z displeje drželo kancelář 52 minut (kola 11–19),
     Robertova přeskočení jen pouštěla další skladbu téhož přání;
  2. podkres se po Robertově přání vrátil k Parni Valjak (starý režim
     interpreta), restart v 9:52 ho vzkřísil znovu;
  3. "A co třeba Depeche mode" nahradilo až "Zahraj … Budulínek" a "chci
     oboje" dopadlo jako jedna (právě hrající) skladba;
  4. "nastavím střídání…" a ve frontě dvě skladby;
  5. "Proč to vůbec nehraje moje písničky?" šlo jako přání k modelu;
  6. studený Codex a "DJ se nedovolá ke svému mozku (síť)".

    .venv/bin/python -m unittest tests.test_pi0926 -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="ytdj-pi0926-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)
os.environ.setdefault("YTDJ_EVENTS_FILE", str(Path(_TMP) / "events.jsonl"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_wishes as tw  # noqa: E402
from test_wishes import SCRIPT, Rig, W, decision, fair_order, run, whos  # noqa: E402
from ytdj import wishes  # noqa: E402
from ytdj.agent.intent import build_intent, corrects, meta_kind, near_same  # noqa: E402
from ytdj.music.catalog import Track  # noqa: E402
from ytdj.wishes import BUDGET, PANEL_BUDGET, Turns  # noqa: E402

PANEL = {"id": "panel-0a1b2c"}  # relace displeje
ROBERT = "client-robert"  # Rig: id klienta podle jména


def trk(i: str, title: str, artist: str) -> Track:
    return Track(i.rjust(11, "x")[:11], title, artist, duration=200)


BUDULINEK = trk("budulinek", "Budulínek vs. Galantní Jelen", "Vojtaano")
VOJTAANO = [BUDULINEK] + [trk(f"voj{i}", f"Vojtaano {i}", "Vojtaano") for i in range(8)]
DEPECHE = [trk("dm-enjoy", "Enjoy the Silence", "Depeche Mode"),
           trk("dm-jesus", "Personal Jesus", "Depeche Mode")] + \
    [trk(f"dm{i}", f"Depeche {i}", "Depeche Mode") for i in range(8)]
PARNI = [trk(f"pv{i}", f"Parni {i}", "Parni Valjak") for i in range(20)]


class PiCatalog(tw.Catalog):
    """Katalog z test_wishes + interpreti z 26. 9."""

    async def artist_tracks(self, name: str, limit: int = 50):
        extra = {"vojtaano": VOJTAANO, "vojtano": VOJTAANO, "depeche mode": DEPECHE,
                 "parni valjak": PARNI}
        if name.lower() in extra:
            return list(extra[name.lower()])[:limit]
        return await super().artist_tracks(name, limit)

    async def search_song(self, artist: str, title: str):
        a = artist.lower()
        for t in VOJTAANO + DEPECHE:
            if a[:5] in t.artist.lower() and title.lower()[:5] in t.title.lower():
                return t
        return await super().search_song(artist, title)


class PiRig(Rig):
    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.catalog = PiCatalog()
        self.pools.catalog = self.catalog
        self.dj.catalog = self.catalog

    async def fast_plan(self, text: str):
        if text in ("pusť Parni Valjak", "Parni Valjak"):
            from ytdj.agent.codex import Plan
            from ytdj.agent.intent import Intent

            return Plan(intent=Intent(kind="artist", text=text, artists=["Parni Valjak"],
                                      mood="Parni Valjak"), artist_tracks=list(PARNI))
        return await super().fast_plan(text)

    def panel(self, text: str):
        return wishes.WishQueue.submit(self.wq, text, "", "panel", client=dict(PANEL))

    async def play_out(self, n: int, pause: float = 0.25) -> list[str]:
        """Nechá dohrát n skladeb; vrátí, co hrálo (videoId)."""
        out = []
        for _ in range(n):
            out.append(self.fake.current_vid())
            self.fake.finish_current()
            await self.settle(pause)
        return out


# --------------------------------------------------------------------------
# 1. přání interpreta z displeje nesmí držet kancelář
# --------------------------------------------------------------------------


class ArtistWishBudget(unittest.TestCase):
    def test_fair_order_budget_puts_robert_before_the_long_wish(self):
        """Displej: interpret, 12 skladeb, 3 už zazněly; Robert: 2 skladby."""
        disp = W("D", 12, 1, kind="artist")
        disp.source = "panel"
        disp.played = PANEL_BUDGET
        rob = W("R", 2, 2)
        order = fair_order([disp, rob], Turns())
        self.assertEqual(whos(order)[:3], "RRD")
        # dokud rozpočet má, střídá se normálně po 2
        disp.played = 0
        self.assertEqual(whos(fair_order([disp, rob], Turns()))[:5], "DDRRD")
        # nikdo jiný nečeká → přání pokračuje i přes rozpočet
        disp.played = 10
        self.assertEqual(whos(fair_order([disp], Turns()))[:2], "DD")

    def test_web_budget_is_larger_than_panel(self):
        self.assertGreater(BUDGET, PANEL_BUDGET)
        web = W("P", 12, 1, kind="artist")
        web.played = PANEL_BUDGET  # 3 zazněly — z webu má ještě jednu
        jana = W("J", 2, 2)
        self.assertEqual(whos(fair_order([web, jana], Turns()))[:4], "PJJP")

    def test_display_is_one_seat_across_sessions(self):
        """Každé zavření přání na displeji = nová relace; v kole je to ale jeden
        člověk — jinak by displej byl pořád "nováček" a šel první."""
        d1 = W("D", 3, 1)
        d1.source, d1.cid = "panel", "panel-111111"
        d2 = W("E", 3, 3)
        d2.source, d2.cid = "panel", "panel-222222"
        rob = W("R", 3, 2)
        turns = Turns()
        turns.start(d1)
        turns.block = None  # displej hrál naposledy
        # nová relace displeje se nepředbíhá před Roberta
        self.assertEqual(whos(fair_order([d1, d2, rob], turns))[0], "R")

    def test_2x_skip_by_others_ends_the_display_wish(self):
        """9:41–9:47: Robert přeskočil Parni Valjak 5× a pokaždé naskočila další
        skladba přání displeje."""
        async def go():
            async with PiRig() as rig:
                await rig.background()
                d = rig.panel("pusť Parni Valjak")
                await rig.until(lambda: d.state == "playing")
                first = rig.fake.current_vid()
                await rig.wq.skip_current("Robert", ROBERT)
                await rig.settle(0.2)
                self.assertTrue(d.active)  # jedno cizí přeskočení: jen konec kola
                self.assertEqual(d.skips_others, 1)
                self.assertNotEqual(rig.fake.current_vid(), first)
                await rig.wq.skip_current("Robert", ROBERT)
                await rig.settle(0.3)
                self.assertEqual(d.state, "skipped")
                self.assertIn("Přeskočeno ostatními", d.reply)
                # nic z toho přání už nečeká ve frontě ani nehraje jako přání
                self.assertNotIn("displej", rig.owners())
                self.assertFalse(rig.wq.is_request_track(rig.fake.current_vid()))
                # a jeho podkres (režim interpreta) šel s ním
                await rig.until(lambda: rig.pools.artist != "Parni Valjak")
                ev = [f for k, f in rig.events if k == "request.skipped"]
                self.assertFalse(ev[0]["by_owner"])

        run(go())

    def test_owner_skip_is_just_next_of_mine(self):
        async def go():
            async with PiRig() as rig:
                await rig.background()
                d = rig.panel("pusť Parni Valjak")
                await rig.until(lambda: d.state == "playing")
                for _ in range(3):  # u displeje přeskakuje ten, kdo si ho přál
                    await rig.wq.skip_current("displej", None)
                    await rig.settle(0.2)
                self.assertEqual(d.skips_others, 0)
                self.assertNotIn("ostatními", d.reply)
                ev = [f for k, f in rig.events if k == "request.skipped"]
                self.assertTrue(ev and all(f["by_owner"] for f in ev))

        run(go())

    def test_skip_by_other_ends_the_turn_at_once(self):
        """Cizí přeskočení: další skladba je Robertova, ne druhá z kola displeje."""
        async def go():
            async with PiRig() as rig:
                await rig.background()
                r0 = rig.wq.submit("Dancing Queen", "Karel")  # víc lidí jako v 9:00
                await rig.until(lambda: r0.state in ("queued", "playing"))
                d = rig.panel("pusť Parni Valjak")
                await rig.until(lambda: d.state in ("queued", "playing"))
                r = rig.wq.submit("Holky z naší školky", "Robert")
                await rig.until(lambda: r.state == "queued")
                # dohrát, až hraje displej
                for _ in range(6):
                    if rig.wq.reason_for(rig.fake.current_vid()).get("id") == d.id:
                        break
                    rig.fake.finish_current()
                    await rig.settle(0.25)
                self.assertEqual(rig.wq.reason_for(rig.fake.current_vid()).get("id"), d.id)
                await rig.wq.skip_current("Robert", ROBERT)
                await rig.settle(0.3)
                self.assertEqual(rig.fake.current_vid(), r.tracks[0].id)

        run(go())

    def test_web_control_next_attributes_the_skip(self):
        """Tlačítko Další z webu nese id klienta — pravidla kola ho potřebují."""
        async def go():
            async with PiRig() as rig:
                await rig.background()
                srv = tw.web.WebServer(tw.WebApp(rig))
                d = rig.panel("pusť Parni Valjak")
                await rig.until(lambda: d.state == "playing")
                resp = await srv._control(tw.req({"action": "next", "client": ROBERT,
                                                  "who": "Robert"}))
                self.assertEqual(resp.status_code, 200)
                await rig.settle(0.2)
                self.assertEqual(d.skips_others, 1)
                self.assertEqual(d.skipped_by, "Robert")

        run(go())


# --------------------------------------------------------------------------
# 2. podkres jde za naposledy splněným přáním
# --------------------------------------------------------------------------


class BackgroundFollowsLatest(unittest.TestCase):
    def test_after_roberts_wish_background_is_not_old_artist(self):
        """9:29 předání Parni Valjak do podkresu; 9:56 Robertovo přání dohrálo
        a podkres jel dál Parni Valjak."""
        async def go():
            async with PiRig() as rig:
                await rig.background()
                d = rig.panel("pusť Parni Valjak")
                await rig.until(lambda: d.handed and rig.pools.artist == "Parni Valjak")
                for _ in range(6):  # přání displeje dohraje, podkres Parni Valjak
                    if not d.active:
                        break
                    rig.fake.finish_current()
                    await rig.settle(0.2)
                self.assertFalse(d.active)
                self.assertEqual(rig.pools.artist, "Parni Valjak")
                r = rig.wq.submit("Holky z naší školky", "Robert")
                await rig.until(lambda: r.state == "playing")
                rig.fake.finish_current()
                await rig.until(lambda: r.state == "done")
                await rig.until(lambda: rig.pools.artist == "")
                self.assertIn("Olympic", rig.pools.mood)  # Robertova skladba a podobné
                await rig.until(lambda: any(k == "request.background_follow" for k, _ in rig.events))
                self.assertEqual(rig.wq.bg_reason.get("id"), r.id)
                follow = [f for k, f in rig.events if k == "request.background_follow"]
                self.assertEqual((follow[-1]["why"], follow[-1]["id"]), ("fulfilled", r.id))

        run(go())

    def test_artist_background_is_bounded(self):
        async def go():
            async with PiRig() as rig:
                pools = rig.pools
                await pools.set_artist("Parni Valjak", list(PARNI), max_tracks=3,
                                       until=time.time() + 3600)
                got = await pools.next_tracks(2) + await pools.next_tracks(2)
                self.assertEqual([t.artist for t in got], ["Parni Valjak"] * 3)
                await pools.next_tracks(2)
                self.assertEqual(pools.artist, "")
                self.assertEqual(pools.mood, "Parni Valjak a podobné")
                # i čas
                await pools.set_artist("Parni Valjak", list(PARNI), max_tracks=50,
                                       until=time.time() - 1)
                await pools.next_tracks(1)
                self.assertEqual(pools.artist, "")

        run(go())

    def test_restart_does_not_resurrect_stale_artist_mode(self):
        """9:52 restart: request.resume → radio.artist_mode Parni valjak."""
        wall = time.time()
        tracks = [wishes._track_json(t) for t in PARNI[:10]]
        base = {"v": 1, "saved": wall - 60, "boot": "", "playing": True, "wishes": [],
                "turns": {}}
        stale = {**base, "bg": {"mode": "artist", "artist": "Parni valjak", "mood": "Parni valjak",
                                "tracks": tracks,
                                "reason": {"kind": "radio", "who": "displej", "id": "fbcaa1ff"}}}
        expired = {**stale, "bg": {**stale["bg"], "left": 4, "until": wall - 5}}
        fresh = {**stale, "bg": {**stale["bg"], "left": 4, "until": wall + 600}}

        async def go(saved):
            async with PiRig() as rig:
                await rig.wq.resume(saved, now=datetime(2026, 9, 26, 9, 52), wall=wall)
                return rig.pools.artist, rig.pools.mood, rig.pools.artist_left

        self.assertEqual(run(go(stale))[:2], ("", "Parni valjak a podobné"))  # starý stav bez omezení
        self.assertEqual(run(go(expired))[0], "")
        artist, mood, left = run(go(fresh))  # čerstvý: jen se zbytkem svého omezení
        self.assertEqual((artist, mood), ("Parni valjak", "Parni valjak"))
        self.assertIsNotNone(left)
        self.assertLessEqual(left, 4)


# --------------------------------------------------------------------------
# 3. přání téhož člověka se nenahrazují; "oboje" = obojí
# --------------------------------------------------------------------------

DM_0935 = decision(action="start_radio", focus_artists=["Depeche Mode"], mood="Depeche Mode",
                   seeds=[{"artist": "Depeche Mode", "title": "Enjoy the Silence"}])
VOJ_0936 = decision(action="start_radio", focus_artists=["Vojtano"], mood="Vojtano",
                    reply="Pustím Vojtaana.")
# 9:41 rekonstrukce z logu (surové dj.decision v podkladech není): intent "song",
# n_requested 1 (Budulínek — přání skončilo s ním), Depeche Mode v seznamu
# seedů; odpověď doslova z request.done
BOTH_0941 = decision(action="start_radio", mood="Budulínek a Depeche Mode",
                     requested=[{"artist": "Vojtaano", "title": "Budulínek"}],
                     seeds=[{"artist": "Depeche Mode", "title": "Enjoy the Silence"},
                            {"artist": "Depeche Mode", "title": "Personal Jesus"}],
                     reply="Jasně — pustím Budulínka a potom navážu Depeche Mode.")
T_0935 = "A co třeba Depeche mode"
T_0936 = "Zahraj od Vojtano píseň Budulínek nebo galantní jelen nebo jak se to jmenuje"
T_0941 = "Ale já chci budulinka a Depeche mode taky chci oboje"


class SamePerson(unittest.TestCase):
    def setUp(self):
        SCRIPT[T_0935] = ("codex", DM_0935)
        SCRIPT[T_0936] = ("codex", VOJ_0936)
        SCRIPT[T_0941] = ("codex", BOTH_0941)

    def test_rules(self):
        self.assertFalse(wishes.replaces(T_0936))
        self.assertTrue(corrects("ne, radši Olympic"))
        self.assertTrue(corrects("místo toho Kabát"))
        self.assertTrue(corrects("zruš to"))
        self.assertTrue(wishes.replaces("něco jiného"))
        self.assertTrue(near_same("pusť Kabát", "pust kabat!"))
        intent = build_intent(T_0941, BOTH_0941)
        self.assertEqual((intent.kind, intent.artists), ("artist", ["Depeche Mode"]))
        self.assertEqual(intent.tracks, [("Vojtaano", "Budulínek")])

    def test_second_wish_goes_first_and_both_stay(self):
        async def go():
            async with PiRig() as rig:
                await rig.background()
                k = rig.panel("pusť Parni Valjak")  # hraje displej (jako v 9:34)
                await rig.until(lambda: k.state == "playing")
                a = rig.wq.submit(T_0935, "Robert")
                await rig.until(lambda: a.state == "queued")
                b = rig.wq.submit(T_0936, "Robert")
                await rig.until(lambda: b.state == "queued")
                await rig.settle(0.2)
                self.assertEqual(a.state, "queued")  # Depeche Mode nezmizelo
                self.assertFalse([e for e in rig.events if e[0] == "request.replaced"])
                robert = [v for v, o in zip(rig.upcoming(), rig.owners()) if o == "Robert"]
                self.assertEqual(robert[0], VOJTAANO[0].id)  # nové přání první
                self.assertLess(b.rank, a.rank)
                # starší zůstává za ním: celé pořadí (i za horizontem playlistu)
                full = fair_order(rig.wq.wishes, rig.wq.turns, limit=60)
                mine = [w.id for w, _ in full if w.key == b.key]
                self.assertIn(a.id, mine)
                self.assertLess(mine.index(b.id), mine.index(a.id))

        run(go())

    def test_both_while_budulinek_plays_queues_depeche_mode(self):
        """9:41: jediná vyžádaná skladba byla ta hrající → přání "hotovo",
        Depeche Mode nikdy nezaznělo."""
        async def go():
            async with PiRig() as rig:
                await rig.background()
                b = rig.wq.submit(T_0936, "Robert")
                await rig.until(lambda: b.state == "playing"
                                and rig.fake.current_vid() == BUDULINEK.id)
                c = rig.wq.submit(T_0941, "Robert")
                await rig.until(lambda: c.state in ("queued", "playing", "done"))
                await rig.settle(0.2)
                self.assertEqual((c.state, c.kind), ("queued", "artist"))
                self.assertNotIn(BUDULINEK.id, [t.id for t in c.tracks])
                self.assertTrue(all(t.artist == "Depeche Mode" for t in c.pending()))
                self.assertIn("právě hraje", c.reply)
                self.assertIn("Enjoy the Silence (Depeche Mode)", c.reply)
                self.assertNotIn("navážu", c.reply)  # model to nesliboval za nás
                rig.fake.finish_current()  # Budulínek dohraje
                await rig.settle(0.3)
                self.assertEqual(rig.fake.current_vid(), DEPECHE[0].id)
                self.assertTrue(c.active)

        run(go())


# --------------------------------------------------------------------------
# 4. odpověď = co se opravdu zařadilo; střídání dvou interpretů
# --------------------------------------------------------------------------

ALT_0957 = decision(  # 9:57: intent "song", 2 vyžádané, slib "nastavím střídání" (odpověď doslova)
    action="start_radio",
    requested=[{"artist": "Vojtaano", "title": "Budulínek vs. Galantní Jelen"},
               {"artist": "Depeche Mode", "title": "Enjoy the Silence"}],
    reply="Máš pravdu — teď nastavím střídání Vojtaana s Depeche Mode, "
          "aby se oba pravidelně vraceli.")


class HonestReplies(unittest.TestCase):
    def test_alternation_is_a_real_two_artist_wish(self):
        async def go():
            async with PiRig() as rig:
                await rig.background()
                SCRIPT["střídej Vojtaana a Depeche Mode"] = ("codex", ALT_0957)
                w = rig.wq.submit("střídej Vojtaana a Depeche Mode", "Robert")
                await rig.until(lambda: w.state in ("queued", "playing") and bool(w.reply))
                self.assertEqual((w.kind, w.artists), ("artist", ["Vojtaano", "Depeche Mode"]))
                artists = [t.artist for t in w.tracks[:4]]
                self.assertEqual(artists, ["Vojtaano", "Depeche Mode"] * 2)
                self.assertIn("střídavě Vojtaano a Depeche Mode", w.reply)
                # Robert je sám → střídání pokračuje v podkresu (omezeně), oba interpreti
                await rig.until(lambda: rig.pools.artist == "Vojtaano, Depeche Mode")
                nxt = [t.artist for t in await rig.pools.next_tracks(4)]
                self.assertEqual(set(nxt), {"Vojtaano", "Depeche Mode"})
                self.assertNotIn("nastavím", w.reply)
                # odpověď vyjmenuje přesně to, co je na řadě
                for t in w.tracks[1:3]:  # (první už možná hraje)
                    self.assertIn(t.title, w.reply)

        run(go())

    def test_nothing_reply_cannot_promise_a_mode(self):
        async def go():
            async with PiRig() as rig:
                await rig.background()
                SCRIPT["co umíš?"] = ("codex", decision(
                    action="nothing", reply="Jasně, nastavím střídání a budu pořád hrát oboje."))
                w = rig.wq.submit("co umíš?", "Robert")
                await rig.until(lambda: w.state == "done")
                self.assertNotIn("nastavím", w.reply)
                self.assertIn("Nic jsem nezařadil", w.reply)

        run(go())


# --------------------------------------------------------------------------
# 5. otázka / stížnost na frontu není přání
# --------------------------------------------------------------------------


class MetaQuestions(unittest.TestCase):
    def test_detection(self):
        for t in ("Proč to vůbec nehraje moje písničky?",
                  "Jako ok budulínek, ale pak je zas pořád jen displej. To není demokracie. "
                  "Displej by neměl mít dlouhodobou přednost. Musí se to střídat.",
                  "Žádné střídání jsi nenastavil", "kdy bude moje písnička?", "co to hraje?"):
            self.assertIsNotNone(meta_kind(t), t)
        for t in ("pusť Kabát", "proč nehraje Kabát, pusť Kabát", T_0935, T_0941,
                  "furt jen Kabát prosím", "něco klidnějšího"):
            self.assertIsNone(meta_kind(t), t)

    def test_why_not_my_songs_answers_from_state(self):
        """9:47: Robert nemá nic ve frontě (jeho přání se "splnilo" v 9:41),
        hraje přání displeje."""
        async def go():
            async with PiRig() as rig:
                await rig.background()
                d = rig.panel("pusť Parni Valjak")
                await rig.until(lambda: d.state == "playing")
                before = list(rig.upcoming())
                asked = len(rig.asked)
                q = rig.wq.submit("Proč to vůbec nehraje moje písničky?", "Robert")
                await rig.until(lambda: q.state == "done")
                self.assertEqual(q.kind, "meta")
                self.assertEqual(q.tracks, [])
                self.assertEqual(len(rig.asked), asked)  # žádný tah modelu
                self.assertIn("Teď hraje Parni", q.reply)
                self.assertIn("přání displej", q.reply)
                self.assertIn("nic nemáš", q.reply)
                self.assertIn("Pravidla", q.reply)
                await rig.settle(0.1)
                self.assertEqual(rig.upcoming(), before)  # žádná hudba navíc
                ev = [f for k, f in rig.events if k == "request.meta"]
                self.assertEqual((ev[0]["meta"], ev[0]["who"]), ("complaint", "Robert"))

        run(go())

    def test_starved_listener_gets_the_next_turn(self):
        async def go():
            async with PiRig() as rig:
                await rig.background()
                rig.wq.submit("Dancing Queen", "Karel")
                d = rig.panel("pusť Parni Valjak")
                await rig.until(lambda: d.state in ("queued", "playing"))
                r = rig.wq.submit("Holky z naší školky", "Robert")
                await rig.until(lambda: r.state == "queued")
                await rig.settle(0.2)
                self.assertNotEqual(rig.upcoming()[0], r.tracks[0].id)
                with mock.patch.object(wishes, "STARVED", 0.0):
                    q = rig.wq.submit("kdy bude moje písnička?", "Robert")
                    await rig.until(lambda: q.state == "done")
                await rig.settle(0.2)
                self.assertTrue(r.play_next)
                self.assertEqual(rig.upcoming()[0], r.tracks[0].id)
                self.assertIn("hned po téhle", q.reply)

        run(go())

    def test_no_alternation_complaint_is_answered_truthfully(self):
        async def go():
            async with PiRig() as rig:
                await rig.background()
                q = rig.wq.submit("Žádné střídání jsi nenastavil", "Robert")
                await rig.until(lambda: q.state == "done")
                self.assertEqual(q.kind, "meta")
                self.assertIn("střídej", q.reply)

        run(go())


# --------------------------------------------------------------------------
# 6. rychlá cesta pro "od Vojtano píseň Budulínek"
# --------------------------------------------------------------------------


class MisspelledArtistSong(unittest.TestCase):
    def test_fast_path_finds_budulinek(self):
        import test_dj_fastpath as fp

        from ytdj.agent.fastpath import find_song, parse_song

        self.assertEqual(parse_song(T_0936).pairs,
                         [("Vojtano", "Budulínek"), ("Vojtano", "galantní jelen")])
        with mock.patch.dict(fp.CATALOG, {"Vojtaano": [BUDULINEK]}), \
                mock.patch.object(fp, "SONGBOOK", fp.SONGBOOK + [BUDULINEK]):
            res = run(find_song(fp.FuzzyCatalog(), T_0936))
        self.assertEqual((res.track and res.track.id, res.reason), (BUDULINEK.id, ""))


if __name__ == "__main__":
    unittest.main()
