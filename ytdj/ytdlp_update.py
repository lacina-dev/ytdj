"""Pomocník skriptu packaging/rpi/ytdlp-update.sh (noční aktualizace yt-dlp).

Skript je v bashi; tady je jen to, co se v bashi dělá špatně nebo co už
umí ytdj sám:

    python -m ytdj.ytdlp_update args            # volby yt-dlp z configu (NUL)
    python -m ytdj.ytdlp_update newer A B       # kód 0, když je verze A novější než B
    python -m ytdj.ytdlp_update pypi < json     # nejnovější vydání z JSON PyPI
    python -m ytdj.ytdlp_update status < json   # idle / outage / busy z /api/status
    python -m ytdj.ytdlp_update event KIND k=text k:=json …

`args` vypíše přesně ty volby, které od mpv dostává yt-dlp i resolver
(cookies, JS runtime, vzdálené komponenty, extractor-args) — stejná funkce
jako diagnostika (`ytdj.diagnose.yt_dlp_args`), nic se nehádá. Výstup
obsahuje cesty k cookies a případný PO token, proto ho skript nikdy
nevypisuje, jen předává yt-dlp.

`event` zapíše jeden řádek do provozního logu (ytdj.telemetry, i s rotací),
bez `session.start`/`session.end` — jeden běh skriptu = jeden řádek.
"""

from __future__ import annotations

import json
import re
import sys


def version_key(v: str) -> tuple[int, ...]:
    """2026.11.23 → (2026, 11, 23); 2026.11.23.1 je novější než 2026.11.23."""
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


def newer(a: str, b: str) -> bool:
    ka, kb = version_key(a), version_key(b)
    return bool(ka) and ka > kb


def pypi_latest(data: dict) -> str:
    """Nejnovější stabilní vydání. `info.version` je u PyPI právě to."""
    v = str((data.get("info") or {}).get("version") or "").strip()
    if not re.fullmatch(r"\d{4}\.\d{1,2}\.\d{1,2}(\.\d+)?", v):
        raise ValueError(f"nečekaná verze z PyPI: {v[:40]!r}")
    return v


def status_word(data: dict) -> str:
    """Smí se restartovat? idle = nic nehraje (nebo pauza), outage = hudba
    stojí kvůli výpadku (jukebox to sám hlásí), busy = hraje / rozjíždí se."""
    if data.get("outage"):
        return "outage"
    if data.get("starting"):
        return "busy"
    if not data.get("playing") or data.get("paused"):
        return "idle"
    return "busy"


def config_args() -> list[str]:
    from .config import Config, load_secrets
    from .diagnose import yt_dlp_args

    load_secrets()
    return yt_dlp_args(Config.load())[1:]


def parse_fields(items: list[str]) -> dict:
    """`k=text` (řetězec) a `k:=json` (číslo, true/false/null, seznam)."""
    out: dict = {}
    for item in items:
        key, _, val = item.partition("=")
        if key.endswith(":"):
            key = key[:-1]
            try:
                out[key] = json.loads(val)
            except ValueError:
                out[key] = val
        else:
            out[key] = val
    return out


def write_event(kind: str, fields: dict) -> None:
    from . import telemetry

    if not telemetry.ENABLED:
        return
    telemetry._w.put(telemetry._line(kind, fields))  # bez session.start/end
    telemetry.flush()


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "args":
        sys.stdout.write("\0".join(config_args()))
        return 0
    if cmd == "newer" and len(rest) == 2:
        return 0 if newer(rest[0], rest[1]) else 1
    if cmd == "pypi":
        print(pypi_latest(json.load(sys.stdin)))
        return 0
    if cmd == "status":
        try:
            print(status_word(json.load(sys.stdin)))
        except ValueError:
            print("unknown")
        return 0
    if cmd == "event" and rest:
        write_event(rest[0], parse_fields(rest[1:]))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
