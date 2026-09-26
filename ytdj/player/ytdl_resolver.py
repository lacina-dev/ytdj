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
  {"op": "ahead", "ids": [...], "hold": false}
                                 co vyřešit dopředu, v tomhle pořadí; nahrazuje
                                 předchozí seznam (zastaralé se zahodí). S hold
                                 se hotové drží, ale nic nového dopředu nezačne
                                 (běží Codex, paměť) — urgentní dotazy mpv ano.
                                 "first": [...] se řeší před vším v ahead, i s hold;
                                 "keep": [...] se nechá v cache, ale neřeší se
                                 (hrající / právě načítaná skladba)
  {"op": "cancel", "ids": [...]} mpv tyhle opustilo při načítání (posluchač
                                 dal Další): jejich čekající "get" hned vrátí
                                 chybu "zrušeno", ať se mpv pohne dál, a
                                 z urgentních vypadnou
  {"op": "drop", "ids": [...]}   mpv skladbu z téhle adresy nepřehrálo — hotové
                                 zahodit, příště vyřešit znovu
  {"op": "ping"}

Stav a měření posílá na stderr strojově čitelnými řádky `EVENT {json}`, které
MpvPlayer převádí na provozní události (ytdj.telemetry); ostatní řádky jsou
lidský log. Druhy začínající "_" jsou jen vnitřní stav pro ytdj (co je
vyřešené, na čem se pracuje) a do logu se nepíšou.

Restart služby (nasazení, pád, watchdog): dřív resolver začínal od nuly —
socket vznikl až po importu yt-dlp (na Pi 3 vteřiny až desítky vteřin), shim
mezitím spouštěl skutečné yt-dlp (~19 s ticha) a prefetch čekal na první
dotaz mpv (bez šablony nevěděl s jakými volbami). Teď:
  * socket poslouchá hned, yt-dlp se importuje až ve vlákně workeru,
  * vyřešené skladby i šablona se průběžně ukládají do RAM (--cache v
    $XDG_RUNTIME_DIR, tmpfs: restart služby přežije, reboot ne, SD karta nic)
    a po startu se hned podávají — zkontrolované krátkým dotazem na
    googlevideo (Range 0-0), mrtvá adresa (403/404/410) se vyřeší znovu,
  * se šablonou z cache se dopředu řeší hned po importu, nečeká se na mpv.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import socket
import sys
import threading
import time

# yt-dlp se importuje až ve workeru (_import_ytdlp): import sám trvá na Pi 3
# vteřiny až desítky vteřin a socket s hotovými skladbami z cache musí
# poslouchat hned po startu.
yt_dlp = None
_ejs = None  # předzpracovaný přehrávač v cache (hlavní zrychlení ~9 s → ~0 s v node)
_IMPORT_MS: int | None = None

WATCH_URL = "https://music.youtube.com/watch?v={}"
VIDEO_ID = re.compile(r"[?&]v=([\w-]{11})")
VID_OK = re.compile(r"^[\w-]{11}$")
MAX_AGE = 90 * 60  # s — adresy streamů YouTube platí ~6 h, rezerva velká
EXPIRE = re.compile(r"[?&/]expire[=/](\d{9,11})")
EXPIRE_MARGIN = 10 * 60  # s — adresu, které zbývá méně, už nepodávat
# Jak dlouho čeká "get" skladby, která v cache není, na import yt-dlp; pak
# odpoví STARTUP_ERROR a shim pustí skutečné yt-dlp (jako před cache). 0 =
# hned — nikdy hůř než dřív (~19 s). Víc se vyplatí, jen když je import na Pi
# (resolver.ready import_ms) krátký proti ~19 s skutečného yt-dlp.
STARTUP_WAIT = 0.0
PROBE_TIMEOUT = 3.0  # s — kontrola adresy z cache na disku
# Mladší hotové z disku se podají bez kontroly adresy: platí ~6 h a jsou
# vázané na IP, která se za pár minut restartu nemění. Kontrola (vlákno
# s HTTPS dotazem) se po restartu přetahovala o GIL s importem yt-dlp a
# navazovaná skladba na ni čekala 2,5 s (Pi 26. 9.). Když přesto mpv adresu
# neotevře, přehrávač ji zahodí (drop) a tutéž položku načte znovu čerstvou.
TRUST_AGE = 30 * 60  # s
CACHE_MAX = 50  # kolik skladeb nejvýš ukládat
CACHE_DEBOUNCE = 2.0  # s — dávka změn se zapíše najednou
DISK_GRACE = 120.0  # s — po startu se hotové z disku drží, i když nejsou v okně ahead
CACHE_MAGIC = "YTDJ-RESOLVER-CACHE 1"
STARTUP_ERROR = "resolver startuje"


