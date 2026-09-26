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
        # starší než TRUST_AGE: podají se až po kontrole adresy
        old = now - r.TRUST_AGE - 60
        a.ready = {vid(1): (old - 30, info(vid(1))), vid(2): (old, info(vid(2)))}
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
        self.assertEqual((get["how"], get["hit"], get["verified"]), ("disk", True, True))
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
        a.ready = {vid(1): (time.time() - r.TRUST_AGE - 60, stale)}
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
        a.ready = {vid(1): (time.time() - r.TRUST_AGE - 60, info(vid(1)))}
        a.flush()
        b = self.fresh()
        b.load_disk()
        with mock.patch.object(r, "probe", return_value=("unknown", None, "abc")) as probe:
            data, err = b.get(argv(vid(1)))
        probe.assert_called_once()
        self.assertEqual(data, a.ready[vid(1)][1])

    def test_young_disk_entry_served_without_probe(self) -> None:
        """Po restartu: mladé z disku hned, bez HTTPS kontroly (ta se přetahovala
        o GIL s importem yt-dlp — 2,5 s); starší se kontrolují dál."""
        r = self.r
        now = time.time()
        a = self.fresh()
        a.template = list(TEMPLATE)
        a.ready = {vid(1): (now - 120, info(vid(1))),
                   vid(2): (now - r.TRUST_AGE - 60, info(vid(2)))}
        a.flush()
        b = self.fresh()
        b.load_disk()
        self.assertEqual(b.trusted, {vid(1)})
        self.assertEqual(b.unverified, {vid(2)})
        self.assertEqual(self.events("resolver.disk")[-1]["trusted"], 1)
        with mock.patch.object(r, "probe", return_value=("dead", 403, "abc")) as probe:
            data, err = b.get(argv(vid(1)))
            probe.assert_not_called()
            self.assertEqual(data, a.ready[vid(1)][1])
            get = self.events("resolver.get")[-1]
            self.assertEqual((get["how"], get["verified"]), ("disk", False))
            self.assertGreaterEqual(get["age_s"], 119)
            # starší: kontrola, mrtvá → vyřeší se znovu ("resolver startuje", yt-dlp není)
            data, err = b.get(argv(vid(2)), timeout=0.3)
            probe.assert_called_once()
        self.assertIsNone(data)
        # mpv adresu neotevřelo → drop → příště čerstvě, ne z disku
        b.drop([vid(1)])
        self.assertNotIn(vid(1), b.trusted)
        data, err = b.get(argv(vid(1)), timeout=0.3)
        self.assertEqual(err, r.STARTUP_ERROR)
        self.assertNotEqual(self.events("resolver.get")[-1]["how"], "disk")

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

    @staticmethod
    def disk_get(h, v: str) -> None:
        """Resolver podal skladbu z cache na disku (adresa z doby před restartem)."""
        h.player._on_resolver_event("resolver.get", {
            "video_id": v, "hit": True, "how": "disk", "ok": True, "verified": False,
            "wait_ms": 2})

    def test_dead_disk_url_replays_same_entry(self) -> None:
        """Adresa z disku nejde otevřít: zahodit, načíst tutéž položku znovu.
        Žádná chyba (černá listina), žádné přeskočení, čekání od restartu."""
        async def go():
            async with Harness() as h:
                p = h.player
                seen: list[tuple[str, str | None]] = []

                async def handler(ev):
                    seen.append((ev.kind, ev.track.id if ev.track else None))
                p.on_event(handler)
                h.fake.fail_once.add(vid(1))
                self.disk_get(h, vid(1))
                self.assertTrue(await p.resume_track(T(1), 80.0))
                await h.settle(0.3)
                self.assertIn({"op": "drop", "ids": [vid(1)]}, h.resolver)
                self.assertEqual(h.fake.current_vid(), vid(1))
                self.assertEqual(h.fake.started, [vid(1)])
                # znovu načítaná položka nese původní požadavek (why=restart)
                self.assertEqual(p._load["req"]["why"], "restart")
                kinds = [k for k, v in seen if v == vid(1)]
                self.assertNotIn("error", kinds)
                self.assertNotIn("unavailable", kinds)
                self.assertNotIn("skipped", kinds)
                retry = [f for k, f in h.events if k == "player.retry"]
                self.assertEqual([(f["video_id"], f["ok"]) for f in retry], [(vid(1), True)])
                ends = [f["reason"] for k, f in h.events
                        if k == "track.end" and f["video_id"] == vid(1)]
                self.assertEqual(ends, ["retry"])
                self.assertEqual(p._err_streak, [])  # nepočítá se do výpadku
        run(go())

    def test_disk_retry_after_mpv_moved_on(self) -> None:
        """mpv po chybě přešlo na další: ta se odsune jako "replaced" (ne
        přeskočení posluchačem) a hraje se znovu ta, která selhala."""
        async def go():
            async with Harness() as h:
                p = h.player
                seen: list[tuple[str, str | None]] = []

                async def handler(ev):
                    seen.append((ev.kind, ev.track.id if ev.track else None))
                p.on_event(handler)
                h.fake.fail_once.add(vid(1))
                self.disk_get(h, vid(1))
                await p.enqueue([T(1), T(2), T(3)])
                await h.settle(0.3)
                self.assertEqual(h.fake.current_vid(), vid(1))
                self.assertEqual(h.fake.upcoming(), [vid(2), vid(3)])
                self.assertIn(("replaced", vid(2)), seen)
                self.assertNotIn(("skipped", vid(2)), seen)
                self.assertFalse([k for k, v in seen if v == vid(1)
                                  and k in ("error", "unavailable", "skipped")])
        run(go())

    def test_disk_retry_only_once(self) -> None:
        """Selže i čerstvá adresa: podruhé už obyčejná chyba (bez smyčky)."""
        async def go():
            async with Harness() as h:
                p = h.player
                seen: list[tuple[str, str | None]] = []

                async def handler(ev):
                    seen.append((ev.kind, ev.track.id if ev.track else None))
                p.on_event(handler)
                h.fake.fail.add(vid(1))
                self.disk_get(h, vid(1))
                await p.enqueue([T(1), T(2)])
                await h.settle(0.4)
                retry = [f for k, f in h.events if k == "player.retry"]
                self.assertEqual(len(retry), 1)
                # "loading failed" není vlastnost videa → unavailable, ne černá listina
                self.assertIn(("unavailable", vid(1)), seen)
                self.assertNotIn(("error", vid(1)), seen)
                self.assertEqual(h.fake.current_vid(), vid(2))
        run(go())

    def test_non_disk_failure_is_not_retried(self) -> None:
        async def go():
            async with Harness() as h:
                h.fake.fail.add(vid(1))
                h.player._on_resolver_event("resolver.get", {
                    "video_id": vid(1), "hit": True, "how": "hit", "ok": True})
                await h.player.enqueue([T(1), T(2)])
                await h.settle(0.2)
                self.assertFalse([f for k, f in h.events if k == "player.retry"])
                self.assertEqual(h.fake.current_vid(), vid(2))
        run(go())

    def test_startup_get_is_not_a_video_error(self) -> None:
        async def go():
            async with Harness() as h:
                h.player._on_resolver_event("resolver.get", {
                    "video_id": vid(1), "hit": False, "how": "startup", "ok": False,
                    "error": "resolver startuje"})
                self.assertNotIn(vid(1), h.player._res_errors)
        run(go())


