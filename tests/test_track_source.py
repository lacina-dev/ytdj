"""Odkud skladba je: YouTube Music, nebo video přímo z YouTube (F-WEB-12, F-TV-21).

    YTDJ_EVENTS_FILE=/tmp/x.jsonl .venv/bin/python -m unittest tests.test_track_source -v

Vlastník 3. 11. 2026: „Jen chci, aby písnička, která není z YT Music, ale je
přímo z YouTube, byla označena. A možná označujme i YT Music." Zdroj se
pozná podle druhu, který uvádí YouTube Music (`videoType`); kde druh není,
odznak není (nic se nehádá).

Nahrávka tests/fixtures/ytm_source.json jsou skutečné odpovědi ytmusicapi
(anonymně, anglický katalog) z 3. 11. 2026: hledání skladeb a videí „Kabát
Pohoda", hledání videí „Tleskač Zelená je tráva", rádio, profil Kabátu
a jedna nahrávka posluchače — jen s poli, která katalog čte.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if not os.environ.get("YTDJ_EVENTS_FILE"):
    _EVENTS_TMP = tempfile.TemporaryDirectory(prefix="ytdj-source-tests-")
    os.environ["YTDJ_EVENTS_FILE"] = str(Path(_EVENTS_TMP.name) / "events.jsonl")

from test_music_match import load as load_ytm  # noqa: E402
from test_music_match import replay_catalog  # noqa: E402

from ytdj import imports  # noqa: E402
from ytdj import wishes as wishes_mod  # noqa: E402
from ytdj.music import source as src_mod  # noqa: E402
from ytdj.music.catalog import ATV, OMV, UGC, Track, to_track  # noqa: E402
from ytdj.music.source import YT, YTM, source  # noqa: E402
from ytdj.player.mpv import MpvPlayer  # noqa: E402
from ytdj.state import Store  # noqa: E402
from ytdj.web import server as web  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ytm_source.json"
_TMP = tempfile.mkdtemp(prefix="ytdj-source-")


def items(label: str) -> list[dict]:
    return json.loads(FIXTURE.read_text())["items"][label]


def run(coro):
    return asyncio.run(coro)


class Classification(unittest.TestCase):
    """Jedno malé pravidlo: druh → ytm / yt / nic."""

    def test_mapping(self):
        self.assertEqual(source(ATV), YTM)
        self.assertEqual(source(OMV), YTM)
        self.assertEqual(source("MUSIC_VIDEO_TYPE_OFFICIAL_SOURCE_MUSIC"), YTM)
        self.assertEqual(source(UGC), YT)
        self.assertEqual(source("MUSIC_VIDEO_TYPE_PRIVATELY_OWNED_TRACK"), YT)
        for unknown in (None, "", 0, "MUSIC_VIDEO_TYPE_PODCAST_EPISODE"):
            self.assertIsNone(source(unknown), unknown)

    def test_real_answers_of_youtube_music(self):
        """Skladby, oficiální klip, nahrávka posluchače, rádio, profil — jak je dnes vrací YTM."""
        got = {label: [(t.id, source(t.video_type)) for t in map(to_track, items(label))]
               for label in ("songs", "videos", "tleskac", "radio", "artist_top", "artist_playlist",
                             "watch_one_ugc")}
        self.assertTrue(all(s == YTM for _, s in got["songs"]))
        self.assertEqual(got["videos"][:2], [("tknsoxpQ-Kw", YTM), ("yhnowRl4HTY", YTM)])  # klipy
        self.assertEqual(got["videos"][3], ("ezR1jsiZIhk", YT))  # „Kabát - Pohoda" od posluchače
        self.assertTrue(all(s == YT for _, s in got["tleskac"]))  # Tleskač jen od posluchačů
        self.assertEqual(got["watch_one_ugc"], [("fdVHXjZrfEY", YT)])  # i jedna skladba (odkaz)
        for label in ("radio", "artist_top", "artist_playlist"):
            self.assertTrue(got[label] and all(s == YTM for _, s in got[label]), label)

    def test_web_and_tv_need_no_catalog(self):
        """Pravidlo je samostatné: obrazovka telky ani web kvůli němu nenačítají katalog."""
        text = Path(src_mod.__file__).read_text()
        self.assertNotRegex(text, r"^(from|import) (?!__future__)", re.M)


class EveryPathKeepsTheType(unittest.TestCase):
    """Každá cesta, kterou skladba přijde, si druh nese až na web."""

    def test_song_search_artist_profile_and_video_fallback(self):
        # skladba z katalogu skladeb
        data = load_ytm("lucie_cerni_andele")
        cat, _ = replay_catalog(data["calls"])
        t = run(cat.search_song(data["artist"], data["title"]))
        self.assertEqual(source(t.video_type), YTM)
        # skladba existuje jen jako cizí video (krok 3 hledání: videa)
        data = load_ytm("monkey_business_piece_of_my_life")
        cat, _ = replay_catalog(data["calls"])
        t = run(cat.search_song(data["artist"], data["title"]))
        self.assertEqual((t.id, t.video_type, source(t.video_type)), (data["resolved"], UGC, YT))
        # profil interpreta (playlist Songs)
        data = load_ytm("artist_midi_lidi")
        cat, _ = replay_catalog(data["calls"])
        got = run(cat.artist_tracks(data["artist"], limit=data["limit"]))
        self.assertTrue(got and all(source(t.video_type) == YTM for t in got))

    def test_band_only_on_youtube_as_listener_uploads(self):
        cat, _ = replay_catalog(json.loads(
            (FIXTURE.parent / "ytm_uploads" / "tleskac.json").read_text())["calls"])
        got = run(cat.artist_uploads("Tleskač"))
        self.assertTrue(got and all(source(t.video_type) == YT for t in got))

    def test_imported_playlist_keeps_the_type_of_uploads(self):
        res = {"title": "mix", "tracks": [items("songs")[0], items("tleskac")[0]]}
        out = imports.normalize(res, "PLx")
        self.assertEqual([source(t.video_type) for t in out.tracks], [YTM, YT])

    def test_wishes_and_background_survive_a_restart_with_the_type(self):
        t = Track("fdVHXjZrfEY", "Zelená je tráva", "Tleskač", video_type=UGC)
        back = wishes_mod._track_from(json.loads(json.dumps(wishes_mod._track_json(t))))
        self.assertEqual(back.video_type, UGC)
        # bez druhu se nic nepřidá (stav na kartě zůstane stejně malý)
        self.assertNotIn("video_type", wishes_mod._track_json(Track("x" * 11, "t", "a")))

    def test_playing_track_restored_after_restart_keeps_the_type(self):
        p = MpvPlayer.__new__(MpvPlayer)
        t = Track("fdVHXjZrfEY", "Zelená je tráva", "Tleskač", duration=200, video_type=UGC)
        p._tracks, p._current_id, p._cur_entry = {t.id: t}, t.id, object()
        p._time_pos, p._paused = 50.0, False
        path = Path(tempfile.mkdtemp(dir=_TMP)) / "playback.json"
        path.write_text(json.dumps(p._playback_state()))
        p.playback_file, p.playback_disk = path, None
        track, _ = p.saved_playback(900)
        self.assertEqual(track.video_type, UGC)

    def test_history_remembers_the_type_and_old_database_migrates(self):
        db = Path(tempfile.mkdtemp(dir=_TMP)) / "state.db"
        old = sqlite3.connect(db)
        old.executescript("""
            CREATE TABLE plays (video_id TEXT NOT NULL, title TEXT, artist TEXT, ts REAL NOT NULL,
                seed_id TEXT, outcome TEXT NOT NULL DEFAULT 'started');
            INSERT INTO plays VALUES ('starastara1','Stará','Kabát',1.0,NULL,'finished');
        """)
        old.commit()
        old.close()
        store = Store(db)
        store.record_start("fdVHXjZrfEY", "Zelená je tráva", "Tleskač", None, UGC)
        store.record_start("5ICWhbAmIaw", "Pohoda", "Kabát", None, ATV)
        store.flush()
        rows = store.recent_history(5)
        self.assertEqual([(r.video_id, r.video_type) for r in rows],
                         [("5ICWhbAmIaw", ATV), ("fdVHXjZrfEY", UGC), ("starastara1", None)])
        self.assertEqual(store.video_types(["fdVHXjZrfEY", "starastara1", "neznámé"]),
                         {"fdVHXjZrfEY": UGC})


# ---- stav pro web a telku ----


class FakePlayer:
    def __init__(self, current, queue) -> None:
        self.current, self.queue = current, queue

    async def status(self):
        return SimpleNamespace(playing=True, paused=False, buffering=False, current=self.current,
                               position=10.0, duration=232.0, queue=self.queue, volume=50,
                               quality="")


def app_with(current, queue, history=(), types=None):
    asked: list[list[str]] = []

    def video_types(ids):
        asked.append(list(ids))
        return {k: v for k, v in (types or {}).items() if k in ids}

    store = SimpleNamespace(recent_history=lambda n: list(history), video_types=video_types)
    app = SimpleNamespace(player=FakePlayer(current, queue),
                          pools=SimpleNamespace(describe=lambda: "", mood=""),
                          store=store, codex_busy=False)
    return app, asked


POHODA = Track("5ICWhbAmIaw", "Pohoda", "Kabát", duration=232, video_type=ATV)
ZELENA = Track("fdVHXjZrfEY", "Zelená je tráva", "Tleskač", duration=190, video_type=UGC)
BEZ = Track("bezdruhu001", "Neznámá", "Někdo", duration=200)


class Payload(unittest.TestCase):
    def snap(self, app):
        return run(web.WebServer(app)._snapshot())

    def test_current_queue_and_history_say_where_from_and_unknown_says_nothing(self):
        from ytdj.state import PlayRecord
        hist = [PlayRecord(ZELENA.id, ZELENA.title, ZELENA.artist, "finished", UGC),
                PlayRecord("staryzaznam", "Starý", "Kdosi", "finished")]
        app, _ = app_with(POHODA, [ZELENA, BEZ], hist)
        s = self.snap(app)
        self.assertEqual(s["current"]["src"], "ytm")
        self.assertEqual([q.get("src") for q in s["queue"]], ["yt", None])
        self.assertNotIn("src", s["queue"][1])  # neznámý zdroj = žádné pole, žádný odznak
        self.assertEqual([h.get("src") for h in s["history"]], ["yt", None])

    def test_unknown_type_comes_from_the_history_once(self):
        """Oblíbené, import nebo Zahrát znovu druh nenesou — vezme se, s jakým tu video hrálo."""
        app, asked = app_with(POHODA, [BEZ], types={BEZ.id: UGC})
        srv = web.WebServer(app)
        s1 = run(srv._snapshot())
        s2 = run(srv._snapshot())
        self.assertEqual(s1["queue"][0]["src"], "yt")
        self.assertEqual(s2["queue"][0]["src"], "yt")
        self.assertEqual(asked, [[BEZ.id]])  # jednou, pak z paměti webu

    def test_payload_stays_small(self):
        """Pár bajtů na skladbu: SSE jde každou vteřinu do každého prohlížeče."""
        plain = [Track(f"vid{i:08d}", "Pohoda", "Kabát", duration=232) for i in range(20)]
        typed = [Track(t.id, t.title, t.artist, duration=232, video_type=UGC) for t in plain]
        a = len(json.dumps(self.snap(app_with(plain[0], plain[1:])[0]), ensure_ascii=False))
        b = len(json.dumps(self.snap(app_with(typed[0], typed[1:])[0]), ensure_ascii=False))
        self.assertLessEqual(b - a, 20 * 14)  # `, "src": "yt"` = 13 znaků na skladbu
        self.assertNotIn("MUSIC_VIDEO_TYPE", json.dumps(self.snap(app_with(typed[0], typed[1:])[0])))


class WebPage(unittest.TestCase):
    """Odznak na stránce: neutrální text, česky popsaný, bez loga (F-WEB-12)."""

    page = (ROOT / "ytdj/web/static/index.html").read_text()
    ai = (ROOT / "ytdj/web/static/ai.js").read_text()

    def has(self, needle: str, text: str | None = None) -> None:
        self.assertTrue(needle in (self.page if text is None else text), needle)  # bez výpisu stránky

    def test_badge_text_and_czech_description(self):
        self.has("Oficiální skladba z YouTube Music")
        self.has("Video z YouTube (nahrál posluchač)")
        self.has('"YT Music"')
        self.has('"YouTube"')
        self.assertTrue(re.search(r"\.src\s*\{", self.page))  # styl odznaku

    def test_badge_wherever_a_track_is_listed(self):
        """Hrající karta, Hraje dál, Odehráno, seznamy hlasů a AI „hraje teď"."""
        for where in ("function renderQueue", "function renderHistory", "function itemRow"):
            body = self.page.split(where, 1)[1].split("\n  function ", 1)[0]
            self.has("srcBadge(", body)
        self.has('id="src"')  # u interpreta hrající skladby
        self.has("ytdjSrcBadge", self.ai)

    def test_no_logo_artwork(self):
        for text in (self.page, self.ai):
            self.assertIsNone(re.search(r"youtube[-_ ]?logo|yt[-_]logo|\.png", text.lower()))

    def test_help_page_explains_the_badge(self):
        help_page = (ROOT / "ytdj/web/static/napoveda.html").read_text()
        self.has("YT Music", help_page)
        self.has("nahrál posluchač", help_page)


