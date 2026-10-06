"""jukebox.local: skript vyhlásí druhé jméno a při změně adresy ho vyhlásí znovu."""

from __future__ import annotations

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
        for name, body in (
            # the fake keeps running like the real one: the record lives while the process does
            ("avahi-publish", f'echo "$@" >> {self.calls}\nexec sleep 60\n'),
            ("ip", f'echo "1.1.1.1 via 10.0.0.1 dev wlan0 src $(cat {self.ipfile}) uid 0"\n'),
        ):
            f = self.dir / name
            f.write_text("#!/bin/sh\n" + body)
            f.chmod(f.stat().st_mode | stat.S_IXUSR)
        env = dict(os.environ, PATH=f"{self.dir}:{os.environ['PATH']}", YTDJ_MDNS_CHECK="0.2")
        self.proc = subprocess.Popen(["bash", str(SCRIPT)], env=env, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, start_new_session=True)

    def tearDown(self):
        os.killpg(self.proc.pid, signal.SIGTERM)
        self.proc.wait(timeout=5)
        self.proc.stdout.close()
        self.tmp.cleanup()

    def _calls(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_publishes_the_default_name_and_follows_the_address(self):
        self.assertTrue(_wait(lambda: self._calls() == ["-a -R jukebox.local 10.0.0.7"]), self._calls())
        time.sleep(0.6)  # the same address is not announced again
        self.assertEqual(len(self._calls()), 1)
        self.ipfile.write_text("10.0.0.9")  # DHCP gave another one
        self.assertTrue(_wait(lambda: self._calls()[-1:] == ["-a -R jukebox.local 10.0.0.9"]), self._calls())
        self.assertEqual(len(self._calls()), 2)


class Packaging(unittest.TestCase):
    def test_unit_and_install_script(self):
        unit = (ROOT / "packaging" / "ytdj-mdns-alias.service").read_text()
        self.assertIn("ExecStart=/usr/local/lib/ytdj/mdns-alias.sh jukebox.local", unit)
        self.assertIn("NoNewPrivileges=yes", unit)
        text = (ROOT / "packaging" / "install-service.sh").read_text()
        self.assertIn("ytdj-mdns-alias.service", text)
        self.assertIn("avahi-utils", text)
        self.assertTrue(os.access(SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main()
