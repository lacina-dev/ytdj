"""mpv as the player, controlled via JSON IPC on a unix socket.

mpv resolves the stream URL itself via ytdl_hook -> yt-dlp. Three things
without which this won't work — and the documentation is silent about them:
  * `secretstorage` must be in yt-dlp's environment, otherwise it can't
    decrypt Chrome cookies
  * `--js-runtimes` + `--remote-components=ejs:github`, otherwise signature
    resolution fails and Premium formats (774/141) are never offered at all
  * node must be on the PATH of the mpv subprocess (see Config.child_env)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from contextlib import suppress
from typing import Any, Callable
from pathlib import Path

from .. import telemetry
from ..config import MPV_SOCKET, Config, save_values
from ..telemetry_sampler import SystemSampler
from . import ytdl_cache
from ..music.catalog import Track
from .base import EventHandler, Player, PlayerEvent, PlayerStatus

log = logging.getLogger(__name__)

WATCH_URL = "https://music.youtube.com/watch?v={}"
PREFETCH_AHEAD = 4  # kolik skladeb z fronty mít vyřešených dopředu (~8 s každá na Pi 3)
RESOLVER_SOCKET = MPV_SOCKET.parent / "ytdl-resolver.sock"

# mpv nad 130 stejně nepustí a ručně zapsaná hodnota v configu by ho jinak
# odmítla nastartovat
VOLUME_MAX = 130
SAVE_DELAY = 2.0  # sekund klidu, než se hlasitost zapíše do configu
QUALITY_DELAY = 12.0  # než se klouzavý průměr bitrate ustálí
# 774 má jmenovitě 251 kb/s, 141 pak 258; běžné formáty končí na 130. Práh
# uprostřed rozliší Premium i při rozkolísaném průměru.
PREMIUM_KBPS = 180


def _clamp_volume(volume: int) -> int:
    return max(0, min(VOLUME_MAX, int(volume)))


REQUEST_MAX_AGE = 600.0  # s — starší "chci další" už se ke startu nepřičítá
RESOLVE_MATCH_AGE = 1800.0  # s — jak starý dotaz resolveru ještě patří ke startu
STALL_MIN_MS = 200  # kratší zaváhání core-idle nejsou výpadek


def parse_resolver_line(text: str) -> tuple[str, dict[str, Any]] | None:
    """`EVENT {"kind": …}` z resolveru → (kind, pole); lidský řádek → None."""
    if not text.startswith("EVENT "):
        return None
    try:
        data = json.loads(text[6:])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    kind = data.pop("kind", None)
    if not isinstance(kind, str) or not kind:
        return None
    return kind, data


def _ms(a: float | None, b: float | None) -> int | None:
    return int((b - a) * 1000) if a is not None and b is not None else None


class MpvPlayer(Player):
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.proc: asyncio.subprocess.Process | None = None
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None

        # Set when mpv goes away on its own. Nobody can be served after that —
        # under systemd it is better to end the process and let it be started
        # again than to keep a web UI that controls nothing.
        self.died = asyncio.Event()
        self._stopping = False

        self._req_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._handlers: list[EventHandler] = []
        self._reader_task: asyncio.Task | None = None
        self._dispatch_task: asyncio.Task | None = None
        # (kind, playlist_entry_id, detail) — the track is looked up only in
        # the dispatch loop, because at the moment of the event the mapping
        # from the loadfile response may not have been recorded yet
        self._events: asyncio.Queue[tuple[str, int, str]] = asyncio.Queue()

        # mpv playlist <-> our Tracks, keyed by videoId
        self._tracks: dict[str, Track] = {}
        self._order: list[str] = []  # order in which we enqueued
        # playlist_entry_id (from mpv) -> videoId. Thanks to this we never
        # have to query mpv for anything while handling events — so no
        # deadlock can arise where a handler waits for a reply that the very
        # same loop is supposed to read.
        self._entries: dict[int, str] = {}
        self._current_id: str | None = None
        self._pos = 0
        self._count = 0
        self._time_pos = 0.0  # průběžná pozice — pro detekci useknuté skladby
        self._replacing = False  # další konec skladby způsobila aplikace, ne posluchač
        self._prefetch_task: asyncio.Task | None = None
        self._prefetch_wake = asyncio.Event()
        self._resolver: asyncio.subprocess.Process | None = None
        self._resolver_task: asyncio.Task | None = None
        # ytdj nastaví: když běží Codex, dopředu se nic neřeší (paměť)
        self.busy_check: Callable[[], bool] | None = None
        self._paused = False
        self._volume = _clamp_volume(cfg.volume)
        # Zápis hlasitosti do configu se odkládá: tažení slideru i držené "+"
        # v REPL by jinak přepisovaly soubor několikrát za vteřinu.
        self._volume_save: asyncio.Task | None = None
        self._volume_pending: int | None = None
        self._quality = ""
        self._quality_task: asyncio.Task | None = None

        # ---- provozní log (ytdj.telemetry) ----
        # poslední "chci další skladbu": proč a kdy — pro čekání do zvuku
        self._req: dict[str, Any] | None = None
        # právě načítaná / hrající skladba: časy start-file → file-loaded → zvuk
        self._load: dict[str, Any] | None = None
        self._core_idle = True
        # co resolver hlásí (EVENT _state): vyřešené, na čem dělá, na co čeká mpv
        self._res_ready: set[str] = set()
        self._res_busy: str | None = None
        self._res_urgent: list[str] = []
        self._res_gets: dict[str, tuple[float, dict]] = {}  # vid → (kdy, resolver.get)
        self._enqueued_at: dict[str, float] = {}  # vid → kdy přišel do fronty
        self._requested_at: dict[str, float] = {}  # vid → kdy přišel přes enqueue_next
        self._resolver_started = 0.0
        self._resolver_restarts = 0
        self.sampler: SystemSampler | None = None

    # ---------- lifecycle ----------

    def _args(self) -> list[str]:
        args = [
            "mpv",
            "--idle=yes",
            "--no-video",
            "--no-terminal",
            "--vid=no",
            f"--input-ipc-server={MPV_SOCKET}",
            f"--ytdl-format={self.cfg.ytdl_format}",
            f"--script-opts=ytdl_hook-ytdl_path={self._ytdl_shim()}",
            "--prefetch-playlist=yes",  # pre-resolves the next track's URL -> no gap
            "--gapless-audio=weak",
            "--cache=yes",
            # Vteřina zvuku v zásobě: na slabém stroji, kde vedle hraje yt-dlp
            # s node, by 0,2 s (výchozí) občas nestačilo a v repráku by lupnulo.
            "--audio-buffer=1",
            # Začít hrát, až jsou v cache aspoň dvě vteřiny proudu. Bez toho
            # mpv na Pi rozjelo skladbu s prvními bajty ze sítě a za zlomek
            # vteřiny mu data došla — PipeWire hlásil výpadky přesně při
            # startu každé skladby a v repráku to lupalo.
            "--cache-pause-initial=yes",
            "--cache-pause-wait=2",
            "--keep-open=no",
            # Když YouTube uprostřed skladby zavře spojení (rotace CDN, síť),
            # ffmpeg to bez tohohle vezme jako konec souboru — mpv ohlásí eof
            # a skočí na další skladbu v půlce té současné. S reconnectem se
            # stream chytí tam, kde vypadl.
            "--stream-lavf-o=reconnect=1,reconnect_streamed=1,reconnect_delay_max=10",
            # Nastavuje se rovnou na příkazové řádce, ne až přes IPC — jinak by
            # první skladba po startu stihla zaznít v původní hlasitosti.
            f"--volume={self._volume}",
        ]
        raw = []
        # An exported jar wins: it is the only source that survives without a
        # desktop session, because nothing has to be decrypted with a key from
        # the keyring.
        if self.cfg.cookies_file:
            raw.append(f"cookies={self.cfg.cookies_file}")
        elif self.cfg.cookies_browser and self.cfg.cookies_browser != "none":
            raw.append(f"cookies-from-browser={self.cfg.cookies_browser}")
        if self.cfg.js_runtimes:
            raw.append(f"js-runtimes={self.cfg.js_runtimes}")
        if self.cfg.remote_components:
            raw.append(f"remote-components={self.cfg.remote_components}")
        if extractor_args := self.cfg.extractor_args():
            raw.append(f"extractor-args={extractor_args}")
        # each option separately via -append, so commas need no escaping
        args += [f"--ytdl-raw-options-append={opt}" for opt in raw]
        args += list(self.cfg.mpv_extra_args)
        return args

    async def start(self) -> None:
        with suppress(FileNotFoundError):
            os.unlink(MPV_SOCKET)
        MPV_SOCKET.parent.mkdir(parents=True, exist_ok=True)

        env = self.cfg.child_env()
        env[ytdl_cache.ENV_REAL] = self.cfg.yt_dlp_path
        env[ytdl_cache.ENV_SOCKET] = str(RESOLVER_SOCKET)
        env.update(telemetry.child_env())
        t0 = time.monotonic()
        self.proc = await asyncio.create_subprocess_exec(
            *self._args(),
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        # Při bootu Pi 3 startuje všechno naráz a mpv socket občas nestihl
        # do 5 s — ytdj pak spadl a systemd ho pouštěl znovu.
        for _ in range(400):  # ~20 s for the socket to appear
            if MPV_SOCKET.exists():
                try:
                    self.reader, self.writer = await asyncio.open_unix_connection(
                        str(MPV_SOCKET)
                    )
                    break
                except (ConnectionRefusedError, FileNotFoundError):
                    pass
            await asyncio.sleep(0.05)
        else:
            telemetry.event("player.fail", error="socket mpv nevznikl")
            raise RuntimeError(f"mpv nenastartoval (socket {MPV_SOCKET} nevznikl)")
        telemetry.event("player.start", took_ms=_ms(t0, time.monotonic()),
                        volume=self._volume, format=self.cfg.ytdl_format)

        self._reader_task = asyncio.create_task(self._read_loop())
        # Resolver až po mpv: jeho start (import yt-dlp) bere Pi 3 desítky
        # vteřin CPU. Než naběhne, jde mpv přes yt-dlp postaru.
        await self._start_resolver()
        self._dispatch_task = asyncio.create_task(self._dispatch_loop())

        for i, prop in enumerate(
            ("playlist-pos", "playlist-count", "pause", "volume", "time-pos", "core-idle"), 1
        ):
            await self._send({"command": ["observe_property", i, prop]}, wait=False)

        self.sampler = SystemSampler(self._telemetry_context, self._telemetry_pids)
        self.sampler.start()

    def expect_exit(self) -> None:
        """Od téhle chvíle je odchod mpv náš záměr, ne porucha.

        Volá se při signálu k ukončení: systemd posílá SIGTERM celé skupině,
        takže mpv může zmizet dřív, než se dostaneme ke stop() — a bez tohohle
        by se to do logu zapsalo jako chyba.
        """
        self._stopping = True

    async def stop(self) -> None:
        self._stopping = True
        self._flush_volume()
        if self.sampler:
            with suppress(Exception):
                await self.sampler.stop()
        for task in (self._reader_task, self._dispatch_task):
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        if self.writer:
            with suppress(Exception):
                self.writer.close()
                await self.writer.wait_closed()
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.proc.wait(), timeout=3)
        with suppress(FileNotFoundError):
            os.unlink(MPV_SOCKET)
        for task in (self._resolver_task, self._prefetch_task):
            if task:
                task.cancel()
        if self._resolver and self._resolver.returncode is None:
            self._resolver.terminate()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._resolver.wait(), timeout=3)

    # ---------- IPC ----------

    async def _send(self, payload: dict, wait: bool = True):
        if not self.writer:
            raise RuntimeError("mpv neběží")
        self._req_id += 1
        rid = self._req_id
        payload = dict(payload, request_id=rid)
        line = (json.dumps(payload) + "\n").encode()

        fut: asyncio.Future | None = None
        if wait:
            fut = asyncio.get_running_loop().create_future()
            self._pending[rid] = fut

        self.writer.write(line)
        await self.writer.drain()

        if fut is None:
            return None
        try:
            return await asyncio.wait_for(fut, timeout=5)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            return None

    async def _command(self, *cmd, wait: bool = True):
        return await self._send({"command": list(cmd)}, wait=wait)

    async def _get(self, prop: str, default=None):
        res = await self._command("get_property", prop)
        if isinstance(res, dict) and res.get("error") == "success":
            return res.get("data", default)
        return default

    async def _read_loop(self) -> None:
        assert self.reader
        while True:
            raw = await self.reader.readline()
            if not raw:  # socket EOF — mpv is gone
                if not self._stopping:
                    log.error("mpv skončil (kód %s)", self.proc.returncode if self.proc else "?")
                    telemetry.event("player.died",
                                    code=self.proc.returncode if self.proc else None,
                                    video_id=self._current_id)
                    self.died.set()
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if "request_id" in msg and msg.get("event") is None:
                fut = self._pending.pop(msg["request_id"], None)
                if fut and not fut.done():
                    fut.set_result(msg)
                continue

            with suppress(Exception):
                self._handle_event(msg)

    def _handle_event(self, msg: dict) -> None:
        """Event processing. Must not call IPC or await anything — it runs
        inside the read loop, so it would be waiting for a reply that the
        very same loop would have to read."""
        event = msg.get("event")

        if event == "property-change":
            name, data = msg.get("name"), msg.get("data")
            if name == "playlist-pos" and isinstance(data, int):
                self._pos = data
            elif name == "playlist-count" and isinstance(data, int):
                self._count = data
            elif name == "pause" and isinstance(data, bool):
                self._paused = data
                self._t_pause(data)
            elif name == "volume" and isinstance(data, (int, float)):
                self._volume = int(data)
            elif name == "time-pos" and isinstance(data, (int, float)):
                self._time_pos = float(data)
            elif name == "core-idle" and isinstance(data, bool):
                self._core_idle = data
                self._t_core_idle(data)
            return

        if event == "start-file":
            self._t_start_file(msg.get("playlist_entry_id", -1))
            self._events.put_nowait(("start", msg.get("playlist_entry_id", -1), ""))
            return

        if event == "file-loaded":
            if self._load and self._load.get("t_loaded") is None:
                self._load["t_loaded"] = time.monotonic()
            return

        if event == "end-file":
            reason = msg.get("reason", "")
            # reason distinguishes played-to-end vs. skipped — that is our
            # implicit signal of whether the track was a good pick
            kind = {
                "eof": "finished",
                "stop": "skipped",
                "quit": "skipped",
                "error": "error",
                "redirect": "skipped",
            }.get(reason, "skipped")
            if kind == "skipped" and self._replacing:
                # odsunula ji aplikace, ne posluchač — nepočítat jako "nelíbí"
                kind = "replaced"
            self._replacing = False
            premature = self._note_premature_end(kind, msg.get("playlist_entry_id", -1))
            self._t_end_file(kind, msg, premature)
            self._time_pos = 0.0
            self._events.put_nowait((kind, msg.get("playlist_entry_id", -1), reason))
            return

        if event == "idle":
            self._events.put_nowait(("idle", -1, ""))

    def _note_premature_end(self, kind: str, entry_id: int) -> bool:
        """Skladba, která 'dohrála' dávno před koncem, je useknutý stream.

        Když spojení umře i přes reconnect, ffmpeg ohlásí konec souboru a mpv
        čistý eof — od uživatelského dohrání se to nijak neliší. Aspoň se to
        nahlas zapíše do logu, ať se dá 'občas se to usekne' dohledat: která
        skladba, v kolikáté vteřině z kolika.
        """
        if kind != "finished":
            return False
        vid = self._entries.get(entry_id) or self._current_id
        track = self._tracks.get(vid or "")
        played = self._time_pos
        if not track or not track.duration or not played:
            return False
        if played + 15 < track.duration * 0.9:
            log.warning(
                "skladba %s skončila předčasně (%d s z %d) — nejspíš výpadek streamu",
                track.label(), int(played), track.duration,
            )
            return True
        return False

    # ---------- provozní log: životní cyklus skladby ----------
    #
    # Volá se z _handle_event (nesmí nic čekat) a z ovládání. Časy:
    #   požadavek (skip / konec / první zařazení) → start-file → file-loaded
    #   (yt-dlp/resolver + otevření proudu) → core-idle=false (zvuk teče).

    def _t_request(self, why: str) -> None:
        """Někdo chce další skladbu — od teď se měří čekání na zvuk."""
        try:
            now = time.monotonic()
            if self._req and self._req.get("why") != "eof" and now - self._req["t0"] < 2:
                return  # jedno přeskočení = jeden požadavek (skip → end-file stop)
            nxt = None
            pos = self._pos
            if 0 <= pos + 1 < len(self._order):
                nxt = self._order[pos + 1]
            self._req = {"why": why, "t0": now, "next": nxt}
            telemetry.event(
                "track.request", why=why, from_id=self._current_id,
                played_s=round(self._time_pos, 1) if self._current_id else None,
                next_id=nxt,
                next_ready=nxt in self._res_ready if nxt else None,
                next_resolving=(nxt == self._res_busy) if nxt else None,
                queue=self.queue_depth,
            )
        except Exception:
            pass

    def _t_start_file(self, entry_id: int) -> None:
        now = time.monotonic()
        req = self._req
        if req and now - req["t0"] > REQUEST_MAX_AGE:
            req = None
        self._req = None
        self._load = {
            "entry": entry_id, "vid": self._entries.get(entry_id), "t_start": now,
            "t_loaded": None, "t_play": None, "req": req, "paused": self._paused,
            "stalls": 0, "stall_ms": 0, "stall_t0": None,
        }

    def _t_pause(self, paused: bool) -> None:
        load = self._load
        if not load:
            return
        if paused:
            load["stall_t0"] = None  # pauza není výpadek
            if load["t_play"] is None:
                load["paused"] = True

    def _t_core_idle(self, idle: bool) -> None:
        load = self._load
        if not load:
            return
        now = time.monotonic()
        if not idle:
            if load["t_play"] is None:
                load["t_play"] = now
                self._t_emit_start(load)
            elif load["stall_t0"] is not None:
                ms = _ms(load["stall_t0"], now) or 0
                load["stall_t0"] = None
                if ms >= STALL_MIN_MS:
                    load["stalls"] += 1
                    load["stall_ms"] += ms
                    telemetry.event("track.stall", video_id=load["vid"], stall_ms=ms,
                                    pos_s=round(self._time_pos, 1))
        elif load["t_play"] is not None and not self._paused:
            load["stall_t0"] = now

    def _t_emit_start(self, load: dict[str, Any]) -> None:
        try:
            vid = load["vid"] or self._current_id
            load["vid"] = vid
            track = self._tracks.get(vid or "")
            req = load["req"]
            t_play = load["t_play"]
            fields: dict[str, Any] = {
                "video_id": vid,
                "artist": track.artist if track else None,
                "title": track.title if track else None,
                "duration_s": track.duration if track else None,
                "why": req["why"] if req else "auto",
                # od požadavku (skip, konec, zařazení) do zvuku
                "wait_ms": _ms(req["t0"] if req else load["t_start"], t_play),
                "load_ms": _ms(load["t_start"], load["t_loaded"]),
                "buffer_ms": _ms(load["t_loaded"] or load["t_start"], t_play),
                "queue": self.queue_depth,
            }
            got = self._res_gets.pop(vid or "", None)
            if got and t_play - got[0] < RESOLVE_MATCH_AGE:
                g = got[1]
                fields["source"] = "prefetched" if g.get("hit") else "on_demand"
                fields["resolve_ms"] = g.get("wait_ms")
                if g.get("how") not in (None, "hit"):
                    fields["resolve_how"] = g.get("how")
                if g.get("blocked_by"):
                    fields["resolve_blocked"] = True
                if load["t_start"] - got[0] > 3:
                    fields["mpv_prefetch"] = True  # vyřešeno už mpv prefetchem
            else:
                fields["source"] = "direct" if self._resolver is None else "unknown"
            if vid in self._requested_at:
                fields["requested"] = True
                fields["since_request_ms"] = _ms(self._requested_at.pop(vid), t_play)
            enq = self._enqueued_at.pop(vid or "", None)
            if enq is not None:
                fields["queued_s"] = int(t_play - enq)
            if req and req.get("next") and req["next"] != vid:
                # mpv hraje něco jiného, než ukazuje naše fronta (a web/panel)
                fields["unexpected"] = True
                fields["expected_id"] = req["next"]
            if load["paused"]:
                fields["paused"] = True  # čekání zahrnuje pauzu — do latencí nepočítat
            telemetry.event("track.start", **fields)
        except Exception:
            pass

    def _t_end_file(self, kind: str, msg: dict, premature: bool) -> None:
        try:
            load = self._load
            entry = msg.get("playlist_entry_id", -1)
            vid = self._entries.get(entry) or (load or {}).get("vid") or self._current_id
            track = self._tracks.get(vid or "")
            fields: dict[str, Any] = {
                "video_id": vid,
                "reason": kind,
                "mpv_reason": msg.get("reason"),
                "played_s": round(self._time_pos, 1),
                "duration_s": track.duration if track else None,
                "started": bool(load and load["t_play"] is not None),
            }
            if msg.get("file_error"):
                fields["error"] = telemetry.clip(msg.get("file_error"), 200)
            if premature:
                fields["premature"] = True
            if load and load["stalls"]:
                fields["stalls"] = load["stalls"]
                fields["stall_ms"] = load["stall_ms"]
            if load and not fields["started"]:
                fields["waited_ms"] = _ms(load["t_start"], time.monotonic())
            if self._quality:
                fields["quality"] = self._quality
            self._load = None
            telemetry.event("track.end", **fields)
            if kind in ("finished", "error"):
                self._t_request("eof" if kind == "finished" else "error")
        except Exception:
            pass

    def _telemetry_context(self) -> dict[str, Any]:
        """Co se právě děje — do sys.sample a audio.xrun."""
        ctx: dict[str, Any] = {
            "track": self._current_id,
            "paused": self._paused,
            "buffering": bool(self._core_idle and not self._paused and self._load),
            "queue": self.queue_depth,
            "resolving": self._res_busy,
            "resolver_wait": len(self._res_urgent),  # na kolik skladeb čeká mpv
            "ready": len(self._res_ready),
        }
        if self._load and self._load["t_play"] is None:
            ctx["loading"] = True
        with suppress(Exception):
            ctx["codex"] = bool(self.busy_check and self.busy_check())
        if self._current_id and not self._paused:
            ctx["pos_s"] = round(self._time_pos, 1)
        return ctx

    def _telemetry_pids(self) -> dict[str, int]:
        pids = {}
        if self.proc and self.proc.returncode is None:
            pids["mpv"] = self.proc.pid
        if self._resolver and self._resolver.returncode is None:
            pids["resolver"] = self._resolver.pid
        return pids

    def _on_resolver_event(self, kind: str, fields: dict[str, Any]) -> None:
        if kind == "_state":
            self._res_ready = set(fields.get("ready") or ())
            self._res_busy = fields.get("busy")
            self._res_urgent = list(fields.get("urgent") or ())
            return
        if kind.startswith("_"):
            return
        if kind == "resolver.get" and fields.get("video_id"):
            self._res_gets[fields["video_id"]] = (time.monotonic(), fields)
            if len(self._res_gets) > 50:  # mpv prefetch bez startu — neudržovat věčně
                for old in sorted(self._res_gets, key=lambda v: self._res_gets[v][0])[:25]:
                    del self._res_gets[old]
        if kind == "resolver.ready":
            fields["startup_ms"] = _ms(self._resolver_started, time.monotonic())
            fields["restarts"] = self._resolver_restarts
        telemetry.event(kind, **fields)

    async def _dispatch_loop(self) -> None:
        """Handlers run outside the read loop (so they may call IPC), but in
        the order the events arrived."""
        while True:
            kind, entry_id, detail = await self._events.get()

            track = None
            if kind != "idle":
                vid = self._entries.get(entry_id)
                if vid is None and kind == "start":
                    # the mapping has not arrived yet — ask mpv directly
                    vid = await self._current_video_id()
                    if vid:
                        self._entries[entry_id] = vid
                if kind == "start":
                    self._current_id = vid
                    if self._load and self._load["entry"] == entry_id and not self._load["vid"]:
                        self._load["vid"] = vid
                    self._measure_quality()
                    self._schedule_prefetch()
                elif vid is None:
                    vid = self._current_id
                track = self._tracks.get(vid or "")

            ev = PlayerEvent(kind, track, detail=detail)
            for handler in self._handlers:
                try:
                    await handler(ev)
                except Exception:
                    log.exception("handler události selhal")

    def _measure_quality(self) -> None:
        """Zjistí, co reálně hraje.

        Jestli prošel Premium formát, se nedá poznat z nastavení — yt-dlp
        nabídne, co dostane, a mpv vezme nejlepší z toho. Bez tohohle je pád
        na 128 kb/s němý.
        """
        self._quality = ""
        if self._quality_task and not self._quality_task.done():
            self._quality_task.cancel()
        self._quality_task = asyncio.create_task(self._read_quality())

    async def _read_quality(self) -> None:
        # mpv hlásí klouzavý průměr, ne jmenovitou hodnotu formátu: po třech
        # vteřinách ukazuje 60 kb/s i u proudu, který má 250. Ustálí se kolem
        # desáté vteřiny — dřív se měřit nevyplatí.
        await asyncio.sleep(QUALITY_DELAY)
        codec = await self._get("audio-codec-name")
        bitrate = await self._get("audio-bitrate")
        if not bitrate:
            await self._log_playing()
            return
        kbps = int(bitrate) // 1000
        premium = " (Premium)" if kbps >= PREMIUM_KBPS else ""
        self._quality = f"{codec or 'audio'} {kbps} kb/s{premium}"
        log.info("kvalita: %s", self._quality)
        telemetry.event("track.quality", video_id=self._current_id, codec=codec,
                        kbps=kbps, premium=kbps >= PREMIUM_KBPS)
        await self._log_playing()

    async def _log_playing(self) -> None:
        """Zapíše, co mpv doopravdy přehrává — důkaz pro "displej ukazuje něco
        jiného, než hraje". ID je z adresy, kterou mpv otevřel, název z JSONu,
        který mu vydal yt-dlp (nebo cache)."""
        vid = await self._current_video_id()
        title = await self._get("media-title")
        expected = self._current_id
        track = self._tracks.get(expected or "")
        if vid is None:
            return  # mpv právě načítá další skladbu — není co porovnat
        if vid != expected:
            log.warning(
                "NESOULAD: mpv hraje %s (%r), ytdj ukazuje %s (%s)",
                vid, title, expected, track.label() if track else "?",
            )
            telemetry.event("track.mismatch", mpv_id=vid, mpv_title=telemetry.clip(title, 120),
                            shown_id=expected)
        else:
            log.info("mpv hraje %s: %r", vid, title)

    async def _current_video_id(self) -> str | None:
        path = await self._get("path")
        if isinstance(path, str) and "v=" in path:
            return path.rsplit("v=", 1)[-1].split("&")[0]
        return None

    def on_event(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    # ---------- controls ----------

    async def enqueue(self, tracks: list[Track]) -> int:
        added = 0
        self._schedule_prefetch()
        if tracks and self._current_id is None and self._load is None and self._req is None:
            self._t_request("enqueue")  # nic nehraje — zvuk až po téhle dávce
        now = time.monotonic()
        for track in tracks:
            self._tracks[track.id] = track
            self._order.append(track.id)
            self._enqueued_at[track.id] = now
            # the response carries playlist_entry_id — the only reliable way
            # to later tell which track an event refers to
            res = await self._command(
                "loadfile", WATCH_URL.format(track.id), "append-play"
            )
            if isinstance(res, dict):
                data = res.get("data")
                if isinstance(data, dict) and "playlist_entry_id" in data:
                    self._entries[data["playlist_entry_id"]] = track.id
            added += 1
        if added:
            count = await self._get("playlist-count")
            if isinstance(count, int):
                self._count = count
        return added

    async def enqueue_next(self, tracks: list[Track]) -> int:
        """Zařadí hned za právě hrající skladbu.

        mpv 0.37 ještě nezná `loadfile ... insert-next`, takže se přidá na
        konec a přesune. Přesouvá se odzadu dopředu, kde `playlist-move src dst`
        položku uloží přesně na dst — a protože zbytek bloku leží až za src,
        jeho indexy se tím neposunou.
        """
        now = time.monotonic()
        for t in tracks:
            self._requested_at[t.id] = now
        added = await self.enqueue(tracks)
        if not added:
            return 0

        count = await self._get("playlist-count", self._count) or 0
        pos = await self._get("playlist-pos", self._pos) or 0
        self._count = count
        self._pos = pos
        start = count - added
        for i in range(added):
            src, dst = start + i, pos + 1 + i
            if src != dst:
                await self._command("playlist-move", src, dst, wait=False)

        # stejná úprava v našem pořadí, ať status() ukazuje reálnou frontu
        block = self._order[-added:]
        del self._order[-added:]
        at = min(pos + 1, len(self._order))
        self._order[at:at] = block
        return added

    async def clear_queue(self) -> None:
        await self._command("playlist-clear")  # removes all but the playing track
        self._order = [self._current_id] if self._current_id else []
        self._count = await self._get("playlist-count", 0) or 0

    async def skip(self, by_user: bool = True) -> None:
        self._replacing = not by_user
        self._t_request("skip" if by_user else "replace")
        await self._command("playlist-next", "force", wait=False)

    async def toggle_pause(self, paused: bool | None = None) -> None:
        target = (not self._paused) if paused is None else paused
        await self._command("set_property", "pause", target, wait=False)
        self._paused = target

    async def set_volume(self, volume: int) -> None:
        volume = _clamp_volume(volume)
        await self._command("set_property", "volume", volume, wait=False)
        self._volume = volume
        self._remember_volume(volume)

    # ---------- streamy dopředu ----------

    def _ytdl_shim(self) -> str:
        """Spustitelný soubor, který mpv volá místo yt-dlp (viz ytdl_cache)."""
        shim = RESOLVER_SOCKET.parent / "yt-dlp-shim"
        body = f'#!/bin/sh\nexec "{sys.executable}" "{ytdl_cache.__file__}" "$@"\n'
        try:
            shim.parent.mkdir(parents=True, exist_ok=True)
            if not shim.exists() or shim.read_text() != body:
                shim.write_text(body)
                shim.chmod(0o755)
        except OSError:
            log.warning("shim pro resolver nejde vytvořit", exc_info=True)
            return self.cfg.yt_dlp_path
        return str(shim)

    def _resolver_python(self) -> str | None:
        """Interpret, pod kterým je yt-dlp i s pluginy (z jeho shebangu)."""
        try:
            with open(self.cfg.yt_dlp_path, "rb") as f:
                first = f.readline().decode(errors="replace").strip()
        except OSError:
            return None
        if first.startswith("#!") and "python" in first:
            return first[2:].strip().split()[0]
        return None

    async def _start_resolver(self) -> None:
        python = self._resolver_python()
        if not python:
            log.warning("resolver nejde spustit (yt-dlp %s není Python skript) — "
                        "skladby se budou řešit postaru", self.cfg.yt_dlp_path)
            return
        RESOLVER_SOCKET.parent.mkdir(parents=True, exist_ok=True)
        self._resolver_started = time.monotonic()
        self._resolver = await asyncio.create_subprocess_exec(
            python, str(Path(__file__).with_name("ytdl_resolver.py")),
            "--socket", str(RESOLVER_SOCKET),
            env=self.cfg.child_env(),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=lambda: os.nice(5),  # zvuk má přednost
        )
        self._resolver_task = asyncio.create_task(self._watch_resolver(self._resolver))

    async def _watch_resolver(self, proc: asyncio.subprocess.Process) -> None:
        """Přeposílá log resolveru a po pádu ho spustí znovu."""
        assert proc.stderr
        async for line in proc.stderr:
            text = line.decode(errors="replace").rstrip()
            parsed = parse_resolver_line(text)
            if parsed is None:
                log.info("resolver: %s", text)
            else:
                with suppress(Exception):
                    self._on_resolver_event(*parsed)
        code = await proc.wait()
        self._res_ready, self._res_busy, self._res_urgent = set(), None, []
        if not self._stopping:
            self._resolver_restarts += 1
            telemetry.event("resolver.exit", code=code, restarts=self._resolver_restarts,
                            uptime_s=int(time.monotonic() - self._resolver_started))
            log.warning("resolver skončil (kód %s), startuji znovu", code)
            await asyncio.sleep(2)
            await self._start_resolver()
            self._schedule_prefetch()

    async def _resolver_call(self, req: dict) -> dict | None:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(RESOLVER_SOCKET)), 3
            )
        except (OSError, asyncio.TimeoutError):
            return None
        try:
            writer.write(json.dumps(req).encode() + b"\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), 5)
            return json.loads(line) if line else None
        except (OSError, ValueError, asyncio.TimeoutError):
            return None
        finally:
            writer.close()

    def _schedule_prefetch(self) -> None:
        """Probudí (nebo spustí) úlohu, která resolveru říká, co chystat dopředu."""
        self._prefetch_wake.set()
        if self._prefetch_task is None or self._prefetch_task.done():
            self._prefetch_task = asyncio.create_task(self._prefetch_loop())

    async def _prefetch_loop(self) -> None:
        sent: list[str] | None = None
        while True:
            try:
                await asyncio.wait_for(self._prefetch_wake.wait(), 5.0)
            except asyncio.TimeoutError:
                pass  # i bez podnětu: resolver mohl spadnout, Codex mohl doběhnout
            self._prefetch_wake.clear()
            await asyncio.sleep(0.5)  # fronta se mění po dávkách — počkat, až se usadí
            pos = self._pos
            ahead = [
                vid for vid in self._order[pos + 1 : pos + 1 + PREFETCH_AHEAD]
                if vid and vid != self._current_id
            ]
            held = bool(self.busy_check and self.busy_check())
            if held:
                ahead = []  # Codex si bere ~200 MB — node vedle by poslal Pi do swapu
            if ahead == sent:
                continue
            resp = await self._resolver_call({"op": "ahead", "ids": ahead})
            ok = bool(resp and resp.get("ok"))
            telemetry.event("prefetch.ahead", n=len(ahead), codex_hold=held, ok=ok,
                            ready=sum(1 for v in ahead if v in self._res_ready))
            if ok:
                sent = ahead
            else:
                sent = None  # resolver nežije — zkusit znovu při dalším kole

    # ---------- zapamatování hlasitosti ----------

    def _remember_volume(self, volume: int) -> None:
        """Přežije restart — jinak se jukebox vrátí na plný kotel.

        Jediné hrdlo, kterým jde hlasitost od REPL i od webu, takže stačí
        zapisovat tady.
        """
        self.cfg.volume = volume
        if volume == self._volume_pending:
            return
        self._volume_pending = volume
        if self._volume_save and not self._volume_save.done():
            self._volume_save.cancel()
        self._volume_save = asyncio.create_task(self._save_volume_later(volume))

    async def _save_volume_later(self, volume: int) -> None:
        await asyncio.sleep(SAVE_DELAY)  # zrušení během čekání = nic se nezapíše
        try:
            await asyncio.to_thread(save_values, {"volume": volume})
            self._volume_pending = None
        except OSError:
            log.warning("hlasitost se nepodařilo uložit do configu", exc_info=True)

    def _flush_volume(self) -> None:
        """Doufat, že se odložený zápis stihne, při ukončování nejde."""
        if self._volume_save and not self._volume_save.done():
            self._volume_save.cancel()
        if self._volume_pending is None:
            return
        try:
            save_values({"volume": self._volume_pending})
        except OSError:
            log.warning("hlasitost se nepodařilo uložit do configu", exc_info=True)
        self._volume_pending = None

    async def status(self) -> PlayerStatus:
        pos = await self._get("playlist-pos", self._pos) or 0
        count = await self._get("playlist-count", self._count) or 0
        self._pos, self._count = pos, count

        upcoming: list[Track] = []
        if 0 <= pos < len(self._order):
            for vid in self._order[pos + 1 :]:
                t = self._tracks.get(vid)
                if t:
                    upcoming.append(t)

        current = self._tracks.get(self._current_id) if self._current_id else None
        # core-idle = mpv právě nic nepřehrává (pauza, nebo čeká na data)
        idle = bool(await self._get("core-idle", False))
        return PlayerStatus(
            buffering=idle and not self._paused and current is not None,
            playing=count > 0 and not self._paused,
            paused=self._paused,
            current=current,
            position=float(await self._get("time-pos", 0) or 0),
            duration=float(await self._get("duration", 0) or 0),
            queue=upcoming,
            volume=self._volume,
            quality=self._quality,
        )

    @property
    def queue_depth(self) -> int:
        return max(0, self._count - self._pos - 1)
