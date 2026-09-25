"""Provozní log: zápis, rotace, odolnost, události přehrávače a souhrn — bez sítě.

    python -m unittest tests.test_telemetry -v
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="ytdj-telemetry-test-")
# setdefault: v jednom běhu `unittest discover` si cíl nastavují i jiné moduly
# a přepsání při importu by jim odklonilo události; zápisy tady si každý test
# přesměruje sám (_LogCase).
os.environ.setdefault("XDG_DATA_HOME", _TMP)  # před importem ytdj.config
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)
os.environ.setdefault("YTDJ_EVENTS_FILE", str(Path(_TMP) / "events.jsonl"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ytdj import telemetry  # noqa: E402
from ytdj import telemetry_report as report  # noqa: E402
from ytdj.telemetry_sampler import parse_pwtop_frame, throttle_flags  # noqa: E402


class _LogCase(unittest.TestCase):
    """Každý test do vlastního souboru a s čerstvým zapisovatelem."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(dir=_TMP))
        self.path = self.dir / "events.jsonl"
        self._env = mock.patch.dict(os.environ, {"YTDJ_EVENTS_FILE": str(self.path)})
        self._env.start()
        self._w = mock.patch.object(telemetry, "_w", telemetry._Writer())
        self._w.start()
        self._en = mock.patch.object(telemetry, "ENABLED", True)
        self._en.start()

    def tearDown(self) -> None:
        telemetry.flush()
        telemetry._w._close()
        self._en.stop()
        self._w.stop()
        self._env.stop()

    def lines(self, path: Path | None = None) -> list[dict]:
        telemetry.flush()
        p = path or self.path
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


