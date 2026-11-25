"""Noční aktualizace yt-dlp (F-PROVOZ-14): packaging/rpi/ytdlp-update.sh
proti falešnému yt-dlp, uv, curl a systemctl v dočasném adresáři.

    YTDJ_EVENTS_FILE=/tmp/ev.jsonl .venv/bin/python -m unittest tests.test_ytdlp_update -v

Falešný yt-dlp se chová jako skutečný: vypíše tabulku formátů, vybraný
formát a „stream“ na stdout; do -v výstupu dá celé volby (cookies, PO token)
i adresu streamu — skript z toho nesmí nic pustit ven. Verze uvedené
v `broken` dávají jen náhledy (jako 2026.08.19 na Pi 23. 11.), verze
v `broken_installed` jen po instalaci (zkouška bokem projde).
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "packaging" / "rpi" / "ytdlp-update.sh"
OLD, NEW = "2026.08.19", "2026.11.23"

FAKE_YTDLP = r"""#!/usr/bin/env bash
S=@S@
v=${FAKE_VERSION:-$(cat "$S/installed")}
if [ "${1:-}" = --version ]; then echo "$v"; exit 0; fi
cookies=""; table=""; fmtf=""; prev=""; mode=installed
[ -n "${FAKE_VERSION:-}" ] && mode=candidate
for a in "$@"; do
    case "$prev" in --cookies) cookies=$a ;; esac
    if [ "$prev" = formats_table ]; then table=$a; fi
    if [ "$prev" = format_id ]; then fmtf=$a; fi
    prev=$a
done
echo "yt-dlp $mode $v cookies=$cookies" >> "$S/calls.log"
echo "[debug] Command-line config: [$*]" >&2
echo "[debug] [youtube] [pot] PO Token Providers: bgutil:http-1.3.1 (external)" >&2
[ -f "$S/slow" ] && sleep "$(cat "$S/slow")"
if grep -qx "$v" "$S/broken" 2>/dev/null \
   || { [ $mode = installed ] && grep -qx "$v" "$S/broken_installed" 2>/dev/null; }; then
    echo "WARNING: [youtube] x: n challenge solving failed: Some formats may be missing" >&2
    echo "ERROR: [youtube] x: Requested format is not available" >&2
    exit 1
fi
if [ -n "$table" ]; then
    printf 'ID  EXT   RESOLUTION\nsb0 mhtml 48x27 storyboard\n251 webm  audio only opus\n140 m4a   audio only mp4a\n' > "$table"
fi
[ -n "$fmtf" ] && echo 251 > "$fmtf"
echo '[debug] Invoking http downloader on "https://rr1.googlevideo.com/videoplayback?SECRETURL=1"' >&2
head -c 300000 /dev/zero
"""

FAKE_UV = r"""#!/usr/bin/env bash
S=@S@
echo "uv $*" >> "$S/calls.log"
case "$1 $2" in
"tool dir") echo "$S/tools" ;;
"tool run")
    shift 2; v=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --from) v=${2#yt-dlp==}; shift 2 ;;
            --with) shift 2 ;;
            yt-dlp) shift; break ;;
            *) shift ;;
        esac
    done
    FAKE_VERSION=$v exec "$S/bin/yt-dlp" "$@" ;;
