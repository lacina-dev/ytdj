"""Klipy na telce — strana obrazovky (POZADAVKY #71): kdy se klip pustí a kdy
ne, srovnání s hudbou, vlastní ochrana a předání obrazovky.

Jako testy panelu systémovým pythonem (Pillow):

    YTDJ_EVENTS_FILE=/tmp/x.jsonl python3 -m unittest tests.test_panel_tv_video
"""

from __future__ import annotations

import configparser
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if not os.environ.get("YTDJ_EVENTS_FILE"):
    _EVENTS_TMP = tempfile.TemporaryDirectory(prefix="ytdj-tvvideo-tests-")
    os.environ["YTDJ_EVENTS_FILE"] = str(Path(_EVENTS_TMP.name) / "events.jsonl")

from ytdj.tv import app as tvapp  # noqa: E402
from ytdj.tv import video  # noqa: E402
from ytdj.tv.fb import PngScreen  # noqa: E402
from ytdj.tv.video import (  # noqa: E402
    Director, Guard, Limits, Readings, Want, capability, player_args, sync_action, want_from,
)

_TMP = Path(tempfile.mkdtemp(prefix="ytdj-tvvideo-test-"))
CLIP, CLIP2 = "clipclipcl1", "clipclipcl2"
COOL = Readings(temp_c=64.0, mem_avail_mb=560, throttled=0)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def tick(self, s: float = 1.0):
        self.t += s


class FakeSession:
    """Přehrávač klipu bez mpv: pamatuje si, co se mu řeklo."""

    made: list["FakeSession"] = []

    def __init__(self, vid, stream, start, paused, size, sock):
        self.vid, self.stream, self.start, self.size, self.sock = vid, stream, start, size, sock
        self.paused, self.speed = paused, 1.0
        self.showing = False
        self.pos: float | None = None
        self.drops = 0
        self.code: int | None = None
        self.seeks: list[float] = []
        self.stopped = False
        self.started_at = FakeSession.clock()
        self.last_seek = float("-inf")
        FakeSession.made.append(self)

    clock = staticmethod(time.monotonic)

    def alive(self):
        return self.code is None and not self.stopped

    def exit_code(self):
        return self.code

    def position(self):
        if self.pos is not None:
            self.showing = True
        return self.pos

    def dropped(self):
        return self.drops

    hw = "v4l2m2m"

    def hwdec(self):
        return self.hw

    def set_pause(self, paused):
        self.paused = paused

    def set_speed(self, speed):
        self.speed = speed

    def seek(self, pos):
        self.seeks.append(pos)
        self.last_seek = FakeSession.clock()
        self.pos = pos

    def stop(self):
        self.stopped = True


class Rig:
    def __init__(self, can=(True, ""), readings=COOL, limits=Limits()):
        self.dir = Path(tempfile.mkdtemp(dir=_TMP))
        self.clock = Clock()
        FakeSession.made = []
        FakeSession.clock = self.clock
        self.can = can
        self.readings = readings
        self.events: list[tuple[str, dict]] = []
        self.slept: list[float] = []
        self.guard = Guard(limits, self.clock)
        self.d = Director(self.dir / "streams", str(self.dir / "video.sock"), lambda: (1920, 1080),
                          guard=self.guard, can=lambda: self.can,
                          readings=lambda: Readings(self.readings.temp_c, self.readings.mem_avail_mb,
                                                    self.readings.throttled),
                          start_session=FakeSession, clock=self.clock,
                          sleep=lambda s: self.slept.append(round(s, 2)),
                          emit=lambda kind, **f: self.events.append((kind, f)),
                          status_file=self.dir / "status.json")
        (self.dir / "streams").mkdir()

    def picture(self, vid: str) -> None:
        (self.dir / "streams" / f"{vid}.json").write_text(json.dumps(
            {"id": vid, "url": "https://rr1.googlevideo.com/videoplayback?x=1",
             "headers": {"User-Agent": "UA"}, "height": 720}))

    def step(self, vid=CLIP, pos=30.0, paused=False, on=True, xruns=7, dt=1.0) -> str:
        self.clock.tick(dt)
        return self.d.step(Want(vid=vid, position=pos, paused=paused, xruns=xruns, on=on))

    @property
    def s(self) -> FakeSession | None:
        return self.d.session

    def kinds(self) -> list[str]:
        return [k for k, _ in self.events]

    def report(self) -> dict:
        return json.loads((self.dir / "status.json").read_text())

    def showing(self, vid=CLIP, pos=30.0) -> FakeSession:
        """Klip běží a je vidět: nastartoval napřed, počkal na hudbu a jede s ní."""
        self.picture(vid)
        self.step(vid, pos)
        s = self.s
        s.pos = s.start  # první snímek, drží se v pauze na místě před hudbou
        assert self.step(vid, pos) == "video"
        assert s.paused
        self.step(vid, s.start)  # hudba tam došla → pustit
        assert not s.paused
        return s


