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

    def __init__(self, current=None, queue=(), tv=None, ytdlp_sleep=0.0, fallback_after=0.0):
        self.dir = Path(tempfile.mkdtemp(dir=_TMP))
        self.cache = self.dir / "resolver-cache"
        self._fallback = mock.patch.object(tvvideo, "FALLBACK_AFTER", fallback_after)
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
                          yt_dlp_args=lambda cfg: [str(fake), "--cookies", "/tajne/cookies.txt"],
                          resolver_cache=self.cache)
        self.events: list[tuple[str, dict]] = []
        self._tel = mock.patch.object(telemetry, "event",
                                      lambda kind, **f: self.events.append((kind, f)))

    def _changed(self):
        self.changes += 1

    def tv_says(self, report: dict) -> None:
        self.tv_status.write_text(json.dumps(report))

    def resolver_has(self, vid: str, age: float = 60.0, formats: list | None = None) -> None:
        """Resolver hudby má skladbu hotovou (řádek v jeho cache, všechny formáty)."""
        if formats is None:
            formats = [
                {"format_id": "251", "vcodec": "none", "acodec": "opus", "url": URL.format(exp=1)},
                {"format_id": "134", **{k: v for k, v in info(vid, height=360).items()
                                        if k not in ("id", "format_id")}, "tbr": 400},
                {"format_id": "136", **{k: v for k, v in info(vid).items()
                                        if k not in ("id", "format_id")}, "tbr": 1000},
                {"format_id": "137", **{k: v for k, v in info(vid, height=1080).items()
                                        if k not in ("id", "format_id")}, "tbr": 3000},
                {"format_id": "247", **{k: v for k, v in info(vid, vcodec="vp09.00.31.08").items()
                                        if k not in ("id", "format_id")}, "tbr": 900},
                {"format_id": "232", **{k: v for k, v in info(vid).items()
                                        if k not in ("id", "format_id")}, "protocol": "m3u8_native"},
            ]
        line = f"{vid}\t{time.time() - age!r}\t" + json.dumps(
            {"id": vid, "format_id": "251", "url": URL.format(exp=1), "formats": formats})
        old = self.cache.read_text() if self.cache.exists() else \
            'YTDJ-RESOLVER-CACHE 1 {"template": [], "saved": 0}\n'
        self.cache.write_text(old + line + "\n")

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
        self._fallback.start()
        return self

    def __exit__(self, *a):
        self._fallback.stop()
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


