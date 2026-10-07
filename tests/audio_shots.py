"""Snímky nastavení s výběrem zvukového výstupu (POZADAVKY #75).

    python3 tests/audio_shots.py [OUT_DIR]      (výchozí /tmp/ytdj-shots/output)

Stránka běží proti falešnému backendu (tests/fake_ytdj.py), ale obsah pole
„Zvukový výstup" je skutečný: dělá ho ytdj/audio_outputs.py nad grafem
PipeWire z Pi (tests/fixtures/pw-dump-pi.json) — jednou s připojenou
vybranou kartou, jednou s odpojenou (hraje náhradní; hlasitost se nemění).
Jen na koukání, žádný test.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
_TMP = tempfile.mkdtemp(prefix="ytdj-audio-shots-")
os.environ.setdefault("XDG_DATA_HOME", _TMP)
os.environ.setdefault("XDG_CONFIG_HOME", _TMP)
os.environ.setdefault("YTDJ_EVENTS_FILE", str(Path(_TMP) / "events.jsonl"))

import websockets  # noqa: E402

from fake_ytdj import make_server  # noqa: E402
from web_shots import CHROME, SIZES, Tab, free_port  # noqa: E402
from ytdj import audio_outputs as ao  # noqa: E402
from ytdj.config import DEFAULTS, Config  # noqa: E402

PIN = "123456"
KEYS = ("loudness_normalize", "loudness_target", "audio_output", "audio_switch_volume", "queue_target")
USB = "alsa_output.usb-GeneralPlus_USB_Audio_Device-00.analog-stereo"


def field_meta() -> dict[str, tuple]:
    tree = ast.parse((ROOT / "ytdj/web/server.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "FIELD_META":
            return ast.literal_eval(node.value)
    raise RuntimeError("FIELD_META nenalezeno")


async def snapshots() -> dict[str, dict]:
    """Skutečné stavy pole: vše připojeno / vybraná karta odpojená."""
    from test_audio_outputs import FakePlayer, FakeWirePlumber

    out = {}
    ao.SETTLE, ao.RETURN_DEBOUNCE = 0.0, 0.3
    cfg = Config(**{**DEFAULTS, "audio_output": USB})
    m = ao.AudioOutputs(cfg, FakePlayer(volume=55), state_file=Path(_TMP) / "state.json")
    m.state.update(adopted=True, last=USB)
    m._t0 -= 60
    ao.save_values = lambda changes: None  # type: ignore[assignment]
    wp = FakeWirePlumber(m, pin=USB)
    wp.boot()
    await asyncio.sleep(0.1)
    out["connected"] = m.snapshot()
    wp.unplug(USB)
    await asyncio.sleep(0.1)
    out["missing"] = m.snapshot()
    return out


def payload(snap: dict, meta: dict) -> dict:
    values = {k: DEFAULTS[k] for k in KEYS}
    values["audio_output"] = snap["chosen"]
    fields = []
    for k in KEYS:
        label, help_text, _ = meta[k]
        d = DEFAULTS[k]
        f = {"key": k, "label": label, "help": help_text, "restart": False,
             "type": "bool" if isinstance(d, bool) else "int" if isinstance(d, int) else "str"}
        if k == "audio_output":
            f.update(type="choice", choices=snap["choices"], notes=snap["notes"],
                     live="/api/audio/outputs", disabled=not snap["available"])
        fields.append(f)
    return {"values": values, "fields": fields}


async def run(out: Path, base: str, fake, cdp: int, snaps: dict) -> None:
    target = json.loads(urllib.request.urlopen(
        urllib.request.Request(f"http://127.0.0.1:{cdp}/json/new?about:blank", method="PUT")).read())
    meta = field_meta()
    async with websockets.connect(target["webSocketDebuggerUrl"], max_size=64 * 2**20) as ws:
        tab = Tab(ws)
        await tab.call("Page.enable")
        await tab.call("Runtime.enable")
        for size in ("phone", "desktop"):
            for scheme in ("light", "dark"):
                w, h, scale, mobile = SIZES[size]
                await tab.call("Emulation.setDeviceMetricsOverride", width=w, height=h,
                               deviceScaleFactor=scale if size == "phone" else 1, mobile=mobile)
                await tab.call("Emulation.setEmulatedMedia",
                               features=[{"name": "prefers-color-scheme", "value": scheme}])
                for name, snap in snaps.items():
                    with fake.lock:
                        fake.config_payload = payload(snap, meta)
                        fake.audio_snapshot = snap
                    await tab.call("Page.navigate", url=base + "/")
                    await asyncio.sleep(0.6)
                    await tab.js("localStorage.setItem('ytdj.who','Petr');localStorage.setItem('ytdj.nick','Petr');"
                                 "localStorage.setItem('ytdj.client','web-shots0001');"
                                 f"localStorage.setItem('ytdj.adminPin', {json.dumps(PIN)});"
                                 f"sessionStorage.setItem('ytdj.adminPin', {json.dumps(PIN)})")
                    await tab.call("Page.reload", ignoreCache=True)
                    await asyncio.sleep(1.6)
                    await tab.js("document.querySelector('#openSettings').click()")
                    await asyncio.sleep(1.0)
                    await tab.js("var f=document.querySelector('.field[data-key=\"audio_output\"]');"
                                 "if(f) f.scrollIntoView({block:'center'})")
                    await asyncio.sleep(0.3)
                    await tab.shot(out / f"{size}-{scheme}-{name}.png", full=False)
                    over = await tab.js("[document.documentElement.scrollWidth, innerWidth,"
                                        " !!document.querySelector('.field[data-key=\"audio_output\"] select')]")
                    print(f"  {size}-{scheme}-{name}: šířka {over[0]}/{over[1]}, pole: {over[2]}")
        # živý seznam: karta se připojí, zatímco je nastavení otevřené
        with fake.lock:
            fake.audio_snapshot = snaps["connected"]
        await asyncio.sleep(3.6)
        text = await tab.js("document.querySelector('.field[data-key=\"audio_output\"] select').innerText")
        print("  po 3 s (karta zpět) seznam:", text.replace("\n", " | "))
        await tab.shot(out / "desktop-dark-live-returned.png", full=False)


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    out = Path(args[0] if args else "/tmp/ytdj-shots/output")
    out.mkdir(parents=True, exist_ok=True)
    snaps = asyncio.run(snapshots())
    server, fake = make_server(0)
    fake.demo()
    fake.admin_pin = PIN
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    cdp = free_port()
    prof = tempfile.mkdtemp(prefix="ytdj-chrome-")
    chrome = subprocess.Popen(
        [CHROME, "--headless=new", f"--remote-debugging-port={cdp}", f"--user-data-dir={prof}",
         "--no-first-run", "--no-default-browser-check", "--hide-scrollbars", "--mute-audio",
         "--disable-gpu", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{cdp}/json/version", timeout=0.5)
                break
            except OSError:
                time.sleep(0.2)
        asyncio.run(run(out, base, fake, cdp, snaps))
    finally:
        chrome.terminate()
        try:
            chrome.wait(5)
        except subprocess.TimeoutExpired:
            chrome.kill()
        server.shutdown()
        shutil.rmtree(prof, ignore_errors=True)


if __name__ == "__main__":
    main()
