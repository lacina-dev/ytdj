"""Network screens: nmcli parsing, the QR encoder, and the whole flow in the sim.

    python -m unittest tests.test_panel_net -v
"""

from __future__ import annotations

import io
import json
import logging
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fake_ytdj import make_server  # noqa: E402
from test_panel import EventLog  # noqa: E402

# no cover art from the internet in tests (fake ids aren't YouTube ids anyway)
os.environ.setdefault("YTDJ_PANEL_ART_URL", "")
from ytdj.panel.app import PanelApp  # noqa: E402
from ytdj.panel.net import (  # noqa: E402
    Connected,
    Link,
    NetError,
    NetStatus,
    NmcliBackend,
    WifiNet,
    friendly_error,
    parse_links,
    parse_wifi_list,
    split_terse,
)
from ytdj.panel.netui import (  # noqa: E402
    BACK,
    BTN_FULL,
    BTN_L,
    BTN_R,
    DOWN,
    EYE,
    WIFI_BTN,
    NetRenderer,
    NetView,
    kb_keys,
    list_row,
)
from ytdj.panel.qr import encode  # noqa: E402
from ytdj.panel.sim import SimScreen, SimTouch  # noqa: E402
from ytdj.panel.ui import NET_TARGET, PLAY  # noqa: E402

SECRET = "tajne-Heslo:42"


def center(box):
    return (box[0] + box[2]) // 2, (box[1] + box[3]) // 2


def wait_for(pred, timeout=8.0, step=0.03):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return False


class ParseTest(unittest.TestCase):
    def test_split_terse_escapes(self):
        self.assertEqual(
            split_terse(r"*:Moje\:síť:53:WPA2:70\:54\:25\:73\:4E\:E2"),
            ["*", "Moje:síť", "53", "WPA2", "70:54:25:73:4E:E2"],
        )
        self.assertEqual(split_terse(r"a\\b:c\\:"), ["a\\b", "c\\", ""])
        self.assertEqual(split_terse(""), [""])

    def test_wifi_list(self):
        out = "\n".join([
            r" :O2-Internet-58A1:40:WPA2",
            r" :O2-Internet-58A1:78:WPA2",  # second AP of the same network
            r"*:EsteTesteReste:53:WPA2",
            r" ::90:WPA2",  # hidden
            r" :Kavárna\: host:61:--",
            r" :weird:abc:WPA1 WPA2",
        ])
        nets = parse_wifi_list(out)
        self.assertEqual([n.ssid for n in nets], ["EsteTesteReste", "O2-Internet-58A1", "Kavárna: host", "weird"])
        self.assertTrue(nets[0].in_use)
        self.assertEqual(nets[1].signal, 78)
        self.assertFalse(nets[2].secure)
        self.assertTrue(nets[3].secure)
        self.assertEqual(nets[3].signal, 0)

    def test_dev_show(self):
        out = (
            "GENERAL.DEVICE:eth0\nGENERAL.TYPE:ethernet\nGENERAL.STATE:100 (connected)\n"
            "IP4.ADDRESS[1]:10.42.0.149/24\nIP4.ADDRESS[2]:10.0.0.5/8\n\n"
            "GENERAL.DEVICE:wlan0\nGENERAL.TYPE:wifi\n"
            "GENERAL.STATE:70 (connecting (getting IP configuration))\n\n"
            "GENERAL.DEVICE:lo\nGENERAL.TYPE:loopback\nGENERAL.STATE:100 (connected (externally))\n"
            "IP4.ADDRESS[1]:127.0.0.1/8\n\n"
            "GENERAL.DEVICE:p2p-dev-wlan0\nGENERAL.TYPE:wifi-p2p\nGENERAL.STATE:30 (disconnected)\n"
        )
        links = parse_links(out)
        self.assertEqual(links, [
            Link("eth0", "ethernet", "connected", "10.42.0.149"),
            Link("wlan0", "wifi", "connecting", ""),
        ])
        self.assertTrue(links[0].up)
        self.assertFalse(links[1].up)

    def test_friendly_error(self):
        self.assertEqual(
            friendly_error("Error: Connection activation failed: (7) Secrets were required, but not provided."),
            "wrong_password",
        )
        self.assertEqual(friendly_error("Error: No network with SSID 'x' found."), "not_found")
        self.assertEqual(
            friendly_error("Error: 802-11-wireless-security.psk: property is invalid."), "bad_password"
        )
        # anything else is shown as is, minus the password should it ever turn up
        self.assertEqual(friendly_error(f"Error: weird {SECRET} thing", SECRET), "weird ••• thing")


