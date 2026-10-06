"""Tentýž člověk na všech adresách jukeboxu ve skutečném prohlížeči — není to unit test.

    python3 tests/identity_shots.py [OUT_DIR]      (výchozí /tmp/ytdj-shots/identity)

Skutečný web ytdj (ytdj/web/server.py) nad malou aplikací z tests/test_issues.py —
žádný přehrávač, žádná hudba, data v dočasné složce. Stejně jako na jukeboxu
poslouchá web na jednom portu a další port na něj jen přesměrovává; adresy
127.0.0.1:A, localhost:A, 127.0.0.1:B a localhost:B jsou pro prohlížeč čtyři
různé adresy (jiné úložiště), dvě jména sdílejí cookie jen mezi svými porty.
K vlastním adresám jsou přidané i dvě, které neodpovídají (zavřený port,
jméno, které neexistuje).

Headless Chrome (řízený přes `websockets`, systémový python3) projde:
  1. nový prohlížeč → přezdívka na první adrese → ostatní adresy ho poznají bez ptaní;
  2. dva dřívější účty na dvou adresách → spojí se do staršího i s hlasy;
  3. na adresu, která neodpovídá, se prohlížeč nikdy nevydá.
Vypíše, co zjistil, a skončí kódem 1, když něco nesedí. Server běží jako
podproces venv Pythonu (`YTDJ_VENV_PY`, jinak .venv v repozitáři).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

OLD, NEW = "pre-old-00000001", "pre-new-00000002"  # dva dřívější účty (scénář 2)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(port_file: str) -> None:
    """Podproces (venv): web + přesměrování dvou portů; porty zapíše do souboru."""
    import test_issues as ti
    from ytdj import identity, votes
    from ytdj.music.catalog import Track

    async def go() -> None:
        rig = ti.make()
        book = votes.wire(rig.app)
        book.load([])
        wq = rig.app.wishes
        # scénář 2: starší účet s hlasy a mladší účet s jinými hlasy (jako před nasazením)
        wq.nicks.set(OLD, "Robert")
        wq.nicks.set(NEW, "Robert 2")
        now = time.time()
        book.clock = lambda: now - 20 * 86400
        book.cast(votes.SONG, OLD, 1, "Robert", track=Track("dama0000001", "Malá dáma", "Kabát"))
        book.clock = lambda: now - 3600
        book.cast(votes.SONG, NEW, 1, "Robert 2", track=Track("pohoda00001", "Pohoda", "Kabát"))
        book.cast(votes.SONG, NEW, -1, "Robert 2", track=Track("dama0000001", "Malá dáma", "Kabát"))
        book.clock = time.time

        rig.srv.host, rig.srv.port = "127.0.0.1", 0
        await rig.srv.start()
        inner = rig.srv.port
        a, b, dead = free_port(), free_port(), free_port()

        async def pipe(reader, writer):
            try:
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
            except (OSError, asyncio.CancelledError):
                pass
            finally:
                try:
                    writer.close()
                except OSError:
                    pass

        async def forward(reader, writer):
            try:
                r2, w2 = await asyncio.open_connection("127.0.0.1", inner)
            except OSError:
                writer.close()
                return
            await asyncio.gather(pipe(reader, w2), pipe(r2, writer))

        for port in (a, b):
            for host in ("127.0.0.1", "::1"):
                try:
                    await asyncio.start_server(forward, host, port)
                except OSError:
                    pass  # stroj bez IPv6
        own = [f"http://{h}:{p}" for p in (a, b) for h in ("127.0.0.1", "localhost")]
        idn = identity.Identity(rig.app)
        idn.origins = identity.OwnOrigins(inner, alias="", hostname="", own_ips=lambda: [],
                                          extra=[*own, f"http://127.0.0.1:{dead}", "http://nowhere.invalid:8765"])
        rig.app.identity = idn
        Path(port_file).write_text(json.dumps({"a": a, "b": b, "dead": dead}))
        await asyncio.Event().wait()

    asyncio.run(go())


def venv_python() -> str:
    for cand in (os.environ.get("YTDJ_VENV_PY"), ROOT / ".venv/bin/python",
                 *(p / ".venv/bin/python" for p in ROOT.parents)):
        if cand and Path(cand).is_file():
            return str(cand)
    raise SystemExit("Nenašel jsem venv s starlette — nastav YTDJ_VENV_PY.")


class Browser:
    """Jedna záložka Chrome přes DevTools; pamatuje si, kam všude hlavní rám šel."""

    def __init__(self, ws) -> None:
        self.ws = ws
        self.n = 0
        self.visited: list[str] = []
        self.requested: list[str] = []

    def _event(self, msg: dict) -> None:
        p = msg.get("params", {})
        if msg.get("method") == "Page.frameNavigated" and not p.get("frame", {}).get("parentId"):
            self.visited.append(p["frame"]["url"])
        if msg.get("method") == "Network.requestWillBeSent" and p.get("type") == "Document":
            self.requested.append(p["request"]["url"])

    async def call(self, method: str, **params):
        self.n += 1
        mid = self.n
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            self._event(msg)

    async def idle(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            try:
                self._event(json.loads(await asyncio.wait_for(self.ws.recv(), left)))
            except asyncio.TimeoutError:
                break

    async def js(self, expr: str):
        r = await self.call("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
        return r.get("result", {}).get("value")

    async def goto(self, url: str, settle: float = 4.5) -> None:
        await self.call("Page.navigate", url=url)
        await self.idle(settle)

    async def shot(self, path: Path) -> None:
        import base64
        r = await self.call("Page.captureScreenshot", format="png")
        path.write_bytes(base64.b64decode(r["data"]))
        print("   ", path)

    async def who(self) -> dict:
        return json.loads(await self.js(
            "JSON.stringify({origin: location.origin, path: location.pathname,"
            " client: localStorage.getItem('ytdj.client'), nick: localStorage.getItem('ytdj.nick'),"
            " ask: !document.querySelector('#nickModal').hasAttribute('hidden'),"
            " me: document.querySelector('#meName').textContent,"
            " toast: document.querySelector('#toast').classList.contains('show') ?"
            "   document.querySelector('#toast').textContent : ''})"))


class Check:
    def __init__(self) -> None:
        self.bad = 0

    def __call__(self, ok: bool, text: str) -> None:
        print(("  ok   " if ok else "  FAIL ") + text)
        self.bad += 0 if ok else 1


async def with_browser(chrome_bin: str, fn) -> None:
    import websockets

    cdp = free_port()
    prof = tempfile.mkdtemp(prefix="ytdj-chrome-")
    chrome = subprocess.Popen(
        [chrome_bin, "--headless=new", f"--remote-debugging-port={cdp}", f"--user-data-dir={prof}",
         "--no-first-run", "--no-default-browser-check", "--hide-scrollbars", "--mute-audio",
         "--disable-gpu", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{cdp}/json/version", timeout=0.5)
                break
            except OSError:
                time.sleep(0.2)
        target = json.loads(urllib.request.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{cdp}/json/new?about:blank", method="PUT")).read())
        async with websockets.connect(target["webSocketDebuggerUrl"], max_size=64 * 2**20) as ws:
            tab = Browser(ws)
            await tab.call("Page.enable")
            await tab.call("Runtime.enable")
            await tab.call("Network.enable")
            await tab.call("Emulation.setDeviceMetricsOverride", width=390, height=844,
                           deviceScaleFactor=2, mobile=True)
            await fn(tab)
    finally:
        chrome.terminate()
        try:
            chrome.wait(5)
        except subprocess.TimeoutExpired:
            chrome.kill()
        shutil.rmtree(prof, ignore_errors=True)


class Fox(Browser):
    """Firefox přes geckodriver (WebDriver po HTTP) — stejné otázky, bez deníku navigací."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.visited: list[str] = []
        self.requested: list[str] = []

    def _wd(self, method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body or {}).encode() if method == "POST" else None,
                                     headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=60).read())["value"]

    async def idle(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def js(self, expr: str):
        return await asyncio.to_thread(self._wd, "POST", "/execute/sync", {"script": "return eval(arguments[0]);", "args": [expr]})

    async def goto(self, url: str, settle: float = 4.5) -> None:
        await asyncio.to_thread(self._wd, "POST", "/url", {"url": url})
        await asyncio.sleep(settle)
        self.visited.append(await self.js("location.href"))

    async def shot(self, path: Path) -> None:
        import base64
        path = path.with_name("firefox-" + path.name)
        path.write_bytes(base64.b64decode(await asyncio.to_thread(self._wd, "GET", "/screenshot")))
        print("   ", path)


async def with_firefox(fn) -> None:
    port = free_port()
    driver = subprocess.Popen(["geckodriver", "--port", str(port)], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    root = f"http://127.0.0.1:{port}"
    sid = None
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(root + "/status", timeout=0.5)
                break
            except OSError:
                time.sleep(0.2)
        req = urllib.request.Request(root + "/session", method="POST", headers={"Content-Type": "application/json"},
                                     data=json.dumps({"capabilities": {"alwaysMatch": {
                                         "moz:firefoxOptions": {"args": ["-headless", "--width=390", "--height=844"]}}}}).encode())
        sid = json.loads(urllib.request.urlopen(req, timeout=120).read())["value"]["sessionId"]
        await fn(Fox(f"{root}/session/{sid}"))
    finally:
        if sid:
            try:
                urllib.request.urlopen(urllib.request.Request(f"{root}/session/{sid}", method="DELETE"), timeout=20)
            except OSError:
                pass
        driver.terminate()
        try:
            driver.wait(5)
        except subprocess.TimeoutExpired:
            driver.kill()


async def run(out: Path, ports: dict, chrome_bin: str) -> int:
    """`chrome_bin` "" = Firefox (geckodriver)."""
    a, b, dead = ports["a"], ports["b"], ports["dead"]
    ip_a, name_a = f"http://127.0.0.1:{a}", f"http://localhost:{a}"
    ip_b, name_b = f"http://127.0.0.1:{b}", f"http://localhost:{b}"
    check = Check()

    def never_unreachable(tab: Browser, what: str) -> None:
        gone = [u for u in tab.visited + tab.requested if f":{dead}" in u or "nowhere.invalid" in u]
        check(not gone, f"{what}: na adresu, která neodpovídá, prohlížeč nešel ({len(tab.visited)} navigací)")

    async def first(tab: Browser) -> None:
        print("1. nový prohlížeč: přezdívka na jedné adrese, ostatní ho poznají")
        await tab.goto(ip_a + "/")
        w = await tab.who()
        hops = [u for u in tab.visited if "/identity" in u]
        check(w["origin"] == ip_a and w["path"] == "/", f"po první návštěvě je zpátky na {ip_a}/")
        if not isinstance(tab, Fox):  # Firefox tu deník navigací nevede
            check(len(hops) >= 3, f"prošel ostatní adresy jednou cestou ({len(hops)} mezistránek)")
        check(w["ask"], "účet nikde není → teprve teď se ptá na přezdívku")
        await tab.shot(out / "1a-new-browser-asks-once.png")
        await tab.js("document.querySelector('#nickIn').value='Ota';"
                     "document.querySelector('#nickForm').requestSubmit()")
        await tab.idle(1.0)
        me = await tab.who()
        check(me["nick"] == "Ota" and not me["ask"], f"přezdívka Ota uložena ({me})")
        for origin, label in ((name_b, "jiné jméno i port"), (ip_b, "stejné jméno, jiný port"),
                              (name_a, "jiné jméno, stejný port")):
            before = len(tab.visited)
            await tab.goto(origin + "/")
            w = await tab.who()
            check(w["origin"] == origin and w["path"] == "/", f"{label}: skončil na {origin}/")
            check(not w["ask"] and w["me"] == "Ota", f"{label}: žádný dotaz na přezdívku, je to Ota")
            check(w["client"] == me["client"], f"{label}: stejné id klienta")
            print(f"       navigací při téhle návštěvě: {len(tab.visited) - before}, hláška: {w['toast']!r}")
            if origin == name_b:
                await tab.shot(out / "1b-other-address-knows-me.png")
        before = len(tab.visited)
        await tab.goto(name_b + "/")
        check(len(tab.visited) - before == 1, "další návštěva už nikam neodbíhá (žádná smyčka)")
        await tab.goto(name_b + "/hlaseni", settle=1.5)
        as_who = await tab.js("document.querySelector('#asWho').textContent")
        check("Ota" in (as_who or ""), f"Chyby a nápady na jiné adrese: {as_who!r}")
        never_unreachable(tab, "scénář 1")

    async def second(tab: Browser) -> None:
        print("2. dva dřívější účty na dvou adresách se spojí do staršího")
        for origin, cid, nick in ((name_a, OLD, "Robert"), (ip_b, NEW, "Robert 2")):
            await tab.goto(origin + "/identity", settle=0.8)  # stránka bez úkolu: jen nastavit úložiště
            await tab.js(f"localStorage.setItem('ytdj.client', '{cid}');"
                         f"localStorage.setItem('ytdj.nick', '{nick}'); localStorage.setItem('ytdj.who', '{nick}')")
        await tab.goto(ip_b + "/")
        w = await tab.who()
        check(w["origin"] == ip_b and w["path"] == "/", f"zpátky na {ip_b}/")
        check(w["client"] == OLD and w["me"] == "Robert", "platí starší účet (Robert)")
        check("druhý účet" in w["toast"] and "Robert 2" in w["toast"], f"řekl to jednou větou: {w['toast']!r}")
        await tab.shot(out / "2a-two-accounts-merged-note.png")
        votes = json.loads(await tab.js(
            f"fetch('/api/votes?client={OLD}').then(r => r.json()).then(d => JSON.stringify(d))"))
        mine = {it["title"]: it.get("mine") for group in votes.values() if isinstance(group, list)
                for it in group if isinstance(it, dict) and it.get("mine")}
        check(mine.get("Pohoda") == 1, f"hlas mladšího účtu (Pohoda 👍) má teď starší účet: {mine}")
        check(mine.get("Malá dáma") == 1, "kde hlasovaly oba, platí hlas staršího (Malá dáma 👍)")
        await tab.goto(name_a + "/")
        w = await tab.who()
        check(w["client"] == OLD and not w["ask"] and w["origin"] == name_a, "na adrese staršího účtu se nic nezměnilo")
        never_unreachable(tab, "scénář 2")

    for scenario in (first, second):  # každý scénář v čistém prohlížeči
        if chrome_bin:
            await with_browser(chrome_bin, scenario)
        else:
            await with_firefox(scenario)
    print("VÝSLEDEK:", "v pořádku" if not check.bad else f"{check.bad} věcí nesedí")
    return 1 if check.bad else 0


def main() -> None:
    if "--serve" in sys.argv:
        serve(sys.argv[sys.argv.index("--serve") + 1])
        return
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    chrome_bin = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser") or ""
    if "--firefox" in sys.argv:  # druhý prohlížeč s jiným oddělováním úložišť
        if not shutil.which("geckodriver"):
            raise SystemExit("geckodriver tu není.")
        chrome_bin = ""
        print("== Firefox ==")
    elif not chrome_bin:
        raise SystemExit("Chrome / Chromium tu není.")
    out = Path(args[0] if args else "/tmp/ytdj-shots/identity")
    out.mkdir(parents=True, exist_ok=True)
    work = tempfile.mkdtemp(prefix="ytdj-identity-shots-")
    port_file = Path(work) / "ports"
    env = dict(os.environ, XDG_DATA_HOME=work, XDG_CONFIG_HOME=work,
               YTDJ_EVENTS_FILE=str(Path(work) / "events.jsonl"))
    server = subprocess.Popen([venv_python(), str(Path(__file__).resolve()), "--serve", str(port_file)],
                              env=env, cwd=str(ROOT))
    code = 1
    try:
        for _ in range(150):
            if port_file.is_file() and port_file.read_text().strip():
                break
            if server.poll() is not None:
                raise SystemExit("server se nespustil")
            time.sleep(0.1)
        code = asyncio.run(run(out, json.loads(port_file.read_text()), chrome_bin))
    finally:
        server.terminate()
        try:
            server.wait(5)
        except subprocess.TimeoutExpired:
            server.kill()
        shutil.rmtree(work, ignore_errors=True)
    sys.exit(code)


if __name__ == "__main__":
    main()
