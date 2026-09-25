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
from contextlib import suppress

from ..config import MPV_SOCKET, Config, save_values
from . import ytdl_cache
from ..music.catalog import Track
from .base import EventHandler, Player, PlayerEvent, PlayerStatus

log = logging.getLogger(__name__)

WATCH_URL = "https://music.youtube.com/watch?v={}"
PREFETCH_AHEAD = 4  # kolik skladeb z fronty mít vyřešených dopředu (~20 s každá na Pi 3)

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
        self._paused = False
        self._volume = _clamp_volume(cfg.volume)
        # Zápis hlasitosti do configu se odkládá: tažení slideru i držené "+"
        # v REPL by jinak přepisovaly soubor několikrát za vteřinu.
        self._volume_save: asyncio.Task | None = None
        self._volume_pending: int | None = None
        self._quality = ""
        self._quality_task: asyncio.Task | None = None

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
        env[ytdl_cache.ENV_DIR] = str(ytdl_cache.cache_dir())
        self.proc = await asyncio.create_subprocess_exec(
            *self._args(),
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        for _ in range(100):  # ~5 s for the socket to appear
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
            raise RuntimeError(f"mpv nenastartoval (socket {MPV_SOCKET} nevznikl)")

        self._reader_task = asyncio.create_task(self._read_loop())
        self._dispatch_task = asyncio.create_task(self._dispatch_loop())

        for i, prop in enumerate(
            ("playlist-pos", "playlist-count", "pause", "volume", "time-pos"), 1
        ):
            await self._send({"command": ["observe_property", i, prop]}, wait=False)

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
            elif name == "volume" and isinstance(data, (int, float)):
                self._volume = int(data)
            elif name == "time-pos" and isinstance(data, (int, float)):
                self._time_pos = float(data)
            return

        if event == "start-file":
            self._events.put_nowait(("start", msg.get("playlist_entry_id", -1), ""))
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
            self._note_premature_end(kind, msg.get("playlist_entry_id", -1))
            self._time_pos = 0.0
            self._events.put_nowait((kind, msg.get("playlist_entry_id", -1), reason))
            return

        if event == "idle":
            self._events.put_nowait(("idle", -1, ""))

    def _note_premature_end(self, kind: str, entry_id: int) -> None:
        """Skladba, která 'dohrála' dávno před koncem, je useknutý stream.

        Když spojení umře i přes reconnect, ffmpeg ohlásí konec souboru a mpv
        čistý eof — od uživatelského dohrání se to nijak neliší. Aspoň se to
        nahlas zapíše do logu, ať se dá 'občas se to usekne' dohledat: která
        skladba, v kolikáté vteřině z kolika.
        """
        if kind != "finished":
            return
        vid = self._entries.get(entry_id) or self._current_id
        track = self._tracks.get(vid or "")
        played = self._time_pos
        if not track or not track.duration or not played:
            return
        if played + 15 < track.duration * 0.9:
            log.warning(
                "skladba %s skončila předčasně (%d s z %d) — nejspíš výpadek streamu",
                track.label(), int(played), track.duration,
            )

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
        await self._log_playing()

    async def _log_playing(self) -> None:
        """Zapíše, co mpv doopravdy přehrává — důkaz pro "displej ukazuje něco
        jiného, než hraje". ID je z adresy, kterou mpv otevřel, název z JSONu,
        který mu vydal yt-dlp (nebo cache)."""
        vid = await self._current_video_id()
        title = await self._get("media-title")
        expected = self._current_id
        track = self._tracks.get(expected or "")
        if vid != expected:
            log.warning(
                "NESOULAD: mpv hraje %s (%r), ytdj ukazuje %s (%s)",
                vid, title, expected, track.label() if track else "?",
            )
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
        for track in tracks:
            self._tracks[track.id] = track
            self._order.append(track.id)
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
        shim = ytdl_cache.cache_dir() / "yt-dlp-cached"
        body = f'#!/bin/sh\nexec "{sys.executable}" "{ytdl_cache.__file__}" "$@"\n'
        try:
            shim.parent.mkdir(parents=True, exist_ok=True)
            if not shim.exists() or shim.read_text() != body:
                shim.write_text(body)
                shim.chmod(0o755)
        except OSError:
            log.warning("shim pro předem vyřešené streamy nejde vytvořit", exc_info=True)
            return self.cfg.yt_dlp_path
        return str(shim)

    def _schedule_prefetch(self) -> None:
        """Probudí (nebo spustí) úlohu, která drží další skladby vyřešené."""
        self._prefetch_wake.set()
        if self._prefetch_task is None or self._prefetch_task.done():
            self._prefetch_task = asyncio.create_task(self._prefetch_loop())

    async def _prefetch_loop(self) -> None:
        done: dict[str, float] = {}
        while True:
            await self._prefetch_wake.wait()
            self._prefetch_wake.clear()
            await asyncio.sleep(1.0)  # fronta se mění po dávkách — počkat, až se usadí
            pos = self._pos
            ahead = [
                vid for vid in self._order[pos + 1 : pos + 1 + PREFETCH_AHEAD]
                if vid and vid != self._current_id
            ]
            if not ahead:
                continue
            if not await asyncio.to_thread(ytdl_cache.has_template):
                # mpv ještě nikdy nevolal yt-dlp, takže nevíme s čím; první
                # skladba to za chvíli zjistí
                await asyncio.sleep(5.0)
                self._prefetch_wake.set()
                continue
            now = asyncio.get_running_loop().time()
            for vid in ahead:
                if now - done.get(vid, -1e9) < ytdl_cache.MAX_AGE / 2:
                    continue
                t0 = asyncio.get_running_loop().time()
                ok = await asyncio.to_thread(
                    ytdl_cache.prefetch, vid, WATCH_URL.format(vid), self.cfg.yt_dlp_path
                )
                took = asyncio.get_running_loop().time() - t0
                if ok:
                    done[vid] = asyncio.get_running_loop().time()
                    track = self._tracks.get(vid)
                    log.info("připraveno dopředu (%.0f s): %s", took, track.label() if track else vid)
                else:
                    log.debug("dopředu se nepodařilo: %s", vid)
                if self._prefetch_wake.is_set():
                    break  # fronta se mezitím změnila — přepočítat

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
