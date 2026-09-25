"""Provozní události — strukturovaný log pro ladění podle skutečného provozu.

Každá podstatná věc (přání posluchače, rozhodnutí DJ, start skladby, výpadek
zvuku, dotyk na panelu…) se zapíše jako jeden JSON řádek do `events.jsonl`:

    {"ts": "2026-09-25T20:31:02.114+02:00", "kind": "track.start", "sid": "a1b2c3", "video_id": "…", …}

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

Provoz (Pi 3 se SD kartou):
  * `event()` jen naformátuje řádek a přidá ho do fronty v paměti; na disk ho
    zapíše vlastní vlákno jedním `write()` (do page cache, bez fsync). Čekání
    na pomalou SD kartu tak nikdy nedopadne na smyčku asyncio ani na mpv.
    Pád Pythonu (výjimka, SIGTERM) nic neztratí — frontu dopíše atexit;
    SIGKILL přijde nanejvýš o pár milisekund, výpadek proudu o to, co jádro
    ještě nestihlo zapsat (řádově desítky vteřin).
  * Rotace podle velikosti: events.jsonl → events.jsonl.1 → … → .KEEP;
    výchozí 5 MB × (1 + 4) souborů = nejvýš ~25 MB (YTDJ_EVENTS_MAX_MB,
    YTDJ_EVENTS_KEEP).
  * Každý proces má krátké `sid`; první událost procesu předchází
    `session.start` (verze, pid, uptime stroje), konec `session.end`.
  * YTDJ_TELEMETRY=0 vypne zápis úplně.

Souhrn: `python -m ytdj.telemetry report [--since 2h|today|YYYY-MM-DD] [--json]`.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import secrets
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

MB = 1024 * 1024
MAX_PENDING = 5000  # řádků ve frontě; když disk stojí, další se zahodí (a spočítají)


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _env_num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


ENABLED = _env_flag("YTDJ_TELEMETRY")
MAX_BYTES = int(_env_num("YTDJ_EVENTS_MAX_MB", 5) * MB)
KEEP = int(_env_num("YTDJ_EVENTS_KEEP", 4))  # kolik starých souborů držet vedle aktuálního

# Identita procesu — odliší restarty v jednom souboru.
SESSION_ID = os.environ.get("YTDJ_SID") or secrets.token_hex(3)
_T0 = time.monotonic()


def events_file() -> Path:
    env = os.environ.get("YTDJ_EVENTS_FILE")
    if env:
        return Path(env)
    data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data / "ytdj" / "events.jsonl"


def rotated_files(path: Path | None = None) -> list[Path]:
    """Všechny soubory logu od nejstaršího po aktuální (ty, které existují)."""
    path = path or events_file()
    olds = sorted(
        (p for p in path.parent.glob(path.name + ".*") if p.suffix[1:].isdigit()),
        key=lambda p: -int(p.suffix[1:]),
    )
    return [p for p in [*olds, path] if p.exists()]


def _version() -> str:
    try:
        from . import __version__

        return __version__
    except Exception:
        return "?"


class _Writer:
    """Fronta řádků + vlákno, které je zapisuje a rotuje soubor."""

    def __init__(self) -> None:
        self.lock = threading.Lock()  # fronta — drží se jen na mikrosekundy
        self.wlock = threading.Lock()  # disk — drží ho jen zapisující vlákno / flush
        self.pending: list[str] = []
        self.wake = threading.Event()
        self.thread: threading.Thread | None = None
        self.fd: int | None = None
        self.path: Path | None = None
        self.size = 0
        self.dropped = 0
        self.written = 0
        self.started = False  # session.start už ve frontě
        self.pid = os.getpid()

    # -- producenti --

    def put(self, line: str) -> None:
        with self.lock:
            if os.getpid() != self.pid:  # po forku začít načisto
                self.__init__()
            if len(self.pending) >= MAX_PENDING:
                self.dropped += 1
                return
            self.pending.append(line)
            if self.thread is None:
                self._start_thread()
        self.wake.set()

    def _start_thread(self) -> None:
        try:
            t = threading.Thread(target=self._run, name="ytdj-telemetry", daemon=True)
            t.start()
            self.thread = t
        except Exception:  # bez vlákna (interpret končí…) se zapíše při flush
            self.thread = None

    # -- zápis --

    def _run(self) -> None:
        while True:
            self.wake.wait()
            self.wake.clear()
            try:
                self.flush()
            except Exception:
                pass

    def flush(self) -> None:
        with self.wlock:
            with self.lock:
                batch, self.pending = self.pending, []
            if not batch:
                return
            data = "".join(batch).encode("utf-8", "replace")
            self._open()
            if self.fd is None:
                return
            view = memoryview(data)
            while view:
                n = os.write(self.fd, view)
                view = view[n:]
            self.size += len(data)
            self.written += len(batch)
            if self.size >= MAX_BYTES:
                self._rotate()

    def _open(self) -> None:
        target = events_file()
        if self.fd is not None and self.path == target:
            # soubor mohl někdo smazat nebo přesunout (ruční úklid, jiná rotace)
            try:
                if os.stat(target).st_ino == os.fstat(self.fd).st_ino:
                    return
            except OSError:
                pass
            self._close()
        target.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        self.path = target
        self.size = os.fstat(self.fd).st_size

    def _close(self) -> None:
        if self.fd is not None:
            with contextlib.suppress(OSError):
                os.close(self.fd)
        self.fd = None

    def _rotate(self) -> None:
        path = self.path
        self._close()
        if path is None:
            return
        try:
            if KEEP <= 0:
                path.unlink(missing_ok=True)
            else:
                oldest = Path(f"{path}.{KEEP}")
                oldest.unlink(missing_ok=True)
                for i in range(KEEP - 1, 0, -1):
                    src = Path(f"{path}.{i}")
                    if src.exists():
                        os.replace(src, f"{path}.{i + 1}")
                os.replace(path, f"{path}.1")
        except OSError:
            pass
        self.size = 0


_w = _Writer()


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _line(kind: str, fields: dict[str, Any]) -> str:
    rec: dict[str, Any] = {"ts": _now_iso(), "kind": kind, "sid": SESSION_ID}
    for k, v in fields.items():
        if k not in rec:
            rec[k] = v
    return json.dumps(rec, ensure_ascii=False, default=str) + "\n"


def _session_fields() -> dict[str, Any]:
    fields: dict[str, Any] = {
        "version": _version(),
        "pid": os.getpid(),
        "python": sys.version.split()[0],
        "argv": " ".join(sys.argv[1:])[:200],
        "systemd": bool(os.environ.get("INVOCATION_ID")),
    }
    with contextlib.suppress(Exception):
        fields["host"] = socket.gethostname()
    with contextlib.suppress(Exception):
        with open("/proc/uptime") as f:
            fields["boot_uptime_s"] = int(float(f.read().split()[0]))
    return fields


def event(kind: str, **fields: Any) -> None:
    """Zapíše jednu událost. Nikdy nevyhodí výjimku."""
    if not ENABLED:
        return
    try:
        if not _w.started:
            _w.started = True
            _w.put(_line("session.start", _session_fields()))
        _w.put(_line(kind, fields))
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


def flush() -> None:
    """Dopíše frontu hned (testy, konec procesu). Nikdy nevyhodí výjimku."""
    try:
        _w.flush()
    except Exception:
        pass


def stats() -> dict[str, int]:
    """Kolik řádků se zapsalo / zahodilo — pro sys.sample."""
    return {"written": _w.written, "dropped": _w.dropped, "pending": len(_w.pending)}


def clip(text: Any, limit: int = 300) -> str:
    """Zkrácený text do události (prompt, odpověď, chyba)."""
    s = str(text or "")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def child_env() -> dict[str, str]:
    """Proměnné pro podprocesy, které si zapisují samy (shim yt-dlp)."""
    if not ENABLED:
        return {"YTDJ_TELEMETRY": "0"}
    return {"YTDJ_EVENTS_FILE": str(events_file()), "YTDJ_SID": SESSION_ID}


def _at_exit() -> None:
    if ENABLED and _w.started:
        try:
            _w.put(_line("session.end", {
                "uptime_s": int(time.monotonic() - _T0),
                "events": _w.written + len(_w.pending),
                "dropped": _w.dropped,
            }))
        except Exception:
            pass
    flush()


atexit.register(_at_exit)


if __name__ == "__main__":
    from .telemetry_report import main

    sys.exit(main(sys.argv[1:]))
