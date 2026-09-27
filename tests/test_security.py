"""Bezpečnost služby na Pi: žádná nová práva pro ytdj a jeho potomky, sandbox Codexu."""

from __future__ import annotations

import configparser
import unittest
from pathlib import Path
from unittest import mock

import ytdj.__main__ as main

ROOT = Path(__file__).resolve().parent.parent


class ServiceUnit(unittest.TestCase):
    def test_no_new_privileges(self):
        # ytdj, Codex, mpv ani yt-dlp nesmí přes sudo (na Pi bez hesla) získat root
        unit = configparser.ConfigParser(strict=False, interpolation=None)
        unit.read(ROOT / "packaging" / "ytdj.service")
        self.assertEqual(unit["Service"]["NoNewPrivileges"], "yes")

    def test_install_script_brings_bubblewrap(self):
        text = (ROOT / "packaging" / "install-service.sh").read_text()
        self.assertIn("apt-get install -y bubblewrap", text)


class SandboxWarning(unittest.TestCase):
    def test_missing_bwrap_on_linux_is_reported(self):
        with mock.patch.object(main.sys, "platform", "linux"), \
                mock.patch.object(main.shutil, "which", return_value=None):
            self.assertIn("bubblewrap", main.sandbox_warning() or "")

    def test_present_bwrap_is_quiet(self):
        with mock.patch.object(main.sys, "platform", "linux"), \
                mock.patch.object(main.shutil, "which", return_value="/usr/bin/bwrap"):
            self.assertIsNone(main.sandbox_warning())


if __name__ == "__main__":
    unittest.main()
