"""Trvale běžící yt-dlp pro mpv — skladby se řeší ~2× rychleji a nikdy dvě naráz.

Spouští ho ytdj (MpvPlayer) interpretem, pod kterým je nainstalované yt-dlp
i s pluginy (uv tool), takže tenhle soubor nesmí nic importovat z ytdj.

Proč: yt-dlp spuštěné pro každou skladbu znovu na Pi 3 trvá ~18–20 s —
start Pythonu a pluginů, a hlavně node, který pokaždé znovu parsuje ~2,5 MB
přehrávače YouTube kvůli podpisové výzvě. Tady běží jeden YoutubeDL pořád a
předzpracovaný přehrávač se drží v cache yt-dlp (EJS to umí, jen to má ve
výchozím stavu vypnuté), takže skladba trvá ~8 s. A protože pracuje jediné
vlákno, nepoběží nikdy dva node současně — na 1 GB RAM se to jinak slévalo
do swapu a v repráku to lupalo.

Protokol (unixový socket, jeden JSON řádek dotaz → jeden řádek odpověď):
  {"op": "get", "argv": [...]}   JSON pro mpv (jako `yt-dlp -J`), čeká na výsledek
  {"op": "ahead", "ids": [...]}  co vyřešit dopředu, v tomhle pořadí; nahrazuje
                                 předchozí seznam (zastaralé se zahodí)
  {"op": "ping"}

Stav a měření posílá na stderr strojově čitelnými řádky `EVENT {json}`, které
MpvPlayer převádí na provozní události (ytdj.telemetry); ostatní řádky jsou
lidský log. Druhy začínající "_" jsou jen vnitřní stav pro ytdj (co je
vyřešené, na čem se pracuje) a do logu se nepíšou.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import threading
import time

_T_IMPORT = time.monotonic()
import yt_dlp  # noqa: E402  — import sám trvá na Pi 3 desítky vteřin, měří se
_IMPORT_MS = int((time.monotonic() - _T_IMPORT) * 1000)

try:  # předzpracovaný přehrávač v cache — hlavní zrychlení (~9 s → ~0 s v node)
    from yt_dlp.extractor.youtube.jsc._builtin import ejs as _ejs

    _ejs.EJSBaseJCP._ENABLE_PREPROCESSED_PLAYER_CACHE = True
except Exception:  # jiná verze yt-dlp — poběží to, jen pomaleji
    _ejs = None

WATCH_URL = "https://music.youtube.com/watch?v={}"
VIDEO_ID = re.compile(r"[?&]v=([\w-]{11})")
MAX_AGE = 90 * 60  # s — adresy streamů YouTube platí ~6 h, rezerva velká


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def emit(kind: str, **fields) -> None:
    """Strojově čitelná událost pro ytdj — nikdy nevyhodí výjimku."""
    try:
        fields["kind"] = kind
        print("EVENT " + json.dumps(fields, ensure_ascii=False), file=sys.stderr, flush=True)
    except Exception:
        pass


class Resolver:
    def __init__(self) -> None:
        self.cv = threading.Condition()
        self.template: list[str] | None = None  # argv mpv bez URL
        self.ydl: yt_dlp.YoutubeDL | None = None
        self.ready: dict[str, tuple[float, str]] = {}  # vid → (čas, JSON)
        self.failed: dict[str, tuple[float, str]] = {}
        self.urgent: list[str] = []  # na tyhle čeká mpv
        self.ahead: list[str] = []
        self.busy: str | None = None

    def _state(self) -> None:
        """Co je nachystané a na čem se dělá — volá se se zamčeným cv."""
        emit("_state", ready=list(self.ready), busy=self.busy, urgent=list(self.urgent),
             ahead=len(self.ahead))

    # ---- vlákno, které jediné volá yt-dlp ----

    def worker(self) -> None:
        while True:
            with self.cv:
                vid = self._next()
                while vid is None:
                    self.cv.wait()
                    vid = self._next()
                self.busy = vid
                why = "urgent" if vid in self.urgent else "ahead"
                ydl = self.ydl
                self._state()
            t0 = time.monotonic()
            try:
                info = ydl.extract_info(WATCH_URL.format(vid), download=False)
                data = json.dumps(ydl.sanitize_info(info))
                result, error = data, None
            except Exception as exc:  # nepřehratelné, síť…
                result, error = None, str(exc).splitlines()[0][:300]
            took = time.monotonic() - t0
            with self.cv:
                self.busy = None
                if vid in self.urgent:
                    self.urgent.remove(vid)
                if result is not None:
                    self.ready[vid] = (time.time(), result)
                    log(f"vyřešeno {vid} za {took:.1f} s")
                else:
                    self.failed[vid] = (time.time(), error or "?")
                    log(f"selhalo {vid} za {took:.1f} s: {error}")
                emit("resolver.resolve", video_id=vid, took_ms=int(took * 1000), why=why,
                     ok=result is not None, error=error, size_kb=len(result or "") // 1024)
                self._state()
                self.cv.notify_all()

    def _next(self) -> str | None:
        if self.ydl is None:
            return None  # bez šablony od mpv nevíme, s jakými volbami
        now = time.time()
        for vid in self.urgent:
            return vid
        for vid in self.ahead:
            fresh = vid in self.ready and now - self.ready[vid][0] < MAX_AGE / 2
            if not fresh and vid not in self.failed:
                return vid
        return None

    # ---- požadavky ----

    def set_template(self, argv: list[str]) -> None:
        template = argv[:-1]
        if template == self.template:
            return
        parsed = yt_dlp.parse_options(template + ["--", WATCH_URL.format("dQw4w9WgXcQ")])
        opts = dict(parsed.ydl_opts)
        opts["quiet"] = True
        opts["no_warnings"] = True
        t0 = time.monotonic()
        self.ydl = yt_dlp.YoutubeDL(opts)
        self.template = template
        log("šablona od mpv převzata")
        emit("resolver.template", took_ms=int((time.monotonic() - t0) * 1000))

    def get(self, argv: list[str], timeout: float = 150.0) -> tuple[str | None, str | None]:
        m = VIDEO_ID.search(argv[-1]) if argv else None
        if not m:
            return None, "neznámé video"
        vid = m.group(1)
        t0 = time.monotonic()
        with self.cv:
            data, error, how, blocked_by = self._get(vid, argv, timeout)
            # "hit" = mpv dostalo hotové, "wait" = řešilo se, zatímco mpv čekalo
            emit("resolver.get", video_id=vid, hit=how == "hit", how=how,
                 wait_ms=int((time.monotonic() - t0) * 1000), ok=data is not None,
                 error=error, blocked_by=blocked_by)
            self._state()
            return data, error

    def _get(self, vid: str, argv: list[str], timeout: float):
        self.set_template(argv)
        self.failed.pop(vid, None)  # mpv to chce teď — zkusit znovu
        hit = self.ready.pop(vid, None)
        if hit and time.time() - hit[0] < MAX_AGE:
            log(f"z cache {vid}")
            return hit[1], None, "hit", None
        how = "stale" if hit else ("joined" if self.busy == vid else "miss")
        # na co mpv čeká, protože jediné vlákno dělá jinou skladbu dopředu
        blocked_by = self.busy if self.busy and self.busy != vid else None
        if vid not in self.urgent:
            self.urgent.insert(0, vid)
        self.cv.notify_all()
        deadline = time.monotonic() + timeout
        while True:
            if vid in self.ready:
                return self.ready.pop(vid)[1], None, how, blocked_by
            if vid in self.failed and vid not in self.urgent and self.busy != vid:
                return None, self.failed.pop(vid)[1], how, blocked_by
            left = deadline - time.monotonic()
            if left <= 0:
                return None, "vypršel čas", how, blocked_by
            self.cv.wait(left)

    def set_ahead(self, ids: list[str]) -> None:
        with self.cv:
            self.ahead = [i for i in ids if isinstance(i, str) and len(i) == 11]
            # co už nikdo nechce, v paměti nedržet
            keep = set(self.ahead) | set(self.urgent)
            dropped = 0
            for d in (self.ready, self.failed):
                for vid in [v for v in d if v not in keep]:
                    del d[vid]
                    dropped += 1
            emit("resolver.ahead", n=len(self.ahead),
                 ready=sum(1 for v in self.ahead if v in self.ready), dropped=dropped)
            self._state()
            self.cv.notify_all()


def serve(path: str) -> None:
    resolver = Resolver()
    threading.Thread(target=resolver.worker, name="resolver", daemon=True).start()
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    os.chmod(path, 0o600)
    srv.listen(8)
    log(f"resolver běží ({'s' if _ejs else 'bez'} cache přehrávače)")
    emit("resolver.ready", import_ms=_IMPORT_MS, ejs_cache=_ejs is not None,
         yt_dlp=getattr(getattr(yt_dlp, "version", None), "__version__", "?"))

    def handle(conn: socket.socket) -> None:
        with conn, conn.makefile("rwb") as f:
            for line in f:
                try:
                    req = json.loads(line)
                    op = req.get("op")
                    if op == "get":
                        data, err = resolver.get(list(req.get("argv") or []))
                        resp = {"ok": data is not None, "json": data, "error": err}
                    elif op == "ahead":
                        resolver.set_ahead(list(req.get("ids") or []))
                        resp = {"ok": True}
                    elif op == "ping":
                        resp = {"ok": True, "template": resolver.template is not None}
                    else:
                        resp = {"ok": False, "error": f"neznámé op {op!r}"}
                except Exception as exc:
                    resp = {"ok": False, "error": str(exc)}
                f.write(json.dumps(resp).encode() + b"\n")
                f.flush()

    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--socket", required=True)
    serve(p.parse_args().socket)
