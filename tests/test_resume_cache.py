"""Restart služby bez ticha: cache resolveru v RAM (tmpfs), kontrola adres,
shim čekající na socket a navázání přerušené skladby.

    YTDJ_EVENTS_FILE=/tmp/x.jsonl python -m unittest tests.test_resume_cache
"""

from __future__ import annotations

import asyncio
import http.server
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="ytdj-resume-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_player_queue import Harness, T, run, vid  # noqa: E402
from ytdj.player import ytdl_cache  # noqa: E402

TEMPLATE = ["--format=774/251", "-J", "--"]


def load_resolver():
    """ytdl_resolver bez skutečného yt-dlp (import je stejně líný)."""
    stub = types.ModuleType("yt_dlp")
    with mock.patch.dict(sys.modules, {"yt_dlp": stub}):
        sys.modules.pop("ytdj.player.ytdl_resolver", None)
        from ytdj.player import ytdl_resolver as r
    return r


def info(v: str, url: str = "https://rr1.googlevideo.com/videoplayback?x=1",
         expire: int | None = None) -> str:
    if expire is None:
        expire = int(time.time()) + 6 * 3600
    return json.dumps({"id": v, "url": f"{url}&expire={expire}",
                       "http_headers": {"User-Agent": "test"}})


def argv(v: str) -> list[str]:
    return TEMPLATE + [f"https://music.youtube.com/watch?v={v}"]


class FakeYdl:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def extract_info(self, url: str, download: bool = False):
        v = url.rsplit("v=", 1)[-1]
        self.calls.append(v)
        return {"id": v, "url": f"https://rr9.googlevideo.com/videoplayback?fresh=1&v={v}"}

    def sanitize_info(self, i):
        return i


class DiskCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.r = load_resolver()
        self.dir = Path(tempfile.mkdtemp(dir=_TMP))
        self.path = str(self.dir / "resolver-cache")
        self.err = io.StringIO()
        self._red = redirect_stderr(self.err)
        self._red.__enter__()

    def tearDown(self) -> None:
        self._red.__exit__(None, None, None)

    def events(self, kind: str) -> list[dict]:
        out = []
        for line in self.err.getvalue().splitlines():
            if line.startswith("EVENT "):
                d = json.loads(line[6:])
                if d.get("kind") == kind:
                    out.append(d)
        return out

    def fresh(self):
        return self.r.Resolver(self.r.DiskCache(self.path))

    def test_round_trip(self) -> None:
        r = self.r
        a = self.fresh()
        a.template = list(TEMPLATE)
        now = time.time()
        a.ready = {vid(1): (now - 60, info(vid(1))), vid(2): (now - 30, info(vid(2)))}
        a.flush()
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        # beze změny se nepíše znovu
        self.assertFalse(a.disk.write(list(TEMPLATE),
                                      [(v, t, d) for v, (t, d) in sorted(a.ready.items(),
                                       key=lambda x: -x[1][0])]))

        b = self.fresh()
        b.load_disk()
        self.assertEqual(b.template, TEMPLATE)
        self.assertEqual(b.ready, a.ready)
        self.assertEqual(b.from_disk, {vid(1), vid(2)})
        self.assertEqual(b.unverified, {vid(1), vid(2)})
        load = self.events("resolver.disk")[-1]
        self.assertEqual((load["phase"], load["loaded"]), ("load", 2))
        # hit z disku hned — ještě bez yt-dlp (b.loaded je False)
        with mock.patch.object(r, "probe", return_value=("ok", 206, "abc")):
            data, err = b.get(argv(vid(1)))
        self.assertEqual(data, a.ready[vid(1)][1])
        get = self.events("resolver.get")[-1]
        self.assertEqual((get["how"], get["hit"]), ("disk", True))
        self.assertNotIn(vid(1), b.unverified)
        # žádná adresa streamu v logu, jen videoId a otisk
        self.assertNotIn("googlevideo", self.err.getvalue())

    def test_expiry_and_age(self) -> None:
        r = self.r
        now = time.time()
        a = self.fresh()
        a.template = list(TEMPLATE)
        a.ready = {
            vid(1): (now - 60, info(vid(1))),                                   # dobrá
            vid(2): (now - 60, info(vid(2), expire=int(now) + 5 * 60)),         # < rezerva
            vid(3): (now - r.MAX_AGE - 1, info(vid(3))),                        # stará
            vid(4): (now + 3600, info(vid(4))),                                 # budoucí čas
        }
        a.flush()
        b = self.fresh()
        b.load_disk()
        self.assertEqual(set(b.ready), {vid(1)})
        self.assertEqual(self.events("resolver.disk")[-1]["expired"], 3)
        # i v paměti: adresa těsně před vypršením se nepodá
        self.assertFalse(r.usable(now, info(vid(5), expire=int(now) + 60)))
        self.assertTrue(r.usable(now, info(vid(5))))

    def test_corrupt_file(self) -> None:
        Path(self.path).write_text("tohle není cache\n\x00\x01")
        b = self.fresh()
        b.load_disk()
        self.assertEqual((b.template, b.ready), (None, {}))
        self.assertTrue(self.events("resolver.disk")[-1]["corrupt"])
        # vadné řádky se přeskočí, dobré zůstanou
        good = info(vid(1))
        Path(self.path).write_text(
            self.r.CACHE_MAGIC + " " + json.dumps({"template": TEMPLATE}) + "\n"
            + f"{vid(1)}\t{time.time():.3f}\t{good}\n"
            + "useknutý řádek\n"
            + f"krátké\t{time.time():.3f}\t{good}\n"
            + f"{vid(2)}\tnečíslo\t{good}\n"
            + f"{vid(3)}\t{time.time():.3f}\t{{nedopsaný\n")
        c = self.fresh()
        c.load_disk()
        self.assertEqual(set(c.ready), {vid(1)})
        self.assertEqual(self.events("resolver.disk")[-1]["bad"], 4)
        # binární smetí v UTF-8 nespadne
        Path(self.path).write_bytes(b"\xff\xfe\x00garbage")
        d = self.fresh()
        d.load_disk()
        self.assertEqual(d.ready, {})

    def test_dead_cached_url_is_resolved_fresh(self) -> None:
        r = self.r
        a = self.fresh()
        a.template = list(TEMPLATE)
        stale = info(vid(1))
        a.ready = {vid(1): (time.time() - 60, stale)}
        a.flush()
        b = self.fresh()
        b.load_disk()
        ydl = FakeYdl()
        b.loaded, b.ydl, b.ydl_for = True, ydl, b.template
        threading.Thread(target=b.worker, daemon=True).start()
        with mock.patch.object(r, "probe", return_value=("dead", 403, "abc")):
            data, err = b.get(argv(vid(1)), timeout=5)
        self.assertIsNone(err)
        self.assertIn("fresh=1", data)
        self.assertEqual(ydl.calls, [vid(1)])
        verify = [e for e in self.events("resolver.disk") if e["phase"] == "verify"][-1]
        self.assertEqual((verify["verdict"], verify["status"]), ("dead", 403))
        get = self.events("resolver.get")[-1]
        self.assertEqual((get["how"], get["hit"], get["ok"]), ("miss", False, True))
        self.assertNotIn(vid(1), b.from_disk)
        # čerstvý výsledek jde do cache na disku (zápis po dávce)
        b.flush()
        c = self.fresh()
        c.load_disk()
        self.assertIn("fresh=1", c.ready[vid(1)][1])

    def test_unknown_probe_serves_cached(self) -> None:
        """Síť nejde / timeout: podat z cache jako dřív z paměti (ne čekat na yt-dlp)."""
        r = self.r
        a = self.fresh()
        a.template = list(TEMPLATE)
        a.ready = {vid(1): (time.time() - 60, info(vid(1)))}
        a.flush()
        b = self.fresh()
        b.load_disk()
        with mock.patch.object(r, "probe", return_value=("unknown", None, "abc")):
            data, err = b.get(argv(vid(1)))
        self.assertEqual(data, a.ready[vid(1)][1])

    def test_other_template_drops_disk(self) -> None:
        """Po restartu s jiným formátem se hotové z disku nesmí podat."""
        a = self.fresh()
        a.template = list(TEMPLATE)
        a.ready = {vid(1): (time.time() - 60, info(vid(1)))}
        a.flush()
        b = self.fresh()
        b.load_disk()
        other = ["--format=251", "-J", "--", f"https://music.youtube.com/watch?v={vid(1)}"]
        with mock.patch.object(self.r, "probe") as probe:
            data, err = b.get(other, timeout=0.3)
        probe.assert_not_called()
        self.assertIsNone(data)
        self.assertEqual(err, self.r.STARTUP_ERROR)  # yt-dlp se ještě "importuje" → shim
        self.assertEqual(b.ready, {})

    def test_grace_keeps_disk_entries_then_drops(self) -> None:
        a = self.fresh()
        a.template = list(TEMPLATE)
        a.ready = {vid(1): (time.time() - 60, info(vid(1)))}
        a.flush()
        b = self.fresh()
        b.load_disk()
        b.set_ahead([], keep=[])  # fronta po restartu ještě prázdná
        self.assertIn(vid(1), b.ready)
        b.t_start -= self.r.DISK_GRACE + 1
        b.set_ahead([vid(2)])
        self.assertNotIn(vid(1), b.ready)

    def test_drop_forgets_entry(self) -> None:
        b = self.fresh()
        b.template = list(TEMPLATE)
        b.ready = {vid(1): (time.time(), info(vid(1)))}
        b.drop([vid(1), vid(2)])
        self.assertEqual(b.ready, {})
        self.assertEqual(self.events("resolver.drop")[-1]["ids"], [vid(1)])

    def test_template_from_disk_allows_prefetch_before_mpv_asks(self) -> None:
        a = self.fresh()
        a.template = list(TEMPLATE)
        a.flush()
        b = self.fresh()
        b.load_disk()
        b.set_ahead([vid(7)])
        self.assertIsNone(b._next())  # yt-dlp ještě není
        b.loaded = True
        self.assertEqual(b._next(), vid(7))