"tool install")
    [ -f "$S/install_fail" ] && { echo "error: no network" >&2; exit 2; }
    shift 2; spec=""; withs=()
    while [ $# -gt 0 ]; do
        case "$1" in
            --force) shift ;;
            --with) withs+=("$2"); shift 2 ;;
            *) spec=$1; shift ;;
        esac
    done
    v=${spec#yt-dlp}; v=${v#>=}; v=${v#==}
    echo "$v" > "$S/installed"
    site="$S/tools/yt-dlp/lib/python3.13/site-packages"
    rm -rf "$S/tools/yt-dlp"; mkdir -p "$site"
    for w in "${withs[@]}"; do
        n=${w%%==*}; n=${n//-/_}; ver=1.0; [[ $w == *==* ]] && ver=${w#*==}
        [ -f "$S/drop_plugin" ] && [ "$n" = bgutil_ytdlp_pot_provider ] && continue
        mkdir -p "$site/$n-$ver.dist-info"
    done ;;
"cache prune") ;;
*) exit 2 ;;
esac
"""

FAKE_CURL = r"""#!/usr/bin/env bash
S=@S@
url=""; for a in "$@"; do url=$a; done
case "$url" in
*pypi*) [ -f "$S/pypi.json" ] && cat "$S/pypi.json" || exit 7 ;;
*api/status*)
    n=$(cat "$S/status.n" 2>/dev/null || echo 0); echo $((n + 1)) > "$S/status.n"
    total=$(wc -l < "$S/status.seq")
    line=$(( n + 1 > total ? total : n + 1 ))
    s=$(sed -n "${line}p" "$S/status.seq")
    echo "status $s" >> "$S/calls.log"
    case "$s" in
        idle) echo '{"playing": false, "paused": false, "outage": null}' ;;
        paused) echo '{"playing": true, "paused": true, "outage": null}' ;;
        playing) echo '{"playing": true, "paused": false, "outage": null}' ;;
        outage) echo '{"playing": false, "paused": false, "outage": {"kind": "playback"}}' ;;
        down) exit 7 ;;
    esac ;;