class QrTest(unittest.TestCase):
    def test_sizes_and_finders(self):
        for text, size in (("http://10.42.0.149:8765", 25), ("x" * 60, 33)):
            m = encode(text)
            self.assertEqual(len(m), size)
            self.assertTrue(all(len(row) == size for row in m))
            # finder pattern in the top-left corner: a dark 7×7 ring
            self.assertTrue(all(m[0][:7]) and all(m[6][:7]))
            self.assertFalse(m[1][1])
        with self.assertRaises(ValueError):
            encode("x" * 500)

    def test_decodes(self):
        try:
            import cv2  # noqa: F401
            import numpy as np
        except ImportError:
            self.skipTest("OpenCV není — dekódování se nedá ověřit")
        det = cv2.QRCodeDetector()
        for text in ("http://192.168.100.200:8765", "http://ytdj.local:8765"):
            m = encode(text)
            n, s = len(m), 6
            img = np.full(((n + 8) * s, (n + 8) * s), 255, np.uint8)
            for y, row in enumerate(m):
                for x, dark in enumerate(row):
                    if dark:
                        img[(y + 4) * s:(y + 5) * s, (x + 4) * s:(x + 5) * s] = 0
            self.assertEqual(det.detectAndDecode(img)[0], text)


class NmcliBackendTest(unittest.TestCase):
    """The real backend against a fake `nmcli` script."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.log = self.dir / "calls"
        self.profiles = self.dir / "profiles"
        self.profiles.write_text("11111111-old:802-11-wireless\n")

    def tearDown(self):
        self.tmp.cleanup()

    def _nmcli(self, connect_ok: bool) -> str:
        script = self.dir / "nmcli"
        script.write_text(f"""#!/bin/sh
echo "$@" >> {self.log}
case "$*" in
  *"con show"*) cat {self.profiles} ;;
  *"con delete"*) : ;;
  *"wifi connect"*)
    {"exit 0" if connect_ok else f'echo "22222222-new:802-11-wireless" >> {self.profiles}; echo "Error: Connection activation failed: (7) Secrets were required, but not provided." >&2; exit 4'} ;;
  *"dev show"*) printf 'GENERAL.DEVICE:wlan0\\nGENERAL.TYPE:wifi\\nGENERAL.STATE:100 (connected)\\nIP4.ADDRESS[1]:192.168.1.57/24\\n' ;;
  *"wifi list"*) printf '*:Nová síť:70:WPA2\\n' ;;
