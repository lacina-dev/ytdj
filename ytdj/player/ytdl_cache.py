"""Předem vyřešené streamy pro mpv — skladby z fronty začínají hned.

mpv si adresu streamu zjišťuje přes yt-dlp (ytdl_hook) až ve chvíli, kdy na
skladbu dojde. Na Pi 3 to trvá ~20 s (podpis přes node, PO token, síť), takže
i skladba, kterou už známe z fronty, začne hrát po dlouhém tichu — a po
"Další" taky.

Tenhle modul se proto mpv podstrčí místo yt-dlp:

  * jako program (`python -m ytdj.player.ytdl_cache …` přes shim) dostane
    přesně ty argumenty, se kterými by mpv volal yt-dlp. Když pro to video má
    čerstvý výsledek, vypíše ho hned; jinak zavolá skutečné yt-dlp, výsledek
    předá mpv a zapamatuje si, s jakými argumenty mpv volá (šablona),
  * ytdj přes `prefetch()` stejnými argumenty dopředu vyřeší pár dalších
    skladeb z fronty, zatímco hraje ta současná.

Výsledek se použije jen jednou a jen do MAX_AGE — adresy streamů YouTube po
čase vyprší a prošlá adresa by skladbu shodila jako nepřehratelnou. Když
cokoli z toho selže, prostě se zavolá skutečné yt-dlp jako dřív.

Modul schválně nic z ytdj neimportuje: mpv ho spouští u každé skladby a
start musí být co nejrychlejší.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ENV_REAL = "YTDJ_YTDL_REAL"    # cesta ke skutečnému yt-dlp
ENV_DIR = "YTDJ_YTDL_CACHE"    # adresář cache
MAX_AGE = 90 * 60              # s — adresy streamů platí ~6 h, rezerva velká
TEMPLATE = "template.json"
VIDEO_ID = re.compile(r"[?&]v=([\w-]{11})")


def cache_dir() -> Path:
    base = os.environ.get(ENV_DIR) or os.path.join(
        os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "ytdj", "ytdl"
    )
    return Path(base)


def _video_id(argv: list[str]) -> str | None:
    # mpv dává URL jako poslední argument (za "--")
    if argv and (m := VIDEO_ID.search(argv[-1])):
        return m.group(1)
    return None


def _entry(video_id: str) -> Path:
    return cache_dir() / f"{video_id}.json"


def _pending(video_id: str) -> Path:
    """Značka "tuhle skladbu právě řeší prefetch"."""
    return cache_dir() / f"{video_id}.pending"


def _wait_for_prefetch(video_id: str, limit: float = 90.0) -> bytes | None:
    """Když skladbu zrovna řeší prefetch, počká na něj.

    Druhé yt-dlp pro tutéž skladbu by se s prvním jen přetahovalo o CPU —
    na Pi 3 by pak obě trvala dvakrát déle.
    """
    marker = _pending(video_id)
    deadline = time.time() + limit
    while time.time() < deadline:
        try:
            if time.time() - marker.stat().st_mtime > limit:
                return None  # zbytek po spadlém prefetchi
        except OSError:
            return _take(video_id)  # prefetch skončil — s výsledkem, nebo bez
        time.sleep(0.25)
    return None


def _is_single_json(argv: list[str]) -> bool:
    """Jen dotaz typu "dej JSON jednoho videa" — nic jiného necachujeme."""
    return ("-J" in argv or "--dump-single-json" in argv) and _video_id(argv) is not None


def _take(video_id: str) -> bytes | None:
    """Vyzvedne a smaže připravený výsledek, pokud je čerstvý."""
    path = _entry(video_id)
    try:
        age = time.time() - path.stat().st_mtime
        data = path.read_bytes()
    except OSError:
        return None
    path.unlink(missing_ok=True)  # jednorázově — případný opakovaný pokus vyřeší znovu
    if age > MAX_AGE or not data.strip():
        return None
    try:  # pojistka: nikdy nevydat JSON jiného videa, než o které mpv žádá
        if json.loads(data).get("id") != video_id:
            return None
    except (ValueError, AttributeError):
        return None
    return data


def _save_template(argv: list[str]) -> None:
    d = cache_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (TEMPLATE + ".tmp")
    tmp.write_text(json.dumps(argv[:-1]))
    tmp.replace(d / TEMPLATE)


def _load_template() -> list[str] | None:
    try:
        argv = json.loads((cache_dir() / TEMPLATE).read_text())
    except (OSError, ValueError):
        return None
    return argv if isinstance(argv, list) and all(isinstance(a, str) for a in argv) else None


def has_template() -> bool:
    return _load_template() is not None


def prefetch(video_id: str, url: str, real: str, timeout: float = 120.0) -> bool:
    """Vyřeší skladbu dopředu. Vrací True, když je připravená.

    Volá se z ytdj ve vlákně; bez šablony (mpv ještě nic nehrál) nic nedělá.
    """
    template = _load_template()
    if template is None:
        return False
    path = _entry(video_id)
    try:
        if time.time() - path.stat().st_mtime < MAX_AGE / 2:
            return True  # už je a ještě dlouho vydrží
    except OSError:
        pass
    marker = _pending(video_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    try:
        try:
            proc = subprocess.run(
                [real, *template, url],
                capture_output=True,
                timeout=timeout,
                preexec_fn=lambda: os.nice(10),  # přehrávání má přednost
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if proc.returncode != 0 or not proc.stdout.strip():
            return False
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(proc.stdout)
        tmp.replace(path)
        return True
    finally:
        marker.unlink(missing_ok=True)


def main(argv: list[str]) -> int:
    real = os.environ.get(ENV_REAL) or "yt-dlp"
    if _is_single_json(argv):
        vid = _video_id(argv)
        try:
            _save_template(argv)
        except OSError:
            pass
        if vid and ((data := _take(vid)) is not None or (data := _wait_for_prefetch(vid)) is not None):
            sys.stdout.buffer.write(data)
            sys.stdout.flush()
            return 0
    # cokoli jiného (nebo nic v cache) — skutečné yt-dlp, beze změny. Jen
    # s nižší prioritou: yt-dlp a node vytíží jádro na desítky vteřin a na Pi 3
    # by jinak braly čas zvuku — praskalo by to.
    try:
        os.nice(5)
    except OSError:
        pass
    os.execv(real, [real, *argv]) if os.path.isabs(real) else os.execvp(real, [real, *argv])
    return 127  # nedosažitelné


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
