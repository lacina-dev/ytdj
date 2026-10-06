"""The clip of the playing track on the TV — muted, in step with the music.

Only when the jukebox says so (`tv.video` in its status: the switch is on and
the playing track itself is an official video whose picture is ready) and only
while the Pi is comfortable. The music is never touched: the picture comes
from a second, expendable player started here with the options measured on the
Pi (hardware H.264 decoder straight to a DRM plane, no scaling in software),
at low priority, and it is told where the music is — never the other way round.

Three small parts, each testable without hardware:

  capability()  can this machine show video at all (KMS, HDMI, decoder)?
  Guard         stop when the Pi is hot, short of memory, throttled, when the
                sound stutters or the video itself does — and stay off for a
                while (longer each time), so nothing flaps
  Director      screen ↔ video: starts and stops the child, follows pause,
                seek and track changes, keeps the picture within ±150 ms of
                the music, and tells the screen when to stop and resume drawing
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

STATUS_FILE = Path(os.environ.get("YTDJ_TV_STATUS", "/run/ytdj-tv/status.json"))
PLAYER = "mpv"

# ---- sync ----
TOLERANCE = 0.15  # s — inside this the picture is "in step"; nothing is corrected
NUDGE_UP_TO = 1.0  # s — small drift: ±5 % speed, invisible on a muted picture
CHASE_UP_TO = 6.0  # s — bigger drift: ±25 % until it is caught, no re-seek
NUDGE, CHASE = 0.05, 0.25
SEEK_LEAD = 0.5  # s — a seek lands a little ahead: seeking itself takes time
START_LEAD = 2.0  # s — the player needs about this long to show its first frame
SEEK_EVERY = 5.0  # s — at most one re-seek in this time (no seek storms)
SYNC_EVERY = 2.0  # s between looks at the drift
CHECK_EVERY = 5.0  # s between looks at temperature, memory, dropped frames
STARTUP_MAX = 25.0  # s without a first frame = the player is stuck → give up


def sync_action(drift: float) -> tuple[str, float]:
    """What to do about `drift` (picture minus music, seconds).

    ("speed", x) — play at x (1.0 = in step); ("seek", lead) — jump to the
    music's position plus `lead`.
    """
    a = abs(drift)
    if a <= TOLERANCE:
        return "speed", 1.0
    ahead = drift > 0
    if a <= NUDGE_UP_TO:
        return "speed", 1.0 - NUDGE if ahead else 1.0 + NUDGE
    if a <= CHASE_UP_TO:
        return "speed", 1.0 - CHASE if ahead else 1.0 + CHASE
    return "seek", SEEK_LEAD


# ---- can this machine do it ----

def capability(root: str | os.PathLike = "/", which: Callable[[str], str | None] = shutil.which
               ) -> tuple[bool, str]:
    """(can it show video, why not — in Czech for the web's switch)."""
    root = Path(root)
    if which(PLAYER) is None:
        return False, "Na jukeboxu chybí přehrávač videa (mpv)."
    if not list((root / "dev/dri").glob("card*")):
        return False, "Jukebox nemá zapnutou grafiku pro video (KMS) — klipy nejdou."
    states = []
    for status in (root / "sys/class/drm").glob("card*-HDMI-A-*/status"):
        try:
            states.append(status.read_text().strip())
        except OSError:
            pass
    if "connected" not in states:
        return False, "Telka není připojená (HDMI)."
    if not (root / "dev/video10").exists():
        return False, "Jukebox nemá zapnutý dekodér videa — klipy nejdou."
    return True, ""


# ---- self-protection ----

@dataclass(frozen=True)
class Limits:
    """Thresholds from the Pi 3 measurements of 6 Oct (720p clip next to music:
    69 → 72.5 °C, free memory ≥ 275 MB, player 157 MB; software scaling: 84 °C
    and throttling within 100 s)."""

    temp_stop: float = 78.0  # °C — the Pi throttles at 80–85; stop before that
    temp_ok: float = 72.0  # °C — and come back only when it has cooled
    # free memory: the DJ's model is closed below 250 MB — the video must go
    # first, so it stops a little above that
    mem_stop: int = 260  # MB
    mem_start: int = 420  # MB — the player takes ~157 MB; stay above mem_stop after
    drop_frames: int = 25  # dropped frames in one CHECK window (10 s) that count as stutter…
    drop_windows: int = 2  # …this many windows in a row
    cooldown: float = 300.0  # s off after a stop; doubles with every further stop
    cooldown_max: float = 1800.0
    forgive_after: float = 1800.0  # s without a stop before the doubling starts over


@dataclass
class Readings:
    temp_c: float | None = None
    mem_avail_mb: int | None = None
    throttled: int | None = None  # get_throttled; low 4 bits = happening right now
    xruns: int | None = None  # the music's audio dropouts so far (from the jukebox)
    dropped: int | None = None  # the video player's dropped frames so far


def read_temp(path: str = "/sys/class/thermal/thermal_zone0/temp") -> float | None:
    try:
        return int(Path(path).read_text().strip()) / 1000.0
    except (OSError, ValueError):
        return None


def read_mem_avail(path: str = "/proc/meminfo") -> int | None:
    try:
        for line in Path(path).read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def read_throttled() -> int | None:
    """`vcgencmd get_throttled` (Raspberry Pi firmware); None where there is none."""
    exe = shutil.which("vcgencmd")
    if exe is None:
        return None
    try:
        out = subprocess.run([exe, "get_throttled"], capture_output=True, text=True, timeout=3).stdout
        return int(out.strip().split("=", 1)[1], 16)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


class Guard:
    """Decides whether video may run; remembers why not and for how long."""

    def __init__(self, limits: Limits = Limits(), clock: Callable[[], float] = time.monotonic) -> None:
        self.limits = limits
        self.clock = clock
        self.blocked_until = 0.0
        self.reason = ""
        self.trips = 0
        self.last_trip = float("-inf")
        self._xruns0: int | None = None
        self._dropped0: int | None = None
        self._bad_windows = 0
        self._hot = False  # stopped for temperature: wait for temp_ok, not just the cooldown

    def trip(self, reason: str) -> float:
        """Video must stop now. Returns for how long it stays off."""
        now = self.clock()
        if now - self.last_trip > self.limits.forgive_after:
            self.trips = 0
        self.trips += 1
        self.last_trip = now
        pause = min(self.limits.cooldown_max, self.limits.cooldown * 2 ** (self.trips - 1))
        self.blocked_until = now + pause
        self.reason = reason
        return pause

    def started(self, r: Readings) -> None:
        """A video just started: dropouts and dropped frames count from here."""
        self._xruns0, self._dropped0, self._bad_windows = r.xruns, r.dropped, 0

    def may_start(self, r: Readings) -> tuple[bool, str]:
        lim = self.limits
        if self.clock() < self.blocked_until:
            return False, self.reason
        if r.throttled is not None and r.throttled & 0xF:
            return False, "procesor je přiškrcený nebo má málo napětí"
        if r.temp_c is not None and r.temp_c > (lim.temp_ok if self._hot else lim.temp_stop - 1):
            return False, "jukebox je horký"
        if r.mem_avail_mb is not None and r.mem_avail_mb < lim.mem_start:
            return False, "málo volné paměti"
        self._hot = False
        self.reason = ""
        return True, ""

    def while_running(self, r: Readings) -> str:
        """"" = carry on; otherwise the reason — the video was stopped (trip)."""
        lim = self.limits
        why = ""
        if r.temp_c is not None and r.temp_c >= lim.temp_stop:
            why, self._hot = "jukebox je horký", True
        elif r.throttled is not None and r.throttled & 0xF:
            why = "procesor je přiškrcený nebo má málo napětí"
        elif r.mem_avail_mb is not None and r.mem_avail_mb < lim.mem_stop:
            why = "málo volné paměti"
        elif r.xruns is not None and self._xruns0 is not None and r.xruns > self._xruns0:
            why = "zvuk začal lupat"
        elif r.dropped is not None and self._dropped0 is not None:
            lost = r.dropped - self._dropped0
            self._dropped0 = r.dropped
            self._bad_windows = self._bad_windows + 1 if lost >= lim.drop_frames else 0
            if self._bad_windows >= lim.drop_windows:
                why = "obraz se zadrhával"
        if r.xruns is not None and self._xruns0 is None:
            self._xruns0 = r.xruns
        if r.dropped is not None and self._dropped0 is None:
            self._dropped0 = r.dropped
        if why:
            self.trip(why)
        return why


# ---- the player child ----

def player_args(url: str, sock: str, size: tuple[int, int] | None, start: float, paused: bool,
                headers: dict | None = None) -> list[str]:
    """The command line measured on the Pi (6 Oct): decoder output goes to its
    own DRM plane, nothing is scaled or converted in software. Without the two
    plane options the picture hides under an opaque plane."""
    args = [
        PLAYER, "--no-config", "--no-terminal", "--no-audio", "--ytdl=no", "--load-scripts=no",
        "--osc=no", "--osd-level=0", "--input-default-bindings=no", "--input-vo-keyboard=no",
        "--vo=gpu", "--gpu-context=drm", "--hwdec=v4l2m2m",
        "--drm-draw-plane=overlay", "--drm-drmprime-video-plane=primary",
        "--keep-open=no", "--idle=no", "--hr-seek=no", "--cache=yes",
        "--demuxer-max-bytes=24MiB", "--demuxer-max-back-bytes=4MiB",
        "--stream-lavf-o=reconnect=1,reconnect_streamed=1,reconnect_delay_max=5",
        f"--input-ipc-server={sock}", f"--start={max(0.0, start):.2f}",
        f"--pause={'yes' if paused else 'no'}",
    ]
    if size:
        # the mode the screen already runs in — not "the first one in the list"
        args.append(f"--drm-mode={size[0]}x{size[1]}")
    headers = headers or {}
    if headers.get("User-Agent"):
        args.append(f"--user-agent={headers['User-Agent']}")
    if headers.get("Referer"):
        args.append(f"--referrer={headers['Referer']}")
    return [*args, "--", url]


class Ipc:
    """mpv's JSON IPC over a unix socket — a handful of blocking calls."""

    def __init__(self, path: str, timeout: float = 1.0) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(path)
        self._buf = b""
        self._id = 0

    def command(self, *cmd: Any) -> Any:
        self._id += 1
        rid = self._id
        self.sock.sendall(json.dumps({"command": list(cmd), "request_id": rid}).encode() + b"\n")
        while True:
            while b"\n" not in self._buf:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise OSError("přehrávač zavřel spojení")
                self._buf += chunk
            line, self._buf = self._buf.split(b"\n", 1)
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("request_id") == rid:
                if msg.get("error") != "success":
                    return None
                return msg.get("data", True)

    def get(self, prop: str) -> Any:
        return self.command("get_property", prop)

    def set(self, prop: str, value: Any) -> None:
        self.command("set_property", prop, value)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class Session:
    """One clip: the child process and the talk to it."""

    def __init__(self, vid: str, stream: dict, start: float, paused: bool,
                 size: tuple[int, int] | None, sock: str, nice: int = 10,
                 popen: Callable[..., Any] = subprocess.Popen) -> None:
        self.vid = vid
        self.sock_path = sock
        try:
            os.unlink(sock)
        except OSError:
            pass
        args = player_args(stream["url"], sock, size, start, paused, stream.get("headers"))
        if shutil.which("nice"):
            args = ["nice", "-n", str(nice), *args]  # the music always goes first
        # stdin stays ours: the service's terminal (tty1), so the player can
        # take the display without "Can't open TTY for VT control"
        self.proc = popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.ipc: Ipc | None = None
        self.started_at = time.monotonic()
        self.showing = False
        self.speed = 1.0
        self.paused = paused
        self.last_seek = float("-inf")

    def alive(self) -> bool:
        return self.proc.poll() is None

    def exit_code(self) -> int | None:
        return self.proc.poll()

    def _connect(self) -> bool:
        if self.ipc is None:
            try:
                self.ipc = Ipc(self.sock_path)
            except OSError:
                return False
        return True

    def position(self) -> float | None:
        """Where the picture is; None until the first frame is up."""
        if not self._connect():
            return None
        try:
            pos = self.ipc.get("time-pos")
        except OSError:
            self.ipc = None
            return None
        if isinstance(pos, (int, float)):
            self.showing = True
            return float(pos)
        return None

    def dropped(self) -> int | None:
        if self.ipc is None:
            return None
        try:
            a, b = self.ipc.get("frame-drop-count"), self.ipc.get("decoder-frame-drop-count")
        except OSError:
            return None
        return int(a or 0) + int(b or 0) if isinstance(a, int) or isinstance(b, int) else None

    def set_pause(self, paused: bool) -> None:
        if self.ipc is not None and paused != self.paused:
            try:
                self.ipc.set("pause", bool(paused))
                self.paused = paused
            except OSError:
                pass

    def set_speed(self, speed: float) -> None:
        if self.ipc is not None and abs(speed - self.speed) > 1e-3:
            try:
                self.ipc.set("speed", round(speed, 3))
                self.speed = speed
            except OSError:
                pass

    def seek(self, pos: float) -> None:
        if self.ipc is not None:
            try:
                self.ipc.command("seek", round(max(0.0, pos), 2), "absolute")
                self.last_seek = time.monotonic()
            except OSError:
                pass

    def stop(self) -> None:
        if self.ipc is not None:
            try:
                self.ipc.command("quit")
            except OSError:
                pass
            self.ipc.close()
            self.ipc = None
        try:
            self.proc.wait(timeout=2)
        except Exception:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        try:
            os.unlink(self.sock_path)
        except OSError:
            pass


# ---- screen ↔ video ----

@dataclass
class Want:
    """What the jukebox's status asks for right now."""

    vid: str = ""  # the playing track whose picture should show ("" = none)
    position: float = 0.0  # where the music is
    paused: bool = False
    xruns: int | None = None
    on: bool = False  # the switch


def want_from(state: dict | None, position: float) -> Want:
    if not isinstance(state, dict):
        return Want()
    tv = state.get("tv") if isinstance(state.get("tv"), dict) else {}
    cur = state.get("current") if isinstance(state.get("current"), dict) else {}
    vid = tv.get("video") if tv.get("on") is True else None
    ok = isinstance(vid, str) and vid and vid == cur.get("id") and not state.get("outage")
    xruns = tv.get("xruns") if isinstance(tv.get("xruns"), int) else None
    return Want(vid=vid if ok else "", position=max(0.0, position),
                paused=bool(state.get("paused") or state.get("buffering")), xruns=xruns,
                on=tv.get("on") is True)


@dataclass
class Director:
    """Runs on the screen's own thread, one `step()` per tick."""

    stream_dir: Path
    sock: str
    size: Callable[[], tuple[int, int] | None]
    guard: Guard = field(default_factory=Guard)
    can: Callable[[], tuple[bool, str]] = capability
    readings: Callable[[], Readings] = lambda: Readings(read_temp(), read_mem_avail(), read_throttled())
    start_session: Callable[..., Any] = Session
    clock: Callable[[], float] = time.monotonic
    emit: Callable[..., None] = lambda kind, **f: None
    status_file: Path | None = STATUS_FILE

    session: Any = None
    mode: str = "screen"  # "video" only while the player really shows a picture
    blocked: str = ""
    _can: tuple[bool, str] = (False, "")
    _can_at: float = float("-inf")
    _sync_at: float = float("-inf")
    _check_at: float = float("-inf")
    _status_sig: tuple = ()
    _status_at: float = float("-inf")
    _refused: str = ""  # a clip we already gave up on (no stream file) — don't retry every tick
    last_drift: float = 0.0

    def capable(self) -> tuple[bool, str]:
        now = self.clock()
        if now - self._can_at >= 30.0:
            self._can_at = now
            try:
                self._can = self.can()
            except Exception as exc:  # a strange /sys must not kill the screen
                self._can = (False, f"nejde zjistit ({type(exc).__name__})")
        return self._can

    def _stream(self, vid: str) -> dict | None:
        try:
            data = json.loads((self.stream_dir / f"{vid}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        ok = isinstance(data, dict) and data.get("id") == vid and str(data.get("url", "")).startswith("https://")
        return data if ok else None

    def _stop(self, why: str) -> None:
        s, self.session = self.session, None
        if s is None:
            return
        was = self.mode
        s.stop()
        self.mode = "screen"
        self.emit("tv.video_stop", video_id=s.vid, reason=why, shown=was == "video",
                  ran_s=int(self.clock() - s.started_at) if hasattr(s, "started_at") else None)

    def step(self, want: Want) -> str:
        """Advance one tick; returns the mode ("video" = the screen must not draw)."""
        now = self.clock()
        can, why_not = self.capable()
        s = self.session
        # 1. stop what should not run any more
        if s is not None:
            if not s.alive():
                self.session = None
                self.mode = "screen"
                code = s.exit_code()
                if want.vid == s.vid and code not in (0, None):
                    # it crashed while it was wanted: no restart loop — back off
                    pause = self.guard.trip("přehrávač videa spadl")
                    self.emit("tv.video_stop", video_id=s.vid, reason="died", code=code,
                              cooldown_s=int(pause))
                else:
                    # the clip ran out (it may be shorter than the song): the
                    # screen takes over for the rest of this track
                    if want.vid == s.vid:
                        self._refused = s.vid
                    self.emit("tv.video_stop", video_id=s.vid, reason="ended")
                s.stop()
                s = None
            elif not can or want.vid != s.vid:
                self._stop("switch_off" if not want.on else "track" if can else "no_display")
                s = None
            elif not s.showing and now - s.started_at > STARTUP_MAX:
                self.guard.trip("obraz se nepodařilo spustit")
                self._stop("no_first_frame")
                s = None
        # 2. look after what runs
        if s is not None:
            s.set_pause(want.paused)
            pos = s.position()
            if s.showing and self.mode != "video":
                self.mode = "video"
                r = self.readings()
                r.xruns, r.dropped = want.xruns, s.dropped()
                self.guard.started(r)
                self.emit("tv.video_start", video_id=s.vid, startup_ms=int((now - s.started_at) * 1000))
            if pos is not None and not want.paused and now - self._sync_at >= SYNC_EVERY:
                self._sync_at = now
                drift = pos - want.position
                action, value = sync_action(drift)
                if action == "seek":
                    if now - s.last_seek >= SEEK_EVERY:
                        s.set_speed(1.0)
                        s.seek(want.position + value)
                        self.emit("tv.video_seek", video_id=s.vid, drift_ms=int(drift * 1000))
                else:
                    s.set_speed(value)
                self.last_drift = drift
            if now - self._check_at >= CHECK_EVERY:
                self._check_at = now
                r = self.readings()
                r.xruns, r.dropped = want.xruns, s.dropped()
                why = self.guard.while_running(r)
                if why:
                    self.emit("tv.video_guard", video_id=s.vid, reason=why, temp_c=r.temp_c,
                              mem_avail_mb=r.mem_avail_mb, throttled=r.throttled, xruns=r.xruns,
                              dropped=r.dropped, cooldown_s=int(self.guard.blocked_until - now))
                    self._stop("guard")
        # 3. start what should run
        elif want.vid and can and want.vid != self._refused:
            r = self.readings()
            ok, why = self.guard.may_start(r)
            if ok:
                stream = self._stream(want.vid)
                if stream is None:
                    self._refused = want.vid  # no picture for this one; the screen stays
                    self.emit("tv.video_skip", video_id=want.vid, reason="no_stream")
                else:
                    try:
                        self.session = self.start_session(
                            want.vid, stream, want.position + START_LEAD, want.paused,
                            self.size(), self.sock)
                        self._sync_at = self._check_at = now
                    except OSError as exc:
                        self.guard.trip("přehrávač videa nejde spustit")
                        self.emit("tv.video_stop", video_id=want.vid, reason=f"spawn:{exc}")
        if not want.vid:
            self._refused = ""
        # what the web's switch says about us
        blocked = ""
        if can and want.on and self.session is None and self.clock() < self.guard.blocked_until:
            blocked = self.guard.reason
        self.blocked = blocked
        self._report(can, why_not)
        return self.mode

    def _report(self, can: bool, why: str) -> None:
        if self.status_file is None:
            return
        now = self.clock()
        sig = (can, why, self.mode, self.blocked)
        if sig == self._status_sig and now - self._status_at < 15.0:
            return
        self._status_sig, self._status_at = sig, now
        try:
            tmp = self.status_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"can": can, "why": why, "mode": self.mode,
                                       "blocked": self.blocked, "at": time.time()},
                                      ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.status_file)
        except OSError:
            self.status_file = None  # no runtime directory (run by hand): never mind

    def close(self) -> None:
        self._stop("shutdown")