class Sync(unittest.TestCase):
    def test_tolerance_nudge_chase_and_seek(self):
        self.assertEqual(sync_action(0.0), ("speed", 1.0))
        self.assertEqual(sync_action(0.15), ("speed", 1.0))  # ±150 ms je „srovnáno“
        self.assertEqual(sync_action(-0.15), ("speed", 1.0))
        self.assertEqual(sync_action(0.4), ("speed", 0.95))  # obraz napřed → zpomalit o 5 %
        self.assertEqual(sync_action(-0.4), ("speed", 1.05))  # pozadu → zrychlit
        self.assertEqual(sync_action(-3.0), ("speed", 1.25))  # větší rozdíl dohnat rychleji
        self.assertEqual(sync_action(3.0), ("speed", 0.75))
        self.assertEqual(sync_action(-20.0), ("seek", video.SEEK_LEAD))  # posun ve skladbě
        self.assertEqual(sync_action(9.0), ("seek", video.SEEK_LEAD))
        self.assertEqual(video.TOLERANCE, 0.15)
        # malý rozdíl se srovná do pár vteřin, střední do půl minuty
        self.assertLessEqual(video.NUDGE_UP_TO / video.NUDGE, 20.0)
        self.assertLessEqual(video.CHASE_UP_TO / video.CHASE, 24.0)

    def test_picture_follows_the_music(self):
        rig = Rig()
        s = rig.showing(pos=30.0)
        self.assertEqual(s.start, 30.0 + video.START_LEAD)  # start s náskokem na rozjezd
        self.assertEqual(s.size, (1920, 1080))
        self.assertEqual(s.seeks, [])  # žádný skok hned po startu
        # obraz 0,6 s pozadu → zrychlit; pak srovnáno → normální rychlost
        s.pos = 39.4
        rig.step(pos=40.0, dt=2.0)
        self.assertEqual(s.speed, 1.05)
        s.pos = 45.05
        rig.step(pos=45.0, dt=2.0)
        self.assertEqual(s.speed, 1.0)
        self.assertEqual(s.seeks, [])
        # někdo posunul hudbu o minutu → skok za ní (jednou, ne bouře skoků)
        s.pos = 47.0
        rig.step(pos=107.0, dt=2.0)
        self.assertEqual(s.seeks, [107.0 + video.SEEK_LEAD])
        s.pos = 20.0
        rig.step(pos=109.0, dt=2.0)
        self.assertEqual(len(s.seeks), 1)  # další skok nejdřív za SEEK_EVERY
        rig.step(pos=113.0, dt=4.0)
        self.assertEqual(len(s.seeks), 2)
        self.assertIn("tv.video_seek", rig.kinds())

    def test_pause_and_resume_at_once(self):
        rig = Rig()
        s = rig.showing()
        rig.step(paused=True, dt=0.5)
        self.assertTrue(s.paused)
        speed = s.speed
        s.pos = 10.0
        rig.step(pos=80.0, paused=True, dt=5.0)  # v pauze se nic nedohání
        self.assertEqual((s.speed, s.seeks), (speed, []))
        rig.step(paused=False, dt=0.5)
        self.assertFalse(s.paused)
        # hudba se teprve načítá (buffering) = taky stát
        w = want_from({"current": {"id": CLIP}, "buffering": True,
                       "tv": {"on": True, "video": CLIP}}, 12.0)
        self.assertTrue(w.paused)