class ProbeTest(unittest.TestCase):
    """Kontrola adresy proti skutečnému HTTP serveru (lokálně)."""

    @classmethod
    def setUpClass(cls) -> None:
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                code = int(self.path.split("code=")[1].split("&")[0])
                self.send_response(code)
                self.send_header("Content-Length", "1")
                self.end_headers()
                self.wfile.write(b"x")

            def log_message(self, *a):
                pass

        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}/videoplayback?"
        cls.r = load_resolver()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.shutdown()

    def test_codes(self) -> None:
        r = self.r
        self.assertEqual(r.probe(info("a", self.base + "code=206"))[0], "ok")
        verdict, status, h = r.probe(info("a", self.base + "code=403"))
        self.assertEqual((verdict, status), ("dead", 403))
        self.assertRegex(h, r"^[0-9a-f]{10}$")
        self.assertEqual(r.probe(info("a", self.base + "code=410"))[0], "dead")
        self.assertEqual(r.probe(info("a", self.base + "code=503"))[0], "unknown")
        # audio + video zvlášť: mrtvá kterákoli = mrtvá
        both = json.dumps({"requested_formats": [{"url": self.base + "code=206"},
                                                 {"url": self.base + "code=403"}]})
        self.assertEqual(r.probe(both)[0], "dead")
        # server neběží → neví se → podat
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.assertEqual(r.probe(info("a", f"http://127.0.0.1:{port}/x?"))[0], "unknown")


