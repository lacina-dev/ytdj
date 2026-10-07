"""Výběr zvukového výstupu (F-ZVUK-28 až F-ZVUK-31, F-HLAS-10).

Vlastník 7. 10. 2026: „dej někam do nastavení možnost výběru zvukového výstupu,
tak aby si to pak vybraný výstup pamatovalo i po rebootu. A aby to vidělo nový
výstup, i když se připojí."

Bez zvukového hardwaru: graf PipeWire z Pi je v tests/fixtures/pw-dump-pi.json,
WirePlumber hraje `FakeWirePlumber` (výchozí výstup podle priorit / připnutí,
proud přehrávače se přepojuje za ním) a příkazy falešné `pw-dump` /
`pw-metadata` (jako falešné nmcli u panelu). Poslední třída pouští skutečné
mpv do soukromého PipeWire se dvěma prázdnými výstupy — jen kde je PipeWire.

    YTDJ_EVENTS_FILE=/tmp/ev.jsonl .venv/bin/python -m unittest tests.test_audio_outputs -v
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="ytdj-audio-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)
os.environ.setdefault("YTDJ_EVENTS_FILE", str(Path(_TMP) / "events.jsonl"))

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from ytdj import audio_outputs as ao  # noqa: E402
from ytdj import telemetry  # noqa: E402
from ytdj.config import DEFAULTS, Config  # noqa: E402

FIXTURE = json.loads((ROOT / "tests/fixtures/pw-dump-pi.json").read_text(encoding="utf-8"))
SOUNDBAR = "alsa_output.usb-Dell_Dell_AC511_USB_SoundBar-00.analog-stereo"
USB = "alsa_output.usb-GeneralPlus_USB_Audio_Device-00.analog-stereo"
JACK = "alsa_output.platform-3f00b840.mailbox.stereo-fallback"
HDMI = "alsa_output.platform-3f902000.hdmi.hdmi-stereo"
NODE = "PipeWire:Interface:Node"
STREAM_ID, MPV_PID = 93, 4242
# krátké lhůty, ať testy neběží desítky vteřin (poměry zůstávají)
FAST = {"RETURN_DEBOUNCE": 0.25, "STARTUP_GRACE": 0.4, "SETTLE": 0.0, "APPLY_WATCH": 0.3,
        "MONITOR_RETRY": 0.2}


def run(coro):
    return asyncio.run(coro)


class FakePlayer:
    """Z přehrávače se smí jen číst pid a stáhnout hlasitost — nic jiného."""

    def __init__(self, volume: int = 55, muted: bool = False) -> None:
        self.proc = SimpleNamespace(pid=MPV_PID)
        self.volume, self.muted = volume, muted
        self.calls: list[tuple] = []
        self.log: list[str] = []  # společné pořadí s FakeWirePlumber

    async def lower_volume(self, limit: int):
        self.calls.append(("lower_volume", limit))
        if self.volume <= limit:
            return None
        old, self.volume = self.volume, limit
        self.log.append(f"volume {old}->{limit}")
        return old, limit

    def __getattr__(self, name):  # cokoli dalšího by byla chyba návrhu
        raise AttributeError(f"přepnutí výstupu nesmí sahat na přehrávač: {name}")


class FakeWirePlumber:
    """WirePlumber v malém: výchozí výstup = připnutý, když je připojený,
    jinak nejvyšší priorita; proud přehrávače se přepojí za výchozím."""

    def __init__(self, manager: ao.AudioOutputs, objects: list[dict] | None = None,
                 pin: str | None = None, stream: bool = True) -> None:
        self.m = manager
        self.objects = {o["id"]: copy.deepcopy(o) for o in (FIXTURE if objects is None else objects)}
        self.pin = pin
        self.stream = stream
        self.commands: list[tuple[str, ...]] = []
        self.unplugged: dict[str, list[dict]] = {}
        self.log = getattr(manager.player, "log", [])
        self.fail_writes = False
        self._next = 200
        self.objects.pop(89, None), self.objects.pop(92, None)  # odkazy si drží sám
        if not stream:
            self.objects.pop(STREAM_ID, None)
        manager._command = self.command  # type: ignore[method-assign]

    # ---- pohled ----

    def sinks(self) -> dict[str, dict]:
        return {o["info"]["props"]["node.name"]: o for o in self.objects.values()
                if o.get("type") == NODE and o["info"]["props"].get("media.class") == "Audio/Sink"}

    def default(self) -> str | None:
        sinks = self.sinks()
        if self.pin in sinks:
            return self.pin
        best = sorted(sinks.values(), key=lambda o: (-o["info"]["props"].get("priority.session", 0),
                                                     o["id"]))
        return best[0]["info"]["props"]["node.name"] if best else None

    def _meta(self) -> dict:
        entries = [{"subject": 0, "key": ao.KEY_DEFAULT, "type": "Spa:String:JSON",
                    "value": {"name": self.default()}}] if self.default() else []
        if self.pin:
            entries.append({"subject": 0, "key": ao.KEY_PIN, "type": "Spa:String:JSON",
                            "value": {"name": self.pin}})
        return {"id": 38, "type": "PipeWire:Interface:Metadata", "props": {"metadata.name": "default"},
                "metadata": entries}

    def _link(self) -> list[dict]:
        out = [{"id": 89, "info": None}]
        target = self.sinks().get(self.default() or "")
        if self.stream and target is not None:
            out.append({"id": 89, "type": "PipeWire:Interface:Link", "info": {
                "output-node-id": STREAM_ID, "input-node-id": target["id"], "state": "active"}})
        return out

    def dump(self) -> list[dict]:
        return [copy.deepcopy(o) for o in self.objects.values() if o["id"] != 38] + \
            [self._meta()] + self._link()

    def boot(self) -> None:
        self.m.feed(self.dump())

    # ---- události ----

    def unplug(self, name: str) -> None:
        node = self.sinks()[name]
        dev = node["info"]["props"].get("device.id")
        gone = [o for o in self.objects.values()
                if o["id"] in (node["id"], dev) or (o.get("info") or {}).get("props", {}).get("device.id") == dev]
        self.unplugged[name] = copy.deepcopy(gone)
        for o in gone:
            del self.objects[o["id"]]
        self.m.feed([{"id": o["id"], "info": None} for o in gone] + [self._meta()] + self._link())

    def plug(self, name: str) -> None:
        back = self.unplugged.pop(name)
        remap = {}
        for o in back:  # po připojení mají objekty nová id
            remap[o["id"]] = self._next
            self._next += 1
        for o in back:
            o["id"] = remap[o["id"]]
            props = o["info"]["props"]
            if props.get("device.id") in remap:
                props["device.id"] = remap[props["device.id"]]
            self.objects[o["id"]] = o
        self.m.feed(copy.deepcopy(back) + [self._meta()] + self._link())

    def add_sink(self, name: str, description: str, priority: int = 2000, **params) -> None:
        self._next += 1
        obj = {"id": self._next, "type": NODE, "info": {"state": "suspended", "props": {
            "node.name": name, "node.description": description, "media.class": "Audio/Sink",
            "priority.session": priority},
            "params": {"Props": [dict({"mute": False, "channelVolumes": [1.0, 1.0]}, **params)]}}}
        self.objects[obj["id"]] = obj
        self.m.feed([copy.deepcopy(obj), self._meta()] + self._link())

    def set_pin_by_hand(self, name: str | None) -> None:
        """Někdo na Pi: wpctl set-default / clear-default."""
        self.pin = name
        self.m.feed([self._meta()] + self._link())

    def restart(self) -> None:
        """Restart WirePlumberu, který svůj stav ztratil (deploy ho smazal)."""
        self.pin = None
        self.m.feed([{"id": 38, "info": None}])
        self.m.feed([self._meta()] + self._link())

    # ---- pw-metadata ----

    async def command(self, *args: str) -> tuple[bool, str]:
        self.commands.append(args)
        if args == ("-n", "default", "0"):
            d = self.default()
            lines = ['Found "default" metadata 38']
            if d:
                lines.append(f"update: id:0 key:'{ao.KEY_DEFAULT}' value:'{{\"name\":\"{d}\"}}' "
                             "type:'Spa:String:JSON'")
            if self.pin:  # WirePlumber píše s mezerami — jako na Pi
                lines.append(f"update: id:0 key:'{ao.KEY_PIN}' value:'{{ \"name\": \"{self.pin}\" }}' "
                             "type:'Spa:String:JSON'")
            return True, "\n".join(lines) + "\n"
        if self.fail_writes:
            return False, ""
        if args[:4] == ("-n", "default", "-d", "0") and args[4:] == (ao.KEY_PIN,):
            self.pin = None
            self.log.append("pin None")
        elif args[:4] == ("-n", "default", "0", ao.KEY_PIN) and args[5:] == ("Spa:String:JSON",):
            self.pin = json.loads(args[4])["name"]
            self.log.append(f"pin {self.pin}")
        else:
            raise AssertionError(f"nečekaný příkaz pw-metadata: {args}")
        await asyncio.sleep(0)
        self.m.feed([self._meta()] + self._link())
        return True, ""

    def writes(self) -> list[str | None]:
        out: list[str | None] = []
        for c in self.commands:
            if "-d" in c:
                out.append(None)
            elif len(c) > 4 and c[3] == ao.KEY_PIN:
                out.append(json.loads(c[4])["name"])
        return out


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(dir=_TMP))
        self.events: list[tuple[str, dict]] = []
        self.saved: list[dict] = []
        for p in (mock.patch.object(telemetry, "event", lambda kind, **f: self.events.append((kind, f))),
                  mock.patch.object(ao, "save_values", lambda changes: self.saved.append(dict(changes))),
                  mock.patch.multiple(ao, **FAST)):
            p.start()
            self.addCleanup(p.stop)

    def make(self, chosen: str = "", volume: int = 55, pin: str | None = USB, stream: bool = True,
             adopted: bool = True, last: str | None = USB, cap: int = 0, objects=None):
        cfg = Config(**{**DEFAULTS, "audio_output": chosen, "audio_switch_volume": cap})
        player = FakePlayer(volume)
        m = ao.AudioOutputs(cfg, player, state_file=self.dir / "audio-outputs.json")
        m.state.update(adopted=adopted, last=last)
        m.available = None
        wp = FakeWirePlumber(m, objects=objects, pin=pin, stream=stream)
        return m, wp, player, cfg

    def kinds(self, kind: str) -> list[dict]:
        return [f for k, f in self.events if k == kind]

    async def settle(self, t: float = 0.08) -> None:
        await asyncio.sleep(t)


class OutputList(Case):
    """F-ZVUK-28: seznam výstupů s lidskými jmény, živě."""

    def test_names_for_people_and_what_plays_now(self) -> None:
        async def go():
            m, wp, _, _ = self.make(chosen=USB)
            wp.boot()
            await self.settle()
            return m.snapshot()

        snap = run(go())
        self.assertTrue(snap["available"])
        self.assertEqual([(c["value"], c["label"]) for c in snap["choices"]], [
            ("", "Automaticky (podle priority)"),
            (SOUNDBAR, "AC511 Sound Bar"),
            (USB, "USB Audio Device — hraje teď"),
            (JACK, "Sluchátkový výstup 3,5 mm"),
            (HDMI, "HDMI (telka)"),
        ])
        self.assertEqual((snap["chosen"], snap["playing"], snap["playing_label"]),
                         (USB, USB, "USB Audio Device"))
        self.assertEqual(snap["notes"], ["Teď hraje: USB Audio Device."])
        self.assertFalse(snap["fallback"])

    def test_newly_plugged_output_appears_without_restart(self) -> None:
        async def go():
            m, wp, _, _ = self.make(chosen=USB)
            wp.boot()
            await self.settle()
            before = [c["value"] for c in m.snapshot()["choices"]]
            wp.add_sink("alsa_output.usb-Nova_Karta-00.analog-stereo", "Nová Karta Analog Stereo")
            await self.settle()
            return before, m.snapshot(), wp

        before, snap, wp = run(go())
        new = "alsa_output.usb-Nova_Karta-00.analog-stereo"
        self.assertNotIn(new, before)
        self.assertIn((new, "Nová Karta"), [(c["value"], c["label"]) for c in snap["choices"]])
        self.assertEqual(snap["playing"], USB)  # nová karta sama nic nepřepnula
        seen = self.kinds("audio.outputs")
        self.assertTrue(seen[0]["first"])
        self.assertEqual((seen[-1]["appeared"], seen[-1]["removed"]), ([new], None))
        self.assertEqual(wp.writes(), [])  # volba platí, není co měnit

    def test_remembered_output_that_is_not_connected_is_shown_as_such(self) -> None:
        async def go():
            m, wp, _, _ = self.make(chosen=USB)
            wp.boot()
            await self.settle()
            wp.unplug(SOUNDBAR)
            await self.settle()
            snap = m.snapshot()
            # po restartu jukeboxu (nový objekt, stav z disku) pořád ví, jak se jmenoval
            m2 = ao.AudioOutputs(m.cfg, FakePlayer(), state_file=m.state_file)
            m2._load_state()
            wp2 = FakeWirePlumber(m2, objects=list(wp.objects.values()), pin=USB)
            wp2.boot()
            await self.settle()
            return snap, m2.snapshot()

        for snap in run(go()):
            self.assertIn({"value": SOUNDBAR, "connected": False,
                           "label": "AC511 Sound Bar (není připojený)"}, snap["choices"])
        self.assertEqual(self.kinds("audio.outputs")[1]["removed"], [SOUNDBAR])

    def test_real_formats_of_pw_metadata_and_names_are_plain(self) -> None:
        text = ("Found \"default\" metadata 38\n"
                "update: id:0 key:'default.audio.sink' value:'{\"name\":\"" + USB + "\"}' type:'Spa:String:JSON'\n"
                "update: id:0 key:'default.audio.source' value:'{\"name\":\"x\"}' type:'Spa:String:JSON'\n"
                "update: id:0 key:'default.configured.audio.sink' value:'{ \"name\": \"" + SOUNDBAR + "\" }' "
                "type:'Spa:String:JSON'\n")
        self.assertEqual(ao.parse_metadata(text), {ao.KEY_DEFAULT: USB, ao.KEY_PIN: SOUNDBAR})
        self.assertEqual(ao.parse_metadata("Found \"default\" metadata 38\n"), {})
        for bad in ("x y", "a;rm -rf /", "$(reboot)", "-d", "a'b", 'a"b', "", "ž", "a" * 201):
            self.assertIsNone(ao.NAME_OK.match(bad), bad)
        for name in (USB, SOUNDBAR, JACK, HDMI, "bluez_output.AA_BB_CC.1", "alsa_output.pci-0000_00_1f.3.analog-stereo"):
            self.assertTrue(ao.NAME_OK.match(name), name)
        # "Dummy Output" (žádná karta) ani uzel s divným jménem se nenabízí
        g = ao.Graph()
        g.apply([{"id": 1, "type": NODE, "info": {"props": {"node.name": "auto_null", "media.class": "Audio/Sink"}}},
                 {"id": 2, "type": NODE, "info": {"props": {"node.name": "a b;c", "media.class": "Audio/Sink"}}}])
        self.assertEqual(g.sinks(), [])

    def test_without_pipewire_it_says_so_and_nothing_crashes(self) -> None:
        async def go():
            cfg = Config(**{**DEFAULTS, "audio_output": USB})
            m = ao.AudioOutputs(cfg, FakePlayer(), state_file=self.dir / "s.json",
                                pw_dump=str(self.dir / "neni-pw-dump"), pw_metadata=str(self.dir / "neni"))
            await m.start()
            notes = await m.config_changed(previous="", who={"ip": "10.0.0.7"})
            snap = m.snapshot()
            await m.stop()
            return m, snap, notes

        m, snap, notes = run(go())
        self.assertIs(snap["available"], False)
        self.assertEqual(snap["choices"], [{"value": USB, "label": "Není k dispozici (na tomhle stroji není PipeWire)"}])
        self.assertIn("není k dispozici", snap["notes"][0])
        self.assertIn("není k dispozici", notes[0])
        self.assertIsNone(m.output_name())
        self.assertEqual(self.kinds("audio.monitor")[0]["ok"], False)


class Switching(Case):
    """F-ZVUK-29: přepnutí hned přes WirePlumber, volba patří jukeboxu."""

    def test_choice_moves_the_default_sink_and_never_touches_the_player(self) -> None:
        async def go():
            m, wp, player, cfg = self.make(chosen=USB, volume=10)
            wp.boot()
            await self.settle()
            cfg.audio_output = SOUNDBAR  # tak to dělá web (LIVE klíč) …
            notes = await m.config_changed(previous=USB, who={"ip": "10.0.0.7", "ua": "Chrome"})
            await self.settle(0.4)
            return m, wp, player, notes

        m, wp, player, notes = run(go())
        self.assertEqual(wp.commands[-2], ("-n", "default", "0", ao.KEY_PIN,
                                           json.dumps({"name": SOUNDBAR}), "Spa:String:JSON"))
        self.assertEqual(wp.writes(), [SOUNDBAR])
        self.assertEqual(m.snapshot()["playing"], SOUNDBAR)
        self.assertEqual(notes, ["Hraje: AC511 Sound Bar."])
        # přehrávač: vůbec nic — žádné audio-device, restart, pauza, seek ani
        # hlasitost (FakePlayer by jinak spadl)
        self.assertEqual(player.calls, [])
        self.assertEqual(player.volume, 10)
        choice = self.kinds("audio.output_choice")
        self.assertEqual(choice, [{"was": USB, "now": SOUNDBAR, "label": "AC511 Sound Bar",
                                   "ip": "10.0.0.7", "ua": "Chrome"}])
        applied = self.kinds("audio.output_apply")[-1]
        self.assertEqual((applied["target"], applied["ok"], applied["reason"], applied["playing"],
                          applied["was"]), (SOUNDBAR, True, "choice", SOUNDBAR, USB))
        self.assertIsInstance(applied["moved_ms"], int)  # proud přehrávače se opravdu přepojil
        self.assertEqual(self.kinds("audio.output_change")[-1]["reason"], "choice")

    def test_automatic_unpins_and_priorities_decide(self) -> None:
        async def go():
            m, wp, player, cfg = self.make(chosen=USB, volume=15)
            wp.boot()
            await self.settle()
            cfg.audio_output = ""
            await m.config_changed(previous=USB, who={})
            await self.settle()
            return m, wp

        m, wp = run(go())
        self.assertEqual(wp.writes(), [None])
        self.assertIsNone(wp.pin)
        self.assertEqual(m.snapshot()["chosen"], "")
        self.assertEqual(m.snapshot()["playing"], SOUNDBAR)  # nejvyšší priorita (první z USB)
        self.assertEqual(self.kinds("audio.output_choice")[0]["label"], "Automaticky (podle priority)")

    def test_first_run_adopts_the_hand_made_default_so_nothing_changes(self) -> None:
        async def go():
            # dnešní stav Pi: v nastavení nic, ve WirePlumberu ručně připnutá nová karta
            m, wp, player, cfg = self.make(chosen="", pin=USB, adopted=False, last=None)
            wp.boot()
            await self.settle()
            first = (cfg.audio_output, list(wp.writes()), player.volume, list(player.calls))
            # další start už nic nepřebírá: "Automaticky" je pak vědomá volba
            cfg2 = Config(**{**DEFAULTS, "audio_output": ""})
            m2 = ao.AudioOutputs(cfg2, FakePlayer(), state_file=m.state_file)
            m2._load_state()
            wp2 = FakeWirePlumber(m2, pin=USB)
            wp2.boot()
            await self.settle()
            return first, m.state, cfg2.audio_output, wp2.writes()

        (chosen, writes, volume, calls), state, chosen2, writes2 = run(go())
        self.assertEqual(chosen, USB)
        self.assertEqual(self.saved, [{"audio_output": USB}])  # do config.toml
        self.assertEqual((writes, volume, calls), ([], 55, []))  # nic slyšitelného
        self.assertEqual((state["adopted"], state["last"]), (True, USB))
        self.assertEqual(self.kinds("audio.output_adopt")[0]["output"], USB)
        self.assertEqual((chosen2, writes2), ("", [None]))

    def test_choice_is_reapplied_at_start_and_when_wireplumber_forgets(self) -> None:
        async def go():
            # start po nasazení, které stav WirePlumberu smazalo: nic připnuto
            m, wp, player, _ = self.make(chosen=USB, pin=None, last=USB, volume=15)
            wp.boot()
            await self.settle()
            at_start = (list(wp.writes()), wp.default())
            wp.restart()  # za běhu: restart WirePlumberu bez stavu
            await self.settle()
            wp.set_pin_by_hand(SOUNDBAR)  # a někdo to přepne na Pi rukou
            await self.settle()
            return at_start, wp, m

        at_start, wp, m = run(go())
        self.assertEqual(at_start, ([USB], USB))
        self.assertEqual(wp.writes(), [USB, USB, USB])  # volba jukeboxu se vždy vrátí
        self.assertEqual((wp.pin, m.snapshot()["playing"]), (USB, USB))

    def test_no_tug_of_war_with_someone_switching_elsewhere(self) -> None:
        async def go():
            m, wp, _, _ = self.make(chosen=USB, volume=10)
            wp.boot()
            await self.settle()
            for _ in range(12):
                wp.set_pin_by_hand(SOUNDBAR)
                await self.settle(0.03)
            return wp

        wp = run(go())
        self.assertEqual(len(wp.writes()), ao.WRITES_PER_MINUTE)  # pak to nechá být
        self.assertEqual(self.kinds("audio.output_conflict")[0]["want"], USB)

    def test_failed_write_is_reported_not_hidden(self) -> None:
        async def go():
            m, wp, _, cfg = self.make(chosen=USB, volume=10)
            wp.boot()
            await self.settle()
            wp.fail_writes = True
            cfg.audio_output = SOUNDBAR
            await m.config_changed(previous=USB, who={})
            return m.snapshot()

        snap = run(go())
        applied = self.kinds("audio.output_apply")[-1]
        self.assertEqual((applied["ok"], applied["moved_ms"], applied["playing"]), (False, None, USB))
        self.assertEqual(snap["playing"], USB)
        self.assertIn("Přepíná se na „AC511 Sound Bar“…", snap["notes"])


class FallbackAndReturn(Case):
    """F-ZVUK-30: vybraný výstup není připojený — hraje se dál náhradním."""

    def test_unplugged_output_falls_back_says_so_and_returns_debounced(self) -> None:
        async def go():
            m, wp, player, _ = self.make(chosen=USB, volume=55)
            wp.boot()
            await self.settle()
            wp.unplug(USB)
            await self.settle()
            away = (m.snapshot(), list(wp.writes()), player.volume, list(player.log))
            player.volume = 40  # mezitím si někdo přidal
            player.log.clear()
            wp.plug(USB)
            await self.settle(0.1)
            early = (list(wp.writes()), wp.default())  # ještě se nevrací (ustálení)
            await self.settle(0.4)
            return away, early, m.snapshot(), wp, player

        (snap, writes, volume, log), early, back, wp, player = run(go())
        self.assertTrue(snap["fallback"])
        self.assertEqual(snap["playing"], SOUNDBAR)  # hudba hraje dál, jinde
        self.assertIn("Vybraný výstup „USB Audio Device“ není připojený — hraje náhradní "
                      "„AC511 Sound Bar“. Až se připojí, jukebox se na něj sám vrátí.", snap["notes"])
        self.assertIn({"value": USB, "connected": False, "label": "USB Audio Device (není připojený)"},
                      snap["choices"])
        self.assertEqual(snap["chosen"], USB)  # volba zůstává
        self.assertEqual(writes, [None])  # nepřipojený výstup se nepřipíná: návrat řídí jukebox
        self.assertEqual(volume, 55)  # hlasitost se nemění (F-HLAS-10, výchozí)
        self.assertFalse([n for n in snap["notes"] if "Hlasitost stažena" in n], snap["notes"])
        self.assertEqual([f["chosen"] for f in self.kinds("audio.output_fallback")], [USB])  # jednou
        self.assertEqual(early, ([None], SOUNDBAR))
        self.assertEqual(wp.writes(), [None, USB])
        self.assertEqual(back["playing"], USB)
        self.assertFalse(back["fallback"])
        self.assertEqual(player.log, [f"pin {USB}"])  # jen přepnutí, hlasitost zůstala
        self.assertEqual((player.volume, player.calls), (40, []))
        self.assertEqual(self.kinds("audio.volume_cap"), [])
        ret = self.kinds("audio.output_return")
        self.assertEqual(len(ret), 1)
        self.assertGreater(ret[0]["away_s"], 0)
        self.assertEqual([c["reason"] for c in self.kinds("audio.output_change")], ["fallback", "return"])

    def test_flapping_connection_does_not_flap_the_music(self) -> None:
        async def go():
            m, wp, _, _ = self.make(chosen=USB, volume=10)
            wp.boot()
            await self.settle()
            wp.unplug(USB)
            await self.settle()
            for _ in range(4):  # vypadávající kabel: tam a zpět rychleji než ustálení
                wp.plug(USB)
                await self.settle(0.06)
                wp.unplug(USB)
                await self.settle(0.06)
            mid = list(wp.writes())
            wp.plug(USB)
            await self.settle(0.5)
            return mid, wp

        mid, wp = run(go())
        self.assertEqual(mid, [None])  # během poskakování se nepřepnulo ani jednou
        self.assertEqual(wp.writes(), [None, USB])
        self.assertEqual(len(self.kinds("audio.output_fallback")), 1)
        self.assertEqual(len(self.kinds("audio.output_return")), 1)

    def test_at_start_it_waits_for_usb_cards_before_calling_it_a_fallback(self) -> None:
        async def go(appears: bool):
            # po zapnutí Pi: nic nehraje, vybraná USB karta se ještě neohlásila
            m, wp, player, _ = self.make(chosen=USB, pin=USB, stream=False, volume=55)
            m._t0 = m.clock()
            wp.unplugged[USB] = []
            usb = wp.sinks()[USB]
            dev = usb["info"]["props"]["device.id"]
            wp.unplugged[USB] = [wp.objects.pop(usb["id"]), wp.objects.pop(dev)]
            wp.boot()
            await self.settle(0.1)
            early = (m.fallback, player.volume, list(wp.writes()))
            if appears:
                wp.plug(USB)
            await self.settle(0.6)
            return early, m.fallback, player.volume, wp.writes(), m.snapshot()["playing"]

        early, fallback, volume, writes, playing = run(go(appears=True))
        self.assertEqual(early, (False, 55, []))
        self.assertEqual((fallback, volume, writes, playing), (False, 55, [], USB))  # ráno beze změny
        self.assertEqual(self.kinds("audio.output_fallback"), [])
        self.events.clear()
        early, fallback, volume, writes, playing = run(go(appears=False))
        self.assertEqual(early, (False, 55, []))
        self.assertEqual((fallback, volume, playing), (True, 55, SOUNDBAR))  # opravdu chybí
        self.assertEqual(len(self.kinds("audio.output_fallback")), 1)

    def test_choosing_an_output_that_is_not_connected_waits_for_it(self) -> None:
        async def go():
            m, wp, player, cfg = self.make(chosen=USB, volume=10)
            wp.boot()
            await self.settle()
            wp.unplug(SOUNDBAR)
            await self.settle()
            cfg.audio_output = SOUNDBAR
            notes = await m.config_changed(previous=USB, who={})
            await self.settle(0.5)
            waiting = (m.snapshot()["playing"], m.fallback)
            wp.plug(SOUNDBAR)
            await self.settle(0.5)
            return notes, waiting, m.snapshot()["playing"]

        notes, waiting, playing = run(go())
        self.assertEqual(notes[0], "Výstup „AC511 Sound Bar“ teď není připojený — přepne se na něj, "
                                   "až se připojí.")
        self.assertEqual(waiting, (USB, True))
        self.assertEqual(playing, SOUNDBAR)


class VolumeSafety(Case):
    """F-HLAS-10: výchozí — hlasitost se při změně výstupu nemění (vlastník
    7. 10. 2026: „hlasitost nech"); strop je volitelné nastavení."""

    def test_by_default_the_volume_is_never_changed(self) -> None:
        self.assertEqual((DEFAULTS["audio_switch_volume"], ao.SWITCH_VOLUME), (0, 0))
        self.assertEqual(Config(**DEFAULTS).audio_switch_volume, 0)

        async def go():
            cfg0 = Config(**{**DEFAULTS, "audio_output": USB})  # výchozí nastavení, ne parametr testu
            player = FakePlayer(70)
            m = ao.AudioOutputs(cfg0, player, state_file=self.dir / "default.json")
            m.state.update(adopted=True, last=USB)
            wp = FakeWirePlumber(m, pin=USB)
            wp.boot()
            await self.settle()
            cfg0.audio_output = SOUNDBAR  # volba v nastavení
            notes = list(await m.config_changed(previous=USB, who={}))
            wp.unplug(SOUNDBAR)  # odpojení → náhradní výstup
            await self.settle()
            notes += m.snapshot()["notes"]
            wp.plug(SOUNDBAR)  # návrat
            await self.settle(0.5)
            notes += m.snapshot()["notes"]
            cfg0.audio_output = ""  # Automaticky + přepnutí odjinud (silnější karta)
            notes += await m.config_changed(previous=SOUNDBAR, who={})
            wp.add_sink("alsa_output.usb-Silny_Zesilovac-00.analog-stereo", "Silný Zesilovač Analog Stereo",
                        priority=3000)
            await self.settle()
            notes += m.snapshot()["notes"]
            return player, notes, m.snapshot()["playing_label"]

        player, notes, playing = run(go())
        self.assertEqual(playing, "Silný Zesilovač")
        reasons = [c["reason"] for c in self.kinds("audio.output_change")]
        self.assertEqual(reasons[:3], ["choice", "fallback", "return"])
        self.assertEqual(reasons[-1], "system")
        self.assertEqual((player.volume, player.calls, player.log.count("volume")), (70, [], 0))
        self.assertFalse([e for e in player.log if e.startswith("volume")], player.log)
        self.assertFalse([n for n in notes if "Hlasitost" in n or "stažen" in n], notes)
        self.assertEqual(self.kinds("audio.volume_cap"), [])

    def test_fallback_and_return_are_capped_when_a_cap_is_set(self) -> None:
        async def go():
            m, wp, player, _ = self.make(chosen=USB, volume=55, cap=20)
            wp.boot()
            await self.settle()
            wp.unplug(USB)
            await self.settle()
            away = player.volume
            player.volume = 40  # mezitím si někdo přidal
            player.log.clear()
            wp.plug(USB)
            await self.settle(0.5)
            return away, player

        away, player = run(go())
        self.assertEqual(away, 20)
        # při návratu nejdřív hlasitost dolů, teprve pak přepnout
        self.assertEqual(player.log, ["volume 40->20", f"pin {USB}"])
        self.assertEqual([c["reason"] for c in self.kinds("audio.volume_cap")], ["fallback", "return"])

    def test_switch_caps_the_volume_before_the_sound_moves_and_says_so(self) -> None:
        async def go():
            m, wp, player, cfg = self.make(chosen=USB, volume=55, cap=20)
            wp.boot()
            await self.settle()
            cfg.audio_output = SOUNDBAR
            notes = await m.config_changed(previous=USB, who={})
            await self.settle()
            return m.snapshot(), notes, player

        snap, notes, player = run(go())
        self.assertEqual(player.log, ["volume 55->20", f"pin {SOUNDBAR}"])  # pořadí!
        text = ("Hlasitost stažena na 20 (bylo 55) — výstup „AC511 Sound Bar“ může hrát hlasitěji. "
                "Přidej si podle potřeby.")
        self.assertEqual(notes, ["Hraje: AC511 Sound Bar.", text])
        self.assertIn(text, snap["notes"])  # zůstává vidět v nastavení
        self.assertEqual(self.kinds("audio.volume_cap"),
                         [{"output": SOUNDBAR, "reason": "choice", "volume_from": 55, "volume_to": 20}])

    def test_lower_volume_is_left_alone_and_zero_switches_the_rule_off(self) -> None:
        async def go(volume: int, cap: int):
            m, wp, player, cfg = self.make(chosen=USB, volume=volume, cap=cap)
            wp.boot()
            await self.settle()
            cfg.audio_output = SOUNDBAR
            notes = await m.config_changed(previous=USB, who={})
            wp.unplug(SOUNDBAR)  # i náhradní výstup bez zásahu
            await self.settle()
            return player.volume, notes

        self.assertEqual(run(go(10, 20)), (10, ["Hraje: AC511 Sound Bar."]))
        self.assertEqual(run(go(20, 20)), (20, ["Hraje: AC511 Sound Bar."]))
        self.assertEqual(run(go(55, 0)), (55, ["Hraje: AC511 Sound Bar."]))  # vypnuto
        self.assertEqual(run(go(55, 35))[0], 35)  # jiná hodnota z nastavení
        self.assertEqual(len(self.kinds("audio.volume_cap")), 1)  # jen poslední běh; pád na náhradní už ne (35 ≤ 35)

    def test_change_made_elsewhere_is_capped_as_soon_as_it_is_seen(self) -> None:
        async def go():
            m, wp, player, _ = self.make(chosen="", pin=None, last=SOUNDBAR, volume=70, cap=20)
            wp.boot()
            await self.settle()
            start = player.volume
            # Automaticky: připojí se karta s vyšší prioritou a WirePlumber na ni
            # přepne sám — jukebox to jen vidí, a hned stáhne hlasitost
            wp.add_sink("alsa_output.usb-Silny_Zesilovac-00.analog-stereo", "Silný Zesilovač Analog Stereo",
                        priority=3000)
            await self.settle()
            return start, player.volume, m.snapshot()

        start, after, snap = run(go())
        self.assertEqual((start, after), (70, 20))
        self.assertEqual(snap["playing_label"], "Silný Zesilovač")
        self.assertEqual(self.kinds("audio.output_change")[0]["reason"], "system")
        self.assertTrue(any("Hlasitost stažena na 20 (bylo 70)" in n for n in snap["notes"]))

    def test_player_lowers_without_unmuting_and_remembers_it(self) -> None:
        from test_player_queue import Harness

        async def go():
            async with Harness() as h:
                await h.player.set_volume(55)
                await h.player.set_mute(True)
                await asyncio.sleep(0.05)
                mark = len(h.fake.log)
                res = await h.player.lower_volume(20)
                again = await h.player.lower_volume(20)
                await asyncio.sleep(0.05)
                st = await h.player.status()
                return res, again, st.volume, st.muted, h.player.cfg.volume, h.fake.log[mark:]

        res, again, volume, muted, saved, log = run(go())
        self.assertEqual((res, again), ((55, 20), None))
        self.assertEqual((volume, muted, saved), (20, True, 20))  # ztlumení zůstalo, hodnota se pamatuje
        self.assertEqual([c for c in log if isinstance(c, list) and c[0] == "set_property"],
                         [["set_property", "volume", 20]])


class SystemVolume(Case):
    """F-ZVUK-31: směšovač karty a hlasitost výstupu v systému se jen čtou."""

    def test_muted_or_zero_output_is_reported_not_changed(self) -> None:
        async def go(change):
            objects = copy.deepcopy(FIXTURE)
            change({o["id"]: o for o in objects})
            m, wp, _, _ = self.make(chosen=USB, objects=objects)
            wp.boot()
            await self.settle()
            return m.snapshot()["notes"], wp.commands

        def sink_muted(o):
            o[95]["info"]["params"]["Props"][0]["mute"] = True

        def sink_zero(o):
            o[95]["info"]["params"]["Props"][0]["channelVolumes"] = [0.0, 0.0]

        def mixer_muted(o):
            [r for r in o[94]["info"]["params"]["Route"] if r["direction"] == "Output"][0]["props"]["mute"] = True

        def other_muted(o):  # ztlumený výstup, na kterém se nehraje, nikoho nezajímá
            o[63]["info"]["params"]["Props"][0]["mute"] = True

        notes, commands = run(go(sink_muted))
        self.assertTrue(any("je v systému ztlumený — jukebox hraje, ale není slyšet" in n for n in notes), notes)
        notes, _ = run(go(sink_zero))
        self.assertTrue(any("stažený na nulu" in n for n in notes), notes)
        notes, _ = run(go(mixer_muted))
        self.assertTrue(any("ztlumený nebo nulový směšovač" in n for n in notes), notes)
        notes, _ = run(go(other_muted))
        self.assertEqual(notes, ["Teď hraje: USB Audio Device."])
        # jediné, co jukebox do PipeWire kdy zapíše, je připnutý výstup
        for c in commands:
            self.assertTrue(c == ("-n", "default", "0") or ao.KEY_PIN in c, c)

    def test_xrun_names_the_output_that_was_playing(self) -> None:
        from ytdj.telemetry_sampler import SystemSampler, parse_pwtop_frame

        frame = lambda usb, bar: [  # noqa: E731
            f"R   95   2048  48000 358.5us  91.0us  0.01  0.00    {usb}    S16LE 2 48000 {USB}",
            f"I   63   2048  48000  29.6us  53.0us  0.00  0.00    {bar}    S16LE 2 48000 {SOUNDBAR}",
            "R   93      0  48000  69.3us 243.5us  0.00  0.01    0     F32P 2 48000  + mpv"]
        sampler = SystemSampler(lambda: {"track": "abc"}, lambda: {})
        playing = [USB]
        sampler.xrun_context = lambda: {"output": playing[0]}
        sampler._compare(parse_pwtop_frame(frame(0, 0)), parse_pwtop_frame(frame(3, 0)))
        playing[0] = SOUNDBAR  # po přepnutí
        sampler._compare(parse_pwtop_frame(frame(3, 0)), parse_pwtop_frame(frame(3, 2)))
        x = self.kinds("audio.xrun")
        self.assertEqual([(e["nodes"], e["output"], e["delta"]) for e in x],
                         [([f"{USB}+3"], USB, 3), ([f"{SOUNDBAR}+2"], SOUNDBAR, 2)])


FAKE_PW_DUMP = """#!{python}
import json, os, sys, time
assert sys.argv[1:] == ["-m", "-N"], sys.argv
d = {dir!r}
print(json.dumps(json.load(open({fixture!r})), indent=2), flush=True)   # první výpis: celý graf
seen = 0
while True:                                # další výpisy: změny (soubory NNN.json)
    for name in sorted(os.listdir(d)):
        if name.endswith(".json") and int(name[:3]) > seen:
            seen = int(name[:3])
            print(json.dumps(json.load(open(os.path.join(d, name))), indent=2), flush=True)
    time.sleep(0.02)
"""

FAKE_PW_METADATA = """#!{python}
import json, os, sys
d = {dir!r}
open(os.path.join(d, "calls.log"), "a").write(json.dumps(sys.argv[1:]) + "\\n")
state = os.path.join(d, "pin")
a = sys.argv[1:]
if a == ["-n", "default", "0"]:
    print('Found "default" metadata 38')
    print("update: id:0 key:'default.audio.sink' value:'{{\\"name\\":\\"{usb}\\"}}' type:'Spa:String:JSON'")
    if os.path.exists(state):
        print("update: id:0 key:'default.configured.audio.sink' value:'{{ \\"name\\": \\"%s\\" }}' type:'Spa:String:JSON'" % open(state).read())
elif "-d" in a:
    os.path.exists(state) and os.remove(state)
else:
    open(state, "w").write(json.loads(a[4])["name"])
"""


class RealCommands(Case):
    """Skutečné podprocesy proti falešným `pw-dump` a `pw-metadata`."""

    def _tools(self) -> tuple[str, str, Path]:
        d = self.dir / "pw"
        d.mkdir()
        out = []
        for name, body in (("pw-dump", FAKE_PW_DUMP), ("pw-metadata", FAKE_PW_METADATA)):
            path = d / name
            path.write_text(body.format(python=sys.executable, dir=str(d), usb=USB,
                                        fixture=str(ROOT / "tests/fixtures/pw-dump-pi.json")))
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
            out.append(str(path))
        return out[0], out[1], d

    def test_monitor_stream_hotplug_and_the_exact_metadata_command(self) -> None:
        dump, meta, d = self._tools()
        (d / "pin").write_text(USB)

        async def go():
            cfg = Config(**{**DEFAULTS, "audio_output": USB})
            m = ao.AudioOutputs(cfg, FakePlayer(volume=10), state_file=self.dir / "state.json",
                                pw_dump=dump, pw_metadata=meta)
            m.state.update(adopted=True, last=USB)
            m._load_state = lambda: None  # type: ignore[method-assign]
            await m.start()
            for _ in range(100):
                if m.available:
                    break
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.2)
            first = m.snapshot()
            # nová karta: PipeWire pošle jen změnu, nic se nerestartuje
            (d / "001.json").write_text(json.dumps([{"id": 300, "type": NODE, "info": {"props": {
                "node.name": "alsa_output.usb-Nova-00.analog-stereo", "media.class": "Audio/Sink",
                "node.description": "Nová Analog Stereo", "priority.session": 2000}}}]))
            await asyncio.sleep(0.3)
            second = m.snapshot()
            cfg.audio_output = SOUNDBAR
            await m.config_changed(previous=USB, who={"ip": "10.0.0.7"}, wait=0.1)
            cfg.audio_output = ""
            await m.config_changed(previous=SOUNDBAR, who={"ip": "10.0.0.7"}, wait=0.1)
            pid = m._proc.pid if m._proc else None
            await m.stop()
            await asyncio.sleep(0.05)
            return first, second, pid, json.loads((self.dir / "state.json").read_text())

        first, second, pid, state = run(go())
        self.assertEqual(first["playing"], USB)
        self.assertEqual(len(first["choices"]), 5)
        self.assertIn("Nová", [c["label"] for c in second["choices"]])
        calls = [json.loads(line) for line in (d / "calls.log").read_text().splitlines()]
        writes = [c for c in calls if c != ["-n", "default", "0"]]
        self.assertEqual(writes, [
            ["-n", "default", "0", "default.configured.audio.sink",
             json.dumps({"name": SOUNDBAR}), "Spa:String:JSON"],
            ["-n", "default", "-d", "0", "default.configured.audio.sink"]])
        self.assertIsNotNone(pid)
        with self.assertRaises(ProcessLookupError):  # sledování po stop() neběží dál
            os.kill(pid, 0)
        self.assertIn(SOUNDBAR, state["known"])  # zapamatováno na disk

    def test_monitor_that_dies_is_restarted_and_settings_say_it(self) -> None:
        dump, meta, d = self._tools()
        broken = d / "pw-dump-broken"
        broken.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(1)\n")
        broken.chmod(0o755)

        async def go():
            m = ao.AudioOutputs(Config(**DEFAULTS), FakePlayer(), state_file=self.dir / "s.json",
                                pw_dump=str(broken), pw_metadata=meta)
            m.state.update(adopted=True)
            await m.start()
            await asyncio.sleep(0.15)
            down = m.snapshot()
            m.pw_dump = dump  # PipeWire je zpátky
            for _ in range(100):
                if m.available:
                    break
                await asyncio.sleep(0.02)
            up = m.snapshot()
            await m.stop()
            return down, up

        down, up = run(go())
        self.assertEqual((down["available"], up["available"]), (False, True))
        self.assertIn("PipeWire neodpovídá", down["notes"][0])
        self.assertEqual([e["ok"] for e in self.kinds("audio.monitor")], [False, True])


