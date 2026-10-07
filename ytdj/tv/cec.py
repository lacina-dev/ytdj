"""Switching the TV off when nobody needs it, and on again — over HDMI-CEC.

A panel that is off does not age: this is the strongest care for a TV that
would otherwise show the jukebox's screen all day. The jukebox does it itself,
but politely — people own the TV, not the jukebox:

  * STANDBY is sent only when the feature is on, nothing has played for the
    configured minutes, the TV says it is on, and the TV's active source is
    the jukebox (somebody watching another input is left alone). The jukebox
    counts as the active source only after it has said so itself once, when
    music started and no other device was shown.
  * The TV is woken (IMAGE VIEW ON + ACTIVE SOURCE) only if the jukebox was the
    one that put it to standby and it is still in standby. A TV switched off
    by its remote stays off; a TV somebody switched on meanwhile is left alone.
  * No storms: one attempt and one retry for an action, then nothing for ten
    minutes; the TV is asked for its state only when a decision needs it.
  * A TV that does not answer (CEC off — "Anynet+" on a Samsung —, no adapter,
    no `cec-ctl`) is simply not managed; the on-screen care goes on and the
    status says why.

Everything goes through the `cec-ctl` tool (v4l-utils); nothing here talks to
the player or the sound. Decisions are plain functions of what the jukebox
reports and what the TV answers, so they are tested with a fake `cec-ctl`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)

DEVICE = "/dev/cec0"
TOOL = "cec-ctl"
OSD_NAME = "Jukebox"
TIMEOUT = 6.0  # s for one cec-ctl call
ACTION_GAP = 60.0  # s — never two actions (standby / wake / claim) closer than this
RETRY_AFTER = 8.0  # s — the one retry of a failed action
BACKOFF = 600.0  # s of silence after an action failed twice
PROBE_EVERY = 1800.0  # s — a TV that did not answer is asked again this often
_PWR = re.compile(r"pwr-state:\s*([a-z-]+)")
_PHYS = re.compile(r"phys-addr:\s*([0-9a-f.]+)", re.I)
_OWN = re.compile(r"Physical Address\s*:\s*([0-9a-f.]+)", re.I)


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
        """Physical address of what the TV shows ("0.0.0.0" = the TV itself); None = nobody said."""
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


@dataclass
class Wish:
    """What the jukebox's status asks of the TV right now."""

    enabled: bool = False  # the setting „Telku vypínat, když se nehraje“
    minutes: int = 10
    active: bool = False  # music plays, or somebody just asked for some (wish, Play)
    idle_s: float = 0.0  # for how long nothing has played


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
    return Wish(enabled=sb.get("on") is True, minutes=minutes, active=playing or asked,
                idle_s=0.0 if playing else idle_s)


