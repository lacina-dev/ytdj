"""jukebox.local: skript vyhlásí druhé jméno a při změně adresy ho vyhlásí znovu."""

from __future__ import annotations

import configparser
import importlib.util
import os
import signal
import socket
import stat
import struct
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "packaging" / "rpi" / "mdns-alias.sh"


def _wait(cond, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class MdnsAlias(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.calls = self.dir / "calls"
        self.ipfile = self.dir / "addr"
        self.ipfile.write_text("10.0.0.7")
        self.broken = self.dir / "broken"
        for name, body in (
            # the fake keeps running like the real one: the record lives while the process does;
            # with the "broken" file it fails at once, like avahi refusing the client
            ("avahi-publish", f'echo "$@" >> {self.calls}\n[ -e {self.broken} ] && exit 1\nexec sleep 60\n'),
            ("ip", f'echo "1.1.1.1 via 10.0.0.1 dev wlan0 src $(cat {self.ipfile}) uid 0"\n'),
        ):
            f = self.dir / name
            f.write_text("#!/bin/sh\n" + body)
            f.chmod(f.stat().st_mode | stat.S_IXUSR)
        self.env = dict(os.environ, PATH=f"{self.dir}:{os.environ['PATH']}", YTDJ_MDNS_CHECK="0.2",
                        YTDJ_MDNS_BACKOFF="2", YTDJ_MDNS_BACKOFF_MAX="4")
        self.proc = None

    def start(self):
        self.proc = subprocess.Popen(["bash", str(SCRIPT)], env=self.env, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, start_new_session=True)

    def tearDown(self):
        if self.proc is not None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            self.proc.wait(timeout=5)
            self.proc.stdout.close()
        self.tmp.cleanup()

    def _calls(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_publishes_the_default_name_and_follows_the_address(self):
        self.start()
        self.assertTrue(_wait(lambda: self._calls() == ["-a -R jukebox.local 10.0.0.7"]), self._calls())
        time.sleep(0.6)  # the same address is not announced again
        self.assertEqual(len(self._calls()), 1)
        self.ipfile.write_text("10.0.0.9")  # DHCP gave another one
        self.assertTrue(_wait(lambda: self._calls()[-1:] == ["-a -R jukebox.local 10.0.0.9"]), self._calls())
        self.assertEqual(len(self._calls()), 2)


    def test_failing_publish_is_not_retried_in_a_tight_loop(self):
        """Pi 6. 10.: avahi klienta odmítlo a skript to zkoušel každých 15 s
        donekonečna. Teď po neúspěchu čeká a pauzu zdvojuje (tady 2 s, 4 s)."""
        self.broken.touch()
        self.start()
        self.assertTrue(_wait(lambda: len(self._calls()) == 1))
        time.sleep(1.3)  # kontrola běží každých 0,2 s — bez pauzy by to bylo ~6 pokusů
        self.assertEqual(len(self._calls()), 1)
        self.assertTrue(_wait(lambda: len(self._calls()) == 2, timeout=4))
        time.sleep(2.5)  # druhá pauza je delší
        self.assertEqual(len(self._calls()), 2)
        # jakmile to jde, vyhlásí se a drží
        self.broken.unlink()
        self.assertTrue(_wait(lambda: len(self._calls()) == 3, timeout=6))
        time.sleep(1.0)
        self.assertEqual(len(self._calls()), 3)
        self.assertEqual(self.proc.poll(), None)  # skript sám běží dál

    def test_new_address_is_published_at_once_even_after_a_failure(self):
        self.env["YTDJ_MDNS_BACKOFF"] = "1"
        self.start()
        self.assertTrue(_wait(lambda: len(self._calls()) == 1))
        self.ipfile.write_text("10.0.0.9")
        self.assertTrue(_wait(lambda: self._calls()[-1:] == ["-a -R jukebox.local 10.0.0.9"],
                              timeout=3), self._calls())


def _announcer():
    spec = importlib.util.spec_from_file_location("mdns_announce", ROOT / "packaging/rpi/mdns-announce.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _records(data: bytes) -> tuple[int, list[tuple]]:
    """(příznaky, [(jméno, typ, třída, platnost, adresa)]) z odpovědi mDNS bez komprese."""
    _id, flags, qd, an, _ns, _ar = struct.unpack("!HHHHHH", data[:12])
    assert qd == 0
    off, out = 12, []
    for _ in range(an):
        parts = []
        while data[off]:
            n = data[off]
            parts.append(data[off + 1:off + 1 + n].decode())
            off += 1 + n
        off += 1
        rtype, rclass, ttl, rdlen = struct.unpack("!HHIH", data[off:off + 10])
        off += 10
        out.append((".".join(parts), rtype, rclass, ttl, socket.inet_ntoa(data[off:off + rdlen])))
        off += rdlen
    assert off == len(data)
    return flags, out


class Announce(unittest.TestCase):
    """Opakované ohlašování jmen pro sítě, které ztrácejí multicast (volitelné)."""

    def rig(self, ours=("jukebox.local", "ytdj.local"), ip="10.0.0.7"):
        mod = _announcer()
        sent: list[tuple[bytes, str]] = []
        state = {"ip": ip, "ours": set(ours), "t": 0.0}
        ann = mod.Announcer(["jukebox.local", "ytdj.local"], send=lambda p, i: sent.append((p, i)),
                            own_ip=lambda: state["ip"],
                            is_ours=lambda name, addr: name in state["ours"],
                            clock=lambda: state["t"], ttl=240)
        return mod, ann, sent, state

    def test_packet_is_a_plain_answer_for_our_names(self):
        mod, ann, sent, _ = self.rig()
        self.assertEqual(ann.step(), ["jukebox.local", "ytdj.local"])
        flags, recs = _records(sent[0][0])
        self.assertEqual(flags, 0x8400)  # odpověď, autoritativní; žádná otázka
        self.assertEqual(recs, [("jukebox.local", 1, 0x8001, 240, "10.0.0.7"),
                                ("ytdj.local", 1, 0x8001, 240, "10.0.0.7")])  # jen A (IPv4)
        self.assertEqual((mod.GROUP, mod.PORT), ("224.0.0.251", 5353))
        self.assertGreaterEqual(mod.TTL // int(mod.EVERY), 20)  # dost pokusů na jednu platnost
        with self.assertRaises(ValueError):
            mod.Announcer(["a" * 70 + ".local"])

    def test_only_names_that_resolve_here_to_our_address(self):
        mod, ann, sent, state = self.rig(ours=("ytdj.local",))  # služba druhého jména neběží
        self.assertEqual(ann.step(), ["ytdj.local"])
        self.assertEqual([r[0] for r in _records(sent[-1][0])[1]], ["ytdj.local"])
        state["ours"] = set()  # ani jedno jméno tu neplatí → ven nejde nic
        state["t"] += mod.RECHECK + 1
        n = len(sent)
        self.assertEqual(ann.step(), [])
        self.assertEqual(len(sent), n)
        # bez sítě taky nic
        mod, ann, sent, state = self.rig(ip="")
        self.assertEqual((ann.step(), sent), ([], []))
        # skutečná kontrola: cizí jméno se na naši adresu nepřeloží
        self.assertFalse(mod.is_ours("tohle-jmeno-neexistuje.invalid", "10.0.0.7"))

    def test_goodbye_only_for_an_old_address_never_on_exit(self):
        mod, ann, sent, state = self.rig()
        ann.step()
        ann.step()
        self.assertTrue(all(r[3] == 240 for p, _ in sent for r in _records(p)[1]))  # žádné „sbohem“
        state["ip"] = "10.0.0.9"  # DHCP dal jinou adresu
        ann.step()
        bye = _records(sent[-2][0])[1]
        self.assertEqual({(r[3], r[4]) for r in bye}, {(0, "10.0.0.7")})  # stará adresa neplatí
        new = _records(sent[-1][0])[1]
        self.assertEqual({(r[3], r[4]) for r in new}, {(240, "10.0.0.9")})
        src = (ROOT / "packaging/rpi/mdns-announce.py").read_text(encoding="utf-8")
        self.assertIn("signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))", src)
        self.assertEqual(src.count("response(self.ours, self.ip, 0)"), 1)  # jediné místo se „sbohem“

    def test_unit_is_optional_harmless_and_keeps_away_from_avahi(self):
        raw = (ROOT / "packaging" / "ytdj-mdns-announce.service").read_text()
        unit = configparser.ConfigParser(strict=False, interpolation=None)
        unit.read_string(raw)
        s = unit["Service"]
        self.assertEqual(s["ExecStart"],
                         "/usr/bin/python3 /usr/local/lib/ytdj/mdns-announce.py jukebox.local %H.local")
        self.assertEqual((s["User"], s["NoNewPrivileges"], s["CapabilityBoundingSet"]),
                         ("@USER@", "yes", ""))
        self.assertEqual(s["RestrictAddressFamilies"], "AF_INET AF_UNIX")
        src = (ROOT / "packaging/rpi/mdns-announce.py").read_text(encoding="utf-8")
        code = "\n".join(ln for ln in src.split('"""', 2)[2].splitlines()
                         if not ln.lstrip().startswith("#"))
        for banned in ("import dbus", "avahi-", "subprocess", "/etc/", "AF_INET6",
                       "IP_MULTICAST_LOOP, 1"):
            self.assertNotIn(banned, code, banned)
        self.assertIn("IP_MULTICAST_LOOP, 0", code)
        text = (ROOT / "packaging" / "install-service.sh").read_text()
        self.assertIn('if [ "${YTDJ_MDNS_ANNOUNCE:-0}" = 1 ]; then', text)  # jen na výslovné přání
        self.assertIn("systemctl disable --now ytdj-mdns-announce", text)
        # skript se sám nespouští znovu a znovu (avahi-publish se kvůli ohlašování nerestartuje)
        alias = SCRIPT.read_text()
        self.assertNotIn("mdns-announce", alias)
        self.assertTrue(os.access(ROOT / "packaging/rpi/mdns-announce.py", os.X_OK))


class Packaging(unittest.TestCase):
    def test_unit_and_install_script(self):
        raw = (ROOT / "packaging" / "ytdj-mdns-alias.service").read_text()
        unit = configparser.ConfigParser(strict=False, interpolation=None)
        unit.read_string(raw)
        s = unit["Service"]
        self.assertEqual(s["ExecStart"], "/usr/local/lib/ytdj/mdns-alias.sh jukebox.local")
        self.assertEqual(s["NoNewPrivileges"], "yes")
        # uživatel, kterého si D-Bus umí dohledat — ne dynamický a ne root
        self.assertEqual(s["User"], "@USER@")
        self.assertNotIn("DynamicUser", s)
        self.assertEqual(s["CapabilityBoundingSet"], "")
        self.assertIn("AF_UNIX", s["RestrictAddressFamilies"])  # D-Bus
        # žádné restartování dokola
        self.assertEqual(s["Restart"], "on-failure")
        self.assertGreaterEqual(int(s["RestartSec"]), 30)
        self.assertEqual(unit["Unit"]["StartLimitBurst"], "5")
        text = (ROOT / "packaging" / "install-service.sh").read_text()
        self.assertIn("ytdj-mdns-alias.service", text)
        self.assertIn("avahi-utils", text)
        self.assertIn('sed -e "s|@USER@|$USER|g" "$repo/packaging/ytdj-mdns-alias.service"', text)
        self.assertIn("YTDJ_MDNS_ALIAS", text)
        self.assertTrue(os.access(SCRIPT, os.X_OK))

    def test_alias_is_a_separate_record_that_cannot_touch_the_host_name(self):
        """Jen vlastní adresní záznam přes klienta avahi: bez reverzního záznamu
        (ten patří hlavnímu jménu), bez zásahu do nastavení avahi a jména stroje."""
        script = SCRIPT.read_text()
        self.assertIn('avahi-publish -a -R "$NAME" "$ip"', script)
        code = "\n".join(ln for ln in script.splitlines() if not ln.lstrip().startswith("#"))
        for banned in ("/etc/avahi", "avahi-daemon", "set-host-name", "hostname", "systemctl",
                       "kill -HUP"):
            self.assertNotIn(banned, code, banned)


if __name__ == "__main__":
    unittest.main()
