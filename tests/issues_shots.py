"""Snímky stránky „Chyby a nápady" v headless Chrome — jen na dívání, není to test.

    python3 tests/issues_shots.py [OUT_DIR]      (výchozí /tmp/ytdj-shots/issues)

Stránku podává skutečný web ytdj (ytdj/web/server.py) nad malou aplikací
z tests/test_issues.py — žádný přehrávač, žádná hudba, data v dočasné složce.
Server potřebuje starlette (venv), Chrome se řídí přes `websockets`
(systémový python3) — proto se server pouští jako podproces venv Pythonu
(`YTDJ_VENV_PY`, jinak .venv v repozitáři).

Telefon 390×844 tmavě i světle a desktop 1280×800: seznam, formulář Přidat,
položka s „Jak to bylo vyřešeno", část pro správce s polem na PIN a odkaz
v hlavičce hlavní stránky.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
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


def serve(port_file: str) -> None:
    """Podproces (venv): web nad ukázkovými daty; port zapíše do souboru."""
    import test_issues as ti
    from ytdj import issues

    async def go() -> None:
        rig = ti.make(seed=json.loads(issues.SEED_FILE.read_text(encoding="utf-8")))
        book = rig.book
        book._rates = {k: issues._Rate(10_000, 600.0, time.monotonic) for k in book._rates}
        now = time.time()
        book.clock = lambda: now - 3 * 86400
        a = book.create(ti.PETR, "Petr", "chyba", "Na telefonu se po odeslání přání neschová klávesnice",
                        "Pošlu přání, klávesnice zůstane otevřená a zakrývá frontu.\n\nAndroid, Chrome.")["id"]
        book.clock = lambda: now - 2 * 86400
        book.comment(a, ti.JANA, "Jana", "Na iPhonu taky.")
        book.plus(a, ti.JANA, "Jana")
        book.plus(a, ti.KAREL, "Karel")
        book.clock = lambda: now - 86400
        book.admin_update(a, state="vyreseno",
                          resolution="Po odeslání se pole přání zavře a klávesnice zmizí.\nNasazeno v úterý večer.")
        book.clock = lambda: now - 7200
        b = book.create(ti.JANA, "Jana", "napad", "Ukázat u písničky, kdo si ji přál, i v Odehráno",
                        "Ve frontě jméno je, v Odehráno už ne.")["id"]
        book.plus(b, ti.PETR, "Petr")
        book.clock = lambda: now - 3600
        book.admin_update(b, state="resi_se")
        book.clock = time.time
        rig.srv.host, rig.srv.port = "127.0.0.1", 0
        await rig.srv.start()
        Path(port_file).write_text(str(rig.srv.port))
        await asyncio.Event().wait()

    asyncio.run(go())


def venv_python() -> str:
    for cand in (os.environ.get("YTDJ_VENV_PY"), ROOT / ".venv/bin/python",
                 *(p / ".venv/bin/python" for p in ROOT.parents)):
        if cand and Path(cand).is_file():
            return str(cand)
    raise SystemExit("Nenašel jsem venv s starlette — nastav YTDJ_VENV_PY.")


async def run(out: Path, base: str, cdp_port: int) -> None:
    import websockets

    from web_shots import SIZES, Tab

    target = json.loads(urllib.request.urlopen(
        urllib.request.Request(f"http://127.0.0.1:{cdp_port}/json/new?about:blank", method="PUT")).read())
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

        async def goto(path: str, wait: float = 0.9) -> None:
            await tab.call("Page.navigate", url=base + path)
            await asyncio.sleep(wait)

        async def width(tag: str) -> None:
            over = await tab.js("[document.documentElement.scrollWidth, innerWidth]")
            print(f"  {tag}: šířka stránky {over[0]} / okno {over[1]}"
                  + ("  <-- VODOROVNÝ POSUN" if over[0] > over[1] else ""))

        await goto("/hlaseni")
        await tab.js("localStorage.clear(); localStorage.setItem('ytdj.client', 'client-petr');"
                     "localStorage.setItem('ytdj.nick', 'Petr')")
        for size, scheme in (("phone", "dark"), ("phone", "light"), ("desktop", "light"), ("desktop", "dark"),
                             ("small", "dark")):
            await setup(size, scheme)
            tag = f"{size}-{scheme}"
            await goto("/hlaseni")
            await width(tag + " seznam")
            await tab.shot(out / f"{tag}-1-list.png")
            await tab.js("document.querySelector('#addBtn').click();"
                         "document.querySelector('#fTitle').value='Hlasitost skáče mezi písničkami';"
                         "document.querySelector('#fBody').value='Jedna písnička řve, další není slyšet.'")
            await asyncio.sleep(0.3)
            await width(tag + " formulář")
            await tab.shot(out / f"{tag}-2-add.png", full=False)
            await goto("/hlaseni#12")
            await width(tag + " položka")
            await tab.shot(out / f"{tag}-3-detail-resolved.png")
            await tab.js("document.querySelector('details.admin').open = true;"
                         "document.querySelector('details.admin .btn.primary').click()")
            await asyncio.sleep(0.6)
            await tab.js("document.querySelector('details.admin').scrollIntoView()")
            await width(tag + " správce")
            await tab.shot(out / f"{tag}-4-admin-pin.png", full=False)
            await goto("/hlaseni#1")
            await tab.shot(out / f"{tag}-5-detail-seed.png")
            await goto("/hlaseni")
            await tab.js("var s=document.querySelector('#stateSel'); s.value='closed';"
                         "s.dispatchEvent(new Event('change'))")
            await asyncio.sleep(0.5)
            await tab.shot(out / f"{tag}-6-filter-closed.png", full=False)
            # odkaz s počtem otevřených v hlavičce hlavní stránky
            await goto("/", 1.5)
            await width(tag + " hlavní stránka")
            await tab.shot(out / f"{tag}-7-index-header.png", full=False)


def main() -> None:
    if len(sys.argv) > 2 and sys.argv[1] == "--serve":
        serve(sys.argv[2])
        return
    from web_shots import CHROME, free_port

    out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/ytdj-shots/issues")
    out.mkdir(parents=True, exist_ok=True)
    work = tempfile.mkdtemp(prefix="ytdj-issues-shots-")
    port_file = Path(work) / "port"
    env = dict(os.environ, XDG_DATA_HOME=work, XDG_CONFIG_HOME=work,
               YTDJ_EVENTS_FILE=str(Path(work) / "events.jsonl"))
    server = subprocess.Popen([venv_python(), str(Path(__file__).resolve()), "--serve", str(port_file)],
                              env=env, cwd=str(ROOT))
    cdp = free_port()
    prof = tempfile.mkdtemp(prefix="ytdj-chrome-")
    chrome = subprocess.Popen(
        [CHROME, "--headless=new", f"--remote-debugging-port={cdp}", f"--user-data-dir={prof}",
         "--no-first-run", "--no-default-browser-check", "--hide-scrollbars", "--mute-audio",
         "--disable-gpu", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if port_file.is_file() and port_file.read_text().strip():
                break
            if server.poll() is not None:
                raise SystemExit("server pro snímky se nespustil")
            time.sleep(0.1)
        base = f"http://127.0.0.1:{port_file.read_text().strip()}"
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{cdp}/json/version", timeout=0.5)
                break
            except OSError:
                time.sleep(0.2)
        asyncio.run(run(out, base, cdp))
    finally:
        for proc in (chrome, server):
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(prof, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