class WebSetting(Case):
    """F-ZVUK-28: nastavení na webu — volba ze seznamu serveru, s PINem."""

    def _server(self, m):
        from test_admin import make
        from ytdj import adminpin

        srv, app, routes, _ = make(self.dir)
        app.audio = m
        app.cfg = m.cfg
        return routes, app, adminpin.ensure_pin(self.dir / "admin-pin")

    def test_field_is_a_choice_from_the_server_list_and_live(self) -> None:
        from test_admin import call

        async def go():
            m, wp, _, _ = self.make(chosen=USB)
            wp.boot()
            await self.settle()
            routes, app, pin = self._server(m)
            cfgr = await call(routes, "GET", "/api/config", pin=pin)
            live = await call(routes, "GET", "/api/audio/outputs", pin=pin)
            nopin = await call(routes, "GET", "/api/audio/outputs")
            return json.loads(cfgr.body), json.loads(live.body), nopin.status_code

        cfgd, live, nopin = run(go())
        field = [f for f in cfgd["fields"] if f["key"] == "audio_output"][0]
        self.assertEqual((field["label"], field["type"], field["restart"], field["live"], field["disabled"]),
                         ("Zvukový výstup", "choice", False, "/api/audio/outputs", False))
        self.assertEqual(field["choices"], live["choices"])
        self.assertEqual(cfgd["values"]["audio_output"], USB)
        self.assertEqual(field["notes"], ["Teď hraje: USB Audio Device."])
        self.assertEqual(nopin, 401)  # bez PINu ani seznam
        cap = [f for f in cfgd["fields"] if f["key"] == "audio_switch_volume"][0]
        self.assertEqual((cap["type"], cap["restart"]), ("int", False))

    def test_post_takes_only_offered_outputs_and_reports_what_happened(self) -> None:
        from test_admin import call
        from ytdj.web import server as web

        async def go():
            m, wp, player, cfg = self.make(chosen=USB, volume=55)
            wp.boot()
            await self.settle()
            routes, app, pin = self._server(m)
            out = []
            with mock.patch.object(web.cfgmod, "save_values") as save:
                for value in ("alsa_output.neznama-karta", "x; reboot", "$(id)", "--help", 7, None):
                    r = await call(routes, "POST", "/api/config", {"audio_output": value}, pin=pin)
                    out.append(r.status_code)
                bad = (list(out), save.call_count, cfg.audio_output, list(wp.writes()))
                r = await call(routes, "POST", "/api/config", {"audio_output": SOUNDBAR}, pin=pin)
                nopin = await call(routes, "POST", "/api/config", {"audio_output": USB})
            await self.settle()
            return bad, r, save.call_args_list, cfg, wp, nopin.status_code

        (codes, saves, chosen, writes), r, saved, cfg, wp, nopin = run(go())
        self.assertEqual(codes, [400] * 6)
        self.assertEqual((saves, chosen, writes), (0, USB, []))  # nic z toho se nikam nedostalo
        self.assertEqual(r.status_code, 200, r.body)
        body = json.loads(r.body)
        self.assertEqual(body["restart_required"], [])  # bez restartu hudby
        self.assertEqual(body["notes"], ["Hraje: AC511 Sound Bar."])  # hlasitost zůstala
        self.assertEqual(saved[0].args[0], {"audio_output": SOUNDBAR})  # config.toml přes zapisovač
        self.assertEqual((cfg.audio_output, wp.pin), (SOUNDBAR, SOUNDBAR))
        self.assertEqual(nopin, 401)
        who = self.kinds("audio.output_choice")[0]
        self.assertEqual((who["was"], who["now"], who["ip"]), (USB, SOUNDBAR, "10.0.0.7"))

    def test_setting_is_refused_honestly_where_there_is_no_pipewire(self) -> None:
        from test_admin import call
        from ytdj.web import server as web

        async def go():
            cfg = Config(**DEFAULTS)
            m = ao.AudioOutputs(cfg, FakePlayer(), state_file=self.dir / "s.json", pw_dump="/neni/pw-dump")
            await m.start()
            routes, app, pin = self._server(m)
            g = await call(routes, "GET", "/api/config", pin=pin)
            with mock.patch.object(web.cfgmod, "save_values") as save:
                p = await call(routes, "POST", "/api/config", {"audio_output": USB}, pin=pin)
            app.audio = None  # i bez správce výstupů (starší sestavení) stránka žije
            g2 = await call(routes, "GET", "/api/config", pin=pin)
            about = await call(routes, "GET", "/api/audio/outputs", pin=pin)
            return json.loads(g.body), p, save.call_count, json.loads(g2.body), json.loads(about.body)

        g, p, saves, g2, live = run(go())
        field = [f for f in g["fields"] if f["key"] == "audio_output"][0]
        self.assertTrue(field["disabled"])
        self.assertIn("Není k dispozici", field["choices"][0]["label"])
        self.assertEqual((p.status_code, saves), (400, 0))
        self.assertIn("není k dispozici", json.loads(p.body)["error"])
        self.assertTrue([f for f in g2["fields"] if f["key"] == "audio_output"][0]["disabled"])
        self.assertIs(live["available"], False)

    def test_page_refreshes_the_list_while_settings_are_open(self) -> None:
        html = (ROOT / "ytdj/web/static/index.html").read_text(encoding="utf-8")
        for needle in ("function refreshLive()", "setInterval(refreshLive, 3000)", "stopLive();",
                       "adminFetch(f.live)", 'class="notes"', "data.notes"):
            self.assertIn(needle, html, needle)


