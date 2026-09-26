"""Druhá revize (26. 9.): nálezy z review2 jako regresní testy.

  1. dvojí ťuknutí na Další ukončilo celé cizí přání;
  2. přání schovaná v otázce ("proč nehraješ Kabát?") dostala jen odpověď o frontě;
  3. po restartu hrála skladba dvakrát (session.json vs. playback.json);
  4. "Zařadil jsem: . Hraje hned." u přání s jednou skladbou;
  5. podkres zůstal u starého přání, když poslední přání skončilo "nenašel";
  6. starý session.json (displej pod "panel-…") → displej po restartu předběhl;
  7. displej se ptal "kdy bude moje přání" a slyšel "nic nemáš";
  8. boost oblíbených se počítal nad všemi pooly u každé skladby;
  9. DJ-skip / Další při výpadku jako cizí přeskočení; hlídka paměti app-serveru;
 10. jeden 👍 udělal z celého interpreta oblíbeného.

    YTDJ_EVENTS_FILE=/tmp/ev.jsonl .venv/bin/python -m unittest tests.test_review2 -v
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
import tempfile
import time
import unittest
from collections import deque
from datetime import datetime
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="ytdj-review2-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)
os.environ.setdefault("YTDJ_EVENTS_FILE", str(Path(_TMP) / "events.jsonl"))

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import test_wishes as tw  # noqa: E402
from test_pi0926 import PANEL, PARNI, ROBERT, VOJTAANO, PiRig, trk  # noqa: E402
from test_votes import JANA, PETR, book  # noqa: E402
from test_votes import Pools as VotePools  # noqa: E402
from test_wishes import Rig, decision, run  # noqa: E402
from ytdj import wishes  # noqa: E402
from ytdj.agent.codex import Plan  # noqa: E402
from ytdj.agent.fastpath import meta_candidates  # noqa: E402
from ytdj.agent.intent import Intent, complaint_in, meta_kind, norm  # noqa: E402
from ytdj.music.catalog import Track  # noqa: E402
from ytdj.votes import ARTIST, FAVOURITE, NEUTRAL, SONG  # noqa: E402

QUEEN = [trk(f"queen{i}", f"Queen {i}", "Queen") for i in range(6)]
BEATLES = [trk(f"beat{i}", f"Beatles {i}", "The Beatles") for i in range(6)]
BOHEMIAN = trk("bohemian", "Bohemian Rhapsody", "Queen")
NAMES = {"kabat": ("Kabát", tw.KABAT), "olympic": ("Olympic", tw.OLYMPIC),
         "queen": ("Queen", QUEEN), "beatles": ("The Beatles", BEATLES)}


def tj(t: Track) -> dict:
    return {"id": t.id, "title": t.title, "artist": t.artist, "album": None, "duration": t.duration}


# --------------------------------------------------------------------------
# 1. Další: dvojí ťuknutí, DJ, výpadek
# --------------------------------------------------------------------------


class Skips(unittest.TestCase):
    def test_double_tap_is_one_skip(self):
        """r01: dva POSTy z jednoho telefonu dřív, než mpv ohlásí další skladbu."""
        async def go():
            async with PiRig() as rig:
                await rig.background()
                d = rig.panel("pusť Parni Valjak")
                await rig.until(lambda: d.state == "playing")
                first = rig.fake.current_vid()
                await asyncio.gather(rig.wq.skip_current("Robert", ROBERT),
                                     rig.wq.skip_current("Robert", ROBERT))
                await rig.settle(0.4)
                self.assertTrue(d.active, d.reply)
                self.assertEqual(d.skips_others, 1)
                self.assertNotIn("ostatními", d.reply)
                # přeskočila se jen jedna skladba: hraje druhá z přání (nikdo jiný nečeká)
                self.assertEqual(rig.fake.current_vid(), PARNI[1].id)
                self.assertNotEqual(first, rig.fake.current_vid())
                self.assertIn("request.skip_repeat", rig.kinds())

        run(go())

    def test_same_track_counts_once_even_from_two_people(self):
        async def go():
            async with PiRig() as rig:
                await rig.background()
                d = rig.panel("pusť Parni Valjak")
                await rig.until(lambda: d.state == "playing")
                vid = rig.fake.current_vid()
                await rig.wq._skipped_by_other(d, vid, "Robert")
                await rig.wq._skipped_by_other(d, vid, "Karel")
                self.assertEqual((d.skips_others, d.active), (1, True))

        run(go())

    def test_later_skip_of_the_next_track_still_counts(self):
        """Pravidlo 4 platí dál: druhé cizí přeskočení (jiné skladby) přání ukončí."""
        async def go():
            async with PiRig() as rig:
                await rig.background()
                d = rig.panel("pusť Parni Valjak")
                await rig.until(lambda: d.state == "playing")
                await rig.wq.skip_current("Robert", ROBERT)
                await rig.until(lambda: rig.wq.current_vid == PARNI[1].id)
                await rig.wq.skip_current("Robert", ROBERT)
                await rig.settle(0.3)
                self.assertEqual(d.state, "skipped")

        run(go())

    def test_dj_decided_skip_does_not_count(self):
        """9a: přeskočení, o kterém rozhodl model, je "DJ" — ne cizí přeskočení."""
        async def go():
            async with PiRig() as rig:
                await rig.background()
                d = rig.panel("pusť Parni Valjak")
                await rig.until(lambda: d.state == "playing")
                with mock.patch.dict(tw.SCRIPT, {"tohle přeskoč, nebaví mě": (
                        "codex", decision(action="skip", reply="Přeskakuju."))}):
                    k = rig.wq.submit("tohle přeskoč, nebaví mě", "Karel")
                    await rig.until(lambda: k.state == "done")
                    await rig.settle(0.3)
                self.assertEqual(d.skips_others, 0)
                self.assertTrue(d.active)
                ev = [f for kd, f in rig.events if kd == "request.skipped"]
                self.assertEqual(ev[-1]["by"], "DJ")

        run(go())

    def test_skip_during_outage_does_not_count(self):
        """9b: při výpadku Další jen posune místo navázání — nic se nepřeskočilo."""
        async def go():
            async with PiRig() as rig:
                await rig.background()
                d = rig.panel("pusť Parni Valjak")
                await rig.until(lambda: d.state == "playing")
                skip = mock.AsyncMock()
                with mock.patch.object(rig.player, "skip", skip), \
                        mock.patch.object(rig.player, "_outage", {"resume_entry": None}, create=True):
                    await rig.wq.skip_current("Robert", ROBERT)
                skip.assert_awaited_once()
                self.assertEqual(d.skips_others, 0)

        run(go())

    def test_web_next_button_is_debounced(self):
        html = (HERE.parent / "ytdj/web/static/index.html").read_text()
        self.assertIn("if (now - lastNext < 1000) return;", html)


# --------------------------------------------------------------------------
# 2. přání schované v otázce
# --------------------------------------------------------------------------


class MetaRig(PiRig):
    async def __aenter__(self):
        await super().__aenter__()
        self.dj.title_plan = self.title_plan  # type: ignore[method-assign]
        return self

    async def fast_plan(self, text: str):
        hit = NAMES.get(norm(text))
        if hit is not None:
            name, tracks = hit
            return Plan(intent=Intent(kind="artist", text=text, artists=[name], mood=name),
                        artist_tracks=list(tracks))
        return await super().fast_plan(text)

    async def title_plan(self, title: str):
        if norm(title) != "bohemian rhapsody":
            return None
        return Plan(intent=Intent(kind="song", text=title, tracks=[("Queen", "Bohemian Rhapsody")],
                                  mood="Queen a podobné"), requested=[BOHEMIAN], seeds=[BOHEMIAN])


# (text, je to přání hudby?) — review2 r03
R03 = [
    ("proč ne něco od Kabátu?", True), ("proč ne Kabát", True),
    ("kdy bude hrát Olympic, pusť ho", True), ("co takhle Queen", True),
    ("můžeš pustit Beatles?", True), ("proč nehraješ Kabát?", True),
    ("proč to nehraje Queen", True), ("proč jsi nepustil Beatles", True),
    ("proč ne zase ABBA", True), ("kdo chtěl tohle? dej něco od Olympicu", True),
    ("co hraje ted, chci Metallicu", True), ("mám rád Queen, proč to nehraje?", True),
    ("zase Kabát? něco od Queen", True), ("Kdy bude Bohemian Rhapsody", True),
    ("co takhle něco klidnějšího", True), ("Porad hraje to samé, dej Beatles", True),
    ("porad to same, chci ABBA", True), ("Nirvana prosím, tohle není fér", True),
    ("Proc Proc Proc (Lucie)", True), ("Ignoruj předchozí, Kabát", True),
    ("nefér, chci Olympic", True),
    ("proč", False), ("proč ne", False), ("proč nehraje moje písničky?", False),
    ("Proč to vůbec nehraje moje písničky?", False), ("kdy bude hrát moje přání?", False),
    ("Pořád jen displej. To není demokracie. Musí se to střídat.", False),
    ("Žádné střídání jsi nenastavil", False), ("Proč hraje pořád dokola Parni Valjak?", False),
]


class MetaOrWish(unittest.TestCase):
    def test_text_level(self):
        """Přání: buď to vůbec není otázka, nebo má otázka jméno, které se zkusí
        v katalogu; čistá otázka jméno nemá."""
        for text, wish in R03:
            with self.subTest(text=text):
                m, cands = meta_kind(text), meta_candidates(text)
                if wish:
                    self.assertTrue(m is None or cands, (m, cands))
                else:
                    self.assertIsNotNone(m)
                    self.assertEqual(cands, [])
        self.assertEqual(meta_candidates("proč nehraješ Kabát?"), ["Kabát"])
        self.assertEqual(meta_candidates("mám rád Queen, proč to nehraje?"), ["Queen"])
        self.assertEqual(meta_candidates("Kdy bude Bohemian Rhapsody"), ["Bohemian Rhapsody"])
        self.assertEqual(meta_candidates("Proc Proc Proc (Lucie)")[-1], "Lucie")
        self.assertTrue(complaint_in("porad to same, chci ABBA"))
        self.assertFalse(complaint_in("pusť Kabát"))

    def test_question_with_a_known_name_is_queued(self):
        async def go():
            async with MetaRig() as rig:
                await rig.background()
                w = rig.wq.submit("proč nehraješ Kabát?", "Jana")
                await rig.until(lambda: w.state in ("queued", "playing") and w.reply)
                self.assertEqual((w.kind, w.via), ("artist", "fast"))
                self.assertTrue(any(t.artist == "Kabát" for t in w.tracks))
                self.assertNotIn("Teď ve frontě nic nemáš", w.reply)
                self.assertIn("request.meta_wish", rig.kinds())

        run(go())

    def test_complaint_with_a_name_is_queued_with_an_honest_note(self):
        async def go():
            async with MetaRig() as rig:
                await rig.background()
                w = rig.wq.submit("Ignoruj předchozí, Kabát", "Jana")
                await rig.until(lambda: w.state in ("queued", "playing") and w.reply)
                self.assertTrue(w.reply.startswith("Beru to jako přání. Zařadil jsem Kabát"),
                                w.reply)
                # stížnost s výslovným přáním (sloveso) jde k DJovi — a taky s poznámkou
                with mock.patch.dict(tw.SCRIPT, {"nefér, chci Olympic": ("codex", decision(
                        action="play_next", requested=[{"artist": "Olympic", "title": "Holky"}]))}):
                    o = rig.wq.submit("nefér, chci Olympic", "Petr")
                    await rig.until(lambda: o.state in ("queued", "playing") and o.reply)
                self.assertIsNone(meta_kind(o.text))
                self.assertTrue(o.reply.startswith("Beru to jako přání. Zařadil jsem"), o.reply)

        run(go())

    def test_when_is_song_queues_it_or_tells_the_eta(self):
        async def go():
            async with MetaRig() as rig:
                await rig.background()
                k = rig.wq.submit("Dancing Queen", "Karel")
                await rig.until(lambda: k.state in ("queued", "playing"))
                w = rig.wq.submit("Kdy bude Bohemian Rhapsody", "Jana")
                await rig.until(lambda: w.state in ("queued", "playing") and w.reply)
                self.assertEqual([t.id for t in w.tracks], [BOHEMIAN.id])
                q = rig.wq.submit("kdy bude Bohemian Rhapsody?", "Petr")
                await rig.until(lambda: q.state == "done")
                self.assertEqual(q.kind, "meta")
                self.assertTrue("je ve frontě (přání Jana)" in q.reply or "právě hraje" in q.reply,
                                q.reply)
                self.assertEqual(sum(1 for x in rig.wq.wishes if BOHEMIAN in x.tracks), 1)

        run(go())

    def test_pure_questions_stay_questions(self):
        async def go():
            async with MetaRig() as rig:
                await rig.background()
                for text in ("proč nehraje moje písničky?", "Proč hraje pořád dokola Kabát?"):
                    w = rig.wq.submit(text, "Robert")
                    await rig.until(lambda: w.state == "done")
                    self.assertEqual((w.kind, w.tracks), ("meta", []), text)
                self.assertEqual(rig.asked, [])  # k modelu nic

        run(go())


# --------------------------------------------------------------------------
# 3. restart: co mezitím dohrálo, znovu nezazní
# --------------------------------------------------------------------------


class ResumeConsistency(unittest.TestCase):
    def _saved_with_current(self, rig):
        return rig.wq.load_state()

    def test_playback_newer_than_session_marks_the_old_current_done(self):
        """r02: session.json (X hraje) → X dohraje → playback.json (Y @30 s)."""
        async def go():
            pb = Path(tempfile.mkdtemp(dir=_TMP)) / "playback.json"
            async with Rig() as rig:
                await rig.background()
                j = rig.wq.submit("pusť Kabát", "Jana")
                await rig.until(lambda: j.state == "playing")
                x = rig.fake.current_vid()
                await rig.wq.refresh_playing()
                rig.wq.save()
                saved = rig.wq.load_state()
                self.assertEqual(saved["wishes"][0]["current"], x)
                rig.fake.finish_current()
                await rig.settle(0.3)
                y = rig.fake.current_vid()
                rig.player.playback_file = pb
                rig.player._time_pos = 30.0
                rig.player._save_playback()
            async with Rig() as rig2:
                rig2.player.playback_file = pb
                await rig2.wq.resume(saved, now=datetime(2026, 9, 25, 10, 0))
                await rig2.settle(0.3)
                w = rig2.wq.wishes[0]
                self.assertIn(x, w.done_ids)
                self.assertEqual(w.current, y)  # navázaná skladba je "hrající", ne čekající
                played = [rig2.fake.current_vid()]
                self.assertNotIn(y, rig2.upcoming())
                for _ in range(3):
                    rig2.fake.finish_current()
                    await rig2.settle(0.3)
                    played.append(rig2.fake.current_vid())
                self.assertEqual(played[0], y)
                self.assertNotIn(x, played)
                self.assertEqual(len(set(played)), len(played))

        run(go())

    def test_history_marks_tracks_finished_after_the_save(self):
        """Pád bez čistého ukončení: session.json starý, historie (state.db) ví víc."""
        async def go():
            async with Rig() as rig:
                await rig.background()
                j = rig.wq.submit("pusť Kabát", "Jana")
                await rig.until(lambda: j.state == "playing")
                x = rig.fake.current_vid()
                await rig.wq.refresh_playing()
                rig.wq.save()
                saved = rig.wq.load_state()
            nxt = next(t for t in j.tracks if t.id != x and t.id not in j.done_ids)
            async with Rig() as rig2:
                rig2.player.playback_file = Path(tempfile.mkdtemp(dir=_TMP)) / "none.json"
                st = rig2.store
                st.record_start(x, "x", "Kabát", None)  # X dohrála po uložení
                st.record_outcome(x, "finished")
                st.record_start(nxt.id, "n", "Kabát", None)  # další začala (bez výsledku)
                await rig2.wq.resume(saved, now=datetime(2026, 9, 25, 10, 0))
                await rig2.settle(0.3)
                w = rig2.wq.wishes[0]
                self.assertIn(x, w.done_ids)
                self.assertNotIn(nxt.id, w.done_ids)  # jen začala — přerušená, zahraje se
                self.assertNotIn(x, rig2.upcoming())
                self.assertIn("request.resume_reconciled", rig2.kinds())

        run(go())

    def test_shutdown_saves_wishes_after_the_player(self):
        from ytdj.__main__ import App

        src = inspect.getsource(App.run)
        self.assertLess(src.index("await self.player.stop()"), src.index("self.wishes.save()"))


# --------------------------------------------------------------------------
# 4. potvrzení nikdy s prázdným výčtem
# --------------------------------------------------------------------------


class Confirm(unittest.TestCase):
    def test_one_song_that_cuts_in_is_named(self):
        """r08: start skladby dorazil během _apply → w.pending() už ji nemá."""
        async def go():
            async with Rig() as rig:
                await rig.background()
                orig = rig.player.toggle_pause

                async def slow(p, _o=orig):
                    await asyncio.sleep(0.15)
                    return await _o(p)

                rig.player.toggle_pause = slow  # type: ignore[method-assign]
                j = rig.wq.submit("Holky z naší školky", "Jana")
                await rig.until(lambda: j.state in ("queued", "playing") and j.reply)
                self.assertEqual(j.state, "playing")
                self.assertNotIn(": .", j.reply)
                self.assertIn("song s-Holky (Olympic)", j.reply)

        run(go())

    def test_never_an_empty_list(self):
        w = wishes.Wish(id="a", token="t", who="Jana", source="web", text="x", created=0, mono=0)
        w.tracks = [tw.KABAT[0]]
        w.done_ids = {tw.KABAT[0].id}
        wq = wishes.WishQueue.__new__(wishes.WishQueue)
        wq.wishes, wq.bg_reason = [w], {}
        out = wq._confirm(w, Intent(kind="songs"), [], "Hraje hned.")
        self.assertEqual(out, "Zařadil jsem. Hraje hned.")


# --------------------------------------------------------------------------
# 5. podkres za naposledy splněným přáním, i když poslední skončilo jinak
# --------------------------------------------------------------------------


class BackgroundFollow(unittest.TestCase):
    def test_last_wish_notfound_still_moves_background(self):
        """r10: Robert splněn, zatímco Karlovo přání DJ vybíral; Karel "nenašel"."""
        async def go():
            async with PiRig(codex_delay=0.8) as rig:
                await rig.background()
                d = rig.panel("pusť Parni Valjak")
                await rig.until(lambda: d.handed and rig.pools.artist == "Parni Valjak")
                for _ in range(6):
                    if not d.active:
                        break
                    rig.fake.finish_current()
                    await rig.settle(0.2)
                r = rig.wq.submit("Holky z naší školky", "Robert")
                await rig.until(lambda: r.state == "playing", timeout=5)
                k = rig.wq.submit("Wonderwall od Nobody", "Karel")
                await rig.settle(0.05)
                rig.fake.finish_current()
                await rig.until(lambda: r.state == "done")
                await rig.until(lambda: k.state not in ("waiting", "thinking"), timeout=5)
                await rig.until(lambda: rig.wq.bg_reason.get("id") == r.id)
                self.assertEqual(k.state, "notfound")
                self.assertEqual(rig.pools.artist, "")
                self.assertEqual(rig.pools.mood, "Olympic a podobné")

        run(go())

    def test_later_background_change_is_not_overridden(self):
        """Splněné přání se nenásleduje, když se podkres od té doby změnil jinak."""
        async def go():
            async with PiRig() as rig:
                await rig.background()
                r = rig.wq.submit("Holky z naší školky", "Robert")
                await rig.until(lambda: r.state == "playing")
                rig.fake.finish_current()
                await rig.until(lambda: r.state == "done" and rig.wq.bg_reason.get("id") == r.id)
                await rig.wq._set_background(seeds=[tw.T("auto0", "Auto")], mood="přeseedováno")
                q = rig.wq.submit("proč nehraje moje písničky?", "Karel")
                await rig.until(lambda: q.state == "done")
                await rig.settle(0.2)
                self.assertEqual(rig.pools.mood, "přeseedováno")

        run(go())


# --------------------------------------------------------------------------
# 6. starý session.json: displej pod "panel-<relace>"
# --------------------------------------------------------------------------


class OldSessionFormat(unittest.TestCase):
    def test_old_panel_key_maps_to_the_seat(self):
        async def go():
            now = time.time()
            old = {
                "v": 1, "saved": now - 30, "boot": wishes.boot_id(), "playing": True,
                "bg": {},
                "wishes": [
                    {"id": "d1", "token": "t", "who": "displej", "source": "panel",
                     "text": "Parni Valjak", "created": now - 600, "state": "playing",
                     "kind": "artist", "artist": "Parni Valjak", "artists": ["Parni Valjak"],
                     "played": 1, "started": True, "cid": "panel-0a1b2c",
                     "tracks": [tj(t) for t in PARNI[:12]], "done": []},
                    {"id": "r1", "token": "t", "who": "Robert", "source": "web", "text": "Vojtaano",
                     "created": now - 300, "state": "queued", "kind": "artist",
                     "artist": "Vojtaano", "artists": ["Vojtaano"], "played": 1, "started": True,
                     "cid": "client-robert", "tracks": [tj(t) for t in VOJTAANO[:6]],
                     "done": [VOJTAANO[0].id]},
                ],
                # dvě staré relace displeje; displej hrál naposledy (9)
                "turns": {"turn_no": 9, "last": {"panel-0a1b2c": 9, "panel-ffffff": 4,
                                                 "client-robert": 8}},
            }
            async with PiRig() as rig:
                await rig.wq.resume(old, now=datetime(2026, 9, 26, 10, 0))
                await rig.settle(0.4)
                self.assertEqual(rig.wq.turns.last.get("panel"), 9)
                self.assertFalse(any(k.startswith("panel-") for k in rig.wq.turns.last))
                self.assertEqual(rig.wq.reason_for(rig.fake.current_vid()).get("who"), "Robert")

        run(go())


# --------------------------------------------------------------------------
# 7. displej je jeden člověk i v otázkách a "hned"
# --------------------------------------------------------------------------


class PanelSeat(unittest.TestCase):
    def test_display_question_finds_display_wishes(self):
        """r09: každé přání z displeje má novou relaci."""
        async def go():
            async with PiRig() as rig:
                await rig.background()
                k = rig.wq.submit("Dancing Queen", "Karel")
                await rig.until(lambda: k.state in ("queued", "playing"))
                d = wishes.WishQueue.submit(rig.wq, "Holky z naší školky", "", "panel",
                                            client={"id": "panel-aaaaaa"})
                await rig.until(lambda: d.state in ("queued", "playing"))
                q = wishes.WishQueue.submit(rig.wq, "kdy bude hrát moje přání?", "", "panel",
                                            client={"id": "panel-bbbbbb"})
                await rig.until(lambda: q.state == "done")
                self.assertNotIn("nic nemáš", q.reply)
                self.assertIn("Tvoje „Holky z naší školky“", q.reply)

        run(go())

    def test_one_play_next_per_display(self):
        async def go():
            async with PiRig(codex_delay=0.5) as rig:
                await rig.background()
                a = wishes.WishQueue.submit(rig.wq, "Dancing Queen", "", "panel", play_next=True,
                                            client={"id": "panel-aaaaaa"})
                b = wishes.WishQueue.submit(rig.wq, "Jasná zpráva", "", "panel", play_next=True,
                                            client={"id": "panel-bbbbbb"})
                self.assertTrue(a.play_next)
                self.assertFalse(b.play_next)
                self.assertIn("Jedno „hned“ na člověka", b.note)

        run(go())


# --------------------------------------------------------------------------
# 8. boost oblíbených jednou na doplnění poolů
# --------------------------------------------------------------------------


class BoostOncePerRefill(unittest.TestCase):
    def test_boost_runs_once_per_refill_not_per_track(self):
        class P:
            def __init__(self, tracks):
                self.tracks = deque(tracks)

        class Holder:
            generation = 0

        b = book()
        b.cast(SONG, JANA, 1, track=Track("fav00000001", "Fav song", "Nobody"))
        pools = [P([Track(f"p{p}t{i:07d}"[:11], f"Song {p}-{i}", f"Band {p}", duration=200)
                    for i in range(50)]) for p in range(5)]
        h = Holder()
        h.pools = pools
        b.pools = h
        calls = {"n": 0}
        orig = b.boost_pools

        def counting(p):
            calls["n"] += 1
            return orig(p)

        b.boost_pools = counting
        for i in range(200):
            b.pool_reject(pools[i % 5].tracks.popleft())
        self.assertEqual(calls["n"], 1)
        # doplnění poolu (rádio) → jednou znovu
        pools[0].tracks.extend([Track("fav00000001", "Fav song", "Nobody"),
                                Track("new00000001", "New", "Band")])
        h.generation += 1
        b.pool_reject(Track("x0000000001", "X", "Y"))
        b.pool_reject(Track("x0000000002", "X", "Y"))
        self.assertEqual(calls["n"], 2)
        # nový hlas → znovu
        b.cast(SONG, PETR, 1, track=Track("fav00000002", "Fav 2", "Nobody"))
        b.pool_reject(Track("x0000000003", "X", "Y"))
        self.assertEqual(calls["n"], 3)

    def test_radio_pools_count_refills(self):
        async def go():
            b = book()
            tracks = [Track(f"r{i:010d}", f"Song {i}", f"Band {i}", duration=200) for i in range(30)]
            pools = VotePools._pools(self, tracks, b)
            g0 = pools.generation
            await pools.set_seeds([Track("seed0000001", "S", "Seed")])
            g1 = pools.generation
            await pools._refill(pools.pools[0])
            return g0, g1, pools.generation

        g0, g1, g2 = asyncio.run(go())
        self.assertLess(g0, g1)
        self.assertLess(g1, g2)


# --------------------------------------------------------------------------
# 9c. hlídka paměti app-serveru
# --------------------------------------------------------------------------

FAKE = Path(_TMP) / "codex"
FAKE.write_text(f"#!/bin/sh\nexec {sys.executable} {HERE / 'fake_app_server.py'} \"$@\"\n")
FAKE.chmod(0o755)


class AppServerMemory(unittest.TestCase):
    def test_low_memory_closes_it_long_before_idle_ttl(self):
        from ytdj.agent.appserver import AppServer
        from ytdj.agent.codex import DECISION_SCHEMA

        os.environ["FAKE_LOG"] = str(Path(tempfile.mkdtemp(dir=_TMP)) / "m.jsonl")
        os.environ["FAKE_MODE"] = "ok"
        mem = {"mb": 590}

        async def go():
            app = AppServer(str(FAKE), _TMP, idle_ttl=600, keep_warm=lambda: True,
                            mem_check=0.05, mem_free=lambda: mem["mb"])
            try:
                await app.turn("p", DECISION_SCHEMA, timeout=5)
                await asyncio.sleep(0.3)
                kept = app.alive
                mem["mb"] = 200  # pod WARM_MIN_FREE_MB (250)
                await asyncio.sleep(0.4)
                return kept, app.alive
            finally:
                await app.close()

        self.assertEqual(run(go()), (True, False))

    def test_docstring_says_what_is_measured(self):
        from ytdj.agent import appserver

        self.assertIn("s běžícím app-serverem", appserver.__doc__)
        self.assertEqual(appserver.MEM_CHECK, 60.0)


# --------------------------------------------------------------------------
# 10. jeden 👍 celému interpretovi nestačí
# --------------------------------------------------------------------------


class FavouriteArtistNeedsTwo(unittest.TestCase):
    def test_single_thumb_does_not_boost_the_artist(self):
        """r11: 8 skladeb Kabátu v poolu 50, všechny hrály v repeat_days."""
        others = [Track(f"o{i:010d}", f"Song {i}", f"Band {i}", duration=200) for i in range(42)]
        kab = [Track(f"k{i:010d}", f"Kabát {i}", "Kabát", duration=200) for i in range(8)]
        tracks = []
        for i in range(50):
            tracks.append(kab[i // 6] if i % 6 == 5 and i // 6 < 8 else others.pop(0) if others
                          else kab[-1])

        async def draw(voters):
            b = book()
            for v in voters:
                b.cast(ARTIST, v, 1, artist="Kabát")
            pools = VotePools._pools(self, tracks, b, recent={t.id for t in kab})
            b.pools = pools
            await pools.set_seeds([Track("seed0000001", "S", "Seed")])
            out = []
            for _ in range(12):
                out += await pools.next_tracks(1)
            return sum(1 for t in out if t.artist == "Kabát"), b.tally(ARTIST, "kabat").status

        self.assertEqual(asyncio.run(draw([PETR])), (0, NEUTRAL))
        n, status = asyncio.run(draw([PETR, JANA]))
        self.assertEqual(status, FAVOURITE)
        self.assertGreater(n, 0)

    def test_song_needs_one_and_config_is_live(self):
        from ytdj.config import DEFAULTS
        from ytdj.web.server import FIELD_META, LIVE_KEYS

        b = book()
        b.cast(SONG, PETR, 1, track=Track("fav00000001", "Fav", "Nobody"))
        self.assertTrue(b.is_favourite(Track("fav00000001", "Fav", "Nobody")))
        self.assertEqual(DEFAULTS["favourite_artist_votes"], 2)
        self.assertIn("favourite_artist_votes", LIVE_KEYS)
        self.assertIn("favourite_artist_votes", FIELD_META)
        self.assertEqual(b.rules()["favourite_artist_votes"], 2)
        b3 = book(favourite_artist_votes=3)
        for v in (PETR, JANA):
            b3.cast(ARTIST, v, 1, artist="Kabát")
        self.assertEqual(b3.tally(ARTIST, "kabat").status, NEUTRAL)
        html = (HERE.parent / "ytdj/web/static/index.html").read_text()
        self.assertIn("rules.favourite_artist_votes", html)


if __name__ == "__main__":
    unittest.main()