class StartUp(unittest.TestCase):
    """Rozjezd klipu: napřed, v pauze, pustit přesně s hudbou; a kam jde čas."""

    def test_starts_ahead_paused_and_is_released_when_the_music_arrives(self):
        rig = Rig()
        rig.picture(CLIP)
        rig.step(pos=30.0)
        s = rig.s
        self.assertEqual((s.start, s.paused), (30.0 + video.START_LEAD, True))
        self.assertEqual(rig.step(pos=31.0), "screen")  # ještě nenaběhl: obrazovka dál
        s.pos = s.start  # první snímek je venku (po ~3 s) a drží
        self.assertEqual(rig.step(pos=33.0), "video")
        self.assertTrue(s.paused)
        rig.step(pos=34.0)
        self.assertTrue(s.paused)  # hudba ještě nedošla
        rig.step(pos=35.2)  # zbývá 0,8 s: dočkat přesně a pustit
        self.assertEqual(rig.slept, [0.8])
        self.assertFalse(s.paused)
        self.assertEqual(s.seeks, [])  # žádný skok — začal ve správném místě
        # hudba v pauze: klip se vůbec nespouští (žádný zamrzlý snímek na telce) …
        rig2 = Rig()
        rig2.picture(CLIP)
        for _ in range(4):
            self.assertEqual(rig2.step(pos=10.0, paused=True), "screen")
        self.assertIsNone(rig2.s)
        # … a rozjede se, až hudba pokračuje
        rig2.step(pos=10.0)
        self.assertEqual(rig2.s.start, 10.0 + rig2.d.lead)
        # pauza přišla, když už první snímek drží: nepustí se, dokud se nehraje
        rig3 = Rig()
        rig3.picture(CLIP)
        rig3.step(pos=10.0)
        s3 = rig3.s
        s3.pos = s3.start
        for _ in range(4):
            rig3.step(pos=10.0, paused=True)
        self.assertTrue(s3.paused)
        self.assertEqual(rig3.slept, [])

    def test_paused_clip_gives_way_to_the_dim_screen_and_returns_with_the_music(self):
        """Zamrzlý jasný snímek na telce nezůstane: po minutě pauzy obrazovka."""
        rig = Rig()
        s = rig.showing()
        self.assertEqual(rig.step(paused=True, dt=30.0), "video")  # krátká pauza: klip drží
        self.assertTrue(s.paused)
        self.assertEqual(rig.step(paused=True, dt=video.PAUSE_MAX), "screen")
        self.assertTrue(s.stopped)
        self.assertEqual(rig.events[-1][1]["reason"], "paused")
        for _ in range(5):
            self.assertEqual(rig.step(paused=True, dt=10.0), "screen")  # v pauze se nespouští
        self.assertEqual(len(FakeSession.made), 1)
        self.assertEqual(rig.guard.trips, 0)  # není to porucha — žádná trestná pauza
        rig.step(pos=95.0)  # hudba pokračuje → klip zpátky, srovnaný
        self.assertEqual(len(FakeSession.made), 2)
        self.assertEqual(rig.s.start, 95.0 + rig.d.lead)
        self.assertLessEqual(video.PAUSE_MAX, 120)

    def test_late_start_is_let_go_at_once_and_chased_not_restarted(self):
        rig = Rig()
        rig.picture(CLIP)
        rig.step(pos=30.0)
        s = rig.s
        for i in range(9):  # rozjezd trval 10 s, náskok byl 6 s
            rig.step(pos=31.0 + i)
        s.pos = s.start
        self.assertEqual(rig.step(pos=40.0), "video")
        self.assertFalse(s.paused)  # hudba už je dál → hned pustit
        ev = [f for k, f in rig.events if k == "tv.video_start"][-1]
        self.assertEqual((ev["startup_ms"], ev["lead_ms"], ev["late_ms"]), (10000, 6000, 4000))
        s.pos = 37.0
        rig.step(pos=42.0, dt=2.0)
        self.assertEqual((s.speed, s.seeks), (1.25, []))  # dohání, neskáče
        # příště startuje s náskokem podle skutečnosti (a v mezích)
        self.assertAlmostEqual(rig.d.lead, 0.5 * 6.0 + 0.5 * (10.0 + video.LEAD_SPARE))
        rig.d.lead = 6.0
        for startup, expect in ((60.0, video.LEAD_MAX), (0.1, None)):
            rig.d.lead = min(video.LEAD_MAX, max(video.LEAD_MIN,
                                                 0.5 * rig.d.lead + 0.5 * (startup + video.LEAD_SPARE)))
            self.assertTrue(video.LEAD_MIN <= rig.d.lead <= video.LEAD_MAX)

    def test_picture_decoded_by_the_cpu_is_stopped_at_once(self):
        """Kdyby hardwarový dekodér nenaběhl, obraz by počítal procesor (84 °C na Pi)."""
        rig = Rig()
        rig.picture(CLIP)
        rig.step(pos=30.0)
        s = rig.s
        s.hw = "no"
        s.pos = s.start
        self.assertEqual(rig.step(pos=31.0), "screen")
        self.assertTrue(s.stopped)
        self.assertEqual(rig.report()["blocked"], "obraz by dekódoval procesor")
        for _ in range(5):
            rig.step(pos=32.0)
        self.assertEqual(len(FakeSession.made), 1)

    def test_phases_and_drift_go_to_the_log(self):
        rig = Rig()
        rig.picture(CLIP)
        rig.step(pos=30.0)
        s = rig.s
        s.ipc_ms, s.open_ms = 900, 2400  # přehrávač běží / proud je otevřený
        s.pos = s.start
        rig.step(pos=31.0)
        ev = [f for k, f in rig.events if k == "tv.video_start"][-1]
        self.assertEqual((ev["ipc_ms"], ev["open_ms"], ev["startup_ms"], ev["late_ms"]),
                         (900, 2400, 1000, 0))
        rig.step(pos=s.start)  # puštěno
        # první srovnání v toleranci
        s.pos = 40.05
        rig.step(pos=40.0, dt=2.0)
        synced = [f for k, f in rig.events if k == "tv.video_synced"]
        self.assertEqual(len(synced), 1)
        self.assertIn(synced[0]["drift_ms"], (49, 50))
        # největší rozdíl za minutu (po prvním srovnání), jednou za minutu
        for i, drift in enumerate((0.02, -0.12, 0.31, 0.08) * 8):
            s.pos = 50.0 + i * 2 + drift
            rig.step(pos=50.0 + i * 2, dt=2.0)
        reports = [f for k, f in rig.events if k == "tv.video_sync"]
        self.assertEqual(len(reports), 1)
        self.assertIn(reports[0]["max_ms"], (309, 310))
        self.assertEqual(reports[0]["video_id"], CLIP)
        self.assertGreaterEqual(reports[0]["n"], 25)
        rig.step(vid="")  # konec skladby: zbytek se dopíše
        self.assertEqual(len([1 for k, _ in rig.events if k == "tv.video_sync"]), 2)
        self.assertEqual(len([1 for k, _ in rig.events if k == "tv.video_synced"]), 1)

    def test_screen_says_the_clip_is_loading(self):
        from tv_shots import NOW, WISH
        from ytdj.tv.screen import Renderer, view_from

        base = {**WISH, "tv": {"on": True, "video": None, "pending": True}}
        self.assertTrue(view_from(base, now=NOW).clip)  # obraz se hledá
        ready = {**WISH, "tv": {"on": True, "video": WISH["current"]["id"]}}
        self.assertTrue(view_from(ready, now=NOW).clip)  # přehrávač nabíhá
        for tv in ({"on": False, "video": None, "pending": True}, {"on": True, "video": None},
                   {"on": True, "video": "jinajinajin"}, None,
                   {"on": True, "video": WISH["current"]["id"], "blocked": "jukebox je horký"}):
            self.assertFalse(view_from({**WISH, "tv": tv}, now=NOW).clip, tv)
        r = Renderer((1280, 720))
        r.render(view_from({**WISH, "tv": {"on": True}}, now=NOW), full=True)
        boxes = r.render(view_from(base, now=NOW))
        head = next(x.box for x in r.regions if x.name == "head")
        self.assertTrue(boxes)  # nápis přibyl — jen v hlavičce
        for b in boxes:
            self.assertTrue(head[0] <= b[0] and head[1] <= b[1] and b[2] <= head[2] and b[3] <= head[3])
        self.assertIn("klip se načítá", (ROOT / "ytdj" / "tv" / "screen.py").read_text(encoding="utf-8"))
        # a když obraz nebude, řekne krátce proč (z toho, co ví jukebox)
        for note in ("bez klipu — jen zvuk", "klip vypnutý: málo volné paměti", "obraz nejde přehrát"):
            v = view_from({**WISH, "tv": {"on": True, "video": None, "note_tv": note}}, now=NOW)
            self.assertEqual((v.clip, v.clip_note), (False, note))
            a = Renderer((1280, 720))
            a.render(view_from({**WISH, "tv": {"on": True}}, now=NOW), full=True)
            self.assertTrue(a.render(v))
        self.assertEqual(view_from({**WISH, "tv": {"on": False, "note_tv": "x"}}, now=NOW).clip_note, "")
        # během rozjezdu se přehrávač hlídá často, pak jednou za vteřinu
        app = tvapp.TvApp(lambda: PngScreen(_TMP / "t.png", (720, 480)), "http://127.0.0.1:9",
                          "jukebox.local", director=rig_director_with(showing=False))
        self.assertEqual(app._tick(), 0.2)
        app.director.session.showing = True
        self.assertEqual(app._tick(), 1.0)


