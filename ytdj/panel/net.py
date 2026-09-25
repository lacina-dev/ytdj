"""Network state and Wi-Fi login through NetworkManager (`nmcli`).

The panel only ever talks to `NetBackend` — three blocking calls that the app
runs off the main thread. `NmcliBackend` is the real thing; tests pass a fake.

Passwords go to nmcli as an argument (the service runs as root, so nothing
needs to prompt) and are never logged: errors are built from nmcli's stderr,
not from the command line, and scrubbed of the password just in case.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

NMCLI = "nmcli"
WIFI_IFACE = "wlan0"
STATUS_TIMEOUT = 8.0
SCAN_TIMEOUT = 20.0
CONNECT_WAIT = 45  # s — nmcli -w; a Pi 3 with a slow DHCP server needs a while
AVAHI_SOCKET = Path("/run/avahi-daemon/socket")


class NetError(Exception):
    """Something went wrong; the message is meant for the screen."""


@dataclass(frozen=True)
class Link:
    iface: str
    kind: str  # "ethernet" | "wifi"
    state: str  # "connected" | "connecting" | "disconnected" | "unavailable" | …
    ip4: str = ""  # without the prefix length
    ssid: str = ""  # wifi only
    signal: int = -1  # wifi only, 0..100

    @property
    def up(self) -> bool:
        return self.state == "connected" and bool(self.ip4)


@dataclass(frozen=True)
class NetStatus:
    hostname: str = ""
    mdns: bool = False  # avahi runs, so <hostname>.local resolves on the LAN
    links: tuple[Link, ...] = ()
    error: str = ""  # NetworkManager unreachable etc.

    def link(self, kind: str) -> Link | None:
        # the connected one wins if there are several (USB dongle next to eth0)
        found = [l for l in self.links if l.kind == kind]
        found.sort(key=lambda l: (not l.up, l.state != "connected"))
        return found[0] if found else None

    @property
    def online(self) -> bool:
        return any(l.up for l in self.links)


@dataclass(frozen=True)
class WifiNet:
    ssid: str
    signal: int  # 0..100
    security: str = ""  # "WPA2", "WPA1 WPA2", "" = open
    in_use: bool = False

    @property
    def secure(self) -> bool:
        return bool(self.security.strip()) and self.security.strip() != "--"


@dataclass(frozen=True)
class Connected:
    ssid: str
    ip4: str = ""


class NetBackend(Protocol):
    def status(self) -> NetStatus: ...

    def scan(self, rescan: bool = False) -> list[WifiNet]: ...

    def connect(self, ssid: str, password: str | None) -> Connected:
        """Blocks until connected (→ Connected) or failed (→ raises NetError)."""


# ---- nmcli terse output ----


def split_terse(line: str) -> list[str]:
    """One `nmcli -t` line → fields. `\\:` is a literal colon, `\\\\` a backslash."""
    out: list[str] = []
    cur: list[str] = []
    i = 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if c == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur))
    return out


def _state_word(raw: str) -> str:
    """'100 (connected)' → 'connected'; '70 (connecting (getting IP configuration))' → 'connecting'."""
    raw = raw.strip()
    if "(" in raw:
        raw = raw[raw.index("(") + 1 :]
    word = raw.split()[0] if raw.split() else ""
    return word.strip("()").lower()


def parse_dev_show(text: str) -> list[dict[str, str]]:
    """`nmcli -t -f GENERAL.DEVICE,… dev show` → one dict per device."""
    devices: list[dict[str, str]] = []
    cur: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            if cur:
                devices.append(cur)
                cur = {}
            continue
        parts = split_terse(line)
        key, value = parts[0], ":".join(parts[1:])
        if key.startswith("GENERAL.DEVICE") and cur:  # no blank line between blocks
            devices.append(cur)
            cur = {}
        base = key.split("[", 1)[0]
        if base not in cur:  # IP4.ADDRESS[1] beats [2]
            cur[base] = value
    if cur:
        devices.append(cur)
    return devices


def parse_links(text: str) -> list[Link]:
    links = []
    for dev in parse_dev_show(text):
        kind = dev.get("GENERAL.TYPE", "")
        if kind not in ("ethernet", "wifi"):
            continue
        ip = dev.get("IP4.ADDRESS", "").split("/", 1)[0].strip()
        links.append(Link(dev.get("GENERAL.DEVICE", ""), kind, _state_word(dev.get("GENERAL.STATE", "")), ip))
    return links


def parse_wifi_list(text: str) -> list[WifiNet]:
    """`nmcli -t -f IN-USE,SSID,SIGNAL,SECURITY dev wifi list` → networks.

    One entry per SSID (the strongest access point, or the one in use),
    hidden networks left out, the connected one first, then by signal.
    """
    best: dict[str, WifiNet] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        f = split_terse(line)
        if len(f) < 4:
            continue
        in_use, ssid, sig, sec = f[0].strip() == "*", f[1], f[2], f[3]
        if not ssid.strip():
            continue
        try:
            signal = max(0, min(100, int(sig)))
        except ValueError:
            signal = 0
        net = WifiNet(ssid, signal, "" if sec.strip() == "--" else sec.strip(), in_use)
        old = best.get(ssid)
        if old is None:
            best[ssid] = net
        else:
            best[ssid] = WifiNet(
                ssid, max(old.signal, signal), old.security or net.security, old.in_use or in_use
            )
    return sorted(best.values(), key=lambda n: (not n.in_use, -n.signal, n.ssid.lower()))


def friendly_error(stderr: str, password: str | None = None) -> str:
    """nmcli's complaint → something a person at the panel can act on."""
    text = " ".join(stderr.split())
    if password:
        text = text.replace(password, "•••")
    low = text.lower()
    if "psk" in low and "invalid" in low:
        return "bad_password"  # WPA wants 8–63 characters
    if "secrets were required" in low or "802-1x" in low:
        return "wrong_password"
    if "no network with ssid" in low:
        return "not_found"
    if "timeout" in low or "timed out" in low:
        return "timeout"
    if "not authorized" in low or "insufficient privileges" in low:
        return "denied"
    for prefix in ("Error: ", "error: "):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    return text or "unknown"