class TelemetryWriteTest(_LogCase):
    def test_session_start_first_and_sid(self) -> None:
        telemetry.event("track.start", video_id="abc", took_ms=5)
        recs = self.lines()
        self.assertEqual([r["kind"] for r in recs], ["session.start", "track.start"])
        self.assertEqual(recs[0]["version"], __import__("ytdj").__version__)
        self.assertTrue(all(r["sid"] == telemetry.SESSION_ID for r in recs))
        self.assertTrue(recs[1]["ts"].startswith("20"))
        self.assertEqual(recs[1]["video_id"], "abc")

    def test_timer_records_took_and_error(self) -> None:
        with telemetry.timer("catalog.search", query="x") as t:
            t["results"] = 3
        with self.assertRaises(ValueError):
            with telemetry.timer("catalog.search", query="y"):
                raise ValueError("boom")
        recs = [r for r in self.lines() if r["kind"] == "catalog.search"]
        self.assertEqual(recs[0]["results"], 3)
        self.assertIn("took_ms", recs[0])
        self.assertIn("boom", recs[1]["error"])

    def test_kill_switch(self) -> None:
        with mock.patch.object(telemetry, "ENABLED", False):
            telemetry.event("track.start", video_id="x")
        self.assertEqual(self.lines(), [])

    def test_never_raises(self) -> None:
        class Evil:
            def __str__(self) -> str:
                raise RuntimeError("no str")

        telemetry.event("x.weird", obj=Evil(), s={1, 2}, b=b"\xff")  # nesmí vyhodit
        telemetry.event("x.ok", n=1)
        kinds = [r["kind"] for r in self.lines()]
        self.assertIn("x.ok", kinds)
        # nezapisovatelné místo: nic nevyhodí, jen se nic nezapíše
        with mock.patch.dict(os.environ, {"YTDJ_EVENTS_FILE": "/proc/nelze/events.jsonl"}):
            telemetry.event("x.lost")
            telemetry.flush()
        with mock.patch.object(telemetry, "_line", side_effect=MemoryError):
            telemetry.event("x.oom")

    def test_threads_do_not_interleave_lines(self) -> None:
        import threading

        def burst(i: int) -> None:
            for j in range(200):
                telemetry.event("x.burst", i=i, j=j, pad="x" * 100)

        ts = [threading.Thread(target=burst, args=(i,)) for i in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        recs = [r for r in self.lines() if r["kind"] == "x.burst"]
        self.assertEqual(len(recs), 800)

    def test_rotation_bounds_disk_use(self) -> None:
        with mock.patch.object(telemetry, "MAX_BYTES", 4000), \
             mock.patch.object(telemetry, "KEEP", 2):
            for i in range(300):
                telemetry.event("x.fill", i=i, pad="y" * 60)
                if i % 10 == 0:
                    telemetry.flush()
            telemetry.flush()
        files = telemetry.rotated_files(self.path)
        # aktuální soubor může po rotaci chvíli chybět (vznikne s dalším zápisem)
        self.assertEqual([f.name for f in files][:2], ["events.jsonl.2", "events.jsonl.1"])
        self.assertLessEqual(len(files), 3)
        self.assertFalse((self.dir / "events.jsonl.3").exists())
        for f in files:
            self.assertLess(f.stat().st_size, 4000 + 2000)
        # čte se od nejstaršího: pořadí událostí zůstává rostoucí
        seq = [e["i"] for e in report.load_events(files) if e["kind"] == "x.fill"]
        self.assertEqual(seq, sorted(seq))
        self.assertEqual(seq[-1], 299)

    def test_reopens_after_external_delete(self) -> None:
        telemetry.event("x.a")
        telemetry.flush()
        self.path.unlink()
        telemetry.event("x.b")
        self.assertEqual([r["kind"] for r in self.lines()], ["x.b"])

    def test_shim_fallback_line_is_valid(self) -> None:
        from ytdj.player import ytdl_cache

        with mock.patch.dict(os.environ, {"YTDJ_SID": "zzz"}):
            ytdl_cache._note_fallback(["-J", "https://music.youtube.com/watch?v=dQw4w9WgXcQ"],
                                      "resolver neodpovídá")
        rec = self.lines()[-1]
        self.assertEqual(rec["kind"], "resolver.fallback")
        self.assertEqual(rec["video_id"], "dQw4w9WgXcQ")
        self.assertEqual(rec["sid"], "zzz")
        self.assertRegex(rec["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d$")


class ResolverEventTest(unittest.TestCase):
    def test_parse_lines(self) -> None:
        from ytdj.player.mpv import parse_resolver_line

        self.assertEqual(
            parse_resolver_line('EVENT {"kind": "resolver.get", "video_id": "a", "hit": true}'),
            ("resolver.get", {"video_id": "a", "hit": True}),
        )
        self.assertIsNone(parse_resolver_line("vyřešeno abc za 8.1 s"))
        self.assertIsNone(parse_resolver_line("EVENT {nejson"))
        self.assertIsNone(parse_resolver_line('EVENT ["kind"]'))
        self.assertIsNone(parse_resolver_line('EVENT {"video_id": "a"}'))

    def test_resolver_emits_parsable_events(self) -> None:
        """Resolver běží pod cizím interpretem — jeho řádky musí projít parserem."""
        from ytdj.player.mpv import parse_resolver_line

        stub = types.ModuleType("yt_dlp")
        stub.YoutubeDL = object  # type: ignore[attr-defined]
        with mock.patch.dict(sys.modules, {"yt_dlp": stub}):
            sys.modules.pop("ytdj.player.ytdl_resolver", None)
            from ytdj.player import ytdl_resolver as r

            res = r.Resolver()
            argv = ["-J", "--", "https://music.youtube.com/watch?v=dQw4w9WgXcQ"]
            res.template = argv[:-1]
            res.ydl = object()  # type: ignore[assignment]
            res.ready["dQw4w9WgXcQ"] = (r.time.time(), '{"id": "x"}')
            err = io.StringIO()
            with redirect_stderr(err):
                data, error = res.get(argv)
                res.set_ahead(["aaaaaaaaaaa"])
        self.assertEqual(data, '{"id": "x"}')
        parsed = [parse_resolver_line(x) for x in err.getvalue().splitlines()]
        events = [p for p in parsed if p]
        kinds = [k for k, _ in events]
        self.assertIn("resolver.get", kinds)
        self.assertIn("_state", kinds)
        self.assertIn("resolver.ahead", kinds)
        get = dict(events)["resolver.get"]
        self.assertTrue(get["hit"])
        self.assertEqual(get["video_id"], "dQw4w9WgXcQ")
        # lidské řádky zůstaly
        self.assertIn("z cache dQw4w9WgXcQ", err.getvalue())


class PlayerLifecycleTest(_LogCase):
    """Události mpv → track.request / track.start / track.end s časy."""

    def make_player(self):
        from ytdj.config import DEFAULTS, Config
        from ytdj.music.catalog import Track
        from ytdj.player.mpv import MpvPlayer

        p = MpvPlayer(Config(**DEFAULTS))
        p._resolver = object()  # type: ignore[assignment]  # "běží"
        for i, vid in enumerate(("aaaaaaaaaaa", "bbbbbbbbbbb")):
            p._tracks[vid] = Track(vid, f"T{i}", f"A{i}", duration=200)
            p._order.append(vid)
            p._entries[i + 1] = vid
        return p

    def test_skip_to_prefetched_and_on_demand(self) -> None:
        async def run():
            p = self.make_player()
            # první skladba hraje
            p._handle_event({"event": "start-file", "playlist_entry_id": 1})
            p._current_id = "aaaaaaaaaaa"
            p._on_resolver_event("resolver.get", {"video_id": "aaaaaaaaaaa", "hit": False,
                                                  "how": "miss", "wait_ms": 7000})
            p._handle_event({"event": "file-loaded"})
            p._handle_event({"event": "property-change", "name": "core-idle", "data": False})
            p._handle_event({"event": "property-change", "name": "time-pos", "data": 42.0})
            # zaseknutí uprostřed
            p._handle_event({"event": "property-change", "name": "core-idle", "data": True})
            p._load["stall_t0"] -= 1.5
            p._handle_event({"event": "property-change", "name": "core-idle", "data": False})
            # posluchač přeskočí, další je nachystaná
            p._on_resolver_event("_state", {"ready": ["bbbbbbbbbbb"], "busy": None, "urgent": []})
            with mock.patch.object(p, "_command", mock.AsyncMock()):
                await p.skip()
            p._handle_event({"event": "end-file", "reason": "stop", "playlist_entry_id": 1})
            p._pos = 1
            p._handle_event({"event": "start-file", "playlist_entry_id": 2})
            p._on_resolver_event("resolver.get", {"video_id": "bbbbbbbbbbb", "hit": True,
                                                  "how": "hit", "wait_ms": 3})
            p._handle_event({"event": "file-loaded"})
            p._handle_event({"event": "property-change", "name": "core-idle", "data": False})
            p._handle_event({"event": "end-file", "reason": "eof", "playlist_entry_id": 2})
            for t in asyncio.all_tasks() - {asyncio.current_task()}:
                t.cancel()

        asyncio.run(run())
        recs = self.lines()
        kinds = [r["kind"] for r in recs]
        self.assertEqual(
            [k for k in kinds if k.startswith("track.")],
            ["track.start", "track.stall", "track.request", "track.end", "track.start",
             "track.end", "track.request"],
        )
        starts = [r for r in recs if r["kind"] == "track.start"]
        self.assertEqual(starts[0]["source"], "on_demand")
        self.assertEqual(starts[0]["resolve_ms"], 7000)
        self.assertEqual(starts[1]["source"], "prefetched")
        self.assertEqual(starts[1]["why"], "skip")
        self.assertNotIn("unexpected", starts[1])  # hraje to, co ukazuje fronta
        self.assertIsInstance(starts[1]["wait_ms"], int)
        req = next(r for r in recs if r["kind"] == "track.request")
        self.assertEqual(req["why"], "skip")
        self.assertEqual(req["next_id"], "bbbbbbbbbbb")
        self.assertTrue(req["next_ready"])
        end1 = next(r for r in recs if r["kind"] == "track.end")
        self.assertEqual(end1["reason"], "skipped")
        self.assertEqual(end1["stalls"], 1)
        self.assertGreaterEqual(end1["stall_ms"], 1400)
        self.assertEqual([r["why"] for r in recs if r["kind"] == "track.request"], ["skip", "eof"])


class SamplerParseTest(unittest.TestCase):
    FRAME = """\
I   30      0      0   0.0us   0.0us  ???   ???     0                  Dummy-Driver
S   31      0      0    ---     ---   ---   ---     0                  Freewheel-Driver
R   48   2048  48000  17.2us  31.9us  0.00  0.00    3    S16LE 2 48000 alsa_output.usb-Dell.analog-stereo
R   57   2048  48000  40.1us  90.2us  0.01  0.02    1    F32LE 2 48000  + mpv
""".splitlines()

    def test_pwtop_frame(self) -> None:
        got = parse_pwtop_frame(self.FRAME)
        self.assertEqual(got["48 alsa_output.usb-Dell.analog-stereo"], 3)
        self.assertEqual(got["57 mpv"], 1)
        self.assertEqual(got["30 Dummy-Driver"], 0)

    def test_xrun_delta_event(self) -> None:
        from ytdj.telemetry_sampler import SystemSampler

        sent: list = []
        s = SystemSampler(lambda: {"track": "abc", "resolving": "def"}, lambda: {})
        s.last = {"cpu": 93.0, "mem_avail_mb": 120}
        with mock.patch.object(telemetry, "event", lambda k, **f: sent.append((k, f))):
            s._compare({"48 sink": 3, "57 mpv": 0}, {"48 sink": 3, "57 mpv": 0})
            s._compare({"48 sink": 3, "57 mpv": 0}, {"48 sink": 5, "57 mpv": 1, "60 new": 0})
        self.assertEqual(len(sent), 1)
        kind, f = sent[0]
        self.assertEqual(kind, "audio.xrun")
        self.assertEqual(f["delta"], 3)
        self.assertEqual(sorted(f["nodes"]), ["mpv+1", "sink+2"])
        self.assertEqual(f["resolving"], "def")
        self.assertEqual(f["last_cpu"], 93.0)

    def test_throttle_flags(self) -> None:
        self.assertEqual(throttle_flags(0), [])
        self.assertEqual(throttle_flags(0x50005), ["podpětí", "throttling", "podpětí (od startu)",
                                                   "throttling (od startu)"])

    def test_sample_runs_here(self) -> None:
        from ytdj.telemetry_sampler import SystemSampler

        s = SystemSampler(lambda: {"codex": False}, lambda: {})
        first = s.sample(emit=False)
        rec = s.sample(emit=False)
        if Path("/proc/meminfo").exists():
            self.assertIn("mem_avail_mb", rec)
            self.assertIn("rss_ytdj_mb", first)  # RSS jednou za minutu
            self.assertNotIn("rss_ytdj_mb", rec)
            self.assertIn("cpu_ytdj", rec)
        self.assertIn("self_us", rec)
        self.assertNotIn("codex", rec)  # False se nepíše


def _fixture_lines() -> list[str]:
    ev = []

    def add(ts: str, kind: str, **f) -> None:
        ev.append(json.dumps({"ts": f"2026-09-25T{ts}.000+02:00", "kind": kind, "sid": f.pop("sid", "s1"), **f},
                             ensure_ascii=False))

    add("09:59:00", "track.start", sid="s0", video_id="old", wait_ms=99999, source="prefetched", why="eof")
    add("10:00:00", "session.start", version="0.1.0")
    add("10:00:05", "track.request", why="enqueue", next_id="v1", next_ready=False, queue=3)
    add("10:00:20", "track.start", video_id="v1", why="enqueue", wait_ms=15000, load_ms=12000,
        buffer_ms=3000, source="on_demand", resolve_ms=11000)
    add("10:01:00", "track.request", why="skip", from_id="v1", next_id="v2", next_ready=True)
    add("10:01:00", "track.end", video_id="v1", reason="skipped", played_s=40, started=True)
    add("10:01:02", "track.start", video_id="v2", why="skip", wait_ms=2000, load_ms=500,
        buffer_ms=1500, source="prefetched", resolve_ms=5)
    add("10:02:00", "track.request", why="skip", from_id="v2", next_id="v3", next_ready=False,
        next_resolving=True)
    add("10:02:00", "track.end", video_id="v2", reason="skipped", played_s=58, started=True)
    add("10:02:09", "track.start", video_id="v3", why="skip", wait_ms=9000, source="on_demand",
        resolve_ms=8000, resolve_how="joined", unexpected=True, expected_id="v9")
    add("10:05:00", "track.end", video_id="v3", reason="finished", played_s=180, started=True,
        stalls=1, stall_ms=800)
    add("10:05:00", "track.request", why="eof", next_id=None)
    add("10:00:30", "resolver.resolve", video_id="v2", took_ms=8000, why="ahead", ok=True)
    add("10:00:40", "resolver.resolve", video_id="vx", took_ms=3000, why="ahead", ok=False,
        error="Video unavailable")
    add("10:01:01", "resolver.get", video_id="v2", hit=True, wait_ms=5)
    add("10:02:01", "resolver.get", video_id="v3", hit=False, how="joined", wait_ms=8000)
    add("10:03:00", "audio.xrun", delta=2, nodes=["sink+2"], track="v3", pos_s=3.2,
        resolving="v4", codex=True, last_cpu=97.0, last_mem_avail_mb=80)
    for i, (mem, temp) in enumerate([(300, 55.0), (80, 61.5), (250, 58.0)]):
        add(f"10:0{i}:10", "sys.sample", cpu=50.0 + i, mem_avail_mb=mem, swap_used_mb=100 + i,
            temp_c=temp, self_us=900)
    add("10:04:00", "sys.throttle", value="0x50000", flags=["podpětí (od startu)"])
    add("10:00:50", "web.prompt", text="něco od Beatles", len=15, status=200, took_ms=21000,
        reply="Pouštím Beatles", ip="192.168.0.5", ua="Firefox/Android")
    add("10:01:30", "web.prompt", text="hlasitěji", status=409, took_ms=3, error="Codex právě pracuje")
    add("10:01:40", "web.control", action="next", status=200, took_ms=4)
    add("10:01:45", "web.sse_open", clients=2)
    add("10:02:30", "dj.request", took_ms=18000, fulfilled=False)
    add("10:02:40", "dj.request", took_ms=12000, fulfilled=True)
    add("10:02:50", "panel.touch", button="next")
    ev.append('{"ts": "2026-09-25T10:06:00.000+02:00", "kind": "track.st')  # useknutý řádek
    return ev


class ReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(dir=_TMP))
        (self.dir / "events.jsonl").write_text("\n".join(_fixture_lines()) + "\n")

    def summary(self, since: str | None = "2026-09-25T10:00") -> dict:
        files = report.input_files([str(self.dir)])
        return report.summarize(report.load_events(files, report.parse_when(since) if since else None))

    def test_aggregation(self) -> None:
        s = self.summary()
        self.assertEqual(s["tracks"]["started"], 3)  # "old" je před --since
        self.assertEqual(s["tracks"]["ended"], {"skipped": 2, "finished": 1})
        self.assertEqual(s["tracks"]["stalls"], 1)
        self.assertEqual(s["tracks"]["unexpected_next"], 1)
        self.assertEqual(s["latency"]["by_source"]["prefetched"]["median"], 2000)
        self.assertEqual(s["latency"]["by_source"]["on_demand"]["n"], 2)
        self.assertEqual(s["skips"], {"total": 2, "landed_unprepared": 1, "landed_total": 2,
                                      "unprepared": 1, "unprepared_but_resolving": 1,
                                      "empty_queue": 0})
        self.assertEqual(s["resolver"]["hit_rate"], 0.5)
        self.assertEqual(s["resolver"]["resolve_errors"], 1)
        self.assertEqual(s["xruns"]["total"], 2)
        self.assertEqual(s["xruns"]["context"]["during_codex"], 1)
        self.assertEqual(s["system"]["mem_avail_mb"]["min"], 80)
        self.assertEqual(s["system"]["temp_c"]["max"], 61.5)
        self.assertEqual(s["system"]["throttle_flags"], ["podpětí (od startu)"])
        self.assertEqual(s["web"]["prompts"], 2)
        self.assertEqual(s["web"]["prompt_ms"]["median"], 21000)
        self.assertEqual(s["other"]["dj.request"]["n"], 2)
        self.assertEqual(s["other"]["dj.request"]["took_ms"]["max"], 18000)
        self.assertEqual(s["other"]["panel.touch"]["took_ms"], None)
        self.assertIn({"kind": "resolver.resolve", "error": "Video unavailable", "n": 1}, s["errors"])

    def test_since_filter_and_render(self) -> None:
        self.assertEqual(self.summary(since=None)["tracks"]["started"], 4)
        text = report.render(self.summary())
        self.assertIn("nachystané dopředu", text)
        self.assertIn("zaznělo až po řešení na místě 1 z 2", text)
        self.assertIn("něco od Beatles", text)
        self.assertIn("dj.request: 2×", text)

    def test_cli_json_and_text(self) -> None:
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            rc = report.main(["report", "--file", str(self.dir), "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["tracks"]["started"], 4)
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            rc = report.main(["report", "--file", str(self.dir), "--since", "2026-09-25T10:00"])
        self.assertEqual(rc, 0)
        self.assertIn("Výpadky zvuku", out.getvalue())

    def test_parse_when(self) -> None:
        from datetime import datetime

        now = datetime(2026, 9, 25, 14, 30)
        self.assertEqual(report.parse_when("2h", now), "2026-09-25T12:30:00")
        self.assertEqual(report.parse_when("today", now), "2026-09-25T00:00:00")
        self.assertEqual(report.parse_when("2026-09-20", now), "2026-09-20T00:00:00")
        with self.assertRaises(ValueError):
            report.parse_when("zítra", now)


class WebHelpersTest(unittest.TestCase):
    def test_short_ua(self) -> None:
        from ytdj.web.server import short_ua

        self.assertEqual(short_ua("Mozilla/5.0 (Android 14; Mobile; rv:130.0) Gecko/130.0 Firefox/130.0"),
                         "Firefox/Android")
        self.assertEqual(short_ua("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                  "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"), "Chrome/Windows")
        self.assertEqual(short_ua("curl/8.5.0"), "curl")
        self.assertEqual(short_ua(""), "?")


if __name__ == "__main__":
    unittest.main()