def rig_director_with(showing: bool):
    class D:
        session = type("S", (), {"showing": showing})()

        def step(self, want):
            return "screen"

        def close(self):
            pass
    return D()


class Wanting(unittest.TestCase):
    def test_only_when_on_and_the_playing_track_itself_is_the_clip(self):
        st = {"current": {"id": CLIP}, "paused": False, "tv": {"on": True, "video": CLIP, "xruns": 3}}
        w = want_from(st, 41.5)
        self.assertEqual((w.vid, w.position, w.paused, w.xruns, w.on), (CLIP, 41.5, False, 3, True))
        self.assertEqual(want_from({**st, "tv": {"on": False, "video": CLIP}}, 1).vid, "")  # vypnuto
        self.assertEqual(want_from({**st, "tv": {"on": True, "video": None}}, 1).vid, "")  # není klip
        self.assertEqual(want_from({**st, "current": {"id": "jinajinajin"}}, 1).vid, "")  # jiná skladba
        self.assertEqual(want_from({**st, "outage": {"reason": "síť"}}, 1).vid, "")
        self.assertEqual(want_from({"current": {"id": CLIP}}, 1).vid, "")  # starý jukebox bez `tv`
        for junk in (None, [], {"tv": "x", "current": 5}, {"tv": {"on": "ano", "video": CLIP}}):
            self.assertEqual(want_from(junk, 0).vid, "")

    def test_screen_until_the_first_frame_then_video_then_back(self):
        rig = Rig()
        self.assertEqual(rig.step(vid="", on=False), "screen")
        self.assertIsNone(rig.s)
        rig.picture(CLIP)
        self.assertEqual(rig.step(), "screen")  # přehrávač se teprve rozjíždí: obrazovka dál
        s = rig.s
        self.assertEqual((s.vid, s.stream["url"][:8]), (CLIP, "https://"))
        self.assertEqual(rig.step(), "screen")
        s.pos = 32.0  # první snímek
        self.assertEqual(rig.step(), "video")
        self.assertEqual(rig.report()["mode"], "video")
        # skladba skončila, další není klip → zpátky obrazovka, hned
        self.assertEqual(rig.step(vid=""), "screen")
        self.assertTrue(s.stopped)
        self.assertIsNone(rig.s)
        self.assertEqual(rig.report()["mode"], "screen")
        self.assertEqual([k for k in rig.kinds() if k.startswith("tv.video_st")],
                         ["tv.video_start", "tv.video_stop"])

    def test_next_clip_replaces_the_current_one_and_switch_off_stops_at_once(self):
        rig = Rig()
        s1 = rig.showing(CLIP)
        rig.picture(CLIP2)
        rig.step(CLIP2, 0.0)  # další skladba je taky klip: starý pryč a nový hned
        self.assertTrue(s1.stopped)
        s2 = rig.s
        self.assertEqual((s2.vid, s2.start), (CLIP2, 0.0 + rig.d.lead))
        s2.pos = s2.start
        self.assertEqual(rig.step(CLIP2, 1.0), "video")
        self.assertEqual(rig.step(vid="", on=False), "screen")  # někdo to vypnul
        self.assertTrue(s2.stopped)
        self.assertEqual(rig.events[-1][1]["reason"], "switch_off")

    def test_no_picture_file_means_screen_without_retrying_every_tick(self):
        rig = Rig()
        for _ in range(5):
            self.assertEqual(rig.step(), "screen")
        self.assertIsNone(rig.s)
        self.assertEqual(rig.kinds().count("tv.video_skip"), 1)
        # rozbitý soubor nebo cizí adresa se nepustí
        for bad in ("{", json.dumps({"id": "jine", "url": "https://x"}),
                    json.dumps({"id": CLIP2, "url": "file:///etc/passwd"})):
            (rig.dir / "streams" / f"{CLIP2}.json").write_text(bad)
            rig.step(vid="")
            self.assertEqual(rig.step(CLIP2), "screen")
            self.assertIsNone(rig.s)


    def test_clip_that_ran_out_is_not_restarted_and_a_crash_backs_off(self):
        rig = Rig()
        s = rig.showing()
        s.code = 0  # klip je kratší než písnička: přehrávač skončil sám
        self.assertEqual(rig.step(), "screen")
        for _ in range(5):
            rig.step()
        self.assertEqual(len(FakeSession.made), 1)  # pro tuhle skladbu už ne
        self.assertEqual(rig.guard.trips, 0)
        # další klip: přehrávač spadne → žádné zkoušení dokola
        rig.picture(CLIP2)
        rig.step(CLIP2)
        s2 = rig.s
        s2.pos = 5.0
        rig.step(CLIP2)
        s2.code = 1
        self.assertEqual(rig.step(CLIP2), "screen")
        self.assertEqual(rig.guard.trips, 1)
        for _ in range(20):
            rig.step(CLIP2, dt=10.0)  # 200 s < 5 min
        self.assertEqual(len(FakeSession.made), 2)
        self.assertEqual(rig.report()["blocked"], "přehrávač videa spadl")
        rig.step(CLIP2, dt=200.0)  # po pauze smí znovu
        self.assertEqual(len(FakeSession.made), 3)

    def test_player_that_never_shows_a_frame_is_given_up(self):
        rig = Rig()
        rig.picture(CLIP)
        rig.step()
        s = rig.s
        for _ in range(int(video.STARTUP_MAX) + 3):
            self.assertEqual(rig.step(), "screen")  # obrazovka celou dobu kreslí dál
        self.assertTrue(s.stopped)
        self.assertEqual(rig.guard.trips, 1)