*) exit 6 ;;
esac
"""

FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
S=@S@
echo "systemctl $*" >> "$S/calls.log"
case "$*" in
*is-active*) [ -f "$S/inactive" ] && exit 3; exit 0 ;;
*show*) cat "$S/active_enter" 2>/dev/null || echo "" ;;
*restart*) date '+%a %Y-%m-%d %H:%M:%S %Z' > "$S/active_enter"; sleep 1 ;;
esac
exit 0
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.s = d / "s"
        (self.s / "bin").mkdir(parents=True)
        self.home = d / "home"
        (self.home / ".config" / "ytdj").mkdir(parents=True)
        self.cookies = self.home / "cookies.txt"
        self.cookies.write_text(".youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\tSECRETCOOKIE\n")
        (self.home / ".config" / "ytdj" / "config.toml").write_text(
            f'cookies_browser = "none"\ncookies_file = "{self.cookies}"\n')
        (self.home / ".config" / "ytdj" / "env").write_text(
            "YTDJ_PO_TOKEN=web_music.gvs+SECRETTOKEN\n")
        self.events = d / "events.jsonl"
        self.state_dir = d / "state"
        for name, text in (("yt-dlp", FAKE_YTDLP), ("uv", FAKE_UV), ("curl", FAKE_CURL),
                           ("systemctl", FAKE_SYSTEMCTL)):
            p = self.s / "bin" / name
            p.write_text(text.replace("@S@", str(self.s)))
            p.chmod(0o755)
        self.set("installed", OLD)
        self.pypi(NEW)
        self.status("idle")
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "TMPDIR": str(d),
            "YTDJ_EVENTS_FILE": str(self.events),
            "YTDLP_UPDATE_STATE_DIR": str(self.state_dir),
            "YTDLP_UPDATE_PY": sys.executable,
            "YTDLP_UPDATE_BIN": str(self.s / "bin" / "yt-dlp"),
            "YTDLP_UPDATE_UV": str(self.s / "bin" / "uv"),
            "YTDLP_UPDATE_CURL": str(self.s / "bin" / "curl"),
            "YTDLP_UPDATE_SYSTEMCTL": str(self.s / "bin" / "systemctl"),
            "YTDLP_UPDATE_WAIT_S": "0",
            "YTDLP_UPDATE_POLL_S": "1",
        }

    def tearDown(self):
        self.tmp.cleanup()

    # -- nastavení falešného světa --

    def set(self, name: str, text: str = "") -> None:
        (self.s / name).write_text(text + "\n" if text else "")

    def pypi(self, version: str) -> None:
        (self.s / "pypi.json").write_text(json.dumps({"info": {"version": version}}))

    def status(self, *seq: str) -> None:
        (self.s / "status.seq").write_text("\n".join(seq) + "\n")
        (self.s / "status.n").unlink(missing_ok=True)

    def checked_recently(self) -> None:
        self.state_dir.mkdir(exist_ok=True)
        (self.state_dir / "ytdlp-update.state").write_text(f"last_check={int(time.time())}\n")

    # -- běh a výsledky --

    def run_script(self, **extra: str) -> subprocess.CompletedProcess:
        (self.s / "calls.log").write_text("")
        env = dict(self.env, **extra)
        out = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True,
                             timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return out

    def calls(self) -> str:
        return (self.s / "calls.log").read_text()

    def installed(self) -> str:
        return (self.s / "installed").read_text().strip()

    def last_event(self) -> dict:
        lines = [json.loads(x) for x in self.events.read_text().splitlines() if x.strip()]
        self.assertTrue(lines)
        self.assertEqual({x["kind"] for x in lines}, {"ytdlp.update"})  # žádné session.*
        return lines[-1]

    def state(self) -> str:
        p = self.state_dir / "ytdlp-update.state"
        return p.read_text() if p.exists() else ""

    def restarts(self) -> int:
        return self.calls().count("restart ytdj.service")


class Decisions(Base):
    def test_healthy_installed_between_weekly_checks_does_nothing(self):
        self.checked_recently()
        self.run_script()
        ev = self.last_event()
        self.assertEqual(ev["action"], "none")
        self.assertIs(ev["before_ok"], True)
        self.assertEqual(ev["before_format"], "251")
        self.assertIs(ev["weekly"], False)
        self.assertNotIn("uv tool", self.calls())  # ani zkouška, ani instalace
        self.assertEqual(self.restarts(), 0)
        self.assertEqual(self.installed(), OLD)

    def test_weekly_newer_healthy_release_is_installed_and_jukebox_restarted_when_idle(self):
        self.run_script()
        ev = self.last_event()
        self.assertEqual((ev["action"], ev["old"], ev["new"], ev["restart"]),
                         ("upgraded", OLD, NEW, "done"))
        self.assertIs(ev["candidate_ok"], True)
        self.assertIs(ev["after_ok"], True)
        self.assertEqual(self.installed(), NEW)
        calls = self.calls()
        # zkouška bokem se stejnými pluginy, pak instalace s nimi; bgutil 1.3.1
        self.assertIn(f"uv tool run --from yt-dlp=={NEW} --with secretstorage "
                      "--with bgutil-ytdlp-pot-provider==1.3.1 yt-dlp", calls)
        self.assertIn(f"uv tool install --force yt-dlp>={NEW} --with secretstorage "
                      "--with bgutil-ytdlp-pot-provider==1.3.1", calls)
        self.assertLess(calls.index("uv tool run"), calls.index("uv tool install"))
        self.assertEqual(self.restarts(), 1)
        self.assertIn("last_check=", self.state())
        # další noc: týden ještě neuplynul, zdravé → nic
        self.run_script()
        self.assertEqual(self.last_event()["action"], "none")
        self.assertNotIn("uv tool", self.calls())

    def test_same_version_on_pypi_means_nothing_to_do(self):
        self.pypi(OLD)
        self.run_script()
        ev = self.last_event()
        self.assertEqual(ev["action"], "none")
        self.assertNotIn("uv tool run", self.calls())
        self.assertIn("last_check=", self.state())

    def test_newer_but_unhealthy_release_is_not_installed(self):
        self.set("broken", NEW)
        self.run_script()
        ev = self.last_event()
        self.assertEqual(ev["action"], "none")
        self.assertIs(ev["candidate_ok"], False)
        self.assertIn("Requested format is not available", ev["candidate_error"])
        self.assertNotIn("uv tool install", self.calls())
        self.assertEqual(self.installed(), OLD)
        self.assertEqual(self.restarts(), 0)
        self.assertNotIn("last_check=", self.state())  # zítra znovu (opravné vydání)

    def test_broken_after_install_rolls_back_and_skips_that_release(self):
        self.set("broken_installed", NEW)
        self.run_script()
        ev = self.last_event()
        self.assertEqual((ev["action"], ev["new"]), ("rolled_back", OLD))
        self.assertIs(ev["after_ok"], False)
        self.assertIs(ev["rollback_ok"], True)
        self.assertIn(f"uv tool install --force yt-dlp=={OLD} --with secretstorage "
                      "--with bgutil-ytdlp-pot-provider==1.3.1", self.calls())
        self.assertEqual(self.installed(), OLD)
        self.assertEqual(self.restarts(), 0)  # resolver drží starou verzi — není co načítat
        self.assertIn(f"bad_version={NEW}", self.state())
        # další noc se totéž vydání už neinstaluje
        self.run_script(YTDLP_UPDATE_FORCE="1")
        ev = self.last_event()
        self.assertEqual(ev["action"], "none")
        self.assertIn("nefungovalo", ev["reason"])
        self.assertNotIn("uv tool install", self.calls())

    def test_missing_plugin_after_install_is_not_kept(self):
        self.set("drop_plugin")  # uv by plugin bgutil vynechal
        self.run_script()
        ev = self.last_event()
        self.assertIn("chybí plugin", ev["reason"])
        self.assertIn(f"uv tool install --force yt-dlp=={OLD}", self.calls())  # návrat
        self.assertEqual(self.installed(), OLD)
        self.assertEqual(self.restarts(), 0)

    def test_failed_install_leaves_the_old_version(self):
        self.set("install_fail")
        self.run_script()
        ev = self.last_event()
        self.assertEqual(ev["action"], "failed")
        self.assertEqual(self.installed(), OLD)
        self.assertEqual(self.restarts(), 0)

    def test_installed_broken_on_a_weekday_tries_the_newest_release_at_once(self):
        self.checked_recently()
        self.set("broken", OLD)
        self.status("outage")  # hudba stojí — jukebox hlásí výpadek
        self.run_script()
        ev = self.last_event()
        self.assertIs(ev["weekly"], False)
        self.assertIs(ev["before_ok"], False)
        self.assertIn("Requested format is not available", ev["before_error"])
        self.assertEqual((ev["action"], ev["new"], ev["restart"]), ("upgraded", NEW, "done"))
        self.assertEqual(self.restarts(), 1)

    def test_installed_broken_and_nothing_newer_is_reported_as_failed(self):
        self.checked_recently()
        self.set("broken", OLD)
        self.pypi(OLD)
        self.run_script()
        ev = self.last_event()
        self.assertEqual(ev["action"], "failed")
        self.assertEqual(self.restarts(), 0)

    def test_pypi_unreachable_is_failed_not_a_crash(self):
        (self.s / "pypi.json").unlink()
        self.run_script()
        self.assertEqual(self.last_event()["action"], "failed")
        self.assertNotIn("uv tool", self.calls())

    def test_one_vanished_video_does_not_make_ytdlp_unhealthy(self):
        self.checked_recently()
        out = self.run_script(YTDLP_UPDATE_VIDEOS="AAAAAAAAAAA BBBBBBBBBBB")
        self.assertIn("zdravé (AAAAAAAAAAA", out.stdout)
        self.assertEqual(self.calls().count("yt-dlp installed"), 1)  # první stačila


class Playing(Base):
    def test_music_playing_until_the_deadline_installs_nothing(self):
        self.status("playing")
        self.run_script(YTDLP_UPDATE_WAIT_S="2")
        ev = self.last_event()
        self.assertEqual((ev["action"], ev["restart"]), ("skipped_playing", "skipped_playing"))
        self.assertIs(ev["candidate_ok"], True)
        self.assertEqual(self.installed(), OLD)
        self.assertNotIn("uv tool install", self.calls())
        self.assertEqual(self.restarts(), 0)
        self.assertGreaterEqual(self.calls().count("status playing"), 2)  # zkoušelo znovu
        # příští noc je ticho → nainstaluje a restartuje
        self.status("idle")
        self.run_script()
        self.assertEqual(self.last_event()["action"], "upgraded")
        self.assertEqual(self.restarts(), 1)

    def test_waits_while_playing_and_upgrades_once_paused(self):
        self.status("playing", "playing", "paused")
        self.run_script(YTDLP_UPDATE_WAIT_S="30")
        ev = self.last_event()
        self.assertEqual((ev["action"], ev["restart"]), ("upgraded", "done"))
        self.assertEqual(self.restarts(), 1)

    def test_status_unreachable_counts_as_busy(self):
        self.status("down")
        self.run_script()
        self.assertEqual(self.last_event()["action"], "skipped_playing")
        self.assertEqual(self.installed(), OLD)

    def test_jukebox_not_running_is_upgraded_without_restart(self):
        self.set("inactive")
        self.run_script()
        ev = self.last_event()
        self.assertEqual((ev["action"], ev["restart"]), ("upgraded", "not_running"))
        self.assertEqual(self.restarts(), 0)

    def test_restart_missed_because_music_started_is_done_the_next_night(self):
        self.checked_recently()
        self.set("broken", OLD)  # nemocný → instaluje se hned, restart čeká na ticho
        self.status("playing")
        self.run_script()
        ev = self.last_event()
        self.assertEqual((ev["action"], ev["restart"]), ("upgraded", "skipped_playing"))
        self.assertEqual(self.restarts(), 0)
        self.assertIn("pending_restart=1", self.state())
        # příští noc: nic nehraje → dlužný restart
        self.status("idle")
        self.run_script()
        ev = self.last_event()
        self.assertEqual((ev["action"], ev["restart"]), ("restarted", "done"))
        self.assertIs(ev["pending_restart"], True)
        self.assertEqual(self.restarts(), 1)
        self.assertNotIn("pending_restart", self.state())

    def test_pending_restart_is_dropped_when_jukebox_restarted_meanwhile(self):
        self.state_dir.mkdir()
        (self.state_dir / "ytdlp-update.state").write_text(
            f"last_check={int(time.time())}\npending_restart=1\nupgraded_at={int(time.time()) - 3600}\n")
        self.set("installed", NEW)
        self.set("active_enter", time.strftime("%a %Y-%m-%d %H:%M:%S %Z"))
        self.run_script()
        self.assertEqual(self.restarts(), 0)
        self.assertNotIn("pending_restart", self.state())


class Safety(Base):
    def test_lock_prevents_a_second_run(self):
        self.state_dir.mkdir()
        with open(self.state_dir / "ytdlp-update.lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            out = self.run_script()
        self.assertIn("už běží", out.stdout)
        self.assertEqual(self.calls(), "")
        self.assertFalse(self.events.exists())

    def test_no_cookies_tokens_or_stream_urls_leave_the_script(self):
        self.run_script()
        self.assertEqual(self.last_event()["action"], "upgraded")
        out = self.run_script(YTDLP_UPDATE_FORCE="1")
        text = out.stdout + out.stderr + self.events.read_text() + self.state()
        for secret in ("SECRETCOOKIE", "SECRETTOKEN", "googlevideo", "SECRETURL", "po_token"):
            self.assertNotIn(secret, text)
        # yt-dlp dostal volby z configu (cookies) — ale jen kopii jaru
        calls = self.calls()
        self.assertIn("cookies=", calls)
        self.assertNotIn(f"cookies={self.cookies}", calls)
        self.assertNotIn("cookies=\n", calls)

    def test_runs_at_lowest_priority(self):
        text = SCRIPT.read_text()
        self.assertIn("renice -n 19", text)
        self.assertIn("ionice -c3", text)
        unit = (ROOT / "packaging" / "rpi" / "ytdj-ytdlp-update.service").read_text()
        for line in ("Nice=19", "IOSchedulingClass=idle", "Type=oneshot"):
            self.assertIn(line, unit)


class Units(unittest.TestCase):
    def test_timer_daily_at_night_persistent_and_deploy_enables_it(self):
        timer = (ROOT / "packaging" / "rpi" / "ytdj-ytdlp-update.timer").read_text()
        self.assertIn("OnCalendar=*-*-* 03:40", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("RandomizedDelaySec=", timer)
        deploy = (ROOT / "packaging" / "rpi" / "deploy.sh").read_text()
        self.assertIn("ytdj-ytdlp-update.timer", deploy)
        self.assertIn("enable --now ytdj-ytdlp-update.timer", deploy)
        install = (ROOT / "packaging" / "install-service.sh").read_text()
        self.assertIn("ytdj-ytdlp-update.timer", install)

    def test_deploy_block_installs_units_idempotently(self):
        """Blok z deploy.sh dvakrát po sobě v dočasném HOME: poprvé instaluje
        a zapne časovač, podruhé nic nemění (žádný daemon-reload)."""
        deploy = (ROOT / "packaging" / "rpi" / "deploy.sh").read_text()
        start = deploy.index("# --- ytdlp-update begin")
        end = deploy.index("# --- ytdlp-update end")
        block = deploy[start:end]
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            repo = home / "ytdj"
            (repo / "packaging" / "rpi").mkdir(parents=True)
            for name in ("ytdj-ytdlp-update.service", "ytdj-ytdlp-update.timer",
                         "cpu-affinity.conf"):
                src = ROOT / "packaging" / "rpi" / name
                (repo / "packaging" / "rpi" / name).write_text(src.read_text())
            log = Path(d) / "sc.log"
            fake = Path(d) / "systemctl"
            fake.write_text(f'#!/bin/sh\necho "$@" >> {log}\n')
            fake.chmod(0o755)
            script = Path(d) / "block.sh"
            script.write_text("set -euo pipefail\nsystemctl() { \"$FAKE_SC\" \"$@\"; }\n"
                              "cd \"$HOME/$DEST\"\n" + block)
            env = dict(os.environ, HOME=str(home), DEST="ytdj", FAKE_SC=str(fake))
            for _ in range(2):
                out = subprocess.run(["bash", str(script)], env=env, capture_output=True,
                                     text=True, timeout=20)
                self.assertEqual(out.returncode, 0, out.stderr)
            calls = log.read_text().splitlines()
            self.assertEqual(calls.count("--user daemon-reload"), 1)
            self.assertIn("--user enable --now ytdj-ytdlp-update.timer", calls)
            unit = (home / ".config/systemd/user/ytdj-ytdlp-update.service").read_text()
            self.assertIn(f"{repo}/packaging/rpi/ytdlp-update.sh", unit)
            self.assertNotIn("@INSTALL_DIR@", unit)
            self.assertTrue((home / ".config/systemd/user/ytdj-ytdlp-update.service.d"
                             / "50-cpu-affinity.conf").exists())


class Helper(unittest.TestCase):
    def test_versions_pypi_and_status(self):
        from ytdj import ytdlp_update as h

        self.assertTrue(h.newer("2026.11.23", "2026.08.19"))
        self.assertTrue(h.newer("2026.11.23.1", "2026.11.23"))
        self.assertTrue(h.newer("2026.11.23", "2026.8.19"))  # PyPI píše bez nul
        self.assertFalse(h.newer("2026.8.19", "2026.08.19"))
        self.assertFalse(h.newer("", "2026.08.19"))
        self.assertEqual(h.pypi_latest({"info": {"version": "2026.11.23"}}), "2026.11.23")
        with self.assertRaises(ValueError):
            h.pypi_latest({"info": {"version": "1.0; rm -rf"}})
        self.assertEqual(h.status_word({"playing": True, "paused": False}), "busy")
        self.assertEqual(h.status_word({"playing": True, "paused": True}), "idle")
        self.assertEqual(h.status_word({"playing": False}), "idle")
        self.assertEqual(h.status_word({"playing": False, "starting": True}), "busy")
        self.assertEqual(h.status_word({"playing": True, "outage": {"kind": "x"}}), "outage")
        self.assertEqual(h.parse_fields(["a=1", "b:=true", "c:=x", "d=e:=f"]),
                         {"a": "1", "b": True, "c": "x", "d": "e:=f"})


if __name__ == "__main__":
    unittest.main()
