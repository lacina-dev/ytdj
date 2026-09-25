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

ENV_REAL = "YTDJ_YTDL_REAL"      # cesta ke skutečnému yt-dlp
ENV_SOCKET = "YTDJ_YTDL_SOCKET"  # socket resolveru
VIDEO_ID = re.compile(r"[?&]v=([\w-]{11})")
TIMEOUT = 150.0  # s — resolver může čekat, než doběhne rozdělaná skladba


def _is_single_json(argv: list[str]) -> bool:
    """Jen dotaz typu "dej JSON jednoho videa" — nic jiného se neposílá."""
    return (
        ("-J" in argv or "--dump-single-json" in argv)
        and bool(argv)
        and bool(VIDEO_ID.search(argv[-1]))
    )


def _ask_resolver(path: str, argv: list[str]) -> dict | None:
    """Odpověď resolveru, nebo None, když s ním nejde mluvit."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(TIMEOUT)
            s.connect(path)
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
        if resp is not None:
            if resp.get("ok") and resp.get("json"):
                sys.stdout.write(resp["json"])
                sys.stdout.flush()
                return 0
            error = str(resp.get("error") or "")
            if error and error != "vypršel čas":
                # video se přehrát nedá — to samé by řeklo i yt-dlp, jen o 20 s později
                print(f"ERROR: {error}", file=sys.stderr)
                return 1
    _exec_real(argv)
    return 127  # nedosažitelné


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