class Hardware(unittest.TestCase):
    def tree(self, dri=True, hdmi="connected", decoder=True) -> Path:
        root = Path(tempfile.mkdtemp(dir=_TMP))
        (root / "dev/dri").mkdir(parents=True)
        (root / "sys/class/drm/card0-HDMI-A-1").mkdir(parents=True)
        if dri:
            (root / "dev/dri/card0").touch()
        if hdmi is not None:
            (root / "sys/class/drm/card0-HDMI-A-1/status").write_text(hdmi + "\n")
        (root / "sys/class/drm/card0-Writeback-1").mkdir()
        (root / "sys/class/drm/card0-Writeback-1/status").write_text("unknown\n")
        if decoder:
            (root / "dev/video10").touch()
        return root

    def test_says_exactly_what_is_missing(self):
        have = lambda name: "/usr/bin/mpv"  # noqa: E731
        self.assertEqual(capability(self.tree(), have), (True, ""))
        self.assertIn("grafiku", capability(self.tree(dri=False), have)[1])  # config.txt vrácený
        self.assertIn("není připojená", capability(self.tree(hdmi="disconnected"), have)[1])
        self.assertIn("není připojená", capability(self.tree(hdmi=None), have)[1])
        self.assertIn("dekodér", capability(self.tree(decoder=False), have)[1])
        self.assertIn("mpv", capability(self.tree(), lambda name: None)[1])
        self.assertFalse(capability(_TMP / "neni", have)[0])

    def test_without_the_hardware_nothing_starts_and_the_web_is_told(self):
        rig = Rig(can=(False, "Telka není připojená (HDMI)."))
        rig.picture(CLIP)
        for _ in range(3):
            self.assertEqual(rig.step(), "screen")
        self.assertEqual(FakeSession.made, [])
        self.assertEqual((rig.report()["can"], rig.report()["why"]),
                         (False, "Telka není připojená (HDMI)."))
        # telku někdo odpojil za běhu → klip skončí
        rig = Rig()
        s = rig.showing()
        rig.can = (False, "Telka není připojená (HDMI).")
        rig.clock.tick(31)
        self.assertEqual(rig.step(), "screen")
        self.assertTrue(s.stopped)


