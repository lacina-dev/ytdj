"""Import playlistu do oblíbených kanceláře (POZADAVKY #48, FUNKCE F-HLASY-12…18, 21…24).

Falešná odpověď ytmusicapi (`get_playlist`), opravdová VoteBook, Store
(SQLite v dočasném adresáři) a obslužné funkce webu bez sítě.

    YTDJ_EVENTS_FILE=/tmp/ev.jsonl .venv/bin/python -m unittest tests.test_playlist_import -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

_TMP = tempfile.mkdtemp(prefix="ytdj-import-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)
os.environ.setdefault("YTDJ_EVENTS_FILE", str(Path(_TMP) / "events.jsonl"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_wishes as tw  # noqa: E402
from ytdj import imports as I  # noqa: E402
from ytdj import votes as V  # noqa: E402
from ytdj.config import DEFAULTS, Config  # noqa: E402
from ytdj.display import Censor  # noqa: E402
from ytdj.imports import Importer, ImportRefused, normalize, parse_ref  # noqa: E402
from ytdj.music.catalog import Track  # noqa: E402
from ytdj.music.radio import RadioPools  # noqa: E402
from ytdj.state import Store  # noqa: E402
from ytdj.votes import ARTIST, BANNED, FAVOURITE, SONG, VoteBook  # noqa: E402
from ytdj.web import server as web  # noqa: E402

PETR, JANA, KAREL, EVA = "client-petr", "client-jana", "client-karel", "client-eva"
PL = "PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"  # 34 znaků jako skutečné PL…


def item(vid: str, title: str, artist: str | None, vtype: str = "MUSIC_VIDEO_TYPE_ATV",
         dur: int | None = 200, avail: bool = True) -> dict:
    """Položka playlistu, jak ji vrací ytmusicapi 1.12 (bez balastu)."""
    return {"videoId": vid, "title": title,
            "artists": [{"name": a, "id": "UC" + a} for a in (artist.split(", ") if artist else [])],
            "album": None, "videoType": vtype, "duration_seconds": dur, "isAvailable": avail,
            "thumbnails": [{"url": "https://x"}], "setVideoId": "s" + vid}


def songs(prefix: str, n: int, artist: str = "") -> list[dict]:
    return [item(f"{prefix}{i:010d}", f"{prefix} song {i}", artist or f"{prefix} band {i}")
            for i in range(n)]


class FakeYT:
    """`YTMusic.get_playlist` nad slovníkem playlistů; zapisuje, ve kterém vlákně běží."""

    def __init__(self, lists: dict[str, dict] | None = None, delay: float = 0.0) -> None:
        self.lists = lists or {}
        self.delay = delay
        self.threads: list[str] = []
        self.calls: list[tuple[str, int]] = []

    def get_playlist(self, pid: str, limit: int = 100) -> dict:
        self.threads.append(threading.current_thread().name)
        self.calls.append((pid, limit))
        if self.delay:
            time.sleep(self.delay)  # synchronní jako requests
        if pid not in self.lists:
            raise KeyError("Unable to find 'contents' using path ['contents', "
                           "'twoColumnBrowseResultsRenderer'] on {...}")
        res = self.lists[pid]
        if isinstance(res, BaseException):
            raise res
        tracks = list(res.get("tracks", []))
        return {**res, "tracks": tracks[:limit],
                "trackCount": res.get("trackCount", len(tracks))}


def playlist(title: str, tracks: list[dict], **kw) -> dict:
    return {"id": "x", "title": title, "privacy": "PUBLIC", "tracks": tracks, **kw}


def book(**cfg) -> VoteBook:
    c = Config(**{**DEFAULTS, **cfg})
    clock = {"t": 1000.0}

    def mono() -> float:
        clock["t"] += 1.0
        return clock["t"]

    return VoteBook(cfg=c, censor=Censor(), rng=lambda: 0.0, mono=mono)


class Clock:
    def __init__(self, t: float = 1_790_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        self.t += 1.0
        return self.t


def importer(b: VoteBook, yt: FakeYT, store=None, **kw) -> Importer:
    b.clock = kw.pop("clock", None) or Clock()
    return Importer(b, store, SimpleNamespace(yt=yt), b.cfg, clock=b.clock, **kw)


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# odkaz → id
# --------------------------------------------------------------------------


class ParseRef(unittest.TestCase):
    def test_url_variants(self):
        ok = {
            f"https://music.youtube.com/playlist?list={PL}": PL,
            f"https://www.youtube.com/playlist?list={PL}": PL,
            f"https://youtube.com/playlist?list={PL}&si=abc123": PL,
            f"https://m.youtube.com/playlist?list={PL}": PL,
            f"https://www.youtube.com/watch?v=dQw4w9WgXcQ&list={PL}&index=3": PL,
            f"https://music.youtube.com/watch?v=dQw4w9WgXcQ&list={PL}": PL,
            f"https://youtu.be/dQw4w9WgXcQ?list={PL}": PL,
            f"https://music.youtube.com/browse/VL{PL}": PL,
            f"tady je můj: https://music.youtube.com/playlist?list={PL} díky": PL,
            PL: PL,
            f"  {PL}  ": PL,
            f"VL{PL}": PL,
            "OLAK5uy_nZXlyjpHvsuEeqWzmUwJgUQYh8yHAaCnE": "OLAK5uy_nZXlyjpHvsuEeqWzmUwJgUQYh8yHAaCnE",
            # výběr YouTube Music (RDCLAK5uy_…) je pevný seznam, ne mix
            "https://music.youtube.com/playlist?list=RDCLAK5uy_kmPRjHDECIcuVwnKsx2Ng7fyNgFKWNJFs":
                "RDCLAK5uy_kmPRjHDECIcuVwnKsx2Ng7fyNgFKWNJFs",
        }
        for text, pid in ok.items():
            with self.subTest(text=text):
                self.assertEqual(parse_ref(text), pid)

    def test_refusals_are_czech_and_specific(self):
        cases = {
            "": "empty",
            "Kabát": "not_a_link",
            "pusť mi něco": "not_a_link",
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ": "video",
            "https://youtu.be/dQw4w9WgXcQ": "video",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDdQw4w9WgXcQ": "mix",
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=RDAMVMdQw4w9WgXcQ": "mix",
            "RDMM": "mix",
            "https://music.youtube.com/playlist?list=LM": "own_list",
            "https://www.youtube.com/playlist?list=LL": "own_list",
            "https://www.youtube.com/playlist?list=WL": "own_list",
            "https://example.com/playlist?x=1": "not_a_link",
        }
        for text, reason in cases.items():
            with self.subTest(text=text):
                with self.assertRaises(ImportRefused) as cm:
                    parse_ref(text)
                self.assertEqual(cm.exception.reason, reason)
                self.assertEqual(cm.exception.status, 400)
        with self.assertRaises(ImportRefused) as cm:
            parse_ref("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDdQw4w9WgXcQ")
        self.assertIn("mix", str(cm.exception))
        self.assertIn("ne playlist", str(cm.exception))


# --------------------------------------------------------------------------
# odpověď ytmusicapi → písničky
# --------------------------------------------------------------------------


class Normalize(unittest.TestCase):
    def test_skips_non_music_and_unavailable(self):
        res = playlist("Moje", [
            item("atv00000001", "Malá dáma", "Kabát"),
            item("omv00000001", "Wonderwall (Official Video)", "Oasis", "MUSIC_VIDEO_TYPE_OMV"),
            # video od fanouška: interpret je v názvu
            item("ugc00000001", "Kabát - Pohoda (videoklip)", "fanda123", "MUSIC_VIDEO_TYPE_UGC"),
            item("gone0000001", "Smazané", "X", avail=False),
            {"videoId": None, "title": "Song deleted", "artists": []},
            item("pod00000001", "Epizoda 12", "Podcast", "MUSIC_VIDEO_TYPE_PODCAST_EPISODE"),
            item("mix00000001", "2 hodiny chillu", "Mixy", dur=7200),
            item("jin00000001", "Znělka", "Rádio", dur=12),
            item("noa00000001", "Bez interpreta", None, "MUSIC_VIDEO_TYPE_UGC"),
        ], trackCount=9)
        f = normalize(res, PL)
        self.assertEqual(f.title, "Moje")
        self.assertEqual((f.seen, f.total, f.unavailable, f.non_music), (9, 9, 2, 4))
        self.assertEqual([(t.id, t.artist, t.title) for t in f.tracks], [
            ("atv00000001", "Kabát", "Malá dáma"),
            ("omv00000001", "Oasis", "Wonderwall"),
            ("ugc00000001", "Kabát", "Pohoda"),
        ])
        # na klíči skladby se video fanouška potká s oficiální nahrávkou
        self.assertEqual(V.song_key("Kabát", "Pohoda"), V.song_key(f.tracks[2].artist, f.tracks[2].title))

    def test_private_or_missing_says_how_to_share(self):
        yt = FakeYT({})
        with self.assertRaises(ImportRefused) as cm:
            run(I.fetch(SimpleNamespace(yt=yt), PL, 100))
        self.assertEqual(cm.exception.status, 404)
        self.assertIn("soukromý", str(cm.exception))
        self.assertIn("Neveřejný (s odkazem)", str(cm.exception))

    def test_network_error_and_timeout(self):
        yt = FakeYT({PL: ConnectionError("Connection aborted")})
        with self.assertRaises(ImportRefused) as cm:
            run(I.fetch(SimpleNamespace(yt=yt), PL, 100))
        self.assertEqual((cm.exception.status, cm.exception.reason), (502, "network"))
        slow = FakeYT({PL: playlist("P", songs("a", 3))}, delay=0.5)
        with self.assertRaises(ImportRefused) as cm:
            run(I.fetch(SimpleNamespace(yt=slow), PL, 100, timeout=0.05))
        self.assertEqual((cm.exception.status, cm.exception.reason), (504, "timeout"))


# --------------------------------------------------------------------------
# import → 👍
# --------------------------------------------------------------------------


class ImportVotes(unittest.TestCase):
    def test_import_is_thumbs_up_under_the_nick_with_source(self):
        b = book()
        yt = FakeYT({PL: playlist("Na práci", songs("a", 5))})
        imp = importer(b, yt)
        rep = run(imp.add(PETR, "Petr", f"https://music.youtube.com/playlist?list={PL}"))
        self.assertEqual((rep["songs"], rep["new"], rep["active"], rep["cut"]), (5, 5, 5, 0))
        self.assertIn("Hotovo: z playlistu ‚Na práci‘ je mezi tvými 👍 5 písniček.", rep["message"])
        key = V.song_key("a band 0", "a song 0")
        self.assertEqual(b.tally(SONG, key).status, FAVOURITE)
        it = b.item(SONG, key, PETR)
        self.assertEqual(it["voters"][0]["nick"], "Petr")
        self.assertEqual(it["voters"][0]["playlist"], "Na práci")
        self.assertTrue(it["voters"][0]["at"])
        self.assertEqual(it["mine"], 1)
        # ytmusicapi běželo ve vlákně, ne v event loopu
        self.assertTrue(yt.threads and all(t != "MainThread" for t in yt.threads))
        # "pusť moje oblíbené" = i písničky z playlistu
        self.assertEqual(len(b.favourite_tracks(PETR)), 5)

    def test_cap_per_import_and_per_person(self):
        b = book(playlist_import_max=5)
        yt = FakeYT({PL: playlist("Velký", songs("a", 8), trackCount=12),
                     "PLsecond000000000000000000000000000": playlist("Druhý", songs("b", 4))})
        imp = importer(b, yt)
        rep = run(imp.add(PETR, "Petr", PL))
        # 8 načteno, 5 se vešlo, 3 ne + 4, které YouTube ani neposlal (12 v playlistu)
        self.assertEqual((rep["songs"], rep["cut"]), (5, 7))
        self.assertIn("7 se nevešlo — limit je 5 písniček z playlistů na člověka", rep["message"])
        self.assertEqual(yt.calls[0], (PL, 5 + I.FETCH_SLACK))
        # limit platí na člověka: další playlist už se nevejde
        with self.assertRaises(ImportRefused) as cm:
            run(imp.add(PETR, "Petr", "PLsecond000000000000000000000000000"))
        self.assertEqual(cm.exception.status, 409)
        self.assertIn("5 písniček", str(cm.exception))
        # jiný člověk má svůj limit
        rep = run(imp.add(JANA, "Jana", "PLsecond000000000000000000000000000"))
        self.assertEqual(rep["songs"], 4)

    def test_duplicates_and_own_votes_win(self):
        b = book()
        dama = Track("dama0000001", "Malá dáma (Official Video)", "Kabát")
        pohoda = Track("pohoda00001", "Pohoda", "Kabát")
        b.cast(SONG, PETR, 1, "Petr", track=dama)  # už má 👍
        b.cast(SONG, PETR, -1, "Petr", track=pohoda)  # vlastní 👎 platí
        yt = FakeYT({PL: playlist("Kabát", [
            item("dama0000009", "Malá dáma", "Kabát"),
            item("dama0000008", "Malá dáma [Live]", "Kabát - Topic"),  # tatáž píseň znovu
            item("pohoda00002", "Pohoda", "Kabát"),
            item("burlaci0001", "Burlaci", "Kabát"),
        ])})
        imp = importer(b, yt)
        rep = run(imp.add(PETR, "Petr", PL))
        self.assertEqual((rep["songs"], rep["doubled"], rep["had"], rep["own_down"], rep["active"]),
                         (3, 1, 1, 1, 1))
        self.assertIn("1 už jsi 👍 měl(a)", rep["message"])
        self.assertIn("u 1 platí tvůj 👎", rep["message"])
        self.assertIn("1 v playlistu dvakrát", rep["message"])
        pk = b.song_key_for(pohoda)
        self.assertEqual(b.items[(SONG, pk)][PETR].vote, -1)  # 👎 zůstal
        self.assertEqual(b.items[(SONG, pk)][PETR].src, "")
        dk = b.song_key_for(dama)
        self.assertEqual(b.items[(SONG, dk)][PETR].src, "")  # vlastní 👍, ne z playlistu
        # tentýž playlist podruhé = obnovení, ne druhý import
        rep = run(imp.add(PETR, "Petr", PL))
        self.assertEqual(rep["action"], "again")
        self.assertEqual(len(b.imports), 1)
        self.assertIn("beze změny", rep["message"])

    def test_explicit_vote_on_imported_song_is_kept_after_removal(self):
        b = book()
        yt = FakeYT({PL: playlist("P", songs("a", 3))})
        imp = importer(b, yt)
        run(imp.add(PETR, "Petr", PL))
        t0 = Track("a0000000000"[:11], "a song 0", "a band 0")
        k0 = b.song_key_for(t0)
        self.assertEqual(b.items[(SONG, k0)][PETR].src != "", True)
        b.cast(SONG, PETR, 1, "Petr", key=k0)  # 👍 znovu, teď vlastní
        self.assertEqual(b.items[(SONG, k0)][PETR].src, "")
        k1 = V.song_key("a band 1", "a song 1")
        b.cast(SONG, PETR, 0, "Petr", key=k1)  # stažení novější než import: platí
        self.assertEqual(b.items[(SONG, k1)][PETR].vote, 0)
        iid = next(iter(b.imports))
        rep = imp.remove(PETR, iid)
        self.assertEqual(rep["removed"], 1)  # jen a song 2 byl čistě z playlistu
        self.assertEqual(b.items[(SONG, k0)][PETR].vote, 1)
        self.assertNotIn((SONG, V.song_key("a band 2", "a song 2")), b.items)

    def test_refresh_adds_and_removes(self):
        b = book()
        tracks = songs("a", 4)
        yt = FakeYT({PL: playlist("P", tracks)})
        imp = importer(b, yt)
        run(imp.add(PETR, "Petr", PL))
        iid = next(iter(b.imports))
        k0 = V.song_key("a band 0", "a song 0")
        added0 = b.imports[iid].items[k0].added
        yt.lists[PL] = playlist("P (nový název)", tracks[:1] + tracks[2:] + songs("n", 2))
        rep = run(imp.refresh(PETR, iid))
        self.assertEqual((rep["new"], rep["gone"], rep["songs"]), (2, 1, 5))
        self.assertIn("obnoven: +2 nové, 1 už v playlistu není (teď 5 písniček)", rep["message"])
        self.assertNotIn((SONG, V.song_key("a band 1", "a song 1")), b.items)
        self.assertEqual(b.items[(SONG, V.song_key("n band 0", "n song 0"))][PETR].vote, 1)
        self.assertEqual(b.imports[iid].items[k0].added, added0)  # písnička zůstala, čas taky
        self.assertEqual(b.imports[iid].title, "P (nový název)")

    def test_remove_withdraws_and_persists(self):
        d = Path(tempfile.mkdtemp(dir=_TMP))
        store = Store(d / "state.db", background=True)
        b = VoteBook(store, Config(**DEFAULTS))
        yt = FakeYT({PL: playlist("P", songs("a", 6))})
        imp = importer(b, yt, store)
        run(imp.add(PETR, "Petr", PL))
        b.cast(SONG, JANA, 1, "Jana", track=Track("x0000000001", "Jiná", "Někdo"))
        store.flush()
        # po restartu: import i jeho 👍 zpátky, vlastní hlasy taky
        b2 = run(VoteBook(store, Config(**DEFAULTS)).aload())
        self.assertEqual(len(b2.imports), 1)
        self.assertEqual(len(b2.favourite_tracks(PETR)), 6)
        self.assertEqual(sorted(b2.lists()["counts"].items()), [("banned", 0), ("favourites", 7),
                                                                ("pending", 0)])
        iid = next(iter(b.imports))
        with self.assertRaises(ImportRefused) as cm:
            imp.remove(JANA, iid)  # cizí playlist ne
        self.assertEqual(cm.exception.status, 403)
        rep = imp.remove(PETR, iid)
        self.assertEqual(rep["removed"], 6)
        self.assertEqual(b.favourite_tracks(PETR), [])
        store.flush()
        b3 = run(VoteBook(store, Config(**DEFAULTS)).aload())
        self.assertEqual((len(b3.imports), len(b3.favourite_tracks(PETR))), (0, 0))
        self.assertEqual(len(b3.favourite_tracks(JANA)), 1)
        store.close()

    def test_colleagues_thumbs_down_still_ban(self):
        b = book()
        yt = FakeYT({PL: playlist("P", songs("a", 3))})
        run(importer(b, yt).add(PETR, "Petr", PL))
        t = Track("a0000000001", "a song 1", "a band 1")
        b.cast(SONG, JANA, -1, "Jana", track=t)
        self.assertNotEqual(b.tally(SONG, b.song_key_for(t)).status, BANNED)  # 1:1
        res = b.cast(SONG, KAREL, -1, "Karel", track=t)
        self.assertEqual(res.changed, "ban")  # 2 lidé 👎 a víc 👎 než 👍
        self.assertNotIn(t.id, [x.id for x in b.favourite_tracks()])
        self.assertNotIn(t.id, [x.id for x in b.favourite_tracks(PETR)])
        self.assertEqual(b.pool_reject(t), "voted_out")

    def test_imports_never_make_a_favourite_artist(self):
        b = book()
        yt = FakeYT({PL: playlist("Kabát", songs("k", 30, "Kabát")),
                     "PLjana00000000000000000000000000000": playlist("Taky Kabát",
                                                                     songs("j", 30, "Kabát"))})
        imp = importer(b, yt)
        run(imp.add(PETR, "Petr", PL))
        run(imp.add(JANA, "Jana", "PLjana00000000000000000000000000000"))
        self.assertEqual(b.favourite_artists(), [])
        self.assertEqual(b.favourite_artists(PETR), [])
        self.assertEqual(b.tally(ARTIST, "kabat").up, 0)
        self.assertNotIn(ARTIST, {t for t, _ in b.items})
        self.assertNotIn("Oblíbení interpreti", b.describe())

    def test_rate_limit_and_one_at_a_time(self):
        b = book()
        yt = FakeYT({PL: playlist("P", songs("a", 2))}, delay=0.2)
        imp = importer(b, yt)

        async def go():
            first = asyncio.create_task(imp.add(PETR, "Petr", PL))
            await asyncio.sleep(0.05)
            with self.assertRaises(ImportRefused) as cm:
                await imp.add(PETR, "Petr", "PLother0000000000000000000000000000")
            self.assertEqual((cm.exception.status, cm.exception.reason), (409, "busy"))
            await first

        run(go())
        yt.delay = 0
        iid = next(iter(b.imports))
        codes = []
        for _ in range(I.RATE_MAX):
            try:
                run(imp.refresh(PETR, iid))
                codes.append(200)
            except ImportRefused as exc:
                codes.append(exc.status)
        self.assertEqual(codes[-1], 429)


# --------------------------------------------------------------------------
# férovost: playlist o 300 písničkách nepřehluší kolegu s dvaceti 👍
# --------------------------------------------------------------------------


class Fairness(unittest.TestCase):
    def office(self) -> VoteBook:
        b = book()
        yt = FakeYT({PL: playlist("Velký", songs("a", 300))})
        run(importer(b, yt).add(PETR, "Petr", PL))
        for i in range(20):
            b.cast(SONG, JANA, 1, "Jana", track=Track(f"j{i:010d}", f"jana {i}", f"jana band {i}"))
            b._rate.clear()
        for i in range(5):
            b.cast(SONG, KAREL, 1, "Karel", track=Track(f"k{i:010d}", f"karel {i}", f"karel band {i}"))
        return b

    @staticmethod
    def owner(t: Track) -> str:
        return {"a": "petr", "j": "jana", "k": "karel"}[t.id[0]]

    def test_each_person_gets_an_equal_share(self):
        b = self.office()
        b.prng.seed(48)
        runs = 400
        first8, first30 = Counter(), Counter()
        for _ in range(runs):
            mix = b.favourite_tracks()
            self.assertEqual(len(mix), 325)
            first8.update(self.owner(t) for t in mix[:8])  # "pusť oblíbené" hraje 8
            first30.update(self.owner(t) for t in mix[:30])
        share8 = {k: v / (8 * runs) for k, v in first8.items()}
        # 3 lidé → po třetině (±3 procentní body); Petr by jinak měl 300/325 = 92 %
        for who in ("petr", "jana", "karel"):
            self.assertAlmostEqual(share8[who], 1 / 3, delta=0.03, msg=share8)
        # Karel má jen 5 → po pěti kolech už dávají jen Petr a Jana
        self.assertEqual(first30["karel"], 5 * runs)
        self.assertAlmostEqual(first30["petr"] / (30 * runs), 12.5 / 30, delta=0.02)
        # různé pořadí pokaždé
        self.assertGreater(len({tuple(t.id for t in b.favourite_tracks()[:8]) for _ in range(20)}), 10)

    def test_order_is_random_not_most_liked_first(self):
        # Pi 6. 10. 2026: "pusť oblíbené" hrálo pokaždé stejně — první skladba stejná ve
        # 30 z 30 spuštění, protože šly napřed ty s nejvíc 👍 a vlastní 👍 před playlistem
        b = self.office()
        shared = Track("j0000000003", "jana 3", "jana band 3")
        b.cast(SONG, KAREL, 1, "Karel", track=shared)
        b.cast(SONG, EVA, 1, "Eva", track=shared)  # 3× 👍
        own = Track("p0000000001", "petrova", "Petrova kapela")
        b.cast(SONG, PETR, 1, "Petr", track=own)
        firsts, own_first, orders = Counter(), 0, set()
        for seed in range(60):
            b.prng.seed(seed)
            mix = b.favourite_tracks()
            firsts[mix[0].id] += 1
            orders.add(tuple(t.id for t in mix[:6]))
            petr = [t for t in mix if t.id[0] in "ap"]
            own_first += petr[0].id == own.id
            self.assertEqual(len({t.id for t in mix}), len(mix))  # nic dvakrát
        self.assertGreater(len(firsts), 8, firsts)  # začíná pokaždé něčím jiným
        self.assertLess(max(firsts.values()), 20, firsts)
        self.assertLess(firsts[shared.id], 20)  # nejoblíbenější není pořád první
        self.assertLess(own_first, 30)  # vlastní 👍 není vždy před playlistem
        self.assertGreater(len(orders), 50)

    def test_a_single_favourite_is_not_always_in_the_first_round(self):
        # dva lidé s jedinou oblíbenou: při prostém střídání by zněla vždy mezi prvními
        b = self.office()
        solo = Track("s0000000001", "jediná", "Sólo kapela")
        b.cast(SONG, "c-solo-00000001", 1, "Solo", track=solo)
        b.cast(SONG, "c-solo-00000002", 1, "Solo2", track=solo)  # 2 👍 = oblíbená kanceláře
        early = 0
        for seed in range(80):
            b.prng.seed(seed)
            ids = [t.id for t in b.favourite_tracks()]
            self.assertIn(solo.id, ids)  # ale zazní v každém kole
            early += ids.index(solo.id) < 5
        self.assertLess(early, 40)

    def test_dj_summary_is_fair_and_stable(self):
        b = self.office()
        favs, _, _ = b.summary(8, 8)
        self.assertEqual(favs, b.summary(8, 8)[0])  # DJ dostane pokaždé stejný text
        owners = Counter(f.split(" — ")[0].split(" ")[0] for f in favs)
        self.assertEqual(sorted(owners.values()), [2, 3, 3], owners)

    def test_pool_boost_capped_for_playlist_only_songs(self):
        b = book()
        yt = FakeYT({PL: playlist("P", songs("a", 40))})
        run(importer(b, yt).add(PETR, "Petr", PL))
        radio = [Track(f"r{i:010d}", f"Song {i}", f"Band {i}", duration=200) for i in range(10)]
        imported = [Track(f"a{i:010d}"[:11], f"a song {i}", f"a band {i}", duration=200)
                    for i in range(12)]
        explicit = Track("e0000000001", "Oblíbená", "Někdo", duration=200)
        b.cast(SONG, JANA, 1, "Jana", track=explicit)
        pool = SimpleNamespace(tracks=__import__("collections").deque(radio + imported + [explicit]))
        moved = b.boost_pools([pool])
        # z 12 písniček jen z Petrova playlistu se posunou 2, vlastní 👍 Jany jako dřív
        self.assertEqual(moved, V.IMPORT_LIFT + 1)
        order = [t.id for t in pool.tracks]
        self.assertLess(order.index(explicit.id), len(order) - 1)
        lifted = [t.id for t in imported if order.index(t.id) < 10 + imported.index(t)]
        self.assertEqual(len(lifted), V.IMPORT_LIFT)

    def test_radio_uses_imported_favourites_like_any_favourite(self):
        b = book()
        yt = FakeYT({PL: playlist("P", songs("a", 3))})
        run(importer(b, yt).add(PETR, "Petr", PL))
        t = Track("a0000000001", "a song 1", "a band 1", duration=200)
        self.assertTrue(b.is_favourite(t))  # 👍 z playlistu = 👍 (F-HLASY-03/05)

        async def radio(video_id, limit=50):
            return [t]

        store = SimpleNamespace(blacklisted=lambda: set(), recently_played=lambda d: {t.id},
                                record_seed=lambda *a: None, recent_history=lambda n: [])
        pools = RadioPools(SimpleNamespace(radio=radio), store, Config(**DEFAULTS))
        pools.votes = b

        async def go():
            await pools.set_seeds([Track("seed0000001", "S", "Seed")])
            return [x.id for x in await pools.next_tracks(3)]

        self.assertIn(t.id, run(go()))  # oblíbená smí i dřív než po repeat_days


# --------------------------------------------------------------------------
# event loop, zápis, API
# --------------------------------------------------------------------------


class LoopNotBlocked(unittest.TestCase):
    def test_import_keeps_the_loop_responsive(self):
        d = Path(tempfile.mkdtemp(dir=_TMP))
        store = Store(d / "state.db", background=True)
        b = VoteBook(store, Config(**DEFAULTS))
        yt = FakeYT({PL: playlist("Velký", songs("a", 360))}, delay=0.4)
        imp = importer(b, yt, store)
        writes = []
        orig = store._run_tx

        def spy(db, statements):
            writes.append(threading.current_thread().name)
            time.sleep(0.2)  # pomalá SD karta
            return orig(db, statements)

        store._run_tx = spy  # type: ignore[method-assign]

        async def go():
            gaps = []
            stop = asyncio.Event()

            async def tick():
                last = time.monotonic()
                while not stop.is_set():
                    await asyncio.sleep(0.01)
                    now = time.monotonic()
                    gaps.append(now - last)
                    last = now

            ticker = asyncio.create_task(tick())
            rep = await imp.add(PETR, "Petr", PL)
            await asyncio.sleep(0.3)
            stop.set()
            await ticker
            return rep, max(gaps)

        rep, worst = run(go())
        self.assertEqual(rep["songs"], min(360, DEFAULTS["playlist_import_max"]))
        # ytmusicapi (0,4 s) ve vlákně, zápis (0,2 s) ve vlákně zápisů Store: smyčka tiká dál
        self.assertLess(worst, 0.15, worst)
        self.assertEqual(writes, ["ytdj-store"])  # jedna transakce, ne 300 zápisů
        store.flush()
        meta, items = store.all_imports()
        self.assertEqual((len(meta), len(items)), (1, min(360, DEFAULTS["playlist_import_max"])))
        store.close()


# --------------------------------------------------------------------------
# úpravy importu: písničky, vyřadit / vrátit, vlastní název (jukebox #31)
# --------------------------------------------------------------------------

PL2 = "PLdruhy0000000000000000000000000000"

# schéma importů, jak je má state.db nasazená před 9. 10. 2026 (bez `label`,
# bez tabulky `import_removed`)
OLD_SCHEMA = """
CREATE TABLE votes (
    target TEXT NOT NULL, key TEXT NOT NULL, voter TEXT NOT NULL, vote INTEGER NOT NULL,
    who TEXT, video_id TEXT, artist TEXT, title TEXT, ts REAL NOT NULL,
    PRIMARY KEY (target, key, voter)
);
CREATE TABLE imports (
    id          TEXT PRIMARY KEY,
    client      TEXT NOT NULL,
    who         TEXT,
    playlist_id TEXT NOT NULL,
    title       TEXT,
    created     REAL NOT NULL,
    fetched     REAL NOT NULL,
    total       INTEGER,
    skipped     INTEGER,
    cut         INTEGER
);
CREATE TABLE import_items (
    import_id TEXT NOT NULL,
    key       TEXT NOT NULL,
    video_id  TEXT,
    artist    TEXT,
    title     TEXT,
    added     REAL NOT NULL,
    pos       INTEGER,
    PRIMARY KEY (import_id, key)
);
"""


def key_of(prefix: str, i: int) -> str:
    return V.song_key(f"{prefix} band {i}", f"{prefix} song {i}")


def up_of(b: VoteBook, who: str) -> set[str]:
    """Klíče písniček, kterým ten člověk právě dává 👍."""
    return {k for (t, k), bs in b.items.items()
            if t == SONG and who in bs and bs[who].vote > 0}


class EditImport(unittest.TestCase):
    def _one(self, n: int = 5, store=None, **cfg):
        b = VoteBook(store, Config(**{**DEFAULTS, **cfg}), censor=Censor())
        yt = FakeYT({PL: playlist("Petrův", songs("a", n))})
        imp = importer(b, yt, store)
        run(imp.add(PETR, "Petr", PL))
        return b, yt, imp, next(iter(b.imports))

    def test_songs_of_an_import_with_state(self):
        b, yt, imp, iid = self._one()
        b.cast(SONG, PETR, 1, "Petr", key=key_of("a", 0))  # vlastní 👍
        b.cast(SONG, PETR, -1, "Petr", key=key_of("a", 1))  # vlastní 👎 platí
        b.cast(SONG, JANA, -1, "Jana", key=key_of("a", 2))
        b.cast(SONG, KAREL, -1, "Karel", key=key_of("a", 2))  # 2× 👎 > 1× 👍: vyřazená
        b.cast(SONG, PETR, 0, "Petr", key=key_of("a", 3))  # stažení novější než import
        imp.set_song(PETR, iid, key_of("a", 4), True)
        # tutéž písničku a0 má Petr i v druhém, mladším playlistu
        yt.lists[PL2] = playlist("Druhý", songs("a", 1) + songs("z", 1))
        run(imp.add(PETR, "Petr", PL2))
        out = imp.songs(iid, JANA)  # otevřít smí každý
        self.assertEqual((out["import"]["mine"], out["import"]["songs"], out["import"]["removed"]),
                         (False, 4, 1))
        rows = out["songs"]
        self.assertEqual([(r["artist"], r["title"]) for r in rows],
                         [(f"a band {i}", f"a song {i}") for i in range(5)])  # vyřazená poslední
        self.assertEqual([r["mine"] for r in rows],
                         ["up", "down", "playlist", "withdrawn", "removed"])
        self.assertEqual([r["removed"] for r in rows], [False] * 4 + [True])
        self.assertEqual((rows[2]["status"], rows[2]["up"], rows[2]["down"]), (BANNED, 1, 2))
        self.assertEqual((rows[0]["status"], rows[1]["status"]), (FAVOURITE, V.DOWN))
        self.assertEqual(imp.songs(iid, PETR)["import"]["mine"], True)
        # druhý playlist: a0 nese Petrův vlastní 👍, z0 playlist sám
        second = next(i.id for i in b.imports.values() if i.playlist_id == PL2)
        self.assertEqual([r["mine"] for r in imp.songs(second)["songs"]], ["up", "playlist"])
        # nic z toho neprozradí id prohlížeče
        self.assertNotIn(PETR, json.dumps(out))
        with self.assertRaises(ImportRefused) as cm:
            imp.songs("neni")
        self.assertEqual(cm.exception.status, 404)

    def test_song_held_by_an_older_playlist_says_which(self):
        b, yt, imp, iid = self._one(3)
        yt.lists[PL2] = playlist("Druhý", songs("a", 2) + songs("z", 1))
        run(imp.add(PETR, "Petr", PL2))
        second = next(i.id for i in b.imports.values() if i.playlist_id == PL2)
        rows = imp.songs(second)["songs"]
        self.assertEqual([(r["mine"], r.get("via")) for r in rows],
                         [("other", "Petrův"), ("other", "Petrův"), ("playlist", None)])

    def test_remove_one_song_and_put_it_back(self):
        b, yt, imp, iid = self._one()
        k1 = key_of("a", 1)
        self.assertEqual(len(up_of(b, PETR)), 5)
        with self.assertRaises(ImportRefused) as cm:
            imp.set_song(JANA, iid, k1, True)  # cizí playlist ne
        self.assertEqual((cm.exception.status, str(cm.exception)),
                         (403, "Upravovat jde jen vlastní playlist."))
        with self.assertRaises(ImportRefused) as cm:
            imp.set_song(PETR, iid, "nikdo|nic", True)
        self.assertEqual(cm.exception.status, 404)
        rep = imp.set_song(PETR, iid, k1, True)
        self.assertEqual((rep["changed"], rep["song"]["mine"], rep["import"]["songs"],
                          rep["import"]["removed"], rep["import"]["active"]),
                         (True, "removed", 4, 1, 4))
        self.assertIn("‚a band 1 — a song 1‘ už z playlistu ‚Petrův‘ tvůj 👍 nemá", rep["message"])
        self.assertNotIn((SONG, k1), b.items)  # 👍 je pryč, nezbyl ani prázdný hlas
        self.assertEqual(up_of(b, PETR), {key_of("a", i) for i in (0, 2, 3, 4)})
        self.assertNotIn(k1, {b.song_key_for(t) for t in b.favourite_tracks(PETR)})
        self.assertEqual(imp.listing(PETR)["mine_songs"], 4)
        # podruhé totéž nic nezmění
        self.assertEqual(imp.set_song(PETR, iid, k1, True)["changed"], False)
        # vrátit
        with self.assertRaises(ImportRefused) as cm:
            imp.set_song(JANA, iid, k1, False)
        self.assertEqual(cm.exception.status, 403)
        rep = imp.set_song(PETR, iid, k1, False)
        self.assertEqual((rep["changed"], rep["song"]["mine"], rep["import"]["songs"],
                          rep["import"]["removed"]), (True, "playlist", 5, 0))
        self.assertIn("je zpátky v playlistu ‚Petrův‘ a má tvůj 👍", rep["message"])
        self.assertEqual(b.items[(SONG, k1)][PETR].src, iid)
        # zpátky na svém místě v pořadí playlistu
        self.assertEqual([r["title"] for r in imp.songs(iid)["songs"]],
                         [f"a song {i}" for i in range(5)])
        self.assertEqual(imp.set_song(PETR, iid, k1, False)["changed"], False)

    def test_removal_keeps_own_vote_other_playlist_and_other_people(self):
        b, yt, imp, iid = self._one()
        k0, k1, k2, k3 = (key_of("a", i) for i in range(4))
        b.cast(SONG, PETR, 1, "Petr", key=k0)  # vlastní 👍
        b.cast(SONG, PETR, -1, "Petr", key=k3)  # vlastní 👎
        b.cast(SONG, JANA, 1, "Jana", key=k2)  # 👍 kolegyně
        yt.lists[PL2] = playlist("Druhý", songs("a", 2))  # a0, a1 i v druhém playlistu
        run(imp.add(PETR, "Petr", PL2))
        second = next(i.id for i in b.imports.values() if i.playlist_id == PL2)
        yt.lists["PLjana00000000000000000000000000000"] = playlist("Janin", songs("a", 3))
        run(imp.add(JANA, "Jana", "PLjana00000000000000000000000000000"))
        # a0: vlastní 👍 zůstává
        rep = imp.set_song(PETR, iid, k0, True)
        self.assertIn("tvůj vlastní 👍 jí zůstal", rep["message"])
        self.assertEqual((b.items[(SONG, k0)][PETR].vote, b.items[(SONG, k0)][PETR].src), (1, ""))
        # a1: 👍 dál dává druhý playlist
        rep = imp.set_song(PETR, iid, k1, True)
        self.assertIn("dál jí ho ale dává playlist ‚Druhý‘", rep["message"])
        self.assertEqual(b.items[(SONG, k1)][PETR].src, second)
        self.assertEqual(len(b.imports[second].items), 2)
        # a2: Petrův 👍 pryč, Janiny (vlastní i z jejího playlistu) beze změny
        imp.set_song(PETR, iid, k2, True)
        self.assertNotIn(PETR, b.items[(SONG, k2)])
        self.assertEqual((b.items[(SONG, k2)][JANA].vote, b.items[(SONG, k2)][JANA].src), (1, ""))
        self.assertEqual(b.items[(SONG, k1)][JANA].vote, 1)
        self.assertEqual(b.tally(SONG, k2).up, 1)
        # a3: vlastní 👎 zůstává při vyřazení i po vrácení
        imp.set_song(PETR, iid, k3, True)
        self.assertEqual(b.items[(SONG, k3)][PETR].vote, -1)
        rep = imp.set_song(PETR, iid, k3, False)
        self.assertEqual(b.items[(SONG, k3)][PETR].vote, -1)
        self.assertIn("platí ale tvůj vlastní 👎", rep["message"])
        # odebrání celého druhého playlistu vyřazenou a1 nevrátí
        imp.remove(PETR, second)
        self.assertNotIn(PETR, b.items.get((SONG, k1), {}))

    def test_put_back_beats_an_older_own_withdrawal(self):
        b, yt, imp, iid = self._one()
        k1 = key_of("a", 1)
        b.cast(SONG, PETR, 0, "Petr", key=k1)  # stáhl si 👍 z playlistu
        self.assertEqual(b.items[(SONG, k1)][PETR].vote, 0)
        imp.set_song(PETR, iid, k1, True)
        rep = imp.set_song(PETR, iid, k1, False)  # vrátil ji výslovně: 👍 zase platí
        self.assertEqual((rep["song"]["mine"], b.items[(SONG, k1)][PETR].vote), ("playlist", 1))

    def test_removed_song_survives_refresh_restart_and_reappearing(self):
        d = Path(tempfile.mkdtemp(dir=_TMP))
        store = Store(d / "state.db", background=True)
        b, yt, imp, iid = self._one(4, store)
        k1, k2 = key_of("a", 1), key_of("a", 2)
        tracks = songs("a", 4)
        imp.set_song(PETR, iid, k1, True)
        imp.set_song(PETR, iid, k2, True)
        imp.set_song(PETR, iid, k2, False)  # a2 zase vrácená
        # Obnovit: a1 v playlistu pořád je, ale zpátky se nedostane
        rep = run(imp.refresh(PETR, iid))
        self.assertEqual((rep["songs"], rep["new"], rep["gone"], rep["kept_out"]), (3, 0, 0, 1))
        self.assertIn("1 vyřazená zůstává mimo", rep["message"])
        self.assertNotIn((SONG, k1), b.items)
        # z YouTube zmizí…
        yt.lists[PL] = playlist("Petrův", [tracks[0], tracks[3]])
        rep = run(imp.refresh(PETR, iid))
        self.assertEqual((rep["songs"], rep["kept_out"]), (2, 0))
        self.assertIn(k1, b.imports[iid].removed)
        # …a zase se objeví (jako jiná nahrávka téže písničky): pořád vyřazená
        again = item("jine0000001", "a song 1 (Remastered 2011)", "a band 1")
        yt.lists[PL] = playlist("Petrův", [tracks[0], again, tracks[2], tracks[3]])
        rep = run(imp.refresh(PETR, iid))
        self.assertEqual((rep["songs"], rep["new"], rep["kept_out"]), (3, 1, 1))
        self.assertNotIn((SONG, k1), b.items)
        self.assertEqual(up_of(b, PETR), {key_of("a", i) for i in (0, 2, 3)})
        # tentýž odkaz vložený znovu = obnovení: taky ne
        rep = run(imp.add(PETR, "Petr", PL))
        self.assertEqual((rep["action"], rep["songs"]), ("again", 3))
        # restart
        store.flush()
        b2 = run(VoteBook(store, Config(**DEFAULTS)).aload())
        self.assertEqual(up_of(b2, PETR), {key_of("a", i) for i in (0, 2, 3)})
        self.assertEqual(list(b2.imports[iid].removed), [k1])
        self.assertEqual(b2.imports[iid].removed[k1].title, "a song 1")
        self.assertEqual([it.title for it in b2.imports[iid].items.values()],
                         ["a song 0", "a song 2", "a song 3"])
        # rozjezd DJe čte importy přímo ze Store: vyřazená mezi oblíbenými není
        meta, items = store.all_imports()
        self.assertEqual(sorted(r[1] for r in items), sorted(key_of("a", i) for i in (0, 2, 3)))
        # vrátit po restartu a znovu restart: je zpátky
        imp2 = importer(b2, yt, store)
        imp2.set_song(PETR, iid, k1, False)
        store.flush()
        b3 = run(VoteBook(store, Config(**DEFAULTS)).aload())
        self.assertEqual(len(up_of(b3, PETR)), 4)
        self.assertEqual(b3.imports[iid].removed, {})
        # odebrání celého playlistu uklidí i vyřazené
        imp2.set_song(PETR, iid, k1, True)
        imp2.remove(PETR, iid)
        store.flush()
        self.assertEqual((store.all_imports(), store.import_removed_rows()), (([], []), []))
        store.close()

    def test_custom_name_survives_refresh_and_restart(self):
        d = Path(tempfile.mkdtemp(dir=_TMP))
        store = Store(d / "state.db", background=True)
        b, yt, imp, iid = self._one(3, store)
        k0 = key_of("a", 0)
        with self.assertRaises(ImportRefused) as cm:
            imp.rename(JANA, iid, "Janin")  # cizí ne
        self.assertEqual((cm.exception.status, str(cm.exception)),
                         (403, "Přejmenovat jde jen vlastní playlist."))
        rep = imp.rename(PETR, iid, "  Do   práce \n")
        self.assertEqual((rep["changed"], rep["import"]["title"], rep["import"]["yt_title"],
                          rep["import"]["custom"]), (True, "Do práce", "Petrův", True))
        self.assertIn("‚Petrův‘ se teď jmenuje ‚Do práce‘", rep["message"])
        # u hlasu „z playlistu ‚Do práce‘" i v přehledu pro DJe
        self.assertEqual(b.item(SONG, k0)["voters"][0]["playlist"], "Do práce")
        self.assertIn("‚Do práce‘", b.favourites_overview(PETR))
        # Obnovit název z YouTube vezme na vědomí, ale vlastní nepřepíše
        yt.lists[PL] = playlist("Petrův (nový)", songs("a", 3))
        rep = run(imp.refresh(PETR, iid))
        self.assertEqual((rep["title"], rep["import"]["title"], rep["import"]["yt_title"]),
                         ("Do práce", "Do práce", "Petrův (nový)"))
        self.assertIn("Playlist ‚Do práce‘ obnoven", rep["message"])
        self.assertEqual(b.item(SONG, k0)["voters"][0]["playlist"], "Do práce")
        # restart
        store.flush()
        b2 = run(VoteBook(store, Config(**DEFAULTS)).aload())
        self.assertEqual((b2.imports[iid].name, b2.imports[iid].title),
                         ("Do práce", "Petrův (nový)"))
        self.assertEqual(b2.item(SONG, k0)["voters"][0]["playlist"], "Do práce")
        # slušnost, délka, řídicí znaky
        for bad, why in (("kurva playlist", "neobstál"), ("x" * 61, "nejvýš 60 znaků")):
            with self.assertRaises(ImportRefused) as cm:
                imp.rename(PETR, iid, bad)
            self.assertEqual(cm.exception.status, 400)
            self.assertIn(why, str(cm.exception))
        self.assertEqual(imp.rename(PETR, iid, "Rock​\x07 <b>")["import"]["title"], "Rock <b>")
        # stejný název podruhé nic nemění; prázdný vrací název z YouTube
        self.assertEqual(imp.rename(PETR, iid, "Rock <b>")["changed"], False)
        rep = imp.rename(PETR, iid, "")
        self.assertEqual((rep["import"]["title"], rep["import"]["custom"]), ("Petrův (nový)", False))
        self.assertEqual(b.item(SONG, k0)["voters"][0]["playlist"], "Petrův (nový)")
        # název stejný jako na YouTube není vlastní: příští Obnovit ho zase sleduje
        self.assertEqual(imp.rename(PETR, iid, "Petrův (nový)")["changed"], False)
        store.flush()
        b3 = run(VoteBook(store, Config(**DEFAULTS)).aload())
        self.assertEqual((b3.imports[iid].label, b3.imports[iid].name), ("", "Petrův (nový)"))
        store.close()

    def test_old_database_migrates_without_loss(self):
        import sqlite3

        d = Path(tempfile.mkdtemp(dir=_TMP))
        path = d / "state.db"
        db = sqlite3.connect(path)
        db.executescript(OLD_SCHEMA)
        db.execute("INSERT INTO imports VALUES(?,?,?,?,?,?,?,?,?,?)",
                   ("imp1", PETR, "Petr", PL, "Petrův", 100.0, 200.0, 5, 1, 0))
        db.execute("INSERT INTO imports VALUES(?,?,?,?,?,?,?,?,?,?)",
                   ("imp2", JANA, "Jana", PL2, "Janin", 150.0, 150.0, None, None, None))
        rows = [("imp1", key_of("a", i), f"a{i:010d}", f"a band {i}", f"a song {i}", 100.0 + i, i)
                for i in range(4)]
        rows += [("imp2", key_of("j", i), f"j{i:010d}", f"j band {i}", f"j song {i}", 150.0, i)
                 for i in range(2)]
        db.executemany("INSERT INTO import_items VALUES(?,?,?,?,?,?,?)", rows)
        db.execute("INSERT INTO votes VALUES(?,?,?,?,?,?,?,?,?)",
                   (SONG, key_of("a", 3), PETR, -1, "Petr", "a0000000003", "a band 3", "a song 3", 300.0))
        db.commit()
        db.close()

        store = Store(path, background=True)  # tady proběhne migrace
        cols = [r[1] for r in store._read("PRAGMA table_info(imports)")]
        self.assertEqual(cols[:10], ["id", "client", "who", "playlist_id", "title", "created",
                                     "fetched", "total", "skipped", "cut"])
        self.assertIn("label", cols)
        self.assertEqual(store.import_removed_rows(), [])
        meta, items = store.all_imports()
        self.assertEqual([m[:10] for m in meta],
                         [("imp1", PETR, "Petr", PL, "Petrův", 100.0, 200.0, 5, 1, 0),
                          ("imp2", JANA, "Jana", PL2, "Janin", 150.0, 150.0, 0, 0, 0)])
        self.assertEqual(sorted(items), sorted(rows))
        b = run(VoteBook(store, Config(**DEFAULTS)).aload())
        self.assertEqual({i: (x.name, x.label, len(x.items), len(x.removed))
                          for i, x in b.imports.items()},
                         {"imp1": ("Petrův", "", 4, 0), "imp2": ("Janin", "", 2, 0)})
        self.assertEqual(up_of(b, PETR), {key_of("a", i) for i in range(3)})  # a3: vlastní 👎
        self.assertEqual(b.items[(SONG, key_of("a", 3))][PETR].vote, -1)
        self.assertEqual(b.imports["imp1"].items[key_of("a", 2)].added, 102.0)
        # nové úpravy nad starými daty fungují a přežijí další start
        imp = importer(b, FakeYT({PL: playlist("Petrův", songs("a", 4))}), store)
        imp.rename(PETR, "imp1", "Staré dobré")
        imp.set_song(PETR, "imp1", key_of("a", 0), True)
        run(imp.refresh(PETR, "imp1"))
        store.flush()
        store.close()
        store = Store(path, background=True)  # druhý start: migrace podruhé nic nerozbije
        b2 = run(VoteBook(store, Config(**DEFAULTS)).aload())
        self.assertEqual((b2.imports["imp1"].name, list(b2.imports["imp1"].removed)),
                         ("Staré dobré", [key_of("a", 0)]))
        self.assertEqual(up_of(b2, PETR), {key_of("a", 1), key_of("a", 2)})
        self.assertEqual((b2.imports["imp1"].created, len(b2.imports["imp2"].items)), (100.0, 2))
        self.assertEqual(up_of(b2, JANA), {key_of("j", 0), key_of("j", 1)})
        store.close()

    def test_edits_are_small_writes_off_the_loop_and_limited(self):
        d = Path(tempfile.mkdtemp(dir=_TMP))
        store = Store(d / "state.db", background=True)
        b, yt, imp, iid = self._one(200, store)
        store.flush()
        writes = []
        orig_tx = store._run_tx

        def spy_tx(db, statements):
            writes.append((threading.current_thread().name,
                           sum(len(p) if isinstance(p, list) else 1 for _s, p in statements)))
            time.sleep(0.2)  # pomalá SD karta
            return orig_tx(db, statements)

        store._run_tx = spy_tx  # type: ignore[method-assign]

        async def go():
            gaps = []
            stop = asyncio.Event()

            async def tick():
                last = time.monotonic()
                while not stop.is_set():
                    await asyncio.sleep(0.01)
                    now = time.monotonic()
                    gaps.append(now - last)
                    last = now

            ticker = asyncio.create_task(tick())
            imp.set_song(PETR, iid, key_of("a", 7), True)
            imp.set_song(PETR, iid, key_of("a", 7), False)
            imp.rename(PETR, iid, "Dlouhý")
            imp.songs(iid, PETR)
            await asyncio.sleep(0.6)
            stop.set()
            await ticker
            return max(gaps)

        worst = run(go())
        self.assertLess(worst, 0.15, worst)
        store.flush()
        # jedna písnička = jedna malá transakce (dva řádky), ne celý playlist znovu
        self.assertEqual(writes, [("ytdj-store", 2), ("ytdj-store", 2)])
        store._run_tx = orig_tx  # type: ignore[method-assign]
        self.assertEqual(store._read("SELECT label FROM imports"), [("Dlouhý",)])
        self.assertEqual(len(store.all_imports()[1]), 200)
        # pojistka: 120 úprav za 10 min, pak 429 (už provedené: 3)
        for i in range(I.EDIT_MAX - 3):
            imp.set_song(PETR, iid, key_of("a", i), True)
        with self.assertRaises(ImportRefused) as cm:
            imp.set_song(PETR, iid, key_of("a", 150), True)
        self.assertEqual((cm.exception.status, str(cm.exception)),
                         (429, "Moc úprav najednou — zkus to prosím za pár minut."))
        with self.assertRaises(ImportRefused) as cm:
            imp.rename(PETR, iid, "Ještě jinak")
        self.assertEqual(cm.exception.status, 429)
        self.assertIn(key_of("a", 150), b.imports[iid].items)
        self.assertEqual(b.imports[iid].name, "Dlouhý")
        store.close()

    def test_put_back_respects_the_limit_per_person(self):
        b, yt, imp, iid = self._one(3, playlist_import_max=3)
        k0 = key_of("a", 0)
        imp.set_song(PETR, iid, k0, True)  # uvolní místo…
        yt.lists[PL2] = playlist("Druhý", songs("z", 1))
        run(imp.add(PETR, "Petr", PL2))  # …a to zabere jiný playlist
        with self.assertRaises(ImportRefused) as cm:
            imp.set_song(PETR, iid, k0, False)
        self.assertEqual(cm.exception.status, 409)
        self.assertIn("Z playlistů už máš 3 písniček", str(cm.exception))
        self.assertIn(k0, b.imports[iid].removed)

    def test_edits_are_logged_without_client_ids(self):
        from unittest import mock

        b, yt, imp, iid = self._one(3)
        with mock.patch.object(I.telemetry, "event") as ev:
            imp.set_song(PETR, iid, key_of("a", 1), True)
            imp.set_song(PETR, iid, key_of("a", 1), False)
            imp.rename(PETR, iid, "Do práce")
        calls = [(c.args[0], c.kwargs) for c in ev.call_args_list]
        self.assertEqual([(k, f["action"]) for k, f in calls],
                         [("vote.import_edit", "song_remove"), ("vote.import_edit", "song_restore"),
                          ("vote.import_edit", "rename")])
        self.assertEqual((calls[0][1]["song"], calls[0][1]["songs"], calls[0][1]["removed"]),
                         ("a band 1 — a song 1", 2, 1))
        self.assertEqual((calls[2][1]["title"], calls[2][1]["custom"]), ("Do práce", True))
        for _k, f in calls:
            self.assertEqual(f["voter"], PETR[-6:])  # jen konec id, jako u vote.import
            self.assertNotIn(PETR, json.dumps(f))


class Api(unittest.TestCase):
    def _server(self):
        from test_votes import FakePlayer, request

        self.request = request
        player = FakePlayer()
        wq = tw.WishQueue(SimpleNamespace(), player, SimpleNamespace(), None, Config(**DEFAULTS))
        wq.nicks.set(PETR, "Petr")
        wq.nicks.set(JANA, "Jana")
        yt = FakeYT({PL: playlist("Petrův", songs("a", 4)),
                     "PLjana00000000000000000000000000000": playlist("Janin", songs("j", 2))})
        app = SimpleNamespace(player=player, wishes=wq, store=None, cfg=Config(**DEFAULTS),
                              pools=SimpleNamespace(describe=lambda: "", mood="", artist=""),
                              dj=None, catalog=SimpleNamespace(yt=yt))
        V.wire(app)
        srv = web.WebServer(app)
        h = {}
        for r in srv._starlette.routes:
            for m in getattr(r, "methods", None) or ():
                h[(r.path, m)] = r.endpoint
        return srv, app, h

    def req(self, body=None, query=None, iid=""):
        r = self.request(body, query)
        r.path_params = {"iid": iid}
        return r

    def test_only_own_imports_can_be_managed(self):
        async def go():
            srv, app, h = self._server()
            add = h[("/api/votes/imports", "POST")]
            lst = h[("/api/votes/imports", "GET")]
            refresh = h[("/api/votes/imports/{iid}/refresh", "POST")]
            remove = h[("/api/votes/imports/{iid}", "DELETE")]
            # bez přezdívky ne (vždy musí být jasné, čí 👍 to jsou)
            resp = await add(self.req({"client": KAREL, "url": PL}))
            self.assertEqual(resp.status_code, 403)
            resp = await add(self.req({"client": PETR, "url": "https://youtu.be/dQw4w9WgXcQ"}))
            self.assertEqual(resp.status_code, 400)
            resp = await add(self.req({"client": PETR, "url": "PLprivate00000000000000000000000000"}))
            self.assertEqual(resp.status_code, 404)
            self.assertIn("Neveřejný (s odkazem)", json.loads(resp.body)["error"])
            resp = await add(self.req({"client": PETR, "url": f"https://music.youtube.com/playlist?list={PL}"}))
            out = json.loads(resp.body)
            self.assertEqual((resp.status_code, out["songs"]), (200, 4))
            iid = out["import"]["id"]
            await add(self.req({"client": JANA, "url": "PLjana00000000000000000000000000000"}))
            # všichni vidí všechny importy; id klienta nikde
            body = (await lst(self.req(query={"client": JANA}))).body
            data = json.loads(body)
            self.assertEqual([(i["who"], i["mine"], i["songs"]) for i in data["imports"]],
                             [("Jana", True, 2), ("Petr", False, 4)])
            self.assertNotIn(PETR.encode(), body)
            self.assertEqual((data["max"], data["mine_songs"]), (DEFAULTS["playlist_import_max"], 2))
            # cizí: obnovit ani odebrat ne
            resp = await refresh(self.req({"client": JANA}, iid=iid))
            self.assertEqual(resp.status_code, 403)
            resp = await remove(self.req({"client": JANA}, iid=iid))
            self.assertEqual(resp.status_code, 403)
            self.assertIn(iid, app.votes.imports)
            # vlastní ano
            resp = await refresh(self.req({"client": PETR}, iid=iid))
            self.assertEqual(resp.status_code, 200)
            resp = await remove(self.req({"client": PETR}, iid=iid))
            self.assertEqual((resp.status_code, json.loads(resp.body)["removed"]), (200, 4))
            self.assertNotIn(iid, app.votes.imports)
            resp = await remove(self.req({"client": PETR}, iid=iid))
            self.assertEqual(resp.status_code, 404)
            # stav webu zůstává malý: importy v něm nejsou
            snap = await srv._snapshot()
            self.assertNotIn("imports", json.dumps(snap))
            # seznamy hlasování: 👍 z playlistu se značkou zdroje, v "moje" ne
            lists = json.loads((await h[("/api/votes", "GET")](self.req(query={"client": JANA}))).body)
            self.assertEqual(lists["counts"]["favourites"], 2)
            self.assertEqual(lists["favourites"][0]["voters"][0]["playlist"], "Janin")
            self.assertEqual(lists["mine"], [])

        tw.run(go())

    def test_songs_are_public_edits_only_for_the_owner(self):
        async def go():
            srv, app, h = self._server()
            add = h[("/api/votes/imports", "POST")]
            songs_get = h[("/api/votes/imports/{iid}/songs", "GET")]
            song = h[("/api/votes/imports/{iid}/songs", "POST")]
            rename = h[("/api/votes/imports/{iid}/rename", "POST")]
            out = json.loads((await add(self.req({"client": PETR, "url": PL}))).body)
            iid = out["import"]["id"]
            k1 = V.song_key("a band 1", "a song 1")
            # písničky playlistu si otevře každý (i bez přezdívky), id klienta v nich není
            resp = await songs_get(self.req(query={"client": KAREL}, iid=iid))
            data = json.loads(resp.body)
            self.assertEqual((resp.status_code, len(data["songs"]), data["import"]["mine"]),
                             (200, 4, False))
            self.assertEqual({r["mine"] for r in data["songs"]}, {"playlist"})
            self.assertNotIn(PETR.encode(), resp.body)
            self.assertEqual((await songs_get(self.req(query={}, iid="neni"))).status_code, 404)
            # upravovat: bez přezdívky ne, cizí ne, bez "removed" ne
            body = {"client": KAREL, "key": k1, "removed": True}
            self.assertEqual((await song(self.req(body, iid=iid))).status_code, 403)
            resp = await song(self.req({**body, "client": JANA}, iid=iid))
            self.assertEqual((resp.status_code, json.loads(resp.body)["error"]),
                             (403, "Upravovat jde jen vlastní playlist."))
            resp = await rename(self.req({"client": JANA, "label": "Janin"}, iid=iid))
            self.assertEqual(resp.status_code, 403)
            resp = await song(self.req({"client": PETR, "key": k1}, iid=iid))
            self.assertEqual(resp.status_code, 400)
            resp = await song(self.req({**body, "client": PETR, "key": "nikdo|nic"}, iid=iid))
            self.assertEqual(resp.status_code, 404)
            self.assertEqual(len(app.votes.imports[iid].items), 4)
            # vlastní ano: 👍 zmizí, úklid podkresu proběhne jako u hlasu
            resp = await song(self.req({**body, "client": PETR}, iid=iid))
            out = json.loads(resp.body)
            self.assertEqual((resp.status_code, out["song"]["mine"], out["import"]["songs"],
                              out["import"]["removed"]), (200, "removed", 3, 1))
            self.assertIn("effect", out)
            self.assertNotIn(("song", k1), app.votes.items)
            lists = json.loads((await h[("/api/votes", "GET")](self.req(query={"client": PETR}))).body)
            self.assertEqual(lists["counts"]["favourites"], 3)
            data = json.loads((await songs_get(self.req(query={"client": PETR}, iid=iid))).body)
            self.assertEqual([(r["title"], r["removed"]) for r in data["songs"]][-1],
                             ("a song 1", True))
            # vrátit
            resp = await song(self.req({**body, "client": PETR, "removed": False}, iid=iid))
            out = json.loads(resp.body)
            self.assertEqual((resp.status_code, out["song"]["mine"], out["import"]["songs"]),
                             (200, "playlist", 4))
            self.assertNotIn("effect", out)
            # vlastní název: v seznamu playlistů i u hlasu; filtr slov z nastavení
            resp = await rename(self.req({"client": PETR, "label": "Do práce"}, iid=iid))
            self.assertEqual((resp.status_code, json.loads(resp.body)["import"]["title"]),
                             (200, "Do práce"))
            resp = await rename(self.req({"client": PETR, "label": "hovno"}, iid=iid))
            self.assertEqual(resp.status_code, 400)
            lst = json.loads((await h[("/api/votes/imports", "GET")](
                self.req(query={"client": JANA}))).body)
            self.assertEqual([(i["title"], i["yt_title"], i["custom"]) for i in lst["imports"]],
                             [("Do práce", "Petrův", True)])
            lists = json.loads((await h[("/api/votes", "GET")](self.req(query={"client": JANA}))).body)
            self.assertEqual(lists["favourites"][0]["voters"][0]["playlist"], "Do práce")
            # stav webu zůstává malý
            self.assertNotIn("Do práce", json.dumps(await srv._snapshot()))

        tw.run(go())


if __name__ == "__main__":
    unittest.main()