esac
""")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return str(script)

    def test_connect_fails_without_leaking(self):
        backend = NmcliBackend(nmcli=self._nmcli(connect_ok=False))
        with self.assertLogs("ytdj", level=logging.DEBUG) as logs:
            with self.assertRaises(NetError) as ctx:
                backend.connect("Nová síť", SECRET)
        self.assertEqual(str(ctx.exception), "wrong_password")
        self.assertNotIn(SECRET, "\n".join(logs.output))
        calls = self.log.read_text()
        # the half-made profile goes, the one that existed before stays
        self.assertIn("con delete uuid 22222222-new", calls)
        self.assertNotIn("11111111-old", calls.split("con delete", 1)[1])

    def test_connect_ok(self):
        backend = NmcliBackend(nmcli=self._nmcli(connect_ok=True))
        with self.assertLogs("ytdj", level=logging.DEBUG) as logs:
            res = backend.connect("Nová síť", SECRET)
        self.assertEqual(res, Connected("Nová síť", "192.168.1.57"))
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertIn(f"dev wifi connect Nová síť password {SECRET} ifname wlan0", self.log.read_text())

    def test_status(self):
        st = NmcliBackend(nmcli=self._nmcli(connect_ok=True)).status()
        self.assertEqual(st.link("wifi"), Link("wlan0", "wifi", "connected", "192.168.1.57", "Nová síť", 70))

    def test_no_nmcli(self):
        st = NmcliBackend(nmcli=str(self.dir / "missing")).status()
        self.assertEqual(st.error, "no_nm")
        self.assertFalse(st.online)


class FakeNet:
    """A NetworkManager stand-in: fixed networks, scripted outcomes."""

    def __init__(self):
        self.lock = threading.Lock()
        self.st = NetStatus(
            "ytdj", True,
            (Link("eth0", "ethernet", "connected", "10.42.0.149"), Link("wlan0", "wifi", "disconnected")),
        )
        self.nets = [WifiNet(f"síť-{i}", 90 - i * 10, "WPA2") for i in range(6)] + [WifiNet("otevřená", 30, "")]
        self.fail = None  # error code the next connect raises
        self.delay = 0.3
        self.connects: list[tuple[str, str | None]] = []
        self.scans = 0

    def status(self):
        with self.lock:
            return self.st

    def scan(self, rescan=False):
        self.scans += 1
        time.sleep(0.05)
        return list(self.nets)

    def connect(self, ssid, password):
        time.sleep(self.delay)
        self.connects.append((ssid, password))
        if self.fail:
            raise NetError(self.fail)
        with self.lock:
            self.st = replace(self.st, links=(
                self.st.links[0], Link("wlan0", "wifi", "connected", "192.168.1.57", ssid, 70),
            ))
        return Connected(ssid, "192.168.1.57")


class NetFlowTest(unittest.TestCase):
    def setUp(self):
        self.log = EventLog()
        self.server, self.fake = make_server(0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.screen = SimScreen(Path(self.tmp.name) / "panel.png")
        self.touch = SimTouch(io.StringIO(""))
        self.net = FakeNet()
        self.app = PanelApp(
            self.screen, self.touch, f"http://127.0.0.1:{self.server.server_address[1]}", net_backend=self.net,
        )
        self.app.page_guard = 0.0  # the tests tap faster than a finger can (see PageGuardTest)
        self.thread = threading.Thread(target=self.app.run, daemon=True)
        self.thread.start()
        self.assertTrue(wait_for(lambda: self.app.online), "panel se nepřipojil")
        self.assertTrue(wait_for(lambda: self.app.net.status is not None))

    def tearDown(self):
        self.app.shutdown()
        self.thread.join(3)
        self.server.closing = True
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()
        self.log.close()

    def page(self):
        return self.app.net.page

    def tap(self, box):
        self.touch.tap(*center(box))
        time.sleep(0.05)

    def shown(self, page):
        return wait_for(lambda: self.app._shown_page == page)

    def open_net(self):
        self.tap(NET_TARGET)
        self.assertTrue(wait_for(lambda: self.page() == "overview"))
        self.assertTrue(self.shown("overview"))

    def type_text(self, text):
        for ch in text:
            if self.app.net.kb_page != "abc" and ch.isalpha():
                self.tap(dict(kb_keys(self.app.net.kb_page))["mode"])
            if ch.isupper() and not self.app.net.shift:
                self.tap(dict(kb_keys("abc"))["shift"])
            keys = dict(kb_keys(self.app.net.kb_page))
            kid = f"c:{ch.lower() if self.app.net.kb_page == 'abc' else ch}"
            if kid not in keys:
                for page in ("123", "#+="):
                    while self.app.net.kb_page != page:
                        cur = dict(kb_keys(self.app.net.kb_page))
                        self.tap(cur["mode"] if self.app.net.kb_page == "abc" else cur["sym"])
                    keys = dict(kb_keys(page))
                    if kid in keys:
                        break
            n = len(self.app.net.password)
            self.tap(keys[kid])
            self.assertTrue(wait_for(lambda: len(self.app.net.password) == n + 1), ch)

    def test_open_and_back(self):
        self.open_net()
        full_before = self.screen.calls
        v = self.app.net.view(time.monotonic())
        self.assertEqual(v.urls, (f"http://10.42.0.149:{self.app.api.port}", f"http://ytdj.local:{self.app.api.port}"))
        self.tap(BACK)
        self.assertTrue(wait_for(lambda: self.page() is None))
        self.assertTrue(self.shown("player"))
        self.assertGreater(self.screen.calls, full_before)
        # the player works as before
        self.tap(PLAY)
        self.assertTrue(wait_for(lambda: self.fake.paused))

    def test_net_button_works_while_ytdj_is_down(self):
        self.server.closing = True
        self.server.shutdown()
        self.server.server_close()
        self.assertTrue(wait_for(lambda: not self.app.online, timeout=10))
        self.open_net()

    def test_media_keys_and_state_while_open(self):
        self.open_net()
        self.app.events.put(("key", "vol_up"))
        self.assertTrue(wait_for(lambda: self.fake.volume == 67))
        # the network page says so in its header, briefly
        self.assertTrue(wait_for(lambda: self.app.net.renderer._sigs.get("title") == ("Síť", "hlasitost 67")))
        before = self.fake.index
        self.app.events.put(("key", "next"))
        self.assertTrue(wait_for(lambda: self.fake.index != before))
        self.assertTrue(wait_for(lambda: self.app.track_key == f"a{self.fake.index + 1}"))
        self.tap(BACK)
        self.assertTrue(self.shown("player"))
        self.assertEqual(self.app._view().volume, 67)
        # what's on the glass now is the player with the new track
        self.assertEqual(self.app.renderer._sigs["track"][1], self.app._view().title)

    def test_list_scroll_and_connect_with_password(self):
        self.open_net()
        self.tap(WIFI_BTN)
        self.assertTrue(wait_for(lambda: self.page() == "list" and len(self.app.net.nets) == 7))
        self.tap(DOWN)
        self.assertTrue(wait_for(lambda: self.app.net.scroll == 3))
        self.tap(list_row(1))  # síť-4
        self.assertTrue(wait_for(lambda: self.page() == "keys"))
        self.assertEqual(self.app.net.ssid, "síť-4")

        self.tap(dict(kb_keys("abc"))["ok"])  # too short: a hint, no connect
        self.assertTrue(wait_for(lambda: self.app.net.hint))
        self.assertEqual(self.page(), "keys")

        with self.assertLogs("ytdj", level=logging.DEBUG) as logs:
            logging.getLogger("ytdj.panel").setLevel(logging.DEBUG)
            try:
                self.type_text(SECRET)
                self.tap(EYE)
                self.assertTrue(wait_for(lambda: self.app.net.show_pw))
                self.tap(dict(kb_keys(self.app.net.kb_page))["ok"])
                self.assertTrue(wait_for(lambda: self.page() == "connect"))
                self.assertTrue(wait_for(lambda: self.app.net.phase == "ok"))
            finally:
                logging.getLogger("ytdj.panel").setLevel(logging.NOTSET)
        self.assertEqual(self.net.connects, [("síť-4", SECRET)])
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertEqual(self.app.net.password, "")  # forgotten once used
        v = self.app.net.view(time.monotonic())
        self.assertEqual(v.result_url, f"http://192.168.1.57:{self.app.api.port}")
        self.tap(BTN_FULL)  # Hotovo
        self.assertTrue(wait_for(lambda: self.page() == "overview"))
        self.assertTrue(wait_for(lambda: self.app.net.urls()[:1] == (f"http://192.168.1.57:{self.app.api.port}",)))

        # the event log: the SSID and the outcome are there, the password never
        self.assertTrue(self.log.wait("panel.net_connect_result", ssid="síť-4", ok=True))
        self.assertTrue(self.log.find("panel.net_connect", ssid="síť-4", secure=True))
        self.assertTrue(self.log.find("panel.net_scan", ok=True, networks=7))
        self.assertTrue(self.log.find("panel.action", button="net", source="touch"))
        pages = [(e["previous"], e["page"]) for e in self.log.all("panel.screen")]
        for step in (("player", "overview"), ("overview", "list"), ("list", "keys"), ("keys", "connect")):
            self.assertIn(step, pages)
        keys = [e["button"] for e in self.log.all("panel.net_action") if e["page"] == "keys"]
        self.assertIn("ok", keys)
        self.assertFalse([k for k in keys if k not in ("ok", "cancel")])  # typed characters never
        self.assertTrue(self.log.wait("panel.net_status", wifi_ssid="síť-4", wifi_ip="192.168.1.57"))
        text = self.log.text()
        self.assertNotIn(SECRET, text)
        self.assertNotIn(json.dumps(SECRET)[1:-1], text)

    def test_connect_failure_and_retry(self):
        self.net.fail = "wrong_password"
        self.open_net()
        self.tap(WIFI_BTN)
        self.assertTrue(wait_for(lambda: len(self.app.net.nets) == 7))
        self.tap(list_row(0))
        self.assertTrue(wait_for(lambda: self.page() == "keys"))
        self.type_text("heslo123")
        self.tap(dict(kb_keys(self.app.net.kb_page))["bksp"])
        self.assertTrue(wait_for(lambda: self.app.net.password == "heslo12"))
        self.type_text("4")
        self.tap(dict(kb_keys(self.app.net.kb_page))["ok"])
        self.assertTrue(wait_for(lambda: self.app.net.phase == "error"))
        self.assertEqual(self.app.net.result_error, "Špatné heslo.")
        self.assertTrue(self.shown("connect"))
        self.net.fail = None
        self.tap(BTN_R)  # Zkusit znovu → the keyboard, password still there
        self.assertTrue(wait_for(lambda: self.page() == "keys"))
        self.assertEqual(self.app.net.password, "heslo124")
        self.tap(dict(kb_keys(self.app.net.kb_page))["ok"])
        self.assertTrue(wait_for(lambda: self.app.net.phase == "ok"))
        self.assertEqual(self.net.connects[-1], ("síť-0", "heslo124"))
        self.assertTrue(wait_for(lambda: len(self.log.all("panel.net_connect_result")) == 2))
        results = self.log.all("panel.net_connect_result")
        self.assertEqual([(r["ok"], r.get("error"), r["attempt"]) for r in results],
                         [(False, "wrong_password", 1), (True, None, 2)])
        self.assertNotIn("heslo12", self.log.text())

    def test_error_text_never_carries_the_password(self):
        # a backend that (wrongly) puts the password into its error message
        self.net.fail = f"nmcli: bad key {SECRET} for síť-0"
        self.open_net()
        self.tap(WIFI_BTN)
        self.assertTrue(wait_for(lambda: len(self.app.net.nets) == 7))
        self.tap(list_row(0))
        self.assertTrue(wait_for(lambda: self.page() == "keys"))
        self.type_text(SECRET)
        self.tap(dict(kb_keys(self.app.net.kb_page))["ok"])
        self.assertTrue(wait_for(lambda: self.app.net.phase == "error"))
        res = self.log.wait("panel.net_connect_result", ok=False)
        self.assertTrue(res)
        self.assertIn("•••", res[0]["error"])
        self.assertNotIn(SECRET, self.log.text())

    def test_open_network_and_cancel(self):
        self.open_net()
        self.tap(WIFI_BTN)
        self.assertTrue(wait_for(lambda: len(self.app.net.nets) == 7))
        self.tap(list_row(0))
        self.assertTrue(wait_for(lambda: self.page() == "keys"))
        self.type_text("abc")
        self.tap(dict(kb_keys("abc"))["cancel"])
        self.assertTrue(wait_for(lambda: self.page() == "list"))
        self.assertEqual(self.app.net.password, "")
        self.tap(DOWN)
        self.assertTrue(wait_for(lambda: self.app.net.scroll == 3))
        self.tap(list_row(3))  # "otevřená" — straight to connecting
        self.assertTrue(wait_for(lambda: self.page() == "connect"))
        self.assertTrue(wait_for(lambda: self.app.net.phase == "ok"))
        self.assertEqual(self.net.connects, [("otevřená", None)])

    def test_key_press_pushes_only_the_key(self):
        self.open_net()
        self.tap(WIFI_BTN)
        self.assertTrue(wait_for(lambda: len(self.app.net.nets) == 7))
        self.tap(list_row(0))
        self.assertTrue(self.shown("keys"))
        time.sleep(0.2)
        box = dict(kb_keys("abc"))["c:g"]
        px = self.screen.pushed_px
        self.touch.feed("down", *center(box))
        self.assertTrue(wait_for(lambda: self.app.net.pressed == "c:g"))
        self.assertTrue(wait_for(lambda: self.screen.pushed_px > px))
        time.sleep(0.1)
        area = (box[2] - box[0]) * (box[3] - box[1])
        self.assertLessEqual(self.screen.pushed_px - px, area)
        self.touch.feed("up", *center(box))
        self.assertTrue(wait_for(lambda: self.app.net.password == "g"))


class NetRenderTest(unittest.TestCase):
    def test_refresh_without_change_pushes_nothing(self):
        v = NetView(loaded=True, hostname="ytdj", eth=Link("eth0", "ethernet", "connected", "10.0.0.2"),
                    urls=("http://10.0.0.2:8765",))
        r = NetRenderer()
        self.assertEqual(r.render(v, full=True), [(0, 0, 480, 320)])
        self.assertEqual(r.render(v), [])

    def test_symbol_page_switch_is_consistent(self):
        """Letters → symbols → letters by diffs must equal a clean full render."""
        v = NetView(page="keys", ssid="x", password="abc")
        r = NetRenderer()
        r.render(v, full=True)
        r.render(replace(v, kb_page="123"))
        r.render(replace(v, kb_page="#+="))
        r.render(v)
        clean = NetRenderer()
        clean.render(v, full=True)
        self.assertEqual(r.frame.tobytes(), clean.frame.tobytes())


if __name__ == "__main__":
    unittest.main()
