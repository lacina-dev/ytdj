"""Konec dne bez restartu (F-RESTART-16, vlastník 3. 11. 2026 „ano"):
nedohraná přání z minulého dne a podkres, který spustila, končí s novým
dnem — stejně jako po restartu (F-RESTART-11), se stejnou definicí dne
(ytdj/day.py). Proti falešnému mpv a s falešnými hodinami:

    YTDJ_EVENTS_FILE=/tmp/x.jsonl .venv/bin/python -m unittest tests.test_new_day

Případ z Pi: Vítek 2. 11. ve 21:37 „oblíbené kanceláře" (pak dál, dokud
neřekneš jinak), ve 21:37:51 pauza, bez restartu přes noc; 3. 11. v 11:23
přání dostalo tah a web ukazoval „Vítek — oblíbené kanceláře".
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="ytdj-newday-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)
os.environ.setdefault("YTDJ_EVENTS_FILE", str(Path(_TMP) / "events.jsonl"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_wishes as tw  # noqa: E402
from test_favourites_mode import V, fav_decision, office  # noqa: E402
from ytdj import day, wishes  # noqa: E402
from ytdj.agent.intent import build_intent  # noqa: E402

EVENING = datetime(2026, 11, 2, 21, 37)  # Vítkovo přání
PAUSED = datetime(2026, 11, 2, 21, 37, 51)
MIDNIGHT = datetime(2026, 11, 3, 0, 0, 10)
MORNING = datetime(2026, 11, 3, 11, 23)  # tah přání na Pi


class Clock:
    """Falešné hodiny: time.time() posunuté na zvolený okamžik a dál běží."""

    def __init__(self) -> None:
        self.real = time.time
        self.off = 0.0

    def __call__(self) -> float:
        return self.real() + self.off

    def at(self, dt: datetime) -> float:
        self.off = dt.timestamp() - self.real()
        return dt.timestamp()


@contextmanager
def fake_clock():
    clock = Clock()
    flag = Path(tempfile.mkdtemp(dir=_TMP)) / "synchronized"
    flag.touch()  # hodiny seřízené ze sítě
    with mock.patch("time.time", clock), mock.patch.object(wishes, "CLOCK_SYNC_FLAG", flag):
        yield clock


def kinds(rig, kind: str) -> list[dict]:
    return [f for k, f in rig.events if k == kind]


def wire_office(rig) -> None:
    """Hlasování a oblíbené kanceláře jako na Pi (bez nich není režim oblíbených)."""
    app = SimpleNamespace(store=rig.store, cfg=rig.cfg, wishes=rig.wq, pools=rig.pools,
                          dj=rig.dj, player=rig.player)
    book = V.wire(app)
    book.prng.seed(3)
    office(book)


async def fresh(rig) -> bool:
    """Nic z minulého dne nečeká — Hrát teď = chytrý rozjezd (play_or_start).
    Pozastavená skladba smí zůstat jen jako zbytek, který Hrát přeskočí."""
    st = await rig.player.status()
    return not st.queue and rig.wq.idle_for_start(st)


async def morning_play(rig, old: set[str]) -> None:
    """Hrát ráno (play_or_start): chytrý rozjezd, zbytek z večera nezazní."""
    self_check = rig.wq.idle_for_start(await rig.player.status())
    assert self_check, "Hrát by jen odpauzovalo včerejšek"
    await rig.wq.start_idle()
    await rig.until(lambda: rig.fake.current_vid() not in old | {None}
                    and not rig.player._paused)
    assert rig.wq.bg_reason.get("kind") == "start", rig.wq.bg_reason
    assert rig.pools.favourites == "" and "oblíbené" not in rig.pools.mood


async def vitek_favourites(rig) -> wishes.Wish:
    """Vítek: „oblíbené kanceláře" → blok oblíbených a podkres pořád."""
    wire_office(rig)
    plain = rig.dj.interpret

    async def interpret(text, auto=False):
        if auto:  # rozjezd (C7) jde dál podle skriptu testů fronty
            return await plain(text, auto=True)
        return build_intent(text, fav_decision("office", True, True), auto=auto)

    rig.dj.interpret = interpret
    await rig.background()
    w = rig.wq.submit("oblíbené kanceláře", "Vítek")
    await rig.until(lambda: w.state in ("queued", "playing") and w.reply)
    await rig.until(lambda: rig.fake.current_vid() in {t.id for t in w.tracks}, 5)
    return w


def names_shown(rig) -> set[str]:
    """Jména, která vidí web, displej i telka (fronta přání a „proč hraje")."""
    out = {d["who"] for d in rig.wq.public()}
    cur = rig.fake.current_vid()
    reason = rig.wq.reason_for(cur) if cur else dict(rig.wq.bg_reason)
    out |= {reason.get("who") or "", reason.get("from_who") or ""}
    out |= {(rig.wq.last or {}).get("who") or ""}
    return out - {""}