class Tv(unittest.TestCase):
    """Telka: štítek „▸ YT Music" / „▸ YouTube" u interpreta a v „dál hraje" (F-TV-21)."""

    STATE = {"playing": True, "position": 10.0, "duration": 190.0,
             "current": {"id": "fdVHXjZrfEY", "title": "Zelená je tráva", "artist": "Tleskač",
                         "duration": 190, "src": "yt"},
             "queue": [{"id": "5ICWhbAmIaw", "title": "Pohoda", "artist": "Kabát", "src": "ytm"},
                       {"id": "bezdruhu001", "title": "Neznámá", "artist": "Někdo"},
                       {"id": "x", "title": "Divná", "artist": "Kdosi", "src": "<b>"}],
             "dj": {}}

    def view(self, state=None, **kw):
        from ytdj.tv.screen import view_from
        return view_from(state or self.STATE, now=1_800_000_000.0, mono=100.0, got_at=100.0,
                         clock="14:37", **kw)

    def test_source_reaches_the_tv_and_unknown_is_nothing(self):
        v = self.view()
        self.assertEqual(v.src, "yt")
        self.assertEqual([n.src for n in v.next], ["ytm", "", ""])

    def test_tag_is_drawn_dim_inside_its_region_on_every_size(self):
        from dataclasses import replace

        from ytdj.tv.screen import ACCENT_DIM, FAINT, SRC_LINE, Renderer
        brightest = max(*ACCENT_DIM, *FAINT, *SRC_LINE)
        long_artist = {**self.STATE, "current": {**self.STATE["current"],
                                                 "artist": "Česká filharmonie, Pražský "
                                                           "filharmonický sbor & Jiří Bělohlávek"}}
        for size in ((720, 480), (1280, 720), (1920, 1080)):
            for state in (self.STATE, long_artist):
                with self.subTest(size=size, long=state is long_artist):
                    v = self.view(state)
                    without = replace(v, src="", next=tuple(replace(n, src="") for n in v.next))
                    r1, r2 = Renderer(size), Renderer(size)
                    r1.render(v, full=True)
                    r2.render(without, full=True)
                    from PIL import ImageChops
                    for name in ("main", "next"):
                        box = next(r.box for r in r1.regions if r.name == name)
                        diff = ImageChops.difference(r1.frame.crop(box), r2.frame.crop(box))
                        bbox = diff.getbbox()
                        self.assertIsNotNone(bbox, name)  # štítek tam je
                        tag = r1.frame.crop(box).crop(bbox)
                        # stálý prvek je tlumený: nic jasnějšího než barvy štítku (péče o panel)
                        added = [p for p, q in zip(tag.getdata(), r2.frame.crop(box).crop(bbox).getdata())
                                 if p != q]
                        self.assertLessEqual(max(max(p) for p in added), brightest + 2, name)
                    # nic nepřeteče mimo bezpečné okraje
                    main = next(r.box for r in r1.regions if r.name == "main")
                    self.assertTrue(0 <= main[0] and main[2] <= size[0])

    def test_tag_moves_with_the_picture(self):
        from dataclasses import replace

        from ytdj.tv.screen import Renderer
        v = self.view()
        r = Renderer((1280, 720))
        r.render(v, full=True)
        a = next(x.box for x in r.regions if x.name == "main")
        r.render(replace(v, shift=6), full=True)
        b = next(x.box for x in r.regions if x.name == "main")
        self.assertNotEqual(a, b)  # štítek je v oblasti, která se s posunem hýbe


if __name__ == "__main__":
    unittest.main()
