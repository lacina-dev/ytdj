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
        self._paused = False
        self._volume = 100

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
            f"--script-opts=ytdl_hook-ytdl_path={self.cfg.yt_dlp_path}",
            "--prefetch-playlist=yes",  # pre-resolves the next track's URL -> no gap
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
        # each option separately via -append, so commas need no escaping
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
            self._events.put_nowait((kind, msg.get("playlist_entry_id", -1), reason))
            return

        if event == "idle":
            self._events.put_nowait(("idle", -1, ""))

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

    # ---------- controls ----------

    async def enqueue(self, tracks: list[Track]) -> int:
        added = 0
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

    async def clear_queue(self) -> None:
        await self._command("playlist-clear")  # removes all but the playing track
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