class Protection(unittest.TestCase):
    def test_thresholds_put_the_video_first_to_go(self):
        lim = Limits()
        # Codex (DJ) se zavírá pod 250 MB volné paměti — klip musí pryč dřív
        self.assertGreater(lim.mem_stop, 250)
        # běžící klip stojí ~120 MB volné paměti (změřeno 6. 10.: 422 → ~300 MB)
        self.assertGreaterEqual(lim.mem_start - 120, lim.mem_stop)
        # výslovně vyžádané video smí níž (DJ pak odpoví na další přání nastudeno),
        # ale se stejným odstupem start/stop
        self.assertLess(lim.mem_stop_wish, 250)
        self.assertGreaterEqual(lim.mem_start_wish - 120, lim.mem_stop_wish)
        self.assertGreaterEqual(lim.mem_stop_wish, 150)
        self.assertLess(lim.temp_stop, 80)  # Pi 3 se přiškrcuje od 80 °C
        self.assertLess(lim.temp_ok, lim.temp_stop - 3)  # hystereze
        self.assertGreaterEqual(lim.cooldown, 300)
        unit = (ROOT / "packaging" / "ytdj-tv.service").read_text(encoding="utf-8")
        self.assertIn("OOMScoreAdjust=900", unit)  # i jádro sáhne nejdřív po telce

    def test_requested_video_may_run_with_less_memory_and_the_reason_is_always_said(self):
        """Večer 6. 10. mělo Pi 188–370 MB volných: klip, o který si někdo řekl,
        nesmí být potichu nemožný — a když nejde, řekne se proč."""
        # co jukebox pouští sám: pod 380 MB nezačne (DJ má přednost) — a řekne to hned,
        # bez „trestné“ pauzy
        rig = Rig(readings=Readings(64.0, 320, 0))
        rig.picture(CLIP)
        for _ in range(3):
            self.assertEqual(rig.step(), "screen")
        self.assertEqual(FakeSession.made, [])
        self.assertEqual(rig.report()["blocked"], "málo volné paměti")
        self.assertEqual(rig.guard.trips, 0)
        rig.readings = Readings(64.0, 400, 0)  # uvolnilo se → jde hned
        rig.step()
        self.assertEqual(len(FakeSession.made), 1)
        # výslovně vyžádané video při 320 MB začne
        rig = Rig(readings=Readings(64.0, 320, 0))
        rig.picture(CLIP)
        rig.clock.tick()
        rig.d.step(Want(vid=CLIP, position=30.0, on=True, explicit=True, xruns=7))
        self.assertEqual(len(FakeSession.made), 1)
        s = rig.s
        s.pos = s.start
        rig.clock.tick()
        self.assertEqual(rig.d.step(Want(vid=CLIP, position=30.0, on=True, explicit=True, xruns=7)),
                         "video")
        # běží dál i při 200 MB (obyčejný klip by pod 260 skončil) …
        rig.readings = Readings(64.0, 200, 0)
        rig.clock.tick(6)
        self.assertEqual(rig.d.step(Want(vid=CLIP, position=40.0, on=True, explicit=True, xruns=7)),
                         "video")
        # … ale pod 180 MB končí taky, a zvuk má přednost vždy
        rig.readings = Readings(64.0, 170, 0)
        rig.clock.tick(6)
        self.assertEqual(rig.d.step(Want(vid=CLIP, position=46.0, on=True, explicit=True, xruns=7)),
                         "screen")
        self.assertEqual(rig.report()["blocked"], "málo volné paměti")
        rig = Rig(readings=Readings(64.0, 320, 0))
        s = None
        rig.picture(CLIP)
        rig.clock.tick()
        rig.d.step(Want(vid=CLIP, position=30.0, on=True, explicit=True, xruns=7))
        s = rig.s
        s.pos = s.start
        rig.clock.tick()
        rig.d.step(Want(vid=CLIP, position=30.0, on=True, explicit=True, xruns=7))
        rig.clock.tick(6)
        self.assertEqual(rig.d.step(Want(vid=CLIP, position=40.0, on=True, explicit=True, xruns=8)),
                         "screen")  # lupnutí zvuku zastaví i vyžádané video
        # obyčejný klip při 250 MB končí (DJ se zavírá pod 250)
        rig = Rig()
        rig.showing()
        rig.readings = Readings(64.0, 250, 0)
        self.assertEqual(rig.step(dt=6.0), "screen")
        # telka ví, že jde o vyžádané video, jen ze stavu jukeboxu
        st = {"current": {"id": CLIP}, "tv": {"on": True, "video": CLIP, "explicit": True}}
        self.assertTrue(want_from(st, 1.0).explicit)
        st["tv"]["explicit"] = False
        self.assertFalse(want_from(st, 1.0).explicit)

    def test_each_trigger_stops_the_clip_and_says_why(self):
        cases = (
            (dict(temp_c=78.5), "jukebox je horký"),
            (dict(mem_avail_mb=240), "málo volné paměti"),
            (dict(throttled=0x4), "procesor je přiškrcený nebo má málo napětí"),
            (dict(throttled=0x1), "procesor je přiškrcený nebo má málo napětí"),
        )
        for change, why in cases:
            rig = Rig()
            s = rig.showing()
            rig.readings = Readings(**{**COOL.__dict__, **change})
            self.assertEqual(rig.step(dt=6.0), "screen", change)
            self.assertTrue(s.stopped)
            self.assertEqual(rig.report()["blocked"], why)
            ev = [f for k, f in rig.events if k == "tv.video_guard"][-1]
            self.assertEqual(ev["reason"], why)
        # dřívější přiškrcení (horní bity) nevadí, jen to, co se děje teď
        rig = Rig(readings=Readings(64.0, 560, 0x50000))
        rig.showing()
        self.assertEqual(rig.step(dt=6.0), "video")

    def test_audio_dropouts_and_stuttering_picture_stop_it(self):
        rig = Rig()
        s = rig.showing()
        self.assertEqual(rig.step(xruns=7, dt=6.0), "video")
        self.assertEqual(rig.step(xruns=8, dt=6.0), "screen")  # zvuk lupl, když běžel klip
        self.assertEqual(rig.report()["blocked"], "zvuk začal lupat")
        rig = Rig()
        s = rig.showing()
        s.drops = 30
        self.assertEqual(rig.step(dt=6.0), "video")  # jedno zadrhnutí (skok) nevadí
        s.drops = 34
        self.assertEqual(rig.step(dt=6.0), "video")
        s.drops = 70
        self.assertEqual(rig.step(dt=6.0), "video")
        s.drops = 110
        self.assertEqual(rig.step(dt=6.0), "screen")  # dvakrát po sobě → vypnout
        self.assertEqual(rig.report()["blocked"], "obraz se zadrhával")

    def test_hysteresis_and_growing_back_off(self):
        clock = Clock()
        g = Guard(Limits(), clock)
        self.assertEqual(g.may_start(COOL), (True, ""))
        self.assertEqual(g.may_start(Readings(64, 370, 0)), (False, "málo volné paměti"))
        self.assertEqual(g.may_start(Readings(64, 560, 0x2))[0], False)
        g.started(COOL)
        self.assertEqual(g.while_running(Readings(79.0, 560, 0)), "jukebox je horký")
        clock.tick(301)  # pauza uplynula, ale pořád je teplo → ne
        self.assertEqual(g.may_start(Readings(75.0, 560, 0)), (False, "jukebox je horký"))
        self.assertEqual(g.may_start(Readings(71.5, 560, 0)), (True, ""))  # až vychladne
        # každé další vypnutí drží klipy déle (5, 10, 20, nejvýš 30 min)
        pauses = [g.trip("x") for _ in range(5)]
        self.assertEqual(pauses, [600.0, 1200.0, 1800.0, 1800.0, 1800.0])
        clock.tick(1801)
        self.assertEqual(g.may_start(COOL), (True, ""))
        clock.tick(1801)  # půl hodiny klidu → počítá se zase od začátku
        self.assertEqual(g.trip("x"), 300.0)
        # čtení, které není k dispozici (jiný stroj), nic nevypíná
        g2 = Guard(Limits(), clock)
        self.assertEqual(g2.may_start(Readings()), (True, ""))
        g2.started(Readings())
        self.assertEqual(g2.while_running(Readings()), "")

    def test_blocked_video_does_not_restart_until_the_pause_is_over(self):
        rig = Rig()
        rig.showing()
        rig.readings = Readings(79.0, 560, 0)
        rig.step(dt=6.0)
        rig.readings = COOL
        for _ in range(10):
            self.assertEqual(rig.step(dt=20.0), "screen")  # 200 s: ještě pauza
        self.assertEqual(len(FakeSession.made), 1)
        rig.step(dt=120.0)
        self.assertEqual(len(FakeSession.made), 2)
        self.assertEqual(rig.report()["blocked"], "")


class CommandLine(unittest.TestCase):
    def test_measured_options_are_pinned(self):
        args = player_args("https://rr1.googlevideo.com/v?x=1", "/run/ytdj-tv/video.sock",
                           (1920, 1080), 41.26, False, {"User-Agent": "UA", "Referer": "https://r/"})
        self.assertEqual(args[0], "mpv")
        for opt in ("--cache-pause=no", "--cache-secs=6", "--demuxer-readahead-secs=3"):
            self.assertIn(opt, args, opt)  # rychlý rozjezd němého obrazu
        self.assertNotIn("--hr-seek=no", args)  # start přesně na místě, ne na klíčovém snímku
        for opt in ("--no-config", "--no-audio", "--vo=gpu", "--gpu-context=drm", "--hwdec=v4l2m2m",
                    "--drm-draw-plane=overlay", "--drm-drmprime-video-plane=primary",
                    "--ytdl=no", "--load-scripts=no", "--drm-mode=1920x1080", "--start=41.26",
                    "--pause=no", "--input-ipc-server=/run/ytdj-tv/video.sock", "--user-agent=UA"):
            self.assertIn(opt, args, opt)
        # nikdy cesta, která obraz přepočítává v procesoru (84 °C a přiškrcení na Pi)
        joined = " ".join(args)
        for banned in ("--vo=drm", "v4l2m2m-copy", "--hwdec=no", "--hwdec=auto", "--vf", "--sws",
                       "--audio-device", "--ao=", "mpv.sock"):
            self.assertNotIn(banned, joined, banned)
        self.assertEqual(args[-2:], ["--", "https://rr1.googlevideo.com/v?x=1"])  # adresa až za --
        paused = player_args("https://x/y", "/s", None, -3, True)
        self.assertIn("--pause=yes", paused)
        self.assertIn("--start=0.00", paused)
        self.assertFalse([a for a in paused if a.startswith("--drm-mode")])

    def test_runs_below_the_music_and_only_reads_the_jukebox(self):
        src = (ROOT / "ytdj" / "tv" / "video.py").read_text(encoding="utf-8")
        self.assertIn('["nice", "-n", str(nice), *args]', src)
        for banned in ("MPV_SOCKET", "ytdj/mpv.sock", "POST", ".control(", ".prompt(", "pipewire",
                       "pactl", "wpctl"):
            self.assertNotIn(banned, src, banned)


