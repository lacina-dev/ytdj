"""Screenshots of the web UI in headless Chrome, against the fake backend.

    python3 tests/web_shots.py [OUT_DIR]      (default /tmp/ytdj-shots/web)

Starts tests/fake_ytdj.py (demo wishes) on a free port, drives Chrome over
the DevTools protocol (needs `websockets`, which system python3 has) and
writes PNGs: phone 390×844 and desktop 1280×800, light and dark, for the
first visit (nick step), the everyday view, a sent wish, the DJ-offline and
YouTube-outage notices and the empty/idle state. Only for looking at the
page — nothing here is a test.
"""

from __future__ import annotations

import asyncio
import base64
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

sys.path.insert(0, str(Path(__file__).resolve().parent))

import websockets  # noqa: E402

from fake_ytdj import make_server  # noqa: E402

CHROME = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
SIZES = {"phone": (390, 844, 3, True), "desktop": (1280, 800, 1, False), "small": (320, 640, 2, True)}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Tab:
    def __init__(self, ws) -> None:
        self.ws = ws
        self.n = 0

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

    async def js(self, expr: str):
        r = await self.call("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
        return r.get("result", {}).get("value")

    async def shot(self, path: Path, full: bool = True) -> None:
        params = {"format": "png"}
        if full:
            m = await self.call("Page.getLayoutMetrics")
            size = m.get("cssContentSize") or m["contentSize"]
            params["captureBeyondViewport"] = True
            params["clip"] = {"x": 0, "y": 0, "width": size["width"], "height": size["height"], "scale": 1}
        r = await self.call("Page.captureScreenshot", **params)
        path.write_bytes(base64.b64decode(r["data"]))
        print(path)


async def run(out: Path, base: str, fake, cdp_port: int) -> None:
    info = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json/version").read())
    target = json.loads(urllib.request.urlopen(
        urllib.request.Request(f"http://127.0.0.1:{cdp_port}/json/new?about:blank", method="PUT")).read())
    del info
    async with websockets.connect(target["webSocketDebuggerUrl"], max_size=64 * 2**20) as ws:
        tab = Tab(ws)
        await tab.call("Page.enable")
        await tab.call("Runtime.enable")

        async def setup(size: str, scheme: str) -> None:
            w, h, scale, mobile = SIZES[size]
            await tab.call("Emulation.setDeviceMetricsOverride", width=w, height=h,
                           deviceScaleFactor=scale if size == "phone" else 1, mobile=mobile)
            await tab.call("Emulation.setTouchEmulationEnabled", enabled=mobile)
            await tab.call("Emulation.setEmulatedMedia",
                           features=[{"name": "prefers-color-scheme", "value": scheme}])

        async def goto(nick: str | None) -> None:
            await tab.call("Page.navigate", url=base + "/")
            await asyncio.sleep(0.6)
            await tab.js("localStorage.clear()")
            if nick:
                await tab.js(f"localStorage.setItem('ytdj.who', {json.dumps(nick)});"
                             "localStorage.setItem('ytdj.client', 'web-shots0001');"
                             f"localStorage.setItem('ytdj.nick', {json.dumps(nick)})")
                await tab.js("fetch('/api/me', {method:'POST', headers:{'Content-Type':'application/json'},"
                             f"body: JSON.stringify({{client:'web-shots0001', nick:{json.dumps(nick)}}})}})")
            await tab.call("Page.reload", ignoreCache=True)
            await asyncio.sleep(1.8)

        def state(**kw) -> None:
            with fake.lock:
                for k, v in kw.items():
                    setattr(fake, k, v)

        for size in ("phone", "desktop", "small"):
            for scheme in (("light",) if size == "small" else ("light", "dark")):
                await setup(size, scheme)
                tag = f"{size}-{scheme}"
                # first visit: nothing in localStorage
                await goto(None)
                await tab.shot(out / f"{tag}-1-first.png", full=False)
                # everyday view
                await goto("Petr")
                await tab.shot(out / f"{tag}-2-main.png")
                await tab.shot(out / f"{tag}-2-main-fold.png", full=False)
                over = await tab.js("[document.documentElement.scrollWidth, innerWidth]")
                print(f"  {tag}: page width {over[0]} / viewport {over[1]}"
                      + ("  <-- HORIZONTAL SCROLL" if over[0] > over[1] else ""))
                # the radio after somebody's wish: "Rádio podle přání Robert · …"
                playing = fake.playing_req
                state(playing_req=None, radio_from={"from_who": "Robert", "from_key": "shots-robert"})
                await asyncio.sleep(1.5)
                await tab.shot(out / f"{tag}-2b-radio-from.png", full=False)
                state(playing_req=playing, radio_from={})
                await asyncio.sleep(1.0)
                # office voting: 👎 choice, a queue row, the Hlasování page, a banned track
                await tab.js("window.scrollTo(0,0); document.querySelector('#vDown').click()")
                await asyncio.sleep(0.6)
                await tab.shot(out / f"{tag}-10-vote-down.png", full=False)
                if scheme == "light":
                    # really vote: 👎 just the song → toast, counts, own vote highlighted
                    await tab.js("document.querySelector('#vsOpts .opt').click()")
                    await asyncio.sleep(1.2)
                    await tab.shot(out / f"{tag}-10b-voted.png", full=False)
                    await tab.js("document.querySelector('#vDown').click()")
                    await asyncio.sleep(0.6)
                    await tab.shot(out / f"{tag}-10c-own-vote-in-sheet.png", full=False)
                await tab.js("document.querySelector('#vsClose').click();"
                             "document.querySelector('#queue .rv').scrollIntoView({block:'center'});"
                             "document.querySelector('#queue .rv').click()")
                await asyncio.sleep(0.6)
                await tab.shot(out / f"{tag}-11-vote-row.png", full=False)
                if scheme == "light":
                    # 👍 celému interpretovi z řádku fronty → pak znovu otevřít: aktivní stav
                    await tab.js("document.querySelector('#vsOpts [data-act^=\"artist:0:1\"]').click()")
                    await asyncio.sleep(1.2)
                    await tab.js("document.querySelector('#queue .rv').click()")
                    await asyncio.sleep(0.8)
                    await tab.shot(out / f"{tag}-11b-artist-up.png", full=False)
                await tab.js("document.querySelector('#vsClose').click(); location.hash='#hlasovani'")
                await asyncio.sleep(1.0)
                await tab.shot(out / f"{tag}-12-votes-page.png")
                over = await tab.js("[document.documentElement.scrollWidth, innerWidth]")
                print(f"  {tag} Hlasování: page width {over[0]} / viewport {over[1]}"
                      + ("  <-- HORIZONTAL SCROLL" if over[0] > over[1] else ""))
                await tab.js("location.hash=''")
                with fake.lock:
                    cur = fake._current()
                    key = fake.song_key(cur["artist"], cur["title"])
                    saved_votes = dict(fake.votes)
                    fake.votes[("song", key)] = {
                        c: {"vote": -1, "who": n, "at": time.time() - 120, "artist": cur["artist"],
                            "title": cur["title"], "video_id": cur["id"]}
                        for c, n in (("web-jana0001", "Jana"), ("web-karel001", "Karel"))}
                await asyncio.sleep(1.6)
                await tab.js("window.scrollTo(0,0)")
                await tab.shot(out / f"{tag}-13-banned-now.png", full=False)
                with fake.lock:
                    fake.votes = saved_votes
                await asyncio.sleep(1.0)
                if scheme == "light" and size != "small":
                    # a wish on its way
                    await tab.js("document.querySelector('#prompt').value='Beatles, něco veselého';"
                                 "document.querySelector('#prompt').dispatchEvent(new Event('input'));"
                                 "document.querySelector('#btnSend').click()")
                    await asyncio.sleep(0.7)
                    await tab.shot(out / f"{tag}-3-sent.png", full=False)
                    await asyncio.sleep(fake.prompt_delay + 1.2)
                    await tab.shot(out / f"{tag}-4-queued.png", full=False)
                    # notices
                    await tab.js("window.scrollTo(0, 0)")
                    state(dj_offline=True)
                    await asyncio.sleep(1.5)
                    await tab.shot(out / f"{tag}-5-djoffline.png", full=False)
                    state(dj_offline=False, outage={"reason": "network", "since": time.time()})
                    await asyncio.sleep(1.5)
                    await tab.shot(out / f"{tag}-6-outage.png", full=False)
                    state(outage=None, idle=True)
                    saved = fake.requests
                    fake.requests = []
                    await asyncio.sleep(1.5)
                    await tab.shot(out / f"{tag}-7-idle.png", full=False)
                    fake.requests = saved
                    state(idle=False)
                    await asyncio.sleep(0.5)
                    # changing the nick: a name somebody else uses, then a rude one
                    await tab.js("window.scrollTo(0,0); document.querySelector('#meBtn').click()")
                    await asyncio.sleep(0.3)
                    await tab.js("var i=document.querySelector('#nickIn'); i.value='Jana';"
                                 "i.dispatchEvent(new Event('input'));"
                                 "document.querySelector('#nickOk').click()")
                    await asyncio.sleep(0.6)
                    await tab.shot(out / f"{tag}-8-nick-shared.png", full=False)
                    await tab.js("var i=document.querySelector('#nickIn'); i.value='kurva';"
                                 "i.dispatchEvent(new Event('input'));"
                                 "document.querySelector('#nickOk').click()")
                    await asyncio.sleep(0.6)
                    await tab.shot(out / f"{tag}-9-nick-rejected.png", full=False)


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/ytdj-shots/web")
    out.mkdir(parents=True, exist_ok=True)
    server, fake = make_server(0)
    fake.demo()
    # a real video id, so the cover path (i.ytimg.com) is exercised when online
    fake.playing_req["track"] = {"id": "dQw4w9WgXcQ", "title": "Never Gonna Give You Up",
                                 "artist": "Rick Astley", "album": None, "duration": 213}
    fake.prompt_delay = 2.0
    import threading
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    cdp = free_port()
    prof = tempfile.mkdtemp(prefix="ytdj-chrome-")
    chrome = subprocess.Popen(
        [CHROME, "--headless=new", f"--remote-debugging-port={cdp}", f"--user-data-dir={prof}",
         "--no-first-run", "--no-default-browser-check", "--hide-scrollbars", "--mute-audio",
         "--disable-gpu", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{cdp}/json/version", timeout=0.5)
                break
            except OSError:
                time.sleep(0.2)
        asyncio.run(run(out, base, fake, cdp))
    finally:
        chrome.terminate()
        try:
            chrome.wait(5)
        except subprocess.TimeoutExpired:
            chrome.kill()
        server.shutdown()
        shutil.rmtree(prof, ignore_errors=True)
        os.environ.pop("_", None)


if __name__ == "__main__":
    main()
