"""jukebox.local: skript vyhlásí druhé jméno a při změně adresy ho vyhlásí znovu."""

from __future__ import annotations

import configparser
import os
import signal
import stat
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