class ShimTest(unittest.TestCase):
    def test_waits_for_socket_that_appears_later(self) -> None:
        path = str(Path(tempfile.mkdtemp(dir=_TMP)) / "r.sock")

        def late_server():
            time.sleep(0.4)
            srv = socket.socket(socket.AF_UNIX)
            srv.bind(path)
            srv.listen(1)
            conn, _ = srv.accept()
            with conn:
                conn.recv(65536)
                conn.sendall(json.dumps({"ok": True, "json": "{}"}).encode() + b"\n")
            srv.close()

        threading.Thread(target=late_server, daemon=True).start()
        resp = ytdl_cache._ask_resolver(path, argv(vid(1)))
        self.assertEqual(resp, {"ok": True, "json": "{}"})

    def test_gives_up_after_socket_wait(self) -> None:
        path = str(Path(tempfile.mkdtemp(dir=_TMP)) / "none.sock")
        with mock.patch.object(ytdl_cache, "SOCKET_WAIT", 0.3):
            t0 = time.monotonic()
            self.assertIsNone(ytdl_cache._ask_resolver(path, argv(vid(1))))
            self.assertLess(time.monotonic() - t0, 2)

    def test_startup_error_falls_back_to_real_ytdlp(self) -> None:
        env = {ytdl_cache.ENV_SOCKET: "/nikde", "YTDJ_TELEMETRY": "0"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(ytdl_cache, "_ask_resolver",
                                  return_value={"ok": False, "error": "resolver startuje"}), \
                mock.patch.object(ytdl_cache, "_exec_real") as real:
            ytdl_cache.main(["-J", "--", f"https://music.youtube.com/watch?v={vid(1)}"])
        real.assert_called_once()
        # skutečná chyba videa jde rovnou (bez 20 s yt-dlp)
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(ytdl_cache, "_ask_resolver",
                                  return_value={"ok": False, "error": "Private video"}), \
                mock.patch.object(ytdl_cache, "_exec_real") as real, \
                redirect_stderr(io.StringIO()):
            rc = ytdl_cache.main(["-J", "--", f"https://music.youtube.com/watch?v={vid(1)}"])
        real.assert_not_called()
        self.assertEqual(rc, 1)


class PlayerResumeTest(unittest.TestCase):
    """MpvPlayer: pozice do tmpfs, navázání po restartu, zahození mrtvé adresy."""

    def test_snapshot_and_resume_at_position(self) -> None:
        pb = Path(tempfile.mkdtemp(dir=_TMP)) / "playback.json"

        async def before():
            async with Harness() as h:
                h.player.playback_file = pb
                await h.player.enqueue([T(1), T(2)])
                await h.settle(0.1)
                h.player._time_pos = 83.4
                h.player._save_playback()  # jako stop() při SIGTERM
        run(before())
        saved = json.loads(pb.read_text())
        self.assertEqual((saved["track"]["id"], saved["pos"]), (vid(1), 83.4))

        async def after():
            async with Harness() as h:
                h.player.playback_file = pb
                got = h.player.saved_playback(900)
                self.assertIsNotNone(got)
                track, pos = got
                self.assertEqual(track.id, vid(1))
                self.assertAlmostEqual(pos, 83.4 - 2.0)
                h.player._on_resolver_event("_state", {"ready": [vid(1)], "busy": None,
                                                       "urgent": []})
                self.assertTrue(await h.player.resume_track(track, pos))
                await h.settle(0.3)
                self.assertEqual(h.fake.current_vid(), vid(1))
                opts = list(h.fake.loadfile_options.values())
                self.assertEqual(opts, [{"start": "81.4"}])
                req = [f for k, f in h.events if k == "track.request"][-1]
                self.assertEqual((req["why"], req["resume_pos_s"]), ("restart", 81.4))
                ev = [f for k, f in h.events if k == "player.resume_track"][-1]
                self.assertTrue(ev["ready"])
                # okno pro resolver se poslalo hned (i s hrající v keep)
                self.assertTrue(any(r.get("op") == "ahead" and r.get("keep") == [vid(1)]
                                    for r in h.resolver))
                # podruhé nic nepřebije
                self.assertFalse(await h.player.resume_track(T(3), 0))
        run(after())

    def test_saved_playback_rejects(self) -> None:
        pb = Path(tempfile.mkdtemp(dir=_TMP)) / "playback.json"

        async def go():
            async with Harness() as h:
                p = h.player
                self.assertIsNone(p.saved_playback(900))  # bez souboru (testy, bez XDG)
                p.playback_file = pb
                self.assertIsNone(p.saved_playback(900))  # soubor není
                base = {"v": 1, "saved": time.time(), "pos": 50.0, "paused": False,
                        "track": {"id": vid(1), "title": "t", "artist": "a", "duration": 200}}
                pb.write_text(json.dumps(base))
                self.assertIsNotNone(p.saved_playback(900))
                pb.write_text(json.dumps({**base, "saved": time.time() - 1000}))
                self.assertIsNone(p.saved_playback(900))  # starý
                pb.write_text(json.dumps({**base, "paused": True}))
                self.assertIsNone(p.saved_playback(900))  # byla pauza
                pb.write_text(json.dumps({**base, "pos": 195.0}))
                self.assertIsNone(p.saved_playback(900))  # těsně před koncem
                pb.write_text(json.dumps({**base, "track": {"id": "../../etc"}}))
                self.assertIsNone(p.saved_playback(900))
                pb.write_text("{nejson")
                self.assertIsNone(p.saved_playback(900))
        run(go())

    def test_failed_open_drops_cached_url(self) -> None:
        async def go():
            async with Harness() as h:
                h.fake.fail.add(vid(1))
                await h.player.enqueue([T(1), T(2)])
                await h.settle(0.2)
                self.assertIn({"op": "drop", "ids": [vid(1)]}, h.resolver)
                self.assertNotIn({"op": "drop", "ids": [vid(2)]}, h.resolver)
                # vypršelá adresa není vlastnost skladby: žádná černá listina
                self.assertNotIn(("error", vid(1)), [(k, f.get("video_id")) for k, f in h.events
                                                     if k == "track.end" and f.get("reason") == "error"])
        run(go())

    def test_startup_get_is_not_a_video_error(self) -> None:
        async def go():
            async with Harness() as h:
                h.player._on_resolver_event("resolver.get", {
                    "video_id": vid(1), "hit": False, "how": "startup", "ok": False,
                    "error": "resolver startuje"})
                self.assertNotIn(vid(1), h.player._res_errors)
        run(go())


if __name__ == "__main__":
    unittest.main()
