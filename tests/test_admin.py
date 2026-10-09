"""PIN správce na webu: nastavení a restart jen s ním, nebezpečné klíče vůbec.

Vlastník 27. 9. 2026: „A jak to vidíš s tou bezpečností" → „Oprav všechny
body, které můžeš". Hudbu (přání, Další, hlasitost, hlasy) ovládá dál každý.

    YTDJ_EVENTS_FILE=/tmp/ev.jsonl .venv/bin/python -m unittest tests.test_admin -v
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="ytdj-admin-test-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)
os.environ.setdefault("YTDJ_EVENTS_FILE", str(Path(_TMP) / "events.jsonl"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from starlette.requests import Request  # noqa: E402

from ytdj import adminpin  # noqa: E402
from ytdj import config as cfgmod  # noqa: E402
from ytdj.config import DEFAULTS, Config  # noqa: E402
from ytdj.web import server as web  # noqa: E402

INDEX = Path(__file__).resolve().parents[1] / "ytdj/web/static/index.html"
PANEL_AND_SCRIPTS = [Path(__file__).resolve().parents[1] / p for p in ("ytdj/panel", "packaging")]


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def make(tmp: Path):
    """WebServer nad minimální aplikací; PIN v dočasné složce, hodiny v ruce testu."""
    app = SimpleNamespace(cfg=Config(**DEFAULTS), restart_requested=asyncio.Event())
    srv = web.WebServer(app)
    clock = Clock()
    srv.admin = adminpin.AdminGuard(tmp / "admin-pin", clock=clock)
    routes = {(r.path, m): r.endpoint for r in srv._starlette.routes for m in (getattr(r, "methods", None) or ())}
    return srv, app, routes, clock


def call(routes, method: str, path: str, body: dict | None = None, pin: str | None = None,
         ip: str = "10.0.0.7"):
    headers = [(b"user-agent", b"Mozilla/5.0 (X11; Linux) Chrome/130")]
    if pin is not None:
        headers.append((b"x-ytdj-pin", pin.encode()))
    raw = json.dumps(body).encode() if body is not None else b""
    if body is not None:
        headers.append((b"content-type", b"application/json"))
    scope = {"type": "http", "method": method, "path": path, "query_string": b"", "headers": headers,
             "client": (ip, 40000), "path_params": {}}
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": raw, "more_body": False}

    return routes[(path, method)](Request(scope, receive))


def body(resp) -> dict:
    return json.loads(resp.body)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.srv, self.app, self.routes, self.clock = make(self.dir)
        self.pin = adminpin.ensure_pin(self.dir / "admin-pin")
        self.wrong = f"{(int(self.pin) + 1) % 10 ** 6:06d}"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_call(self, *args, **kw):
        return asyncio.run(call(self.routes, *args, **kw))


class PinFile(unittest.TestCase):
    """F-BEZP-09: PIN je 6 náhodných číslic v souboru 0600 vedle config.toml."""

    def test_created_0600_six_digits_and_stable(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sub" / "admin-pin"
            pin = adminpin.ensure_pin(path)
            self.assertRegex(pin, r"^\d{6}$")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.read_text().strip(), pin)
            self.assertEqual(adminpin.ensure_pin(path), pin)  # podruhé stejný
            self.assertEqual(adminpin.read_pin(path), pin)
            self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["admin-pin"])  # žádný .tmp
            path.write_text("abc\n")  # rozbitý → nový
            again = adminpin.ensure_pin(path)
            self.assertRegex(again, r"^\d{6}$")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(adminpin.read_pin(Path(d) / "nic"), "")

    def test_lives_next_to_config_toml(self):
        self.assertEqual(adminpin.PIN_FILE, cfgmod.CONFIG_FILE.parent / "admin-pin")
        self.assertEqual(adminpin.PIN_FILE.parent, cfgmod.CONFIG_DIR)
        self.assertTrue(str(cfgmod.CONFIG_DIR).startswith(os.environ["XDG_CONFIG_HOME"]))

    def test_random_pins_differ(self):
        with tempfile.TemporaryDirectory() as d:
            pins = {adminpin.ensure_pin(Path(d) / f"p{i}") for i in range(20)}
        self.assertGreater(len(pins), 15)

    def test_start_writes_pin_off_the_loop_and_logs_only_the_path(self):
        async def go():
            with tempfile.TemporaryDirectory() as d:
                app = SimpleNamespace(cfg=Config(**DEFAULTS))
                srv = web.WebServer(app, "127.0.0.1", 0)
                srv.admin = adminpin.AdminGuard(Path(d) / "admin-pin")
                real = asyncio.to_thread
                offloaded = []

                async def spy(fn, *a, **kw):
                    offloaded.append(getattr(fn, "__name__", ""))
                    return await real(fn, *a, **kw)

                with mock.patch.object(web.asyncio, "to_thread", spy), \
                        self.assertLogs("ytdj.web.server", logging.INFO) as logs:
                    await srv.start()
                    await srv.stop()
                pin = adminpin.read_pin(Path(d) / "admin-pin")
                self.assertRegex(pin, r"^\d{6}$")
                self.assertIn("ensure_pin", offloaded)
                text = "\n".join(logs.output)
                self.assertIn(str(Path(d) / "admin-pin"), text)
                self.assertNotIn(pin, text)

        asyncio.run(go())

    def test_start_does_not_touch_the_websockets_library(self):
        """Web nemá žádný WebSocket: start nesmí záviset na tom, jaká verze
        knihovny `websockets` je zrovna v systému (venv vidí systémové balíky,
        se starou uvicorn při startu spadl na ImportError)."""
        async def go():
            with tempfile.TemporaryDirectory() as d:
                srv = web.WebServer(SimpleNamespace(cfg=Config(**DEFAULTS)), "127.0.0.1", 0)
                srv.admin = adminpin.AdminGuard(Path(d) / "admin-pin")
                # None v sys.modules = import toho modulu skončí ImportError
                broken = {name: None for name in list(sys.modules)
                          if name == "websockets" or name.startswith(("websockets.", "wsproto"))
                          or name.startswith("uvicorn.protocols.websockets.")}
                broken.update({"websockets": None, "wsproto": None,
                               "uvicorn.protocols.websockets.auto": None})
                with mock.patch.dict(sys.modules, broken):
                    await srv.start()
                    try:
                        self.assertIsNone(srv._server.config.ws_protocol_class)
                        reader, writer = await asyncio.open_connection("127.0.0.1", srv.port)
                        writer.write(b"GET /api/nic-takoveho HTTP/1.1\r\nHost: x\r\n"
                                     b"Connection: close\r\n\r\n")
                        await writer.drain()
                        head = await asyncio.wait_for(reader.readline(), 5)
                        writer.close()
                        self.assertTrue(head.startswith(b"HTTP/1.1 "), head)  # web odpovídá
                    finally:
                        await srv.stop()

        asyncio.run(go())


class ConfigNeedsPin(Base):
    """F-BEZP-09: nastavení a restart z webu jen se správným PINem v hlavičce."""

    def test_no_pin_wrong_pin_right_pin(self):
        r = self.run_call("GET", "/api/config")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(body(r)["pin"], "required")
        self.assertIn("Síť", body(r)["error"])
        self.assertNotIn("values", body(r))
        r = self.run_call("GET", "/api/config", pin=self.wrong)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(body(r)["pin"], "wrong")
        r = self.run_call("GET", "/api/config", pin=self.pin)
        self.assertEqual(r.status_code, 200)
        self.assertIn("queue_target", body(r)["values"])

    def test_post_needs_pin_and_then_saves(self):
        with mock.patch.object(web.cfgmod, "save_values") as save:
            r = self.run_call("POST", "/api/config", {"queue_target": 7})
            self.assertEqual(r.status_code, 401)
            r = self.run_call("POST", "/api/config", {"queue_target": 7}, pin=self.wrong)
            self.assertEqual(r.status_code, 403)
            save.assert_not_called()
            self.assertEqual(self.app.cfg.queue_target, DEFAULTS["queue_target"])
            r = self.run_call("POST", "/api/config", {"queue_target": 7}, pin=" " + self.pin + " ")
            self.assertEqual(r.status_code, 200, r.body)
            save.assert_called_once_with({"queue_target": 7})
        self.assertEqual(self.app.cfg.queue_target, 7)

    def test_restart_needs_pin(self):
        r = self.run_call("POST", "/api/restart")
        self.assertEqual(r.status_code, 401)
        r = self.run_call("POST", "/api/restart", pin=self.wrong)
        self.assertEqual(r.status_code, 403)
        self.assertFalse(self.app.restart_requested.is_set())
        r = self.run_call("POST", "/api/restart", pin=self.pin)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.app.restart_requested.is_set())

    def test_localhost_is_not_exempt(self):
        """Na Pi běží i model (Codex): ani 127.0.0.1 nemění nastavení bez PINu."""
        for ip in ("127.0.0.1", "::1"):
            self.assertEqual(self.run_call("GET", "/api/config", ip=ip).status_code, 401)
            self.assertEqual(self.run_call("POST", "/api/restart", ip=ip).status_code, 401)
        # a nic místního tyhle adresy nevolá (jinak by výjimku potřebovalo)
        for root in PANEL_AND_SCRIPTS:
            for f in root.rglob("*"):
                if f.is_file() and f.suffix in (".py", ".sh", ".service", ""):
                    text = f.read_text(errors="ignore")
                    self.assertNotIn("/api/config", text, f)
                    self.assertNotIn("/api/restart", text, f)

    def test_music_stays_open_without_pin(self):
        guarded = {key for key, ep in self.routes.items() if getattr(ep, "admin_only", False)}
        self.assertEqual(guarded, {("/api/config", "GET"), ("/api/config", "HEAD"), ("/api/config", "POST"),
                                   ("/api/restart", "POST"),
                                   # živý seznam zvukových výstupů v nastavení (F-ZVUK-28)
                                   ("/api/audio/outputs", "GET"), ("/api/audio/outputs", "HEAD"),
                                   # Chyby a nápady: stav, řešení a mazání (F-HLASENI-05); psát smí každý
                                   ("/api/issues/{iid}/resolve", "POST"), ("/api/issues/{iid}", "DELETE"),
                                   ("/api/issues/{iid}/comments/{cid}", "DELETE")})
        for key in (("/api/prompt", "POST"), ("/api/control", "POST"), ("/api/me", "POST"),
                    ("/api/votes", "POST"), ("/api/status", "GET"), ("/api/requests/{rid}", "DELETE")):
            self.assertIn(key, self.routes)
            self.assertFalse(getattr(self.routes[key], "admin_only", True), key)

    def test_pin_never_in_logs_or_telemetry(self):
        events = []
        with mock.patch.object(web.telemetry, "event", lambda name, **kw: events.append((name, kw))), \
                self.assertLogs("ytdj.web.server", logging.WARNING) as logs:
            for _ in range(adminpin.MAX_FAILURES):
                self.run_call("GET", "/api/config", pin=self.wrong)
        dump = json.dumps(events) + "\n".join(logs.output)
        self.assertNotIn(self.wrong, dump)
        self.assertNotIn(self.pin, dump)
        self.assertEqual([n for n, _ in events].count("web.admin_denied"), adminpin.MAX_FAILURES - 1)
        self.assertIn("web.admin_locked", [n for n, _ in events])


class Brake(Base):
    """F-BEZP-10: po 5 špatných PINech (od kohokoli) je správa 5 minut zavřená."""

    def test_five_wrong_lock_for_five_minutes(self):
        for i in range(adminpin.MAX_FAILURES - 1):
            ip = f"10.0.0.{i + 2}"  # různé adresy — počítá se za celý jukebox
            self.assertEqual(self.run_call("GET", "/api/config", pin=self.wrong, ip=ip).status_code, 403)
        r = self.run_call("GET", "/api/config", pin=self.wrong)
        self.assertEqual(r.status_code, 429)
        self.assertEqual(body(r)["pin"], "locked")
        self.assertIn("5 min", body(r)["error"])
        self.assertEqual(r.headers["retry-after"], "300")
        # zamčeno i pro správný PIN, i pro restart
        self.assertEqual(self.run_call("GET", "/api/config", pin=self.pin).status_code, 429)
        self.assertEqual(self.run_call("POST", "/api/restart", pin=self.pin).status_code, 429)
        self.assertFalse(self.app.restart_requested.is_set())
        self.clock.t += 181
        r = self.run_call("GET", "/api/config", pin=self.pin)
        self.assertEqual(r.status_code, 429)
        self.assertIn("2 min", body(r)["error"])
        self.clock.t += 120
        self.assertEqual(self.run_call("GET", "/api/config", pin=self.pin).status_code, 200)
        # po zámku se počítá znovu od nuly
        self.assertEqual(self.run_call("GET", "/api/config", pin=self.wrong).status_code, 403)
        self.assertEqual(self.srv.admin.failures, 1)

    def test_missing_pin_is_not_a_guess_and_success_resets(self):
        for _ in range(10):
            self.assertEqual(self.run_call("GET", "/api/config").status_code, 401)
        for _ in range(adminpin.MAX_FAILURES - 1):
            self.run_call("GET", "/api/config", pin=self.wrong)
        self.assertEqual(self.run_call("GET", "/api/config", pin=self.pin).status_code, 200)
        self.assertEqual(self.srv.admin.failures, 0)
        self.assertEqual(self.run_call("GET", "/api/config", pin=self.wrong).status_code, 403)


class LockedKeys(Base):
    """F-BEZP-08: klíče, které spouštějí kód, míří na soubory nebo mění síť, jen v config.toml."""

    def test_classification_covers_every_key(self):
        live_or_form = set(DEFAULTS) - web.HIDDEN_KEYS - web.WEB_LOCKED_KEYS
        self.assertTrue({"mpv_extra_args", "web_host", "web_port", "web_enabled", "cookies_file",
                         "cookies_browser", "remote_components", "js_runtimes"} <= web.WEB_LOCKED_KEYS)
        # co na webu zůstalo, jsou čísla, přepínače a jména — žádné cesty ani programy
        for key in live_or_form:
            self.assertIn(type(DEFAULTS[key]), (int, bool, str, list), key)
            self.assertNotIn(key, ("mpv_extra_args", "cookies_file"))

    def test_get_hides_them(self):
        data = body(self.run_call("GET", "/api/config", pin=self.pin))
        keys = {f["key"] for f in data["fields"]}
        for key in web.WEB_LOCKED_KEYS:
            self.assertNotIn(key, data["values"])
            self.assertNotIn(key, keys)
        self.assertIn("codex_model", keys)
        self.assertIn("wish_block", keys)

    def test_post_rejects_them_even_with_pin(self):
        bad = {"mpv_extra_args": "--script=/tmp/x.lua", "web_host": "10.9.9.9", "web_port": 1,
               "web_enabled": False, "cookies_file": "/etc/shadow", "cookies_browser": "chrome:x",
               "remote_components": "ejs:npm", "js_runtimes": "node:/tmp/evil"}
        with mock.patch.object(web.cfgmod, "save_values") as save:
            for key, value in bad.items():
                r = self.run_call("POST", "/api/config", {key: value}, pin=self.pin)
                self.assertEqual(r.status_code, 400, key)
                self.assertIn("mění se jen v config.toml na Pi", body(r)["error"])
                # ani schované mezi povolenými
                r = self.run_call("POST", "/api/config", {"queue_target": 6, key: value}, pin=self.pin)
                self.assertEqual(r.status_code, 400, key)
            save.assert_not_called()
        self.assertEqual(self.app.cfg.web_host, DEFAULTS["web_host"])
        self.assertEqual(self.app.cfg.mpv_extra_args, [])

    def test_names_passed_on_as_arguments_are_plain(self):
        with mock.patch.object(web.cfgmod, "save_values") as save:
            for key, value in (("codex_model", "--dangerously-bypass"), ("codex_model", "gpt 5"),
                               ("player_client", "web;po_token=x"), ("player_client", "web music")):
                r = self.run_call("POST", "/api/config", {key: value}, pin=self.pin)
                self.assertEqual(r.status_code, 400, (key, value))
            save.assert_not_called()
            for key, value in (("codex_model", "gpt-5.4-mini"), ("codex_model", ""),
                               ("player_client", "web_music,default"), ("player_client", "")):
                r = self.run_call("POST", "/api/config", {key: value}, pin=self.pin)
                self.assertEqual(r.status_code, 200, (key, value, r.body))


class WebPage(unittest.TestCase):
    """F-BEZP-09: stránka se na PIN zeptá jednou, pamatuje si ho a posílá v hlavičce."""

    def test_page_asks_for_the_pin_and_sends_it(self):
        html = INDEX.read_text()
        self.assertIn('id="pinBox"', html)
        self.assertIn("PIN je na displeji jukeboxu: Síť.", html)
        self.assertIn('"X-YTDJ-PIN"', html)
        self.assertIn('var PIN_KEY = "ytdj.adminPin"', html)
        self.assertIn('adminFetch("/api/config")', html)
        self.assertIn('adminFetch("/api/restart"', html)
        self.assertIn("Moc špatných PINů", html)
        self.assertIn("Špatný PIN.", html)
        # jen správa jde přes adminFetch; přání a ovládání dál bez PINu
        self.assertNotIn('adminFetch("/api/prompt"', html)
        self.assertNotIn('adminFetch("/api/control"', html)
        self.assertEqual(adminpin.HEADER, "x-ytdj-pin")


if __name__ == "__main__":
    unittest.main()
