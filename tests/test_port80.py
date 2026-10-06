"""Web i na portu 80 (POZADAVKY #68): přesměrování v jádře, jen vlastní tabulka,
port webu z konfigurace jukeboxu.

    python -m unittest tests.test_port80
"""

from __future__ import annotations

import configparser
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "packaging" / "rpi" / "port80.sh"
UNIT = ROOT / "packaging" / "ytdj-port80.service"


class Rules(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.fed = self.dir / "fed"  # co dostal (falešný) nft na vstup
        nft = self.dir / "nft"
        nft.write_text(f'#!/bin/sh\necho "ARGS $@" >> {self.fed}\ncat >> {self.fed}\n')
        nft.chmod(nft.stat().st_mode | stat.S_IXUSR)
        self.env = dict(os.environ, YTDJ_NFT=str(nft))

    def tearDown(self):
        self.tmp.cleanup()

    def config(self, text: str) -> str:
        path = self.dir / "config.toml"
        path.write_text(text)
        return str(path)

    def run_script(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(SCRIPT), *args], env=self.env, capture_output=True,
                              text=True, timeout=10)

    def fed_text(self) -> str:
        return self.fed.read_text() if self.fed.exists() else ""

    def test_redirect_follows_web_port_from_the_jukebox_config(self):
        cfg = self.config('language = "cs"\nweb_host = "0.0.0.0"\nweb_port = 9123  # vlastní\n')
        out = self.run_script("print", cfg)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.count("tcp dport 80 redirect to :9123"), 2)  # zvenku i z Pi
        self.assertNotIn("8765", out.stdout)
        self.assertEqual(self.fed_text(), "")  # print nic nenahrává
        # start nahraje přesně totéž
        self.assertEqual(self.run_script("start", cfg).returncode, 0)
        self.assertIn("ARGS -f -", self.fed_text())
        self.assertIn(out.stdout.strip(), self.fed_text())
        # bez konfigurace (nebo bez klíče) výchozí port webu — stejný jako v aplikaci
        defaults = (ROOT / "ytdj" / "config.py").read_text(encoding="utf-8")
        self.assertIn('"web_port": 8765', defaults)
        for cfg in (self.config("language = 'cs'\n"), str(self.dir / "neni.toml"), ""):
            out = self.run_script("print", cfg) if cfg else self.run_script("print")
            self.assertIn("redirect to :8765", out.stdout)

    def test_only_our_own_table_is_touched_and_nothing_is_blocked(self):
        out = self.run_script("print", self.config("web_port = 8765\n")).stdout
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        # nahrazení naráz: založit, smazat a znovu postavit JEN naši tabulku
        self.assertEqual(lines[:3], ["table ip ytdj_web", "delete table ip ytdj_web",
                                     "table ip ytdj_web {"])
        self.assertEqual(out.count("table "), 3)
        for banned in ("flush", "ruleset", "drop", "reject", "filter", "inet", "ip6", "include"):
            self.assertNotIn(banned, out, banned)
        self.assertEqual(out.count("policy accept"), 2)
        self.assertEqual(out.count("type nat hook"), 2)
        # jen spojení na vlastní adresy stroje, jen port 80
        self.assertEqual(out.count("fib daddr type local tcp dport 80 redirect"), 2)
        # stop: zase jen naše tabulka, a jde zavolat i podruhé (nejdřív ji založí)
        self.assertEqual(self.run_script("stop").returncode, 0)
        fed = [ln for ln in self.fed_text().splitlines() if not ln.startswith("ARGS")]
        self.assertEqual(fed, ["table ip ytdj_web", "delete table ip ytdj_web"])

    def test_wrong_port_is_refused_and_port_80_needs_no_redirect(self):
        for bad in ("22", "443", "70000", "1023"):
            out = self.run_script("start", self.config(f"web_port = {bad}\n"))
            self.assertNotEqual(out.returncode, 0, bad)
            self.assertNotIn("redirect", self.fed_text())
        # nesmysl místo čísla se nevezme vůbec (žádné vkládání textu do pravidel)
        out = self.run_script("print", self.config('web_port = "80; flush ruleset"\n'))
        self.assertIn("redirect to :8765", out.stdout)
        self.assertNotIn("flush", out.stdout)
        # web sám na 80: přesměrování není potřeba a staré se uklidí
        self.fed.unlink(missing_ok=True)
        out = self.run_script("start", self.config("web_port = 80\n"))
        self.assertEqual(out.returncode, 0)
        self.assertNotIn("redirect", self.fed_text())
        self.assertIn("delete table ip ytdj_web", self.fed_text())
        self.assertEqual(self.run_script("neco").returncode, 2)


class Packaging(unittest.TestCase):
    def test_unit_loads_only_our_rules_with_minimal_privileges(self):
        raw = UNIT.read_text(encoding="utf-8")
        unit = configparser.ConfigParser(strict=False, interpolation=None)
        unit.read_string(raw)
        s = unit["Service"]
        self.assertEqual((s["Type"], s["RemainAfterExit"]), ("oneshot", "yes"))
        cfg = "@HOME@/.config/ytdj/config.toml"
        self.assertEqual(s["ExecStart"], f"/usr/local/lib/ytdj/port80.sh start {cfg}")
        self.assertEqual(s["ExecReload"], s["ExecStart"])  # znovunahrání = nahrazení naráz
        self.assertEqual(s["ExecStop"], "/usr/local/lib/ytdj/port80.sh stop")
        self.assertEqual(s["CapabilityBoundingSet"], "CAP_NET_ADMIN CAP_DAC_READ_SEARCH")
        self.assertEqual(s["NoNewPrivileges"], "yes")
        self.assertEqual((s["ProtectSystem"], s["ProtectHome"]), ("strict", "read-only"))
        # na nftables.service nezávisí
        deps = " ".join(unit["Unit"].get(k, "") for k in ("Requires", "Wants", "After", "BindsTo"))
        self.assertNotIn("nftables", deps)
        self.assertTrue(os.access(SCRIPT, os.X_OK))

    def test_installed_only_on_the_jukebox_with_an_opt_out_and_a_rollback(self):
        text = (ROOT / "packaging" / "install-service.sh").read_text(encoding="utf-8")
        block = text[text.index("port80=no"):text.index("# Druhé jméno v síti")]
        self.assertIn("YTDJ_PORT80", block)
        self.assertIn('"$model" == Raspberry\\ Pi*', block)
        self.assertIn("command -v nft", block)
        self.assertIn("systemctl enable ytdj-port80.service", block)
        self.assertIn("reload-or-restart ytdj-port80.service", block)  # bez výpadku portu 80
        for banned in ("apt-get", "nftables.service", "/etc/nftables.conf", "sysctl"):
            self.assertNotIn(banned, block, banned)
        notes = (ROOT / "packaging" / "rpi" / "NOTES.md").read_text(encoding="utf-8")
        self.assertIn("systemctl disable --now ytdj-port80", notes)
        self.assertIn("ytdj_web", notes)

    def test_the_app_keeps_its_own_port_and_does_not_bind_80(self):
        """Port webu zůstává (záložky, naskenované QR kódy, displej i telka přes 127.0.0.1)."""
        unit = (ROOT / "packaging" / "ytdj.service").read_text(encoding="utf-8")
        self.assertIn("NoNewPrivileges=yes", unit)
        self.assertNotIn("CAP_NET_BIND_SERVICE", unit)
        self.assertNotIn("ip_unprivileged_port_start", "".join(
            p.read_text(encoding="utf-8") for p in (ROOT / "packaging").rglob("*.sh")))


if __name__ == "__main__":
    unittest.main()
