"""mpv jako přehrávač, řízený přes JSON IPC na unix socketu.

Stream URL řeší mpv sám přes ytdl_hook -> yt-dlp. Tři věci, bez kterých to
nepůjde a v dokumentaci se o nich mlčí:
  * `secretstorage` musí být v prostředí yt-dlp, jinak nerozšifruje Chrome cookies
  * `--js-runtimes` + `--remote-components=ejs:github`, jinak selže řešení
    signatur a Premium formáty (774/141) se vůbec nenabídnou
  * node musí být v PATH podprocesu mpv (viz Config.child_env)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import suppress

from ..config import MPV_SOCKET, Config
from ..music.catalog import Track
from .base import EventHandler, Player, PlayerEvent, PlayerStatus

log = logging.getLogger(__name__)

WATCH_URL = "https://music.youtube.com/watch?v={}"


class MpvPlayer(Player):
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.proc: asyncio.subprocess.Process | None = None
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None

        self._req_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._handlers: list[EventHandler] = []
        self._reader_task: asyncio.Task | None = None
        self._dispatch_task: asyncio.Task | None = None
        # (kind, playlist_entry_id, detail) — skladba se dohledává až
        # v dispatch smyčce, protože v okamžiku události ještě nemusí být
        # zapsané mapování z odpovědi na loadfile
        self._events: asyncio.Queue[tuple[str, int, str]] = asyncio.Queue()

        # mpv playlist <-> naše Tracky, klíčem je videoId
        self._tracks: dict[str, Track] = {}
        self._order: list[str] = []  # pořadí, jak jsme enqueovali
        # playlist_entry_id (od mpv) -> videoId. Díky tomuhle se u událostí
        # nemusíme mpv na nic doptávat — a nemůže tak vzniknout deadlock,
        # kdy handler čeká na odpověď, kterou má přečíst tatáž smyčka.
        self._entries: dict[int, str] = {}
        self._current_id: str | None = None
        self._pos = 0
        self._count = 0
        self._paused = False
        self._volume = 100

    # ---------- životní cyklus ----------

    def _args(self) -> list[str]:
        args = [
            "mpv",
            "--idle=yes",
            "--no-video",
            "--no-terminal",
            "--vid=no",
            f"--input-ipc-server={MPV_SOCKET}",
            f"--ytdl-format={self.cfg.ytdl_format}",
            f"--script-opts=ytdl_hook-ytdl_path={self.cfg.yt_dlp_path}",
            "--prefetch-playlist=yes",  # předřeší URL další skladby -> žádná mezera
            "--gapless-audio=weak",
            "--cache=yes",
            "--keep-open=no",
        ]
        raw = []
        if self.cfg.cookies_browser and self.cfg.cookies_browser != "none":
            raw.append(f"cookies-from-browser={self.cfg.cookies_browser}")
        if self.cfg.js_runtimes:
            raw.append(f"js-runtimes={self.cfg.js_runtimes}")
        if self.cfg.remote_components:
            raw.append(f"remote-components={self.cfg.remote_components}")
        # každá volba zvlášť přes -append, ať se nemusí escapovat čárky
        args += [f"--ytdl-raw-options-append={opt}" for opt in raw]
        args += list(self.cfg.mpv_extra_args)
        return args

    async def start(self) -> None:
        with suppress(FileNotFoundError):
            os.unlink(MPV_SOCKET)
        MPV_SOCKET.parent.mkdir(parents=True, exist_ok=True)

        self.proc = await asyncio.create_subprocess_exec(
            *self._args(),
            env=self.cfg.child_env(),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        for _ in range(100):  # ~5 s na vytvoření socketu
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

        for i, prop in enumerate(("playlist-pos", "playlist-count", "pause", "volume"), 1):
            await self._send({"command": ["observe_property", i, prop]}, wait=False)

    async def stop(self) -> None:
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
            if not raw:
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
        """Zpracování události. Nesmí volat IPC ani nic awaitovat — běží
        uvnitř čtecí smyčky, takže by čekalo na odpověď, kterou by musela
        přečíst ta samá smyčka."""
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
            return

        if event == "start-file":
            self._events.put_nowait(("start", msg.get("playlist_entry_id", -1), ""))
            return

        if event == "end-file":
            reason = msg.get("reason", "")
            # reason rozlišuje dohráno vs. přeskočeno — to je náš implicitní
            # signál, jestli se skladba trefila
            kind = {
                "eof": "finished",
                "stop": "skipped",
                "quit": "skipped",
                "error": "error",
                "redirect": "skipped",
            }.get(reason, "skipped")
            self._events.put_nowait((kind, msg.get("playlist_entry_id", -1), reason))
            return

        if event == "idle":
            self._events.put_nowait(("idle", -1, ""))

    async def _dispatch_loop(self) -> None:
        """Handlery běží mimo čtecí smyčku (takže smí volat IPC), ale v pořadí,
        v jakém události přišly."""
        while True:
            kind, entry_id, detail = await self._events.get()

            track = None
            if kind != "idle":
                vid = self._entries.get(entry_id)
                if vid is None and kind == "start":
                    # mapování ještě nedorazilo — zeptáme se mpv přímo
                    vid = await self._current_video_id()
                    if vid:
                        self._entries[entry_id] = vid
                if kind == "start":
                    self._current_id = vid
                elif vid is None:
                    vid = self._current_id
                track = self._tracks.get(vid or "")

            ev = PlayerEvent(kind, track, detail=detail)
            for handler in self._handlers:
                try:
                    await handler(ev)
                except Exception:
                    log.exception("handler události selhal")

    async def _current_video_id(self) -> str | None:
        path = await self._get("path")
        if isinstance(path, str) and "v=" in path:
            return path.rsplit("v=", 1)[-1].split("&")[0]
        return None

    def on_event(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    # ---------- ovládání ----------

    async def enqueue(self, tracks: list[Track]) -> int:
        added = 0
        for track in tracks:
            self._tracks[track.id] = track
            self._order.append(track.id)
            # odpověď nese playlist_entry_id — jediný spolehlivý způsob, jak
            # pak u událostí poznat, o kterou skladbu jde
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

    async def clear_queue(self) -> None:
        await self._command("playlist-clear")  # smaže vše kromě právě hrající
        self._order = [self._current_id] if self._current_id else []
        self._count = await self._get("playlist-count", 0) or 0

    async def skip(self) -> None:
        await self._command("playlist-next", "force", wait=False)

    async def toggle_pause(self, paused: bool | None = None) -> None:
        target = (not self._paused) if paused is None else paused
        await self._command("set_property", "pause", target, wait=False)
        self._paused = target

    async def set_volume(self, volume: int) -> None:
        volume = max(0, min(130, volume))
        await self._command("set_property", "volume", volume, wait=False)
        self._volume = volume

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
        return PlayerStatus(
            playing=count > 0 and not self._paused,
            paused=self._paused,
            current=current,
            position=float(await self._get("time-pos", 0) or 0),
            duration=float(await self._get("duration", 0) or 0),
            queue=upcoming,
            volume=self._volume,
        )

    @property
    def queue_depth(self) -> int:
        return max(0, self._count - self._pos - 1)