class DayDefinition(unittest.TestCase):
    def test_one_day_for_restart_and_for_running(self):
        """Půlnoc dělí dny — pro obnovu po zapnutí Pi i pro běh bez restartu."""
        self.assertTrue(day.earlier_day(EVENING.timestamp(), MORNING))
        self.assertTrue(day.earlier_day(datetime(2026, 11, 2, 23, 59, 59), MIDNIGHT))
        self.assertFalse(day.earlier_day(datetime(2026, 11, 3, 0, 0, 1), MORNING))
        self.assertEqual(day.day_of(MORNING), MORNING.date())
        saved = {"saved": EVENING.timestamp(), "clock": True}
        self.assertEqual(wishes.cold_restore(saved, True, MORNING), (False, "earlier_day"))
        self.assertEqual(wishes.cold_restore(saved, True, datetime(2026, 11, 2, 23, 0)),
                         (True, "same_day"))


class NewDay(unittest.TestCase):
    def test_vitek_case_wish_and_favourites_end_overnight_while_paused(self):
        async def go():
            with fake_clock() as clock:
                clock.at(EVENING)
                async with tw.Rig() as rig:
                    w = await vitek_favourites(rig)
                    self.assertEqual(rig.pools.favourites, "office")
                    self.assertEqual(rig.wq.bg_reason.get("who"), "Vítek")
                    clock.at(PAUSED)
                    await rig.wq.stop_all()  # pauza
                    await rig.settle(0.1)
                    # večer ještě nic nekončí
                    self.assertEqual((await rig.wq.expire_days())["expired"], 0)
                    self.assertTrue(w.active)
                    clock.at(MIDNIGHT)

                    def turns_of(x):
                        return sum(1 for f in kinds(rig, "request.turn") if f.get("id") == x.id)

                    turns_at_midnight = turns_of(w)
                    await rig.wq.refresh_playing()
                    out = await rig.wq.expire_days()
                    await rig.settle(0.2)
                    self.assertEqual(out, {"expired": 1, "background": "none", "dropped": True})
                    self.assertEqual(rig.wq.active(), [])
                    self.assertIsNone(rig.wq.by_id(w.id))
                    # podkres pryč, nic nečeká, ani pozastavená skladba
                    self.assertEqual(rig.pools.favourites, "")
                    self.assertFalse(rig.pools.pools)
                    self.assertTrue(await fresh(rig))
                    self.assertNotIn("Vítek", names_shown(rig))
                    ev = kinds(rig, "request.expired")[-1]
                    self.assertEqual((ev["id"], ev["who"], ev["reason"], ev["where"]),
                                     (w.id, "Vítek", "new_day", "tick"))
                    self.assertAlmostEqual(ev["age_s"], 2 * 3600 + 23 * 60 + 10, delta=5)
                    bg = kinds(rig, "request.background_expired")[-1]
                    self.assertEqual((bg["who"], bg["mode"], bg["to"]), ("Vítek", "favourites", "none"))
                    # ráno Hrát = chytrý rozjezd (F-START-01), ne „oblíbené kanceláře"
                    clock.at(MORNING)
                    self.assertEqual((await rig.wq.expire_days())["expired"], 0)
                    await morning_play(rig, {t.id for t in w.tracks})
                    self.assertNotIn("Vítek", names_shown(rig))
                    # přání ráno žádný tah nedostane (Pi 3. 11. 11:23 ano)
                    self.assertEqual(turns_of(w), turns_at_midnight)

        tw.run(go())

    def test_wish_playing_at_midnight_finishes_but_does_not_go_on(self):
        async def go():
            with fake_clock() as clock:
                clock.at(datetime(2026, 11, 2, 23, 50))  # 10 min před půlnocí
                async with tw.Rig() as rig:
                    await rig.background()
                    w = rig.wq.submit("pusť Kabát", "Jana")
                    await rig.until(lambda: w.state == "playing")
                    cur = rig.fake.current_vid()
                    self.assertIn(cur, {t.id for t in w.tracks})
                    self.assertTrue(w.pending())
                    clock.at(MIDNIGHT)
                    out = await rig.wq.expire_days()
                    self.assertEqual(out["expired"], 1)
                    await rig.settle(0.3)
                    # hrající skladba se neutne a ví se, čí je
                    self.assertEqual(rig.fake.current_vid(), cur)
                    self.assertFalse(rig.player._paused)
                    self.assertEqual(w.state, "removed")
                    self.assertEqual(rig.wq.reason_for(cur).get("who"), "Jana")
                    # …ale nic dalšího z přání ve frontě nezůstalo
                    kabat = {t.id for t in tw.KABAT}
                    self.assertFalse(kabat & set(rig.upcoming()))
                    self.assertEqual(kinds(rig, "request.expired")[-1]["current"], cur)
                    # dohraje → hraje to, co by hrálo po Hrát (rozjezd), ne Kabát
                    await rig.until(lambda: rig.wq.bg_reason.get("kind") == "start", 3)
                    self.assertEqual(rig.pools.artist, "")
                    await rig.until(lambda: rig.upcoming(), 3)
                    rig.fake.finish_current()
                    await rig.until(lambda: rig.fake.current_vid() not in (None, cur))
                    nxt = rig.fake.current_vid()
                    self.assertNotIn(nxt, kabat)
                    self.assertNotEqual(rig.wq.reason_for(nxt).get("kind"), "wish")
                    self.assertNotIn("Jana", names_shown(rig))

        tw.run(go())

    def test_same_day_wish_is_untouched(self):
        async def go():
            with fake_clock() as clock:
                clock.at(datetime(2026, 11, 3, 8, 0))
                async with tw.Rig() as rig:
                    await rig.background()
                    w = rig.wq.submit("pusť Kabát", "Jana")
                    await rig.until(lambda: w.state == "playing")
                    before = (list(rig.upcoming()), dict(rig.wq.bg_reason), rig.pools.mood)
                    for t in (datetime(2026, 11, 3, 18, 0), datetime(2026, 11, 3, 23, 59, 50)):
                        clock.at(t)
                        self.assertEqual(await rig.wq.expire_days(),
                                         {"expired": 0, "background": "", "dropped": False})
                    self.assertTrue(w.active)
                    self.assertEqual((list(rig.upcoming()), dict(rig.wq.bg_reason), rig.pools.mood),
                                     before)
                    self.assertFalse(kinds(rig, "request.expired"))

        tw.run(go())

    def test_unsynced_clock_ends_nothing_and_day_follows_age(self):
        """Neseřízené hodiny: den nejde soudit, nic nekončí. Přání vzniklé před
        seřízením dostane čas podle stáří, ne podle špatných hodin."""
        async def go():
            with fake_clock() as clock:
                flag = wishes.CLOCK_SYNC_FLAG
                flag.unlink()
                clock.at(datetime(2026, 11, 2, 18, 0))  # hodiny Pi po zapnutí (poslední vypnutí)
                async with tw.Rig() as rig:
                    await rig.background()
                    w = rig.wq.submit("pusť Kabát", "Jana")
                    await rig.until(lambda: w.state == "playing")
                    clock.at(datetime(2026, 11, 3, 8, 0, 5))  # síť: skutečný čas
                    self.assertEqual((await rig.wq.expire_days())["expired"], 0)  # pořád neseřízené
                    flag.touch()
                    self.assertEqual((await rig.wq.expire_days())["expired"], 0)
                    self.assertTrue(w.active)
                    self.assertEqual(day.day_of(w.created), datetime(2026, 11, 3).date())

        tw.run(go())


    def test_every_background_mode_from_a_wish_ends(self):
        """Režim interpreta, album, „Obohatit" (mix) i oblíbené: po přání
        z minulého dne nic z toho ráno nepokračuje; ani poslední odpověď DJe."""
        modes = {
            "artist": dict(artist="Kabát", mood="Kabát", artist_tracks=tw.KABAT),
            "album": dict(album=True, artist="Load", artist_tracks=tw.OLYMPIC),
            "mix": dict(seeds=tw.KABAT[:2] + tw.OLYMPIC[:2], mood="Kabát + Olympic",
                        mix=[{"label": "Kabát", "seeds": tw.KABAT[:2]},
                             {"label": "Olympic", "seeds": tw.OLYMPIC[:2]}]),
        }

        async def go(mode: str):
            with fake_clock() as clock:
                clock.at(datetime(2026, 11, 2, 18, 30))
                async with tw.Rig() as rig:
                    await rig.background()
                    await rig.wq._set_background(who="Petr", wid="petr0001", **modes[mode])
                    rig.wq.note_last(text="pusť", reply="Hraju, dokud neřekneš jinak.")
                    rig.wq.last["who"] = "Petr"
                    await rig.wq.stop_all()
                    await rig.settle(0.1)
                    clock.at(datetime(2026, 11, 3, 0, 5))
                    await rig.wq.refresh_playing()
                    out = await rig.wq.expire_days()
                    self.assertEqual(out["background"], "none", mode)
                    self.assertEqual((rig.pools.artist, rig.pools.album, rig.wq.mix), ("", "", []))
                    self.assertFalse(rig.pools.pools)
                    self.assertIsNone(rig.wq.last)
                    self.assertTrue(await fresh(rig))
                    self.assertNotIn("Petr", names_shown(rig))
                    self.assertEqual(kinds(rig, "request.background_expired")[-1]["who"], "Petr")
                    rig.wq.save()
                    self.assertNotIn("Petr", str(rig.wq.load_state()["bg"]))

        for mode in modes:
            tw.run(go(mode))

    def test_web_hides_yesterdays_dj_reply(self):
        async def go():
            with fake_clock() as clock:
                clock.at(EVENING)
                async with tw.Rig() as rig:
                    then = time.time()
                    self.assertFalse(rig.wq.from_earlier_day(then))
                    clock.at(MORNING)
                    self.assertTrue(rig.wq.from_earlier_day(then))
                    self.assertFalse(rig.wq.from_earlier_day(time.time() - 60))
                    wishes.CLOCK_SYNC_FLAG.unlink()  # neseřízené hodiny: nejde říct
                    rig.wq._clock_ok = False
                    self.assertFalse(rig.wq.from_earlier_day(then))
        tw.run(go())


