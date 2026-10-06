"""Ztlumit zvuk jedním kliknutím (POZADAVKY #70, F-HLAS-08, F-HLAS-09) — přehrávač,
web a skutečné mpv (místní tón do roury, nic nehraje nahlas):

    .venv/bin/python -m unittest tests.test_mute -v
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import math
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ytdj-mute-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)
os.environ.setdefault("YTDJ_EVENTS_FILE", str(Path(_TMP) / "events.jsonl"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_controls as tc  # noqa: E402  (RealRig, watch, as_app, outcomes, sounds)
import test_wishes as tw  # noqa: E402
from test_player_queue import Harness, T, vid  # noqa: E402
from ytdj import config as cfgmod  # noqa: E402
from ytdj.config import DEFAULTS, Config  # noqa: E402
from ytdj.player.mpv import AUDIO_BUFFER, MpvPlayer  # noqa: E402
from ytdj.web import server as web  # noqa: E402

run = tw.run
PETR, JANA = "client-petr", "client-jana"
INDEX = Path(__file__).resolve().parents[1] / "ytdj/web/static/index.html"


def post(body: dict, **kw):
    return tw.req(body, **kw)


class PlayerMute(unittest.TestCase):
    """F-HLAS-08: ztlumení je vlastnost `mute`, hlasitost se nemění."""

    def test_toggle_keeps_the_level(self) -> None:
        async def go():
            async with Harness() as h:
                p = h.player
                seen = tc.watch(p)
                await p.enqueue([T(i) for i in range(3)])
                await h.settle()
                await p.set_volume(40)
                await h.settle()
                saves = p._volume_pending
                self.assertFalse((await p.status()).muted)
                self.assertTrue(await p.set_mute(True))
                await h.settle()
                st = await p.status()
                self.assertEqual((st.muted, st.volume, st.playing, st.paused), (True, 40, True, False))
                self.assertEqual((h.fake.props["mute"], h.fake.props["volume"]), (True, 40))
                self.assertEqual((p.cfg.volume, p._volume_pending), (40, saves))  # nic se neukládá
                self.assertFalse(await p.set_mute())  # bez hodnoty = přepnout
                self.assertTrue(await p.set_mute())
                self.assertFalse(await p.set_mute(False))
                await h.settle()
                st = await p.status()
                self.assertEqual((st.muted, st.volume), (False, 40))  # přesně jako předtím
                sets = [c[1:] for c in h.fake.log if isinstance(c, list) and c[0] == "set_property"]
                self.assertEqual([s for s in sets if s[0] == "volume"], [["volume", 40]])  # nikdy 0
                self.assertNotIn(["pause", True], sets)  # není to pauza
                # hudba běží dál: žádný konec skladby, stejná skladba i fronta
                self.assertEqual([k for k, _ in seen if k not in ("start", "sound")], [])
                self.assertEqual((h.fake.current_vid(), h.fake.upcoming()), (vid(0), [vid(1), vid(2)]))

        run(go())

    def test_touching_the_volume_turns_the_sound_back_on(self) -> None:
        async def go():
            async with Harness() as h:
                p = h.player
                await p.enqueue([T(0)])
                await h.settle()
                await p.set_volume(70)
                await p.set_mute(True)
                await p.set_volume(55)  # posuvník, displej, kolečko, „hlasitost 55" — jedna cesta
                await h.settle()
                st = await p.status()
                self.assertEqual((st.muted, st.volume), (False, 55))
                self.assertEqual(h.fake.props["mute"], False)
                # se zvukem se na ztlumení při změně hlasitosti nesahá
                n = len(h.fake.log)
                await p.set_volume(50)
                self.assertEqual([c for c in h.fake.log[n:] if isinstance(c, list) and "mute" in c], [])

        run(go())

    def test_player_follows_what_mpv_reports(self) -> None:
        async def go():
            async with Harness() as h:
                p = h.player
                self.assertIn("mute", h.fake.observed)  # sleduje se jako hlasitost a pauza
                p._handle_event({"event": "property-change", "name": "mute", "data": True})
                self.assertTrue((await p.status()).muted)
                p._handle_event({"event": "property-change", "name": "mute", "data": False})
                self.assertFalse((await p.status()).muted)

        run(go())

    def test_after_a_restart_the_sound_is_on(self) -> None:
        # ztlumení se nikam neukládá a mpv startuje vždy se zvukem: po restartu
        # služby ani po zapnutí Pi jukebox nehraje „potichu bez důvodu"
        self.assertEqual([k for k in DEFAULTS if "mute" in k], [])
        self.assertNotIn("mute", Path(cfgmod.__file__).read_text())
        p = MpvPlayer(Config(**{**DEFAULTS, "volume": 35}))
        self.assertFalse(p._muted)
        args = p._args()
        self.assertEqual([a for a in args if "mute" in a], [])
        self.assertIn("--volume=35", args)  # hlasitost si pamatuje dál (F-HLAS-05)


class WebMute(unittest.TestCase):
    """F-HLAS-08, F-HLAS-09: /api/control {"action": "mute"} a stav pro všechny."""

    def test_endpoint_sets_toggles_and_reports(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                await rig.background()
                srv = web.WebServer(tw.WebApp(rig))
                await rig.player.set_volume(45)
                self.assertEqual((await srv._snapshot())["muted"], False)
                r = await srv._control(post({"action": "mute", "value": True, "client": PETR,
                                             "who": "Petr"}))
                self.assertEqual((r.status_code, tw.body(r)), (200, {"ok": True, "muted": True, "volume": 45}))
                snap = await srv._snapshot()
                self.assertEqual((snap["muted"], snap["volume"], snap["playing"], snap["paused"]),
                                 (True, 45, True, False))
                r = await srv._control(post({"action": "mute", "client": JANA}))  # přepnout — kdokoli
                self.assertEqual(tw.body(r)["muted"], False)
                self.assertEqual((await srv._snapshot())["volume"], 45)
                for bad in (1, 0, "ano", "true", [True]):
                    r = await srv._control(post({"action": "mute", "value": bad, "client": "client-x"}))
                    self.assertEqual(r.status_code, 400, bad)
                ev = [f for k, f in rig.events if k == "player.mute"]
                self.assertEqual([(f["muted"], f["by"], f["source"], f["cid"]) for f in ev],
                                 [(True, "Petr", "web", PETR[-6:]), (False, "někdo", "web", JANA[-6:])])
                await asyncio.sleep(web.SEEK_WINDOW)
                r = await srv._control(post({"action": "mute", "value": True}, ua="ytdj-panel"))
                self.assertEqual(r.status_code, 200)
                last = [f for k, f in rig.events if k == "player.mute"][-1]
                self.assertEqual((last["by"], last["source"]), ("displej", "panel"))
                ctl = [f for k, f in rig.events if k == "web.control" and f.get("action") == "mute"]
                self.assertEqual((ctl[0]["status"], ctl[0]["muted"]), (200, True))

        run(go())

    def test_volume_from_the_web_unmutes(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                await rig.background()
                srv = web.WebServer(tw.WebApp(rig))
                await srv._control(post({"action": "mute", "value": True, "client": PETR}))
                r = await srv._control(post({"action": "volume", "value": 30, "client": JANA}))
                self.assertEqual(r.status_code, 200)
                snap = await srv._snapshot()
                self.assertEqual((snap["muted"], snap["volume"]), (False, 30))
                # povel slovy jde stejnou cestou
                await srv._control(post({"action": "mute", "value": True, "client": PETR}))
                r = await srv._prompt(post({"text": "hlasitost 40", "who": "Jana", "client": JANA}))
                self.assertEqual(tw.body(r)["reply"], "Hlasitost 40.")
                snap = await srv._snapshot()
                self.assertEqual((snap["muted"], snap["volume"]), (False, 40))

        run(go())

    def test_rate_limit(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                await rig.background()
                srv = web.WebServer(tw.WebApp(rig))
                codes = [(await srv._control(post({"action": "mute", "client": PETR}))).status_code
                         for _ in range(7)]
                self.assertEqual(codes, [200] * web.SEEK_RATE + [429] * (7 - web.SEEK_RATE))
                self.assertEqual(len([1 for k, _ in rig.events if k == "player.mute"]), web.SEEK_RATE)
                # kolega tím omezený není a posun ve skladbě má vlastní počítadlo
                r = await srv._control(post({"action": "mute", "client": JANA}))
                self.assertEqual(r.status_code, 200)
                await tc.sounds(rig, 200.0, 10.0)
                r = await srv._control(post({"action": "seek", "value": 50, "client": PETR}))
                self.assertEqual(r.status_code, 200)

        run(go())

    def test_everybody_sees_it_at_once(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                await rig.background()
                srv = web.WebServer(tw.WebApp(rig))
                watcher = asyncio.create_task(tw.sse(srv, 0.6))
                await asyncio.sleep(0.25)
                await srv._control(post({"action": "mute", "value": True, "client": PETR}))
                chunks = await watcher
                states = [json.loads(c[6:]) for c in chunks if c.startswith("data: ")]
                self.assertEqual([s["muted"] for s in states][0], False)
                self.assertEqual(states[-1]["muted"], True)  # dřív než další tik (1 s)

        run(go())

    def test_music_wishes_and_history_do_not_notice(self) -> None:
        async def go():
            async with tw.Rig() as rig:
                app = tc.as_app(rig)
                seen = tc.watch(rig.player)
                await rig.background()
                srv = web.WebServer(tw.WebApp(rig))
                p = rig.wq.submit("pusť Kabát", "Petr")
                await rig.until(lambda: p.state == "playing")
                await rig.settle(0.2)
                cur, queue = rig.fake.current_vid(), rig.upcoming()
                state = (p.state, p.played, set(p.done_ids), p.current, p.skips_others)
                n = len(seen)
                await srv._control(post({"action": "mute", "value": True, "client": JANA}))
                # přání kolegyně během ztlumení: zařadí se, hraje se dál — a zůstává ztlumeno
                j = rig.wq.submit("Holky z naší školky", "Jana")
                await rig.until(lambda: j.state == "queued")
                await rig.settle(0.2)
                snap = await srv._snapshot()
                self.assertEqual((snap["muted"], snap["playing"], snap["paused"]), (True, True, False))
                self.assertEqual(rig.fake.current_vid(), cur)
                self.assertEqual(seen[n:], [])  # žádný konec skladby, žádné přeskočení
                self.assertEqual((p.state, p.played, set(p.done_ids), p.current, p.skips_others), state)
                self.assertEqual(app.skips._skips, [])
                self.assertEqual(tc.outcomes(rig)[cur], "started")
                self.assertIsNone(rig.wq._paused_at)  # není to úmyslná pauza (F-START-04)
                self.assertTrue(set(queue) <= set(rig.upcoming()))
                # Další funguje dál a zvuk tím nenaskočí
                await srv._control(post({"action": "next", "client": PETR, "who": "Petr"}))
                await rig.until(lambda: rig.fake.current_vid() != cur)
                self.assertTrue((await srv._snapshot())["muted"])
                await asyncio.sleep(web.SEEK_WINDOW)
                await srv._control(post({"action": "mute", "value": False, "client": JANA}))
                self.assertFalse((await srv._snapshot())["muted"])

        run(go())


class Page(unittest.TestCase):
    def setUp(self) -> None:
        self.html = INDEX.read_text()

    def test_speaker_icon_is_the_mute_button(self) -> None:
        html = self.html
        self.assertRegex(html, r'<button class="mute" id="btnMute" type="button" aria-pressed="false" '
                               r'aria-label="Ztlumit zvuk \(hudba běží dál\)" title="Ztlumit \(hudba běží dál\)">')
        box = html[html.index('<div class="vol" id="volBox">'):html.index('<output id="volOut"')]
        self.assertLess(box.index('id="btnMute"'), box.index('id="vol"'))  # hned u posuvníku
        self.assertIn('control("mute", on)', html)
        self.assertIn('setAttribute("aria-pressed", on ? "true" : "false")', html)
        self.assertIn("showMuted(!!s.muted)", html)  # stav ze serveru, pro všechny
        css = re.search(r"\n  \.mute \{(.*?)\}", html, re.S).group(1)
        self.assertIn("width: 44px; height: 44px", css)
        # posuvník svou úroveň drží; ztlumení ji jen zašedí
        self.assertIn(".vol.muted input[type=range] { opacity: .4; }", html)
        muted = html[html.index("function showMuted(on)"):html.index("el.btnMute.addEventListener")]
        self.assertNotIn("el.vol.value", muted)
        # sáhnutí na posuvník během ztlumení zvuk vrátí hned i na stránce
        self.assertIn("if (S.muted) holdMuted(false);", html)
        # stav, který přišel během krátkého podržení po ťuknutí, se neztratí
        self.assertIn("setTimeout(function () { if (S.status) showMuted(!!S.status.muted); }, 1600)", html)

    def test_styles_use_only_theme_tokens(self) -> None:
        css = self.html[self.html.index("/* ztlumit jedním ťuknutím"):self.html.index("  input[type=range] {")]
        self.assertGreater(len(css), 400)
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", css), [])
        self.assertIn("var(--warn)", css)


def _tone(path: Path, secs: float, rate: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(16000 * math.sin(2 * math.pi * 440 * i / rate))) * 2
                               for i in range(int(rate * secs))))


@unittest.skipUnless(shutil.which("mpv"), "mpv na tomhle stroji není")
class RealMpv(unittest.TestCase):
    def test_mpv_is_muted_at_once_and_the_music_runs_on(self) -> None:
        """Skutečné mpv přes MpvPlayer: `mute` platí hned, hlasitost se nehne,
        skladba běží dál a posun i Další fungují jako vždy."""
        async def go():
            async with tc.RealRig() as rig:
                p = rig.player
                seen = tc.watch(p)
                await p.enqueue([tc.RT(i) for i in range(3)])
                await rig.plays(vid(0))
                await p.set_volume(60)
                await asyncio.sleep(0.3)
                t0 = time.monotonic()
                await p.set_mute(True)
                self.assertIs(await p._get("mute"), True)
                took = time.monotonic() - t0
                self.assertLess(took, 0.5)
                self.assertEqual(await p._get("volume"), 60)
                pos = await rig.mpv_pos()
                await asyncio.sleep(1.0)
                later = await rig.mpv_pos()
                self.assertTrue(0.7 <= later - pos <= 1.6, (pos, later))  # hraje dál, jen potichu
                st = await p.status()
                self.assertEqual((st.muted, st.volume, st.playing, st.paused, st.current.id),
                                 (True, 60, True, False, vid(0)))
                self.assertEqual((await p.seek(position=10))["to"], 10.0)  # posun i ztlumený
                await p.set_mute(False)
                self.assertIs(await p._get("mute"), False)
                self.assertEqual(await p._get("volume"), 60)  # přesně jako před ztlumením
                await p.set_mute(True)
                await p.set_volume(45)  # sáhnutí na hlasitost zvuk vrátí
                await asyncio.sleep(0.1)
                self.assertEqual((await p._get("mute"), await p._get("volume")), (False, 45))
                self.assertEqual(tc.since_start(seen, vid(0)), [("start", vid(0)), ("sound", vid(0))])

        run(go())

    def test_output_goes_silent_and_comes_back_at_exactly_the_same_level(self) -> None:
        """Co z mpv opravdu teče (--ao=pcm do roury, čtené rychlostí přehrávání,
        zásoba zvuku jako v provozu): po ztlumení čisté ticho, po zapnutí táž
        úroveň jako předtím — bez přechodu jinou hlasitostí."""
        rate = 48000
        d = Path(tempfile.mkdtemp(dir=_TMP))
        src, fifo, sock = d / "tone.wav", d / "out.pcm", d / "s"
        _tone(src, 10, rate)
        os.mkfifo(fifo)
        proc = subprocess.Popen(
            ["mpv", "--no-config", "--idle=yes", "--no-video", "--no-terminal", "--ytdl=no",
             f"--input-ipc-server={sock}", "--gapless-audio=weak", f"--audio-buffer={AUDIO_BUFFER:g}",
             "--keep-open=no", "--volume=60", "--volume-max=100", "--audio-format=s16",
             f"--audio-samplerate={rate}", "--audio-channels=stereo", "--ao=pcm",
             "--ao-pcm-waveheader=no", f"--ao-pcm-file={fifo}", str(src)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        data = bytearray()
        done = threading.Event()
        try:
            fd = os.open(fifo, os.O_RDONLY)
            try:
                fcntl.fcntl(fd, 1031, 4096)  # F_SETPIPE_SZ: co nejmenší roura
            except OSError:
                pass

            def reader() -> None:
                t0, bps = time.monotonic(), rate * 4
                while not done.is_set():
                    want = int((time.monotonic() - t0) * bps) - len(data)
                    if want <= 0:
                        time.sleep(0.002)
                        continue
                    chunk = os.read(fd, min(want, 65536))
                    if not chunk:
                        return
                    data.extend(chunk)

            threading.Thread(target=reader, daemon=True).start()
            t0 = time.monotonic()
            while not sock.exists():
                self.assertLess(time.monotonic() - t0, 15, "mpv nenastartovalo")
                time.sleep(0.01)
            s = socket.socket(socket.AF_UNIX)
            s.connect(str(sock))
            s.settimeout(10)
            f = s.makefile("rwb")

            def cmd(*c, rid: int = 0):
                f.write((json.dumps({"command": list(c), "request_id": rid}) + "\n").encode())
                f.flush()
                if rid:
                    while True:
                        m = json.loads(f.readline())
                        if m.get("request_id") == rid:
                            return m.get("data")

            time.sleep(2.0)
            at_mute = len(data) // 4
            cmd("set_property", "mute", True)
            time.sleep(2.5)
            self.assertEqual(cmd("get_property", "volume", rid=5), 60)  # ztlumené: hlasitost 60
            at_unmute = len(data) // 4
            cmd("set_property", "mute", False)
            time.sleep(2.5)
            volume = cmd("get_property", "volume", rid=6)
            done.set()
            time.sleep(0.05)
            f.close()
            s.close()
        finally:
            done.set()
            proc.kill()
            proc.wait(10)
        left = struct.unpack("<%dh" % (len(data) // 2), bytes(data[:len(data) // 2 * 2]))[::2]
        win = rate // 100

        def peak(a: int, b: int) -> int:
            return max(abs(v) for v in left[a:b])

        loud = peak(at_mute - rate, at_mute - win)
        self.assertGreater(loud, 1000)
        silent = next((i for i in range(at_mute, at_unmute, win // 4) if peak(i, i + win) == 0), None)
        self.assertIsNotNone(silent, "po ztlumení nepřišlo ticho")
        back = next((i for i in range(max(silent + win, at_unmute - win), len(left) - win, win // 4)
                     if peak(i, i + win) > 0), None)
        self.assertIsNotNone(back, "po zapnutí se zvuk nevrátil")
        # mezi tím jen ticho, žádný zbytek staré zásoby na plnou hlasitost
        self.assertEqual(peak(silent, back - win), 0)
        # úroveň po zapnutí = úroveň před ztlumením, na vzorek přesně; a nikdy víc
        self.assertEqual(peak(back + rate // 4, min(len(left), back + rate)), loud)
        self.assertLessEqual(peak(back, len(left)), loud)
        self.assertEqual(volume, 60)
        # ztlumení nepočkalo na celou zásobu zvuku na staré hlasitosti a pak dál
        self.assertLess((silent - at_mute) / rate, 2.4)


if __name__ == "__main__":
    unittest.main()
