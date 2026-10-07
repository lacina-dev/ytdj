"""Switching the TV off when nobody needs it, and on again — over HDMI-CEC.

A panel that is off does not age: this is the strongest care for a TV that
would otherwise show the jukebox's screen all day. The jukebox does it itself,
but politely — people own the TV, not the jukebox:

  * STANDBY is sent by itself only when the feature is on, nothing has played
    for the configured minutes, the TV says it is on, and the TV is KNOWN to
    show the jukebox. Somebody watching another input is left alone; when we
    do not know what the TV shows, nothing is sent (and the status says so).
  * The TV is woken by itself (IMAGE VIEW ON + ACTIVE SOURCE) only if the
    jukebox was the one that put it to standby and it is still in standby. A
    TV switched off by its remote — or from the jukebox's web page, which is
    a person's decision just the same — stays off when music starts.
  * A person can switch the TV off and on from the web at any time
    (`Wish.command`); switched on that way, it is the jukebox's to manage again.
  * No storms: one attempt and one retry for an action, then nothing for ten
    minutes; the TV is asked for its power state (a question changes nothing)
    every two minutes at most, so that the web can say what it really is.
  * A TV that does not answer (CEC off — "Anynet+" on a Samsung —, no adapter,
    no `cec-ctl`) is simply not managed; the on-screen care goes on and the
    status says why.

How we know what the TV shows. Asking does not work: after we announce
ourselves, WE are the active source, and <Request Active Source> is answered by
the active source itself — nobody answers (first real test, 7 Oct: standby
refused for ever). So we listen instead: a `cec-ctl --wait-for-msgs` child
receives what the TV broadcasts (<Set Stream Path>, <Routing Change>, <Active
Source>, <Standby>; it needs no privilege) and the route is believed to be ours
from our own announcement until the bus says otherwise. If the listener dies,
the belief becomes "unknown" and nothing is sent blindly.

Everything goes through the `cec-ctl` tool (v4l-utils); nothing here talks to
the player or the sound. Decisions are plain functions of what the jukebox
reports, what the TV answers and what the bus said, so they are tested with a
fake `cec-ctl`.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)

DEVICE = "/dev/cec0"
TOOL = "cec-ctl"
OSD_NAME = "Jukebox"
TIMEOUT = 6.0  # s for one cec-ctl call
ACTION_GAP = 60.0  # s — never two automatic actions closer than this
RETRY_AFTER = 8.0  # s — the one retry of a failed action
BACKOFF = 600.0  # s of silence after an action failed twice
PROBE_EVERY = 1800.0  # s — a TV that did not answer is asked again this often
POWER_EVERY = 120.0  # s — how often the TV is asked whether it is on (for the web)
SETTLE = 30.0  # s after our own action before its answer is believed (it takes 8–15 s)
COMMAND_GAP = 10.0  # s between two actions asked for from the web
CONFIRM_FOR = 40.0  # s — how long we wait for the TV to say it did it (it takes 8–15 s)
CONFIRM_EVERY = 4.0  # s between asking while waiting
LISTEN_FOR = 86400  # s per listener child; it is restarted when it ends
_PWR = re.compile(r"pwr-state:\s*([a-z-]+)")
_PHYS = re.compile(r"phys-addr:\s*([0-9a-f.]+)", re.I)
_OWN = re.compile(r"Physical Address\s*:\s*([0-9a-f.]+)", re.I)
_RX = re.compile(r"Received from (.+?) to (.+?) \((\d+) to (\d+)\): ([A-Z_]+) \(0x([0-9a-f]+)\)",
                 re.I)
# Requests the TV sends straight to us that we do not serve. Without a listener
# the kernel refused them (<Feature Abort>); with one it leaves them to us, so we
# refuse them the same way — the TV sees the device it saw before.
UNSERVED = {"GIVE_DECK_STATUS", "MENU_REQUEST", "DECK_CONTROL", "PLAY", "VENDOR_COMMAND",
            "VENDOR_COMMAND_WITH_ID", "GIVE_AUDIO_STATUS", "GIVE_TUNER_DEVICE_STATUS"}
_PA_EVENT = re.compile(r"State Change: PA: ([0-9a-f.]+)", re.I)


@dataclass(frozen=True)
class BusEvent:
    """One thing heard on the CEC bus that matters to us."""

    kind: str  # route | standby | power_query | unserved | address | listener
    addr: str = ""  # route: what the TV now shows; address: our own physical address
    src: int = -1  # logical address of the sender (0 = the TV)
    op: int = 0  # unserved: the opcode we refuse


def parse_listen(lines) -> "list[BusEvent]":
    """`cec-ctl --wait-for-msgs` output → events (the fields come on the lines
    after the message name)."""
    out: list[BusEvent] = []
    pending: tuple[str, int] | None = None
    for raw in lines:
        line = raw.rstrip("\n")
        m = _PA_EVENT.search(line)
        if m:
            out.append(BusEvent("address", m.group(1)))
            pending = None
            continue
        m = _RX.search(line)
        if m:
            src, dst, name = int(m.group(3)), int(m.group(4)), m.group(5)
            pending = None
            if name == "STANDBY":
                out.append(BusEvent("standby", src=src))
            elif name == "GIVE_DEVICE_POWER_STATUS" and dst != 15:
                out.append(BusEvent("power_query", src=src))
            elif name in UNSERVED and dst != 15:
                out.append(BusEvent("unserved", name, src, int(m.group(6), 16)))
            elif name in ("ACTIVE_SOURCE", "SET_STREAM_PATH", "ROUTING_CHANGE"):
                pending = (name, src)
            continue
        if pending is not None:
            key = "new-phys-addr" if pending[0] == "ROUTING_CHANGE" else "phys-addr"
            m = re.search(rf"(?<![a-z-]){key}:\s*([0-9a-f.]+)", line, re.I)
            if m:
                out.append(BusEvent("route", m.group(1), pending[1]))
                pending = None
    return out


class Cec:
    """The few CEC messages we use, each one `cec-ctl` call. None = no answer."""

    def __init__(self, device: str = DEVICE, tool: str = TOOL,
                 run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> None:
        self.device, self.tool, self._run = device, tool, run

    def available(self) -> bool:
        return shutil.which(self.tool) is not None

    def _call(self, *args: str) -> str | None:
        try:
            p = self._run([self.tool, "-d", self.device, "-s", *args], capture_output=True,
                          text=True, timeout=TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            return None
        return (p.stdout or "") + (p.stderr or "") if p.returncode == 0 else None

    def configure(self) -> str | None:
        """Become a playback device named Jukebox; returns our physical address."""
        try:
            p = self._run([self.tool, "-d", self.device, "--playback", "--osd-name", OSD_NAME],
                          capture_output=True, text=True, timeout=TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            return None
        m = _OWN.search(p.stdout or "") if p.returncode == 0 else None
        addr = m.group(1) if m else None
        return addr if addr and addr not in ("f.f.f.f", "0.0.0.0") else None

    def power(self) -> str | None:
        """"on" | "standby" | "to-on" | "to-standby"; None when the TV does not answer."""
        out = self._call("--to", "0", "--give-device-power-status")
        m = _PWR.search(out or "")
        return m.group(1) if m else None

    def active_source(self) -> str | None:
        """Physical address of a device that says it is the source; None when
        nobody answers — which is the normal case once WE are the source."""
        out = self._call("--request-active-source")
        m = _PHYS.search(out or "")
        return m.group(1) if m else None

    def standby(self) -> bool:
        return self._call("--to", "0", "--standby") is not None

    def wake(self, addr: str) -> bool:
        ok = self._call("--to", "0", "--image-view-on") is not None
        return self.claim(addr) and ok

    def claim(self, addr: str) -> bool:
        return self._call("--active-source", f"phys-addr={addr}") is not None

    def answer(self, ev: BusEvent) -> bool:
        """Answer a request sent straight to us (with a listener attached the
        kernel no longer does): our power is "on"; what we do not serve is refused."""
        if ev.src < 0 or ev.src > 14:
            return False
        if ev.kind == "power_query":
            return self._call("--to", str(ev.src), "--report-power-status",
                              "pwr-state=on") is not None
        if ev.kind == "unserved":
            return self._call("--to", str(ev.src), "--feature-abort",
                              f"abort-msg={ev.op},reason=unrecognized-op") is not None
        return False

    def listen_command(self) -> list[str]:
        cmd = [self.tool, "-d", self.device, "-s", "--wait-for-msgs", "--monitor-time",
               str(LISTEN_FOR)]
        # line by line, or the events would sit in the child's buffer
        return ["stdbuf", "-oL", *cmd] if shutil.which("stdbuf") else cmd


class Listener(threading.Thread):
    """Reads the bus through a `cec-ctl --wait-for-msgs` child and queues what
    matters. Cheap (the child sleeps in the kernel), never blocks drawing, and
    is restarted with a growing pause when it dies."""

    def __init__(self, command: Callable[[], list[str]],
                 answer: Callable[[BusEvent], object] | None = None,
                 popen: Callable[..., subprocess.Popen] = subprocess.Popen,
                 pause: float = 5.0, pause_max: float = 300.0) -> None:
        super().__init__(name="tv-cec-listen", daemon=True)
        self.command, self.popen, self.stop = command, popen, threading.Event()
        self.answer = answer
        self._answered: dict[tuple, float] = {}
        self.pause, self.pause_max = pause, pause_max
        self.events: "queue.Queue[BusEvent]" = queue.Queue()
        self.proc: subprocess.Popen | None = None
        self.starts = 0

    def run(self) -> None:
        pause = self.pause
        while not self.stop.is_set():
            t0 = time.monotonic()
            try:
                self.proc = self.popen(self.command(), stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                       text=True)
            except OSError:
                self.proc = None
            if self.proc is not None:
                self.starts += 1
                self.events.put(BusEvent("listener", "up"))
                block: list[str] = []
                try:
                    for line in self.proc.stdout:  # type: ignore[union-attr]
                        # a message is its header line plus indented field lines
                        if not line.startswith(("\t", " ")):
                            block = []
                        block.append(line)
                        found = parse_listen(block)
                        if found:  # at once — the next line may come hours later
                            for ev in found:
                                self._take(ev)
                            block = []
                except (OSError, ValueError):
                    pass
                try:
                    self.proc.wait(timeout=2)
                except Exception:
                    self.close_child()
                try:
                    self.proc.stdout.close()  # type: ignore[union-attr]
                except (OSError, ValueError, AttributeError):
                    pass
            # whatever happened on the bus meanwhile was not heard
            self.events.put(BusEvent("listener", "down"))
            pause = self.pause if time.monotonic() - t0 > 60 else min(self.pause_max, pause * 2)
            self.stop.wait(pause)

    def _take(self, ev: BusEvent) -> None:
        if ev.kind in ("power_query", "unserved"):
            # answered here and now (the asker waits a second, the screen's loop
            # comes round in half a minute) — the same question at most every 2 s
            key, now = (ev.kind, ev.src, ev.op), time.monotonic()
            if self.answer is not None and now - self._answered.get(key, -1e9) >= 2.0:
                self._answered[key] = now
                try:
                    self.answer(ev)
                except Exception:
                    log.debug("odpověď na CEC selhala", exc_info=True)
            return
        self.events.put(ev)

    def shutdown(self) -> None:
        self.stop.set()
        self.close_child()

    def close_child(self) -> None:
        p = self.proc
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass

    def drain(self) -> list[BusEvent]:
        out = []
        while True:
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                return out


@dataclass
class Wish:
    """What the jukebox's status asks of the TV right now."""

    enabled: bool = False  # the setting „Telku vypínat, když se nehraje“
    minutes: int = 10
    active: bool = False  # music plays, or somebody just asked for some (wish, Play)
    idle_s: float = 0.0  # for how long nothing has played
    # a person pressed the button on the web: (request id, "on" | "off")
    command: tuple[int, str] | None = None


