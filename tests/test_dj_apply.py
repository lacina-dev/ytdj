"""DJ: interpret → resolve → play against a fake catalog and player — no network.

    python -m unittest tests.test_dj_apply -v

Replays what went wrong on the Pi on 2026-09-25 and checks it no longer does.
The model outputs below are the literal decisions from the Pi's Codex sessions.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ytdj-test-")
os.environ["XDG_DATA_HOME"] = _TMP  # before ytdj.config is imported
os.environ["XDG_CONFIG_HOME"] = _TMP
os.environ["YTDJ_EVENTS_FILE"] = str(Path(_TMP) / "events.jsonl")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ytdj.agent.codex import CodexDJ, interleave  # noqa: E402
from ytdj.agent.intent import ListenerIntent, build_intent  # noqa: E402
from ytdj.config import DEFAULTS, Config  # noqa: E402
from ytdj.music.catalog import Track  # noqa: E402
from ytdj.music.radio import RadioPools  # noqa: E402
from ytdj.player.base import PlayerStatus  # noqa: E402
from ytdj.state import Store  # noqa: E402


def T(i: str, artist: str, title: str | None = None) -> Track:
    return Track(id=i, title=title or f"song {i}", artist=artist, duration=200)


STYPKA = [T(f"s{i}", "David Stypka" if i % 3 else "David Stypka, Bandjeez") for i in range(8)]
STYPKA[1] = T("s1", "David Stypka", "Dobré ráno, milá (feat. Ewa Farna)")
MIDI = [T(f"m{i}", "Midi Lidi") for i in range(12)]
KABAT = [T(f"k{i}", "Kabát", t) for i, t in enumerate(
    ["Pohoda", "Malá dáma", "Dole v dole", "Colorado", "Starej bar", "Burlaci"])]
SKWOR = [T(f"w{i}", "Škwor") for i in range(4)]
OTHERS = [T(f"o{i}", f"Other {i}") for i in range(40)]
ARTISTS = {"david stypka": STYPKA, "midi lidi": MIDI, "kabát": KABAT, "škwor": SKWOR}


class FakeCatalog:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def search_song(self, artist: str, title: str):
        self.calls.append(("search_song", artist, title))
        if artist.lower().startswith("david stypka"):
            return STYPKA[1]
        if artist == "Nobody":
            return None
        return T(f"x-{artist}-{title}", artist, title)

    async def artist_tracks(self, name: str, limit: int = 50):
        self.calls.append(("artist_tracks", name))
        return list(ARTISTS.get(name.lower(), []))[:limit]

    async def find_artist_tracks(self, name: str, limit: int = 10):
        return await self.artist_tracks(name, limit)

    async def search(self, query: str, limit: int = 8):
        return OTHERS[:3]

    async def radio(self, video_id: str, limit: int = 50):
        # radio from anything drifts to other artists — that's the point
        self.calls.append(("radio", video_id))
        return OTHERS[:30]


async def _fallback_song(artist, title):
    return T("md", "Kabát", "Malá dáma")


class FakePlayer:
    def __init__(self, current: Track | None = None, queue: list[Track] | None = None):
        self.current = current
        self.queue: list[Track] = list(queue or [])
        self.log: list[tuple] = []

    async def status(self) -> PlayerStatus:
        return PlayerStatus(
            playing=self.current is not None, paused=False, current=self.current,
            position=0.0, duration=0.0, queue=list(self.queue), volume=50,
        )

    async def enqueue(self, tracks):
        self.queue += tracks
        self.log.append(("enqueue", [t.id for t in tracks]))
        return len(tracks)

    async def enqueue_next(self, tracks):
        self.queue[0:0] = tracks
        self.log.append(("enqueue_next", [t.id for t in tracks]))
        return len(tracks)

    async def clear_queue(self):
        self.queue = []
        self.log.append(("clear",))

    async def skip(self, by_user: bool = True):
        self.log.append(("skip", by_user))
        if self.queue:
            self.current = self.queue.pop(0)

    async def toggle_pause(self, paused=None):
        self.log.append(("pause", paused))

    async def set_volume(self, v):
        self.log.append(("volume", v))

    @property
    def queue_depth(self):
        return len(self.queue)


def make(current=None, queue=None):
    cfg = Config(**DEFAULTS)
    store = Store(Path(tempfile.mkdtemp(dir=_TMP)) / "state.db")
    catalog = FakeCatalog()
    pools = RadioPools(catalog, store, cfg)
    player = FakePlayer(current, queue)
    dj = CodexDJ(cfg, catalog, pools, player, store)
    dj._wish_file = Path(tempfile.mkdtemp(dir=_TMP)) / "intent.json"
    dj.wish = ListenerIntent()
    return dj, player, store


def decision(**kw) -> dict:
    base = {"action": "nothing", "seeds": [], "requested": [], "focus_artists": [],
            "after_current": False, "avoid": [], "mood": "", "volume": 0,
            "remember": "", "reply": ""}
    base.update(kw)
    return base


def run(coro):
    return asyncio.run(coro)


async def go(dj, text, data, auto=False, interrupt=None):
    """What CodexDJ.turn does after the model answered."""
    intent = build_intent(text, data, auto=auto)
    plan = await dj.resolve(intent)
    reply = await dj.play(plan, interrupt=not auto if interrupt is None else interrupt)
    if not plan.failed:
        dj._remember_wish(intent)
    return intent, reply


async def listen(dj, player, n):
    """Simulate the queue filler + playback for n tracks; returns what played."""
    played = [player.current] if player.current else []
    for _ in range(n):
        if len(player.queue) < 3:
            await player.enqueue(await dj.next_tracks(5 - len(player.queue)))
        if not player.queue:
            break
        player.current = player.queue.pop(0)
        dj.note_started(player.current.id)
        played.append(player.current)
    return played


class PiReplays(unittest.TestCase):
    """The literal model outputs from the Pi, 2026-09-25."""

    def test_2004_hraj_davida_stypku(self):
        # model: play_next of ONE track; the old radio kept playing
        dj, player, _ = make(current=T("lb", "Lucie Bílá", "Láska je láska"))
        raw = json.loads(
            '{"action":"play_next","seeds":[],"requested":[{"artist":"David Stypka",'
            '"title":"Dobré ráno, milá"}],"mood":"stávající nálada","volume":0,'
            '"remember":"","reply":"Zařazuji Davida Stypku — Dobré ráno, milá — hned '
            'za právě hrající skladbu."}')
        intent, reply = run(go(dj, "Hraj davida Stypku", raw))
        self.assertEqual((intent.kind, intent.artists), ("artist", ["David Stypka"]))
        self.assertIn(("skip", False), player.log)  # starts now, not after 7 minutes
        self.assertEqual(player.current.id, STYPKA[1].id)  # the named one first
        played = run(listen(dj, player, 20))
        self.assertTrue(all("David Stypka" in t.artist for t in played), played)
        self.assertNotIn("Zařazuji", reply)  # the model's reply described the wrong thing
        self.assertIn("dokud neřekneš jinak", reply)

    def test_2023_hraj_pisnicky_od_midi_lidi(self):
        # model: start_radio from 4 Midi Lidi seeds; one track later it was elsewhere
        dj, player, _ = make(current=T("ef", "Ewa Farna, Mirai"))
        raw = decision(action="start_radio", mood="česká alternativní elektronika",
                       seeds=[{"artist": "Midi Lidi", "title": t} for t in
                              ("Protektor", "Hastrmanská", "Když je zima", "Československá")])
        intent, _ = run(go(dj, "Hraj pisnicky od midi lidi", raw))
        self.assertEqual(intent.kind, "artist")
        played = run(listen(dj, player, 30))[1:]
        self.assertGreaterEqual(len(played), 30)
        self.assertTrue(all(t.artist == "Midi Lidi" for t in played), played)

    def test_2006_reseed_does_not_wipe_request(self):
        # the auto reseed's clear_queue removed the requested Stypka track
        dj, player, _ = make(current=T("lb", "Lucie Bílá"))
        run(go(dj, "zařaď Stypku Dobré ráno milá", decision(
            action="play_next", after_current=True,
            requested=[{"artist": "David Stypka", "title": "Dobré ráno, milá"}])))
        self.assertEqual(player.queue[0].id, STYPKA[1].id)
        run(go(dj, "auto", decision(
            action="start_radio", seeds=[{"artist": "Other 2", "title": "x"}],
            remember="Uživateli nesedí opera."), auto=True))
        self.assertEqual(player.queue[0].id, STYPKA[1].id)
        self.assertNotIn(("skip", False), player.log)  # auto turn doesn't cut the song

    def test_2302_wrong_song_is_not_claimed(self):
        # model: play_next "Kabát — Z nouze ctnost" (no such song); catalog
        # answered with Kabát — Malá dáma and the reply claimed the asked title
        dj, player, _ = make(current=T("c", "Kabát", "Na sever"))
        dj.catalog.search_song = _fallback_song  # "at least something by him"
        _, reply = run(go(dj, "Hraj z nouze cnost", decision(
            action="play_next", reply="Pouštím „Z nouze ctnost“ od Kabátu.",
            requested=[{"artist": "Kabát", "title": "Z nouze ctnost"}])))
        self.assertNotIn("Pouštím", reply)
        self.assertIn("Nenašel jsem", reply)
        self.assertIn("Malá dáma", reply)  # says what the catalog offered instead
        self.assertEqual(player.current.id, "c")  # and doesn't play it
        self.assertNotIn(("skip", False), player.log)

    def _vagner(self):
        vagner = T("hv", "Karel Vágner se svým orchestrem, Stanislav Hložek, Petr Kotvald",
                   "Holky z naší školky")

        async def catalog_without_olympic_version(artist, title, strict=False):
            return vagner  # the only recording the catalog knows

        dj, player, _ = make(current=T("c", "Someone"))
        dj.catalog.search_song = catalog_without_olympic_version
        return dj, player, vagner

    def test_2346_other_version_is_said_out_loud(self):
        # "pusť Holky z naší školky od Olympicu" → model picked the Vágner /
        # Hložek / Kotvald recording and the reply kept quiet about it.
        dj, player, vagner = self._vagner()
        _, reply = run(go(dj, "pusť Holky z naší školky od Olympicu", decision(
            action="play_next", reply="Pouštím Holky z naší školky.",
            requested=[{"artist": vagner.artist, "title": "Holky z naší školky"}])))
        self.assertEqual(player.current.id, "hv")  # most likely a misremembered artist
        self.assertEqual(reply, f"Od Olympicu ji nemám — hraju verzi {vagner.artist}.")

    def test_2346_insisted_artist_is_not_replaced(self):
        dj, player, vagner = self._vagner()
        _, reply = run(go(dj, "pusť Holky z naší školky jen od Olympicu", decision(
            action="play_next", reply="Pouštím Holky z naší školky.",
            requested=[{"artist": vagner.artist, "title": "Holky z naší školky"}])))
        self.assertEqual(player.current.id, "c")  # nothing played or cut
        self.assertIn("Nenašel jsem", reply)
        self.assertIn("od Olympicu", reply)
        self.assertNotIn("Pouštím", reply)

    def test_named_artist_version_is_found_instead(self):
        vagner = T("hv", "Karel Vágner, Stanislav Hložek, Petr Kotvald", "Holky z naší školky")
        olympic = T("ho", "Olympic", "Holky z naší školky")

        async def search(artist, title, strict=False):
            return olympic if "olympic" in artist.lower() else vagner

        dj, player, _ = make(current=T("c", "Someone"))
        dj.catalog.search_song = search
        run(go(dj, "pusť Holky z naší školky od Olympicu", decision(
            action="play_next",
            requested=[{"artist": vagner.artist, "title": "Holky z naší školky"}])))
        self.assertEqual(player.current.id, "ho")

    def test_auto_turn_does_not_write_taste(self):
        # six "Uživateli nesedí…" lines in taste.md came from skip reseeds
        dj, _, store = make()
        run(go(dj, "auto", decision(remember="Uživateli nesedí jazz."), auto=True))
        self.assertNotIn("jazz", store.taste())
        run(go(dj, "tohle mám rád", decision(remember="Má rád Stypku.")))
        self.assertIn("Stypku", store.taste())


class Scenarios(unittest.TestCase):
    """Realistic Czech requests → expected behaviour (the report's table)."""

    def test_specific_song_then_similar(self):
        dj, player, _ = make(current=T("c", "Current"))
        intent, _ = run(go(dj, "pusť Jasnou zprávu od Olympicu", decision(
            action="start_radio",
            requested=[{"artist": "Olympic", "title": "Jasná zpráva"}],
            seeds=[{"artist": "Olympic", "title": "Dávno"},
                   {"artist": "Žlutý pes", "title": "Ešus"}])))
        self.assertEqual(intent.kind, "song")
        self.assertEqual(player.current.title, "Jasná zpráva")
        self.assertEqual(dj.focus, "")

    def test_one_from_artist(self):
        dj, player, _ = make(current=T("c", "Current"), queue=[T("q", "Queued")])
        intent, _ = run(go(dj, "zahraj jednu od Chinaski", decision(
            action="play_next", requested=[{"artist": "Chinaski", "title": "Každý ráno"}])))
        self.assertEqual(intent.kind, "songs")
        self.assertEqual(player.current.artist, "Chinaski")
        self.assertEqual(player.queue[0].id, "q")  # the mood goes on after it

    def test_two_artists_alternate(self):
        dj, player, _ = make()
        intent, _ = run(go(dj, "pusť Kabát a Škwor", decision(
            action="start_radio", focus_artists=["Kabát", "Škwor"])))
        self.assertEqual(intent.kind, "artist")
        played = run(listen(dj, player, 8))
        self.assertEqual({t.artist for t in played}, {"Kabát", "Škwor"})
        self.assertEqual([t.artist for t in played[:4]], ["Kabát", "Škwor"] * 2)

    def test_artist_but_not_that_song(self):
        dj, player, _ = make()
        run(go(dj, "Kabát, ale ne Pohodu", decision(
            action="start_radio", focus_artists=["Kabát"],
            avoid=[{"artist": "Kabát", "title": "Pohoda"}])))
        played = run(listen(dj, player, 12))
        self.assertTrue(played)
        self.assertNotIn("Pohoda", [t.title for t in played])
        self.assertTrue(all(t.artist == "Kabát" for t in played))

    def test_more_like_this_starts_from_current(self):
        cur = T("cur", "Tata Bojs", "Opakování")
        dj, _, _ = make(current=cur)
        intent = build_intent("víc takového", decision(
            action="start_radio", seeds=[{"artist": "Other 1", "title": "x"}]))
        self.assertTrue(intent.more_like_current)
        plan = run(dj.resolve(intent))
        self.assertEqual(plan.seeds[0].id, "cur")

    def test_something_else_ends_artist_mode(self):
        dj, player, _ = make()
        run(go(dj, "hraj Kabát", decision(action="start_radio", focus_artists=["Kabát"])))
        self.assertEqual(dj.focus, "Kabát")
        run(go(dj, "něco jiného", decision(
            action="start_radio", seeds=[{"artist": "Other 1", "title": "song o1"}])))
        self.assertEqual(dj.focus, "")

    def test_unknown_artist_is_said_honestly(self):
        dj, player, _ = make(current=T("c", "Someone"))
        intent, reply = run(go(dj, "hraj Nobody Known", decision(
            action="start_radio", focus_artists=["Nobody Known"], reply="Pouštím.")))
        self.assertIn("nenašel", reply)
        self.assertNotIn(("clear",), player.log)  # playback left alone
        self.assertEqual(dj.focus, "")

    def test_auto_turn_cannot_end_artist_mode(self):
        dj, _, _ = make()
        run(go(dj, "hraj Kabát", decision(action="start_radio", focus_artists=["Kabát"])))
        run(go(dj, "auto", decision(action="start_radio",
                                    seeds=[{"artist": "Other 3", "title": "x"}]), auto=True))
        self.assertEqual(dj.focus, "Kabát")

    def test_song_plays_now_or_after(self):
        dj, player, _ = make(current=T("c", "Current"))
        run(go(dj, "pusť Wonderwall", decision(
            action="play_next", requested=[{"artist": "Oasis", "title": "Wonderwall"}])))
        self.assertEqual(player.current.title, "Wonderwall")
        dj, player, _ = make(current=T("c", "Current"))
        run(go(dj, "zařaď Wonderwall", decision(
            action="play_next", after_current=True,
            requested=[{"artist": "Oasis", "title": "Wonderwall"}])))
        self.assertEqual(player.current.id, "c")
        self.assertEqual(player.queue[0].title, "Wonderwall")

    def test_missing_part_is_reported(self):
        dj, _, _ = make(current=T("c", "Current"))
        _, reply = run(go(dj, "pusť Wonderwall a Nothing", decision(
            action="play_next", reply="Pouštím obě.",
            requested=[{"artist": "Oasis", "title": "Wonderwall"},
                       {"artist": "Nobody", "title": "Nothing"}])))
        self.assertIn("Nenašel jsem: Nobody — Nothing", reply)


class Wish(unittest.TestCase):
    def test_last_wish_is_in_the_prompt_and_survives_restart(self):
        dj, _, _ = make()
        run(go(dj, "Hraj davida Stypku", decision(
            action="start_radio", focus_artists=["David Stypka"])))
        prompt = run(dj._build_prompt("Nic nehraje. Pusť hudbu a navaž."))
        self.assertIn("Poslední výslovné přání posluchače: „Hraj davida Stypku“", prompt)
        self.assertIn("Režim interpreta: hraje se jen David Stypka", prompt)
        again = ListenerIntent.load(dj._wish_file)  # a restarted process
        self.assertEqual(again.focus_artists, ["David Stypka"])

    def test_earlier_wishes_are_kept(self):
        dj, _, _ = make()
        run(go(dj, "jen česky", decision(action="start_radio",
                                         seeds=[{"artist": "Other 1", "title": "x"}])))
        run(go(dj, "teď něco rychlejšího", decision(
            action="start_radio", seeds=[{"artist": "Other 2", "title": "y"}])))
        text = dj.wish.describe()
        self.assertIn("teď něco rychlejšího", text)
        self.assertIn("předtím: „jen česky“", text)


class Interleave(unittest.TestCase):
    def test_alternates_and_dedups(self):
        a = [T("1", "A"), T("2", "A"), T("3", "A")]
        b = [T("x", "B"), T("2", "A")]
        self.assertEqual([t.id for t in interleave([a, b])], ["1", "x", "2", "3"])


if __name__ == "__main__":
    unittest.main()