class TvPower:
    """Decides when to send what. One `step()` per tick of the screen's loop;
    it talks to the TV only when a decision needs it."""

    def __init__(self, cec: Cec, clock: Callable[[], float] = time.monotonic,
                 emit: Callable[..., None] = lambda kind, **f: None,
                 state_file: str | os.PathLike | None = None) -> None:
        self.cec, self.clock, self.emit = cec, clock, emit
        # "the TV sleeps because of us" must survive a restart of this process
        # (a deploy while the TV is off) — otherwise nobody would wake it
        self.state_file = state_file
        self.addr: str | None = None  # our physical address once the adapter is set up
        self.state = "unknown"  # what we last learnt: on | standby | no_answer | no_cec | unknown
        self.we_slept = False  # the TV is in standby because WE sent it there
        self.claimed = False  # we told the TV we are its source (so "active source" means us)
        self.note = ""  # for the web: what is going on, in Czech
        self._last_action = float("-inf")
        self._retry: tuple[str, float] | None = None  # (action, when) — the one retry
        self._quiet_until = float("-inf")
        self._probe_at = float("-inf")
        self._was_active = False
        self._refused = ""  # why the last standby was not sent (logged once)
        self._load()

    def _load(self) -> None:
        if self.state_file is None:
            return
        try:
            with open(self.state_file, encoding="utf-8") as fh:
                data = json.load(fh)
            self.we_slept = data.get("we_slept") is True
            self.claimed = data.get("claimed") is True
        except (OSError, ValueError, AttributeError):
            pass

    def _save(self) -> None:
        if self.state_file is None:
            return
        try:
            tmp = f"{self.state_file}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"we_slept": self.we_slept, "claimed": self.claimed}, fh)
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
            self._set("no_cec", "Telku vypínat nejde: na jukeboxu chybí nástroj pro HDMI-CEC.")
            return False
        self.addr = self.cec.configure()
        if self.addr is None:
            self._set("no_cec", "Telku vypínat nejde: jukebox na HDMI nevidí telku s CEC.")
            return False
        return True

    def _set(self, state: str, note: str) -> None:
        if (state, note) != (self.state, self.note):
            self.state, self.note = state, note
            self.emit("tv.cec_state", state=state, note=note[:120] or None)

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

    # ---- the decisions ----

    def step(self, wish: Wish) -> str:
        """Returns the note for the status ("" = nothing to say)."""
        started = wish.active and not self._was_active
        self._was_active = wish.active
        if not wish.enabled:
            # switched off in the settings: never touch the TV — but a TV that
            # WE put to sleep is not left dark when music starts again
            if not (self.we_slept and wish.active):
                self._set("off", "")
                return self.note
        if not self._setup() or not self._may_act():
            return self.note
        if wish.active:
            if self.we_slept:
                power = self.cec.power()
                if power is None:
                    self._no_answer()
                elif power in ("standby", "to-standby"):
                    if self._act("wake", lambda: self.cec.wake(self.addr)):
                        self.we_slept, self.claimed = False, True
                        self._save()
                        self._set("on", "Telka je zapnutá (hraje se).")
                else:
                    # somebody switched it on meanwhile: theirs, we don't touch the input
                    self.we_slept = False
                    self._save()
                    self._set("on", "")
            elif started and wish.enabled and not self.claimed:
                # music started and the TV is on: say once that the jukebox is what
                # it shows — unless another device is being watched
                if self.cec.power() == "on":
                    src = self.cec.active_source()
                    if src in (None, "0.0.0.0", self.addr):
                        if self._act("claim", lambda: self.cec.claim(self.addr), source=src):
                            self.claimed = True
                            self._save()
                    self._set("on", "")
            return self.note
        if self.we_slept or wish.idle_s < wish.minutes * 60:
            return self.note
        # nothing has played long enough: may the TV go to sleep?
        power = self.cec.power()
        if power is None:
            self._no_answer()
            return self.note
        if power != "on":
            self._refuse("tv_not_on", "")
            return self.note
        src = self.cec.active_source()
        if not self.claimed or src != self.addr:
            # somebody watches something else (or we never were what it shows)
            self._refuse("not_our_picture", "Telka zůstává zapnutá: neukazuje jukebox.", source=src)
            return self.note
        if self._act("standby", self.cec.standby, idle_min=int(wish.idle_s // 60)):
            self.we_slept, self._refused = True, ""
            self._save()
            self._set("standby", f"Telku jsem vypnul — {wish.minutes} min se nehrálo. "
                                 "Zapne se sama, až začne hrát hudba.")
        return self.note

    def _no_answer(self) -> None:
        self.addr = None  # set the adapter up again at the next probe
        self._probe_at = self.clock() + PROBE_EVERY
        self._set("no_answer", "Telka na HDMI-CEC neodpovídá (na telce Samsung zapni Anynet+) — "
                               "šetří se jen úpravou obrazu.")

    def _refuse(self, why: str, note: str, **fields) -> None:
        self._quiet_until = self.clock() + 300.0  # ask the TV again in five minutes, not sooner
        if why != self._refused:
            self._refused = why
            self.emit("tv.cec", action="standby", ok=False, skipped=why, **fields)
        if note:
            self._set("on", note)
