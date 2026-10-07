"""Péče o panel telky (POZADAVKY #77): vypínání telky přes HDMI-CEC, když se
nehraje, posun a střídání statických částí, tlumený QR kód a spořič.

Jako testy panelu systémovým pythonem (Pillow):

    YTDJ_EVENTS_FILE=/tmp/x.jsonl python3 -m unittest tests.test_panel_tv_care
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if not os.environ.get("YTDJ_EVENTS_FILE"):
    _EVENTS_TMP = tempfile.TemporaryDirectory(prefix="ytdj-tvcare-tests-")
    os.environ["YTDJ_EVENTS_FILE"] = str(Path(_EVENTS_TMP.name) / "events.jsonl")

from ytdj.tv import app as tvapp  # noqa: E402
from ytdj.tv import cec, screen  # noqa: E402
from ytdj.tv.cec import Cec, Listener, TvPower, Wish, parse_listen, wish_from  # noqa: E402
from ytdj.tv.fb import PngScreen  # noqa: E402
from ytdj.tv.screen import Renderer, view_from  # noqa: E402
from tv_shots import NOW, SIZES, WISH  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="ytdj-tvcare-test-"))
OURS, OTHER = "3.0.0.0", "2.0.0.0"

FAKE = r'''#!/bin/bash
# Falešný cec-ctl: telka je soubor se stavem; zapisuje, co se jí poslalo.
dir="$(dirname "$0")"
echo "$*" >> "$dir/calls"
[ -e "$dir/dead" ] && exit 1          # adaptér / telka neodpovídá vůbec
power="$(cat "$dir/power" 2>/dev/null || echo on)"
source="$(cat "$dir/source" 2>/dev/null)"
case "$*" in
  *--wait-for-msgs*) exec cat "$dir/bus" ;;   # poslech sběrnice: co test napíše do roury
  *--playback*) printf '\tPhysical Address           : 3.0.0.0\n\tOSD Name : Jukebox\n' ;;
  *--give-device-power-status*)
     [ -e "$dir/mute" ] && exit 0     # CEC na telce vypnuté: žádná odpověď
     printf 'GIVE_DEVICE_POWER_STATUS (0x8f)\n    Received from TV (0):\n    REPORT_POWER_STATUS (0x90):\n\tpwr-state: %s (0x00)\n' "$power" ;;
  *--request-active-source*)
     # jakmile jsme zdroj my, neodpoví nikdo (tak to dělá skutečná telka)
     [ -n "$source" ] && [ "$source" != 3.0.0.0 ] && printf 'REQUEST_ACTIVE_SOURCE (0x85)\n    Received from TV (0):\n    ACTIVE_SOURCE (0x82):\n\tphys-addr: %s\n' "$source" ;;
  *--report-power-status*|*--feature-abort*) ;;
  *--standby*) [ -e "$dir/fail" ] && exit 1; [ -e "$dir/deaf" ] || echo standby > "$dir/power" ;;
  *--image-view-on*) [ -e "$dir/fail" ] && exit 1; [ -e "$dir/deaf" ] || echo on > "$dir/power" ;;
  *--active-source*) [ -e "$dir/fail" ] && exit 1; echo 3.0.0.0 > "$dir/source" ;;
esac
exit 0
'''

# Co je slyšet na sběrnici (tvar řádků z `cec-ctl --wait-for-msgs` na Pi, 7. 10. 2026)
TV_STANDBY = "Received from TV to all (0 to 15): STANDBY (0x36)\n"
ASKS_POWER = "Received from TV to Playback Device 2 (0 to 8): GIVE_DEVICE_POWER_STATUS (0x8f)\n"
ASKS_DECK = ("Received from TV to Playback Device 2 (0 to 8): GIVE_DECK_STATUS (0x1a):\n"
             "\tstatus-req: on (0x01)\n")


def set_path(addr: str) -> str:
    return f"Received from TV to all (0 to 15): SET_STREAM_PATH (0x86):\n\tphys-addr: {addr}\n"


def routing(old: str, new: str) -> str:
    return ("Received from TV to all (0 to 15): ROUTING_CHANGE (0x80):\n"
            f"\torig-phys-addr: {old}\n\tnew-phys-addr: {new}\n")


def active(addr: str, la: int = 4) -> str:
    who = "TV" if la == 0 else "Playback Device 1"
    return f"Received from {who} to all ({la} to 15): ACTIVE_SOURCE (0x82):\n\tphys-addr: {addr}\n"


class Clock:
    def __init__(self):
        self.t = 5000.0

    def __call__(self):
        return self.t


class Tv:
    """Falešná telka + TvPower nad skutečnou třídou Cec."""

    def __init__(self, power="on", source="0.0.0.0", state_file=None, listen=False):
        self.dir = Path(tempfile.mkdtemp(dir=_TMP))
        tool = self.dir / "cec-ctl"
        tool.write_text(FAKE)
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
        self.tool = str(tool)
        self.set(power=power, source=source)
        self.clock = Clock()
        self.events: list[tuple[str, dict]] = []
        self.state_file = state_file or self.dir / "cec-state.json"
        self.listen, self.fd = listen, None
        if listen:
            os.mkfifo(self.dir / "bus")
            self.plug()
        self.pw = self.new_power()

    def new_power(self) -> TvPower:
        c = Cec(tool=self.tool)
        c.available = lambda: True
        listener = Listener(c.listen_command, answer=c.answer, pause=0.05) if self.listen else None
        return TvPower(c, clock=self.clock, emit=lambda k, **f: self.events.append((k, f)),
                       state_file=self.state_file, listener=listener)

    # ---- skutečný poslech (roura místo sběrnice) ----

    def plug(self):
        self.fd = os.open(self.dir / "bus", os.O_RDWR)

    def unplug(self):
        """Poslech spadne (roura se zavře → dítě skončí)."""
        os.close(self.fd)
        self.fd = None

    def say(self, text: str, n: int = 1):
        """Telka něco řekne na sběrnici; počká, až to poslech zachytí."""
        q = self.pw.listener.events
        before = q.qsize()
        os.write(self.fd, text.encode())
        self.until(lambda: q.qsize() >= before + n)

    def until(self, cond, timeout=5.0):
        end = time.monotonic() + timeout
        while not cond():
            if time.monotonic() > end:
                raise AssertionError("podmínka nenastala")
            time.sleep(0.01)

    def close(self):
        if self.pw.listener is not None:
            self.pw.listener.shutdown()
        if self.fd is not None:
            self.unplug()

    # ---- bez vlákna: rovnou to, co by poslech předal ----

    def bus(self, text: str):
        self.pw.hear(parse_listen(text.splitlines(keepends=True)))

    def set(self, power=None, source=None):
        if power is not None:
            (self.dir / "power").write_text(power)
        if source is not None:
            (self.dir / "source").write_text(source)

    def flag(self, name: str, on: bool = True):
        (self.dir / name).touch() if on else (self.dir / name).unlink(missing_ok=True)

    def calls(self) -> list[str]:
        f = self.dir / "calls"
        return f.read_text().splitlines() if f.exists() else []

    def sent(self) -> list[str]:
        """Jen to, co telku ovládá (ne dotazy a odpovědi)."""
        out = []
        for c in self.calls():
            for word in ("--standby", "--image-view-on", "--active-source"):
                if word in c and "--request-active-source" not in c:
                    out.append(word.lstrip("-"))
        return out

    def asked(self, what: str = "--give-device-power-status") -> int:
        return sum(1 for c in self.calls() if what in c)

    def power(self) -> str:
        return (self.dir / "power").read_text().strip()

    def step(self, active=False, idle_min=0.0, enabled=True, minutes=10, dt=61.0,
             command=None) -> str:
        self.clock.t += dt
        return self.pw.step(Wish(enabled=enabled, minutes=minutes, active=active,
                                 idle_s=idle_min * 60, command=command))

    def actions(self) -> list[tuple]:
        return [(f.get("action"), f.get("ok"), f.get("skipped")) for k, f in self.events
                if k == "tv.cec"]

    def routes(self) -> list[tuple]:
        return [(f.get("route"), f.get("why")) for k, f in self.events if k == "tv.cec_route"]


class CecTool(unittest.TestCase):
    def test_messages_and_answers(self):
        tv = Tv()
        c = Cec(tool=tv.tool)
        self.assertEqual(c.configure(), OURS)
        self.assertEqual(c.power(), "on")
        self.assertEqual(c.active_source(), "0.0.0.0")
        self.assertTrue(c.standby())
        self.assertEqual((tv.power(), c.power()), ("standby", "standby"))
        self.assertTrue(c.wake(OURS))
        # jakmile jsme zdroj my, na dotaz „kdo je zdroj“ neodpoví nikdo
        self.assertEqual((tv.power(), c.active_source()), ("on", None))
        calls = tv.calls()
        self.assertIn("-d /dev/cec0 --playback --osd-name Jukebox", calls[0])
        self.assertIn("-d /dev/cec0 -s --to 0 --give-device-power-status", calls)
        self.assertIn("-d /dev/cec0 -s --to 0 --standby", calls)
        self.assertIn("-d /dev/cec0 -s --to 0 --image-view-on", calls)
        self.assertIn("-d /dev/cec0 -s --active-source phys-addr=3.0.0.0", calls)
        # poslech: bez práv správce (--wait-for-msgs, ne --monitor) a po řádcích
        cmd = c.listen_command()
        self.assertEqual(cmd[-7:-1], [tv.tool, "-d", "/dev/cec0", "-s", "--wait-for-msgs",
                                      "--monitor-time"])
        self.assertIn("--wait-for-msgs", cmd)
        self.assertNotIn("--monitor", cmd)
        self.assertNotIn("-m", cmd)
        if cmd[0] != tv.tool:
            self.assertEqual(cmd[:2], ["stdbuf", "-oL"])
        # telka neodpovídá / nástroj selže / nástroj není → None, žádná výjimka
        tv.flag("mute")
        self.assertIsNone(c.power())
        tv.flag("dead")
        self.assertIsNone(c.configure())
        self.assertFalse(c.standby())
        self.assertIsNone(Cec(tool=str(tv.dir / "neni")).power())
        self.assertFalse(Cec(tool="tenhle-nastroj-neexistuje").available())

    def test_what_the_jukebox_wants_of_the_tv(self):
        st = {"current": {"id": "x"}, "paused": False,
              "tv": {"standby": {"on": True, "minutes": 15}}}
        w = wish_from(st, 999.0)
        self.assertEqual((w.enabled, w.minutes, w.active, w.idle_s), (True, 15, True, 0.0))
        self.assertIsNone(w.command)
        w = wish_from({**st, "paused": True}, 700.0)
        self.assertEqual((w.active, w.idle_s), (False, 700.0))
        # někdo právě poslal přání / zmáčkl Hrát → telka má být vzhůru dřív, než to hraje
        self.assertTrue(wish_from({"current": None, "dj": {"busy": True}, "tv": {}}, 900.0).active)
        self.assertTrue(wish_from({"current": None, "starting": True}, 900.0).active)
        for junk in (None, [], {"tv": "x"}, {"tv": {"standby": {"on": "ano", "minutes": "x"}}}):
            w = wish_from(junk, 5.0)
            self.assertEqual((w.enabled, w.minutes, w.active), (False, 10, False))
        self.assertEqual(wish_from({"tv": {"standby": {"on": True, "minutes": 99999}}}, 0).minutes, 240)
        # tlačítko na webu: povel přijde ve stavu; nesmysl se nebere
        ask = lambda req: wish_from({"tv": {"power_request": req}}, 0).command  # noqa: E731
        self.assertEqual(ask({"id": 17, "action": "off"}), (17, "off"))
        self.assertEqual(ask({"id": 18, "action": "on"}), (18, "on"))
        for junk in (None, "off", {"id": "x", "action": "off"}, {"id": 3, "action": "reboot"}):
            self.assertIsNone(ask(junk))


class Polite(unittest.TestCase):
    """Telka patří lidem: jukebox ji vypíná a zapíná jen tehdy, když nikomu nesahá do díla."""

    def test_off_in_settings_means_the_tv_is_never_touched(self):
        tv = Tv()
        for active, idle in ((True, 0), (False, 30), (False, 600), (True, 0)):
            tv.step(active=active, idle_min=idle, enabled=False)
        self.assertEqual(tv.sent(), [])  # nic, co by telku ovládalo
        # ptá se jen na stav (aby web věděl, jestli je zapnutá), a zřídka
        self.assertTrue(all("--give-device-power-status" in c or "--playback" in c
                            for c in tv.calls()), tv.calls())
        self.assertLessEqual(tv.asked(), 3)
        self.assertEqual(tv.pw.note, "")
        self.assertEqual(tv.pw.report()["power_tv"], "on")

    def test_sleeps_after_the_set_minutes_and_wakes_with_the_music(self):
        tv = Tv(power="on", source="0.0.0.0")
        tv.step(active=True)  # hudba začala, telka je zapnutá a nic jiného neukazuje
        self.assertEqual(tv.sent(), ["active-source"])  # jednou řekne „to jsem já“
        self.assertEqual(tv.pw.route, "ours")
        tv.step(active=True)
        tv.step(active=True)
        self.assertEqual(tv.sent(), ["active-source"])
        tv.step(active=False, idle_min=4)  # pauza 4 minuty: nic
        tv.step(active=False, idle_min=9)
        self.assertEqual(tv.sent(), ["active-source"])
        note = tv.step(active=False, idle_min=10)
        self.assertEqual((tv.sent()[-1], tv.power()), ("standby", "standby"))
        self.assertIn("Telku jsem vypnul", note)
        self.assertTrue(tv.pw.we_slept)
        self.assertEqual((tv.pw.report()["power_tv"], tv.pw.report()["power_by"]),
                         ("standby", "jukebox"))
        n = len(tv.sent())
        for idle in (11, 30, 120):  # spí: žádné další povely
            tv.step(active=False, idle_min=idle)
        self.assertEqual(len(tv.sent()), n)
        tv.step(active=True)  # někdo pustil hudbu / poslal přání
        self.assertEqual(tv.sent()[-2:], ["image-view-on", "active-source"])
        self.assertEqual(tv.power(), "on")
        self.assertFalse(tv.pw.we_slept)
        self.assertEqual(tv.pw.route, "ours")  # po probuzení ukazuje nás
        self.assertEqual([a for a, ok, _ in tv.actions() if ok], ["claim", "standby", "wake"])
        # a příště zase usne (není to jednorázové)
        tv.step(active=False, idle_min=10)
        self.assertEqual(tv.power(), "standby")

    def test_standby_goes_out_although_nobody_answers_who_the_source_is(self):
        """Přesně to, co se stalo 7. 10. ve 20:49: ohlášení se povedlo, pak se na dotaz
        „kdo je zdroj“ neozval nikdo (zdroj jsme my) a telka se nikdy nevypnula."""
        tv = Tv(power="on", source="0.0.0.0")
        tv.step(active=True)
        self.assertEqual([(a, ok) for a, ok, _ in tv.actions()], [("claim", True)])
        self.assertIsNone(tv.pw.cec.active_source())  # nikdo neodpovídá — a nevadí to
        asked = tv.asked("--request-active-source")
        note = tv.step(active=False, idle_min=10)
        self.assertEqual(tv.power(), "standby")
        self.assertIn("Telku jsem vypnul", note)
        # rozhodnutí se o ten dotaz vůbec neopírá
        self.assertEqual(tv.asked("--request-active-source"), asked)
        self.assertNotIn("not_our_picture", [s for _, _, s in tv.actions()])

    def test_somebody_watching_another_input_is_left_alone(self):
        tv = Tv(power="on", source="0.0.0.0")
        tv.step(active=True)
        tv.bus(routing(OURS, OTHER))  # kolega přepnul na jiné zařízení
        self.assertEqual((tv.pw.route, tv.pw.route_addr), ("other", OTHER))
        note = tv.step(active=False, idle_min=15)
        self.assertNotIn("standby", tv.sent())
        self.assertIn("jiný vstup", note)
        n = len(tv.calls())
        for _ in range(4):  # a neptá se každou minutu
            tv.step(active=False, idle_min=20)
        self.assertLessEqual(len(tv.calls()) - n, 2)
        self.assertEqual(tv.actions()[-1], ("standby", False, "other_input"))
        # hudba začne, zatímco se dívají jinam → nepřepínat jim to
        tv.step(active=True)
        tv.step(active=False, idle_min=0)
        tv.step(active=True)
        self.assertEqual(tv.sent(), ["active-source"])
        # přepnuli zpátky na jukebox → zase se smí vypnout
        tv.bus(routing(OTHER, OURS))
        self.assertEqual(tv.pw.route, "ours")
        tv.step(active=False, idle_min=30, dt=301)
        self.assertEqual((tv.sent()[-1], tv.power()), ("standby", "standby"))
        # jiné zařízení se samo ohlásí za zdroj / telka přepne na svůj obraz (anténa, aplikace)
        for line in (active(OTHER), active("0.0.0.0", la=0), set_path(OTHER)):
            tv2 = Tv()
            tv2.step(active=True)
            tv2.bus(line)
            tv2.step(active=False, idle_min=15)
            self.assertEqual(tv2.sent(), ["active-source"], line)
            tv2.bus(set_path(OURS))  # telka si vybrala náš vstup
            tv2.step(active=False, idle_min=30, dt=301)
            self.assertEqual(tv2.power(), "standby", line)
        # nevíme, co ukazuje, a jiné zařízení na dotaz řekne „já“ → neohlašovat se
        tv3 = Tv(power="on", source=OTHER)
        tv3.step(active=True)
        self.assertEqual(tv3.sent(), [])
        self.assertEqual(tv3.pw.route, "other")
        tv3.step(active=False, idle_min=30)
        self.assertEqual(tv3.sent(), [])

    def test_unknown_picture_means_nothing_is_sent_and_the_reason_is_honest(self):
        tv = Tv()  # hudba od startu nehrála: nevíme, co telka ukazuje
        note = tv.step(active=False, idle_min=30)
        self.assertEqual(tv.sent(), [])
        self.assertEqual(tv.pw.route, "unknown")
        self.assertIn("nevím, co zrovna ukazuje", note)
        self.assertNotIn("neukazuje jukebox", note)  # to bychom tvrdili něco, co nevíme
        self.assertEqual(tv.actions()[-1], ("standby", False, "route_unknown"))
        for _ in range(5):
            tv.step(active=False, idle_min=60)
        self.assertEqual(tv.sent(), [])
        self.assertEqual([s for _, _, s in tv.actions()].count("route_unknown"), 1)  # do logu jednou
        # hudba začne → jedno ohlášení a od té chvíle to víme
        tv.step(active=True)
        self.assertEqual((tv.sent(), tv.pw.route), (["active-source"], "ours"))
        self.assertEqual(tv.routes()[-1], ("ours", "claim"))

    def test_tv_switched_off_by_people_stays_off_and_one_switched_on_is_theirs(self):
        tv = Tv()
        tv.step(active=True)
        tv.set(power="standby")
        tv.bus(TV_STANDBY)  # vypnutá ovladačem: telka to řekne všem
        self.assertTrue(tv.pw.people_off)
        for act in (False, True, True, False, True):
            tv.step(active=act)
        self.assertEqual(tv.sent(), ["active-source"])  # nebudit: nevypnuli jsme ji my
        self.assertEqual(tv.pw.report()["power_by"], "people")
        # ovladačem zapnutá na našem vstupu → zase se o ni staráme
        tv.set(power="on")
        tv.bus(set_path(OURS))
        self.assertFalse(tv.pw.people_off)
        tv.step(active=False, idle_min=10)
        self.assertEqual(tv.power(), "standby")
        self.assertTrue(tv.pw.we_slept)
        # vypnutá už při startu (poslech nic neřekl, řekne to dotaz na stav)
        tv = Tv(power="standby")
        tv.step(active=True)
        tv.step(active=True)
        self.assertEqual(tv.sent(), [])
        self.assertTrue(tv.pw.people_off)
        tv.set(power="on")  # zapnutá ovladačem, na sběrnici nic
        tv.step(active=False, dt=cec.POWER_EVERY)
        self.assertFalse(tv.pw.people_off)
        self.assertEqual(tv.pw.route, "unknown")
        # my jsme ji uspali, někdo ji mezitím zapnul a dívá se na něco jiného
        tv = Tv()
        tv.step(active=True)
        tv.step(active=False, idle_min=10)
        self.assertEqual(tv.power(), "standby")
        tv.set(power="on", source=OTHER)
        tv.bus(routing("0.0.0.0", OTHER))
        tv.step(active=True)
        self.assertEqual(tv.sent(), ["active-source", "standby"])  # nic dalšího
        self.assertFalse(tv.pw.we_slept)
        # totéž, když poslech přepnutí neslyšel: zeptá se a jinému zařízení do obrazu nevleze
        tv = Tv()
        tv.step(active=True)
        tv.step(active=False, idle_min=10)
        tv.set(power="on", source=OTHER)
        tv.step(active=True)
        tv.step(active=False)
        tv.step(active=True)
        self.assertEqual(tv.sent(), ["active-source", "standby"])
        self.assertEqual((tv.pw.we_slept, tv.pw.route), (False, "other"))

    def test_setting_switched_off_while_the_tv_sleeps_still_wakes_it(self):
        tv = Tv()
        tv.step(active=True)
        tv.step(active=False, idle_min=10)
        self.assertEqual(tv.power(), "standby")
        tv.step(active=True, enabled=False)  # mezitím to někdo v nastavení vypnul
        self.assertEqual(tv.power(), "on")  # telka nezůstane tmavá naší vinou
        n = len(tv.sent())
        tv.step(active=False, idle_min=60, enabled=False)
        self.assertEqual(len(tv.sent()), n)

    def test_sleep_survives_a_restart_of_the_process(self):
        tv = Tv()
        tv.step(active=True)
        tv.step(active=False, idle_min=10)
        self.assertEqual(json.loads(Path(tv.state_file).read_text()),
                         {"we_slept": True, "people_off": False, "done_id": 0})
        tv.pw = tv.new_power()  # nasazení / restart služby, telka spí
        self.assertTrue(tv.pw.we_slept)
        self.assertEqual(tv.pw.route, "unknown")  # co ukazuje, si přes restart nepamatuje
        tv.step(active=True)
        self.assertEqual(tv.power(), "on")
        Path(tv.state_file).write_text("{rozbité")
        self.assertFalse(tv.new_power().we_slept)


class NoStorms(unittest.TestCase):
    def test_no_answer_falls_back_to_on_screen_care_and_asks_rarely(self):
        tv = Tv()
        tv.step(active=True)
        tv.flag("mute")  # Anynet+ vypnuté: telka na CEC mlčí
        note = tv.step(active=False, idle_min=10)
        self.assertEqual(tv.pw.state, "no_answer")
        self.assertIn("Anynet+", note)
        self.assertIn("úpravou obrazu", note)
        self.assertEqual(tv.pw.report()["power_tv"], "none")
        n = len(tv.calls())
        for _ in range(20):  # 20 minut: žádné další dotazy
            tv.step(active=False, idle_min=30)
        self.assertEqual(len(tv.calls()), n)
        tv.flag("mute", False)
        tv.clock.t += cec.PROBE_EVERY
        tv.step(active=False, idle_min=60)  # po půl hodině to zkusí znovu
        self.assertGreater(len(tv.calls()), n)
        self.assertNotEqual(tv.pw.state, "no_answer")
        self.assertIn(tv.pw.report()["power_tv"], ("on", "standby"))
        # jedna ztracená odpověď na běžný dotaz ještě neznamená „CEC je vypnuté“
        tv4 = Tv()
        tv4.step(active=False, enabled=False)
        tv4.flag("mute")
        tv4.step(active=False, enabled=False, dt=cec.POWER_EVERY)
        self.assertEqual(tv4.pw.report()["power_tv"], "on")
        tv4.step(active=False, enabled=False, dt=30)
        self.assertEqual((tv4.pw.state, tv4.pw.report()["power_tv"]), ("no_answer", "none"))
        # bez nástroje nebo bez telky s CEC: nic se neposílá a stav to řekne
        tv2 = Tv()
        tv2.pw.cec.available = lambda: False
        tv2.step(active=False, idle_min=30)
        self.assertEqual((tv2.pw.state, tv2.calls()), ("no_cec", []))
        tv3 = Tv()
        tv3.flag("dead")
        tv3.step(active=False, idle_min=30)
        self.assertEqual(tv3.pw.state, "no_cec")
        self.assertEqual(tv3.sent(), [])

    def test_one_attempt_one_retry_then_a_long_pause(self):
        tv = Tv()
        tv.step(active=True)
        tv.flag("fail")  # telka zprávu nepřijme
        tv.step(active=False, idle_min=10)
        self.assertEqual(tv.sent().count("standby"), 1)
        tv.step(active=False, idle_min=10, dt=cec.RETRY_AFTER + 1)
        self.assertEqual(tv.sent().count("standby"), 2)  # jedno zopakování
        for _ in range(8):  # 8 minut: ticho
            tv.step(active=False, idle_min=20)
        self.assertEqual(tv.sent().count("standby"), 2)
        self.assertFalse(tv.pw.we_slept)
        tv.flag("fail", False)
        tv.step(active=False, idle_min=30, dt=cec.BACKOFF)
        self.assertEqual(tv.power(), "standby")
        self.assertEqual([ok for a, ok, _ in tv.actions() if a == "standby"], [False, False, True])

    def test_never_two_actions_within_a_minute(self):
        tv = Tv()
        tv.step(active=True)
        tv.step(active=False, idle_min=10, dt=5.0)  # hned po „to jsem já“: ještě ne
        self.assertNotIn("standby", tv.sent())
        tv.step(active=False, idle_min=10, dt=60.0)
        self.assertIn("standby", tv.sent())
        tv.step(active=True, dt=5.0)  # hudba 5 s po uspání: počká se do minuty
        self.assertEqual(tv.power(), "standby")
        tv.step(active=True, dt=60.0)
        self.assertEqual(tv.power(), "on")
        self.assertGreaterEqual(cec.ACTION_GAP, 60)
        self.assertGreaterEqual(cec.BACKOFF, 300)
        # na stav se neptá častěji než jednou za dvě minuty
        tv = Tv()
        for _ in range(60):
            tv.step(active=False, enabled=False, dt=10.0)
        self.assertLessEqual(tv.asked(), 6)
        self.assertGreaterEqual(cec.POWER_EVERY, 120)

    def test_screen_loop_passes_idle_time_and_reports_the_state(self):
        seen: list[Wish] = []

        class Power:
            state, note = "standby", "Telku jsem vypnul — 10 min se nehrálo."

            def step(self, wish):
                seen.append(wish)
                return self.note

        class D:
            session, extra = None, {}

            def step(self, want):
                return "screen"

            def close(self):
                pass

        d = D()
        app = tvapp.TvApp(lambda: PngScreen(_TMP / "p.png", (720, 480)), "http://127.0.0.1:9",
                          "jukebox.local", director=d, power=Power())
        app._on_state({"current": None, "queue": [], "tv": {"standby": {"on": True, "minutes": 7}}})
        app._idle_since -= 500
        app.power_step()
        self.assertEqual((seen[-1].enabled, seen[-1].minutes, seen[-1].active), (True, 7, False))
        self.assertGreaterEqual(seen[-1].idle_s, 500)
        self.assertEqual(d.extra, {"power": "standby",
                                   "power_note": "Telku jsem vypnul — 10 min se nehrálo."})

        class Broken:
            def step(self, wish):
                raise RuntimeError("bum")

        app.power = Broken()
        app.power_step()  # vypínání telky nikdy neshodí obrazovku
        plain = tvapp.TvApp(lambda: PngScreen(_TMP / "p.png", (720, 480)), "http://127.0.0.1:9")
        plain.power_step()


class Bus(unittest.TestCase):
    """Co telka ukazuje, se pozná poslechem sběrnice — ne dotazem."""

    def test_lines_from_the_real_bus_are_understood(self):
        text = ("Initial Event: State Change: PA: 3.0.0.0, LA mask: 0x0100, Conn Info: yes\n"
                + ASKS_POWER + TV_STANDBY + ASKS_DECK + set_path(OURS)
                + "Received from Playback Device 1 to all (4 to 15): DEVICE_VENDOR_ID (0x87):\n"
                  "\tvendor-id: 240 (0x000000f0)\n"
                + routing(OURS, OTHER) + active(OTHER))
        ev = parse_listen(text.splitlines(keepends=True))
        self.assertEqual([(e.kind, e.addr, e.src) for e in ev], [
            ("address", OURS, -1), ("power_query", "", 0), ("standby", "", 0),
            ("unserved", "GIVE_DECK_STATUS", 0), ("route", OURS, 0),
            ("route", OTHER, 0),  # u přepnutí platí NOVÁ adresa, ne původní
            ("route", OTHER, 4)])
        self.assertEqual(ev[3].op, 0x1a)
        self.assertEqual(parse_listen(["nesmysl\n", "\tphys-addr: 1.0.0.0\n", ""]), [])
        # vypnutí, které ohlásí jiné zařízení než telka, o telce nic neříká
        tv = Tv()
        tv.step(active=True)
        tv.bus("Received from Playback Device 1 to all (4 to 15): STANDBY (0x36)\n")
        self.assertFalse(tv.pw.people_off)

    def test_listening_child_feeds_the_belief_and_every_change_is_logged(self):
        tv = Tv(listen=True)
        self.addCleanup(tv.close)
        tv.step(active=True)  # hudba: ohlášení → obraz je náš
        tv.until(lambda: tv.pw.listener.starts == 1)
        self.assertTrue(any("--wait-for-msgs" in c for c in tv.calls()))
        self.assertEqual(tv.pw.route, "ours")
        tv.say(routing(OURS, OTHER))
        tv.step(active=False, idle_min=15)
        self.assertEqual((tv.pw.route, tv.sent()), ("other", ["active-source"]))
        tv.say(set_path(OURS))
        tv.step(active=False, idle_min=30, dt=301)
        self.assertEqual(tv.power(), "standby")
        self.assertEqual(tv.routes(), [("ours", "claim"), ("other", "bus"), ("ours", "bus")])
        # telka se ptá přímo nás: odpovíme hned (z vlákna poslechu), co neumíme, odmítneme
        tv.say(ASKS_POWER + ASKS_POWER + ASKS_DECK, n=0)
        tv.until(lambda: tv.asked("--feature-abort") == 1)
        self.assertEqual(tv.asked("--report-power-status pwr-state=on"), 1)  # stejný dotaz jednou
        self.assertIn("-d /dev/cec0 -s --to 0 --feature-abort abort-msg=26,reason=unrecognized-op",
                      tv.calls())

    def test_listener_that_dies_is_restarted_and_nothing_is_sent_blindly(self):
        tv = Tv(listen=True)
        self.addCleanup(tv.close)
        tv.step(active=True)
        self.assertEqual(tv.pw.route, "ours")
        q = tv.pw.listener.events
        tv.until(lambda: tv.pw.listener.starts == 1)
        tv.unplug()  # poslech spadl: od teď jsme mohli přepnutí vstupu přeslechnout
        tv.until(lambda: any(e.addr == "down" for e in list(q.queue)))
        note = tv.step(active=False, idle_min=15)
        self.assertEqual(tv.pw.route, "unknown")
        self.assertEqual(tv.sent(), ["active-source"])  # naslepo nic
        self.assertIn("nevím, co zrovna ukazuje", note)
        self.assertEqual(tv.routes()[-1], ("unknown", "listener_down"))
        tv.plug()
        tv.until(lambda: tv.pw.listener.starts >= 2)  # pustil se znovu sám
        tv.say(set_path(OURS))
        tv.step(active=False, idle_min=30, dt=301)
        self.assertEqual((tv.pw.route, tv.power()), ("ours", "standby"))
        # ukončení služby: dítě nezůstane viset
        tv.pw.listener.shutdown()
        tv.pw.listener.join(timeout=5)
        self.assertFalse(tv.pw.listener.is_alive())
        self.assertIsNotNone(tv.pw.listener.proc.poll())

    def test_cable_unplugged_makes_the_picture_unknown(self):
        tv = Tv()
        tv.step(active=True)
        tv.bus("Event: State Change: PA: f.f.f.f, LA mask: 0x0000, Conn Info: yes\n")
        self.assertEqual((tv.pw.route, tv.pw.addr), ("unknown", None))
        tv.step(active=False, idle_min=30)
        self.assertEqual(tv.sent(), ["active-source"])


class WebButton(unittest.TestCase):
    """Telka z webu (POZADAVKY #78): Vypnout / Zapnout smí kdokoli; je to rozhodnutí člověka."""

    def press(self, tv, rid, action, **kw):
        """Povel + počkání na potvrzení od telky (po 5 s)."""
        kw.setdefault("dt", 61.0)
        tv.step(command=(rid, action), **kw)
        kw["dt"] = 5.0
        tv.step(command=(rid, action), **kw)
        return tv.pw.report()

    def test_off_from_the_web_is_a_persons_decision_music_does_not_wake_it(self):
        tv = Tv()
        tv.step(active=True)
        note = tv.step(active=True, command=(1, "off"))
        self.assertEqual((tv.sent()[-1], note), ("standby", "Posílám telce povel…"))
        self.assertTrue(tv.pw.pending)
        self.assertEqual(tv.pw.report()["power_busy"], 1)
        tv.step(active=True, command=(1, "off"), dt=2.0)  # ještě se neptá
        self.assertTrue(tv.pw.pending)
        tv.step(active=True, command=(1, "off"), dt=3.0)  # telka potvrdila
        r = tv.pw.report()
        self.assertEqual((r["power_tv"], r["power_by"], r["power_busy"]), ("standby", "people", 0))
        self.assertEqual((r["power_done_id"], r["power_done_action"], r["power_done_ok"]),
                         (1, "off", True))
        self.assertIn("vypnutá", r["power_done_note"])
        self.assertFalse(tv.pw.pending)
        n = len(tv.sent())
        for act in (True, False, True, True, False, True):  # hudba hraje, končí, začíná…
            tv.step(active=act, command=(1, "off"))
        self.assertEqual((len(tv.sent()), tv.power()), (n, "standby"))  # sama ji nezapne
        # pamatuje si to i přes restart služby
        tv.pw = tv.new_power()
        self.assertTrue(tv.pw.people_off)
        tv.step(active=False, command=(1, "off"))
        tv.step(active=True, command=(1, "off"))
        self.assertEqual(len(tv.sent()), n)  # a starý povel neprovede podruhé
        self.assertEqual([a for a, ok, _ in tv.actions() if a.startswith("web")],
                         ["web_off", "web_off_result"])

    def test_on_from_the_web_is_ours_to_manage_again(self):
        tv = Tv()
        tv.step(active=True)
        self.press(tv, 1, "off")
        r = self.press(tv, 2, "on")
        self.assertEqual(tv.sent()[-2:], ["image-view-on", "active-source"])  # zapnout + náš vstup
        self.assertEqual((tv.power(), r["power_tv"], r["power_by"], r["power_done_ok"]),
                         ("on", "on", "", True))
        self.assertEqual((tv.pw.people_off, tv.pw.route), (False, "ours"))
        tv.step(active=False, idle_min=10)  # nehraje se → vypne ji sám (je-li to zapnuté)
        self.assertEqual(tv.power(), "standby")
        self.assertTrue(tv.pw.we_slept)
        tv.step(active=True)  # a s hudbou ji zase probudí
        self.assertEqual(tv.power(), "on")
        # zapnout jde i telku, kterou jukebox nevypnul (ovladač), a i s vypnutým nastavením
        tv = Tv(power="standby")
        tv.step(active=False, enabled=False)
        r = self.press(tv, 5, "on", enabled=False)
        self.assertEqual((tv.power(), r["power_done_ok"]), ("on", True))
        r = self.press(tv, 6, "off", enabled=False)
        self.assertEqual((tv.power(), r["power_by"]), ("standby", "people"))

    def test_off_works_on_another_input_and_says_which(self):
        tv = Tv()
        tv.step(active=True)
        tv.bus(routing(OURS, OTHER))
        r = self.press(tv, 1, "off")
        self.assertEqual(tv.power(), "standby")
        self.assertIn("jiný vstup", r["power_done_note"])
        self.assertIn(OTHER, r["power_done_note"])
        ev = [f for k, f in tv.events if k == "tv.cec" and f.get("action") == "web_off"][0]
        self.assertEqual((ev["request"], ev["other_input"]), (1, OTHER))

    def test_one_command_at_a_time_and_ten_seconds_apart(self):
        tv = Tv()
        tv.step(active=True)
        self.press(tv, 1, "off")  # povel v čase 0, potvrzení v 5 s
        tv.step(command=(2, "on"), dt=1.0)  # 6 s po minulém: ještě ne
        self.assertEqual(tv.power(), "standby")
        self.assertFalse(tv.pw.pending)
        tv.step(command=(2, "on"), dt=4.0)  # 10 s: teď
        self.assertEqual(tv.sent()[-2:], ["image-view-on", "active-source"])
        n = len(tv.sent())
        tv.step(command=(3, "off"), dt=1.0)  # čeká se na potvrzení: další povel počká
        self.assertEqual(len(tv.sent()), n)
        tv.step(command=(3, "off"), dt=4.0)
        self.assertEqual(tv.pw.report()["power_done_id"], 2)
        tv.step(command=(2, "on"), dt=60.0)  # starší nebo stejné číslo se neprovede
        self.assertEqual(len(tv.sent()), n)
        self.assertGreaterEqual(cec.COMMAND_GAP, 10)

    def test_tv_that_does_not_confirm_gets_an_honest_answer(self):
        tv = Tv()
        tv.step(active=True)
        tv.flag("deaf")  # povel přijme, ale nevypne se
        tv.step(command=(1, "off"))
        asked = tv.asked()
        steps = 0
        while tv.pw.pending:
            tv.step(command=(1, "off"), dt=2.0)
            steps += 1
            self.assertLess(steps, 40)
        r = tv.pw.report()
        self.assertGreaterEqual(steps * 2.0, cec.CONFIRM_FOR)
        self.assertEqual((r["power_done_id"], r["power_done_ok"]), (1, False))
        self.assertIn("nepotvrdila", r["power_done_note"])
        self.assertFalse(tv.pw.people_off)  # nevypnula se → nic se nemění
        self.assertLessEqual(tv.asked() - asked, 11)  # ptá se po 4 s, ne pořád
        self.assertEqual(tv.sent().count("standby"), 1)  # a povel neopakuje
        # telka s vypnutým CEC: řekne, co zapnout
        tv = Tv()
        tv.step(active=True)
        tv.flag("mute")
        tv.step(command=(1, "on"))
        while tv.pw.pending:
            tv.step(command=(1, "on"), dt=4.0)
        self.assertIn("Anynet+", tv.pw.report()["power_done_note"])
        # žádný adaptér / nástroj: odpověď hned, nic se neposílá
        tv = Tv()
        tv.flag("dead")
        tv.step(command=(1, "off"))
        r = tv.pw.report()
        self.assertEqual((r["power_done_id"], r["power_done_ok"], tv.pw.pending), (1, False, False))
        self.assertIn("nevidí telku", r["power_done_note"])
        self.assertEqual(tv.sent(), [])

    def test_screen_loop_carries_the_command_and_the_answer(self):
        seen: list[Wish] = []

        class Power:
            state, note, pending, listener = "on", "", True, None

            def step(self, wish):
                seen.append(wish)
                return ""

            def report(self):
                return {"power": "on", "power_tv": "on", "power_busy": 7}

        class D:
            session, extra = None, {}

            def step(self, want):
                return "screen"

            def close(self):
                pass

        d = D()
        app = tvapp.TvApp(lambda: PngScreen(_TMP / "p.png", (720, 480)), "http://127.0.0.1:9",
                          "jukebox.local", director=d, power=Power())
        app._on_state({"current": None, "queue": [],
                       "tv": {"power_request": {"id": 7, "action": "off"}}})
        self.assertTrue(app.wake.is_set())  # povel probudí smyčku hned
        app.power_step()
        self.assertEqual(seen[-1].command, (7, "off"))
        self.assertEqual(d.extra, {"power": "on", "power_tv": "on", "power_busy": 7})
        self.assertLessEqual(app._tick(), 2.0)  # dokud se čeká na telku, dívá se často
        app.power.pending = False
        self.assertGreater(app._tick(), 2.0)
        # proces na telce dál jen čte: povel si bere ze stavu, jukeboxu nic neposílá
        for name in ("app.py", "cec.py"):
            text = (ROOT / "ytdj" / "tv" / name).read_text(encoding="utf-8")
            for banned in ("POST", "urlopen", ".control(", ".prompt("):
                self.assertNotIn(banned, text, (name, banned))


class Moving(unittest.TestCase):
    """Nic nestojí na místě: posun celého obrazu a střídání stran."""

    def test_drift_is_slow_wide_bounded_and_repeatable(self):
        for w, h in SIZES:
            path = [screen.drift(i, (w, h)) for i in range(screen.PERIOD_X * screen.PERIOD_Y)]
            self.assertEqual(path, [screen.drift(i, (w, h)) for i in range(391)])  # stejné vždy
            xs, ys = [p[0] for p in path], [p[1] for p in path]
            # desítky bodů, ne ±1
            self.assertGreaterEqual(max(xs) - min(xs), w * 0.03, (w, h))
            self.assertGreaterEqual(max(ys) - min(ys), h * 0.025, (w, h))
            # ale v mezích okraje (nic neuteče z obrazu)
            r = Renderer((w, h))
            self.assertLess(max(map(abs, xs)), r.mx - w * 0.03 + 1)
            self.assertLess(max(map(abs, ys)), r.my - h * 0.025 + 1)
            # skoro každou minutu jinde, kroky malé (žádné skoky)
            moved = sum(1 for a, b in zip(path, path[1:]) if a != b)
            self.assertGreater(moved, len(path) * 0.9)
            self.assertLessEqual(max(abs(a[0] - b[0]) for a, b in zip(path, path[1:])), w * 0.006 + 1)
            self.assertGreater(len(set(path)), 150)
        self.assertEqual(screen.SHIFT_EVERY, 60)
        self.assertEqual(view_from(None, now=NOW).shift + 1, view_from(None, now=NOW + 60).shift)

    def test_static_blocks_swap_sides(self):
        self.assertNotEqual(view_from(None, now=NOW).swap,
                            view_from(None, now=NOW + screen.SWAP_EVERY).swap)
        v = replace(view_from(WISH, now=NOW, address="jukebox.local", address_ip="192.168.0.24"),
                    shift=0, swap=False, saver=False, dim=False)
        for w, h in SIZES:
            r = Renderer((w, h))
            r.render(v, full=True)
            a = {x.name: x.box for x in r.regions}
            left = r.frame.copy()
            self.assertEqual(r.render(replace(v, swap=True)), [(0, 0, w, h)])
            b = {x.name: x.box for x in r.regions}
            self.assertGreater(a["web"][0], a["next"][0])  # adresa a QR vpravo …
            self.assertLess(b["web"][0], b["next"][0])  # … a po výměně vlevo
            self.assertEqual(a["web"][2] - a["web"][0], b["web"][2] - b["web"][0])
            # název a hodiny v hlavičce si taky vyměnily strany
            head = a["head"]
            half = (head[0] + head[2]) // 2
            lum = lambda im, box: sum(im.crop(box).convert("L").getdata())  # noqa: E731
            l_box, r_box = (head[0], head[1], half, head[3] - 6), (half, head[1], head[2], head[3] - 6)
            self.assertGreater(lum(left, l_box), lum(left, r_box))  # název + stav vlevo
            self.assertGreater(lum(r.frame, r_box), lum(r.frame, l_box))


class DimStatic(unittest.TestCase):
    def test_qr_is_dark_on_grey_and_still_scans(self):
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.skipTest("OpenCV není — dekódování se nedá ověřit")
        det = cv2.QRCodeDetector()
        target = "192.168.100.200:8765"  # nejdelší běžný tvar adresy
        v = replace(view_from(WISH, now=NOW, address="jukebox.local", address_ip=target),
                    shift=0, swap=False, saver=False, dim=False)
        for (w, h), zoom in (((720, 480), 3), ((1280, 720), 1), ((1920, 1080), 1)):
            for swap in (False, True):
                r = Renderer((w, h))
                r.render(replace(v, swap=swap), full=True)
                web = next(x.box for x in r.regions if x.name == "web")
                crop = r.frame.crop(web)
                if zoom > 1:  # malá obrazovka: jako telefon blíž u telky
                    crop = crop.resize((crop.width * zoom, crop.height * zoom))
                img = np.array(crop.convert("L"))
                self.assertEqual(det.detectAndDecode(img)[0], f"http://{target}", (w, h, swap))
                # žádná bílá plocha: světlé je tu jen písmo, kód je šedý
                px = list(crop.convert("L").getdata())
                self.assertLess(sum(1 for q in px if q > 200) / len(px), 0.03, (w, h))
        self.assertLess(max(screen.QR_LIGHT), 170)
        self.assertGreater(min(screen.QR_LIGHT) - max(screen.QR_DARK), 110)  # kontrast pro čtečku

    def test_static_parts_are_dimmer_than_the_song(self):
        self.assertLess(sum(screen.ACCENT_DIM), sum(screen.ACCENT) * 0.8)  # stálý nápis JUKEBOX
        self.assertLess(sum(screen.FAINT), sum(screen.TEXT) * 0.5)  # popisky
        v = replace(view_from(WISH, now=NOW, address="jukebox.local", address_ip="192.168.0.24"),
                    shift=0, swap=False, saver=False, dim=False)
        r = Renderer((1920, 1080))
        r.render(v, full=True)
        # v patičce (stálé části) není žádná velká světlá plocha
        for name in ("web", "head"):
            box = next(x.box for x in r.regions if x.name == name)
            px = list(r.frame.crop(box).convert("L").getdata())
            self.assertLess(sum(1 for p in px if p > 200) / len(px), 0.02, name)


class Saver(unittest.TestCase):
    def test_dim_then_dark_saver_then_back(self):
        idle = {"current": None, "queue": [], "dj": {}}
        at = lambda s, st=idle: view_from(st, now=NOW, mono=1000.0, idle_since=1000.0 - s)  # noqa: E731
        self.assertEqual((at(60).dim, at(60).saver), (False, False))
        self.assertEqual((at(screen.DIM_AFTER).dim, at(screen.DIM_AFTER).saver), (True, False))
        self.assertTrue(at(screen.SAVER_AFTER).saver)
        self.assertTrue(at(screen.SAVER_AFTER, {**WISH, "paused": True}).saver)  # i dlouhá pauza
        self.assertFalse(view_from(WISH, now=NOW, mono=1000.0, idle_since=None).saver)  # hraje se
        self.assertLessEqual(screen.SAVER_AFTER, 600)
        for w, h in SIZES:
            r = Renderer((w, h))
            normal = replace(view_from(WISH, now=NOW, address="jukebox.local"), saver=False, dim=False)
            r.render(normal, full=True)
            sv = replace(at(600, {**WISH, "paused": True}), shift=3)
            self.assertEqual(r.render(sv), [(0, 0, w, h)])  # přechod = celý snímek
            px = list(r.frame.convert("L").resize((96, 54)).getdata())
            self.assertLess(sum(px) / len(px), 4)  # skoro černo
            self.assertLess(max(r.frame.convert("L").getdata()), 120)  # nic jasného
            first = r._saver_box
            self.assertEqual(r.render(sv), [])  # beze změny nic
            # další minuta: hodiny jsou jinde; překreslí se jen staré a nové místo
            boxes = r.render(replace(sv, shift=4, clock="14:38"))
            self.assertEqual(boxes, [first, r._saver_box])
            self.assertNotEqual(first, r._saver_box)
            for b in boxes:
                self.assertTrue(b[0] >= r.mx and b[1] >= r.my and b[2] <= w - r.mx and b[3] <= h - r.my)
            # hudba zase hraje: zpátky celá obrazovka
            self.assertEqual(r.render(normal), [(0, 0, w, h)])
            self.assertIsNone(r._saver_box)
        # za hodinu projde spořič celou plochu, ne jeden roh
        r = Renderer((1280, 720))
        spots = set()
        for i in range(60):
            r.render(replace(at(600), shift=i))
            spots.add(r._saver_box[:2])
        xs, ys = [p[0] for p in spots], [p[1] for p in spots]
        self.assertGreater(max(xs) - min(xs), 1280 * 0.5)
        self.assertGreater(max(ys) - min(ys), 720 * 0.5)
        self.assertGreater(len(spots), 40)


class Wiring(unittest.TestCase):
    def test_setting_status_unit_and_docs(self):
        config = (ROOT / "ytdj" / "config.py").read_text(encoding="utf-8")
        self.assertIn('"tv_standby": False', config)  # výchozí vypnuto: zapíná správce
        self.assertIn('"tv_standby_minutes": 10', config)
        server = (ROOT / "ytdj" / "web" / "server.py").read_text(encoding="utf-8")
        self.assertIn('"tv_standby": (\n        "Telku vypínat, když se nehraje"', server)
        self.assertIn('"tv_standby_minutes": (', server)
        tvvideo = (ROOT / "ytdj" / "tvvideo.py").read_text(encoding="utf-8")
        for piece in ('"standby": {"on": bool(getattr(self.cfg, "tv_standby", False))',
                      '"power_note": str(self.report.get("power_note")'):
            self.assertIn(piece, tvvideo)
        page = (ROOT / "ytdj" / "web" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("tv.power_note ||", page)  # proč telka zůstává zapnutá, řekne řádek Telka
        main = (ROOT / "ytdj" / "tv" / "__main__.py").read_text(encoding="utf-8")
        self.assertIn("listener=Listener(cec.listen_command, answer=cec.answer)", main)
        unit = (ROOT / "packaging" / "ytdj-tv.service").read_text(encoding="utf-8")
        self.assertIn("DeviceAllow=/dev/cec0 rw", unit)
        self.assertIn("LogsDirectory=ytdj-tv", unit)  # tam si pamatuje, že telku uspal
        notes = (ROOT / "packaging" / "rpi" / "NOTES.md").read_text(encoding="utf-8")
        for word in ("Anynet+", "cec-ctl", "tv_standby", "--image-view-on", "--wait-for-msgs"):
            self.assertIn(word, notes, word)
        # vypínání telky dělá jen proces obrazovky, nikdy přehrávač ani jukebox sám
        for path in (ROOT / "ytdj").rglob("*.py"):
            if "cec-ctl" in path.read_text(encoding="utf-8"):
                self.assertEqual(path.name, "cec.py", path)


if __name__ == "__main__":
    unittest.main()