class TemplateOrderTest(unittest.TestCase):
    """F-RESTART-09: tytéž volby od mpv v jiném pořadí nejsou jiná šablona."""

    def test_reordered_options_keep_prepared_tracks(self) -> None:
        r = load_resolver()
        a = ["--no-warnings", "-J", "--format", "774/251", "--cookies", "/c.txt",
             "--js-runtimes", "node", "--"]
        b = ["--js-runtimes", "node", "--no-warnings", "--cookies", "/c.txt", "-J",
             "--format", "774/251", "--"]
        self.assertEqual(r.template_key(a), r.template_key(b))
        self.assertNotEqual(r.template_key(a),
                            r.template_key(["--no-warnings", "-J", "--format", "251",
                                            "--cookies", "/c.txt", "--js-runtimes", "node", "--"]))
        res = r.Resolver()
        res.template = a
        res.ready[vid(1)] = (time.time(), info(vid(1)))
        with redirect_stderr(io.StringIO()) as err:
            res.set_template(b + ["url"])  # mpv po restartu: jiné pořadí
            self.assertIs(res.template, a)
            self.assertIn(vid(1), res.ready)
            res.set_template(["--format", "251", "-J", "--", "url"])  # opravdu jiný formát
        self.assertNotIn(vid(1), res.ready)
        self.assertEqual(err.getvalue().count("šablona od mpv převzata"), 1)


