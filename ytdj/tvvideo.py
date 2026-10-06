"""Klipy na telce — strana jukeboxu (POZADAVKY #71).

Na telce běží samostatný proces (ytdj/tv). Umí místo obrazovky „právě hraje“
pustit obraz klipu, bez zvuku a srovnaný s hudbou — ale jen když:

  * to někdo v prohlížeči povolil (vypínač pro všechny, výchozí vypnuto,
    pamatuje se přes restart),
  * hrající skladba SAMA je oficiální klip (stejné video, které hraje jako
    zvuk — písnička se za klip nikdy nezaměňuje, klip bývá jiná nahrávka),
  * obraz toho klipu je připravený (adresa proudu H.264 do 720p).

Tenhle modul drží vypínač, zjišťuje druh skladby (katalog YouTube Music) a
chystá adresu obrazu. Hudby se to nedotkne: nic tu nesahá na přehrávač ani na
jeho resolver — obraz se hledá vlastním, odloženým procesem yt-dlp s nejnižší
prioritou, a když se nenajde včas, telka prostě ukáže obrazovku jako dřív.

Procesu na telce se nic neposílá: co má hrát, si přečte ze stavu jukeboxu
(`tv.video` ve /api/status) a adresu proudu ze souboru v RAM (tmpfs, jen pro
téhož uživatele — adresy proudů se nikam nevypisují). Zpátky čteme jeho malý
stavový soubor: jestli telka klipy umí a co zrovna dělá, ať vypínač neslibuje,
co nejde.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from . import telemetry

log = logging.getLogger(__name__)

OMV = "MUSIC_VIDEO_TYPE_OMV"  # oficiální klip; ATV = písnička s obalem, UGC = nahrál někdo
# H.264 do 720p: jediné, co Pi 3 dekóduje hardwarem (720p25 ≈ 10 % jádra,
# změřeno 6. 10.); přímý proud https, ne HLS
FORMAT = "bestvideo[vcodec^=avc1][height<=720][protocol^=http]"
TV_STATUS = Path(os.environ.get("YTDJ_TV_STATUS", "/run/ytdj-tv/status.json"))
TV_STATUS_FRESH = 150.0  # s — starší hlášení = proces na telce neběží (píše nejdéle po 30 s)
RESOLVE_TIMEOUT = 120.0  # s — yt-dlp na Pi studeně ~19 s; s nejnižší prioritou déle
RESOLVE_FAIL_KEEP = 30 * 60.0  # s — nepovedené hledání obrazu se hned nezkouší znovu
EXPIRE_MARGIN = 10 * 60.0  # s — adresa, které zbývá míň, se hledá znovu
KIND_KEEP = 2000  # kolik druhů skladeb si pamatovat
# Obraz z toho, co už má resolver hudby: jeho hotový výsledek pro tutéž skladbu
# nese všechny formáty včetně videa (Pi 6. 10.: 11 z 11 položek mělo H.264 720p
# s přímou adresou). Čte se jen jeho soubor v RAM — resolveru se nic neposílá.
# Stejná pravidla důvěry jako u zvuku: stáří výsledku a platnost adresy s rezervou.
CACHE_MAX_AGE = 90 * 60.0  # s — jako MAX_AGE resolveru
# Vlastní yt-dlp jen jako záloha: až když skladba už chvíli hraje a v cache
# resolveru obraz pořád není (první klip na Pi tak čekal 28 s zbytečně).
FALLBACK_AFTER = 20.0  # s
TICK = 2.0
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _write(path: Path, data: dict, mode: int = 0o600) -> None:
    """Dočasný soubor a přejmenování (volat ve vlákně — disk)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, ensure_ascii=False))
    os.replace(tmp, path)


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def expires_at(url: str) -> float:
    """Kdy adrese proudu vyprší platnost (parametr expire), 0 = neznámo."""
    try:
        return float(parse_qs(urlsplit(url).query).get("expire", ["0"])[0])
    except (ValueError, IndexError):
        return 0.0


def stream_from_info(info: Any) -> dict | None:
    """Z výstupu yt-dlp (-j) jen to, co telka potřebuje — nebo None, když to
    není přímý proud H.264 do 720p (pak se klip nepouští)."""
    if not isinstance(info, dict):
        return None
    url = info.get("url")
    codec = str(info.get("vcodec") or "")
    height = info.get("height")
    if not isinstance(url, str) or not url.startswith("https://"):
        return None
    if not codec.startswith("avc1") or not isinstance(height, int) or not 0 < height <= 720:
        return None
    if "m3u8" in str(info.get("protocol") or ""):
        return None
    headers = info.get("http_headers") if isinstance(info.get("http_headers"), dict) else {}
    return {"id": str(info.get("id") or ""), "url": url,
            "headers": {str(k): str(v) for k, v in headers.items()
                        if str(k).lower() in ("user-agent", "referer", "origin")},
            "height": height, "fps": info.get("fps"), "format": str(info.get("format_id") or ""),
            "expire": expires_at(url), "resolved": time.time()}


