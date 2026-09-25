"""Talking to a running ytdj over its local HTTP API — stdlib only.

Two background threads, both of which survive anything the network throws at
them: `StatusFeed` follows the SSE stream (falling back to polling
/api/status when the stream isn't available) and reconnects forever;
`Commander` sends control commands one after another, coalescing volume
changes so a drag never builds up a backlog of stale requests.
"""

from __future__ import annotations

import http.client
import json
import logging
import threading
import time
from collections import deque
from typing import Any, Callable
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

# the server sends a keepalive every 15 s; silence much longer than that
# means the connection is dead even if the socket hasn't noticed
READ_TIMEOUT = 40.0
CONNECT_TIMEOUT = 3.0
CONTROL_TIMEOUT = 6.0
PROMPT_TIMEOUT = 240.0  # tah Codexu na Pi trvá i přes minutu
POLL_INTERVAL = 1.0
# how long to stay on polling before trying the stream again
POLL_SPELL = 60.0
BACKOFF_MIN = 0.5
# ytdj under systemd comes back in ~5 s, so there's no point waiting longer
BACKOFF_MAX = 5.0

NET_ERRORS = (OSError, http.client.HTTPException, ValueError)


class ApiError(Exception):
    """The server answered, but not with success."""


class _NoStream(Exception):
    """/api/events isn't usable (old server, proxy…) — poll instead."""


class Api:
    def __init__(self, base_url: str) -> None:
        parts = urlsplit(base_url if "//" in base_url else f"http://{base_url}")
        if parts.scheme not in ("http", ""):
            raise ValueError(f"podporuji jen http://, ne {parts.scheme}://")
        self.host = parts.hostname or "127.0.0.1"
        self.port = parts.port or 80
        self.prefix = parts.path.rstrip("/")

    @property
    def target(self) -> str:
        return f"{self.host}:{self.port}"

    def connection(self, timeout: float) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)

    def _request(self, method: str, path: str, body: dict | None, timeout: float) -> Any:
        conn = self.connection(timeout)
        try:
            data = json.dumps(body).encode() if body is not None else None
            headers = {"Content-Type": "application/json"} if data else {}
            conn.request(method, self.prefix + path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            try:
                payload = json.loads(raw) if raw else {}
            except ValueError:
                payload = {}
            if resp.status >= 400:
                msg = payload.get("error") if isinstance(payload, dict) else None
                raise ApiError(msg or f"HTTP {resp.status}")
            return payload
        finally:
            conn.close()

    def status(self, timeout: float = 5.0) -> dict:
        data = self._request("GET", "/api/status", None, timeout)
        if not isinstance(data, dict):
            raise ValueError("stav není JSON objekt")
        return data

    def prompt(self, text: str) -> None:
        """Požadavek na DJ — odpověď přijde až po tahu Codexu (desítky vteřin)."""
        self._request("POST", "/api/prompt", {"text": text}, PROMPT_TIMEOUT)

    def control(self, action: str, value: int | None = None) -> None:
        body: dict[str, Any] = {"action": action}
        if value is not None:
            body["value"] = value
        self._request("POST", "/api/control", body, CONTROL_TIMEOUT)


class StatusFeed(threading.Thread):
    """Delivers every state snapshot to `on_state`; `on_offline` when unreachable."""

    def __init__(
        self,
        api: Api,
        on_state: Callable[[dict], None],
        on_offline: Callable[[str], None],
        stop: threading.Event,
    ) -> None:
        super().__init__(name="panel-feed", daemon=True)
        self.api = api
        self.on_state = on_state
        self.on_offline = on_offline
        self.stop = stop

    def run(self) -> None:
        backoff = BACKOFF_MIN
        poll_until = 0.0
        while not self.stop.is_set():
            try:
                if time.monotonic() < poll_until:
                    self.on_state(self.api.status())
                    backoff = BACKOFF_MIN
                    self.stop.wait(POLL_INTERVAL)
                    continue
                if self._follow():
                    backoff = BACKOFF_MIN
                # the stream ended cleanly (ytdj shutting down?) — reconnect
                # right away; if it's really gone, the next attempt says so
                continue
            except _NoStream as exc:
                log.info("SSE nejde (%s), přecházím na dotazování", exc)
                poll_until = time.monotonic() + POLL_SPELL
                continue
            except NET_ERRORS as exc:
                reason = _describe(exc)
            except Exception as exc:  # never let the thread die
                log.exception("neočekávaná chyba ve čtení stavu")
                reason = str(exc) or type(exc).__name__
            log.debug("ytdj nedostupný: %s (další pokus za %.1f s)", reason, backoff)
            self.on_offline(reason)
            self.stop.wait(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)

    def _follow(self) -> bool:
        """Reads the SSE stream until it ends; True if any state came through."""
        conn = self.api.connection(CONNECT_TIMEOUT)
        got = False
        try:
            conn.request(
                "GET",
                self.api.prefix + "/api/events",
                headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"},
            )
            resp = conn.getresponse()
            ctype = resp.getheader("Content-Type", "")
            if resp.status != 200 or "text/event-stream" not in ctype:
                raise _NoStream(f"HTTP {resp.status} {ctype}")
            if conn.sock is not None:
                conn.sock.settimeout(READ_TIMEOUT)
            data: list[str] = []
            while not self.stop.is_set():
                raw = resp.readline()
                if not raw:
                    return got
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("data:"):
                    value = line[5:]
                    data.append(value[1:] if value.startswith(" ") else value)
                elif not line and data:
                    try:
                        state = json.loads("\n".join(data))
                    except ValueError:
                        log.debug("nečitelná SSE zpráva přeskočena")
                    else:
                        if isinstance(state, dict) and state:
                            got = True
                            self.on_state(state)
                    data = []
                # comments (": ping") and other fields just keep us alive
            return got
        finally:
            conn.close()


def _describe(exc: BaseException) -> str:
    if isinstance(exc, ConnectionRefusedError):
        return "spojení odmítnuto"
    if isinstance(exc, TimeoutError):
        return "vypršel čas"
    return str(exc) or type(exc).__name__


class Commander(threading.Thread):
    """Sends /api/control commands in order; volume is coalesced to the latest."""

    def __init__(
        self,
        api: Api,
        on_result: Callable[[str, int | None, str | None], None],
        stop: threading.Event,
    ) -> None:
        super().__init__(name="panel-control", daemon=True)
        self.api = api
        self.on_result = on_result
        self.stop = stop
        self._cv = threading.Condition()
        self._actions: deque[str] = deque()
        self._volume: int | None = None

    def send(self, action: str) -> None:
        with self._cv:
            self._actions.append(action)
            self._cv.notify()

    def volume(self, value: int) -> None:
        with self._cv:
            self._volume = value
            self._cv.notify()

    def wake(self) -> None:
        with self._cv:
            self._cv.notify()

    def run(self) -> None:
        while not self.stop.is_set():
            with self._cv:
                while not self._actions and self._volume is None and not self.stop.is_set():
                    self._cv.wait()
                if self.stop.is_set():
                    return
                if self._actions:
                    action, value = self._actions.popleft(), None
                else:
                    action, value, self._volume = "volume", self._volume, None
            error = None
            try:
                self.api.control(action, value)
            except ApiError as exc:
                error = str(exc)
            except NET_ERRORS as exc:
                error = _describe(exc)
            except Exception as exc:
                log.exception("povel %s selhal", action)
                error = str(exc) or type(exc).__name__
            if error:
                log.warning("povel %s selhal: %s", action, error)
            self.on_result(action, value, error)