def wish_from(state: dict | None, idle_s: float) -> Wish:
    if not isinstance(state, dict):
        return Wish(idle_s=idle_s)
    tv = state.get("tv") if isinstance(state.get("tv"), dict) else {}
    sb = tv.get("standby") if isinstance(tv.get("standby"), dict) else {}
    dj = state.get("dj") if isinstance(state.get("dj"), dict) else {}
    playing = isinstance(state.get("current"), dict) and not state.get("paused")
    asked = bool(state.get("starting") or dj.get("busy"))
    try:
        minutes = max(1, min(240, int(sb.get("minutes") or 10)))
    except (TypeError, ValueError):
        minutes = 10
    req = tv.get("power_request") if isinstance(tv.get("power_request"), dict) else {}
    command = None
    if isinstance(req.get("id"), int) and req.get("action") in ("on", "off"):
        command = (req["id"], req["action"])
    return Wish(enabled=sb.get("on") is True, minutes=minutes, active=playing or asked,
                idle_s=0.0 if playing else idle_s, command=command)


class TvPower:
    """Decides when to send what. One `step()` per tick of the screen's loop;
    it talks to the TV only when a decision needs it."""

    def __init__(self, cec: Cec, clock: Callable[[], float] = time.monotonic,
                 emit: Callable[..., None] = lambda kind, **f: None,
                 state_file: str | os.PathLike | None = None,
                 listener: Listener | None = None) -> None:
        self.cec, self.clock, self.emit = cec, clock, emit
        # "the TV sleeps because of us / because a person said so" must survive a
        # restart of this process (a deploy while the TV is off)
        self.state_file = state_file
        self.listener = listener
        self.addr: str | None = None  # our physical address once the adapter is set up
        self.state = "unknown"  # on | standby | no_answer | no_cec | off (not managed) | unknown
        self.tv = ""  # the TV's power as last learnt: "on" | "standby" | "" (not known)
        # what the TV shows, as far as we know: "ours" | "other" | "unknown"
        self.route = "unknown"
        self.route_addr = ""  # the other input's address when route == "other"
        self.we_slept = False  # the TV is in standby because WE sent it there by ourselves
        self.people_off = False  # a person switched it off (from the web): never wake it by music
        self.note = ""  # for the web: what is going on, in Czech
        # the button on the web: which request we are doing / did, and how it went
        self.done_id = 0
        self.done: dict = {}
        self._confirm: dict | None = None
        self._command_at = float("-inf")
        self._last_action = float("-inf")
        self._retry: tuple[str, float] | None = None  # (action, when) — the one retry
        self._quiet_until = float("-inf")
        self._probe_at = float("-inf")
        self._was_active = False
        self._refused = ""  # why the last standby was not sent (logged once)
        self._power_at = float("-inf")  # when the TV is asked for its power state next
        self._misses = 0
        self._claim_due = False  # music started and we do not know what the TV shows
        self._look_at = float("-inf")  # a refused standby is thought about again then
        self._load()

    @property
    def pending(self) -> bool:
        """A web command is being carried out — the loop should come back soon."""
        return self._confirm is not None

    def _load(self) -> None:
        if self.state_file is None:
            return
        try:
            with open(self.state_file, encoding="utf-8") as fh:
                data = json.load(fh)
            self.we_slept = data.get("we_slept") is True
            self.people_off = data.get("people_off") is True
            self.done_id = int(data.get("done_id") or 0)
        except (OSError, ValueError, AttributeError, TypeError):
            pass

    def _save(self) -> None:
        if self.state_file is None:
            return
        try:
            tmp = f"{self.state_file}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"we_slept": self.we_slept, "people_off": self.people_off,
                           "done_id": self.done_id}, fh)
            os.replace(tmp, self.state_file)
        except OSError:
            log.debug("stav vypínání telky se nepodařilo uložit", exc_info=True)

    # ---- talking to the TV ----

    def _setup(self) -> bool:
        now = self.clock()
        if self.addr is not None:
            return True
        if now < self._probe_at:
            return False
        self._probe_at = now + PROBE_EVERY
        if not self.cec.available():
            self._set("no_cec", "Telku ovládat nejde: na jukeboxu chybí nástroj pro HDMI-CEC.")
            return False
        self.addr = self.cec.configure()
        if self.addr is None:
            self._set("no_cec", "Telku ovládat nejde: jukebox na HDMI nevidí telku s CEC.")
            return False
        if self.listener is not None and not self.listener.is_alive():
            try:
                self.listener.start()
            except RuntimeError:
                pass
        return True

    def _set(self, state: str, note: str) -> None:
        if (state, note) != (self.state, self.note):
            self.state, self.note = state, note
            self.emit("tv.cec_state", state=state, note=note[:120] or None, route=self.route,
                      tv=self.tv or None)

    def _believe(self, route: str, addr: str = "", why: str = "") -> None:
        if (route, addr) != (self.route, self.route_addr):
            self.route, self.route_addr = route, addr
            self.emit("tv.cec_route", route=route, addr=addr or None, why=why or None)

    def _act(self, action: str, send: Callable[[], bool], **fields) -> bool:
        """One attempt now; on failure one retry later, then a long pause."""
        now = self.clock()
        ok = send()
        self._last_action = now
        self.emit("tv.cec", action=action, ok=ok, retry=bool(self._retry) or None, **fields)
        if ok:
            self._retry = None
            return True
        if self._retry is None:
            self._retry = (action, now + RETRY_AFTER)
        else:
            self._retry = None
            self._quiet_until = now + BACKOFF
        return False

    def _may_act(self) -> bool:
        now = self.clock()
        if now < self._quiet_until:
            return False
        if self._retry is not None:
            return now >= self._retry[1]
        return now - self._last_action >= ACTION_GAP

    # ---- what the bus said ----

    def hear(self, events: "list[BusEvent]") -> None:
        for ev in events:
            if ev.kind == "listener":
                if ev.addr == "down" and self.route != "unknown":
                    # from here on we may have missed an input change
                    self._believe("unknown", why="listener_down")
            elif ev.kind == "address":
                if ev.addr in ("f.f.f.f", "0.0.0.0", ""):
                    self.addr = None  # HDMI unplugged / TV gone
                    self._probe_at = self.clock() + 60.0
                    self._believe("unknown", why="no_address")
                elif self.addr is None:
                    self._probe_at = float("-inf")  # it is back: set the adapter up again
                elif ev.addr != self.addr:
                    self.addr = ev.addr
                    self._believe("unknown", why="address_changed")
            elif ev.kind == "route":
                ours = self.addr is not None and ev.addr == self.addr
                self._believe("ours" if ours else "other", "" if ours else ev.addr,
                              why="bus")
                # a route is being set: the TV is on — switched on by somebody
                # (remote), so it is theirs, not "asleep because of us" any more
                self.tv = "on"
                if (self.we_slept or self.people_off) and self._confirm is None:
                    self.we_slept = self.people_off = False
                    self._save()
                    self.emit("tv.cec", action="seen_on", ok=True)
            elif ev.kind == "standby" and ev.src == 0:
                self.tv = "standby"
                if not self.we_slept and self._confirm is None and not self.people_off:
                    # by the remote: a person's decision, like the button on the web
                    self.people_off = True
                    self._save()
                    self.emit("tv.cec", action="seen_off", ok=True, by="bus")
                    # what it shows when somebody switches it on again is not ours to assume
                    self._believe("unknown", why="switched_off_by_people")

    # ---- the button on the web ----

    def _command(self, wish: Wish) -> None:
        now = self.clock()
        if self._confirm is not None:
            c = self._confirm
            if now < c["next"]:
                return
            power = self.cec.power()
            want = "standby" if c["action"] == "off" else "on"
            if power == want:
                self._finish(c, True, "Telka je vypnutá." if want == "standby" else
                             "Telka je zapnutá a ukazuje jukebox.")
            elif now >= c["until"]:
                self._finish(c, False, "Telka povel nepotvrdila"
                             + (" — na HDMI-CEC neodpovídá (zapni na ní Anynet+)."
                                if power is None else "."))
            else:
                c["next"] = now + CONFIRM_EVERY
            return
        if wish.command is None or wish.command[0] <= self.done_id:
            return
        if now - self._command_at < COMMAND_GAP:
            return  # it stays in the status and is done when the gap is over
        rid, action = wish.command
        self._command_at = now
        self._probe_at = float("-inf")  # a person asked: look for the TV now
        self._retry, self._quiet_until = None, float("-inf")  # and no back-off
        if not self._setup():
            self.done_id = rid
            self.done = {"id": rid, "action": action, "ok": False,
                         "note": self.note or "Telku ovládat nejde."}
            self.emit("tv.cec", action=f"web_{action}", ok=False, request=rid, skipped=self.state)
            self._save()
            return
        was_other = self.route_addr if self.route == "other" else ""
        ok = self.cec.standby() if action == "off" else self.cec.wake(self.addr)
        self._last_action = now
        self._power_at = now + SETTLE
        self.emit("tv.cec", action=f"web_{action}", ok=ok, request=rid,
                  other_input=was_other or None)
        self._confirm = {"id": rid, "action": action, "until": now + CONFIRM_FOR,
                         "next": now + CONFIRM_EVERY, "other": was_other}
        self._set(self.state, "Posílám telce povel…")

    def _finish(self, c: dict, ok: bool, note: str) -> None:
        self._confirm = None
        self.done_id = c["id"]
        if ok and c["action"] == "off":
            # a person switched it off: like the remote — music will not wake it
            self.people_off, self.we_slept, self.tv = True, False, "standby"
            if c["other"]:
                note += f" (Ukazovala jiný vstup, {c['other']}.)"
            self._set("standby", "Telka je vypnutá (vypnuto z webu) — zapne ji jen tlačítko "
                                 "nebo ovladač.")
        elif ok:
            # switched on from the web: ours to manage again
            self.people_off = self.we_slept = False
            self.tv = "on"
            self._believe("ours", why="web_on")
            self._set("on", "")
        else:
            self._set(self.state, "")
        self.done = {"id": c["id"], "action": c["action"], "ok": ok, "note": note}
        self._power_at = self.clock() + (POWER_EVERY if ok else 0.0)
        self.emit("tv.cec", action=f"web_{c['action']}_result", ok=ok, request=c["id"],
                  other_input=c["other"] or None)
        self._save()

    # ---- the decisions ----

    def _poll(self) -> None:
        """Now and then: is the TV on? (A question; it changes nothing.)"""
        now = self.clock()
        if now < self._power_at or not self._setup():
            return
        self._power_at = now + POWER_EVERY
        power = self.cec.power()
        if power is None:
            self._misses += 1
            if self._misses >= 2:  # one missed answer is not "CEC is off"
                self._no_answer()
            else:
                self._power_at = now + 20.0
            return
        self._misses = 0
        self._learn(power)

    def _learn(self, power: str) -> None:
        new = "on" if power in ("on", "to-on") else "standby"
        if new == "on" and (self.we_slept or self.people_off):
            # somebody switched it on (remote): theirs — and ours to manage again
            self.we_slept = self.people_off = False
            self._save()
            self.emit("tv.cec", action="seen_on", ok=True, by="power")
            if self.tv != "on":  # the bus did not tell us what it shows now
                self._believe("unknown", why="switched_on_by_people")
        elif new == "standby" and not self.we_slept and not self.people_off:
            self.people_off = True  # off, and not by us: only a person switches it on
            self._save()
            self.emit("tv.cec", action="seen_off", ok=True, by="power")
            self._believe("unknown", why="switched_off_by_people")
        self.tv = new

    def report(self) -> dict:
        """For the status file → the jukebox → the web (flat, plain values)."""
        tv = self.tv or ("none" if self.state in ("no_cec", "no_answer") else "")
        return {"power": self.state, "power_note": self.note, "power_tv": tv,
                "power_by": "people" if self.people_off else "jukebox" if self.we_slept else "",
                "power_route": self.route,
                "power_busy": self._confirm["id"] if self._confirm else 0,
                "power_done_id": self.done.get("id", 0),
                "power_done_action": self.done.get("action", ""),
                "power_done_ok": bool(self.done.get("ok")),
                "power_done_note": self.done.get("note", "")}

    def step(self, wish: Wish) -> str:
        """Returns the note for the status ("" = nothing to say)."""
        if self.listener is not None:
            self.hear(self.listener.drain())
        if wish.active and not self._was_active:
            self._claim_due = True
        elif not wish.active:
            self._claim_due = False
        self._was_active = wish.active
        self._command(wish)
        if self._confirm is not None:
            return self.note
        self._poll()
        if not wish.enabled and not (self.we_slept and wish.active):
            # switched off in the settings: the TV is never switched by ourselves —
            # but a TV that WE put to sleep is not left dark when music starts again
            if self.state not in ("no_cec", "no_answer"):
                self._set("off", "")
            return self.note
        if not self._setup() or not self._may_act():
            return self.note
        if wish.active:
            if self.people_off:
                return self.note  # a person switched it off: only a person switches it on
            if self.we_slept:
                power = self.cec.power()
                if power is None:
                    self._no_answer()
                elif power in ("standby", "to-standby"):
                    if self._act("wake", lambda: self.cec.wake(self.addr)):
                        self.we_slept, self.tv = False, "on"
                        self._power_at = self.clock() + SETTLE
                        self._believe("ours", why="wake")
                        self._save()
                        self._set("on", "Telka je zapnutá (hraje se).")
                else:
                    self._learn(power)  # somebody switched it on meanwhile: theirs
                    self._set("on", "")
            elif self._claim_due and self.route == "unknown" and self.tv == "on":
                # music started and we do not know what the TV shows: say once that
                # the jukebox is here — unless another device says it is being watched
                self._claim_due = False
                src = self.cec.active_source()
                if src not in (None, "0.0.0.0", self.addr):
                    self._believe("other", src, why="asked")
                    self.emit("tv.cec", action="claim", ok=False, skipped="other_input", addr=src)
                elif self._act("claim", lambda: self.cec.claim(self.addr)):
                    self._believe("ours", why="claim")
                self._set("on", "")
            elif self.state in ("off", "unknown") and self.tv:
                self._set(self.tv, "")
            return self.note
        if self.we_slept or self.people_off or wish.idle_s < wish.minutes * 60 \
                or self.clock() < self._look_at:
            return self.note
        # nothing has played long enough: may the TV go to sleep?
        if self.route == "other":
            self._refuse("other_input", "Telka zůstává zapnutá: někdo se na ní dívá na jiný vstup.",
                         addr=self.route_addr or None)
            return self.note
        if self.route != "ours":
            # honest: we simply do not know — nothing is sent blindly
            self._refuse("route_unknown", "Telku nevypínám: nevím, co zrovna ukazuje. "
                                          "Zjistím to, až začne hrát hudba.")
            return self.note
        power = self.cec.power()
        if power is None:
            self._no_answer()
            return self.note
        if power != "on":
            self._learn(power)
            self._refuse("tv_not_on", "")
            return self.note
        if self._act("standby", self.cec.standby, idle_min=int(wish.idle_s // 60)):
            self.we_slept, self._refused, self.tv = True, "", "standby"
            self._power_at = self.clock() + SETTLE
            self._save()
            self._set("standby", f"Telku jsem vypnul — {wish.minutes} min se nehrálo. "
                                 "Zapne se sama, až začne hrát hudba.")
        return self.note

    def _no_answer(self) -> None:
        self.addr = None  # set the adapter up again at the next probe
        self.tv = ""
        self._probe_at = self.clock() + PROBE_EVERY
        self._set("no_answer", "Telka na HDMI-CEC neodpovídá (na telce Samsung zapni Anynet+) — "
                               "šetří se jen úpravou obrazu.")

    def _refuse(self, why: str, note: str, **fields) -> None:
        # look again in five minutes, not sooner (music starting meanwhile is not held up)
        self._look_at = self.clock() + 300.0
        if why != self._refused:
            self._refused = why
            self.emit("tv.cec", action="standby", ok=False, skipped=why, **fields)
        if note:
            self._set("on", note)
