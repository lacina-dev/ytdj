"""Provozní události — strukturovaný log pro ladění podle skutečného provozu.

Každá podstatná věc (přání posluchače, rozhodnutí DJ, start skladby, výpadek
zvuku, dotyk na panelu…) se zapíše jako jeden JSON řádek do `events.jsonl`:

    {"ts": "2026-09-25T20:31:02.114+02:00", "kind": "track.start", "video_id": "…", …}

Použití kdekoli v ytdj:

    from ytdj import telemetry
    telemetry.event("dj.decision", action="start_radio", seeds=4, took_ms=18200)
    with telemetry.timer("catalog.search", query=q) as t:
        ...
        t["results"] = len(found)   # pole doplněná uvnitř bloku se zapíšou taky

Zásady (platí pro všechny, kdo sem píšou):
  * `event()` nikdy nevyhodí výjimku a nikdy neblokuje déle než zápis řádku —
    log nesmí shodit ani zpomalit přehrávání,
  * `kind` je "oblast.co" malými písmeny (track.start, dj.request, panel.touch…),
  * pole jsou JSON-serializovatelné skaláry nebo krátké seznamy; žádná hesla,
    tokeny, cookies ani celé odpovědi modelu (jen jejich podstatu/zkrácení),
  * časy trvání jako `*_ms` (int), velikosti jako `*_mb`.

Soubor: $YTDJ_EVENTS_FILE, jinak ~/.local/share/ytdj/events.jsonl.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

_lock = threading.Lock()
_path: Path | None = None


def events_file() -> Path:
    env = os.environ.get("YTDJ_EVENTS_FILE")
    if env:
        return Path(env)
    data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data / "ytdj" / "events.jsonl"


def _target() -> Path:
    global _path
    if _path is None:
        _path = events_file()
        _path.parent.mkdir(parents=True, exist_ok=True)
    return _path


def event(kind: str, **fields: Any) -> None:
    """Zapíše jednu událost. Nikdy nevyhodí výjimku."""
    try:
        rec = {"ts": datetime.now().astimezone().isoformat(timespec="milliseconds"), "kind": kind}
        rec.update(fields)
        line = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
        with _lock:
            with open(_target(), "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass


@contextlib.contextmanager
def timer(kind: str, **fields: Any) -> Iterator[dict[str, Any]]:
    """Změří blok a zapíše ho s `took_ms`; výjimka se zapíše jako `error`."""
    extra: dict[str, Any] = {}
    t0 = time.monotonic()
    try:
        yield extra
    except BaseException as exc:
        extra.setdefault("error", f"{type(exc).__name__}: {exc}"[:300])
        raise
    finally:
        event(kind, **fields, **extra, took_ms=int((time.monotonic() - t0) * 1000))
