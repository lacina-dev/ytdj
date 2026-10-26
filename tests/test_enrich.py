"""„Obohatit" — přání přimíchá do podkresu, nepřepne ho (F-FRONTA-25, F-FRONTA-26).

Hlášení z jukeboxu #34 a #35 (Radek, 19. a 20. 10. 2026): hrálo jen rádio,
„Přidej nějaké skladby skupiny Gojira" / „Archspire - drone corpse aviator"
s „Hned po téhle skladbě" — a celé „Hraje dál" se vyměnilo za Gojiru /
„Archspire a podobné". Vlastník 26. 10. 2026: „Udělej tam checkbox ‚Obohatit'
… rádio namíchané ze dvou nebo více přání."

    YTDJ_EVENTS_FILE=/tmp/x .venv/bin/python -m unittest tests.test_enrich -v
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_wishes as tw  # noqa: E402
from test_wishes import SCRIPT, Rig, T, decision, run  # noqa: E402
from ytdj.wishes import MIX_SOURCES, Wish, _mix_seeds, mix_label  # noqa: E402

GOJIRA = "Přidej nějaké skladby skupiny Kabát"  # Radkův text (#34); Kabát zná atrapa katalogu
ARCHSPIRE = "Archspire - drone corpse aviator"  # #35


def source_of(t) -> str:
    """Z kterého semínka je skladba rádia (atrapa: interpret = '<semínko>~<i>')."""
    return t.artist.split("~")[0]


def seeded_radio(rig: Rig) -> None:
    """Rádio podle semínka — ať jde poznat, ze kterého zdroje mixu skladba je."""
    async def radio(video_id: str, limit: int = 50):
        return [T(f"{video_id[-5:]}{i:02d}", f"{video_id}~{i}") for i in range(40)]

    rig.catalog.radio = radio


def setup_script() -> None:
    SCRIPT[GOJIRA] = ("codex", decision(
        action="start_radio", focus_artists=["Kabát"],
        seeds=[{"artist": "Kabát", "title": "Pohoda"}]))
    SCRIPT[ARCHSPIRE] = ("codex", decision(
        action="play_next", requested=[{"artist": "Archspire", "title": "Aviator"}]))
    SCRIPT["Stranded od Gojiry"] = ("codex", decision(
        action="play_next", requested=[{"artist": "Gojira", "title": "Stranded"}]))
    SCRIPT["Sonne od Rammstein"] = ("codex", decision(
        action="play_next", requested=[{"artist": "Rammstein", "title": "Sonne"}]))


class Mix(unittest.TestCase):
    def setUp(self) -> None:
        setup_script()

    def upcoming_bg(self, rig: Rig) -> list:
        """Skladby podkresu v „Hraje dál" (bez skladeb přání), jako Track."""
        return [rig.pools.known[v] for v in rig.upcoming()
                if v not in rig.wq.owner and v in rig.pools.known]

    def test_34_artist_with_enrich_then_radio_alternates_old_and_new(self):
        async def go():
            async with Rig() as rig:
                seeded_radio(rig)
                await rig.background()
                old_seed = rig.pools.pools[0].seed.id
                w = rig.wq.submit(GOJIRA, "Radek", enrich=True)
                await rig.until(lambda: w.state in ("queued", "playing"))
                await rig.until(lambda: bool(rig.wq.mix))
                await rig.settle(0.3)
                self.assertTrue(w.enrich)
                self.assertEqual(len(w.tracks), rig.wq.amounts.budget_of(w))  # přání jako dnes
                self.assertEqual(w.rest, [])  # zbytek interpreta do podkresu nejde
                self.assertEqual(rig.pools.artist, "")  # žádný režim interpreta
                self.assertEqual(rig.pools.mood, "Mix: podkres + Kabát")
                self.assertEqual(rig.wq.bg_reason.get("mode"), "mix")
                up = rig.upcoming()
                n = len([t for t in w.tracks if t.id in up])
                self.assertEqual(up[:n], [t.id for t in w.tracks if t.id in up])  # přání první
                srcs = [source_of(t) for t in self.upcoming_bg(rig)]
                self.assertGreaterEqual(len(srcs), 4)
                self.assertEqual(set(srcs[:4]), {old_seed, w.tracks[0].id})
                for a, b in zip(srcs, srcs[1:]):
                    self.assertNotEqual(a, b, srcs)  # napřeskáčku, ne po blocích
                # web, displej i telka ukazují podkres podle reason_for (F-FRONTA-19)
                bg_vid = next(v for v in rig.upcoming() if v not in rig.wq.owner)
                why = rig.wq.reason_for(bg_vid)
                self.assertEqual((why.get("text"), why.get("mode")), ("Mix: podkres + Kabát", "mix"))
                created = [f for k, f in rig.events if k == "request.created"][-1]
                self.assertIs(created.get("enrich"), True)
                self.assertTrue(any(k == "request.mix" for k, _ in rig.events))

        run(go())

    def test_35_song_after_this_one_with_enrich(self):
        """Archspire hned po hrající (nic se neutne), pak dál rádio, které hrálo,
        namíchané s rádiem z Archspire."""
        async def go():
            async with Rig() as rig:
                seeded_radio(rig)
                await rig.background()
                cur = rig.fake.current_vid()
                old_seed = rig.pools.pools[0].seed.id
                w = rig.wq.submit(ARCHSPIRE, "Radek", play_next=True, enrich=True)
                await rig.until(lambda: w.state == "queued" and bool(rig.wq.mix))
                await rig.settle(0.3)
                self.assertEqual(rig.fake.current_vid(), cur)  # rádio se neutne
                up = rig.upcoming()
                self.assertEqual(up[0], w.tracks[0].id)  # hned po hrající
                self.assertEqual(rig.pools.mood, "Mix: podkres + Archspire")
                srcs = [source_of(t) for t in self.upcoming_bg(rig)]
                self.assertEqual(set(srcs[:2]), {old_seed, w.tracks[0].id})
                rig.fake.finish_current()  # skladba rádia dohraje sama → přání
                await rig.until(lambda: w.state == "playing")
                self.assertEqual(rig.fake.current_vid(), w.tracks[0].id)
                rig.fake.finish_current()
                await rig.until(lambda: w.state == "done")
                await rig.settle(0.3)
                self.assertEqual(rig.pools.mood, "Mix: podkres + Archspire")  # po přání zůstává

        run(go())

    def test_ticked_while_somebody_elses_wish_plays(self):
        """Hraje cizí přání: „Hned po téhle" + „Obohatit" ho neutne (jako dnes),
        zahraje hned po něm. Podkres se, dokud čekají jiní, nemění; potom jde
        za naposledy splněným přáním (F-FRONTA-13) — mix, bylo-li to tohle."""
        async def go():
            async with Rig() as rig:
                seeded_radio(rig)
                await rig.background()
                p = rig.wq.submit("pusť Kabát", "Petr")
                await rig.until(lambda: p.state == "playing")
                cur = rig.fake.current_vid()
                w = rig.wq.submit(ARCHSPIRE, "Radek", play_next=True, enrich=True)
                await rig.until(lambda: w.state == "queued")
                await rig.settle(0.2)
                self.assertEqual(rig.fake.current_vid(), cur)
                self.assertEqual(rig.upcoming()[0], w.tracks[0].id)
                self.assertEqual(rig.wq.mix, [])  # čeká Petr — podkres se teď nemění
                rig.fake.finish_current()  # Petrova skladba dohraje sama → Radek
                await rig.until(lambda: w.state == "playing")

        run(go())

    def test_without_the_box_nothing_changes(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                w = rig.wq.submit(GOJIRA, "Radek")
                await rig.until(lambda: w.handed and rig.pools.artist == "Kabát")
                self.assertFalse(w.enrich)
                self.assertEqual(rig.wq.mix, [])
            async with Rig() as rig:
                await rig.background()
                w = rig.wq.submit("Holky z naší školky", "Radek")
                await rig.until(lambda: w.state == "playing")
                rig.fake.finish_current()
                await rig.until(lambda: w.state == "done")
                await rig.until(lambda: rig.wq.bg_reason.get("id") == w.id)
                self.assertEqual(rig.pools.mood, "Olympic a podobné")  # jako dřív
                self.assertEqual(rig.wq.mix, [])

        run(go())

    def test_normal_wish_after_a_mix_ends_it(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                a = rig.wq.submit(ARCHSPIRE, "Radek", enrich=True)
                await rig.until(lambda: bool(rig.wq.mix))
                b = rig.wq.submit("pusť Kabát", "Radek")
                await rig.until(lambda: b.state in ("queued", "playing"))
                for _ in range(3):  # Archspire dohraje, Kabát předá zbytek do podkresu
                    if b.handed:
                        break
                    rig.fake.finish_current()
                    await rig.settle(0.2)
                await rig.until(lambda: b.handed and rig.pools.artist == "Kabát")
                self.assertEqual(rig.wq.mix, [])
                self.assertNotEqual(rig.wq.bg_reason.get("mode"), "mix")
                self.assertTrue(a.enrich)

        run(go())

    def test_more_enrich_wishes_add_sources_up_to_four(self):
        async def go():
            async with Rig() as rig:
                seeded_radio(rig)
                await rig.background()
                labels = []
                for text in (ARCHSPIRE, "Stranded od Gojiry", "Sonne od Rammstein",
                             "Holky z naší školky"):
                    w = rig.wq.submit(text, "Radek", enrich=True)
                    await rig.until(lambda: rig.wq.bg_reason.get("id") == w.id)
                    labels.append([x["label"] for x in rig.wq.mix])
                self.assertEqual(labels[0], ["podkres", "Archspire"])
                self.assertEqual(labels[1], ["podkres", "Archspire", "Gojira"])
                self.assertEqual(labels[2], ["podkres", "Archspire", "Gojira", "Rammstein"])
                # pátý zdroj: nejstarší vypadne, zůstanou 4 nejnovější
                self.assertEqual(labels[3], ["Archspire", "Gojira", "Rammstein", "Olympic"])
                self.assertEqual(len(rig.wq.mix), MIX_SOURCES)
                self.assertEqual(rig.pools.mood, "Mix: Archspire + Gojira + Rammstein + Olympic")
                # stejný podíl: z každého zdroje stejně semínek, prokládaně
                seeds = [p.seed.id for p in rig.pools.pools]
                self.assertEqual(seeds, [x["seeds"][0].id for x in rig.wq.mix])

        run(go())

    def test_mix_survives_restart_like_any_radio(self):
        async def go():
            async with Rig() as rig:
                await rig.background()
                w = rig.wq.submit(ARCHSPIRE, "Radek", enrich=True)
                await rig.until(lambda: bool(rig.wq.mix))
                saved = rig.wq.session_state()
            bg = saved["bg"]
            self.assertEqual((bg["mode"], bg["mood"]), ("seeds", "Mix: podkres + Archspire"))
            self.assertEqual([x["label"] for x in bg["mix"]], ["podkres", "Archspire"])
            async with Rig() as rig2:
                await rig2.wq.resume(saved, now=datetime(2026, 10, 26, 10, 0))
                self.assertEqual(rig2.pools.mood, "Mix: podkres + Archspire")
                self.assertEqual([x["label"] for x in rig2.wq.mix], ["podkres", "Archspire"])
                # další „Obohatit" po restartu přimíchá dál
                g = rig2.wq.submit("Stranded od Gojiry", "Radek", enrich=True)
                await rig2.until(lambda: rig2.wq.bg_reason.get("id") == g.id)
                self.assertEqual(rig2.pools.mood, "Mix: podkres + Archspire + Gojira")
            self.assertTrue(w.enrich)

        run(go())

    def test_enrich_flag_is_kept_with_the_wish(self):
        w = Wish(id="a", token="t", who="R", source="web", text="x", created=1.0, mono=1.0,
                 enrich=True)
        self.assertTrue(Wish.from_json(w.to_json()).enrich)
        w.enrich = False
        self.assertFalse(Wish.from_json(w.to_json()).enrich)

    def test_nothing_playing_means_todays_behaviour(self):
        """Bez podkresu (nic nehrálo) není s čím míchat — jako bez zaškrtnutí."""
        async def go():
            async with Rig() as rig:
                w = rig.wq.submit("Holky z naší školky", "Radek", enrich=True)
                await rig.until(lambda: w.state == "playing")
                rig.fake.finish_current()
                await rig.until(lambda: w.state == "done")
                await rig.until(lambda: rig.wq.bg_reason.get("id") == w.id)
                self.assertEqual(rig.pools.mood, "Olympic a podobné")
                self.assertEqual(rig.wq.mix, [])

        run(go())

    def test_seeds_are_equal_and_interleaved(self):
        a = {"label": "A", "seeds": [T("a1"), T("a2"), T("a3")]}
        b = {"label": "B", "seeds": [T("b1"), T("b2")]}
        c = {"label": "C", "seeds": [T("c1")]}
        self.assertEqual([t.id for t in _mix_seeds([a, b])],
                         [tw.vid("a1"), tw.vid("b1"), tw.vid("a2"), tw.vid("b2")])
        self.assertEqual([t.id for t in _mix_seeds([a, b, c])],
                         [tw.vid("a1"), tw.vid("b1"), tw.vid("c1")])
        self.assertEqual(mix_label([a, b]), "Mix: A + B")


class WebBox(unittest.TestCase):
    def test_checkbox_next_to_play_next_sends_enrich_once(self):
        src = (Path(__file__).resolve().parents[1] / "ytdj" / "web" / "static"
               / "index.html").read_text(encoding="utf-8")
        nxt = src.index('id="playNext"')
        box = src.index('id="enrich"')
        self.assertLess(nxt, box)
        self.assertLess(box - nxt, 600)  # hned vedle „Hned po téhle skladbě"
        self.assertIn("Obohatit", src[box:box + 80])
        self.assertIn("enrich: !!el.enrich.checked", src)
        self.assertIn("el.enrich.checked = false", src)  # nepamatuje se

    def test_server_passes_the_flag(self):
        src = (Path(__file__).resolve().parents[1] / "ytdj" / "web" / "server.py").read_text(
            encoding="utf-8")
        self.assertIn('enrich = data.get("enrich") is True', src)
        self.assertIn("enrich=enrich", src)


if __name__ == "__main__":
    unittest.main()
