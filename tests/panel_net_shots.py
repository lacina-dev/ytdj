"""Renders the panel's network screens to PNGs, for eyeballing the design.

    python tests/panel_net_shots.py [out_dir]

Also prints how many pixels typical in-screen updates push (a key press,
a typed character, a signal change) — those go to the slow SPI glass.
"""

from __future__ import annotations

import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ytdj.panel.net import Link, WifiNet  # noqa: E402
from ytdj.panel.netui import NetRenderer, NetView  # noqa: E402
from ytdj.panel.ui import Renderer  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from panel_shots import PLAYING  # noqa: E402

ETH = Link("eth0", "ethernet", "connected", "10.42.0.149")
WIFI = Link("wlan0", "wifi", "connected", "192.168.0.24", "EsteTesteReste", 54)
BASE = NetView(loaded=True, hostname="ytdj", mdns=True)

NETS = (
    WifiNet("EsteTesteReste", 54, "WPA2", True),
    WifiNet("O2-Internet-58A1", 78, "WPA2"),
    WifiNet("Kavárna U Žluťoučkého koně — host", 61, ""),
    WifiNet("TP-Link_2.4GHz_7C3E", 40, "WPA1 WPA2"),
    WifiNet("sousedi", 22, "WPA2"),
    WifiNet("DIRECT-2F-HP LaserJet", 12, "WPA2"),
)

KEYS = replace(BASE, page="keys", ssid="O2-Internet-58A1", password="kun-Upel7", can_connect=True)

STATES = {
    "overview-eth-only": replace(
        BASE, eth=ETH, wifi=Link("wlan0", "wifi", "disconnected"),
        urls=("http://10.42.0.149:8765", "http://ytdj.local:8765"),
    ),
    "overview-wifi-eth": replace(
        BASE, eth=ETH, wifi=WIFI,
        urls=("http://192.168.0.24:8765", "http://10.42.0.149:8765", "http://ytdj.local:8765"),
    ),
    "overview-offline": replace(
        BASE, mdns=False, eth=Link("eth0", "ethernet", "unavailable"), wifi=Link("wlan0", "wifi", "disconnected"),
    ),
    "overview-loading": NetView(),
    "overview-volume-toast": replace(
        BASE, eth=ETH, wifi=WIFI, note="hlasitost 54",
        urls=("http://192.168.0.24:8765", "http://10.42.0.149:8765", "http://ytdj.local:8765"),
    ),
    "overview-no-nm": replace(BASE, error="NetworkManager (nmcli) není k dispozici."),
    "list": replace(BASE, page="list", nets=NETS, scanned=True),
    "list-scrolled": replace(BASE, page="list", nets=NETS, scanned=True, scroll=3, pressed="row1"),
    "list-scanning": replace(BASE, page="list", scanning=True),
    "list-empty": replace(BASE, page="list", scanned=True),
    "keys-letters-hidden": KEYS,
    "keys-letters-shown-shift": replace(KEYS, show_pw=True, shift=1, pressed="c:k"),
    "keys-caps": replace(KEYS, show_pw=True, shift=2),
    "keys-symbols": replace(KEYS, kb_page="123", show_pw=True, pressed="c:@"),
    "keys-symbols2": replace(KEYS, kb_page="#+=", show_pw=True),
    "keys-short-hint": replace(KEYS, password="abc", can_connect=False, hint="aspoň 8 znaků"),
    "keys-long-shown": replace(KEYS, password="Příliš-dlouhé-heslo-1234567890-abcdefghijklmnop", show_pw=True),
    "keys-long-hidden": replace(KEYS, password="x" * 40),
    "connecting": replace(BASE, page="connect", ssid="O2-Internet-58A1", phase="busy", elapsed=7),
    "connect-ok": replace(
        BASE, page="connect", ssid="O2-Internet-58A1", phase="ok", result_ip="192.168.1.57",
        result_url="http://192.168.1.57:8765",
    ),
    "connect-error": replace(BASE, page="connect", ssid="O2-Internet-58A1", phase="error", result_error="Špatné heslo."),
    "connect-error-long": replace(
        BASE, page="connect", ssid="O2-Internet-58A1", phase="error",
        result_error="Connection activation failed: IP configuration could not be reserved (no available address, timeout, etc.).",
    ),
}


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/ytdj-panel-net-shots")
    out.mkdir(parents=True, exist_ok=True)
    for name, net in (("wifi", ("wifi", 54)), ("eth", ("eth",)), ("off", ("off",))):
        r = Renderer("cs")
        r.render(replace(PLAYING, net=net), full=True)
        r.frame.save(out / f"player-net-{name}.png")
    r = Renderer("cs")
    r.render(replace(PLAYING, net=("wifi", 80), pressed="net", online=False, connecting=False), full=True)
    r.frame.save(out / "player-offline-net-pressed.png")
    for name, view in STATES.items():
        nr = NetRenderer("cs")
        nr.render(view, full=True)
        nr.frame.save(out / f"{name}.png")
    print(out)

    def cost(a: NetView, b: NetView) -> str:
        nr = NetRenderer("cs")
        nr.render(a, full=True)
        t = time.perf_counter()
        boxes = nr.render(b)
        ms = (time.perf_counter() - t) * 1000
        px = sum((x1 - x0) * (y1 - y0) for x0, y0, x1, y1 in boxes)
        return f"{ms:.1f} ms render, {px} px in {boxes}"

    print("key press:        ", cost(KEYS, replace(KEYS, pressed="c:g")))
    print("key typed:        ", cost(replace(KEYS, pressed="c:g"), replace(KEYS, password=KEYS.password + "g")))
    print("shift:            ", cost(KEYS, replace(KEYS, shift=1)))
    print("abc → 123:        ", cost(KEYS, replace(KEYS, kb_page="123")))
    ov = STATES["overview-wifi-eth"]
    print("signal 54 → 61:   ", cost(ov, replace(ov, wifi=replace(WIFI, signal=61))))
    print("refresh, no change:", cost(ov, ov))
    ls = STATES["list"]
    print("list scroll:      ", cost(ls, replace(ls, scroll=3)))


if __name__ == "__main__":
    main()
