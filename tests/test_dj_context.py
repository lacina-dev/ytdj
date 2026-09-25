"""DJ: rozjezd bez zadání podle času, dne a historie kanceláře.

    python -m unittest tests.test_dj_context -v

Žádná síť; state.db je dočasná SQLite postavená přes ytdj.state.Store.
Časy mají pevnou zónu (+01:00 / +02:00), takže nezáleží na TZ stroje.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_tmp = tempfile.TemporaryDirectory()
EVENTS = Path(_tmp.name) / "events.jsonl"
os.environ["YTDJ_EVENTS_FILE"] = str(EVENTS)

from ytdj.agent.context import (  # noqa: E402
    czech_holiday,
    easter_sunday,
    is_working_day,
    part_of_day,
    slot_ranges,
    start_context,
    start_instruction,
)
from ytdj.state import Store  # noqa: E402

SUMMER = timezone(timedelta(hours=2))
WINTER = timezone(timedelta(hours=1))

MON_0830 = datetime(2026, 9, 21, 8, 30, tzinfo=SUMMER)
WED_1315 = datetime(2026, 9, 23, 13, 15, tzinfo=SUMMER)
FRI_1600 = datetime(2026, 9, 25, 16, 0, tzinfo=SUMMER)
SAT_2000 = datetime(2026, 9, 26, 20, 0, tzinfo=SUMMER)


class _DB:
    """Dočasná state.db a pár zkratek pro vkládání historie s daným časem."""

    def __init__(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.dir.name) / "state.db")
        # každý INSERT je v autocommitu vlastní transakce; bez fsync je test rychlý
        self.store.db.execute("PRAGMA synchronous=OFF")
        self.store.db.execute("PRAGMA journal_mode=MEMORY")

    def play(self, when: datetime, artist: str, outcome: str, mood: str = "",
             vid: str | None = None, title: str = "t") -> None:
        vid = vid or f"{artist}-{when.isoformat()}"
        self.store.db.execute(
            "INSERT INTO plays(video_id,title,artist,ts,seed_id,outcome) VALUES(?,?,?,?,?,?)",
            (vid, title, artist, when.timestamp(), mood or None, outcome),
        )

    def seed(self, when: datetime, mood: str) -> None:
        self.store.db.execute("INSERT INTO seeds(video_id,mood,ts) VALUES('x',?,?)",
                              (mood, when.timestamp()))

    def request(self, when: datetime, artist: str) -> None:
        self.store.db.execute(
            "INSERT INTO requests(video_id,title,artist,ts) VALUES('r','t',?,?)",
            (artist, when.timestamp()))

    def feedback(self, when: datetime, vid: str, rating: str) -> None:
        self.store.db.execute("INSERT INTO feedback(video_id,rating,ts) VALUES(?,?,?)",
                              (vid, rating, when.timestamp()))

    def close(self) -> None:
        self.store.close()
        self.dir.cleanup()


def office_history(db: _DB, now: datetime) -> None:
    """Tři týdny zpátky: ráno se dohrával Chinaski a Norah Jones, Kabát se
    přeskakoval, večer hrálo něco jiného, o víkendu metal."""
    for weeks in (1, 2, 3):
        mon = now - timedelta(weeks=weeks)
        for i, artist in enumerate(["Chinaski", "Norah Jones", "Chinaski"]):
            db.play(mon + timedelta(minutes=5 * i), artist, "finished", "klidný ranní pop")
        db.play(mon + timedelta(minutes=20), "Kabát", "skipped", "klidný ranní pop")
        db.play(mon + timedelta(minutes=25), "Kabát", "skipped", "klidný ranní pop")
        # jiná doba — nesmí se započítat do rána
        db.play(mon.replace(hour=19), "Daft Punk", "finished", "večerní disco")
        # víkend — do pracovního dne nepatří
        db.play(mon - timedelta(days=2), "Metallica", "finished", "metal")


class Calendar(unittest.TestCase):
    def test_easter(self):
        self.assertEqual(easter_sunday(2024), date(2024, 3, 31))
        self.assertEqual(easter_sunday(2025), date(2025, 4, 20))
        self.assertEqual(easter_sunday(2026), date(2026, 4, 5))
        self.assertEqual(easter_sunday(2027), date(2027, 3, 28))

    def test_czech_holidays(self):
        self.assertEqual(czech_holiday(date(2026, 4, 6)), "Velikonoční pondělí")
        self.assertEqual(czech_holiday(date(2026, 4, 3)), "Velký pátek")
        self.assertEqual(czech_holiday(date(2026, 12, 24)), "Štědrý den")
        self.assertEqual(czech_holiday(date(2026, 9, 28)), "Den české státnosti")
        self.assertEqual(czech_holiday(date(2026, 11, 17)), "Den boje za svobodu a demokracii")
        self.assertIsNone(czech_holiday(date(2026, 9, 25)))
        self.assertIsNone(czech_holiday(date(2015, 4, 3)))  # Velký pátek až od 2016

    def test_working_day(self):
        self.assertTrue(is_working_day(date(2026, 9, 25)))
        self.assertFalse(is_working_day(date(2026, 9, 26)))  # sobota
        self.assertFalse(is_working_day(date(2026, 4, 6)))

    def test_part_of_day(self):
        cases = {0: "noc", 4 * 60 + 59: "noc", 5 * 60: "ráno", 8 * 60 + 30: "ráno",
                 9 * 60: "dopoledne", 12 * 60: "poledne", 13 * 60 + 15: "odpoledne",
                 16 * 60: "pozdní odpoledne", 20 * 60: "večer", 22 * 60: "noc"}
        for minute, label in cases.items():
            self.assertEqual(part_of_day(minute), label, minute)


class Situations(unittest.TestCase):
    """Bez historie: jen čas a den."""

    def ctx(self, now, cfg=None):
        return start_context(None, now, cfg)

    def test_monday_morning(self):
        c = self.ctx(MON_0830)
        self.assertEqual((c.part_of_day, c.day_type, c.rule.name), ("ráno", "pracovní den",
                                                                     "monday_morning"))
        self.assertEqual(c.rule.energy, 2)
        text = start_instruction(c)
        self.assertIn("pondělí 21. 9. 8:30", text)
        self.assertIn("pondělní ráno", text)

    def test_wednesday_after_lunch(self):
        c = self.ctx(WED_1315)
        self.assertEqual((c.part_of_day, c.rule.name, c.rule.energy),
                         ("odpoledne", "after_lunch", 4))

    def test_friday_afternoon_is_liveliest(self):
        c = self.ctx(FRI_1600)
        self.assertEqual((c.part_of_day, c.rule.name), ("pozdní odpoledne", "friday_afternoon"))
        self.assertEqual(c.rule.energy, 5)
        self.assertGreater(c.rule.energy, self.ctx(WED_1315).rule.energy - 0)
        self.assertGreater(c.rule.energy, self.ctx(MON_0830).rule.energy)

    def test_weekend_evening(self):
        c = self.ctx(SAT_2000)
        self.assertEqual((c.part_of_day, c.day_type, c.rule.name, c.working_day),
                         ("večer", "víkend", "off_evening", False))
        self.assertIn("sobota 26. 9. 20:00, večer; víkend.", start_instruction(c))

    def test_christmas_eve(self):
        c = self.ctx(datetime(2026, 12, 24, 10, 0, tzinfo=WINTER))  # čtvrtek
        self.assertEqual((c.day_type, c.holiday, c.rule.name), ("svátek", "Štědrý den", "off_day"))
        self.assertIn("svátek (Štědrý den)", start_instruction(c))

    def test_easter_monday_is_not_a_monday_morning(self):
        c = self.ctx(datetime(2026, 4, 6, 8, 30, tzinfo=SUMMER))
        self.assertEqual((c.day_type, c.holiday), ("svátek", "Velikonoční pondělí"))
        self.assertEqual(c.rule.name, "off_day")
        self.assertNotIn("pondělní ráno", start_instruction(c))

    def test_empty_history(self):
        text = start_instruction(self.ctx(FRI_1600))
        self.assertTrue(text.startswith("Nic nehraje a nikdo si nic nevyžádal"))
        self.assertIn("zatím nemám", text)
        self.assertIn("start_radio", text)
        self.assertIn("v práci", text)
        self.assertNotIn("Přeskakovalo", text)
        self.assertLess(len(text), 800)

    def test_office_off(self):
        text = start_instruction(self.ctx(FRI_1600, SimpleNamespace(office=False)))
        self.assertNotIn("v práci", text)

    def test_naive_now_is_local(self):
        c = start_context(None, datetime(2026, 9, 25, 16, 0))
        self.assertEqual((c.now.hour, c.rule.name), (16, "friday_afternoon"))

    def test_deterministic(self):
        self.assertEqual(start_instruction(self.ctx(MON_0830)),
                         start_instruction(self.ctx(MON_0830)))


class History(unittest.TestCase):
    def setUp(self):
        self.db = _DB()

    def tearDown(self):
        self.db.close()

    def test_empty_db(self):
        c = start_context(self.db.store, MON_0830)
        self.assertEqual(c.history.n_plays, 0)
        self.assertIn("zatím nemám", start_instruction(c))

    def test_slot_stats(self):
        office_history(self.db, MON_0830)
        c = start_context(self.db.store, MON_0830)
        h = c.history
        self.assertEqual(h.worked_artists, ["Chinaski", "Norah Jones"])
        self.assertEqual(h.skipped_artists, ["Kabát"])
        self.assertNotIn("Daft Punk", h.worked_artists)  # jiná doba
        self.assertNotIn("Metallica", h.worked_artists)  # víkend
        self.assertEqual(h.n_plays, 15)
        self.assertEqual(h.n_days, 3)
        # nálada: 9 dohraných, 6 přeskočených = 40 % → ani osvědčená, ani odmítnutá
        self.assertEqual(h.worked_moods, [])
        text = start_instruction(c)
        self.assertIn("Chinaski, Norah Jones", text)
        self.assertIn("Přeskakovalo se: Kabát.", text)
        self.assertLess(len(text), 800)

    def test_one_session_is_not_everyone(self):
        # dvě dohrané od jednoho interpreta v jediném dni nestačí
        day = MON_0830 - timedelta(days=7)
        self.db.play(day, "Lucie", "finished")
        self.db.play(day + timedelta(minutes=4), "Lucie", "finished")
        self.assertEqual(start_context(self.db.store, MON_0830).history.worked_artists, [])
        self.db.play(day - timedelta(days=7), "Lucie", "finished")
        self.assertEqual(start_context(self.db.store, MON_0830).history.worked_artists, ["Lucie"])

    def test_mood_that_worked(self):
        for d in (7, 14):
            for i in range(3):
                self.db.play(WED_1315 - timedelta(days=d, minutes=4 * i), f"A{i}", "finished",
                             "svižný pop po obědě")
        h = start_context(self.db.store, WED_1315).history
        self.assertEqual(h.worked_moods, ["svižný pop po obědě"])

    def test_video_id_in_seed_column_is_not_a_mood(self):
        for d in (7, 14):
            for i in range(3):
                self.db.play(WED_1315 - timedelta(days=d, minutes=4 * i), "X", "finished",
                             "dQw4w9WgXcQ")
        self.assertEqual(start_context(self.db.store, WED_1315).history.worked_moods, [])

    def test_overplayed_excluded_from_worked(self):
        office_history(self.db, MON_0830)
        for i in range(8):  # poslední dny pořád dokola
            self.db.play(MON_0830 - timedelta(days=1 + i % 3, hours=i), "Chinaski", "finished")
        h = start_context(self.db.store, MON_0830).history
        self.assertEqual(h.overplayed, ["Chinaski"])
        self.assertNotIn("Chinaski", h.worked_artists)
        self.assertIn("hrálo až moc: Chinaski — vynech je", start_instruction(
            start_context(self.db.store, MON_0830)))

    def test_dislike_and_requests(self):
        day1, day2 = FRI_1600 - timedelta(days=7), FRI_1600 - timedelta(days=14)
        self.db.play(day1, "Tata Bojs", "finished", vid="tb1")
        self.db.feedback(day1, "tb1", "dislike")
        self.db.play(day2, "Tata Bojs", "finished", vid="tb2")
        self.db.feedback(day2, "tb2", "dislike")
        self.db.request(day1, "Mig 21")
        self.db.request(day2, "Mig 21")
        h = start_context(self.db.store, FRI_1600).history
        self.assertEqual(h.worked_artists, ["Mig 21"])
        self.assertEqual(h.skipped_artists, ["Tata Bojs"])

    def test_same_weekday_weighs_more(self):
        # Po a Út ráno — dohráno; na pondělí vede pondělní interpret
        for w in (1, 2):
            self.db.play(MON_0830 - timedelta(weeks=w), "Pondělní", "finished")
            self.db.play(MON_0830 - timedelta(weeks=w) + timedelta(days=1), "Úterní", "finished")
        h = start_context(self.db.store, MON_0830).history
        self.assertEqual(h.worked_artists, ["Pondělní", "Úterní"])

    def test_last_mood(self):
        self.db.seed(MON_0830 - timedelta(hours=3), "klidná elektronika")
        self.db.seed(MON_0830 - timedelta(days=30), "stará nálada")
        h = start_context(self.db.store, MON_0830).history
        self.assertEqual((h.last_mood, h.last_mood_age_h), ("klidná elektronika", 3.0))
        self.assertIn("Naposledy (před 3 h) hrálo „klidná elektronika“",
                      start_instruction(start_context(self.db.store, MON_0830)))
        self.db.seed(MON_0830 + timedelta(hours=1), "budoucí")  # po `now` se nepočítá
        self.assertEqual(start_context(self.db.store, MON_0830).history.last_mood,
                         "klidná elektronika")

    def test_stale_last_mood_ignored(self):
        self.db.seed(MON_0830 - timedelta(days=30), "stará nálada")
        self.assertIsNone(start_context(self.db.store, MON_0830).history.last_mood)

    def test_length_cap_with_long_names(self):
        long = "Velmi Dlouhé Jméno Kapely Která Nemá Konce A Ještě Něco"
        for d in (7, 14, 21):
            for i in range(6):
                self.db.play(FRI_1600 - timedelta(days=d, minutes=3 * i), f"{long} {i}",
                             "finished", f"{long} nálada {i % 2}")
            for i in range(3):
                self.db.play(FRI_1600 - timedelta(days=d, minutes=40 + i), f"{long} S{i}",
                             "skipped", f"{long} špatná")
        for i in range(12):
            for k in range(3):
                self.db.play(FRI_1600 - timedelta(days=1, hours=5, minutes=i * 4 + k),
                             f"{long} O{k}", "finished")
        self.db.seed(FRI_1600 - timedelta(hours=2), long * 3)
        text = start_instruction(start_context(self.db.store, FRI_1600))
        self.assertLessEqual(len(text), 800, text)
        self.assertIn("Přeskakovalo se", text)
        self.assertIn("…", text)

    def test_broken_store_degrades(self):
        class Broken:
            def __getattr__(self, name):
                raise RuntimeError("db gone")
        with self.assertLogs("ytdj.agent.context", "ERROR"):
            c = start_context(Broken(), MON_0830)
        self.assertEqual(c.history.n_plays, 0)
        self.assertIn("zatím nemám", start_instruction(c))

    def test_fast_enough(self):
        artists = [f"Interpret {i}" for i in range(80)]
        rows = []
        for i in range(5000):
            when = MON_0830 - timedelta(minutes=13 * i)
            rows.append((f"v{i}", "t", artists[i % 80], when.timestamp(), f"nálada {i % 7}",
                         "skipped" if i % 5 == 0 else "finished"))
        self.db.store.db.executemany(
            "INSERT INTO plays(video_id,title,artist,ts,seed_id,outcome) VALUES(?,?,?,?,?,?)",
            rows)
        t0 = time.perf_counter()
        c = start_context(self.db.store, MON_0830)
        took = time.perf_counter() - t0
        self.assertGreater(c.history.n_plays, 100)
        self.assertLess(took, 0.2)  # Pi 3 je ~5× pomalejší; cíl tam < 50 ms na dnešní objemy


class Telemetry(unittest.TestCase):
    def test_event(self):
        db = _DB()
        try:
            office_history(db, MON_0830)
            with mock.patch("ytdj.agent.context.telemetry.event") as ev:
                start_context(db.store, MON_0830)
        finally:
            db.close()
        ev.assert_called_once()
        kind, fields = ev.call_args.args[0], ev.call_args.kwargs
        self.assertEqual(kind, "dj.start_context")
        self.assertEqual(fields["part_of_day"], "ráno")
        self.assertEqual(fields["day_type"], "pracovní den")
        self.assertEqual(fields["rule"], "monday_morning")
        self.assertEqual(fields["n_plays"], 15)
        self.assertEqual(fields["worked_artists"], ["Chinaski", "Norah Jones"])
        self.assertIn("took_ms", fields)
        json.dumps(fields, default=str)  # musí jít zapsat


class Ranges(unittest.TestCase):
    def test_same_kind_of_day_only(self):
        rs = slot_ranges(MON_0830)
        self.assertTrue(all(is_working_day(r.day) for r in rs))
        self.assertEqual(rs[-1].end, MON_0830.timestamp())  # dnešek končí teď
        self.assertEqual({r.weight for r in rs if r.day.weekday() == 0}, {2.0})
        sat = slot_ranges(SAT_2000)
        self.assertTrue(all(not is_working_day(r.day) for r in sat))

    def test_dst(self):
        try:
            from zoneinfo import ZoneInfo
            prague = ZoneInfo("Europe/Prague")
        except Exception:
            self.skipTest("bez tzdata")
        now = datetime(2026, 10, 26, 8, 30, tzinfo=prague)  # první pondělí zimního času
        before = datetime(2026, 10, 19, 8, 30, tzinfo=prague)  # ještě letní čas
        r = [r for r in slot_ranges(now) if r.day == before.date()][0]
        self.assertEqual(r.start, before.timestamp() - 90 * 60)
        db = _DB()
        try:
            for w in (1, 2):
                db.play(now - timedelta(weeks=w), "Vltava", "finished")
            self.assertEqual(start_context(db.store, now).history.worked_artists, ["Vltava"])
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