class Requested(unittest.TestCase):
    """Výslovně vyžádané video se ukáže, i když není oficiální klip (POZADAVKY #73)."""

    def test_requested_video_is_shown_whatever_its_kind_but_a_song_never(self):
        async def go():
            for vid, asked, expect, reason in (
                    (UGC, True, UGC, ""),  # koncert od cizího uživatele z odkazu → ukázat
                    ("neznameid00", True, "neznameid00", ""),  # video mimo katalog → ukázat
                    (SONG, True, None, "audio_only"),  # písnička obraz nemá, ani na přání
                    (UGC, False, None, "not_official"),  # co jukebox vybral sám: jen oficiální
                    (CLIP, False, CLIP, "")):
                with Rig(current=vid) as rig:
                    rig.tv._explicit = lambda v, asked=asked: asked
                    rig.resolver_has(vid)
                    await rig.tv.set(True, "x")
                    await rig.tv.tick()
                    p = rig.tv.public()
                    self.assertEqual((p["video"], p["explicit"]), (expect, asked and bool(expect)
                                                                   or (asked and vid == SONG)), vid)
                    if expect:
                        self.assertEqual(p["reason"], "loading")  # připravený, telka ho rozjíždí
                        rig.tv_says({"can": True, "mode": "video", "blocked": ""})
                        rig.tv._report_mono = -1e9
                        await rig.tv.tick()
                        p = rig.tv.public()
                        self.assertEqual((p["reason"], p["note"]), ("", ""))
                    else:
                        self.assertEqual(p["reason"], reason, vid)
                        self.assertTrue(p["note"] and p["note_tv"])
        run(go())

    def test_reason_is_always_given_and_logged_once_per_track(self):
        async def go():
            with Rig(current=SONG) as rig:
                await rig.tv.set(True, "x")
                for _ in range(4):
                    await rig.tv.tick()
                p = rig.tv.public()
                self.assertEqual(p["note"], "tahle skladba je jen zvuk (není to klip)")
                self.assertEqual(p["note_tv"], "bez klipu — jen zvuk")
                skips = [f for k, f in rig.events if k == "tv.video_skip"]
                self.assertEqual(len(skips), 1)  # jednou na skladbu
                self.assertEqual((skips[0]["reason"], skips[0]["video_id"]), ("audio_only", SONG))
                # cizí nahrávka, kterou vybral jukebox
                rig.player.current = UGC
                await rig.tv.tick()
                self.assertIn("není to oficiální klip", rig.tv.public()["note"])
                self.assertEqual([f["reason"] for k, f in rig.events if k == "tv.video_skip"],
                                 ["audio_only", "not_official"])
                # klip bez vhodného formátu
                rig.player.current = CLIP
                await rig.settle(lambda: rig.tv.public()["reason"] == "no_format")
                self.assertEqual(rig.tv.public()["note"], "pro tuhle skladbu není vhodný formát obrazu")
                # druh se nepodařilo zjistit
                rig.catalog.fail = True
                rig.player.current = NEXT_CLIP
                await rig.tv.tick()
                self.assertEqual(rig.tv.public()["reason"], "unknown")
                # nic nehraje / vypnuto → žádná poznámka
                rig.player.current = None
                await rig.tv.tick()
                self.assertEqual(rig.tv.public()["note"], "")
                await rig.tv.set(False, "x")
                self.assertEqual(rig.tv.public()["reason"], "")
            # ochrana na telce (paměť, teplo) a nepřipojená telka: taky řečeno
            with Rig(current=CLIP, tv={"can": True, "mode": "screen",
                                       "blocked": "málo volné paměti"}) as rig:
                rig.resolver_has(CLIP)
                await rig.tv.set(True, "x")
                await rig.tv.tick()
                p = rig.tv.public()
                self.assertEqual((p["reason"], p["note"], p["note_tv"]),
                                 ("blocked", "málo volné paměti — klip je vypnutý",
                                  "klip vypnutý: málo volné paměti"))
            with Rig(current=CLIP, tv={"can": False, "why": "Telka není připojená (HDMI)."}) as rig:
                await rig.tv.set(True, "x")
                await rig.tv.tick()
                p = rig.tv.public()
                self.assertEqual((p["reason"], p["note"]), ("tv_off", "Telka není připojená (HDMI)."))
            page = (ROOT / "ytdj" / "web" / "static" / "index.html").read_text(encoding="utf-8")
            self.assertIn('else if (tv.note) note = "Teď bez obrazu: " + tv.note', page)
        run(go())

    def test_who_asked_comes_from_the_wish_and_lasts_as_long_as_it(self):
        """Odkaz na video a přání „i s obrazem“ — z fronty přání, přežije restart."""
        from test_wishes import W
        from ytdj.wishes import Wish, WishQueue

        wq = WishQueue.__new__(WishQueue)
        wq.owner, wq.wishes = {}, []
        wq.pools = SimpleNamespace(video_only=False)
        wq.by_id = lambda wid: next((w for w in wq.wishes if w.id == wid), None)
        link = W("Petr", 1, 1.0, kind="songs")
        link.via = "link"  # odkaz na jedno video
        playlist = W("Jana", 1, 2.0, kind="song")
        playlist.via = "link"  # odkaz na playlist: skladby vybírá jukebox
        video = W("Eva", 1, 3.0, kind="artist")
        video.want_video = True  # „pusť video…“
        plain = W("Karel", 1, 4.0, kind="songs")
        # „pusť video – koncert…“, oficiální klip se nenašel a hraje nahrávka od uživatele
        concert = W("Ota", 1, 5.0, kind="songs")
        concert.asked_video = True
        wq.wishes = [link, playlist, video, plain, concert]
        wq.owner = {"vidlink0000": link.id, "vidplay0000": playlist.id, "vidvideo000": video.id,
                    "vidplain000": plain.id, "vidconcert0": concert.id}
        self.assertTrue(wq.picture_wanted("vidconcert0"))
        self.assertTrue(Wish.from_json(concert.to_json()).asked_video)
        src = (ROOT / "ytdj" / "wishes.py").read_text(encoding="utf-8")
        self.assertIn('w.asked_video = bool(getattr(intent, "want_video", False))', src)
        self.assertTrue(wq.picture_wanted("vidlink0000"))
        self.assertTrue(wq.picture_wanted("vidvideo000"))
        self.assertFalse(wq.picture_wanted("vidplay0000"))
        self.assertFalse(wq.picture_wanted("vidplain000"))
        self.assertFalse(wq.picture_wanted("podkres0000"))  # co vybral jukebox
        self.assertFalse(wq.picture_wanted(None))
        wq.pools.video_only = True  # podkres z klipů k přání s obrazem
        self.assertTrue(wq.picture_wanted("podkres0000"))
        # s přáním to přežije restart …
        for w in (link, video):
            back = Wish.from_json(w.to_json())
            self.assertEqual((back.via, back.kind, back.want_video), (w.via, w.kind, w.want_video))
        # … a skončí s ním
        wq.wishes, wq.owner = [plain], {"vidplain000": plain.id}
        wq.pools.video_only = False
        self.assertFalse(wq.picture_wanted("vidlink0000"))
        main = (ROOT / "ytdj" / "__main__.py").read_text(encoding="utf-8")
        self.assertIn("explicit=lambda vid: self.wishes.picture_wanted(vid)", main)

    def test_long_track_gets_a_fresh_address_before_the_old_one_runs_out(self):
        """Dvouhodinové video: adresa obrazu platí ~6 h, ale ne věčně — než vyprší,
        vezme se nová (z obnovené cache resolveru, jinak vlastním hledáním)."""
        async def go():
            with Rig(current=CLIP) as rig:
                rig.resolver_has(CLIP)
                await rig.tv.set(True, "x")
                await rig.tv.tick()
                self.assertEqual(rig.tv.video, CLIP)
                first = json.loads(rig.tv.stream_file(CLIP).read_text())["url"]
                # po hodinách hraní: adrese zbývá míň než rezerva
                rig.tv._streams[CLIP]["expire"] = time.time() + 120
                rig.cache.unlink()
                fresh = [{"format_id": "136", **{k: v for k, v in info(
                    CLIP, url=URL.format(exp=int(time.time()) + 5 * 3600) + "&novy=1").items()
                    if k not in ("id", "format_id")}, "tbr": 1000}]
                rig.resolver_has(CLIP, formats=fresh)  # resolver hudby ji mezitím obnovil
                await rig.tv.tick()
                self.assertEqual(rig.tv.video, CLIP)
                second = json.loads(rig.tv.stream_file(CLIP).read_text())["url"]
                self.assertNotEqual(first, second)
                self.assertIn("novy=1", second)
                self.assertEqual(rig.ytdlp_calls(), [])
            # paměť přehrávače klipu nezávisí na délce: pevné stropy
            from_src = (ROOT / "ytdj" / "tv" / "video.py").read_text(encoding="utf-8")
            for opt in ("--demuxer-max-bytes=24MiB", "--demuxer-max-back-bytes=4MiB", "--cache-secs=6"):
                self.assertIn(opt, from_src)
        run(go())


