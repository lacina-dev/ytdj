"""Skladba nebo kapela, celé album, výslovně vulgární přání — hlášení z kanceláře
5.–6. 10. 2026 (texty přání jsou doslova z provozního logu, bez jmen):

    .venv/bin/python -m unittest tests.test_wish_understanding -v

A) "nerdíci v neklidu" — skladbu, kterou model nezná, bral jako kapelu a DJ
   řekl "nenašel jsem", ač v katalogu je.
B) "metallica - album load" — model album pochopil, aplikace hrála hity
   interpreta z jiných alb.
C) "tu nejvulgárnější, nejsprostější prasárnu, co znáš" — filtr explicitních
   skladeb z rádia vyřadil právě to, o co šlo, a zbylo Mötley Crüe.

Bez sítě a bez modelu: falešný katalog, falešné mpv, rozhodnutí modelu
skriptovaná (jak je model na Pi skutečně vrátil, a jak je má vracet teď).
Skutečný model měří tests/model_wish_check.py.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_wishes as tw  # noqa: E402  (zkušební stolice fronty přání)
from test_wishes import Rig, T, run  # noqa: E402

from ytdj.agent import codex as codex_mod  # noqa: E402
from ytdj.agent.codex import DECISION_SCHEMA  # noqa: E402
from ytdj.agent.fastpath import name_matches  # noqa: E402
from ytdj.agent.intent import build_intent  # noqa: E402
from ytdj.agent.prompts import ROLE, render_state  # noqa: E402
from ytdj.config import DEFAULTS, Config  # noqa: E402
from ytdj.music import match  # noqa: E402
from ytdj.music.catalog import Album, Catalog, Probe, Track, album_rank  # noqa: E402
from ytdj.music.radio import RadioPools  # noqa: E402

# ---- texty přání z provozu ----
WISH_SONG_THEN_ARTIST = "Zahraj nerdíky v neklidu, poté další věci od Pondělníků"
WISH_BARE_TITLE = "nerdíci v neklidu"
WISH_TWO_ALBUMS = "sabaton - alba primo victoria a art of war"
WISH_ALBUM = "metallica - album load"
WISH_LIVE_ALBUM = "rammstein live aus berlin 1998 remastered"
WISH_VULGAR = "Zahraj tu nejvulgárnější, nejsprostější prasárnu, co znáš"
WISH_VULGAR_CZ = ("Zahraj něco vulgárního, nechutnýho a nekorektního, co pohorší všechny slušný "
                  "lidi v místnosti. A ať je to česky.")

SONG = Track("nerdicivnek", "Nerdíci v neklidu", "Pondělníci", duration=81)
BAND = [Track(f"pondelnic{i:02d}", f"Pondělní {i}", "Pondělníci", duration=150) for i in range(8)]
LOAD = [Track(f"load{i:07d}", t, "Metallica", "Load", 300) for i, t in enumerate(
    ["Ain't My Bitch", "2 x 4", "The House Jack Built", "Until It Sleeps", "King Nothing",
     "Hero of the Day", "Bleeding Me", "Cure", "Poor Twisted Me", "Wasting My Hate"])]
PRIMO = [Track(f"primo{i:06d}", f"Primo {i}", "Sabaton", "Primo Victoria", 240) for i in range(5)]
ART = [Track(f"artofwar{i:03d}", f"Art {i}", "Sabaton", "The Art of War", 240) for i in range(5)]


def decision(**kw) -> dict:
    base = tw.decision(albums=[], explicit_ok=False)
    base.update(kw)
    return base


def P(artist: str, title: str) -> dict:
    return {"artist": artist, "title": title}


def teach(rig: Rig) -> None:
    """Falešný katalog se naučí kapelu, její skladbu a alba."""
    cat = rig.catalog
    plain_song, plain_artist = cat.search_song, cat.artist_tracks

    async def search_song(artist: str, title: str, strict: bool = False):
        cat.calls.append(("search_song", artist, title))
        if "neklidu" in match.norm(title) and "poté" not in title:
            # hledání podle názvu ji najde; jméno kapely ve 2. pádě katalog nezná
            return SONG if match.norm(artist) in ("", "pondelnici") else None
        if match.norm(title) in ("nerdici v neklidu, pote dalsi veci",):
            return None
        return await plain_song(artist, title)

    async def artist_tracks(name: str, limit: int = 50):
        if match.norm(name) == "pondelnici":
            return list(BAND)[:limit]
        if "neklidu" in match.norm(name):
            return []  # taková kapela není
        return await plain_artist(name, limit)

    async def album_tracks(artist: str, title: str):
        cat.calls.append(("album_tracks", artist, title))
        key = match.norm(title)
        if key == "load":
            return Album("Load", "Metallica", list(LOAD), "1996")
        if key == "primo victoria":
            return Album("Primo Victoria (Re-Armed)", "Sabaton", list(PRIMO))
        if key == "the art of war":
            return Album("The Art of War (Re-Armed)", "Sabaton", list(ART))
        return None

    cat.search_song, cat.artist_tracks, cat.album_tracks = search_song, artist_tracks, album_tracks


async def top_up(rig: Rig) -> None:
    """Co dělá plnič fronty v aplikaci: doplnit podkres do queue_target."""
    need = rig.cfg.queue_target - rig.player.queue_depth
    if need > 0:
        fresh = await rig.dj.next_tracks(need)
        if fresh:
            await rig.player.enqueue(fresh)


async def play_through(rig: Rig, n: int) -> list[str]:
    """Nechá dohrát `n` skladeb; vrací, co postupně hrálo (videoId)."""
    played = [rig.fake.current_vid()]
    for _ in range(n):
        await top_up(rig)
        rig.fake.finish_current()
        await rig.settle(0.3)
        played.append(rig.fake.current_vid())
    return played


class _YT:
    """ytmusicapi nahraný z odpovědí (jen to, co katalog volá)."""

    def __init__(self, mixed=(), albums=(), album_data=None) -> None:
        self.mixed, self.albums, self.album_data = list(mixed), list(albums), album_data or {}
        self.calls: list[tuple] = []

    def search(self, query, filter=None, limit=20):
        self.calls.append(("search", query, filter))
        return list(self.albums) if filter == "albums" else list(self.mixed)

    def get_album(self, browse_id):
        self.calls.append(("get_album", browse_id))
        return self.album_data[browse_id]


class _NoPlayer:
    async def status(self):
        from ytdj.player.base import PlayerStatus
        return PlayerStatus(playing=False, paused=False, current=None, position=0.0,
                            duration=0.0, queue=[], volume=50)


def real_catalog(yt: _YT) -> Catalog:
    cat = Catalog(Config(**DEFAULTS))
    cat.yt = yt
    return cat


# ---------------------------------------------------------------------------
# A) skladba, nebo kapela
# ---------------------------------------------------------------------------

class SongOrBand(unittest.TestCase):
    def test_unknown_band_name_is_tried_as_song_title(self):
        """Pi 5. 10. 9:04: model dal název skladby do focus_artists → "nic jsem nenašel"."""
        rig = Rig()
        teach(rig)
        intent = build_intent(WISH_BARE_TITLE, decision(
            action="start_radio", focus_artists=["Nerdíci v neklidu"],
            seeds=[P("Nerdíci v neklidu", ""), P("Nerdíci v neklidu", "")]))
        self.assertEqual(intent.kind, "artist")
        plan = run(rig.dj.resolve(intent))
        self.assertEqual(plan.failed, "")
        self.assertEqual([t.id for t in plan.requested], [SONG.id])
        self.assertEqual(plan.intent.kind, "song")  # skladba hned, pak podobné
        self.assertEqual(plan.intent.artists, [])
        self.assertTrue(any("je to skladba" in n and "Pondělníci" in n for n in plan.notes), plan.notes)
        self.assertFalse(any("nenašel" in n for n in plan.notes), plan.notes)

    def test_song_first_then_more_by_the_artist(self):
        """Pi 5. 10. 9:03: "…nerdíky v neklidu, poté další věci od Pondělníků" hrálo
        kapelu od jiné písně a tu vyžádanou vůbec."""
        for data in (
            # jak to model vrátil na Pi (název jako druhý "interpret")
            decision(action="start_radio", focus_artists=["Nerdíky v neklidu", "Pondělníci"]),
            # jak to má vracet s podkladem z katalogu
            decision(action="start_radio", focus_artists=["Pondělníci"],
                     requested=[P("Pondělníci", "Nerdíci v neklidu")]),
        ):
            rig = Rig()
            teach(rig)
            plan = run(rig.dj.resolve(build_intent(WISH_SONG_THEN_ARTIST, data)))
            self.assertEqual(plan.failed, "")
            self.assertEqual(plan.intent.kind, "artist")
            self.assertEqual(plan.intent.artists, ["Pondělníci"])
            self.assertEqual([t.id for t in plan.requested], [SONG.id])
            self.assertEqual(len(plan.artist_tracks), len(BAND))
            self.assertFalse(any(n.startswith("Nenašel") or "ji nemám" in n for n in plan.notes),
                             plan.notes)

    def test_song_first_in_the_queue_then_only_the_artist(self):
        async def go():
            async with Rig() as rig:
                teach(rig)
                tw.SCRIPT[WISH_SONG_THEN_ARTIST] = ("codex", decision(
                    action="start_radio", focus_artists=["Nerdíky v neklidu", "Pondělníci"]))
                await rig.background()
                w = rig.wq.submit(WISH_SONG_THEN_ARTIST, "kolega")
                await rig.until(lambda: w.state in ("queued", "playing"))
                await rig.settle(0.3)
                self.assertEqual(w.tracks[0].id, SONG.id)
                self.assertTrue(all(t.artist == "Pondělníci" for t in w.tracks))
                self.assertEqual(rig.fake.current_vid(), SONG.id)  # hraje hned (utne podkres)
                self.assertEqual(rig.pools.artist, "Pondělníci")

        try:
            run(go())
        finally:
            tw.SCRIPT.pop(WISH_SONG_THEN_ARTIST, None)

    def test_really_unknown_name_is_still_not_found(self):
        rig = Rig()
        teach(rig)

        async def nothing(artist, title, strict=False):
            return None

        rig.catalog.search_song = nothing
        plan = run(rig.dj.resolve(build_intent("hraj Úplně Neznámí", decision(
            action="start_radio", focus_artists=["Úplně Neznámí"]))))
        self.assertIn("nic nenašel", plan.failed)

    def test_band_in_plural_matches_its_other_cases(self):
        self.assertTrue(name_matches(["pondelniku"], "Pondělníci"))
        self.assertTrue(name_matches(["pondelniky"], "Pondělníci"))
        self.assertFalse(name_matches(["pondelnik"], "Pondělníci"))
        self.assertFalse(name_matches(["olympic"], "Olympica"))  # pořád jiná kapela

    def test_probe_tells_songs_artists_albums_apart(self):
        yt = _YT(mixed=[
            {"resultType": "song", "title": "Nerdíci v neklidu", "videoId": "nerdicivnek",
             "artists": [{"name": "Pondělníci"}], "album": {"name": "Demo"}},
            {"resultType": "video", "title": "Pondělníci - Nerdíci v neklidu",
             "artists": [{"name": "Pondělníci Official"}]},
            {"resultType": "artist", "artist": "Pondělníci", "browseId": "UC1"},
            {"resultType": "album", "title": "Load", "type": "Album", "year": "1996",
             "artists": [{"name": "Metallica"}]},
            {"resultType": "playlist", "title": "něco"},
        ])
        probe = run(real_catalog(yt).probe("Nerdici v neklidu od kapely Polednici"))
        self.assertEqual(yt.calls, [("search", "Nerdici v neklidu od kapely Polednici", None)])
        self.assertEqual(probe.songs, ["Pondělníci — Nerdíci v neklidu [album Demo]"])
        self.assertEqual(probe.artists, ["Pondělníci"])
        self.assertEqual(probe.albums, ["Metallica — Load (Album, 1996)"])
        lines = "\n".join(probe.lines())
        self.assertIn("skladby: Pondělníci — Nerdíci v neklidu", lines)
        self.assertIn("interpreti: Pondělníci", lines)

    def test_probe_failure_is_just_empty(self):
        class Broken:
            def search(self, *a, **k):
                raise RuntimeError("síť")

        probe = run(real_catalog(Broken()).probe("cokoli"))
        self.assertFalse(probe)
        self.assertEqual(probe.lines(), [])

    def test_model_gets_catalogue_lines_with_the_wish(self):
        """Model do katalogu nevidí — dostane, co katalog pod textem přání zná."""
        async def go():
            rig = Rig()
            seen: list[str] = []

            async def probe(text):
                return Probe(songs=["Pondělníci — Nerdíci v neklidu"], took_ms=5)

            async def model(prompt, auto):
                seen.append(prompt)
                return decision(action="start_radio",
                                requested=[P("Pondělníci", "Nerdíci v neklidu")])

            rig.catalog.probe = probe
            rig.dj.player = _NoPlayer()
            rig.dj._model_decision = model
            rig.dj.note_wish = lambda: None
            intent = await rig.dj.interpret(WISH_BARE_TITLE)
            self.assertEqual(intent.tracks, [("Pondělníci", "Nerdíci v neklidu")])
            self.assertIn("Katalog k textu přání", seen[0])
            self.assertIn("skladby: Pondělníci — Nerdíci v neklidu", seen[0])
            self.assertTrue(seen[0].rstrip().endswith(f"Uživatel říká: {WISH_BARE_TITLE}"))
            # zadání od aplikace (přeseedování) nejsou slova posluchače — bez hledání
            await rig.dj.interpret("Posluchač přeskočil několik skladeb", auto=True)
            self.assertNotIn("Katalog k textu přání (", seen[1].split("Aktuální stav")[1])

        run(go())

    def test_probe_starts_with_the_wish_and_never_holds_the_turn(self):
        async def go():
            rig = Rig()
            started: list[str] = []

            async def slow_probe(text):
                started.append(text)
                await asyncio.sleep(5)
                return Probe(songs=["x — y"])

            rig.catalog.probe = slow_probe
            rig.dj.probe_start(WISH_BARE_TITLE)  # s příchodem přání (fast_plan)
            rig.dj.probe_start(WISH_BARE_TITLE)  # podruhé nic nového
            await asyncio.sleep(0)
            self.assertEqual(started, [WISH_BARE_TITLE])
            with mock.patch.object(codex_mod, "PROBE_WAIT", 0.05):
                t0 = asyncio.get_running_loop().time()
                lines = await rig.dj._probe_lines(WISH_BARE_TITLE + " (dovětek fronty)")
                self.assertLess(asyncio.get_running_loop().time() - t0, 0.5)
            self.assertEqual(lines, [])  # katalog nestihl — tah jede bez podkladu
            self.assertEqual(started, [WISH_BARE_TITLE])  # a nehledalo se znovu

        run(go())

    def test_prompt_decides_song_or_band_from_the_catalogue(self):
        self.assertIn("Katalog k textu přání", ROLE)
        self.assertIn("rozhodni podle katalogu", ROLE)
        state = render_state("", [], "žádné aktivní seedy", [], "",
                             catalog=["  skladby: A — B"])
        self.assertIn("  skladby: A — B", state)
        self.assertNotIn("Katalog k textu", render_state("", [], "x", [], ""))


# ---------------------------------------------------------------------------
# B) celé album
# ---------------------------------------------------------------------------

class Albums(unittest.TestCase):
    def test_schema_and_prompt_know_albums(self):
        self.assertIn("albums", DECISION_SCHEMA["properties"])
        self.assertIn("albums", DECISION_SCHEMA["required"])
        self.assertIn("Pole `albums`", ROLE)

    def test_album_decision_wins_over_artist_mode(self):
        """Pi 5. 10. 14:55: "metallica - album load" skončilo jako režim interpreta."""
        intent = build_intent(WISH_ALBUM, decision(
            action="start_radio", albums=[P("Metallica", "Load")], focus_artists=["Metallica"],
            seeds=[P("Metallica", "Until It Sleeps")]))
        self.assertEqual(intent.kind, "album")
        self.assertEqual(intent.albums, [("Metallica", "Load")])
        self.assertEqual(intent.artists, [])
        self.assertTrue(intent.changes_music)
        # bez alb zůstává interpret interpretem
        plain = build_intent("pusť Metallicu", decision(
            action="start_radio", focus_artists=["Metallica"]))
        self.assertEqual((plain.kind, plain.albums), ("artist", []))

    def test_edition_follows_the_wording(self):
        def pick(wanted: str, found: list[tuple[str, str]]) -> str:
            ranked = [(album_rank(t, kind, wanted, n), t) for n, (t, kind) in enumerate(found)]
            return min((r for r in ranked if r[0] is not None), default=(None, ""))[1]

        load = [("Load (Remastered Deluxe Box Set)", "Album"), ("Load", "Album"), ("Reload", "Album")]
        self.assertEqual(pick("Load", load), "Load")  # studiová, ne box set, ne Reload
        self.assertEqual(pick("Load remastered", load), "Load (Remastered Deluxe Box Set)")
        giaa = [("All Is Violent, All Is Bright (Live)", "Album"),
                ("All Is Violent, All Is Bright (2011 Remastered Edition)", "Album"),
                ("All Is Violent, All Is Bright (2025)", "Single")]
        self.assertEqual(pick("All Is Violent, All Is Bright", giaa),
                         "All Is Violent, All Is Bright (2011 Remastered Edition)")
        self.assertEqual(pick("All Is Violent, All Is Bright live", giaa),
                         "All Is Violent, All Is Bright (Live)")
        live = [("Live aus Berlin", "Album"), ("Sehnsucht", "Album")]
        self.assertEqual(pick("Live aus Berlin", live), "Live aus Berlin")
        self.assertEqual(pick("Live aus Berlin 1998 remastered", live), "Live aus Berlin")
        self.assertEqual(pick("Úplně jiné album", live), "")

    def test_catalogue_returns_the_album_in_order(self):
        yt = _YT(
            albums=[
                {"title": "Reload", "type": "Album", "browseId": "B2", "artists": [{"name": "Metallica"}]},
                {"title": "Load", "type": "Album", "browseId": "B1", "year": "1996",
                 "artists": [{"name": "Metallica"}]},
                {"title": "Load", "type": "Album", "browseId": "B3", "artists": [{"name": "Jiná Kapela"}]},
            ],
            album_data={"B1": {"title": "Load", "year": "1996", "artists": [{"name": "Metallica"}],
                               "tracks": [
                                   {"videoId": "a" * 11, "title": "Ain't My Bitch", "artists": [{"name": "Metallica"}],
                                    "duration_seconds": 304, "isExplicit": True},
                                   {"videoId": None, "title": "nedostupná"},
                                   {"videoId": "b" * 11, "title": "2 x 4", "artists": None},
                                   {"videoId": "c" * 11, "title": "x", "isAvailable": False},
                               ]}})
        album = run(real_catalog(yt).album_tracks("Metallica", "Load"))
        self.assertEqual(yt.calls, [("search", "Metallica Load", "albums"), ("get_album", "B1")])
        self.assertEqual(album.label(), "Metallica — Load")
        self.assertEqual([t.title for t in album.tracks], ["Ain't My Bitch", "2 x 4"])
        self.assertEqual({t.artist for t in album.tracks}, {"Metallica"})
        self.assertEqual({t.album for t in album.tracks}, {"Load"})
        self.assertIsNone(run(real_catalog(yt).album_tracks("Metallica", "Neexistuje")))

    def test_album_pool_keeps_order_plays_once_then_similar(self):
        rig = Rig()
        pools = rig.pools
        dirty = Track("explicit000", "Sprostá", "Metallica", "Load", 30, explicit=True)  # i krátká
        album = LOAD[:3] + [dirty] + LOAD[3:6]
        run(pools.set_album("Metallica — Load", album, done={LOAD[0].id}, artists=["Metallica"]))
        self.assertEqual(pools.album, "Metallica — Load")
        first = run(pools.next_tracks(3))
        self.assertEqual([t.id for t in first], [LOAD[1].id, LOAD[2].id, dirty.id])
        pools.give_back(first[1:])  # plnič je nezařadil — vrátí se na své místo
        again = run(pools.next_tracks(2))
        self.assertEqual([t.id for t in again], [LOAD[2].id, dirty.id])
        pools.note_started(LOAD[1].id)
        self.assertEqual([t.id for t in pools.album_remaining()][:2], [LOAD[2].id, dirty.id])
        rest = run(pools.next_tracks(10))
        self.assertEqual([t.id for t in rest], [t.id for t in LOAD[3:6]])
        self.assertEqual(pools.album, "Metallica — Load")
        after = run(pools.next_tracks(3))  # album dohrálo → rádio "… a podobné"
        self.assertEqual(pools.album, "")
        self.assertEqual(pools.artist, "")
        self.assertEqual(pools.mood, "Metallica — Load a podobné")
        self.assertTrue(after and not {t.id for t in after} & {t.id for t in album})

    def test_first_four_are_the_wish_and_the_rest_follows_in_album_order(self):
        async def go():
            async with Rig() as rig:
                teach(rig)
                tw.SCRIPT[WISH_ALBUM] = ("codex", decision(
                    action="start_radio", albums=[P("Metallica", "Load")],
                    mood="album Load od Metallicy"))
                await rig.background()
                w = rig.wq.submit(WISH_ALBUM, "kolega")
                await rig.until(lambda: w.state in ("queued", "playing"))
                await rig.settle(0.3)
                self.assertEqual(w.kind, "album")
                self.assertEqual([t.id for t in w.tracks], [t.id for t in LOAD[:4]])  # rozpočet 4
                self.assertTrue(w.handed)
                self.assertEqual(rig.pools.album, "Metallica — Load")
                self.assertEqual(rig.wq.bg_reason.get("mode"), "album")
                self.assertIn("Album Metallica — Load (10 skladeb)", w.reply)
                self.assertIn("první čtyři jako tvoje přání, zbytek hraje dál jako podkres", w.reply)
                played = await play_through(rig, len(LOAD) - 1)
                self.assertEqual(played, [t.id for t in LOAD])  # celé album, v pořadí, nic jiného
                self.assertEqual(w.state, "done")
                self.assertEqual(w.played, 4)
                # po albu už ne interpret ani album dokola, ale podobná hudba
                after = await play_through(rig, 2)
                self.assertFalse(set(after[1:]) & {t.id for t in LOAD})
                self.assertEqual(rig.pools.album, "")

        try:
            run(go())
        finally:
            tw.SCRIPT.pop(WISH_ALBUM, None)

    def test_two_albums_only_those_two_in_order(self):
        """Pi 5. 10. 13:18: "sabaton - alba primo victoria a art of war" hrálo The Last
        Stand, Bismarck… — ani jednu skladbu z těch dvou alb."""
        async def go():
            async with Rig() as rig:
                teach(rig)
                tw.SCRIPT[WISH_TWO_ALBUMS] = ("codex", decision(
                    action="start_radio",
                    albums=[P("Sabaton", "Primo Victoria"), P("Sabaton", "The Art of War")]))
                await rig.background()
                w = rig.wq.submit(WISH_TWO_ALBUMS, "kolega")
                await rig.until(lambda: w.state in ("queued", "playing"))
                await rig.settle(0.3)
                self.assertIn("Sabaton — Primo Victoria (Re-Armed) + Sabaton — The Art of War (Re-Armed)",
                              w.reply)
                played = await play_through(rig, 9)
                self.assertEqual(played, [t.id for t in PRIMO + ART])

        try:
            run(go())
        finally:
            tw.SCRIPT.pop(WISH_TWO_ALBUMS, None)

    def test_somebody_elses_wish_goes_first_and_the_album_resumes(self):
        async def go():
            async with Rig() as rig:
                teach(rig)
                tw.SCRIPT[WISH_ALBUM] = ("codex", decision(
                    action="start_radio", albums=[P("Metallica", "Load")]))
                await rig.background()
                w = rig.wq.submit(WISH_ALBUM, "kolega")
                await rig.until(lambda: w.state in ("queued", "playing"))
                await rig.settle(0.3)
                played = await play_through(rig, 4)  # 4 z přání + 1 z podkresu alba hraje
                self.assertEqual(played, [t.id for t in LOAD[:5]])
                # kolegyně si přeje písničku a pak náladu — obojí má přednost
                j = rig.wq.submit("Holky z naší školky", "kolegyně")
                await rig.until(lambda: j.state in ("queued", "playing"))
                rig.catalog.radio_base = 500
                k = rig.wq.submit("něco klidnějšího", "kolegyně")
                await rig.until(lambda: k.state in ("queued", "playing"))
                await rig.settle(0.3)
                self.assertEqual(rig.pools.album, "Metallica — Load")  # album drží dál
                self.assertEqual(rig.wq.bg_reason.get("id"), w.id)
                more = await play_through(rig, 4 + len(LOAD) - 5)
                theirs = {j.tracks[0].id} | {t.id for t in k.tracks}
                self.assertEqual(len(theirs), 4)
                load_ids = [t.id for t in LOAD]
                # její přání utne podkres (jako vždy) a zazní celé před albem
                first_album = next(i for i, v in enumerate(more) if v in load_ids[5:])
                self.assertEqual({v for v in more[:first_album] if v not in load_ids}, theirs)
                # pak album tam, kde přestalo — nic se neopakuje, nic nevypadne, nic cizího
                self.assertEqual(more[first_album:first_album + 5], load_ids[5:])
                self.assertFalse(set(more[first_album + 5:]) & set(load_ids))

        try:
            run(go())
        finally:
            tw.SCRIPT.pop(WISH_ALBUM, None)

    def test_album_waits_for_others_then_continues_as_background(self):
        async def go():
            async with Rig() as rig:
                teach(rig)
                tw.SCRIPT[WISH_ALBUM] = ("codex", decision(
                    action="start_radio", albums=[P("Metallica", "Load")]))
                await rig.background()
                j = rig.wq.submit("pusť Kabát", "kolegyně")  # dlouhé přání někoho jiného
                await rig.until(lambda: j.state in ("queued", "playing"))
                w = rig.wq.submit(WISH_ALBUM, "kolega")
                await rig.until(lambda: w.state in ("queued", "playing"))
                await rig.settle(0.3)
                self.assertFalse(w.handed)
                self.assertEqual(len(w.tracks), 4)
                self.assertIn("první čtyři jako tvoje přání; čekají i další", w.reply)
                played = await play_through(rig, 25)
                load_ids = [t.id for t in LOAD]
                heard = [v for v in played if v in load_ids]
                self.assertEqual(heard, load_ids)  # celé album, v pořadí, každá jednou

        try:
            run(go())
        finally:
            tw.SCRIPT.pop(WISH_ALBUM, None)

    def test_authors_next_wish_ends_the_album(self):
        async def go():
            async with Rig() as rig:
                teach(rig)
                tw.SCRIPT[WISH_ALBUM] = ("codex", decision(
                    action="start_radio", albums=[P("Metallica", "Load")]))
                await rig.background()
                w = rig.wq.submit(WISH_ALBUM, "kolega")
                await rig.until(lambda: w.state in ("queued", "playing"))
                await rig.settle(0.3)
                rig.catalog.radio_base = 700
                k = rig.wq.submit("něco klidnějšího", "kolega")  # týž člověk
                await rig.until(lambda: k.state in ("queued", "playing"))
                await rig.settle(0.3)
                self.assertEqual(rig.pools.album, "")
                self.assertEqual(rig.pools.mood, "klidný pop")
                self.assertIsNone(rig.wq._album_due())

        try:
            run(go())
        finally:
            tw.SCRIPT.pop(WISH_ALBUM, None)

    def test_unknown_album_is_said_honestly(self):
        rig = Rig()
        teach(rig)
        plan = run(rig.dj.resolve(build_intent("album Neexistuje", decision(
            action="start_radio", albums=[P("Metallica", "Neexistuje")]))))
        self.assertIn("Album Metallica — Neexistuje jsem v katalogu nenašel", plan.failed)

    def test_album_background_survives_restart(self):
        async def go():
            async with Rig() as rig:
                teach(rig)
                await rig.wq._set_background(album=True, artist="Metallica — Load",
                                             artist_tracks=list(LOAD), artists=["Metallica"],
                                             done={t.id for t in LOAD[:4]}, who="kolega")
                rig.pools.note_started(LOAD[4].id)
                bg = rig.wq.session_state()["bg"]
                self.assertEqual(bg["mode"], "album")
                self.assertEqual(bg["done"], [t.id for t in LOAD[:5]])
                self.assertEqual(len(bg["tracks"]), len(LOAD))

        run(go())


# ---------------------------------------------------------------------------
# C) výslovně vulgární přání
# ---------------------------------------------------------------------------

DIRTY = [Track(f"dirty{i:06d}", f"Sprostá {i}", f"Sprosťák {i}", None, 200, explicit=True)
         for i in range(12)]
CLEAN = [Track(f"clean{i:06d}", f"Slušná {i}", f"Slušňák {i}", None, 200) for i in range(12)]


def vulgar_radio(rig: Rig) -> None:
    async def radio(video_id: str, limit: int = 50):
        # rádio ze sprosté písně: hlavně sprosté, pár slušných mezi nimi
        return DIRTY[:8] + CLEAN[:6]

    rig.catalog.radio = radio


class ExplicitOnRequest(unittest.TestCase):
    def test_schema_and_prompt_know_explicit_ok(self):
        self.assertEqual(DECISION_SCHEMA["properties"]["explicit_ok"], {"type": "boolean"})
        self.assertIn("explicit_ok", DECISION_SCHEMA["required"])
        self.assertIn("Pole `explicit_ok`", ROLE)
        self.assertIn("česká kancelář", ROLE)

    def test_decision_carries_explicit_ok_only_for_music(self):
        mood = build_intent(WISH_VULGAR, decision(
            action="start_radio", explicit_ok=True, seeds=[P("Sprosťák 0", "Sprostá 0")]))
        self.assertTrue(mood.explicit_ok)
        self.assertFalse(build_intent("něco veselého", decision(
            action="start_radio", seeds=[P("A", "B")])).explicit_ok)
        self.assertFalse(build_intent("ahoj", decision(action="nothing", explicit_ok=True)).explicit_ok)

    def test_pool_lets_explicit_through_only_when_asked(self):
        """Pi 2. 10. 9:52: radio.pool rejected {"explicit": 13} ze 16 → Mötley Crüe."""
        rig = Rig()
        vulgar_radio(rig)
        seed = [T("seedx", "Seed")]
        run(rig.pools.set_seeds(seed, mood="sprosté"))
        self.assertFalse(any(t.explicit for t in run(rig.pools.next_tracks(6))))
        run(rig.pools.set_seeds(seed, mood="sprosté", explicit_ok=True))
        rig.pools.session_seen.clear()
        got = run(rig.pools.next_tracks(6))
        self.assertEqual([t.id for t in got[:2]], [DIRTY[0].id, DIRTY[1].id])
        # každé další naplnění poolů filtr vrací
        run(rig.pools.set_seeds(seed, mood="jiné"))
        self.assertFalse(rig.pools.explicit_ok)
        run(rig.pools.set_seeds(seed, mood="sprosté", explicit_ok=True))
        run(rig.pools.set_artist("Kabát", tw.KABAT))
        self.assertFalse(rig.pools.explicit_ok)

    def test_vulgar_wish_gets_vulgar_songs_and_the_next_wish_is_clean_again(self):
        async def go():
            async with Rig() as rig:
                vulgar_radio(rig)
                for text in (WISH_VULGAR, WISH_VULGAR_CZ):
                    tw.SCRIPT[text] = ("codex", decision(
                        action="start_radio", explicit_ok=True, mood="vulgární a sprostá hudba",
                        seeds=[P("Sprosťák 0", "Sprostá 0")]))
                await rig.background()
                w = rig.wq.submit(WISH_VULGAR, "kolega")
                await rig.until(lambda: w.state in ("queued", "playing"))
                await rig.settle(0.2)
                self.assertTrue(w.explicit_ok)
                self.assertTrue(w.tracks and all(t.explicit for t in w.tracks), w.tracks)
                self.assertTrue(rig.pools.explicit_ok)  # i jeho podkres
                self.assertIn(("radio.explicit_ok", True),
                              [(k, f.get("on")) for k, f in rig.events])
                # další přání (kohokoli, čehokoli) → filtr zase platí
                j = rig.wq.submit("Holky z naší školky", "kolegyně")
                await rig.until(lambda: j.state in ("queued", "playing"))
                self.assertFalse(rig.pools.explicit_ok)
                rig.pools.session_seen.clear()
                self.assertFalse(any(t.explicit for t in await rig.dj.next_tracks(5)))
                # a obyčejná nálada sprosté nedostane vůbec
                k = rig.wq.submit("něco klidnějšího", "kolegyně")
                await rig.until(lambda: k.state in ("queued", "playing"))
                self.assertFalse(k.explicit_ok)
                self.assertFalse(any(t.explicit for t in k.tracks))

        try:
            run(go())
        finally:
            tw.SCRIPT.pop(WISH_VULGAR, None)
            tw.SCRIPT.pop(WISH_VULGAR_CZ, None)

    def test_search_results_carry_the_explicit_badge(self):
        """Odznak E měly jen skladby z rádia; výsledky hledání ho ztrácely."""
        c = match.from_song({"videoId": "abcdefghijk", "title": "X", "artists": [{"name": "A"}],
                             "isExplicit": True})
        self.assertTrue(c.explicit)
        from ytdj.music.catalog import _track
        self.assertTrue(_track(c).explicit)
        self.assertIsNone(_track(match.from_song(
            {"videoId": "abcdefghijk", "title": "X", "artists": [{"name": "A"}]})).explicit)


if __name__ == "__main__":
    unittest.main()