@unittest.skipUnless(shutil.which("mpv") and shutil.which("ffmpeg"), "potřebuje mpv a ffmpeg")
class RealPlayer(unittest.TestCase):
    """Skutečné mpv (bez obrazu, --vo=null): řízení přes IPC a srovnání s hudbou."""

    def test_real_player_is_steered_into_step_with_the_music(self):
        clip = _TMP / "clip.mp4"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "testsrc=duration=240:size=320x180:rate=25", "-c:v", "libx264", "-g", "50",
                        "-pix_fmt", "yuv420p", str(clip)], check=True, timeout=120)

        class NullSession(video.Session):
            def __init__(self, vid, stream, start, paused, size, sock):
                def popen(args, **kw):
                    i = args.index("mpv")
                    keep = [a for a in args[i:] if not a.startswith(
                        ("--vo=", "--gpu-context", "--hwdec", "--drm-", "--stream-lavf-o"))]
                    return subprocess.Popen([*keep[:1], "--vo=null", *keep[1:-2], "--", str(clip)],
                                            **kw)
                super().__init__(vid, stream, start, paused, size, sock, popen=popen)

        events: list[tuple[str, dict]] = []
        d = Director(_TMP / "streams-real", str(_TMP / "real.sock"), lambda: None,
                     can=lambda: (True, ""), readings=lambda: Readings(60.0, 600, 0),
                     start_session=NullSession, emit=lambda k, **f: events.append((k, f)),
                     status_file=None, require_hwdec=False)
        (_TMP / "streams-real").mkdir(exist_ok=True)
        (_TMP / "streams-real" / f"{CLIP}.json").write_text(json.dumps(
            {"id": CLIP, "url": "https://example.invalid/x", "headers": {}}))
        t0 = time.monotonic()
        music = lambda: 20.0 + (time.monotonic() - t0)  # noqa: E731 — hudba je na 20. vteřině a běží
        try:
            drifts: list[float] = []
            end = time.monotonic() + 30
            while time.monotonic() < end:
                d.step(Want(vid=CLIP, position=music(), on=True))
                if d.mode == "video":
                    pos = d.session.position()
                    if pos is not None:
                        drifts.append(pos - music())
                    if len(drifts) > 12 and all(abs(x) <= 0.2 for x in drifts[-4:]):
                        break
                time.sleep(0.5)
            self.assertEqual(d.mode, "video", events)
            self.assertTrue(drifts and all(abs(x) <= 0.2 for x in drifts[-4:]),
                            ([round(x, 2) for x in drifts], events))
            # posun v hudbě o 25 s dopředu → obraz skočí za ní a zase se srovná
            t0 -= 25.0
            end = time.monotonic() + 25
            ok = False
            while time.monotonic() < end and not ok:
                d.step(Want(vid=CLIP, position=music(), on=True))
                pos = d.session.position() if d.session else None
                ok = pos is not None and abs(pos - music()) <= 0.3
                time.sleep(0.5)
            self.assertTrue(ok, (pos, music(), events[-4:]))
            self.assertIn("tv.video_seek", [k for k, _ in events])
            # pauza platí hned
            d.step(Want(vid=CLIP, position=music(), paused=True, on=True))
            self.assertTrue(d.session.ipc.get("pause"))
            # skladba skončila → přehrávač pryč
            proc = d.session.proc
            d.step(Want(vid="", on=True))
            self.assertIsNotNone(proc.poll())
            self.assertEqual(d.mode, "screen")
        finally:
            d.close()


class HandOver(unittest.TestCase):
    """Obrazovka nekreslí pod klipem a po něm se vrátí celá."""

    def test_screen_stops_drawing_under_the_clip_and_redraws_after(self):
        mode = ["screen"]

        class FakeDirector:
            session = None

            def step(self, want):
                self.want = want
                return mode[0]

            def close(self):
                self.closed = True

        out = _TMP / "handover.png"
        screens: list[PngScreen] = []

        def open_screen():
            screens.append(PngScreen(out, (720, 480)))
            return screens[-1]

        d = FakeDirector()
        app = tvapp.TvApp(open_screen, "http://127.0.0.1:9", "jukebox.local", director=d)
        app.feed = type("NoFeed", (), {"start": lambda self: None})()  # stav dodá test sám
        app._on_state({"current": {"id": CLIP, "title": "T", "artist": "A"}, "paused": False,
                       "position": 12.0, "duration": 200, "queue": [],
                       "tv": {"on": True, "video": CLIP}})
        t = threading.Thread(target=app.run, daemon=True)
        t.start()
        try:
            end = time.monotonic() + 5
            while not (screens and screens[0].calls) and time.monotonic() < end:
                time.sleep(0.02)
            self.assertTrue(screens[0].calls)  # napřed obrazovka „právě hraje“
            self.assertEqual(d.want.vid, CLIP)
            self.assertGreaterEqual(d.want.position, 12.0)
            mode[0] = "video"
            app.wake.set()
            time.sleep(0.4)
            calls = screens[0].calls
            app._on_state({"current": {"id": CLIP, "title": "Jiný název", "artist": "A"},
                           "paused": False, "position": 15.0, "duration": 200, "queue": [],
                           "tv": {"on": True, "video": CLIP}})
            time.sleep(0.5)
            self.assertEqual(screens[0].calls, calls)  # pod klipem se nekreslí nic
            px = screens[0].pushed_px
            mode[0] = "screen"
            app.wake.set()
            end = time.monotonic() + 5
            while screens[0].calls == calls and time.monotonic() < end:
                time.sleep(0.02)
            self.assertGreaterEqual(screens[0].pushed_px - px, 720 * 480)  # celá obrazovka znovu
            self.assertLessEqual(app._tick(), 30.0)
        finally:
            app.shutdown()
            t.join(3)

    def test_a_failing_director_never_takes_the_screen_down(self):
        class Broken:
            session = None

            def step(self, want):
                raise RuntimeError("bum")

            def close(self):
                pass

        app = tvapp.TvApp(lambda: PngScreen(_TMP / "b.png", (720, 480)), "http://127.0.0.1:9",
                          "jukebox.local", director=Broken())
        self.assertFalse(app.video_step())
        plain = tvapp.TvApp(lambda: PngScreen(_TMP / "b.png", (720, 480)), "http://127.0.0.1:9",
                            "jukebox.local")
        self.assertFalse(plain.video_step())  # bez režiséra (simulace) se nic nemění


