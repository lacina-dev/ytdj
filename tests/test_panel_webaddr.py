"""Adresa webu na obrazovkách je ta, která opravdu odpovídá (POZADAVKY #68).

Jako testy panelu se pouští systémovým pythonem (Pillow):

    YTDJ_EVENTS_FILE=/tmp/x.jsonl python3 -m unittest tests.test_panel_webaddr
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if not os.environ.get("YTDJ_EVENTS_FILE"):
    _EVENTS_TMP = tempfile.TemporaryDirectory(prefix="ytdj-webaddr-tests-")
    os.environ["YTDJ_EVENTS_FILE"] = str(Path(_EVENTS_TMP.name) / "events.jsonl")

from fake_ytdj import make_server  # noqa: E402

from ytdj.panel import webaddr  # noqa: E402
from ytdj.panel.net import Link, NetStatus  # noqa: E402
from ytdj.panel.netapp import NetController  # noqa: E402
from ytdj.panel.netui import NetRenderer  # noqa: E402
from ytdj.panel.qr import encode as qr_encode  # noqa: E402
from ytdj.panel.webaddr import Reach, Watch, probe  # noqa: E402
from ytdj.tv import app as tvapp  # noqa: E402
from ytdj.tv.fb import PngScreen  # noqa: E402
from ytdj.tv.screen import Renderer, view_from  # noqa: E402

IP = "192.168.0.24"
_TMP = Path(tempfile.mkdtemp(prefix="ytdj-webaddr-test-"))


def world(port80: bool, names: tuple[str, ...] = (), web_up: bool = True, ip: str = IP,
          names80: tuple[str, ...] | None = None):
    """Falešná síť: co se přeloží a co odpoví. Vrací (answers, resolves, own_ip, dotazy)."""
    asked: list[tuple[str, int]] = []

    def answers(host: str, port: int) -> bool:
        asked.append((host, port))
        if not web_up:
            return False
        if port == 80 and not port80:
            return False
        if host in (ip, "127.0.0.1"):
            return True
        ok = names80 if (port == 80 and names80 is not None) else names
        return host in ok

    return answers, (lambda name: name in names), (lambda: ip), asked


def look(port80, names=(), **kw) -> Reach | None:
    a, r, o, _ = world(port80, names, **kw)
    return probe(8765, "ytdj", answers=a, resolves=r, own_ip=o)


class Choice(unittest.TestCase):
    def test_best_working_address_with_all_fallbacks(self):
        both = ("jukebox.local", "ytdj.local")
        # port 80 jede a jména fungují → jen jméno, bez portu
        self.assertEqual(look(True, both).address(), "jukebox.local")
        self.assertEqual(look(True, both).url(), "http://jukebox.local")
        # jukebox.local nefunguje (služba druhého jména neběží) → jméno stroje
        self.assertEqual(look(True, ("ytdj.local",)).address(), "ytdj.local")
        # žádné jméno → adresa
        self.assertEqual(look(True).address(), IP)
        # bez portu 80 totéž s portem webu
        self.assertEqual(look(False, both).address(), "jukebox.local:8765")
        self.assertEqual(look(False, ("ytdj.local",)).address(), "ytdj.local:8765")
        self.assertEqual(look(False).address(), f"{IP}:8765")
        self.assertEqual(look(False).url(), f"http://{IP}:8765")
        # jméno se přeloží, ale na portu 80 pod ním neodpoví jukebox → nepoužije se
        r = look(True, both, names80=("ytdj.local",))
        self.assertEqual((r.names, r.address()), (("ytdj.local",), "ytdj.local"))
        # bez sítě není co ukázat (a nic se nevymýšlí)
        self.assertEqual(look(False, ip="").address(), "")
        # web sám na portu 80: nic se neověřuje přes přesměrování, port se nepíše
        a, r_, o, _ = world(False, both)
        self.assertEqual(probe(80, "ytdj", answers=lambda h, p: True, resolves=r_,
                               own_ip=o).address(), "jukebox.local")

    def test_port_80_counts_only_when_our_own_web_answers_there(self):
        # jiný webový server na portu 80 není jukebox
        self.server, _fake = make_server(0)
        port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        try:
            self.assertTrue(webaddr.answers("127.0.0.1", port))  # /api/status jukeboxu
        finally:
            self.server.shutdown()
            self.server.server_close()
        self.assertFalse(webaddr.answers("127.0.0.1", port, timeout=0.5))  # nikdo neposlouchá

        import http.server

        class Other(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = b'{"hello": "world"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        other = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Other)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        try:
            self.assertFalse(webaddr.answers("127.0.0.1", other.server_address[1]))
        finally:
            other.shutdown()
            other.server_close()
        self.assertFalse(webaddr.resolves("tohle-jmeno-neexistuje.invalid"))

    def test_web_down_confirms_nothing_and_the_shown_address_stays(self):
        self.assertIsNone(look(False, ("jukebox.local",), web_up=False))
        results = [Reach(8765, True, ("jukebox.local",), IP), None, Reach(8765, True, (), ""),
                   Reach(8765, False, (), IP)]
        changes: list[str] = []
        w = Watch(8765, threading.Event(), on_change=lambda r: changes.append(r.address()),
                  hostname="ytdj", look=lambda port, host: results.pop(0))
        self.assertEqual(w.once().address(), "jukebox.local")
        self.assertEqual(w.once().address(), "jukebox.local")  # web chvíli neběží (restart)
        self.assertEqual(w.once().address(), "jukebox.local")  # ani síť: neukazovat prázdno
        self.assertEqual(w.once().address(), f"{IP}:8765")  # port 80 i jméno přestaly → co funguje
        self.assertEqual(changes, ["jukebox.local", f"{IP}:8765"])

    def test_checking_is_rare_and_never_on_a_ui_thread(self):
        threads: list[str] = []
        stop = threading.Event()

        def slow(port, host):
            threads.append(threading.current_thread().name)
            time.sleep(0.05)
            return Reach(port, True, ("jukebox.local",), IP)

        w = Watch(8765, stop, hostname="ytdj", look=slow, schedule=(0.0, 0.05), every=0.05)
        t0 = time.monotonic()
        w.start()  # nikdy nečeká volající vlákno
        self.assertLess(time.monotonic() - t0, 0.03)
        time.sleep(0.45)
        stop.set()
        w.join(2)
        self.assertFalse(w.is_alive())
        self.assertTrue(threads and set(threads) == {"web-address"})
        self.assertLess(len(threads), 8)
        # naostro: po startu párkrát, pak jednou za pár minut
        self.assertGreaterEqual(webaddr.EVERY, 120)
        self.assertLessEqual(len(webaddr.SCHEDULE), 5)
        # chyba při ověřování vlákno neshodí
        bad = Watch(8765, threading.Event(), hostname="x", look=lambda p, h: 1 / 0)
        self.assertEqual(bad.once(), bad.reach)

    def test_order_of_questions_is_port_80_first_then_names(self):
        a, r, o, asked = world(True, ("jukebox.local", "ytdj.local"))
        probe(8765, "ytdj", answers=a, resolves=r, own_ip=o)
        self.assertEqual(asked[0], (IP, 80))  # přes vlastní adresu, jako návštěvník
        self.assertIn(("jukebox.local", 80), asked)
        self.assertNotIn(("jukebox.local", 8765), asked)


class PanelUrls(unittest.TestCase):
    """Displej: adresa a QR kód jen s tím, co funguje; port jen když je potřeba."""

    def ctrl(self, reach) -> NetController:
        c = NetController(_Backend(), lambda m: None, pin_reader=lambda: "",
                          reach=(lambda: reach) if reach is not None else None)
        c.status = NetStatus("ytdj", True, (Link("wlan0", "wifi", "connected", IP, "Sit", 70),))
        return c

    def test_qr_and_address_drop_the_port_only_with_a_verified_port_80(self):
        on80 = self.ctrl(Reach(8765, True, ("jukebox.local",), IP))
        self.assertEqual(on80.urls(), (f"http://{IP}", "http://jukebox.local"))
        off = self.ctrl(Reach(8765, False, ("ytdj.local",), IP))
        self.assertEqual(off.urls(), (f"http://{IP}:8765", "http://ytdj.local:8765"))
        # neověřené jméno se nenabízí
        self.assertEqual(self.ctrl(Reach(8765, False, (), IP)).urls(), (f"http://{IP}:8765",))
        # QR kód na displeji nese přesně první (zobrazenou) adresu
        r = NetRenderer("cs")
        self.assertEqual(r._qr(on80.urls()[0]), qr_encode(f"http://{IP}", "M"))
        self.assertNotEqual(r._qr(on80.urls()[0]), r._qr(off.urls()[0]))
        # po připojení k Wi-Fi stejné pravidlo
        self.assertEqual(on80.view(time.monotonic()).urls[0], f"http://{IP}")

    def test_without_checking_everything_is_as_before(self):
        self.assertEqual(self.ctrl(None).urls(), (f"http://{IP}:8765", "http://ytdj.local:8765"))


class _Backend:
    def status(self):
        return NetStatus()

    def scan(self):
        return []

    def connect(self, ssid, password):
        raise AssertionError("nepřipojovat")


class OfficeName(unittest.TestCase):
    """Jméno z DNS kanceláře (web_name v config.toml): ověřené má přednost."""

    def test_configured_name_is_validated_verified_and_preferred(self):
        clean = webaddr.clean_name
        self.assertEqual(clean("Jukebox.Example.ORG."), "jukebox.example.org")
        self.assertEqual(clean("jukebox"), "jukebox")
        for bad in ("", None, 5, "http://jukebox.example.org", "jukebox.example.org/x",
                    "jukebox example", "juke_box.cz", "-jukebox.cz", "jukebox..cz", "a" * 64 + ".cz",
                    "x" * 254, "192.168.0.24", "jukebox.cz:8080", "jukebox.cz;rm -rf", "<b>x</b>"):
            self.assertEqual(clean(bad), "", bad)
        name = "jukebox.example.org"
        a, r, o, asked = world(True, (name, "jukebox.local", "ytdj.local"))
        reach = probe(8765, "ytdj", name=name, answers=a, resolves=r, own_ip=o)
        self.assertEqual(reach.names[0], name)
        self.assertEqual((reach.address(), reach.ip_address()), (name, IP))
        # nepřekládá se nebo pod ním neodpovídá jukebox → nepoužije se, zůstane to ostatní
        a, r, o, _ = world(True, ("jukebox.local",))
        reach = probe(8765, "ytdj", name=name, answers=a, resolves=r, own_ip=o)
        self.assertEqual(reach.names, ("jukebox.local",))
        # hlídač ho předává dál a nesmysl z konfigurace zahodí
        seen = []
        w = Watch(8765, threading.Event(), hostname="ytdj", name="Jukebox.Example.org",
                  look=lambda port, host, **kw: seen.append(kw) or Reach(port, True, (), IP))
        w.once()
        self.assertEqual(seen, [{"name": name}])
        self.assertEqual(Watch(8765, threading.Event(), hostname="x", name="a b").web_name, "")

    def test_name_comes_from_the_file_only(self):
        """Web nastavení `web_name` neukáže ani nezmění: v aplikaci ten klíč vůbec není."""
        config = (ROOT / "ytdj" / "config.py").read_text(encoding="utf-8")
        server = (ROOT / "ytdj" / "web" / "server.py").read_text(encoding="utf-8")
        self.assertNotIn("web_name", config)
        self.assertNotIn("web_name", server)
        for main in ("ytdj/tv/__main__.py", "ytdj/panel/__main__.py"):
            self.assertIn('cfg.get("web_name")', (ROOT / main).read_text(encoding="utf-8"), main)
        notes = (ROOT / "packaging" / "rpi" / "NOTES.md").read_text(encoding="utf-8")
        for word in ("DHCP reservation", "DNS record", 'web_name = "jukebox.example.org"'):
            self.assertIn(word, notes, word)


class TvAddress(unittest.TestCase):
    def test_tv_shows_name_and_address_and_the_qr_never_depends_on_a_name(self):
        state = {"current": None, "queue": [], "dj": {}}
        for size in ((720, 480), (1280, 720), (1920, 1080)):
            r = Renderer(size)
            r.render(view_from(state, address="jukebox.local", address_ip=IP, now=0.0), full=True)
            # QR kód vede na číselnou adresu, ne na jméno .local
            self.assertEqual(r._qr, (IP, qr_encode(f"http://{IP}")))
            both = r.frame.copy()
            web = next(x.box for x in r.regions if x.name == "web")
            # obě adresy jsou na obrazovce: jiné vykreslení než jen se jménem
            only = Renderer(size)
            only.render(view_from(state, address="jukebox.local", now=0.0), full=True)
            self.assertNotEqual(both.crop(web).tobytes(), only.frame.crop(web).tobytes())
            self.assertEqual(only._qr[0], "jukebox.local")  # bez sítě není co jiného nabídnout
            # změna adresy stroje překreslí jen ten roh
            boxes = r.render(view_from(state, address="jukebox.local", address_ip="10.0.0.9", now=0.0))
            self.assertTrue(boxes)
            for b in boxes:
                self.assertTrue(web[0] <= b[0] and web[1] <= b[1] and b[2] <= web[2] and b[3] <= web[3])
            self.assertEqual(r._qr[0], "10.0.0.9")
            # bez jména (jen adresa): jednou, ne dvakrát pod sebou
            r.render(view_from(state, address=f"{IP}:8765", address_ip=f"{IP}:8765", now=0.0))
            self.assertEqual(r._qr[0], f"{IP}:8765")
        # proces telky: jméno i adresa z téhož ověření, obojí bez portu jen s portem 80
        reach = [Reach(8765, True, ("jukebox.local",), IP)]

        class W:
            @property
            def reach(self):
                return reach[0]

        app = tvapp.TvApp(lambda: PngScreen(_TMP / "n.png", (720, 480)), "http://127.0.0.1:9",
                          watch=W())
        v = app.view()
        self.assertEqual((v.address, v.address_ip), ("jukebox.local", IP))
        reach[0] = Reach(8765, False, ("jukebox.local",), IP)
        v = app.view()
        self.assertEqual((v.address, v.address_ip), ("jukebox.local:8765", f"{IP}:8765"))
        # displej: QR taky na číselnou adresu (první v seznamu)
        c = PanelUrls().ctrl(Reach(8765, True, ("jukebox.local",), IP))
        self.assertEqual(c.urls()[0], f"http://{IP}")

    def test_tv_shows_the_verified_address_and_its_qr_follows_it(self):
        state = {"current": None, "queue": [], "dj": {}}
        r = Renderer((1280, 720))
        r.render(view_from(state, address="jukebox.local", now=0.0), full=True)
        self.assertEqual(r._qr, ("jukebox.local", qr_encode("http://jukebox.local")))
        a = r.frame.copy()
        boxes = r.render(view_from(state, address=f"{IP}:8765", now=0.0))
        web = next(x.box for x in r.regions if x.name == "web")
        self.assertTrue(boxes)  # jen roh s adresou
        for b in boxes:
            self.assertTrue(web[0] <= b[0] and web[1] <= b[1] and b[2] <= web[2] and b[3] <= web[3])
        self.assertEqual(r._qr, (f"{IP}:8765", qr_encode(f"http://{IP}:8765")))
        self.assertNotEqual(a.crop(web).tobytes(), r.frame.crop(web).tobytes())
        # žádná ověřená adresa → žádná adresa ani QR (lepší nic než nefunkční)
        r.render(view_from(state, address="", now=0.0))
        self.assertLess(max(p[0] for p in r.frame.crop(web).getdata()), 200)  # žádný QR kód
        self.assertEqual(view_from(state, now=0.0).address, "")

    def test_tv_process_uses_the_watch_unless_an_address_is_forced(self):
        reach = [Reach(8765, False, (), IP)]

        class FakeWatch:
            def __init__(self):
                self.started = False

            @property
            def reach(self):
                return reach[0]

            def is_alive(self):
                return self.started

            def start(self):
                self.started = True

        out = _TMP / "tv.png"
        watch = FakeWatch()
        app = tvapp.TvApp(lambda: PngScreen(out, (720, 480)), "http://127.0.0.1:9", watch=watch)
        self.assertEqual(app.view().address, f"{IP}:8765")
        reach[0] = Reach(8765, True, ("jukebox.local",), IP)
        self.assertEqual(app.view().address, "jukebox.local")
        t = threading.Thread(target=app.run, daemon=True)
        t.start()
        time.sleep(0.3)
        app.shutdown()
        t.join(3)
        self.assertTrue(watch.started)
        # výslovně zadaná adresa se neověřuje ani nemění
        fixed = tvapp.TvApp(lambda: PngScreen(out, (720, 480)), "http://127.0.0.1:9",
                            "jukebox.example:8080")
        self.assertIsNone(fixed.watch)
        self.assertEqual(fixed.view().address, "jukebox.example:8080")
        # bez zadání si proces hlídače založí sám, pro port webu z adresy API
        auto = tvapp.TvApp(lambda: PngScreen(out, (720, 480)), "http://127.0.0.1:8765")
        self.assertEqual((type(auto.watch).__name__, auto.watch.port), ("Watch", 8765))
        src = (ROOT / "ytdj" / "tv").glob("*.py")
        self.assertFalse([p.name for p in src if "jukebox.local" in p.read_text(encoding="utf-8")])


if __name__ == "__main__":
    unittest.main()
