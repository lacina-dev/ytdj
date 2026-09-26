"""Co mpv spouští místo yt-dlp: dotaz na trvale běžící resolver.

mpv si adresu streamu zjišťuje přes yt-dlp (ytdl_hook) až ve chvíli, kdy na
skladbu dojde. Tenhle skript (přes shim, viz MpvPlayer._ytdl_shim) dostane
přesně ty argumenty, se kterými by mpv volal yt-dlp, a:

  * dotaz na JSON jednoho videa (`-J … <url>`) pošle resolveru
    (ytdl_resolver.py) — ten má skladby z fronty vyřešené dopředu a jinak
    je vyřeší ~2× rychleji než nové yt-dlp,
  * když resolver neběží nebo neodpoví, zavolá skutečné yt-dlp jako dřív,
  * cokoli jiného jde rovnou do skutečného yt-dlp.

Modul schválně nic z ytdj neimportuje: mpv ho spouští u každé skladby a
start musí být co nejrychlejší.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import time

ENV_REAL = "YTDJ_YTDL_REAL"      # cesta ke skutečnému yt-dlp
ENV_SOCKET = "YTDJ_YTDL_SOCKET"  # socket resolveru
VIDEO_ID = re.compile(r"[?&]v=([\w-]{11})")
TIMEOUT = 150.0  # s — resolver může čekat, než doběhne rozdělaná skladba
# Po startu služby vzniká socket resolveru až chvíli po mpv (start Pythonu,
# načtení cache z RAM). Dřív shim hned sáhl po skutečném yt-dlp (~19 s ticha)
# — teď chvíli počká: resolver má skladbu často hotovou z cache na disku.
SOCKET_WAIT = 6.0  # s
# odpovědi resolveru, po kterých se má zkusit skutečné yt-dlp (ne "nedá se hrát")
FALLBACK_ERRORS = ("vypršel čas", "resolver startuje")


def _is_single_json(argv: list[str]) -> bool:
    """Jen dotaz typu "dej JSON jednoho videa" — nic jiného se neposílá."""
    return (
        ("-J" in argv or "--dump-single-json" in argv)
        and bool(argv)
        and bool(VIDEO_ID.search(argv[-1]))
    )


def _connect(path: str) -> socket.socket | None:
    """Spojení s resolverem; chvíli počká, když socket ještě nevznikl."""
    deadline = time.monotonic() + SOCKET_WAIT
    while True:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(path)
            return s
        except (FileNotFoundError, ConnectionRefusedError):
            # resolver teprve startuje (restart služby, pád) — zkusit znovu
            s.close()
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.1)
        except OSError:
            s.close()
            return None


def _ask_resolver(path: str, argv: list[str]) -> dict | None:
    """Odpověď resolveru, nebo None, když s ním nejde mluvit."""
    try:
        s = _connect(path)
        if s is None:
            return None
        with s:
            s.settimeout(TIMEOUT)
            s.sendall(json.dumps({"op": "get", "argv": argv}).encode() + b"\n")
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(1 << 16)
                if not chunk:
                    return None
                buf += chunk
        return json.loads(buf)
    except (OSError, ValueError):
        return None


def _note_fallback(argv: list[str], reason: str) -> None:
    """Zapíše do provozního logu, že skladba šla mimo resolver (pomalu).

    ytdj se importovat nesmí (start musí být rychlý), takže se řádek připíše
    rovnou — jeden krátký zápis s O_APPEND je atomický i vedle zapisovatele
    v ytdj. Cestu a sid předává MpvPlayer v prostředí.
    """
    path = os.environ.get("YTDJ_EVENTS_FILE")
    if not path or os.environ.get("YTDJ_TELEMETRY", "1") == "0":
        return
    try:
        m = VIDEO_ID.search(argv[-1]) if argv else None
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        ms = int(time.time() * 1000) % 1000
        tz = time.strftime("%z")
        rec = {
            "ts": f"{ts}.{ms:03d}{tz[:3]}:{tz[3:]}",
            "kind": "resolver.fallback",
            "sid": os.environ.get("YTDJ_SID", ""),
            "video_id": m.group(1) if m else None,
            "reason": reason,
        }
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, (json.dumps(rec) + "\n").encode())
        finally:
            os.close(fd)
    except Exception:
        pass


def _exec_real(argv: list[str]) -> None:
    real = os.environ.get(ENV_REAL) or "yt-dlp"
    # s nižší prioritou: yt-dlp a node vytíží jádro na desítky vteřin a na Pi 3
    # by jinak braly čas zvuku
    try:
        os.nice(5)
    except OSError:
        pass
    if os.path.isabs(real):
        os.execv(real, [real, *argv])
    os.execvp(real, [real, *argv])


def main(argv: list[str]) -> int:
    path = os.environ.get(ENV_SOCKET)
    if path and _is_single_json(argv):
        resp = _ask_resolver(path, argv)
        if resp is None:
            _note_fallback(argv, "resolver neodpovídá")
        else:
            if resp.get("ok") and resp.get("json"):
                sys.stdout.write(resp["json"])
                sys.stdout.flush()
                return 0
            error = str(resp.get("error") or "")
            if error and error not in FALLBACK_ERRORS:
                # video se přehrát nedá — to samé by řeklo i yt-dlp, jen o 20 s později
                print(f"ERROR: {error}", file=sys.stderr)
                return 1
            _note_fallback(argv, error or "prázdná odpověď")
    _exec_real(argv)
    return 127  # nedosažitelné


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
