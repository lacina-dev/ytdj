"""Falešné mpv na unixovém socketu — JSON IPC v rozsahu, který používá MpvPlayer.

Playlist, `current`, loadfile (append / append-play / insert-next / insert-at),
playlist-clear / -move / -remove / -next, get/set/observe_property a události
start-file / end-file / idle. Každý příkaz může mít náhodné zpoždění, aby se
souběžné korutiny ytdj opravdu proplétaly.

`old=True` hraje mpv 0.37: `insert-next` / `insert-at` nezná.
"""

from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path


class FakeMpv:
    def __init__(self, path: Path, *, old: bool = False, jitter: float = 0.0,
                 seed: int = 1) -> None:
        self.path = path
        self.old = old
        self.jitter = jitter
        self.rng = random.Random(seed)
        self.playlist: list[dict] = []  # {"id", "filename"}
        self.cur: int | None = None  # index
        self._next_id = 1
        self.observed: dict[str, int] = {}
        self.writer: asyncio.StreamWriter | None = None
        self.server: asyncio.AbstractServer | None = None
        self.log: list[list] = []
        # jak mpv ohlásí konec přeskočené položky (opuštěná při načítání → "error")
        self.next_reason = "stop"
        self.fail: set[str] = set()  # videoId, které se "nedají otevřít"
        self.started: list[str] = []  # co opravdu začalo hrát

    # ---- pohled pro testy ----

    def ids(self) -> list[str]:
        return [e["filename"].rsplit("v=", 1)[-1] for e in self.playlist]

    def current_vid(self) -> str | None:
        return self.ids()[self.cur] if self.cur is not None else None

    def upcoming(self) -> list[str]:
        if self.cur is None:
            return []
        return self.ids()[self.cur + 1 :]

    # ---- server ----

    async def start(self) -> None:
        self.server = await asyncio.start_unix_server(self._client, path=str(self.path))

    async def close(self) -> None:
        if self.writer:
            self.writer.close()
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        while True:
            line = await reader.readline()
            if not line:
                return
            msg = json.loads(line)
            if self.jitter:
                await asyncio.sleep(self.rng.random() * self.jitter)
            rid = msg.get("request_id")
            try:
                data = self._do(msg["command"])
                resp = {"error": "success", "request_id": rid}
                if data is not None:
                    resp["data"] = data
            except _Err as exc:
                resp = {"error": str(exc), "request_id": rid}
            self._send(resp)
            await writer.drain()

    def _send(self, obj: dict) -> None:
        assert self.writer
        self.writer.write((json.dumps(obj) + "\n").encode())

    def _notify(self) -> None:
        for name, oid in self.observed.items():
            if name in ("playlist", "playlist-pos", "playlist-count"):
                self._send({"event": "property-change", "id": oid, "name": name,
                            "data": self._prop(name)})

    def _prop(self, name: str):
        if name == "playlist":
            out = []
            for i, e in enumerate(self.playlist):
                d = dict(e)
                if i == self.cur:
                    d["current"] = True
                    d["playing"] = True
                out.append(d)
            return out
        if name == "playlist-pos":
            return -1 if self.cur is None else self.cur
        if name == "playlist-count":
            return len(self.playlist)
        if name == "core-idle":
            return self.cur is None
        if name == "path":
            return self.playlist[self.cur]["filename"] if self.cur is not None else None
        if name in ("time-pos", "duration", "volume"):
            return 0
        if name == "pause":
            return False
        raise _Err("property unavailable")

    # ---- příkazy ----

    def _start(self, idx: int | None) -> None:
        """Jako mpv: položka, která se nedá otevřít (self.fail — výpadek sítě
        nebo vadné video), skončí hned chybou a jede se na další."""
        while True:
            self.cur = idx
            if idx is None:
                self._send({"event": "idle"})
                return
            entry = self.playlist[idx]
            self._send({"event": "start-file", "playlist_entry_id": entry["id"]})
            if entry["filename"].rsplit("v=", 1)[-1] not in self.fail:
                self.started.append(entry["filename"].rsplit("v=", 1)[-1])
                return
            self._notify()
            self._send({"event": "end-file", "reason": "error", "file_error": "loading failed",
                        "playlist_entry_id": entry["id"]})
            idx = idx + 1 if idx + 1 < len(self.playlist) else None

    def _do(self, cmd: list):
        self.log.append(cmd)
        name = cmd[0]
        if name == "observe_property":
            self.observed[cmd[2]] = cmd[1]
            return None
        if name == "get_property":
            return self._prop(cmd[1])
        if name == "set_property":
            return None
        if name == "loadfile":
            url, flag = cmd[1], (cmd[2] if len(cmd) > 2 else "replace")
            entry = {"id": self._next_id, "filename": url}
            self._next_id += 1
            if flag in ("append", "append-play"):
                self.playlist.append(entry)
                if flag == "append-play" and self.cur is None:
                    self._notify()
                    self._start(len(self.playlist) - 1)
            elif flag in ("insert-next", "insert-at") and not self.old:
                if flag == "insert-next":
                    at = 0 if self.cur is None else self.cur + 1
                else:
                    at = int(cmd[3])
                self.playlist.insert(at, entry)
                if self.cur is not None and at <= self.cur:
                    self.cur += 1
            else:
                raise _Err("invalid parameter")
            self._notify()
            return {"playlist_entry_id": entry["id"]}
        if name == "playlist-clear":
            if self.cur is None:
                self.playlist = []
            else:
                self.playlist = [self.playlist[self.cur]]
                self.cur = 0
            self._notify()
            return None
        if name == "playlist-remove":
            idx = int(cmd[1])
            if idx == self.cur:
                raise _Err("not supported in fake")
            del self.playlist[idx]
            if self.cur is not None and idx < self.cur:
                self.cur -= 1
            self._notify()
            return None
        if name == "playlist-move":
            src, dst = int(cmd[1]), int(cmd[2])
            cur_entry = self.playlist[self.cur] if self.cur is not None else None
            target = self.playlist[dst] if dst < len(self.playlist) else None
            e = self.playlist.pop(src)
            if target is None:
                self.playlist.append(e)
            else:
                self.playlist.insert(self.playlist.index(target), e)
            if cur_entry is not None:
                self.cur = self.playlist.index(cur_entry)
            self._notify()
            return None
        if name == "stop":
            if "keep-playlist" not in cmd[1:]:
                self.playlist = []
            if self.cur is not None and self.cur < len(self.playlist):
                self._send({"event": "end-file", "reason": "stop",
                            "playlist_entry_id": self.playlist[self.cur]["id"]})
            self.cur = None
            self._send({"event": "idle"})
            self._notify()
            return None
        if name == "playlist-play-index":
            idx = int(cmd[1])
            if not 0 <= idx < len(self.playlist):
                raise _Err("invalid index")
            if self.cur is not None:
                self._send({"event": "end-file", "reason": "stop",
                            "playlist_entry_id": self.playlist[self.cur]["id"]})
            self._start(idx)
            self._notify()
            return None
        if name == "playlist-next":
            if self.cur is None:
                raise _Err("error running command")
            old = self.playlist[self.cur]
            self._send({"event": "end-file", "reason": self.next_reason,
                        "playlist_entry_id": old["id"]})
            nxt = self.cur + 1 if self.cur + 1 < len(self.playlist) else None
            self._start(nxt)
            self._notify()
            return None
        raise _Err(f"unknown command {name}")

    def finish_current(self) -> None:
        """Skladba dohrála (eof) → další."""
        assert self.cur is not None
        old = self.playlist[self.cur]
        self._send({"event": "end-file", "reason": "eof", "playlist_entry_id": old["id"]})
        self._start(self.cur + 1 if self.cur + 1 < len(self.playlist) else None)
        self._notify()


class _Err(Exception):
    pass
