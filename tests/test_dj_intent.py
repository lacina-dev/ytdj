"""DJ: pure logic around what the listener asked for.

    python -m unittest tests.test_dj_intent -v

The cases come from the Pi session of 2026-09-25 (see the report in the
commit): "Hraj davida Stypku" ended up as a single track that never played.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ytdj.agent.intent import (  # noqa: E402
    ListenerIntent,
    artist_request,
    build_intent,
    track_avoided,
    SkipWatch,
    artist_match,
    local_command,
    mentions,
    repair_decision,
)


class Mentions(unittest.TestCase):
    def test_czech_declension(self):
        self.assertTrue(mentions("Hraj davida Stypku", "David Stypka"))
        self.assertTrue(mentions("pusť Kabát", "Kabát"))
        self.assertTrue(mentions("dej něco od kabátu", "Kabát"))
        # every significant word of the name must be there — surname alone is not
        self.assertFalse(mentions("chci slyšet Nohavicu", "Jaromír Nohavica"))

    def test_diacritics_and_spacing(self):
        self.assertTrue(mentions("zahraj deda mladek", "Děda Mládek"))
        self.assertTrue(mentions("pust tribalneed", "TribalNeed"))
        self.assertTrue(mentions("pusť AC/DC", "AC/DC"))

    def test_not_mentioned(self):
        self.assertFalse(mentions("Hraj davida Stypku", "Dobré ráno, milá"))
        self.assertFalse(mentions("něco klidného", "Kabát"))
        self.assertFalse(mentions("pusť U2", "UB40"))


class ArtistMatch(unittest.TestCase):
    def test_featuring_and_lists(self):
        self.assertTrue(artist_match("David Stypka, Bandjeez", "David Stypka"))
        self.assertTrue(artist_match("Ewa Farna & David Stypka", "David Stypka"))
        self.assertTrue(artist_match("Jaromir Nohavica", "Jaromír Nohavica"))

    def test_band_with_a_in_name_stays_whole(self):
        self.assertTrue(artist_match("Mňága a Žďorp", "Mňága a Žďorp"))

    def test_no_prefix_confusion(self):
        # the band Lucie is not the singer Lucie Bílá
        self.assertFalse(artist_match("Lucie Bílá", "Lucie"))
        self.assertTrue(artist_match("Lucie", "Lucie"))
        self.assertFalse(artist_match("Bandjeez", "David Stypka"))
        self.assertFalse(artist_match("Tomas Klus", "David Stypka"))


class Repair(unittest.TestCase):
    def test_pi_2026_09_25_stypka(self):
        # the actual decision the model returned on the Pi
        fix = repair_decision(
            "Hraj davida Stypku",
            "play_next",
            [{"artist": "David Stypka", "title": "Dobré ráno, milá"}],
            [],
        )
        self.assertEqual(fix.action, "start_radio")
        self.assertEqual(fix.focus_artists, ["David Stypka"])

    def test_surname_only(self):
        fix = repair_decision(
            "pusť Nohavicu", "play_next",
            [{"artist": "Jaromír Nohavica", "title": "Kometa"}], [],
        )
        self.assertEqual(fix.focus_artists, ["Jaromír Nohavica"])

    def test_named_track_stays_single(self):
        fix = repair_decision(
            "pusť Wonderwall", "play_next",
            [{"artist": "Oasis", "title": "Wonderwall"}], [],
        )
        self.assertEqual((fix.action, fix.focus_artists), ("play_next", []))

    def test_track_and_artist_named(self):
        fix = repair_decision(
            "pusť Wonderwall od Oasis", "play_next",
            [{"artist": "Oasis", "title": "Wonderwall"}], [],
        )
        self.assertEqual(fix.action, "play_next")

    def test_explicit_single(self):
        fix = repair_decision(
            "zahraj jednu od Chinaski", "play_next",
            [{"artist": "Chinaski", "title": "Každý ráno"}], [],
        )
        self.assertEqual(fix.action, "play_next")

    def test_like_is_a_mood(self):
        fix = repair_decision(
            "něco jako Nirvana", "play_next",
            [{"artist": "Nirvana", "title": "Come As You Are"}], [],
        )
        self.assertEqual(fix.action, "play_next")
        self.assertEqual(fix.focus_artists, [])

    def test_focus_forces_start_radio(self):
        fix = repair_decision("hraj Kabát", "nothing", [], ["Kabát"])
        self.assertEqual(fix.action, "start_radio")
        fix = repair_decision("hraj Kabát", "play_next", [], ["Kabát"])
        self.assertEqual(fix.action, "start_radio")

    def test_mood_untouched(self):
        fix = repair_decision("něco klidného", "start_radio", [], [])
        self.assertEqual((fix.action, fix.focus_artists, fix.note), ("start_radio", [], ""))


class Intent(unittest.TestCase):
    def test_roundtrip_and_ttl(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "intent.json"
            ListenerIntent("hraj Stypku", 1000.0, ["David Stypka"], "česky").save(path)
            got = ListenerIntent.load(path)
            self.assertEqual(got.focus_artists, ["David Stypka"])
            self.assertIn("hraj Stypku", got.describe(now=1000.0 + 300))
            self.assertIn("před 5 min", got.describe(now=1000.0 + 300))
            self.assertEqual(got.describe(now=1000.0 + 13 * 3600), "")

    def test_missing_or_broken_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "intent.json"
            self.assertEqual(ListenerIntent.load(path).describe(), "")
            path.write_text("{nope")
            self.assertEqual(ListenerIntent.load(path).text, "")


class LocalCommands(unittest.TestCase):
    def test_commands(self):
        self.assertEqual(local_command("další"), ("skip", 0))
        self.assertEqual(local_command("Hlasitěji"), ("louder", 0))
        self.assertEqual(local_command("louder"), ("louder", 0))
        self.assertEqual(local_command("tišeji"), ("quieter", 0))
        self.assertEqual(local_command("hlasitost 70"), ("volume", 70))
        self.assertEqual(local_command("pauza"), ("pause", 0))
        self.assertEqual(local_command("stop"), ("stop", 0))

    def test_wishes_go_to_the_model(self):
        for text in ("pusť něco hlasitějšího", "hraj Kabát", "další od Kabátu",
                     "něco jiného", "hraj"):
            self.assertIsNone(local_command(text), text)


class Skips(unittest.TestCase):
    def test_three_skips_trigger(self):
        w = SkipWatch()
        for i, t in enumerate((10, 20, 30)):
            w.skipped(f"t{i}", t)
        self.assertEqual(w.due(31), ["t2", "t1", "t0"])

    def test_no_trigger_after_restart(self):
        # a fresh process knows nothing about skips before the restart
        self.assertIsNone(SkipWatch().due(5))

    def test_window_slide_does_not_trigger(self):
        # Pi 20:06:15 — the count dropped, nothing was skipped, yet it reseeded
        w = SkipWatch(window=600)
        for t in (0, 60, 120, 180):
            w.skipped("x", t)
        w.turn(200, by_user=False)  # the reseed that followed
        self.assertIsNone(w.due(700))
        self.assertIsNone(w.due(900))

    def test_quiet_after_user_request(self):
        w = SkipWatch(quiet_after_user=180)
        w.turn(1000, by_user=True)
        for t in (1010, 1020, 1030):
            w.skipped("x", t)
        self.assertIsNone(w.due(1040))
        self.assertIsNotNone(w.due(1181))

    def test_skips_before_turn_dont_count(self):
        w = SkipWatch()
        w.skipped("a", 1)
        w.skipped("b", 2)
        w.turn(3, by_user=False)
        w.skipped("c", 4)
        self.assertIsNone(w.due(5))


def _d(**kw):
    base = {"action": "start_radio", "seeds": [], "requested": [], "focus_artists": [],
            "after_current": False, "avoid": [], "mood": "", "volume": 0,
            "remember": "", "reply": "model reply"}
    base.update(kw)
    return base


def _seeds(artist, *titles):
    return [{"artist": artist, "title": t} for t in titles]


class HowIsItMeant(unittest.TestCase):
    """Czech phrasings → kind of wish, even when the model misreads it."""

    def kind(self, text, **kw):
        i = build_intent(text, _d(**kw))
        return i.kind, i.artists

    def test_artist_phrasings(self):
        cases = [
            ("Hraj pisnicky od midi lidi", "Midi Lidi"),
            ("chci slyšet Tata Bojs", "Tata Bojs"),
            ("něco od Kabátu", "Kabát"),
            ("víc od Kabátu", "Kabát"),
            ("ještě od Kabátu prosím", "Kabát"),
            ("pusť Stypku", "David Stypka"),
            ("dej mi Midi Lidi", "Midi Lidi"),
        ]
        for text, artist in cases:
            # the model's typical miss: a radio from that artist's songs, no focus
            got = self.kind(text, seeds=_seeds(artist, "a", "b", "c"))
            self.assertEqual(got, ("artist", [artist]), text)

    def test_several_artists(self):
        got = self.kind("hraj Kabát a Škwor",
                        seeds=_seeds("Kabát", "Pohoda") + _seeds("Škwor", "Síla"))
        self.assertEqual(got, ("artist", ["Kabát", "Škwor"]))

    def test_artist_with_exception(self):
        i = build_intent("Kabát, ale ne Pohodu", _d(
            seeds=_seeds("Kabát", "Malá dáma"),
            avoid=[{"artist": "Kabát", "title": "Pohoda"}]))
        self.assertEqual((i.kind, i.artists), ("artist", ["Kabát"]))
        self.assertEqual(i.exclude, [("Kabát", "Pohoda")])

    def test_song_is_a_song(self):
        i = build_intent("pusť Jasnou zprávu od Olympicu", _d(
            requested=[{"artist": "Olympic", "title": "Jasná zpráva"}],
            seeds=_seeds("Olympic", "Dávno")))
        self.assertEqual(i.kind, "song")
        self.assertEqual(i.reply, "model reply")  # nothing repaired, reply kept

    def test_one_from_artist(self):
        i = build_intent("zahraj jednu od Chinaski", _d(
            action="play_next", requested=[{"artist": "Chinaski", "title": "Každý ráno"}]))
        self.assertEqual(i.kind, "songs")

    def test_moods_stay_moods(self):
        for text, seeds in [
            ("něco jako Nirvana", _seeds("Nirvana", "Lithium") + _seeds("Pixies", "Debaser")),
            ("něco jako Nirvana", _seeds("Nirvana", "Lithium", "Come As You Are")),
            ("český rap z devadesátek", _seeds("PSH", "Praha") + _seeds("Chaozz", "Zkus mě")),
            ("něco klidného od Kabátu", _seeds("Kabát", "Pohoda")),  # the model decides
            ("rock, třeba Kabát", _seeds("Kabát", "Pohoda")),
        ]:
            self.assertEqual(build_intent(text, _d(seeds=seeds)).kind, "mood", text)

    def test_model_focus_is_kept(self):
        i = build_intent("pusť něco klidného od Kabátu", _d(focus_artists=["Kabát"]))
        self.assertEqual(i.kind, "artist")

    def test_auto_turns_are_not_repaired(self):
        i = build_intent("Hraj pisnicky od midi lidi", _d(seeds=_seeds("Midi Lidi", "a")),
                         auto=True)
        self.assertEqual(i.kind, "mood")

    def test_more_like_this(self):
        for text in ("víc takového", "ještě něco podobného", "v tomhle duchu dál"):
            self.assertTrue(build_intent(text, _d(seeds=_seeds("X", "y"))).more_like_current, text)
        self.assertFalse(build_intent("něco jiného", _d(seeds=_seeds("X", "y"))).more_like_current)

    def test_controls(self):
        i = build_intent("ztlum to na 40", _d(action="volume", volume=40))
        self.assertEqual((i.kind, i.control, i.volume), ("control", "volume", 40))

    def test_artist_request_needs_nothing_else(self):
        self.assertEqual(artist_request("pusť Wonderwall", ["Oasis"]), [])
        self.assertEqual(artist_request("pusť Lucii", ["Lucie Bílá", "Lucie"]), ["Lucie"])
        self.assertEqual(artist_request("pusť Lucii Bílou", ["Lucie", "Lucie Bílá"]), ["Lucie Bílá"])


class Avoid(unittest.TestCase):
    def test_title_and_artist(self):
        self.assertTrue(track_avoided("Kabát", "Pohoda", [("Kabát", "Pohoda")]))
        self.assertTrue(track_avoided("Kabát", "Pohoda (Live)", [("Kabát", "Pohoda")]))
        self.assertFalse(track_avoided("Kabát", "Malá dáma", [("Kabát", "Pohoda")]))
        self.assertTrue(track_avoided("Olympic", "Dávno", [("Olympic", "")]))
        self.assertFalse(track_avoided("Kabát", "Pohoda", [("Olympic", "")]))


if __name__ == "__main__":
    unittest.main()
