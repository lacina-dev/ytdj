#!/usr/bin/env python3
"""Opakované ohlašování jmen jukeboxu v místní síti (mDNS) — pro sítě, které
ztrácejí multicast.

Jména `.local` drží v počítačích a telefonech jen dočasně (120 s) a obnovují
se odpovědí poslanou všem (multicast). Kancelářská Wi-Fi umí většinu takových
odpovědí zahodit (změřeno 6. 10.: z kabelu na Wi-Fi došla 1–3 z 10) — jméno pak
funguje, dokud je v paměti zařízení, a přestane, když se obnova ztratí.

Tenhle proces nic neregistruje a avahi se nedotýká. Jen každých pár vteřin
pošle všem tutéž odpověď, kterou by avahi poslalo na dotaz („jukebox.local je
na adrese …“), s delší platností. Čím víc pokusů se vejde do doby platnosti,
tím menší šance, že se ztratí všechny (20 % doručení, 24 pokusů → 0,5 %).

Bezpečnost: ohlašuje jen jména, která se na tomhle stroji opravdu překládají
na jeho vlastní adresu (tedy je avahi samo zná), jen záznam adresy IPv4, a při
ukončení neposílá nic — jména dál obsluhuje avahi. „Sbohem“ (platnost 0) jde
ven jen pro starou adresu, když se adresa stroje změní.

    mdns-announce.py jukebox.local ytdj.local
    YTDJ_MDNS_EVERY=10 (s)  YTDJ_MDNS_TTL=240 (s)
"""

from __future__ import annotations

import os
import signal
import socket
import struct
import sys
import time

GROUP, PORT = "224.0.0.251", 5353
EVERY = float(os.environ.get("YTDJ_MDNS_EVERY", "10"))
TTL = int(os.environ.get("YTDJ_MDNS_TTL", "240"))
RECHECK = 60.0  # s — jak často ověřit, že jméno pořád patří nám


def own_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # nic se neposílá, jen se zvolí odchozí adresa
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


def is_ours(name: str, ip: str) -> bool:
    """Překládá se jméno tady na naši adresu? (zná ho avahi a patří tomuhle stroji)"""
    try:
        found = {a[4][0] for a in socket.getaddrinfo(name, None, socket.AF_INET)}
    except (OSError, UnicodeError):
        return False
    return ip in found


def encode_name(name: str) -> bytes:
    out = b""
    for part in name.rstrip(".").split("."):
        raw = part.encode("idna")
        if not 0 < len(raw) < 64:
            raise ValueError(f"špatné jméno: {name!r}")
        out += bytes([len(raw)]) + raw
    return out + b"\0"


def response(names: list[str], ip: str, ttl: int) -> bytes:
    """Odpověď mDNS (bez otázky): záznam A pro každé jméno, s příznakem „jediný vlastník“."""
    addr = socket.inet_aton(ip)
    out = struct.pack("!HHHHHH", 0, 0x8400, 0, len(names), 0, 0)
    for name in names:
        out += encode_name(name) + struct.pack("!HHIH", 1, 0x8001, ttl, 4) + addr
    return out


def open_socket(ip: str) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", PORT))  # odpovědi mDNS musí odcházet z portu 5353
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)  # avahi na tomhle stroji to nevidí
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
    return s


class Announcer:
    def __init__(self, names: list[str], send=None, own_ip=own_ip, is_ours=is_ours,
                 clock=time.monotonic, ttl: int = TTL) -> None:
        self.names = [n.rstrip(".").lower() for n in names]
        for n in self.names:
            encode_name(n)
        self._send = send
        self.own_ip, self.is_ours, self.clock, self.ttl = own_ip, is_ours, clock, ttl
        self.ip = ""
        self.ours: list[str] = []
        self._checked = float("-inf")
        self._sock: socket.socket | None = None

    def send(self, packet: bytes, ip: str) -> None:
        if self._send is not None:
            self._send(packet, ip)
            return
        if self._sock is None:
            self._sock = open_socket(ip)
        self._sock.sendto(packet, (GROUP, PORT))

    def step(self) -> list[str]:
        """Jedno ohlášení; vrací, která jména šla ven."""
        ip = self.own_ip()
        if ip != self.ip:
            if self.ip and self.ours:
                # adresa se změnila: stará už neplatí (jediné „sbohem“, které posíláme)
                try:
                    self.send(response(self.ours, self.ip, 0), self.ip)
                except OSError:
                    pass
            if self._sock is not None:
                self._sock.close()
                self._sock = None
            self.ip, self.ours, self._checked = ip, [], float("-inf")
        if not ip:
            return []
        if self.clock() - self._checked >= RECHECK:
            self._checked = self.clock()
            self.ours = [n for n in self.names if self.is_ours(n, ip)]
        if not self.ours:
            return []
        try:
            self.send(response(self.ours, ip, self.ttl), ip)
        except OSError as exc:
            print(f"mdns-announce: odeslání selhalo: {exc}", flush=True)
            if self._sock is not None:
                self._sock.close()
                self._sock = None
            return []
        return list(self.ours)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    ann = Announcer(argv)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # bez „sbohem“: avahi jména drží dál
    said: list[str] | None = None
    while True:
        out = ann.step()
        if out != said:
            said = out
            print(f"mdns-announce: {', '.join(out) or 'nic (jména tu neplatí)'} -> {ann.ip or '?'} "
                  f"každých {EVERY:g} s, platnost {ann.ttl} s", flush=True)
        time.sleep(EVERY)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