def _import_ytdlp() -> None:
    global yt_dlp, _ejs, _IMPORT_MS
    t0 = time.monotonic()
    import yt_dlp as mod

    try:
        from yt_dlp.extractor.youtube.jsc._builtin import ejs

        ejs.EJSBaseJCP._ENABLE_PREPROCESSED_PLAYER_CACHE = True
        _ejs = ejs
    except Exception:  # jiná verze yt-dlp — poběží to, jen pomaleji
        _ejs = None
    yt_dlp = mod
    _IMPORT_MS = int((time.monotonic() - t0) * 1000)


def url_hash(url: str) -> str:
    """Do logu nikdy celou adresu streamu (platí jako heslo) — jen otisk."""
    return hashlib.sha256(url.encode()).hexdigest()[:10]


def expires_at(data: str) -> int | None:
    """Nejbližší `expire=` v adresách JSONu pro mpv (unixový čas), nebo None."""
    found = [int(m.group(1)) for m in EXPIRE.finditer(data)]
    return min(found) if found else None


def usable(t: float, data: str, now: float | None = None) -> bool:
    """Dá se hotový JSON ještě podat? Stáří i platnost adresy s rezervou."""
    now = time.time() if now is None else now
    if not -60 <= now - t < MAX_AGE:  # čas z budoucnosti = posunuté hodiny, nevěřit
        return False
    exp = expires_at(data)
    return exp is None or exp - EXPIRE_MARGIN > now


def probe(data: str) -> tuple[str, int | None, str | None]:
    """Žije adresa streamu z JSONu? ("ok" | "dead" | "unknown", HTTP kód, otisk).

    Jeden GET s Range 0-0 na každou adresu, kterou by mpv otevřelo. Jen 401/
    403/404/410 znamená mrtvou (vypršelá, jiná IP); cokoli jiného (síť,
    timeout) je "unknown" a adresa se podá — jako dřív z paměti.
    """
    import urllib.error
    import urllib.request

    try:
        info = json.loads(data)
    except ValueError:
        return "dead", None, None
    fmts = info.get("requested_formats") or [info]
    status, h = None, None
    for f in fmts:
        url = f.get("url") if isinstance(f, dict) else None
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        h = url_hash(url)
        hdrs = f.get("http_headers") or info.get("http_headers") or {}
        headers = {str(k): str(v) for k, v in hdrs.items() if isinstance(v, (str, int))}
        headers["Range"] = "bytes=0-0"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                        timeout=PROBE_TIMEOUT) as r:
                status = r.status
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 404, 410):
                return "dead", exc.code, h
            return "unknown", exc.code, h
        except Exception:
            return "unknown", None, h
    if status is None:
        return "unknown", None, h
    return "ok", status, h


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def emit(kind: str, **fields) -> None:
    """Strojově čitelná událost pro ytdj — nikdy nevyhodí výjimku."""
    try:
        fields["kind"] = kind
        print("EVENT " + json.dumps(fields, ensure_ascii=False), file=sys.stderr, flush=True)
    except Exception:
        pass