class PlayerNotReady(unittest.TestCase):
    def test_player_that_is_not_up_is_a_normal_state_not_an_error(self):
        """Po startu služby krok běží dřív než mpv: žádná chyba v logu, čeká se."""
        async def go():
            with Rig(current=CLIP) as rig:
                rig.resolver_has(CLIP)
                await rig.tv.set(True, "x")
                real = rig.player.status

                async def down():
                    raise RuntimeError("mpv neběží")

                rig.player.status = down
                with self.assertNoLogs("ytdj.tvvideo", level="INFO"):  # ani varování, ani chyba
                    for _ in range(3):
                        await rig.tv.tick()
                self.assertIsNone(rig.tv.public()["video"])
                # smyčka běží dál a první krok po naběhnutí přehrávače funguje
                with self.assertNoLogs("ytdj.tvvideo", level="WARNING"):
                    with mock.patch.object(tvvideo, "TICK", 0.02):
                        rig.tv.start()
                        await asyncio.sleep(0.1)
                        rig.player.status = real
                        await asyncio.sleep(0.2)
                self.assertEqual(rig.tv.video, CLIP)
                # přehrávač spadne za běhu: klip se odvolá, zase bez chyby
                rig.player.status = down
                with self.assertNoLogs("ytdj.tvvideo", level="INFO"):
                    await rig.tv.tick()
                self.assertIsNone(rig.tv.video)
                await rig.tv.stop()
            # při ukončení se smyčka zastavuje dřív než přehrávač
            main = (ROOT / "ytdj" / "__main__.py").read_text(encoding="utf-8")
            self.assertLess(main.index("await self.tvvideo.stop()"),
                            main.index("await self.player.stop()  # playback.json"))
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

    def test_picture_comes_from_what_the_music_resolver_already_has(self):
        """První klip na Pi čekal 28 s na druhé yt-dlp; adresa obrazu přitom
        byla v hotovém výsledku resolveru hudby. Teď se bere odtud."""
        async def go():
            with Rig(current=CLIP, queue=(NEXT_CLIP,)) as rig:
                rig.resolver_has(CLIP)
                rig.resolver_has(NEXT_CLIP)
                await rig.tv.set(True, "x")
                t0 = time.monotonic()
                await rig.tv.tick()
                self.assertLess(time.monotonic() - t0, 0.5)
                self.assertEqual(rig.tv.video, CLIP)  # hned, bez čekání
                self.assertEqual(rig.ytdlp_calls(), [])  # žádné další yt-dlp
                saved = json.loads(rig.tv.stream_file(CLIP).read_text())
                # nejlepší přímý H.264 do 720p: ne 1080p, ne VP9, ne HLS, ne zvuk
                self.assertEqual((saved["format"], saved["height"], saved["id"]), ("136", 720, CLIP))
                self.assertTrue(rig.tv.stream_file(NEXT_CLIP).exists())  # i další skladba dopředu
                evs = [f for k, f in rig.events if k == "tv.video_resolve"]
                self.assertEqual({(e["source"], e["ok"]) for e in evs}, {("cache", True)})
                self.assertNotIn("googlevideo", json.dumps(evs))
                # resolveru se nic neposílá a jeho soubor se jen čte
                src = (ROOT / "ytdj" / "tvvideo.py").read_text(encoding="utf-8")
                self.assertNotIn("socket", src)
                self.assertEqual(src.count("open(self.resolver_cache"), 1)
                # čtení souboru i rozbor běží ve vlákně
                self.assertIn("await asyncio.to_thread(self._cache_lookup, video_id)", src)
        run(go())

    def test_cache_trust_rules_and_fallback_only_after_a_while(self):
        async def go():
            line = lambda **kw: None  # noqa: E731
            # pravidla důvěry jako u zvuku: stáří výsledku a platnost adresy
            with Rig(current=CLIP) as rig:
                rig.resolver_has(CLIP, age=91 * 60)  # starší než 90 min
                self.assertEqual(rig.tv._cache_lookup(CLIP), (None, "old"))
            with Rig(current=CLIP) as rig:
                soon = [{"format_id": "136", **{k: v for k, v in info(
                    CLIP, url=URL.format(exp=int(time.time()) + 300)).items()
                    if k not in ("id", "format_id")}}]
                rig.resolver_has(CLIP, formats=soon)  # adresa vyprší za 5 min
                self.assertEqual(rig.tv._cache_lookup(CLIP), (None, "no_avc1_720"))
                self.assertEqual(rig.tv._cache_lookup(NEXT_CLIP), (None, "missing"))
                rig.cache.write_text("nesmysl\n" + CLIP + "\tabc\t{\n")
                self.assertEqual(rig.tv._cache_lookup(CLIP), (None, "bad"))
                rig.cache.unlink()
                self.assertEqual(rig.tv._cache_lookup(CLIP), (None, "missing"))
            # v cache není: vlastní yt-dlp až když skladba už chvíli hraje
            with Rig(current=CLIP, fallback_after=0.6) as rig:
                rig.has_picture(CLIP)
                await rig.tv.set(True, "x")
                for _ in range(3):
                    await rig.tv.tick()
                    await asyncio.sleep(0.05)
                self.assertEqual(rig.ytdlp_calls(), [])  # zvuk se možná ještě řeší
                self.assertTrue(rig.tv.public()["pending"])  # web: „obraz se chystá“
                await asyncio.sleep(0.6)
                await rig.settle(lambda: rig.tv.video == CLIP)
                ev = [f for k, f in rig.events if k == "tv.video_resolve"][-1]
                self.assertEqual((ev["source"], ev["cache"]), ("ytdlp", "missing"))
            # resolver mezitím skladbu dořešil → obraz odtud, yt-dlp se nespustí
            with Rig(current=CLIP, fallback_after=30.0) as rig:
                await rig.tv.set(True, "x")
                await rig.tv.tick()
                self.assertIsNone(rig.tv.video)
                rig.resolver_has(CLIP)
                await rig.tv.tick()
                self.assertEqual((rig.tv.video, rig.ytdlp_calls()), (CLIP, []))
            self.assertGreaterEqual(tvvideo.FALLBACK_AFTER, 15)
        run(go())

    def test_next_clip_is_never_resolved_by_our_own_yt_dlp(self):
        async def go():
            with Rig(current=CLIP, queue=(NEXT_CLIP, SONG), ytdlp_sleep=0.2) as rig:
                rig.has_picture(CLIP)
                rig.has_picture(NEXT_CLIP)
                await rig.tv.set(True, "x")
                await rig.settle(lambda: rig.tv.video == CLIP)
                for _ in range(3):
                    await rig.tv.tick()
                self.assertEqual(len(rig.ytdlp_calls()), 1)  # jen hrající; další chystá resolver
                self.assertNotIn(NEXT_CLIP, " ".join(rig.ytdlp_calls()))
                # resolver ji mezitím připravil → obraz je hned, když začne
                rig.resolver_has(NEXT_CLIP)
                rig.player.current, rig.player.queue = NEXT_CLIP, [SONG]
                await rig.tv.tick()
                self.assertEqual((rig.tv.video, len(rig.ytdlp_calls())), (NEXT_CLIP, 1))
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