class Packaging(unittest.TestCase):
    def test_unit_gives_the_clip_what_it_needs_and_nothing_more(self):
        raw = (ROOT / "packaging" / "ytdj-tv.service").read_text(encoding="utf-8")
        unit = configparser.ConfigParser(strict=False, interpolation=None)
        unit.read_string(raw)
        s = unit["Service"]
        allowed = [ln.split("=", 1)[1] for ln in raw.splitlines() if ln.startswith("DeviceAllow=")]
        self.assertEqual(allowed, ["/dev/fb0 rw", "/dev/tty1 rw", "char-drm rw",
                                   "char-video4linux rw", "/dev/vchiq rw", "/dev/cec0 rw"])
        self.assertEqual(s["SupplementaryGroups"], "video render")
        self.assertEqual((s["RuntimeDirectory"], s["RuntimeDirectoryMode"]), ("ytdj-tv", "0755"))
        self.assertEqual(s["User"], "@USER@")
        self.assertEqual(s["NoNewPrivileges"], "yes")
        self.assertIn("cookies.txt", s["InaccessiblePaths"])  # ke cookies se telka nedostane
        self.assertGreater(int(s["Nice"]), 0)

    def test_boot_config_is_never_edited_and_slim_keeps_an_explicit_choice(self):
        slim = (ROOT / "packaging" / "rpi" / "slim.sh").read_text(encoding="utf-8")
        self.assertIn("grep -Eq '^dtoverlay=vc4-kms-v3d' \"$cfg\"", slim)
        self.assertIn("YTDJ_SLIM_NO_VIDEO", slim)
        install = (ROOT / "packaging" / "install-service.sh").read_text(encoding="utf-8")
        for word in ("gpu_mem", "vc4-kms", "cma-"):
            self.assertNotIn(word, install.replace("gpu_mem/KMS", ""), word)
        notes = (ROOT / "packaging" / "rpi" / "NOTES.md").read_text(encoding="utf-8")
        for word in ("gpu_mem=64", "dtoverlay=vc4-kms-v3d,cma-128", "max_framebuffers=2",
                     "config.txt.ytdj-pred-videem", "YTDJ_SLIM_NO_VIDEO=1"):
            self.assertIn(word, notes, word)
        # slim.sh opravdu nesáhne na zapnutou grafiku (spuštěno nanečisto)
        cfg = _TMP / "config.txt"
        fake_bin = _TMP / "bin"
        fake_bin.mkdir(exist_ok=True)
        for name, body in (("sudo", 'exec "$@"\n'), ("systemctl", "exit 0\n")):
            f = fake_bin / name
            f.write_text("#!/bin/sh\n" + body)
            f.chmod(0o755)
        (_TMP / "cloud").mkdir(exist_ok=True)
        script = slim.replace("cfg=/boot/firmware/config.txt", f"cfg={cfg}") \
                     .replace("/etc/cloud/cloud-init.disabled", str(_TMP / "cloud" / "disabled"))
        env = dict(os.environ, PATH=f"{fake_bin}:{os.environ['PATH']}")
        video_cfg = ("camera_auto_detect=1\n[all]\ngpu_mem=64\n"
                     "dtoverlay=vc4-kms-v3d,cma-128\nmax_framebuffers=2\n")
        cfg.write_text(video_cfg)
        subprocess.run(["bash", "-c", script], env=env, check=True, capture_output=True, timeout=30)
        kept = cfg.read_text()
        for line in ("gpu_mem=64", "dtoverlay=vc4-kms-v3d,cma-128", "max_framebuffers=2"):
            self.assertIn("\n" + line + "\n", kept)
        self.assertNotIn("gpu_mem=16", kept)
        # výslovný návrat: zpátky úsporně
        subprocess.run(["bash", "-c", script], env=dict(env, YTDJ_SLIM_NO_VIDEO="1"), check=True,
                       capture_output=True, timeout=30)
        back = cfg.read_text()
        self.assertIn("\ngpu_mem=16\n", back)
        self.assertIn("#dtoverlay=vc4-kms-v3d,cma-128", back)
        # a bez klipů se chová jako dřív
        cfg.write_text("camera_auto_detect=1\ndtoverlay=vc4-kms-v3d\nmax_framebuffers=2\n[all]\n")
        subprocess.run(["bash", "-c", script], env=dict(env, YTDJ_SLIM_NO_VIDEO="1"), check=True,
                       capture_output=True, timeout=30)
        self.assertIn("gpu_mem=16", cfg.read_text())
        self.assertIn("#dtoverlay=vc4-kms-v3d", cfg.read_text())


if __name__ == "__main__":
    unittest.main()