class NmcliBackend:
    def __init__(self, iface: str = WIFI_IFACE, nmcli: str = NMCLI) -> None:
        self.iface = iface
        self.nmcli = nmcli

    def _run(self, args: list[str], timeout: float) -> subprocess.CompletedProcess:
        env = dict(os.environ, LC_ALL="C", LANG="C")  # messages we can match on
        try:
            return subprocess.run(
                [self.nmcli, *args], capture_output=True, text=True, timeout=timeout, env=env, check=False
            )
        except FileNotFoundError as exc:
            raise NetError("no_nm") from exc
        except subprocess.TimeoutExpired as exc:
            raise NetError("timeout") from exc

    def status(self) -> NetStatus:
        host = socket.gethostname()
        mdns = AVAHI_SOCKET.exists()
        try:
            p = self._run(
                ["-t", "-f", "GENERAL.DEVICE,GENERAL.TYPE,GENERAL.STATE,IP4.ADDRESS", "dev", "show"],
                STATUS_TIMEOUT,
            )
            if p.returncode != 0:
                raise NetError(friendly_error(p.stderr))
            links = parse_links(p.stdout)
            wifi = [l for l in links if l.kind == "wifi" and l.state == "connected"]
            if wifi:
                nets = self._wifi_list(False, wifi[0].iface)
                cur = next((n for n in nets if n.in_use), None)
                if cur is not None:
                    links = [
                        Link(l.iface, l.kind, l.state, l.ip4, cur.ssid, cur.signal) if l is wifi[0] else l
                        for l in links
                    ]
        except NetError as exc:
            return NetStatus(host, mdns, (), str(exc))
        return NetStatus(host, mdns, tuple(links))

    def _wifi_list(self, rescan: bool, iface: str | None = None) -> list[WifiNet]:
        args = ["-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "dev", "wifi", "list",
                "ifname", iface or self.iface, "--rescan", "yes" if rescan else "auto"]
        p = self._run(args, SCAN_TIMEOUT)
        if p.returncode != 0 and rescan:
            # "Scanning not allowed while already scanning" and friends —
            # what NetworkManager already knows is still worth showing
            p = self._run(args[:-1] + ["no"], SCAN_TIMEOUT)
        if p.returncode != 0:
            raise NetError(friendly_error(p.stderr))
        return parse_wifi_list(p.stdout)

    def scan(self, rescan: bool = False) -> list[WifiNet]:
        return self._wifi_list(rescan)

    def _profiles(self) -> set[str]:
        p = self._run(["-t", "-f", "UUID,TYPE", "con", "show"], STATUS_TIMEOUT)
        out = set()
        for line in p.stdout.splitlines():
            f = split_terse(line)
            if len(f) >= 2 and f[1] == "802-11-wireless":
                out.add(f[0])
        return out

    def connect(self, ssid: str, password: str | None) -> Connected:
        try:
            before = self._profiles()
        except NetError:
            before = None
        args = ["-w", str(CONNECT_WAIT), "dev", "wifi", "connect", ssid]
        if password:
            args += ["password", password]
        args += ["ifname", self.iface]
        log.info("wifi: připojuji k %r (%s)", ssid, "s heslem" if password else "otevřená")
        t0 = time.monotonic()
        p = self._run(args, CONNECT_WAIT + 15)
        if p.returncode != 0:
            err = friendly_error(p.stderr or p.stdout, password)
            log.warning("wifi: připojení k %r selhalo za %.0f s: %s", ssid, time.monotonic() - t0, err)
            self._forget_new(before, ssid)
            raise NetError(err)
        ip = ""
        for _ in range(10):  # the address can trail the "activated" by a moment
            st = self.status()
            link = next((l for l in st.links if l.iface == self.iface), None)
            if link and link.ip4:
                ip = link.ip4
                break
            time.sleep(0.5)
        log.info("wifi: připojeno k %r, IP %s", ssid, ip or "?")
        return Connected(ssid, ip)

    def _forget_new(self, before: set[str] | None, ssid: str) -> None:
        """A failed attempt leaves a profile with the wrong password behind —
        NetworkManager would keep retrying it. Only profiles this attempt
        created go; everything that existed before stays untouched."""
        if before is None:
            return
        try:
            for uuid in self._profiles() - before:
                log.info("wifi: mažu nepovedený profil pro %r", ssid)
                self._run(["con", "delete", "uuid", uuid], STATUS_TIMEOUT)
        except NetError:
            pass