def stream_from_cache_line(video_id: str, line: str, now: float | None = None
                           ) -> tuple[dict | None, str]:
    """Obraz z řádku cache resolveru hudby (`id<TAB>čas<TAB>JSON`): (proud, proč ne).

    Nejlepší přímý proud H.264 do 720p ze seznamu formátů téhož videa; výsledek
    starší než CACHE_MAX_AGE nebo s adresou těsně před vypršením se nebere.
    """
    now = time.time() if now is None else now
    parts = line.rstrip("\n").split("\t", 2)
    if len(parts) != 3 or parts[0] != video_id:
        return None, "missing"
    try:
        saved = float(parts[1])
        info = json.loads(parts[2])
    except ValueError:
        return None, "bad"
    if not -60 <= now - saved < CACHE_MAX_AGE:
        return None, "old"
    formats = info.get("formats") if isinstance(info, dict) else None
    best: dict | None = None
    for f in formats if isinstance(formats, list) else []:
        s = stream_from_info(f)
        if s is None or (s["expire"] and s["expire"] - now < EXPIRE_MARGIN):
            continue
        rank = (s["height"], float(f.get("tbr") or 0))
        if best is None or rank > best["_rank"]:
            best = {**s, "_rank": rank}
    if best is None:
        return None, "no_avc1_720"
    best.pop("_rank")
    best["id"] = video_id
    return best, ""