class SameRuleAfterRestart(unittest.TestCase):
    def _evening_state(self, clock, save_at: datetime) -> tuple[dict, str]:
        async def go():
            clock.at(EVENING)
            async with tw.Rig() as rig:
                w = await vitek_favourites(rig)
                clock.at(PAUSED)
                await rig.wq.stop_all()
                await rig.settle(0.1)
                clock.at(save_at)
                await rig.wq.refresh_playing()
                rig.wq.save()
                return rig.wq.load_state(), w.id
        return tw.run(go())

    async def _check_clean(self, rig, wid: str, where: str) -> None:
        self.assertEqual(rig.wq.active(), [])
        self.assertEqual(rig.pools.favourites, "")
        self.assertFalse(rig.pools.pools)
        self.assertTrue(await fresh(rig))
        self.assertNotIn("Vítek", names_shown(rig))
        ev = kinds(rig, "request.expired")[-1]
        self.assertEqual((ev["id"], ev["reason"], ev["where"]), (wid, "new_day", where))
        res = kinds(rig, "request.resume")[-1]
        self.assertEqual((res["expired"], res["restore_reason"]), (1, "earlier_day"))

    def test_restart_after_midnight_behaves_like_running_through(self):
        """Restart služby ve 00:30 (stav z 23:55, do 2 h → obnovuje se): přání
        z 21:37 se nevrátí, ani oblíbené kanceláře — jako bez restartu."""
        with fake_clock() as clock:
            saved, wid = self._evening_state(clock, datetime(2026, 11, 2, 23, 55))
            self.assertEqual(saved["bg"]["mode"], "favourites")
            self.assertAlmostEqual(saved["bg"]["since"], EVENING.timestamp(), delta=30)

            async def go():
                clock.at(datetime(2026, 11, 3, 0, 30))
                async with tw.Rig() as rig2:
                    wire_office(rig2)
                    why = await rig2.wq.resume(dict(saved, boot=rig2.wq._boot))
                    await rig2.settle(0.3)
                    self.assertEqual(why, "earlier_day")
                    await self._check_clean(rig2, wid, "restore")
                    clock.at(datetime(2026, 11, 3, 8, 0))
                    await morning_play(rig2, set(saved["bg"]["played"])
                                       | {t["id"] for d in saved["wishes"] for t in d["tracks"]})
            tw.run(go())

    def test_cold_restore_of_a_state_saved_after_midnight(self):
        """Výpadek proudu ve 00:00:03 — stav uložený už 3. 11., ale přání z 2. 11.
        Ráno po zapnutí Pi (stejný den) se přání ani podkres nevrátí."""
        with fake_clock() as clock:
            saved, wid = self._evening_state(clock, datetime(2026, 11, 3, 0, 0, 3))

            async def go():
                clock.at(datetime(2026, 11, 3, 7, 30))
                async with tw.Rig() as rig2:
                    wire_office(rig2)
                    why = await rig2.wq.resume(dict(saved, boot="predchozi-boot"),
                                               now=datetime(2026, 11, 3, 7, 30), synced=True)
                    await rig2.settle(0.3)
                    self.assertEqual(why, "earlier_day")
                    await self._check_clean(rig2, wid, "restore")
            tw.run(go())

    def test_same_day_state_is_restored_as_before(self):
        with fake_clock() as clock:
            saved, wid = self._evening_state(clock, datetime(2026, 11, 2, 21, 50))

            async def go():
                clock.at(datetime(2026, 11, 2, 22, 10))
                async with tw.Rig() as rig2:
                    wire_office(rig2)
                    await rig2.wq.resume(dict(saved, boot=rig2.wq._boot))
                    await rig2.settle(0.2)
                    self.assertEqual([w.id for w in rig2.wq.active()], [wid])
                    self.assertEqual(rig2.pools.favourites, "office")
                    self.assertFalse(kinds(rig2, "request.expired"))
            tw.run(go())


if __name__ == "__main__":
    unittest.main()