class Deploy(unittest.TestCase):
    """F-ZVUK-29: nasazení volbu výstupu nikdy potichu nezahodí."""

    def test_deploy_no_longer_deletes_the_pinned_default(self) -> None:
        text = (ROOT / "packaging/rpi/deploy.sh").read_text(encoding="utf-8")
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotIn("default-nodes", code)
        conf = (ROOT / "packaging/rpi/51-ytdj-audio-priority.conf").read_text(encoding="utf-8")
        self.assertNotIn("deploy.sh\n# clears that pin", conf)
        self.assertIn("audio_output", conf)  # kde se výstup vybírá


def _sine(path: Path, secs: float, amp: float = 0.25, rate: int = 48000) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(
            struct.pack("<h", int(32767 * amp * math.sin(2 * math.pi * 440 * i / rate))) * 2
            for i in range(int(rate * secs))))


_PW_TOOLS = ("pipewire", "wireplumber", "pw-cli", "pw-link", "pw-record", "pw-dump", "pw-metadata", "mpv")


@unittest.skipUnless(all(shutil.which(t) for t in _PW_TOOLS), "PipeWire / WirePlumber / mpv tu nejsou")
class RealPipeWire(unittest.TestCase):
    """F-ZVUK-29: skutečné mpv hraje do soukromého PipeWire (vlastní socket,
    žádné zvukové karty, dva prázdné výstupy A a B — zvuku stroje se to
    nedotkne) a skutečný správce výstupů ho přepne. Jeden záznam bere monitor
    A do levého a monitor B do pravého kanálu."""

    RATE = 48000

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(dir=_TMP))
        run_dir, conf = self.tmp / "run", self.tmp / "conf"
        run_dir.mkdir()
        # bez zvukových karet, kamer a bluetooth — pro WirePlumber 0.4 (lua) i 0.5 (conf)
        for sub, text in (("main.lua.d/90-off.lua", "alsa_monitor.enabled = false\n"
                           "v4l2_monitor.enabled = false\nlibcamera_monitor.enabled = false\n"),
                          ("bluetooth.lua.d/90-off.lua", "bluez_monitor.enabled = false\n"),
                          ("wireplumber.conf.d/90-off.conf", "wireplumber.profiles = { main = {\n"
                           "  monitor.alsa = disabled\n  monitor.bluez = disabled\n"
                           "  monitor.bluez-midi = disabled\n  monitor.v4l2 = disabled\n"
                           "  monitor.libcamera = disabled\n} }\n")):
            path = conf / "wireplumber" / sub
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.env = dict(os.environ, XDG_RUNTIME_DIR=str(run_dir), XDG_CONFIG_HOME=str(conf),
                        XDG_STATE_HOME=str(self.tmp / "state"))
        self.env.pop("PIPEWIRE_REMOTE", None)
        self.procs: list[subprocess.Popen] = []
        self.addCleanup(self._cleanup)
        self.spawn("pipewire")
        t0 = time.monotonic()
        while not (run_dir / "pipewire-0").exists():
            if time.monotonic() - t0 > 5:
                self.skipTest("soukromý PipeWire nenastartoval")
            time.sleep(0.05)
        self.spawn("wireplumber")
        time.sleep(1.0)
        for name, prio in (("ytdjTestA", 1000), ("ytdjTestB", 500)):
            r = self.sh("pw-cli", "create-node", "adapter",
                        "{ factory.name=support.null-audio-sink node.name=" + name +
                        " node.description=\"Zkušební " + name[-1] + "\" media.class=Audio/Sink "
                        "object.linger=true audio.position=[FL FR] priority.session=" + str(prio) + " }")
            if r.returncode:
                self.skipTest("prázdný výstup nejde vytvořit")
        time.sleep(0.4)
        names = self.sh("pw-dump").stdout
        if "alsa_output" in names or "ytdjTestB" not in names:
            self.skipTest("soukromý PipeWire není čistý (vidí zvukové karty stroje)")

    def spawn(self, *argv: str) -> subprocess.Popen:
        p = subprocess.Popen(argv, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(p)
        return p

    def sh(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=5)

    def _cleanup(self) -> None:
        for p in reversed(self.procs):
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
        for p in self.procs:
            try:
                p.wait(3)
            except subprocess.TimeoutExpired:
                p.kill()

    def test_switch_keeps_playing_with_gain_filter_mute_seek_and_gapless(self) -> None:
        from ytdj.player.mpv import GAIN_CLIENT, GAIN_SCRIPT

        first, second = self.tmp / "watch?v=TRACK000000", self.tmp / "watch?v=TRACK000001"
        _sine(first, 9.0)
        _sine(second, 3.0)
        rec, sock = self.tmp / "rec.wav", self.tmp / "mpv.sock"
        self.spawn("pw-record", "--rate", str(self.RATE), "--channels", "2", "--format", "f32", "-P",
                   "{ node.autoconnect=false node.name=ytdjTestRec }", str(rec))
        time.sleep(0.6)
        for a, b in (("ytdjTestA:monitor_FL", "ytdjTestRec:input_FL"),
                     ("ytdjTestB:monitor_FL", "ytdjTestRec:input_FR")):
            self.assertEqual(self.sh("pw-link", a, b).returncode, 0)
        mpv = self.spawn("mpv", "--no-config", "--idle=yes", "--no-video", "--no-terminal", "--ytdl=no",
                         f"--input-ipc-server={sock}", "--ao=pipewire", "--gapless-audio=weak",
                         "--audio-buffer=2", "--cache=yes", "--volume=100", "--volume-max=100",
                         f"--script={GAIN_SCRIPT}")
        t0 = time.monotonic()
        while not sock.exists():
            self.assertLess(time.monotonic() - t0, 10)
            time.sleep(0.02)
        s = socket.socket(socket.AF_UNIX)
        s.connect(str(sock))
        s.settimeout(10)
        f = s.makefile("rwb")
        rid = [0]

        def cmd(*c):
            rid[0] += 1
            f.write((json.dumps({"command": list(c), "request_id": rid[0]}) + "\n").encode())
            f.flush()
            while True:
                m = json.loads(f.readline())
                if m.get("request_id") == rid[0]:
                    return m.get("data")

        events: list[tuple[str, dict]] = []

        class Player:  # co správce výstupů z přehrávače potřebuje
            proc = SimpleNamespace(pid=mpv.pid)
            lowered: list = []

            async def lower_volume(self, limit):
                self.lowered.append(limit)
                return None

        async def scenario():
            cfg = Config(**{**DEFAULTS, "audio_output": "ytdjTestA", "audio_switch_volume": 0})
            m = ao.AudioOutputs(cfg, Player(), state_file=self.tmp / "state.json")
            m.state.update(adopted=True)
            await m.start()
            for _ in range(150):
                if m.available and len(m.graph.sinks()) == 2:
                    break
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.3)
            marks = {}
            cmd("script-message-to", GAIN_CLIENT, "gain", "TRACK000000", "-6.0", "-8.0")
            cmd("script-message-to", GAIN_CLIENT, "gain", "TRACK000001", "-12.0", "-2.0")
            cmd("loadfile", str(first), "append-play")
            cmd("loadfile", str(second), "append-play")
            await asyncio.sleep(2.0)
            marks["a"] = m.snapshot()["playing"]
            pos0 = cmd("get_property", "time-pos")
            cfg.audio_output = "ytdjTestB"
            t = time.monotonic()
            notes = await m.config_changed(previous="ytdjTestA", who={"ip": "test"})
            marks["switch_s"] = time.monotonic() - t
            await asyncio.sleep(1.5)
            marks["b"] = m.snapshot()["playing"]
            marks["advanced"] = cmd("get_property", "time-pos") - pos0  # hraje dál, nezačalo znovu
            marks["volume"], marks["af"] = cmd("get_property", "volume"), cmd("get_property", "af")
            cmd("set_property", "mute", True)  # ztlumení po přepnutí
            await asyncio.sleep(0.8)
            cmd("set_property", "mute", False)
            await asyncio.sleep(0.6)
            cmd("seek", 6.0, "absolute")  # posun po přepnutí; za 3 s navazuje druhá skladba
            await asyncio.sleep(0.4)
            marks["after_seek"] = cmd("get_property", "time-pos")
            await asyncio.sleep(1.6)  # mpv už čte druhou skladbu (první doznívá ze zásoby)
            marks["second"] = cmd("get_property", "path")
            await asyncio.sleep(3.0)
            marks["notes"] = notes
            await m.stop()
            return marks

        with mock.patch.dict(os.environ, {k: self.env[k] for k in ("XDG_RUNTIME_DIR", "XDG_CONFIG_HOME",
                                                                    "XDG_STATE_HOME")}), \
                mock.patch.object(telemetry, "event", lambda kind, **fl: events.append((kind, fl))):
            marks = run(scenario())
        cmd("quit")
        f.close()
        s.close()
        self._cleanup()

        self.assertEqual((marks["a"], marks["b"]), ("ytdjTestA", "ytdjTestB"))
        self.assertEqual(marks["notes"], ["Hraje: Zkušební B."])
        self.assertGreater(marks["advanced"], 1.0)
        self.assertEqual(marks["volume"], 100)  # hlasitost mpv beze změny (pojistka tu je vypnutá)
        self.assertEqual([x["label"] for x in marks["af"]], ["ytdjgain"])  # filtr skladby zůstal
        self.assertTrue(6.0 <= marks["after_seek"] < 7.0, marks["after_seek"])
        self.assertTrue(str(marks["second"]).endswith("TRACK000001"))
        applied = [fl for k, fl in events if k == "audio.output_apply"][-1]
        self.assertEqual((applied["target"], applied["ok"], applied["playing"]), ("ytdjTestB", True, "ytdjTestB"))
        self.assertLess(applied["moved_ms"], 1500)

        blob = rec.read_bytes()
        raw = blob[blob.index(b"data") + 8:]
        frames = list(struct.iter_unpack("<ff", raw[:len(raw) // 8 * 8]))
        A, B = [x[0] for x in frames], [x[1] for x in frames]

        def level(x: list[float]) -> float:  # dB proti sinusu s amplitudou 0,25
            rms = math.sqrt(sum(v * v for v in x) / max(1, len(x)))
            return 20 * math.log10(max(rms, 1e-9) / (0.25 / math.sqrt(2)))

        win = self.RATE // 10
        lv = [(level(A[i:i + win]), level(B[i:i + win])) for i in range(0, len(A) - win, win)]
        on_a = [i for i, (a, b) in enumerate(lv) if a > -30]
        on_b = [i for i, (a, b) in enumerate(lv) if b > -30]
        self.assertTrue(on_a and on_b, "zvuk musí být nejdřív na A, potom na B")
        self.assertLess(max(on_a), min(on_b) + 2)  # po přepnutí už na A nic
        # mezera při přepnutí: nejvýš pár desetin vteřiny (měřeno 0–85 ms)
        self.assertLessEqual(min(on_b) - max(on_a), 4)
        # před přepnutím i po něm hraje první skladba se svým ziskem −6 dB
        self.assertAlmostEqual(lv[on_a[len(on_a) // 2]][0], -6.0, delta=0.7)
        self.assertAlmostEqual(lv[min(on_b) + 3][1], -6.0, delta=0.7)
        # ztlumení: na B je díra (≥ 0,5 s ticha) a pak zase zvuk
        gaps = [i for i in range(min(on_b), max(on_b)) if lv[i][1] < -60]
        self.assertGreaterEqual(len(gaps), 5, "ztlumení po přepnutí musí být slyšet (ticho)")
        # druhá skladba navázala na B se svým ziskem −12 dB (filtr na skladbu, gapless)
        tail = [b for a, b in lv[max(on_b) - 12:max(on_b) - 2]]
        self.assertTrue(any(abs(b + 12.0) < 0.8 for b in tail), tail)


if __name__ == "__main__":
    unittest.main()