class DiskCache:
    """Hotové skladby a šablona v RAM (tmpfs) — přežijí restart služby.

    Formát: první řádek `CACHE_MAGIC {"template": [...]}`, pak řádek na
    skladbu `videoId<TAB>čas<TAB>JSON pro mpv`. JSON od json.dumps nemá holé
    konce řádků, takže se při zápisu ani čtení nic znovu neescapuje a
    neparsuje. Soubor jen pro vlastníka (adresy streamů platí jako heslo).
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.written: tuple | None = None  # podpis naposledy zapsaného obsahu

    def load(self) -> tuple[list[str] | None, dict[str, tuple[float, str]], dict]:
        stats: dict = {"loaded": 0, "expired": 0, "bad": 0, "kb": 0}
        entries: dict[str, tuple[float, str]] = {}
        try:
            with open(self.path, encoding="utf-8") as f:
                head = f.readline()
                if not head.startswith(CACHE_MAGIC + " "):
                    raise ValueError("chybí hlavička")
                meta = json.loads(head[len(CACHE_MAGIC) + 1:])
                template = meta.get("template") if isinstance(meta, dict) else None
                if not (isinstance(template, list) and all(isinstance(a, str) for a in template)):
                    raise ValueError("vadná šablona")
                size = len(head)
                now = time.time()
                for line in f:
                    size += len(line)
                    parts = line.rstrip("\n").split("\t", 2)
                    try:
                        vid, t, data = parts[0], float(parts[1]), parts[2]
                    except (IndexError, ValueError):
                        stats["bad"] += 1
                        continue
                    if not VID_OK.match(vid) or not data.startswith("{") or not data.endswith("}"):
                        stats["bad"] += 1
                        continue
                    if not usable(t, data, now):
                        stats["expired"] += 1
                        continue
                    entries[vid] = (t, data)
        except FileNotFoundError:
            return None, {}, stats
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            log(f"cache na disku nejde přečíst ({exc}) — začínám naprázdno")
            stats["corrupt"] = True
            return None, {}, stats
        if len(entries) > CACHE_MAX:
            keep = sorted(entries, key=lambda v: entries[v][0], reverse=True)[:CACHE_MAX]
            entries = {v: entries[v] for v in keep}
        stats["loaded"] = len(entries)
        stats["kb"] = size // 1024
        return template, entries, stats

    def write(self, template: list[str], items: list[tuple[str, float, str]]) -> bool:
        """Atomicky (tmp + rename), jen když se obsah změnil. True = zapsáno."""
        sig = (tuple(template), tuple((v, t) for v, t, _ in items))
        with self.lock:
            if sig == self.written:
                return False
            tmp = f"{self.path}.tmp"
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(CACHE_MAGIC + " " + json.dumps({"template": template,
                                                        "saved": time.time()}) + "\n")
                for vid, t, data in items:
                    if "\n" not in data:
                        f.write(f"{vid}\t{t!r}\t{data}\n")
            os.replace(tmp, self.path)
            self.written = sig
            return True


class Resolver:
    def __init__(self, disk: DiskCache | None = None) -> None:
        self.cv = threading.Condition()
        self.template: list[str] | None = None  # argv mpv bez URL
        self.ydl = None  # yt_dlp.YoutubeDL pro ydl_for (staví ho worker)
        self.ydl_for: list[str] | None = None
        self.loaded = False  # yt-dlp naimportované — do té doby jen hotové z cache
        self.ready: dict[str, tuple[float, str]] = {}  # vid → (čas, JSON)
        self.failed: dict[str, tuple[float, str]] = {}
        self.urgent: list[str] = []  # na tyhle čeká mpv
        self.ahead: list[str] = []
        self.hold = False  # nic nového dopředu (běží Codex)
        self.first: list[str] = []  # přednostně, i během hold (ytdj čeká na přepnutí)
        self.busy: str | None = None
        self.waiting: dict[str, int] = {}  # vid → kolik "get" na něj čeká
        self.cancelled: set[str] = set()
        # ---- cache na disku (tmpfs) ----
        self.disk = disk
        self.from_disk: set[str] = set()  # hotové načtené po startu (ne vyřešené tímhle během)
        self.unverified: set[str] = set()  # z disku, adresa ještě nezkontrolovaná
        self.trusted: set[str] = set()  # z disku, mladé — podají se bez kontroly
        self.verifying: dict[str, threading.Event] = {}
        self.t_start = time.monotonic()
        self._tmpl_changed = False

    def _state(self) -> None:
        """Co je nachystané a na čem se dělá — volá se se zamčeným cv."""
        emit("_state", ready=list(self.ready), busy=self.busy, urgent=list(self.urgent),
             ahead=len(self.ahead))

    def _forget(self, vid: str) -> None:
        """Hotové pryč (se zamčeným cv)."""
        self.ready.pop(vid, None)
        self.from_disk.discard(vid)
        self.unverified.discard(vid)
        self.trusted.discard(vid)

    def _dirty(self) -> None:
        if self.disk is not None:
            self.disk.wake.set()

    # ---- cache na disku ----

    def load_disk(self) -> None:
        if self.disk is None:
            return
        t0 = time.monotonic()
        template, entries, stats = self.disk.load()
        with self.cv:
            if template is not None and self.template is None:
                self.template = template
                now = time.time()
                for vid, entry in entries.items():
                    self.ready.setdefault(vid, entry)
                    self.from_disk.add(vid)
                    if 0 <= now - entry[0] < TRUST_AGE:
                        self.trusted.add(vid)
                    else:
                        self.unverified.add(vid)
                stats["trusted"] = len(self.trusted)
                self._state()  # ytdj hned ví, co je hotové (okno ahead, track.request)
            else:
                entries = {}
        if entries:
            log(f"z disku {len(entries)} hotových skladeb")
        emit("resolver.disk", phase="load", took_ms=int((time.monotonic() - t0) * 1000),
             template=template is not None, **stats)

    def flush(self) -> None:
        """Zapíše hotové do cache na disku (když se od minula něco změnilo)."""
        if self.disk is None:
            return
        with self.cv:
            if self.template is None:
                return
            tmpl = list(self.template)
            items = sorted(((v, t, d) for v, (t, d) in self.ready.items()),
                           key=lambda x: x[1], reverse=True)[:CACHE_MAX]
        try:
            self.disk.write(tmpl, items)
        except OSError as exc:
            log(f"cache na disk nejde zapsat: {exc}")

    def persist_loop(self) -> None:
        assert self.disk is not None
        while True:
            self.disk.wake.wait()
            time.sleep(CACHE_DEBOUNCE)  # dávka změn (okno ahead, nové vyřešené) najednou
            self.disk.wake.clear()
            self.flush()

    def _verify(self, vid: str) -> None:
        """Adresa z disku ještě platí? Mrtvou zahodí (vyřeší se znovu)."""
        with self.cv:
            entry = self.ready.get(vid)
            if vid not in self.unverified or entry is None:
                self.unverified.discard(vid)
                return
            ev = self.verifying.get(vid)
            mine = ev is None
            if mine:
                ev = self.verifying[vid] = threading.Event()
        if not mine:
            ev.wait(PROBE_TIMEOUT * 2 + 1)  # kontroluje ji jiné vlákno
            return
        t0 = time.monotonic()
        try:
            verdict, status, h = probe(entry[1])
        except Exception:
            verdict, status, h = "unknown", None, None
        with self.cv:
            self.unverified.discard(vid)
            self.verifying.pop(vid, None)
            if verdict == "dead" and self.ready.get(vid) is entry:
                self._forget(vid)
                self._dirty()
                self._state()
            self.cv.notify_all()
        ev.set()
        log(f"z disku {vid} [{h}]: {verdict} ({status})")
        emit("resolver.disk", phase="verify", video_id=vid, verdict=verdict, status=status,
             url_hash=h, took_ms=int((time.monotonic() - t0) * 1000))

    def verify_loop(self) -> None:
        """Po startu zkontroluje adresy z disku — nejdřív ty, na které se čeká."""
        while True:
            with self.cv:
                order = self.urgent + self.first + self.ahead
                pick = next((v for v in order if v in self.unverified), None)
                if pick is None:
                    pick = next(iter(sorted(self.unverified)), None)
                if pick is None:
                    return
            self._verify(pick)

    # ---- vlákno, které jediné volá yt-dlp ----

    def _build(self, tmpl: list[str]):
        t0 = time.monotonic()
        parsed = yt_dlp.parse_options(tmpl + ["--", WATCH_URL.format("dQw4w9WgXcQ")])
        opts = dict(parsed.ydl_opts)
        opts["quiet"] = True
        opts["no_warnings"] = True
        ydl = yt_dlp.YoutubeDL(opts)
        emit("resolver.template", took_ms=int((time.monotonic() - t0) * 1000),
             changed=self._tmpl_changed)
        return ydl

    def worker(self) -> None:
        if not self.loaded:
            try:
                _import_ytdlp()
            except BaseException as exc:  # bez yt-dlp není co dělat — jako dřív: skončit
                log(f"yt-dlp nejde importovat: {exc}")
                emit("resolver.import_failed", error=str(exc)[:300])
                os._exit(3)
            with self.cv:
                self.loaded = True
                self.cv.notify_all()
            log(f"yt-dlp načteno za {_IMPORT_MS} ms ({'s' if _ejs else 'bez'} cache přehrávače)")
            emit("resolver.ready", import_ms=_IMPORT_MS, ejs_cache=_ejs is not None,
                 yt_dlp=getattr(getattr(yt_dlp, "version", None), "__version__", "?"))
        while True:
            with self.cv:
                vid = self._next()
                while vid is None:
                    self.cv.wait()
                    vid = self._next()
                self.busy = vid
                why = "urgent" if vid in self.urgent else ("first" if vid in self.first else "ahead")
                tmpl = self.template
                ydl = self.ydl if self.ydl_for is tmpl else None
                self._state()
            t0 = time.monotonic()
            try:
                if ydl is None:
                    ydl = self._build(tmpl)
                    with self.cv:
                        if self.template is tmpl:
                            self.ydl, self.ydl_for = ydl, tmpl
                    t0 = time.monotonic()
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
                if self.template is not tmpl:
                    # mezitím přišel dotaz s jinými volbami (jiný formát) —
                    # výsledek k nim nepatří; kdo na něj čeká, dostane nový
                    log(f"{vid}: šablona se změnila, výsledek zahazuji")
                    if vid in self.waiting and vid not in self.cancelled:
                        self.urgent.insert(0, vid)
                elif result is not None:
                    self._forget(vid)
                    self.ready[vid] = (time.time(), result)
                    self._dirty()
                    log(f"vyřešeno {vid} za {took:.1f} s")
                else:
                    self.failed[vid] = (time.time(), error or "?")
                    log(f"selhalo {vid} za {took:.1f} s: {error}")
                emit("resolver.resolve", video_id=vid, took_ms=int(took * 1000), why=why,
                     ok=result is not None, error=error, size_kb=len(result or "") // 1024)
                self._state()
                self.cv.notify_all()

    def _next(self) -> str | None:
        if not self.loaded or self.template is None:
            return None  # bez yt-dlp nebo bez šablony (od mpv / z disku) nevíme, s jakými volbami
        now = time.time()
        for vid in self.urgent:
            return vid
        for vid in self.first + ([] if self.hold else self.ahead):
            fresh = vid in self.ready and now - self.ready[vid][0] < MAX_AGE / 2
            if not fresh and vid not in self.failed:
                return vid
        return None

    # ---- požadavky ----

    def set_template(self, argv: list[str]) -> None:
        template = argv[:-1]
        if template == self.template:
            return
        changed = self.template is not None
        self.template = template
        self.ydl, self.ydl_for = None, None  # postaví worker (potřebuje yt-dlp)
        self._tmpl_changed = changed
        if changed:
            # Hotové JSONy jsou s jinými volbami (formát!) — mpv by dostalo
            # třeba video HLS místo zvuku. Stává se jen, když se resolveru
            # zeptá někdo jiný než mpv (ruční ladění shimem), nebo se po
            # restartu změnilo nastavení formátu (šablona z disku).
            self.ready.clear()
            self.failed.clear()
            self.from_disk.clear()
            self.unverified.clear()
            self.trusted.clear()
        self._dirty()
        log("šablona od mpv převzata")

    def get(self, argv: list[str], timeout: float = 150.0) -> tuple[str | None, str | None]:
        m = VIDEO_ID.search(argv[-1]) if argv else None
        if not m:
            return None, "neznámé video"
        vid = m.group(1)
        t0 = time.monotonic()
        if argv[:-1] == self.template:
            self._verify(vid)  # jen starší skladba z disku: platí její adresa ještě?
        with self.cv:
            self.waiting[vid] = self.waiting.get(vid, 0) + 1
            trusted = vid in self.trusted
            disk_t = self.ready[vid][0] if vid in self.from_disk and vid in self.ready else None
            try:
                data, error, how, blocked_by = self._get(vid, argv, timeout)
            finally:
                self.waiting[vid] -= 1
                if self.waiting[vid] <= 0:
                    del self.waiting[vid]
                    self.cancelled.discard(vid)
            extra = {}
            if how == "disk":
                # verified=False: podáno bez kontroly adresy (mladší než TRUST_AGE)
                extra = {"verified": not trusted,
                         "age_s": int(time.time() - disk_t) if disk_t else None}
            # "hit" = mpv dostalo hotové ("disk" = hotové z cache před restartem),
            # "wait" = řešilo se, zatímco mpv čekalo
            emit("resolver.get", video_id=vid, hit=how in ("hit", "disk"), how=how,
                 wait_ms=int((time.monotonic() - t0) * 1000), ok=data is not None,
                 error=error, blocked_by=blocked_by, **extra)
            self._state()
            return data, error

    def _get(self, vid: str, argv: list[str], timeout: float):
        self.set_template(argv)
        self.failed.pop(vid, None)  # mpv to chce teď — zkusit znovu
        self.cancelled.discard(vid)
        # Hotové zůstává v cache, dokud je ve frontě (zahodí ho až set_ahead):
        # mpv si skladbu vyžádá i při --prefetch-playlist a potom znovu, když
        # se fronta mezitím změnila a na skladbu dojde později.
        hit = self.ready.get(vid)
        if hit and usable(hit[0], hit[1]):
            disk = vid in self.from_disk
            log(f"z {'disku' if disk else 'cache'} {vid}")
            return hit[1], None, "disk" if disk else "hit", None
        if hit:
            self._forget(vid)
            self._dirty()
        how = "stale" if hit else ("joined" if self.busy == vid else "miss")
        # na co mpv čeká, protože jediné vlákno dělá jinou skladbu dopředu
        blocked_by = self.busy if self.busy and self.busy != vid else None
        if vid not in self.urgent:
            self.urgent.insert(0, vid)
        self.cv.notify_all()
        t_in = time.monotonic()
        deadline = t_in + timeout
        while True:
            if vid in self.ready:
                return self.ready[vid][1], None, how, blocked_by
            if vid in self.cancelled:
                return None, "zrušeno", "cancelled", blocked_by
            if vid in self.failed and vid not in self.urgent and self.busy != vid:
                return None, self.failed.pop(vid)[1], how, blocked_by
            now = time.monotonic()
            if not self.loaded and now - t_in >= STARTUP_WAIT:
                # yt-dlp se pořád importuje — ať mpv nečeká neurčito, shim
                # pustí skutečné yt-dlp (jako dřív); tady se to řešit nebude
                if vid in self.urgent:
                    self.urgent.remove(vid)
                return None, STARTUP_ERROR, "startup", blocked_by
            left = deadline - now
            if left <= 0:
                return None, "vypršel čas", how, blocked_by
            if not self.loaded:
                left = min(left, max(0.01, STARTUP_WAIT - (now - t_in)))
            self.cv.wait(left)

    def cancel(self, ids: list[str]) -> None:
        with self.cv:
            for vid in ids:
                if vid not in self.waiting:
                    continue  # mpv na ni nečeká — není co rušit
                self.cancelled.add(vid)
                if vid in self.urgent:
                    self.urgent.remove(vid)
                emit("resolver.cancel", video_id=vid, busy=self.busy == vid)
            self._state()
            self.cv.notify_all()

    def drop(self, ids: list[str]) -> None:
        """mpv skladbu z hotové adresy nepřehrálo — příště ji vyřešit znovu."""
        with self.cv:
            gone = [v for v in ids if isinstance(v, str) and v in self.ready]
            for vid in gone:
                self._forget(vid)
            if gone:
                self._dirty()
                emit("resolver.drop", ids=gone)
                self._state()
                self.cv.notify_all()

    def set_ahead(self, ids: list[str], hold: bool = False, first: list[str] | None = None,
                  keep: list[str] | None = None) -> None:
        with self.cv:
            self.ahead = [i for i in ids if isinstance(i, str) and len(i) == 11]
            self.first = [i for i in first or () if isinstance(i, str) and len(i) == 11]
            self.hold = bool(hold)
            # co už nikdo nechce, v paměti nedržet
            keep = (set(self.ahead) | set(self.first) | set(self.urgent) | set(self.waiting)
                    | set(keep or ()))
            # Hned po restartu je fronta v mpv ještě prázdná / neúplná (ytdj ji
            # teprve obnovuje) — hotové z disku se chvíli drží, i když v okně nejsou.
            if time.monotonic() - self.t_start < DISK_GRACE:
                keep |= self.from_disk
            dropped = 0
            for vid in [v for v in self.ready if v not in keep]:
                self._forget(vid)
                dropped += 1
            for vid in [v for v in self.failed if v not in keep]:
                del self.failed[vid]
                dropped += 1
            if dropped:
                self._dirty()
            emit("resolver.ahead", n=len(self.ahead), hold=self.hold,
                 ready=sum(1 for v in self.ahead if v in self.ready), dropped=dropped)
            self._state()
            self.cv.notify_all()


def serve(path: str, cache: str | None = None) -> None:
    resolver = Resolver(DiskCache(cache) if cache else None)
    resolver.load_disk()  # malý soubor v RAM — hotové se podávají hned od prvního dotazu
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    os.chmod(path, 0o600)
    srv.listen(8)
    log(f"resolver poslouchá ({len(resolver.ready)} hotových z disku), načítám yt-dlp")
    emit("resolver.listen", disk=len(resolver.ready), template=resolver.template is not None)
    threading.Thread(target=resolver.worker, name="resolver", daemon=True).start()
    if resolver.unverified:
        threading.Thread(target=resolver.verify_loop, name="verify", daemon=True).start()
    if resolver.disk is not None:
        threading.Thread(target=resolver.persist_loop, name="persist", daemon=True).start()

        def on_term(*_a) -> None:
            resolver.flush()  # poslední změny (≤ CACHE_DEBOUNCE) nezahodit
            os._exit(0)

        signal.signal(signal.SIGTERM, on_term)

    def handle(conn: socket.socket) -> None:
        try:
            serve_conn(conn)
        except (BrokenPipeError, ConnectionResetError):
            pass  # klient odešel (zrušené čekání při přeskočení) — odpověď nikdo nečte

    def serve_conn(conn: socket.socket) -> None:
        with conn, conn.makefile("rwb") as f:
            for line in f:
                try:
                    req = json.loads(line)
                    op = req.get("op")
                    if op == "get":
                        data, err = resolver.get(list(req.get("argv") or []))
                        resp = {"ok": data is not None, "json": data, "error": err}
                    elif op == "ahead":
                        resolver.set_ahead(list(req.get("ids") or []), bool(req.get("hold")),
                                           list(req.get("first") or []),
                                           list(req.get("keep") or []))
                        resp = {"ok": True}
                    elif op == "cancel":
                        resolver.cancel(list(req.get("ids") or []))
                        resp = {"ok": True}
                    elif op == "drop":
                        resolver.drop(list(req.get("ids") or []))
                        resp = {"ok": True}
                    elif op == "ping":
                        resp = {"ok": True, "template": resolver.template is not None,
                                "loaded": resolver.loaded}
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
    p.add_argument("--cache", help="soubor pro hotové skladby (tmpfs, přežije restart služby)")
    a = p.parse_args()
    serve(a.socket, a.cache)