class TvVideo:
    def __init__(self, cfg: Any, catalog: Any, player: Any, state_file: Path, stream_dir: Path,
                 on_change: Callable[[], None] | None = None, tv_status: Path = TV_STATUS,
                 yt_dlp_args: Callable[[Any], list[str]] | None = None,
                 resolver_cache: Path | None = None) -> None:
        # hotové výsledky resolveru hudby (tmpfs) — odtud se bere obraz nejdřív
        self.resolver_cache = resolver_cache
        self._cur: tuple[str | None, float] = (None, 0.0)  # co hraje a odkdy
        self._cache_miss: dict[str, str] = {}  # videoId → proč v cache obraz není
        self.cfg = cfg
        self.catalog = catalog
        self.player = player
        self.state_file = state_file
        self.stream_dir = stream_dir
        self.on_change = on_change or (lambda: None)
        self.tv_status = tv_status
        self._yt_dlp_args = yt_dlp_args
        self.enabled = False  # výchozí vypnuto
        self.by = ""
        self.at = 0.0
        self.loaded = False
        self.report: dict = {}  # co o sobě řekl proces na telce
        self._report_at = 0.0
        self.kinds: dict[str, str] = {}  # videoId → druh (OMV / ATV / UGC / "")
        self._streams: dict[str, dict] = {}  # videoId → připravený proud
        self._failed: dict[str, float] = {}  # videoId → kdy se hledání nepovedlo
        self._resolving: str | None = None
        self.video: str | None = None  # hrající skladba je klip a obraz je připravený
        self.pending = False  # hrající skladba je klip, obraz se teprve chystá
        self._task: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None

    # ---- vypínač ----

    async def load(self) -> None:
        """Stav vypínače z disku (ve vlákně); rozbitý soubor = vypnuto."""
        data = await asyncio.to_thread(_read, self.state_file)
        if data:
            self.enabled = data.get("on") is True
            self.by = str(data.get("by") or "")[:40]
            with contextlib.suppress(TypeError, ValueError):
                self.at = float(data.get("at") or 0)
        self.loaded = True

    async def set(self, on: bool, who: str = "", client: dict | None = None) -> dict:
        """Zapnout / vypnout — smí kdokoli, platí pro celý jukebox a pamatuje se."""
        on = bool(on)
        changed = on != self.enabled
        self.enabled, self.by, self.at = on, (who or "")[:40], time.time()
        if not on:
            self.video, self.pending = None, False
        telemetry.event("tv.video_switch", on=on, who=self.by or None, changed=changed,
                        can=self.can()[0], **(client or {}))
        try:
            # na kartu ve vlákně — smyčka nečeká
            await asyncio.to_thread(_write, self.state_file,
                                    {"on": on, "by": self.by, "at": self.at}, 0o644)
        except OSError:
            log.warning("stav klipů na telce se nepodařilo uložit", exc_info=True)
        self.on_change()
        return self.public()

    # ---- co umí telka ----

    def can(self) -> tuple[bool, str]:
        """Jde to teď vůbec? (ano/ne, proč ne — česky pro web)"""
        r = self.report
        if not r or time.time() - self._report_at > TV_STATUS_FRESH:
            return False, "Obrazovka na telce neběží."
        if r.get("can") is not True:
            return False, str(r.get("why") or "Telka klipy neumí.")[:160]
        return True, ""

    def _read_report(self) -> tuple[dict, float]:
        try:
            at = self.tv_status.stat().st_mtime
        except OSError:
            return {}, 0.0
        return _read(self.tv_status) or {}, at

    def public(self) -> dict:
        """Do stavu pro web i telku (jen paměť)."""
        can, why = self.can()
        r = self.report if can else {}
        return {
            "on": self.enabled, "by": self.by, "can": can, "why": why,
            # co telka zrovna dělá: "video" | "screen"; proč ne video (ochrana…)
            "mode": str(r.get("mode") or "screen"), "blocked": str(r.get("blocked") or ""),
            "video": self.video if self.enabled and can else None,
            "pending": bool(self.pending and self.enabled and can),
            "xruns": getattr(getattr(self.player, "sampler", None), "xruns", None),
        }

    # ---- druh skladby a obraz ----

    async def kind(self, video_id: str) -> str:
        """Druh skladby podle katalogu YouTube Music (jedno volání na skladbu,
        mimo smyčku); "" = nezjištěno (pak se klip nepouští)."""
        if video_id in self.kinds:
            return self.kinds[video_id]
        kind = ""
        try:
            kind = str(await self.catalog.video_type(video_id) or "")
        except Exception as exc:
            log.debug("druh skladby %s se nepodařilo zjistit: %s", video_id, exc)
            return ""  # příště znovu
        if len(self.kinds) >= KIND_KEEP:
            self.kinds.pop(next(iter(self.kinds)))
        self.kinds[video_id] = kind
        return kind

    def stream_file(self, video_id: str) -> Path:
        return self.stream_dir / f"{video_id}.json"

    def _ready(self, video_id: str) -> bool:
        s = self._streams.get(video_id)
        if not s:
            return False
        if s.get("expire") and s["expire"] - time.time() < EXPIRE_MARGIN:
            self._streams.pop(video_id, None)
            return False
        return True

    def _args(self, video_id: str) -> list[str]:
        if self._yt_dlp_args is None:
            from .diagnose import yt_dlp_args

            self._yt_dlp_args = yt_dlp_args
        args = self._yt_dlp_args(self.cfg) + [
            "-f", FORMAT, "-j", "--no-playlist", "--no-warnings",
            f"https://music.youtube.com/watch?v={video_id}"]
        # nejnižší priorita CPU i disku: hudba a její resolver mají vždy přednost
        if shutil.which("ionice"):
            args = ["ionice", "-c3", *args]
        if shutil.which("nice"):
            args = ["nice", "-n", "19", *args]
        return args

    async def _resolve(self, video_id: str) -> None:
        """Najde adresu obrazu vlastním procesem yt-dlp (ne resolverem hudby)."""
        t0 = time.monotonic()
        self._resolving = video_id
        stream, err = None, ""
        try:
            env = self.cfg.child_env() if hasattr(self.cfg, "child_env") else None
            self._proc = await asyncio.create_subprocess_exec(
                *self._args(video_id), env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(self._proc.communicate(), RESOLVE_TIMEOUT)
            line = out.decode(errors="replace").strip().splitlines()
            stream = stream_from_info(json.loads(line[-1])) if line else None
            if stream is None:
                err = "no_avc1_720"
        except asyncio.TimeoutError:
            err = "timeout"
            with contextlib.suppress(ProcessLookupError):
                self._proc.kill()
        except asyncio.CancelledError:
            with contextlib.suppress(ProcessLookupError, AttributeError):
                self._proc.kill()
            raise
        except (OSError, ValueError) as exc:
            err = type(exc).__name__
        finally:
            self._resolving, self._proc = None, None
        if stream is not None:
            stream["id"] = video_id
            try:
                await asyncio.to_thread(_write, self.stream_file(video_id), stream)
                self._streams[video_id] = stream
            except OSError as exc:
                stream, err = None, type(exc).__name__
        if stream is None:
            self._failed[video_id] = time.monotonic()
        # adresa proudu do logu nepatří — jen že se našla a jak dlouho to trvalo
        telemetry.event("tv.video_resolve", video_id=video_id, ok=stream is not None,
                        source="ytdlp", cache=self._cache_miss.get(video_id),
                        error=err or None, took_ms=int((time.monotonic() - t0) * 1000),
                        height=stream.get("height") if stream else None,
                        format=stream.get("format") if stream else None)

    def _cache_lookup(self, video_id: str) -> tuple[dict | None, str]:
        """Ve vlákně: najde skladbu v souboru resolveru a vytáhne z ní obraz."""
        if self.resolver_cache is None:
            return None, "no_cache"
        try:
            with open(self.resolver_cache, encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith(video_id + "\t"):
                        return stream_from_cache_line(video_id, line)
        except FileNotFoundError:
            return None, "missing"  # resolver ještě nic neuložil
        except (OSError, UnicodeDecodeError):
            return None, "unreadable"
        return None, "missing"

    async def _from_cache(self, video_id: str) -> bool:
        """Obraz z hotového výsledku resolveru hudby — bez dalšího yt-dlp."""
        t0 = time.monotonic()
        stream, why = await asyncio.to_thread(self._cache_lookup, video_id)
        if stream is None:
            self._cache_miss[video_id] = why
            if len(self._cache_miss) > 200:
                self._cache_miss.clear()
            return False
        try:
            await asyncio.to_thread(_write, self.stream_file(video_id), stream)
        except OSError:
            return False
        self._streams[video_id] = stream
        self._cache_miss.pop(video_id, None)
        telemetry.event("tv.video_resolve", video_id=video_id, ok=True, source="cache",
                        took_ms=int((time.monotonic() - t0) * 1000), height=stream.get("height"),
                        format=stream.get("format"))
        return True

    async def _wanted(self) -> tuple[str | None, list[str]]:
        """(co hraje, [co hraje a co bude dál]) — jen platná id."""
        st = await self.player.status()
        cur = st.current.id if st.current is not None else None
        ids = [cur] if cur else []
        ids += [t.id for t in st.queue[:1]]
        return cur, [v for v in ids if v and VIDEO_ID.match(v)]

    async def tick(self) -> None:
        """Jeden krok: stav telky, druh hrající a další skladby, obraz dopředu."""
        if time.monotonic() - getattr(self, "_report_mono", -1e9) >= 5.0:
            self._report_mono = time.monotonic()
            report, at = await asyncio.to_thread(self._read_report)
            if (report, at) != (self.report, self._report_at):
                self.report, self._report_at = report, at
                self.on_change()
        if not self.enabled or not self.can()[0]:
            if self.video or self.pending:
                self.video, self.pending = None, False
                self.on_change()
            return
        try:
            cur, ids = await self._wanted()
            self._player_down = False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Přehrávač ještě nenaběhl, restartuje se nebo se právě vypíná — pro
            # tenhle krok běžný stav, ne chyba: potichu počkat (Pi 6. 10. 21:19:
            # tři výpisy chyb hned po startu služby).
            if not getattr(self, "_player_down", False):
                self._player_down = True
                log.debug("klipy na telce: přehrávač zatím neodpovídá (%s)", exc)
            if self.video or self.pending:
                self.video, self.pending = None, False
                self.on_change()
            return
        if cur != self._cur[0]:
            self._cur = (cur, time.monotonic())
        video, pending = None, False
        for vid in ids:
            if await self.kind(vid) != OMV:
                continue  # písnička (ATV) ani cizí nahrávka (UGC) se za obraz nemění
            if self._ready(vid):
                if vid == cur:
                    video = vid
                continue
            if self.report.get("blocked"):
                continue  # telka klipy zrovna nesmí (ochrana) — nic se nechystá
            # nejdřív z toho, co už resolver hudby pro tuhle skladbu má
            if await self._from_cache(vid):
                if vid == cur:
                    video = vid
                continue
            if vid != cur:
                continue  # další skladbu nechystáme vlastním yt-dlp: resolver ji teprve řeší
            pending = True
            failed = self._failed.get(vid)
            if failed is not None and time.monotonic() - failed < RESOLVE_FAIL_KEEP:
                pending = False  # nenašlo se — telka ukáže obrazovku
                continue
            if time.monotonic() - self._cur[1] < FALLBACK_AFTER:
                continue  # zvuk se možná ještě řeší; jeho výsledek přinese i obraz
            if self._resolving is None and not self.report.get("blocked"):
                # na pozadí, jeden po druhém; hudba ani tenhle krok na to nečekají
                self._resolving = vid
                task = asyncio.create_task(self._resolve(vid), name="ytdj-tv-video-resolve")
                task.add_done_callback(lambda _t: None)
                self._resolve_task = task
        if (video, pending) != (self.video, self.pending):
            self.video, self.pending = video, pending
            self.on_change()

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("klipy na telce: krok selhal")
            await asyncio.sleep(TICK)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="ytdj-tv-video")

    async def stop(self) -> None:
        for task in (self._task, getattr(self, "_resolve_task", None)):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        # adresy proudů po sobě nenechávat
        with contextlib.suppress(OSError):
            await asyncio.to_thread(shutil.rmtree, self.stream_dir, True)
