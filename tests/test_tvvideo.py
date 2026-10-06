"""Klipy na telce — strana jukeboxu (POZADAVKY #71): vypínač pro všechny,
jen skladby, které jsou samy oficiální klip, obraz hledaný stranou od hudby.

    YTDJ_EVENTS_FILE=/tmp/x.jsonl python -m unittest tests.test_tvvideo
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="ytdj-tvvideo-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)
os.environ.setdefault("YTDJ_EVENTS_FILE", str(Path(_TMP) / "events.jsonl"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ytdj import telemetry, tvvideo  # noqa: E402
from ytdj.music.catalog import Catalog, Track  # noqa: E402
from ytdj.tvvideo import OMV, TvVideo, stream_from_info  # noqa: E402
from ytdj.web import tv_api  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CLIP, SONG, UGC, NEXT_CLIP = "clipclipcl1", "songsongso1", "ugcugcugcu1", "clipclipcl2"
KINDS = {CLIP: OMV, NEXT_CLIP: OMV, SONG: "MUSIC_VIDEO_TYPE_ATV", UGC: "MUSIC_VIDEO_TYPE_UGC"}
URL = "https://rr1---sn-x.googlevideo.com/videoplayback?expire={exp}&itag=136&sig=TAJNE"


def info(vid: str, **kw) -> dict:
    base = {"id": vid, "url": URL.format(exp=int(time.time()) + 6 * 3600), "vcodec": "avc1.4d401f",
            "height": 720, "fps": 25, "format_id": "136",
            "http_headers": {"User-Agent": "UA", "Cookie": "SID=tajne", "Accept": "*/*"}}
    base.update(kw)
    return base


class FakeCatalog:
    def __init__(self):
        self.asked: list[str] = []
        self.fail = False

    async def video_type(self, vid):
        self.asked.append(vid)
        if self.fail:
            raise OSError("síť")
        return KINDS.get(vid)


class FakePlayer:
    def __init__(self, current: str | None = None, queue: tuple[str, ...] = ()):
        self.current, self.queue = current, list(queue)
        self.sampler = SimpleNamespace(xruns=7)

    async def status(self):
        tr = lambda v: Track(v, "t", "a", duration=200)  # noqa: E731
        return SimpleNamespace(current=tr(self.current) if self.current else None,
                               queue=[tr(v) for v in self.queue])


class Rig:
    """TvVideo s falešným katalogem, přehrávačem, telkou a yt-dlp (skript)."""

    def __init__(self, current=None, queue=(), tv=None, ytdlp_sleep=0.0):
        self.dir = Path(tempfile.mkdtemp(dir=_TMP))
        self.catalog, self.player = FakeCatalog(), FakePlayer(current, queue)
        self.changes = 0
        self.calls = self.dir / "ytdlp-calls"
        self.out = self.dir / "ytdlp-out"  # co falešný yt-dlp vypíše (JSON), nebo nic
        fake = self.dir / "yt-dlp"
        fake.write_text(f'#!/bin/sh\necho "$@" >> {self.calls}\nsleep {ytdlp_sleep}\n'
                        f'for a; do last="$a"; done\nid="${{last##*=}}"\n'
                        f'[ -e {self.out}.$id ] && cat {self.out}.$id\n')
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.tv_status = self.dir / "tv-status.json"
        if tv is not False:
            self.tv_says(tv or {"can": True, "why": "", "mode": "screen", "blocked": ""})
        self.tv = TvVideo(SimpleNamespace(child_env=lambda: dict(os.environ)), self.catalog,
                          self.player, self.dir / "state" / "tv-video.json", self.dir / "streams",
                          on_change=self._changed, tv_status=self.tv_status,
                          yt_dlp_args=lambda cfg: [str(fake), "--cookies", "/tajne/cookies.txt"])
        self.events: list[tuple[str, dict]] = []
        self._tel = mock.patch.object(telemetry, "event",
                                      lambda kind, **f: self.events.append((kind, f)))

    def _changed(self):
        self.changes += 1

    def tv_says(self, report: dict) -> None:
        self.tv_status.write_text(json.dumps(report))

    def has_picture(self, vid: str, **kw) -> None:
        Path(f"{self.out}.{vid}").write_text(json.dumps(info(vid, **kw)))

    def ytdlp_calls(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    async def settle(self, cond, timeout=5.0):
        end = time.monotonic() + timeout
        while not cond():
            if time.monotonic() > end:
                raise AssertionError("podmínka nenastala")
            self.tv._report_mono = -1e9
            await self.tv.tick()
            await asyncio.sleep(0.03)

    def __enter__(self):
        self._tel.start()
        return self

    def __exit__(self, *a):
        self._tel.stop()


def run(coro):
    return asyncio.run(coro)


class Switch(unittest.TestCase):
    def test_default_off_anyone_toggles_global_and_survives_restart(self):
        async def go():
            with Rig() as rig:
                tv = rig.tv
                await tv.load()
                self.assertFalse(tv.enabled)  # výchozí vypnuto
                self.assertFalse(tv.public()["on"])
                state = await tv.set(True, "Kolega", {"ip": "10.0.0.7", "ua": "Chrome/Linux"})
                self.assertTrue(state["on"])
                self.assertEqual(state["by"], "Kolega")
                self.assertGreaterEqual(rig.changes, 1)  # všem hned (stav / SSE)
                kind, f = rig.events[-1]
                self.assertEqual((kind, f["on"], f["who"], f["changed"], f["ip"]),
                                 ("tv.video_switch", True, "Kolega", True, "10.0.0.7"))
                # jiný proces (restart): stav se načte z disku
                again = TvVideo(tv.cfg, rig.catalog, rig.player, tv.state_file, tv.stream_dir,
                                tv_status=rig.tv_status)
                await again.load()
                self.assertTrue(again.enabled)
                self.assertEqual(again.by, "Kolega")
                await again.set(False, "")
                self.assertFalse(json.loads(tv.state_file.read_text())["on"])
        run(go())

    def test_writes_off_the_loop_and_broken_state_file_means_off(self):
        async def go():
            with Rig() as rig:
                threads: list[int] = []
                real = tvvideo._write
                import threading

                def write(*a, **kw):
                    threads.append(threading.get_ident())
                    return real(*a, **kw)

                with mock.patch.object(tvvideo, "_write", write):
                    await rig.tv.set(True, "x")
                self.assertTrue(threads and threading.get_ident() not in threads)
                for broken in ("", "{", "\0" * 64, "[1]", '{"on": "ano"}'):
                    rig.tv.state_file.write_text(broken)
                    fresh = TvVideo(rig.tv.cfg, rig.catalog, rig.player, rig.tv.state_file,
                                    rig.tv.stream_dir, tv_status=rig.tv_status)
                    await fresh.load()
                    self.assertFalse(fresh.enabled, broken)
                # disk nejde: přepnutí platí dál v paměti, nic nespadne
                with mock.patch.object(tvvideo, "_write", side_effect=OSError("plno")):
                    self.assertTrue((await rig.tv.set(True, "x"))["on"])
        run(go())

    def test_says_honestly_when_the_tv_cannot_do_it(self):
        async def go():
            with Rig(tv=False) as rig:  # proces na telce neběží (žádné hlášení)
                await rig.tv.tick()
                self.assertEqual(rig.tv.public()["can"], False)
                self.assertIn("neběží", rig.tv.public()["why"])
                rig.tv_says({"can": False, "why": "Telka není připojená (HDMI).", "mode": "screen"})
                rig.tv._report_mono = -1e9
                await rig.tv.tick()
                p = rig.tv.public()
                self.assertEqual((p["can"], p["why"]), (False, "Telka není připojená (HDMI)."))
                # zapnout jde (pro později), ale nic se nehledá ani neslibuje
                rig.player.current = CLIP
                await rig.tv.set(True, "x")
                await rig.tv.tick()
                self.assertEqual((rig.tv.public()["video"], rig.catalog.asked, rig.ytdlp_calls()),
                                 (None, [], []))
                # staré hlášení (proces spadl) = neběží
                rig.tv_says({"can": True, "mode": "video"})
                os.utime(rig.tv_status, (time.time() - 600, time.time() - 600))
                rig.tv._report_mono = -1e9
                await rig.tv.tick()
                self.assertFalse(rig.tv.public()["can"])
                # telka hlásí, co dělá a proč zrovna ne (ochrana)
                rig.tv_says({"can": True, "mode": "screen", "blocked": "jukebox je horký"})
                rig.tv._report_mono = -1e9
                await rig.tv.tick()
                p = rig.tv.public()
                self.assertEqual((p["can"], p["mode"], p["blocked"]), (True, "screen", "jukebox je horký"))
                self.assertEqual(p["xruns"], 7)
        run(go())


class WhichTracks(unittest.TestCase):
    def test_only_a_track_that_is_itself_an_official_video(self):
        async def go():
            for vid, expect in ((CLIP, CLIP), (SONG, None), (UGC, None), ("neznameid00", None)):
                with Rig(current=vid) as rig:
                    rig.has_picture(vid)
                    await rig.tv.set(True, "x")
                    if expect:
                        await rig.settle(lambda: rig.tv.video == expect)
                    else:
                        for _ in range(3):
                            await rig.tv.tick()
                        self.assertIsNone(rig.tv.video, vid)
                        self.assertEqual(rig.ytdlp_calls(), [], vid)  # obraz se ani nehledá
                    # nikdy jiné video než to, které hraje (žádná záměna za protějšek)
                    self.assertIn(rig.tv.public()["video"], (None, vid))
        run(go())

    def test_off_means_no_lookups_at_all(self):
        async def go():
            with Rig(current=CLIP, queue=(NEXT_CLIP,)) as rig:
                rig.has_picture(CLIP)
                for _ in range(3):
                    await rig.tv.tick()
                self.assertEqual((rig.catalog.asked, rig.ytdlp_calls(), rig.tv.public()["video"]),
                                 ([], [], None))
                await rig.tv.set(True, "x")
                await rig.settle(lambda: rig.tv.video == CLIP)
                await rig.tv.set(False, "y")  # vypnutí platí hned
                self.assertIsNone(rig.tv.public()["video"])
        run(go())

    def test_kind_is_asked_once_per_track_and_a_failed_lookup_is_retried(self):
        async def go():
            with Rig(current=SONG) as rig:
                await rig.tv.set(True, "x")
                for _ in range(4):
                    await rig.tv.tick()
                self.assertEqual(rig.catalog.asked, [SONG])
                rig.player.current = CLIP
                rig.catalog.fail = True
                await rig.tv.tick()
                self.assertIsNone(rig.tv.video)
                rig.catalog.fail = False
                rig.has_picture(CLIP)
                await rig.settle(lambda: rig.tv.video == CLIP)
        run(go())

    def test_catalogue_tells_the_kind_of_exactly_that_video(self):
        async def go():
            calls = []

            class YT:
                def get_watch_playlist(self, videoId=None, limit=None, **kw):
                    calls.append((videoId, limit))
                    return {"tracks": [{"videoId": "jinejinejin", "videoType": OMV},
                                       {"videoId": videoId, "videoType": KINDS.get(videoId)}]}

            cat = Catalog.__new__(Catalog)
            cat.yt = YT()
            self.assertEqual(await cat.video_type(CLIP), OMV)
            self.assertEqual(await cat.video_type(SONG), "MUSIC_VIDEO_TYPE_ATV")
            self.assertIsNone(await cat.video_type("neznameid00"))
            self.assertEqual(calls[0], (CLIP, 1))
        run(go())


class Picture(unittest.TestCase):
    def test_only_direct_h264_up_to_720p(self):
        self.assertEqual(stream_from_info(info(CLIP))["height"], 720)
        self.assertEqual(stream_from_info(info(CLIP, height=480))["format"], "136")
        for bad in (dict(vcodec="vp09.00.31.08"), dict(vcodec="av01.0.05M.08"), dict(height=1080),
                    dict(height=None), dict(url="http://x/y"), dict(url=None), dict(url="x.m3u8")):
            self.assertIsNone(stream_from_info(info(CLIP, **bad)), bad)
        self.assertIsNone(stream_from_info(None))
        self.assertIsNone(stream_from_info([]))
        # telce jde jen to, co potřebuje — žádné cookies
        self.assertEqual(stream_from_info(info(CLIP))["headers"], {"User-Agent": "UA"})
        self.assertIn("[vcodec^=avc1]", tvvideo.FORMAT)
        self.assertIn("[height<=720]", tvvideo.FORMAT)

    def test_resolved_aside_from_the_music_and_never_waits_for_it(self):
        async def go():
            with Rig(current=CLIP, ytdlp_sleep=0.6) as rig:
                rig.has_picture(CLIP)
                await rig.tv.set(True, "x")
                t0 = time.monotonic()
                await rig.tv.tick()  # krok nečeká na yt-dlp (19 s na Pi)
                self.assertLess(time.monotonic() - t0, 0.3)
                p = rig.tv.public()
                self.assertEqual((p["video"], p["pending"]), (None, True))  # zatím obrazovka
                await rig.settle(lambda: rig.tv.video == CLIP)
                call = rig.ytdlp_calls()[0]
                self.assertIn("-f bestvideo[vcodec^=avc1][height<=720][protocol^=http] -j", call)
                self.assertIn(f"watch?v={CLIP}", call)
                self.assertIn("--cookies /tajne/cookies.txt", call)  # stejné volby jako hudba
                args = rig.tv._args(CLIP)
                self.assertEqual(args[:3], ["nice", "-n", "19"])  # nejnižší priorita
                # vlastní proces, ne resolver hudby
                src = (ROOT / "ytdj" / "tvvideo.py").read_text(encoding="utf-8")
                for banned in ("ytdl_resolver", "RESOLVER_SOCKET", "_resolver_call", "loadfile",
                               "MPV_SOCKET", "toggle_pause", "enqueue"):
                    self.assertNotIn(banned, src, banned)
                # adresa proudu: jen v souboru pro téhož uživatele, nikdy v logu ani ve stavu
                f = rig.tv.stream_file(CLIP)
                self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
                saved = json.loads(f.read_text())
                self.assertEqual((saved["id"], saved["height"]), (CLIP, 720))
                self.assertNotIn("Cookie", json.dumps(saved))
                self.assertNotIn("googlevideo", json.dumps([e for e in rig.events], default=str))
                self.assertNotIn("googlevideo", json.dumps(rig.tv.public()))
                ev = [f for k, f in rig.events if k == "tv.video_resolve"][-1]
                self.assertEqual((ev["ok"], ev["height"], ev["video_id"]), (True, 720, CLIP))
        run(go())

    def test_next_clip_is_prepared_ahead_one_at_a_time(self):
        async def go():
            with Rig(current=CLIP, queue=(NEXT_CLIP, SONG), ytdlp_sleep=0.2) as rig:
                rig.has_picture(CLIP)
                rig.has_picture(NEXT_CLIP)
                await rig.tv.set(True, "x")
                await rig.tv.tick()
                await rig.tv.tick()
                self.assertEqual(rig.tv._resolving, CLIP)  # jeden po druhém: nejdřív hrající
                self.assertLessEqual(len(rig.ytdlp_calls()), 1)
                await rig.settle(lambda: rig.tv.stream_file(NEXT_CLIP).exists())
                self.assertEqual(rig.tv.video, CLIP)
                self.assertEqual(len(rig.ytdlp_calls()), 2)
                # další skladba začne: obraz je hned, nic se nehledá
                rig.player.current, rig.player.queue = NEXT_CLIP, [SONG]
                await rig.tv.tick()
                self.assertEqual((rig.tv.video, len(rig.ytdlp_calls())), (NEXT_CLIP, 2))
                self.assertNotIn(SONG, " ".join(rig.ytdlp_calls()))
        run(go())

    def test_no_picture_is_given_up_quietly_and_not_retried_in_a_loop(self):
        async def go():
            with Rig(current=CLIP) as rig:  # yt-dlp nic nevrátí (nebo jen VP9)
                await rig.tv.set(True, "x")
                await rig.settle(lambda: any(k == "tv.video_resolve" for k, _ in rig.events))
                for _ in range(5):
                    await rig.tv.tick()
                    await asyncio.sleep(0.02)
                p = rig.tv.public()
                self.assertEqual((p["video"], p["pending"]), (None, False))  # telka: obrazovka
                self.assertEqual(len(rig.ytdlp_calls()), 1)
                ev = [f for k, f in rig.events if k == "tv.video_resolve"][-1]
                self.assertEqual((ev["ok"], ev["error"]), (False, "no_avc1_720"))
            with Rig(current=CLIP) as rig:
                rig.has_picture(CLIP, vcodec="vp09.00.31.08")
                await rig.tv.set(True, "x")
                await rig.settle(lambda: any(k == "tv.video_resolve" for k, _ in rig.events))
                self.assertIsNone(rig.tv.video)
                self.assertFalse(rig.tv.stream_file(CLIP).exists())
        run(go())

    def test_expiring_address_is_fetched_again_and_nothing_is_left_behind(self):
        async def go():
            with Rig(current=CLIP) as rig:
                rig.has_picture(CLIP, url=URL.format(exp=int(time.time()) + 120))  # platí už jen 2 min
                await rig.tv.set(True, "x")
                await rig.settle(lambda: len(rig.ytdlp_calls()) >= 2)  # hledá se nová
                self.assertTrue(rig.tv.stream_dir.exists())
                await rig.tv.stop()
                self.assertFalse(rig.tv.stream_dir.exists())  # adresy proudů po sobě nenechává
            # když telka klipy zrovna nesmí (ochrana), obraz se nehledá
            with Rig(current=CLIP, tv={"can": True, "mode": "screen", "blocked": "jukebox je horký"}) as rig:
                rig.has_picture(CLIP)
                await rig.tv.set(True, "x")
                for _ in range(3):
                    await rig.tv.tick()
                self.assertEqual(rig.ytdlp_calls(), [])
        run(go())


class Web(unittest.TestCase):
    def srv(self, rig, wq=None):
        async def body(request):
            return json.loads(await request.body() or b"{}")

        poked = []
        app = SimpleNamespace(tvvideo=rig.tv, wishes=wq)
        return SimpleNamespace(app=app, _body=body, poke=lambda: poked.append(1)), poked

    def req(self, body, ip="10.0.0.7"):
        async def read():
            return json.dumps(body).encode()

        return SimpleNamespace(client=SimpleNamespace(host=ip), body=read, method="POST",
                               headers={"user-agent": "Mozilla/5.0 (X11; Linux) Chrome/130"},
                               url=SimpleNamespace(path="/api/tv"), path_params={})

    def test_anyone_switches_it_without_a_pin_and_everybody_sees_it(self):
        async def go():
            with Rig() as rig:
                srv, poked = self.srv(rig, SimpleNamespace(name_for=lambda cid, raw: "Kolega"))
                route = tv_api.routes(srv)[0]
                self.assertEqual((route.path, sorted(route.methods)), ("/api/tv", ["POST"]))
                resp = await route.endpoint(self.req({"video": True, "who": "x", "client": "c1"}))
                self.assertEqual(resp.status_code, 200)
                data = json.loads(resp.body)
                self.assertTrue(data["ok"] and data["tv"]["on"])
                self.assertEqual(data["tv"]["by"], "Kolega")  # přezdívka podle zařízení
                self.assertTrue(poked)
                self.assertTrue(tv_api.public(srv.app)["on"])
                resp = await route.endpoint(self.req({"video": False}))
                self.assertFalse(json.loads(resp.body)["tv"]["on"])
                # nesmysl místo ano/ne
                self.assertEqual((await route.endpoint(self.req({"video": "ano"}))).status_code, 400)
                # brzda proti cvakání z jedné adresy; jiná adresa jde dál
                codes = [(await route.endpoint(self.req({"video": True}, ip="10.0.0.9"))).status_code
                         for _ in range(tv_api.LIMIT + 2)]
                self.assertEqual(codes.count(429), 2)
                self.assertEqual((await route.endpoint(self.req({"video": True}, ip="10.0.0.8")))
                                 .status_code, 200)
                # bez klipů (starý běh) stav klíč `tv` nemá a cesta řekne 404
                self.assertIsNone(tv_api.public(SimpleNamespace()))
                none, _ = self.srv(rig)
                none.app.tvvideo = None
                self.assertEqual((await tv_api.routes(none)[0].endpoint(self.req({"video": True})))
                                 .status_code, 404)
        run(go())

    def test_wired_into_the_server_status_and_the_page(self):
        server = (ROOT / "ytdj" / "web" / "server.py").read_text(encoding="utf-8")
        self.assertIn("*tv_api.routes(self)", server)
        self.assertIn('extra["tv"] = tv', server)
        # PIN správce na vypínači není (smí kdokoli, jako hlasitost)
        self.assertNotIn("_admin_only", (ROOT / "ytdj" / "web" / "tv_api.py").read_text())
        page = (ROOT / "ytdj" / "web" / "static" / "index.html").read_text(encoding="utf-8")
        for piece in ('id="tvBtn"', "Klipy na telce: ", 'postJSON("/api/tv"', "showTv(s.tv)",
                      "el.tvBtn.disabled = !tv.can && !on"):
            self.assertIn(piece, page, piece)
        main = (ROOT / "ytdj" / "__main__.py").read_text(encoding="utf-8")
        self.assertIn("await self.tvvideo.load()", main)
        self.assertIn('DATA_DIR / "tv-video.json"', main)


if __name__ == "__main__":
    unittest.main()