class TvPowerWeb(unittest.TestCase):
    """Telka z webu (POZADAVKY #78): stav telky a tlačítka Vypnout / Zapnout."""

    ON = {"can": True, "why": "", "mode": "screen", "blocked": "", "power": "on",
          "power_tv": "on", "power_by": "", "power_route": "ours"}
    srv, req = Web.srv, Web.req

    async def ask(self, route, action, ip="10.0.0.7"):
        resp = await route.endpoint(self.req({"power": action, "who": "x", "client": "c1"}, ip=ip))
        return resp.status_code, json.loads(resp.body)

    async def read(self, rig):
        rig.tv._report_mono = -1e9
        await rig.tv.tick()

    def test_anyone_asks_without_a_pin_and_the_tv_process_gets_the_request(self):
        async def go():
            with Rig(tv=self.ON) as rig:
                await self.read(rig)
                p = rig.tv.public()
                self.assertEqual((p["power_tv"], p["power_runs"], p["power_request"], p["power_ctl"]),
                                 ("on", True, None, None))
                srv, poked = self.srv(rig, SimpleNamespace(name_for=lambda cid, raw: "Kolega"))
                route = tv_api.routes(srv)[0]
                code, data = await self.ask(route, "off")
                self.assertEqual(code, 200)
                self.assertTrue(poked)  # stav jde hned všem — i procesu na telce
                tv = data["tv"]
                self.assertEqual(tv["power_request"]["action"], "off")
                self.assertIsInstance(tv["power_request"]["id"], int)
                self.assertEqual(tv["power_ctl"], {"state": "sending", "action": "off",
                                                   "who": "Kolega", "note": ""})
                ev = [f for k, f in rig.events if k == "tv.power_ask"]
                self.assertEqual(len(ev), 1)
                self.assertEqual((ev[0]["action"], ev[0]["who"], ev[0]["tv"], ev[0]["ip"]),
                                 ("off", "Kolega", "on", "10.0.0.7"))
                # nesmysl místo on/off
                self.assertEqual((await self.ask(route, "reboot"))[0], 400)
                # jukebox telku neovládá sám — jen předá přání (viz test péče o telku)
                text = (ROOT / "ytdj" / "tvvideo.py").read_text(encoding="utf-8")
                self.assertNotIn("cec-ctl", text)
            # obrazovka na telce neběží (nebo dlouho mlčí): řekne to a nic neslibuje
            for rig_kw, stale in (({"tv": False}, False), ({"tv": self.ON}, True)):
                with Rig(**rig_kw) as rig:
                    await self.read(rig)
                    if stale:
                        rig.tv._report_at -= tvvideo.TV_STATUS_FRESH + 10
                    srv, _ = self.srv(rig)
                    code, data = await self.ask(tv_api.routes(srv)[0], "on")
                    self.assertEqual(code, 409)
                    self.assertIn("neběží", data["error"])
                    p = rig.tv.public()
                    self.assertEqual((p["power_tv"], p["power_runs"], p["power_request"]),
                                     ("", False, None))
        run(go())

    def test_one_at_a_time_ten_seconds_apart_and_the_result_comes_from_the_tv(self):
        async def go():
            with Rig(tv=self.ON) as rig:
                await self.read(rig)
                srv, _ = self.srv(rig)
                route = tv_api.routes(srv)[0]
                code, data = await self.ask(route, "off")
                rid = data["tv"]["power_request"]["id"]
                # dokud telka nepotvrdí, další povel nejde — ani od někoho jiného
                code, data = await self.ask(route, "on", ip="10.0.0.99")
                self.assertEqual(code, 429)
                self.assertIn("už jeden povel", data["error"])
                await self.read(rig)
                self.assertEqual(rig.tv.public()["power_ctl"]["state"], "sending")  # nic nepředstírá
                # proces na telce: vypnuto a potvrzeno stavem telky
                rig.tv_says({**self.ON, "power_tv": "standby", "power_by": "people",
                             "power_done_id": rid, "power_done_action": "off",
                             "power_done_ok": True, "power_done_note": "Telka je vypnutá."})
                changes = rig.changes
                await self.read(rig)
                p = rig.tv.public()
                self.assertGreater(rig.changes, changes)  # web se to dozví hned
                self.assertEqual((p["power_tv"], p["power_by"], p["power_request"]),
                                 ("standby", "people", None))
                self.assertEqual(p["power_ctl"], {"state": "ok", "action": "off", "who": "x",
                                                  "note": "Telka je vypnutá."})
                ev = [f for k, f in rig.events if k == "tv.power_result"]
                self.assertEqual([(e["action"], e["ok"], e["who"]) for e in ev], [("off", True, "x")])
                # hned další povel: ještě ne (10 s mezi povely pro celý jukebox)
                code, data = await self.ask(route, "on", ip="10.0.0.99")
                self.assertEqual(code, 429)
                self.assertIn("počkej", data["error"])
                rig.tv._power["at"] -= tvvideo.POWER_GAP + 1
                code, data = await self.ask(route, "on", ip="10.0.0.99")
                self.assertEqual(code, 200)
                self.assertGreater(data["tv"]["power_request"]["id"], rid)  # čísla jen rostou
                # starý výsledek jiného povelu se za tenhle nevydává
                await self.read(rig)
                self.assertEqual(rig.tv.public()["power_ctl"]["state"], "sending")
                self.assertGreaterEqual(tvvideo.POWER_GAP, 10)
        run(go())

    def test_request_numbers_only_grow_whatever_the_clock_does(self):
        """Číslo povelu je čas v ms, ale nesmí se opakovat ani klesnout: proces
        na telce bere jen číslo vyšší, než jaké už vyřídil, a web podle čísla
        pozná, ke kterému povelu potvrzení patří."""
        async def go():
            with Rig(tv=self.ON) as rig:
                await self.read(rig)
                srv, _ = self.srv(rig)
                route = tv_api.routes(srv)[0]
                t0 = time.time()
                with mock.patch.object(tvvideo.time, "time", lambda: t0):
                    # dva povely ve stejné milisekundě (hodiny stojí)
                    code, data = await self.ask(route, "off")
                    rid = data["tv"]["power_request"]["id"]
                    self.assertEqual(rid, int(t0 * 1000))  # běžně je to čas
                    rig.tv_says({**self.ON, "power_done_id": rid, "power_done_action": "off",
                                 "power_done_ok": False, "power_done_note": "Nepovedlo se."})
                    await self.read(rig)
                    self.assertEqual(rig.tv.public()["power_ctl"]["state"], "failed")
                    rig.tv._power = None  # výsledek z webu zmizel
                    code, data = await self.ask(route, "on")
                    self.assertEqual(code, 200)
                    self.assertGreater(data["tv"]["power_request"]["id"], rid)
                    await self.read(rig)  # výsledek minulého povelu se za tenhle nevydává
                    self.assertEqual(rig.tv.public()["power_ctl"]["state"], "sending")
                    rid = data["tv"]["power_request"]["id"]
                # hodiny skočily zpátky (i přes restart jukeboxu: telka si číslo pamatuje)
                rig.tv_says({**self.ON, "power_done_id": rid, "power_done_action": "on",
                             "power_done_ok": True, "power_done_note": "Telka je zapnutá."})
                rig.tv._power, rig.tv._power_id = None, 0  # jako po restartu
                await self.read(rig)
                with mock.patch.object(tvvideo.time, "time", lambda: t0 - 3600):
                    code, data = await self.ask(route, "off")
                    self.assertEqual(code, 200)
                    self.assertGreater(data["tv"]["power_request"]["id"], rid)
                    await self.read(rig)
                    self.assertEqual(rig.tv.public()["power_ctl"]["state"], "sending")
        run(go())

    def test_no_confirmation_is_said_honestly_and_the_result_fades(self):
        async def go():
            with Rig(tv=self.ON) as rig:
                await self.read(rig)
                srv, _ = self.srv(rig)
                route = tv_api.routes(srv)[0]
                code, data = await self.ask(route, "off")
                rid = data["tv"]["power_request"]["id"]
                # telka povel přijala, ale nevypnula se: řekne to proces na telce
                rig.tv_says({**self.ON, "power_done_id": rid, "power_done_action": "off",
                             "power_done_ok": False, "power_done_note": "Telka povel nepotvrdila."})
                await self.read(rig)
                p = rig.tv.public()
                self.assertEqual((p["power_ctl"]["state"], p["power_ctl"]["note"], p["power_tv"]),
                                 ("failed", "Telka povel nepotvrdila.", "on"))
                rig.tv._power["end"] -= tvvideo.POWER_SHOW + 1  # po chvíli hláška zmizí
                await self.read(rig)
                self.assertIsNone(rig.tv.public()["power_ctl"])
                # proces na telce se neozval vůbec: po minutě to web řekne sám
                code, data = await self.ask(route, "on")
                self.assertEqual(code, 200)
                await self.read(rig)
                self.assertEqual(rig.tv.public()["power_ctl"]["state"], "sending")
                rig.tv._power["at"] -= tvvideo.POWER_WAIT + 1
                await self.read(rig)
                p = rig.tv.public()
                self.assertEqual(p["power_ctl"]["state"], "failed")
                self.assertIn("nepotvrdila", p["power_ctl"]["note"])
                self.assertIsNone(p["power_request"])  # a povel už na telku nečeká
                ev = [f for k, f in rig.events if k == "tv.power_result"]
                self.assertEqual([e["ok"] for e in ev], [False, False])
        run(go())

    def test_page_has_the_tv_row_with_state_and_buttons(self):
        page = (ROOT / "ytdj" / "web" / "static" / "index.html").read_text(encoding="utf-8")
        for piece in ('id="tvPowRow"', 'id="tvPowBtn"', "Vypnout telku", "Zapnout telku",
                      "Telka je zapnutá", "Telka je vypnutá", "Telka neodpovídá na HDMI-CEC",
                      "Zapni na ní Anynet+", 'postJSON("/api/tv", { power: act',
                      "el.tvPowBtn.disabled = sending", "Posílám povel",
                      "Hudba ji sama nezapne"):
            self.assertIn(piece, page, piece)
        # PIN správce na tom není (smí kdokoli)
        self.assertNotIn("_admin_only", (ROOT / "ytdj" / "web" / "tv_api.py").read_text())
        helppage = (ROOT / "ytdj" / "web" / "static" / "napoveda.html").read_text(encoding="utf-8")
        self.assertIn("Vypnout telku", helppage)


if __name__ == "__main__":
    unittest.main()
