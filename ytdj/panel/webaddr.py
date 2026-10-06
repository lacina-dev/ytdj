"""Which web address to show people — decided by what actually answers.

The TV screen and the touch panel print the jukebox's address and a QR code.
An address that doesn't work is worse than none (the owner's complaint was a
screen advertising a name that didn't), so nothing is assumed: from time to
time a background thread asks the jukebox's own web under each candidate and
the screens show the best one that answered —

    jukebox.local  →  <hostname>.local  →  the IP address

and each of them without a port only when port 80 really is served here
(packaging/ytdj-port80.service), otherwise with the web's own port.

The check runs on the jukebox itself, so it proves that the name resolves
here and that this machine answers on that port — not that every phone on the
network can resolve `.local` names. That is why the panel's QR code keeps
using the IP address (it works everywhere) and only drops the port.

Stdlib only; nothing here runs on a UI loop.
"""

from __future__ import annotations

import http.client
import json
import logging
import re
import socket
import threading
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)

ALIAS = "jukebox.local"
# a name from the office's own DNS (config.toml: web_name = "jukebox.firma.cz") —
# a plain host name, nothing else gets on a screen or into a QR code
_HOST = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
                   r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")


def clean_name(value: object) -> str:
    """`web_name` from config.toml as a host name, or "" when it isn't one."""
    if not isinstance(value, str):
        return ""
    name = value.strip().lower().rstrip(".")
    return name if _HOST.match(name) and not name.replace(".", "").isdigit() else ""
TIMEOUT = 3.0
# after a start things are still coming up (avahi, the redirect, ytdj itself):
# look again soon, then settle to once in a few minutes
SCHEDULE = (0.0, 15.0, 45.0, 120.0)
EVERY = 300.0


@dataclass(frozen=True)
class Reach:
    """What was found to work at the last look."""

    port: int = 8765  # the web's own port
    port80: bool = False  # the web answers on port 80 on this machine
    names: tuple[str, ...] = ()  # names that resolve here and answer, best first
    ip: str = ""  # this machine's address on the network

    def _suffix(self) -> str:
        return "" if self.port80 or self.port == 80 else f":{self.port}"

    def host(self) -> str:
        """The best thing to type: a name that works, else the address."""
        return (self.names[0] if self.names else self.ip) or ""

    def address(self) -> str:
        """As shown on a screen, e.g. "jukebox.local" or "192.168.0.24:8765" ("" = unknown)."""
        host = self.host()
        return host + self._suffix() if host else ""

    def ip_address(self) -> str:
        """The form that needs no name lookup at all, e.g. "192.168.0.24" ("" = no network).
        This is what QR codes carry: `.local` names depend on multicast, which an
        office Wi-Fi may drop (measured 6 Oct: 1–3 answers in 10 reached a laptop)."""
        return self.ip + self._suffix() if self.ip else ""

    def url(self, host: str | None = None) -> str:
        """http://… for `host` (default: the best one) with the port only if needed."""
        host = self.host() if host is None else host
        return f"http://{host}{self._suffix()}" if host else ""


def own_ip() -> str:
    """The address this machine uses towards the network (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


def resolves(name: str) -> bool:
    try:
        return bool(socket.getaddrinfo(name, None, socket.AF_INET, socket.SOCK_STREAM))
    except (OSError, UnicodeError):
        return False


def answers(host: str, port: int, timeout: float = TIMEOUT) -> bool:
    """Is it the jukebox's web that answers at host:port? (GET /api/status)"""
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", "/api/status", headers={"User-Agent": "ytdj-addr"})
        resp = conn.getresponse()
        raw = resp.read(256 * 1024)
        if resp.status != 200:
            return False
        data = json.loads(raw)
        # some other web server on port 80 is not us
        return isinstance(data, dict) and "queue" in data and "dj" in data
    except (OSError, http.client.HTTPException, ValueError):
        return False
    finally:
        conn.close()


def probe(port: int, hostname: str = "", alias: str = ALIAS, name: str = "",
          answers: Callable[[str, int], bool] = answers,
          resolves: Callable[[str], bool] = resolves,
          own_ip: Callable[[], str] = own_ip) -> Reach | None:
    """One look at what works; None while the web itself doesn't answer (nothing
    can be confirmed then). May take a few seconds (name lookups) — call it in a thread."""
    ip = own_ip()
    # through the machine's own address, like a visitor would; without a
    # network at least the loopback says whether port 80 is ours
    port80 = port == 80 or answers(ip or "127.0.0.1", 80)
    use = 80 if port80 else port
    if not port80 and not answers("127.0.0.1", port):
        # the web itself is not up: nothing can be confirmed right now
        return None
    names = []
    # the office's own DNS name first (when one is configured), then ours
    for cand in (name, alias, f"{hostname}.local" if hostname else ""):
        if cand and cand not in names and resolves(cand) and answers(cand, use):
            names.append(cand)
    return Reach(port=port, port80=port80, names=tuple(names), ip=ip)


class Watch(threading.Thread):
    """Keeps `reach` current; tells `on_change` when the shown address changed."""

    def __init__(self, port: int, stop: threading.Event,
                 on_change: Callable[[Reach], None] | None = None, hostname: str | None = None,
                 look: Callable[..., Reach | None] = probe, schedule: tuple[float, ...] = SCHEDULE,
                 every: float = EVERY, name: str = "") -> None:
        super().__init__(name="web-address", daemon=True)
        self.port = port
        self.stop = stop
        self.on_change = on_change
        self.hostname = socket.gethostname() if hostname is None else hostname
        self.look = look
        self.web_name = clean_name(name)  # web_name from config.toml ("" = none)
        self.schedule = schedule
        self.every = every
        # until the first look: the bare address with the port always works
        self.reach = Reach(port=port, ip=own_ip())
        self.looks = 0

    def once(self) -> Reach:
        try:
            new = self.look(self.port, self.hostname, name=self.web_name) if self.web_name \
                else self.look(self.port, self.hostname)
        except Exception:  # never let the thread die
            log.debug("zjišťování adresy webu selhalo", exc_info=True)
            return self.reach
        self.looks += 1
        if new is None or (not new.address() and self.reach.address()):
            # the web or the network is down for a moment: keep what worked
            # (the screens say "not answering" themselves; flipping the
            # address back and forth around a restart helps nobody)
            return self.reach
        if new != self.reach:
            log.info("adresa webu: %s", new.address() or "?")
            self.reach = new
            if self.on_change is not None:
                try:
                    self.on_change(new)
                except Exception:
                    log.debug("on_change selhal", exc_info=True)
        return self.reach

    def run(self) -> None:
        last = 0.0
        for at in self.schedule:
            if self.stop.wait(max(0.0, at - last)):
                return
            last = at
            self.once()
        while not self.stop.wait(self.every):
            self.once()