class FastPreparationTest(unittest.TestCase):
    """F-ZVUK-20: rychlý klient se zálohou, pluginy jednou, trvalý node."""

    def test_fast_client_falls_back_to_default_choice(self) -> None:
        r = load_resolver()
        res = r.Resolver()
        res.template = list(TEMPLATE)

        class Failing(FakeYdl):
            _ytdj_client = "web_music"

            def extract_info(self, url, download=False):
                raise RuntimeError("ERROR: [youtube] x: This video is not available")

        plain = FakeYdl()
        res._plain["main"] = (res.template, plain)
        with redirect_stderr(io.StringIO()):
            data, fields = res._extract(Failing(), vid(1), res.template, False)
        self.assertIn("fresh=1", data)
        self.assertEqual(plain.calls, [vid(1)])
        self.assertIn("not available", fields["fallback"])
        # bez rychlého klienta (šablona ho určuje sama) se chyba hlásí rovnou
        own = Failing()
        own._ytdj_client = None  # type: ignore[assignment]
        with self.assertRaises(RuntimeError):
            res._extract(own, vid(2), res.template, False)
        self.assertEqual(plain.calls, [vid(1)])

    def test_non_premium_result_is_retried_with_default_choice(self) -> None:
        r = load_resolver()
        res = r.Resolver()
        tmpl = ["--format", "774/141/251/140/bestaudio", "--cookies", "/c.txt", "-J", "--"]

        class Fmt(FakeYdl):
            def __init__(self, fmt, client=None):
                super().__init__()
                self.fmt, self._ytdj_client = fmt, client

            def extract_info(self, url, download=False):
                d = super().extract_info(url)
                d["format_id"] = self.fmt
                return d

        plain = Fmt("774")
        res._plain["main"] = (tmpl, plain)
        err = io.StringIO()
        with redirect_stderr(err):
            data, fields = res._extract(Fmt("251", "web_music"), vid(1), tmpl, False)
            self.assertEqual(json.loads(data)["format_id"], "774")
            self.assertEqual((fields["format"], fields["quality_retry"]), ("774", "251"))
            self.assertIn('"resolver.quality_retry"', err.getvalue())
            # Premium z rychlého klienta → žádné druhé řešení
            data, fields = res._extract(Fmt("774", "web_music"), vid(2), tmpl, False)
            self.assertNotIn("quality_retry", fields)
            # bez cookies se Premium nečeká (běžná kvalita je v pořádku)
            anon = ["--format", "774/251", "-J", "--"]
            data, fields = res._extract(Fmt("251", "web_music"), vid(3), anon, False)
            self.assertNotIn("quality_retry", fields)
        self.assertEqual(plain.calls, [vid(1)])

    def test_build_sets_fast_client_only_when_template_does_not(self) -> None:
        r = load_resolver()
        seen = []

        class Parsed:
            def __init__(self, ea):
                self.ydl_opts = {"format": "774", "extractor_args": ea}

        stub = types.SimpleNamespace(
            parse_options=lambda argv: Parsed({"youtube": {"player_client": ["tv"]}}
                                              if "--extractor-args" in argv else {}),
            YoutubeDL=lambda opts: seen.append(opts) or types.SimpleNamespace())
        res = r.Resolver()
        with mock.patch.object(r, "yt_dlp", stub), redirect_stderr(io.StringIO()):
            fast = res._build(list(TEMPLATE))
            own = res._build(["--extractor-args", "youtube:player_client=tv", "-J", "--"])
            plain = res._build(list(TEMPLATE), fast=False)
        self.assertEqual(seen[0]["extractor_args"]["youtube"]["player_client"], ["web_music"])
        self.assertEqual(fast._ytdj_client, "web_music")
        self.assertEqual(seen[1]["extractor_args"]["youtube"]["player_client"], ["tv"])
        self.assertIsNone(own._ytdj_client)
        self.assertFalse(seen[2].get("extractor_args"))
        self.assertIsNone(plain._ytdj_client)

    def test_youtubedl_is_never_built_twice_at_once(self) -> None:
        """Dvě vlákna naráz načetla pluginy dvakrát (PoTokenProvider … already
        registered, Pi 26. 9.) — pluginy jednou při importu, stavba pod zámkem."""
        r = load_resolver()
        inside, overlap = [0], []

        def ydl(opts):
            inside[0] += 1
            overlap.append(inside[0])
            time.sleep(0.05)
            inside[0] -= 1
            return types.SimpleNamespace()

        stub = types.SimpleNamespace(parse_options=lambda a: types.SimpleNamespace(ydl_opts={}),
                                     YoutubeDL=ydl)
        res = r.Resolver()
        with mock.patch.object(r, "yt_dlp", stub), redirect_stderr(io.StringIO()):
            ts = [threading.Thread(target=res._build, args=(list(TEMPLATE),)) for _ in range(3)]
            [t.start() for t in ts]
            [t.join() for t in ts]
        self.assertEqual(max(overlap), 1)
        # import: pluginy se načtou jednou, hned (ne až v YoutubeDL() dvou vláken)
        calls = []
        pkg = types.ModuleType("yt_dlp")
        plugins = types.ModuleType("yt_dlp.plugins")
        plugins.load_all_plugins = lambda: calls.append(1)  # type: ignore[attr-defined]
        pkg.plugins = plugins  # type: ignore[attr-defined]
        with mock.patch.dict(sys.modules, {"yt_dlp": pkg, "yt_dlp.plugins": plugins}), \
                redirect_stderr(io.StringIO()):
            r._import_ytdlp()
        self.assertEqual(calls, [1])

    def test_persistent_node_solves_and_keeps_compiled_player(self) -> None:
        import shutil

        node = shutil.which("node")
        if not node:
            self.skipTest("node není")
        from ytdj.player import ytdl_jsc

        lib_core = ("var lib = {};\nObject.assign(globalThis, lib);\n"
                    "var jsc = (function () { return function main(input) {"
                    " const s = {}; Function('_result', 'code' in input ? input.code : '')(s);"
                    " return { type: 'result', responses: [], preprocessed_player: "
                    "'_result.n = (c) => c + \"!\"; _result.sig = (c) => c.split(\"\").reverse().join(\"\");' };"
                    " }; })();")
        srv = ytdl_jsc.NodeServer(idle_s=60)
        try:
            player = '_result.n = (c) => c + "!"; _result.sig = (c) => c.split("").reverse().join("");'
            reqs = [{"type": "n", "challenges": ["ab"]}, {"type": "sig", "challenges": ["xyz"]}]
            out = json.loads(srv.solve(node, lib_core, player, True, reqs))
            self.assertEqual(out["responses"][0], {"type": "result", "data": {"ab": "ab!"}})
            self.assertEqual(out["responses"][1]["data"], {"xyz": "zyx"})
            pid = srv.proc.pid
            # podruhé bez kódu přehrávače (node ho drží), týž proces
            with mock.patch.object(srv, "_ask", wraps=srv._ask) as ask:
                out = json.loads(srv.solve(node, lib_core, player, True,
                                           [{"type": "n", "challenges": ["q"]}]))
            self.assertEqual(out["responses"][0]["data"], {"q": "q!"})
            self.assertNotIn("preprocessed_player", ask.call_args.args[0]["data"])
            self.assertEqual(srv.proc.pid, pid)
            # nová verze přehrávače: celé jádro (vrátí předzpracovaný pro cache)
            out = json.loads(srv.solve(node, lib_core, "raw player", False, reqs))
            self.assertIn("preprocessed_player", out)
            # node spadl → výjimka (poskytovatel pak řeší postaru), příště nový
            srv.proc.kill()
            srv.proc.wait()
            with self.assertRaises(Exception):
                srv._ask({"op": "solve", "key": "k", "data": {"type": "preprocessed",
                                                            "requests": []}})
            srv.stop_locked()
            out = json.loads(srv.solve(node, lib_core, player, True, reqs))
            self.assertEqual(out["responses"][0]["data"], {"ab": "ab!"})
            self.assertEqual(srv.starts, 2)
            # zaseknutý node: po timeoutu výjimka (zámek se uvolní), pak zase jede
            self.assertLessEqual(ytdl_jsc.TIMEOUT_S, 10)
            srv.timeout_s = 0.5
            t0 = time.monotonic()
            with self.assertRaises(TimeoutError):
                srv.solve(node, lib_core, "_result.n = () => { for (;;) {} };", True,
                          [{"type": "n", "challenges": ["a"]}])
            self.assertLess(time.monotonic() - t0, 3)
            srv.timeout_s = 10
            out = json.loads(srv.solve(node, lib_core, player, True, reqs))
            self.assertEqual(out["responses"][0]["data"], {"ab": "ab!"})
        finally:
            with srv.lock:
                srv.stop_locked()


if __name__ == "__main__":
    unittest.main()
