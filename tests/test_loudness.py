"""Srovnání hlasitosti skladeb (F-ZVUK-24, F-ZVUK-25, F-ZVUK-32).

Vlastník 6. 10. 2026: „Potřebuju, aby to srovnávalo hlasitost písniček na
stejnou úroveň, protože přestože mám stejnou úroveň hlasitosti nastavenou,
tak každá ta písnička je vytvořená s jinou hlasitostí."

Výpočet zisku, hlasitost z odpovědi YouTube, resolver (zisk na skladbu v JSONu
pro mpv), přehrávač proti falešnému mpv (telemetrie, hlasitost se nemění,
nastavení za běhu, pojistka) a — když je na stroji mpv — skutečné mpv se
skriptem ytdj_gain.lua do souboru (zisk přesně na hranici skladeb).

    YTDJ_EVENTS_FILE=/tmp/ev.jsonl .venv/bin/python -m unittest tests.test_loudness -v
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import types
import unittest
import wave
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="ytdj-loudness-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)
os.environ.setdefault("YTDJ_EVENTS_FILE", str(Path(_TMP) / "events.jsonl"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_player_queue import Harness, T, run, vid  # noqa: E402
from test_resume_cache import FakeYdl, argv, info, load_resolver  # noqa: E402
from ytdj.config import DEFAULTS, Config  # noqa: E402
from ytdj.player import mpv as mpvmod  # noqa: E402
from ytdj.player import ytdl_loudness as L  # noqa: E402
from ytdj.player.mpv import GAIN_SCRIPT, MpvPlayer  # noqa: E402

# Skutečné odpovědi přehrávače YouTube (6. 10. 2026, jen část o hlasitosti):
# Karel Gott – Lady Carneval. Klient web míří na −14 LUFS, web_music na −7 —
# `loudnessDb` je rozdíl proti cíli toho klienta, absolutní hodnota je stejná.
PR_WEB = {
    "playerConfig": {"audioConfig": {
        "loudnessDb": 5.96, "perceptualLoudnessDb": -8.04, "enablePerFormatLoudness": True,
        "trackAbsoluteLoudnessLkfs": -8.04, "loudnessTargetLkfs": -14}},
    "streamingData": {"adaptiveFormats": [
        {"itag": 137, "mimeType": "video/mp4; codecs=\"avc1\"", "loudnessDb": 99},
        {"itag": 140, "mimeType": "audio/mp4; codecs=\"mp4a.40.2\"", "loudnessDb": 5.96},
        {"itag": 251, "mimeType": "audio/webm; codecs=\"opus\"", "loudnessDb": 5.95},
        {"itag": 251, "mimeType": "audio/webm; codecs=\"opus\"", "loudnessDb": 1.5, "isDrc": True},
    ]},
}
PR_MUSIC = {
    "playerConfig": {"audioConfig": {
        "loudnessDb": -1.04, "perceptualLoudnessDb": -8.04, "enablePerFormatLoudness": True,
        "trackAbsoluteLoudnessLkfs": -8.04, "loudnessTargetLkfs": -7}},
    "streamingData": {"adaptiveFormats": [
        {"itag": 774, "mimeType": "audio/webm; codecs=\"opus\"", "loudnessDb": -1.05},
        {"itag": 251, "mimeType": "audio/webm; codecs=\"opus\"", "loudnessDb": -1.04},
    ]},
}


def with_loud(v: str, lufs: float | None) -> str:
    """JSON skladby, jak ho resolver drží v cache (s hlasitostí od YouTube)."""
    data = info(v)
    return L.tag(data, L.KEY_LOUD, lufs) if lufs is not None else data


class GainMath(unittest.TestCase):
    """F-ZVUK-24: jeden zisk na skladbu = cíl − hlasitost, s mezemi."""

    def test_gain_brings_every_track_to_the_target(self) -> None:
        # změřené skladby (ffmpeg ebur128 = hodnota YouTube ±0,06 dB)
        for lufs, want in ((-3.51, -10.5), (-8.04, -6.0), (-13.75, -0.2), (-14.0, 0.0),
                           (-15.96, 2.0), (-17.93, 3.9)):
            gain = L.gain_db(lufs, -14)
            self.assertEqual(gain, want, lufs)
            self.assertAlmostEqual(lufs + gain, -14, delta=0.06)
        self.assertEqual(L.gain_db(-8.04, -18), -10.0)  # jiný cíl z nastavení
        self.assertEqual(str(L.gain_db(-14.02, -14)), "0.0")  # žádné "-0.0"

    def test_boost_is_capped_and_cut_is_bounded(self) -> None:
        self.assertEqual(L.MAX_BOOST, 6.0)
        self.assertEqual(L.gain_db(-25.0, -14), 6.0)  # tichá: nejvýš +6 dB
        self.assertEqual(L.gain_db(-20.0, -14), 6.0)
        self.assertEqual(L.gain_db(-19.0, -14), 5.0)
        self.assertEqual(L.gain_db(-30.0, -14, max_boost=0), 0.0)  # jen tlumit
        self.assertEqual(L.gain_db(-9.0, -14, max_boost=0), -5.0)
        self.assertEqual(L.gain_db(5.0, -24), -L.MAX_CUT)  # pojistka dolů
        for lufs in (-49.0, -30.0, -14.0, -3.0, 5.9):
            for target in (-24, -14, -8):
                self.assertLessEqual(L.gain_db(lufs, target), L.MAX_BOOST)
                self.assertGreaterEqual(L.gain_db(lufs, target), -L.MAX_CUT)

    def test_unknown_or_nonsense_loudness_means_no_change(self) -> None:
        for lufs in (None, "", "-8", True, float("nan"), float("inf"), -70.0, 20.0, {}, []):
            self.assertIsNone(L.gain_db(lufs, -14), repr(lufs))
        self.assertIsNone(L.gain_db(-8.0, None))  # vypnuto / bez cíle


class YoutubeLoudness(unittest.TestCase):
    """Hlasitost skladby z odpovědi přehrávače YouTube (yt-dlp ji nepodává)."""

    def test_value_of_the_chosen_format_with_the_clients_own_target(self) -> None:
        web, music = L.summarize([PR_WEB]), L.summarize([PR_MUSIC])
        self.assertEqual(L.pick(web, "251"), -8.05)  # −14 + 5,95
        self.assertEqual(L.pick(web, "140"), -8.04)
        self.assertEqual(L.pick(web, "251-drc"), -12.5)  # zvlášť, nezaměnit s 251
        # web_music míří na −7: −7 + (−1,05), ne −14 + (−1,05)
        self.assertEqual(L.pick(music, "774"), -8.05)
        self.assertEqual(L.pick(L.summarize([{}, PR_MUSIC, PR_WEB]), 774), -8.05)
        self.assertNotIn("137", web[0]["formats"])  # video se nepočítá

    def test_absolute_value_when_format_is_unknown_and_never_a_guessed_target(self) -> None:
        self.assertEqual(L.pick(L.summarize([PR_WEB]), "999"), -8.04)
        self.assertEqual(L.pick(L.summarize([PR_WEB]), None), -8.04)
        only_perceptual = {"playerConfig": {"audioConfig": {"perceptualLoudnessDb": -11.3}}}
        self.assertEqual(L.pick(L.summarize([only_perceptual]), "251"), -11.3)
        # jen rozdíl proti neznámému cíli: nehádat (−14? −7?) → nic
        no_target = {"playerConfig": {"audioConfig": {"loudnessDb": 3.0}},
                     "streamingData": {"adaptiveFormats": [
                         {"itag": 251, "mimeType": "audio/webm", "loudnessDb": 3.0}]}}
        self.assertIsNone(L.pick(L.summarize([no_target]), "251"))
        self.assertIsNone(L.pick(L.summarize([{"videoDetails": {}}, None, "x"]), "251"))
        self.assertIsNone(L.pick([], "251"))
        silly = {"playerConfig": {"audioConfig": {"trackAbsoluteLoudnessLkfs": -90.0}}}
        self.assertIsNone(L.pick(L.summarize([silly]), None))

    def test_tag_and_read_roundtrip(self) -> None:
        data = json.dumps({"id": "x", "formats": [{"url": "u" * 5000}], "note": {"a": 1}})
        tagged = L.tag(data, L.KEY_LOUD, -8.04)
        self.assertEqual(json.loads(tagged)[L.KEY_LOUD], -8.04)
        self.assertEqual(L.read(tagged, L.KEY_LOUD), -8.04)
        self.assertIsNone(L.read(tagged, L.KEY_GAIN))
        both = L.tag(tagged, L.KEY_GAIN, -6.0)
        self.assertEqual((L.read(both, L.KEY_LOUD), L.read(both, L.KEY_GAIN)), (-8.04, -6.0))
        self.assertEqual(json.loads(both)[L.KEY_GAIN], -6.0)
        self.assertTrue(both.endswith('"ytdj_loudness_lufs": -8.04, "ytdj_gain_db": -6.00}'))
        self.assertEqual(json.loads(L.tag("{}", L.KEY_GAIN, 3.9)), {L.KEY_GAIN: 3.9})
        self.assertEqual(L.tag("[1]", L.KEY_GAIN, 1.0), "[1]")  # není objekt → nesahat
        self.assertEqual(L.tag(data, L.KEY_GAIN, float("nan")), data)
        self.assertIsNone(L.read(None, L.KEY_LOUD))
        # stejně pojmenovaný text hluboko v JSONu (popis videa) se nečte
        fake = json.dumps({"description": '"ytdj_gain_db": 12.00, ', "pad": "x" * 500})
        self.assertIsNone(L.read(fake, L.KEY_GAIN))

    def test_install_hands_over_the_summary_and_survives_errors(self) -> None:
        class YoutubeIE:
            def _extract_player_responses(self, clients, video_id, *a):
                return [PR_MUSIC], "player-url"

        pkg = {n: types.ModuleType(n) for n in ("yt_dlp", "yt_dlp.extractor",
                                                "yt_dlp.extractor.youtube")}
        pkg["yt_dlp.extractor.youtube"].YoutubeIE = YoutubeIE  # type: ignore[attr-defined]
        got: list = []
        with mock.patch.dict(sys.modules, pkg):
            self.assertTrue(L.install(lambda v, s: got.append((v, s))))
            self.assertEqual(YoutubeIE()._extract_player_responses((), "abc", 1, 2),
                             ([PR_MUSIC], "player-url"))  # výsledek yt-dlp beze změny
            self.assertEqual(got[0][0], "abc")
            self.assertEqual(L.pick(got[0][1], "774"), -8.05)
            # podruhé se neobaluje znovu, jen se vymění příjemce
            again: list = []
            self.assertTrue(L.install(lambda v, s: again.append(v)))
            YoutubeIE()._extract_player_responses((), "def")
            self.assertEqual((len(got), again), (1, ["def"]))

            def boom(v, s):
                raise RuntimeError("x")

            L.install(boom)  # chyba příjemce nesmí shodit řešení skladby
            self.assertEqual(YoutubeIE()._extract_player_responses((), "ghi")[1], "player-url")


class ResolverGain(unittest.TestCase):
    """F-ZVUK-24/25: resolver nese hlasitost od YouTube a ke každé podané
    skladbě připíše její vlastní zisk."""

    def setUp(self) -> None:
        self.r = load_resolver()
        self.res = self.r.Resolver()
        self.res.loaded = True
        self.res.template = argv("x")[:-1]
        self.events: list[dict] = []
        self._emit = mock.patch.object(
            self.r, "emit", lambda kind, **f: self.events.append(dict(f, kind=kind)))
        self._emit.start()
        self.addCleanup(self._emit.stop)

    def serve(self, v: str) -> str:
        with redirect_stderr(io.StringIO()):
            data, err = self.res.get(argv(v), timeout=0.1)
        self.assertIsNone(err)
        return data

    def last(self, kind: str) -> dict:
        return [e for e in self.events if e["kind"] == kind][-1]

    def test_resolved_track_carries_youtube_loudness(self) -> None:
        class Ydl(FakeYdl):
            def extract_info(self, url, download=False):
                d = super().extract_info(url)
                d["format_id"] = "774"
                # tohle dělá ytdl_loudness.install uvnitř yt-dlp
                self_r._LOUD_LOCAL.last = (d["id"], L.summarize([PR_MUSIC]))
                return d

        self_r = self.r
        with redirect_stderr(io.StringIO()):
            data, fields = self.res._extract(Ydl(), vid(1), self.res.template, False)
        self.assertEqual(json.loads(data)[L.KEY_LOUD], -8.05)
        self.assertEqual(fields["loud"], -8.05)  # → resolver.resolve
        self.assertNotIn(L.KEY_GAIN, data)  # zisk až při podání (cíl se může změnit)
        # YouTube hlasitost neřekl (nebo se yt-dlp změnilo): skladba se vyřeší dál
        with redirect_stderr(io.StringIO()):
            data, fields = self.res._extract(FakeYdl(), vid(2), self.res.template, False)
        self.assertNotIn("ytdj_", data)
        self.assertNotIn("loud", fields)
        # hodnota jiné skladby (zbylá ve vlákně) se téhle nepřipíše
        self.r._LOUD_LOCAL.last = (vid(9), L.summarize([PR_MUSIC]))
        self.assertIsNone(self.r._loudness(vid(3), "774"))

    def test_each_track_gets_its_own_gain_and_none_leaks_to_the_next(self) -> None:
        self.res.gain_target = -14.0
        now = time.time()
        self.res.ready = {vid(1): (now, with_loud(vid(1), -3.51)),  # moderní, hlasitá
                          vid(2): (now, with_loud(vid(2), None)),  # YouTube neřekl
                          vid(3): (now, with_loud(vid(3), -17.93)),  # tichá
                          vid(4): (now, with_loud(vid(4), -30.0))}  # hodně tichá
        loud = json.loads(self.serve(vid(1)))
        self.assertEqual((loud[L.KEY_LOUD], loud[L.KEY_GAIN]), (-3.51, -10.5))
        self.assertEqual((self.last("resolver.get")["loud"], self.last("resolver.get")["gain"]),
                         (-3.51, -10.5))
        unknown = self.serve(vid(2))
        self.assertEqual(unknown, info(vid(2)))  # beze změny: žádný zisk předchozí skladby
        self.assertNotIn("gain", self.last("resolver.get"))
        self.assertNotIn("loud", self.last("resolver.get"))
        self.assertEqual(json.loads(self.serve(vid(3)))[L.KEY_GAIN], 3.9)
        self.assertEqual(json.loads(self.serve(vid(4)))[L.KEY_GAIN], 6.0)  # strop zesílení
        # tatáž skladba podruhé (návrat ve frontě): zase svůj zisk, jednou
        again = self.serve(vid(1))
        self.assertEqual(again.count(L.KEY_GAIN), 1)
        self.assertEqual(json.loads(again)[L.KEY_GAIN], -10.5)
        # v cache zůstává jen hlasitost
        self.assertNotIn(L.KEY_GAIN, self.res.ready[vid(1)][1])

    def test_off_serves_the_cached_json_unchanged(self) -> None:
        self.assertIsNone(self.res.gain_target)  # bez --gain-target vypnuto
        cached = with_loud(vid(1), -3.51)
        self.res.ready = {vid(1): (time.time(), cached)}
        self.assertEqual(self.serve(vid(1)), cached)
        self.assertNotIn("gain", self.last("resolver.get"))
        self.assertEqual(self.last("resolver.get")["loud"], -3.51)  # jen do logu

    def test_target_change_applies_to_tracks_served_afterwards(self) -> None:
        self.res.ready = {vid(1): (time.time(), with_loud(vid(1), -8.04))}
        self.res.set_gain(-14)
        self.assertEqual(json.loads(self.serve(vid(1)))[L.KEY_GAIN], -6.0)
        self.res.set_gain(-18)
        self.assertEqual(self.last("resolver.gain")["target"], -18.0)
        self.assertEqual(json.loads(self.serve(vid(1)))[L.KEY_GAIN], -10.0)
        for off in (None, "x", True):
            self.res.set_gain(off)
            self.assertNotIn(L.KEY_GAIN, self.serve(vid(1)))

    def test_loudness_survives_restart_in_disk_cache(self) -> None:
        path = str(Path(tempfile.mkdtemp(dir=_TMP)) / "cache")
        first = self.r.Resolver(self.r.DiskCache(path))
        first.template = argv("x")[:-1]
        first.ready = {vid(1): (time.time(), with_loud(vid(1), -3.51))}
        first.flush()
        after = self.r.Resolver(self.r.DiskCache(path))
        after.gain_target = -14.0
        with redirect_stderr(io.StringIO()):
            after.load_disk()
            data, err = after.get(argv(vid(1)), timeout=0.1)
        self.assertIsNone(err)
        self.assertEqual(json.loads(data)[L.KEY_GAIN], -10.5)  # navázaná skladba po restartu
        self.assertEqual(self.last("resolver.get")["how"], "disk")


class _Proc:
    """Atrapa procesu resolveru pro _start_resolver."""

    returncode = None
    pid = 0

    def __init__(self) -> None:
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()

    async def wait(self) -> int:
        return 0

    def terminate(self) -> None:
        pass


class PlayerGain(unittest.TestCase):
    """F-ZVUK-24/25: přehrávač — zapnutí, telemetrie, hlasitost se nemění."""

    def gain_event(self, h: Harness, *args: str) -> None:
        h.fake._send({"event": "client-message", "args": [mpvmod.GAIN_MESSAGE, *args]})

    def test_script_and_resolver_get_the_target_only_when_enabled(self) -> None:
        self.assertTrue(GAIN_SCRIPT.is_file())
        self.assertIs(DEFAULTS["loudness_normalize"], True)  # výchozí: zapnuto
        self.assertEqual(DEFAULTS["loudness_target"], -14)
        on = MpvPlayer(Config(**DEFAULTS))
        off = MpvPlayer(Config(**{**DEFAULTS, "loudness_normalize": False}))
        script = f"--script={GAIN_SCRIPT}"
        with mock.patch.object(MpvPlayer, "_ytdl_shim", lambda self: "/shim"):
            a_on, a_off = on._args(), off._args()
        self.assertIn(script, a_on)
        # vypnuto = mpv přesně jako dřív; a hlasitost je v obou případech stejná
        self.assertEqual([a for a in a_on if a != script], a_off)
        self.assertIn(f"--volume={DEFAULTS['volume']}", a_on)
        self.assertFalse(any(a.startswith(("--af", "--volume-gain", "--replaygain")) for a in a_on))
        self.assertEqual((on._gain_script, off._gain_script), (True, False))

        async def spawn(cfg: Config) -> list[str]:
            player = MpvPlayer(cfg)
            player._stopping = True
            seen: list = []

            async def fake_exec(*argv_, **kw):
                seen.extend(argv_)
                return _Proc()

            ytdlp = Path(tempfile.mkdtemp(dir=_TMP)) / "yt-dlp"
            ytdlp.write_text("#!/usr/bin/python3\n")
            cfg.yt_dlp_path = str(ytdlp)
            with mock.patch.object(mpvmod.asyncio, "create_subprocess_exec", fake_exec), \
                    mock.patch.object(mpvmod, "RESOLVER_SOCKET", Path(_TMP) / "r.sock"):
                await player._start_resolver()
                await asyncio.sleep(0.01)
            return [str(a) for a in seen]

        self.assertIn("--gain-target=-14", run(spawn(Config(**DEFAULTS))))
        self.assertIn("--gain-target=-18", run(spawn(Config(**{**DEFAULTS, "loudness_target": -18}))))
        self.assertFalse(any("gain" in a for a in run(spawn(
            Config(**{**DEFAULTS, "loudness_normalize": False})))))
        # ručně zapsaný nesmysl v config.toml se drží v mezích nastavení
        self.assertEqual(MpvPlayer(Config(**{**DEFAULTS, "loudness_target": 40}))._gain_target(), -8)
        self.assertEqual(MpvPlayer(Config(**{**DEFAULTS, "loudness_target": -99}))._gain_target(), -24)

    def test_one_telemetry_event_per_track_and_reset_for_the_next(self) -> None:
        async def go():
            async with Harness() as h:
                await h.player.enqueue([T(1), T(2), T(3)])
                await h.settle()
                self.gain_event(h, vid(1), "-10.5", "-3.51", "json", "ok")
                await h.settle()
                h.fake._send({"event": "property-change", "name": "core-idle", "data": False})
                await h.settle()
                h.fake.finish_current()
                await h.settle()
                # další skladba: YouTube hlasitost neřekl → nic se nenasadilo
                self.gain_event(h, vid(2), "", "", "none", "none")
                await h.settle()
                h.fake._send({"event": "property-change", "name": "core-idle", "data": True})
                h.fake._send({"event": "property-change", "name": "core-idle", "data": False})
                await h.settle()
                h.fake.finish_current()
                await h.settle()
                self.gain_event(h, vid(3), "3.9", "-17.93", "message", "limited")
                await h.settle()
                return h.events

        events = run(go())
        gains = [f for k, f in events if k == "track.gain"]
        self.assertEqual(len(gains), 3)  # jedna událost na skladbu
        self.assertEqual(gains[0], {"video_id": vid(1), "loud_lufs": -3.51, "gain_db": -10.5,
                                    "src": "youtube", "via": "json", "state": "ok", "target": -14})
        self.assertEqual((gains[1]["gain_db"], gains[1]["loud_lufs"], gains[1]["src"],
                          gains[1]["state"]), (0.0, None, "none", "none"))
        self.assertEqual((gains[2]["gain_db"], gains[2]["state"], gains[2]["via"]),
                         (3.9, "limited", "message"))
        starts = [f for k, f in events if k == "track.start"]
        self.assertEqual([(s["video_id"], s.get("gain_db")) for s in starts],
                         [(vid(1), -10.5), (vid(2), 0.0)])  # druhá nezdědila −10,5

    def test_volume_is_never_touched(self) -> None:
        async def go():
            async with Harness() as h:
                h.player._gain_script = True
                await h.player.set_volume(40)
                await h.player.enqueue([T(1), T(2)])
                await h.settle()
                mark = len(h.fake.log)
                self.gain_event(h, vid(1), "-10.5", "-3.51", "json", "ok")
                h.player._on_resolver_event("resolver.get", {
                    "video_id": vid(2), "hit": True, "ok": True, "loud": -17.93, "gain": 3.9})
                h.player.cfg.loudness_target = -18
                await h.player.config_changed()
                h.player.cfg.loudness_normalize = False
                await h.player.config_changed()
                await h.settle()
                st = await h.player.status()
                return st.volume, h.player.cfg.volume, h.player._volume, h.fake.log[mark:]

        volume, saved, held, log = run(go())
        self.assertEqual((volume, saved, held), (40, 40, 40))
        flat = [c for c in log if isinstance(c, list)]
        self.assertFalse([c for c in flat if c[0] == "set_property"], flat)
        self.assertFalse([c for c in flat if "volume" in json.dumps(c)], flat)

    def test_live_change_from_web_reaches_resolver_and_loads_script(self) -> None:
        async def go():
            async with Harness() as h:
                h.player.cfg.loudness_normalize = False
                h.player._gain_sent = None  # spuštěno vypnuté: skript v mpv není
                await h.player.config_changed()
                self.assertEqual(h.resolver, [])  # nic se nezměnilo → nic se neposílá
                h.player.cfg.loudness_normalize = True
                await h.player.config_changed()
                self.assertIn(["load-script", str(GAIN_SCRIPT)], h.fake.log)
                self.assertTrue(h.player._gain_script)
                h.player.cfg.loudness_target = -18
                await h.player.config_changed()
                h.player.cfg.loudness_normalize = False
                await h.player.config_changed()
                await h.player.config_changed()
                loads = [c for c in h.fake.log if isinstance(c, list) and c[0] == "load-script"]
                return h.resolver, h.events, loads

        resolver, events, loads = run(go())
        self.assertEqual([r for r in resolver if r["op"] == "gain"],
                         [{"op": "gain", "target": -14}, {"op": "gain", "target": -18},
                          {"op": "gain", "target": None}])
        self.assertEqual(len(loads), 1)  # skript se načte jednou
        conf = [f for k, f in events if k == "player.gain_config"]
        self.assertEqual([(c["enabled"], c["target"], c["ok"]) for c in conf],
                         [(True, -14, True), (True, -18, True), (False, None, True)])

    def test_change_is_retried_until_the_resolver_takes_it(self) -> None:
        async def go():
            async with Harness() as h:
                calls: list[dict] = []
                alive = False

                async def resolver_call(req: dict):
                    calls.append(req)
                    return {"ok": True} if alive else None  # resolver se zrovna restartuje

                h.player._resolver_call = resolver_call  # type: ignore[method-assign]
                h.player.cfg.loudness_target = -20
                await h.player.config_changed()
                self.assertEqual(h.player._gain_sent, -14)  # nepřevzal → zkusí se znovu
                alive = True
                with mock.patch.object(mpvmod, "PREFETCH_SETTLE", 0.0):
                    h.player._schedule_prefetch()
                    await h.settle(0.1)
                return calls, h.player._gain_sent

        calls, sent = run(go())
        self.assertEqual([c for c in calls if c["op"] == "gain"],
                         [{"op": "gain", "target": -20}] * 2)
        self.assertEqual(sent, -20)

    def test_lift_of_quiet_passages_is_off_by_default_and_reaches_the_script(self) -> None:
        # F-ZVUK-32: výchozí vypnuto = mpv i skript přesně jako bez něj
        self.assertIs(DEFAULTS["loudness_lift_quiet"], False)
        with mock.patch.object(MpvPlayer, "_ytdl_shim", lambda self: "/shim"):
            plain = MpvPlayer(Config(**DEFAULTS))._args()
            on = MpvPlayer(Config(**{**DEFAULTS, "loudness_lift_quiet": True, "loudness_target": -12}))
            lifted = on._args()
            off = MpvPlayer(Config(**{**DEFAULTS, "loudness_lift_quiet": True,
                                      "loudness_normalize": False}))._args()
        self.assertFalse(any("ytdj_gain-" in a for a in plain + off))
        self.assertEqual([a for a in lifted if a not in plain],
                         ["--script-opts-append=ytdj_gain-lift=yes",
                          "--script-opts-append=ytdj_gain-target=-12"])
        self.assertEqual(on._lift_sent, (True, -12))
        # ručně zapsaný nesmysl v config.toml nic nezapne
        for junk in (1, "yes", "x", None):
            self.assertEqual(
                MpvPlayer(Config(**{**DEFAULTS, "loudness_lift_quiet": junk}))._gain_lift(),
                (False, None))

        async def go():
            async with Harness() as h:
                await h.player.config_changed()  # nic se nezměnilo → žádný příkaz
                quiet = [c for c in h.fake.log if isinstance(c, list) and c[0] == "change-list"]
                h.player.cfg.loudness_lift_quiet = True
                await h.player.config_changed()
                h.player.cfg.loudness_target = -18  # cíl se změnil: skript ho dostane taky
                await h.player.config_changed()
                h.player.cfg.loudness_lift_quiet = False
                await h.player.config_changed()
                await h.player.config_changed()
                self.gain_event(h, vid(1), "-2.0", "-12.00", "json", "ok", "lift")
                self.gain_event(h, vid(2), "-2.0", "-12.00", "json", "ok")
                await h.settle()
                return quiet, [c[3] for c in h.fake.log
                               if isinstance(c, list)
                               and c[:3] == ["change-list", "script-opts", "append"]], h.events

        quiet, sent, events = run(go())
        self.assertEqual(quiet, [])
        self.assertEqual(sent, ["ytdj_gain-lift=yes", "ytdj_gain-target=-14",
                                "ytdj_gain-lift=yes", "ytdj_gain-target=-18", "ytdj_gain-lift=no"])
        self.assertEqual([(f["enabled"], f["target"]) for k, f in events if k == "player.gain_lift"],
                         [(True, -14), (True, -18), (False, None)])
        self.assertEqual([f.get("lift") for k, f in events if k == "track.gain"], [True, None])

    def test_gain_is_pushed_to_the_script_for_old_mpv(self) -> None:
        async def go(script: bool):
            async with Harness() as h:
                h.player._gain_script = script
                h.player._on_resolver_event("resolver.get", {
                    "video_id": vid(1), "hit": True, "ok": True, "loud": -3.51, "gain": -10.5})
                h.player._on_resolver_event("resolver.get", {  # bez zisku (neznámá / vypnuto)
                    "video_id": vid(2), "hit": True, "ok": True})
                await h.settle()
                return [c for c in h.fake.log if isinstance(c, list) and c[0].startswith("script")]

        self.assertEqual(run(go(True)), [["script-message-to", mpvmod.GAIN_CLIENT, "gain",
                                          vid(1), "-10.5", "-3.51"]])
        self.assertEqual(run(go(False)), [])  # vypnuto: žádný příkaz navíc

    def test_track_whose_filter_fails_is_replayed_without_it_not_blacklisted(self) -> None:
        async def go():
            async with Harness() as h:
                seen: list[tuple[str, str]] = []

                async def handler(ev):
                    seen.append((ev.kind, ev.track.id if ev.track else ""))

                h.player.on_event(handler)
                await h.player.enqueue([T(1), T(2)])
                await h.settle()
                entry = h.fake.playlist[0]["id"]
                self.gain_event(h, vid(1), "3.9", "-17.93", "json", "limited")
                await h.settle()
                # mpv filtr nepostavilo: skladba vůbec nezazněla, mpv jde na další
                h.fake._send({"event": "end-file", "reason": "error", "playlist_entry_id": entry,
                              "file_error": "no audio or video data played"})
                self.gain_event(h, vid(1), "", "", "none", "failed")  # hlásí skript
                h.fake._start(1)
                h.fake._notify()
                await h.settle(0.2)
                first = (list(seen), h.fake.current_vid())
                # podruhé (skript ji pustil bez filtru) a selhala zase → skutečná chyba
                self.gain_event(h, vid(1), "3.9", "-17.93", "json", "broken")
                await h.settle()
                h.fake._send({"event": "end-file", "reason": "error", "playlist_entry_id": entry,
                              "file_error": "no audio or video data played"})
                await h.settle(0.2)
                return first, seen, h.events

        (seen1, playing), seen2, events = run(go())
        # žádná černá listina ("error") ani výpadek ("unavailable" ×2 → stojí se)
        self.assertFalse([k for k, _ in seen1 if k in ("error", "unavailable", "outage")], seen1)
        self.assertEqual(playing, vid(1))  # tatáž položka znovu, ne další skladba
        retry = [f for k, f in events if k == "player.retry"]
        self.assertEqual([(r["video_id"], r["why"], r["ok"]) for r in retry],
                         [(vid(1), "gain_filter", True)])
        self.assertEqual([f for k, f in events if k == "player.gain_failed"],
                         [{"video_id": vid(1), "disabled": False}])
        # bez filtru už se selhání neschovává (jde běžnou cestou chyb přehrávání)
        self.assertTrue([k for k, v in seen2 if k in ("error", "unavailable") and v == vid(1)], seen2)


def _sine(path: Path, secs: float, amp: float, rate: int = 48000) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(
            struct.pack("<h", int(32767 * amp * math.sin(2 * math.pi * 440 * i / rate))) * 2
            for i in range(int(rate * secs))))


@unittest.skipUnless(shutil.which("mpv"), "mpv na tomhle stroji není")
class RealMpv(unittest.TestCase):
    """F-ZVUK-25: skutečné mpv se skriptem ytdj_gain.lua, výstup do souboru
    (--ao=pcm, nic nehraje nahlas), s volbami zvuku jako v provozu."""

    RATE = 48000
    SECS = 1.0

    def play(self, tracks: list[tuple[float, str | None]], json_tail: str | None = None,
             script: Path = GAIN_SCRIPT, extra: tuple = (), before: tuple = ()):
        """tracks = [(amplituda, zisk nebo None)]. Vrací (zprávy skriptu,
        vzorky levého kanálu, hlasitost mpv na konci)."""
        d = Path(tempfile.mkdtemp(dir=_TMP))
        sock, out = d / "s", d / "out.wav"
        files = []
        for i, (amp, _gain) in enumerate(tracks):
            path = d / f"watch?v=TRACK{i:06d}"  # skript bere videoId z adresy
            _sine(path, self.SECS, amp, self.RATE)
            files.append(path)
        proc = subprocess.Popen(
            ["mpv", "--no-config", "--idle=yes", "--no-video", "--no-terminal", "--ytdl=no",
             f"--input-ipc-server={sock}", "--gapless-audio=weak", "--audio-buffer=2",
             "--keep-open=no", "--volume=100", "--volume-max=100", f"--script={script}",
             "--audio-format=float", "--ao=pcm", f"--ao-pcm-file={out}", *extra],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            t0 = time.monotonic()
            while not sock.exists():
                self.assertLess(time.monotonic() - t0, 15, "mpv nenastartovalo")
                time.sleep(0.01)
            s = socket.socket(socket.AF_UNIX)
            s.connect(str(sock))
            s.settimeout(30)
            f = s.makefile("rwb")

            def cmd(*c, rid: int = 0) -> None:
                f.write((json.dumps({"command": list(c), "request_id": rid}) + "\n").encode())
                f.flush()

            def wait_for(pred):
                while True:
                    line = f.readline()
                    self.assertTrue(line, "mpv skončilo")
                    m = json.loads(line)
                    if m.get("event") == "client-message":
                        msgs.append(m["args"])
                    if pred(m):
                        return m

            msgs: list[list[str]] = []
            cmd("get_property", "mpv-version", rid=7)  # skripty jsou načtené, až mpv odpovídá
            wait_for(lambda m: m.get("request_id") == 7)
            time.sleep(0.2)
            for i, (_amp, gain) in enumerate(tracks):
                if gain is not None:  # tak to posílá ytdj (MpvPlayer._push_gain)
                    cmd("script-message-to", mpvmod.GAIN_CLIENT, "gain", f"TRACK{i:06d}", gain,
                        "-10.00")
            if json_tail is not None:  # co po sobě nechává ytdl_hook v mpv ≥ 0.38
                cmd("set_property", "user-data/mpv/ytdl/json-subprocess-result",
                    {"status": 0, "stdout": '{"id": "x", "pad": "' + "y" * 4000 + '"' + json_tail})
            for c in before:  # příkazy, které ytdj posílá za běhu (nastavení z webu)
                cmd(*c)
            if before:
                time.sleep(0.2)
            for path in files:
                cmd("loadfile", str(path), "append-play")
            wait_for(lambda m: m.get("event") == "start-file")
            wait_for(lambda m: m.get("event") == "idle")
            cmd("get_property", "volume", rid=8)
            volume = wait_for(lambda m: m.get("request_id") == 8).get("data")
            cmd("quit")
            proc.wait(10)
            f.close()
            s.close()
        finally:
            if proc.poll() is None:
                proc.kill()
        blob = out.read_bytes()
        _tag, ch, rate, _br, _al, bits = struct.unpack_from("<HHIIHH", blob, blob.index(b"fmt ") + 8)
        raw = blob[blob.index(b"data") + 8:]
        self.assertEqual((rate, bits), (self.RATE, 32))  # float jako výstup na Pi
        left = [v[0] for v in struct.iter_unpack("<" + "f" * ch, raw[:len(raw) // (4 * ch) * 4 * ch])]
        return msgs, left, volume

    @staticmethod
    def db(samples: list[float], amp: float) -> float:
        """O kolik dB je úsek hlasitější než sinus s amplitudou `amp`."""
        rms = math.sqrt(sum(v * v for v in samples) / len(samples))
        return 20 * math.log10(max(rms, 1e-9) / (amp / math.sqrt(2)))

    def test_gain_changes_exactly_at_the_track_boundary_and_resets(self) -> None:
        gains = ["-6.0", None, "-12.0", "4.0"]
        msgs, left, volume = self.play([(0.25, g) for g in gains])
        n = int(self.RATE * self.SECS)
        self.assertEqual(len(left), n * len(gains))  # bez mezery a bez ztracených vzorků
        want = [-6.0, 0.0, -12.0, 4.0]  # druhá skladba: zisk první se na ni nepřenesl
        edge = self.RATE // 50  # 20 ms hned za začátkem a těsně před koncem skladby
        for i, w in enumerate(want):
            a = i * n
            for name, part in (("začátek", left[a + 48:a + 48 + edge]),
                               ("střed", left[a + n // 4:a + 3 * n // 4]),
                               ("konec", left[a + n - 48 - edge:a + n - 48])):
                self.assertAlmostEqual(self.db(part, 0.25), w, delta=0.15, msg=f"skladba {i} {name}")
        self.assertEqual([m[1:] for m in msgs if m[0] == mpvmod.GAIN_MESSAGE], [
            ["TRACK000000", "-6.0", "-10.00", "message", "ok"],
            ["TRACK000001", "", "", "none", "none"],
            ["TRACK000002", "-12.0", "-10.00", "message", "ok"],
            ["TRACK000003", "4.0", "-10.00", "message", "limited"]])
        self.assertEqual(volume, 100)  # hlasitost mpv se nezměnila

    def test_boost_goes_through_the_limiter_and_never_clips(self) -> None:
        # sinus se špičkou 0,8 zesílený o 6 dB by měl špičku 1,6 (přebuzení)
        msgs, left, _ = self.play([(0.8, "6.0"), (0.8, None)])
        n = int(self.RATE * self.SECS)
        boosted, plain = left[:n], left[n:]
        self.assertLessEqual(max(abs(v) for v in boosted), 0.9)  # strop −1 dBFS
        self.assertGreater(self.db(boosted[n // 4:], 0.8), 0.5)  # a přesto hlasitější
        self.assertAlmostEqual(max(abs(v) for v in plain), 0.8, delta=0.01)  # další beze změny
        self.assertEqual(msgs[0][-1], "limited")

    def test_gain_from_resolver_json_is_applied(self) -> None:
        # zisk na konci JSONu od resolveru (cesta mpv ≥ 0.38 přes ytdl_hook)
        tail = ', "ytdj_loudness_lufs": -8.04, "ytdj_gain_db": -6.00}'
        msgs, left, _ = self.play([(0.25, None)], json_tail=tail)
        if msgs and msgs[0][4] == "none":
            self.skipTest("tohle mpv nemá vlastnosti user-data")
        self.assertEqual(msgs[0][1:], ["TRACK000000", "-6.0", "-8.04", "json", "ok"])
        self.assertAlmostEqual(self.db(left[4800:-4800], 0.25), -6.0, delta=0.1)
        # JSON bez zisku (vypnuto / YouTube hlasitost neřekl) → beze změny
        msgs, left, _ = self.play([(0.25, None)], json_tail=', "ytdj_loudness_lufs": -8.04}')
        self.assertEqual(msgs[0][4:], ["none", "none"])
        self.assertAlmostEqual(self.db(left[4800:-4800], 0.25), 0.0, delta=0.1)


    def test_quiet_passages_are_lifted_by_a_bounded_amount_only_when_asked(self) -> None:
        # F-ZVUK-32: hlasitá / tichá / velmi tichá „pasáž" (zisk skladby 0 dB)
        amps = [0.5, 0.02, 0.002]
        tracks = [(a, "0.0") for a in amps]
        n = int(self.RATE * self.SECS)

        def levels(left: list[float]) -> list[float]:
            self.assertEqual(len(left), n * len(amps))
            return [self.db(left[i * n + n // 2:(i + 1) * n - 480], a) for i, a in enumerate(amps)]

        # výchozí (0): nic se nepřidává — skladba na cílové hlasitosti nemá žádný filtr
        msgs, left, _ = self.play(tracks)
        self.assertEqual([m[5:] for m in msgs if m[0] == mpvmod.GAIN_MESSAGE], [["unity"]] * 3)
        for got in levels(left):  # 0,15: zaokrouhlení 16bitového sinu u nejtišší pasáže
            self.assertAlmostEqual(got, 0.0, delta=0.15)
        opts = MpvPlayer._lift_opts((True, -12))
        started = self.play(tracks, extra=tuple(f"--script-opts-append={o}" for o in opts))
        live = self.play(tracks, before=tuple(  # zapnuto za běhu, jak to posílá ytdj
            ("change-list", "script-opts", "append", o) for o in opts))
        for msgs, left, volume in (started, live):
            self.assertEqual([m[5:] for m in msgs if m[0] == mpvmod.GAIN_MESSAGE],
                             [["ok", "lift"]] * 3)
            loud, quiet, faint = levels(left)
            self.assertLess(abs(loud), 2.5)  # hlasitá pasáž zůstává, kde byla
            self.assertAlmostEqual(quiet, 6.5, delta=0.5)  # tichá se přizvedne…
            self.assertAlmostEqual(faint, 6.5, delta=0.5)  # …ale nikdy o víc (šum, dozvuk)
            self.assertLessEqual(max(abs(v) for v in left), 0.9)  # vždy přes omezovač
            self.assertEqual(volume, 100)
        # vypnutí za běhu: další skladby zase beze změny
        msgs, left, _ = self.play(
            tracks, extra=tuple(f"--script-opts-append={o}" for o in opts),
            before=(("change-list", "script-opts", "append", "ytdj_gain-lift=no"),))
        self.assertEqual([m[5:] for m in msgs if m[0] == mpvmod.GAIN_MESSAGE], [["unity"]] * 3)
        for got in levels(left):  # 0,15: zaokrouhlení 16bitového sinu u nejtišší pasáže
            self.assertAlmostEqual(got, 0.0, delta=0.15)

    def test_lift_follows_the_target(self) -> None:
        # práh je vztažený k cíli: při cíli −18 je „tichá" až pasáž o 6 dB tišší
        n = int(self.RATE * self.SECS)
        out = []
        for target in (-12, -18):
            opts = MpvPlayer._lift_opts((True, target))
            _, left, _ = self.play([(0.06, "0.0")],
                                   extra=tuple(f"--script-opts-append={o}" for o in opts))
            out.append(self.db(left[n // 2:n - 480], 0.06))
        self.assertGreater(out[0], out[1] + 1.0)

    def test_unbuildable_filter_is_dropped_instead_of_silence(self) -> None:
        # jiné ffmpeg, které filtr nezná: skladba s filtrem by vůbec nezazněla
        text = GAIN_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("latency=1", text)
        broken = Path(tempfile.mkdtemp(dir=_TMP)) / GAIN_SCRIPT.name
        broken.write_text(text.replace("latency=1", "no_such_option=1"), encoding="utf-8")
        msgs, left, _ = self.play([(0.25, "4.0")] * 4, script=broken)
        self.assertEqual([(m[1][-1], m[5]) for m in msgs if m[0] == mpvmod.GAIN_MESSAGE], [
            ("0", "limited"), ("0", "failed"),  # nezazněla → ytdj ji pustí znovu (bez filtru)
            ("1", "limited"), ("1", "disabled"),  # podruhé za sebou → do restartu vypnuto
            ("2", "broken"), ("3", "broken")])  # další už hrají, beze změny
        n = int(self.RATE * self.SECS)
        self.assertEqual(len(left), 2 * n)
        self.assertAlmostEqual(self.db(left[n // 4:-n // 4], 0.25), 0.0, delta=0.1)


class WebSettings(unittest.TestCase):
    """Nastavení na webu: jen přepínač a číslo v mezích, mění se za běhu."""

    def test_keys_are_plain_live_and_bounded(self) -> None:
        from ytdj.web import server as web

        for key in ("loudness_normalize", "loudness_target", "loudness_lift_quiet"):
            self.assertIn(key, web.LIVE_KEYS)
            self.assertIn(key, web.FIELD_META)
            self.assertNotIn(key, web.RESTART_KEYS)
        self.assertEqual(web._field_type("loudness_normalize"), "bool")
        self.assertEqual(web._field_type("loudness_target"), "int")
        self.assertEqual(web.FIELD_META["loudness_target"][2], mpvmod.GAIN_TARGET_RANGE)
        self.assertEqual(web.coerce_value("loudness_target", "-18"), -18)
        self.assertIs(web.coerce_value("loudness_normalize", "false"), False)
        for bad in (0, -7, -25, "hodně", None, True, "-14; rm"):
            with self.assertRaises(web.BadValue):
                web.coerce_value("loudness_target", bad)
        with self.assertRaises(web.BadValue):
            web.coerce_value("loudness_normalize", "--script=x")
        # F-ZVUK-32: přizvednutí tichých pasáží je jen přepínač, výchozí vypnuto
        self.assertEqual(web._field_type("loudness_lift_quiet"), "bool")
        self.assertIs(web.coerce_value("loudness_lift_quiet", "true"), True)
        with self.assertRaises(web.BadValue):
            web.coerce_value("loudness_lift_quiet", "yes,ytdj_gain-target=0")

    def test_config_post_tells_the_player(self) -> None:
        from test_admin import call, make
        from ytdj import adminpin
        from ytdj.web import server as web

        async def go():
            d = Path(tempfile.mkdtemp(dir=_TMP))
            srv, app, routes, _clock = make(d)
            told: list[tuple] = []

            async def config_changed():
                told.append((app.cfg.loudness_normalize, app.cfg.loudness_target))

            app.player = SimpleNamespace(config_changed=config_changed)
            pin = adminpin.ensure_pin(d / "admin-pin")
            with mock.patch.object(web.cfgmod, "save_values") as save:
                r = await call(routes, "POST", "/api/config",
                               {"loudness_target": -18, "loudness_normalize": True}, pin=pin)
            return r, told, save.call_args, app.cfg.volume

        r, told, saved, volume = run(go())
        self.assertEqual(r.status_code, 200, r.body)
        self.assertEqual(json.loads(r.body)["restart_required"], [])  # bez restartu hudby
        self.assertEqual(told, [(True, -18)])
        self.assertEqual(saved.args[0], {"loudness_target": -18, "loudness_normalize": True})
        self.assertEqual(volume, DEFAULTS["volume"])


if __name__ == "__main__":
    unittest.main()
