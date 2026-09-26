"""The network screens' state machine: overview → Wi-Fi list → password → connect.

Lives inside `PanelApp` and runs on its main thread. Everything that talks
to NetworkManager goes to a short-lived worker thread whose result comes back
through the app's event queue as ("net", job, value, error) — the panel keeps
answering touches, media keys and ytdj updates while nmcli takes its time.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from .hw import Box, TouchEvent
from .net import Connected, NetBackend, NetError, NetStatus, WifiNet
from .netui import ROWS, STRINGS, NetRenderer, NetView, targets
from .stats import emit, scrub
from .touchpress import Press, closest, nearest

log = logging.getLogger(__name__)

STATUS_EVERY = 5.0  # s, while a network page is open
STATUS_EVERY_IDLE = 30.0  # s, on the player screen (just for the status icon)
IDLE_CLOSE = 180.0  # s without a touch → back to the player
NOTE_TIME = 2.5
SCROLL_STEP = ROWS - 1  # keep one row in view for orientation
CAPS_TAP = 0.45  # s — shift tapped twice this fast locks capitals
MIN_PRESS = 0.02
MAX_PASSWORD = 63
# klávesy, které smí do provozního logu — písmena hesla nikdy
LOGGED_KEYS = frozenset({"ok", "cancel"})


class NetController:
    def __init__(
        self,
        backend: NetBackend,
        post: Callable[[tuple], None],
        lang: str = "cs",
        web_port: int = 8765,
        now: float | None = None,
        count: Callable[[str], None] | None = None,
    ) -> None:
        self.backend = backend
        self.count = count or (lambda key: None)  # odmítnuté dotyky → souhrn za minutu
        self.post = post
        self.lang = lang if lang in STRINGS else "cs"
        self.s = STRINGS[self.lang]
        self.web_port = web_port
        self.renderer = NetRenderer(self.lang)

        self.page: str | None = None  # None = the player screen
        self.status: NetStatus | None = None
        self.inflight: set[str] = set()
        self.job_started: dict[str, float] = {}
        self._status_sig: tuple | None = None  # co se naposledy zapsalo jako panel.net_status
        self.connect_attempts = 0
        self.next_status = now if now is not None else time.monotonic()
        self.last_touch = 0.0

        # list
        self.nets: list[WifiNet] = []
        self.scroll = 0
        self.scanned = False
        self.scan_error = ""

        # keyboard
        self.ssid = ""
        self.secure = True
        self.password = ""
        self.show_pw = False
        self.kb_page = "abc"
        self.shift = 0
        self.shift_at = 0.0
        self.hint = ""

        # connecting
        self.phase = ""
        self.conn_started = 0.0
        self.result: Connected | None = None
        self.result_error = ""

        # gesture
        self.pressed: str | None = None
        self.press: Press | None = None  # the press in progress (touchpress rules)
        self.press_at = 0.0
        self.last_xy = (0, 0)
        self.inside = False

    # ---- jobs ----

    def _job(self, kind: str, fn: Callable[[], object]) -> None:
        if kind in self.inflight:
            return
        self.inflight.add(kind)
        self.job_started[kind] = time.monotonic()

        def run() -> None:
            value, error = None, None
            try:
                value = fn()
            except NetError as exc:
                error = str(exc) or "unknown"
            except Exception as exc:  # a bug must not leave the job "in flight" forever
                log.exception("síť: úloha %s selhala", kind)
                error = type(exc).__name__
            self.post(("net", kind, value, error))

        threading.Thread(target=run, name=f"panel-net-{kind}", daemon=True).start()

    def refresh(self) -> None:
        self._job("status", self.backend.status)

    def handle(self, msg: tuple) -> None:
        _, kind, value, error = msg
        self.inflight.discard(kind)
        now = time.monotonic()
        took_ms = int((now - self.job_started.pop(kind, now)) * 1000)
        if kind == "status":
            if isinstance(value, NetStatus):
                self.status = value
            elif error:
                self.status = NetStatus(error=error)
            self._log_status(took_ms)
            self.next_status = now + (STATUS_EVERY if self.page else STATUS_EVERY_IDLE)
        elif kind == "scan":
            emit(
                "panel.net_scan", ok=not error, error=error, took_ms=took_ms,
                networks=None if error else len(value or []),
            )
            if error:
                self.scan_error = self._error_text(error)
            else:
                self.scan_error = ""
                self.nets = list(value or [])
                self.scroll = 0
            self.scanned = True
        elif kind == "connect":
            ip = value.ip4 if isinstance(value, Connected) else ""
            emit(
                "panel.net_connect_result", ssid=self.ssid, ok=not error,
                error=scrub(error, self.password)[:120] if error else None,
                ip=ip or None, took_ms=took_ms, attempt=self.connect_attempts,
                abandoned=self.phase != "busy" or None,
            )
            if self.phase != "busy":
                return
            if error:
                self.phase = "error"
                self.result_error = self._error_text(error)
                # the password stays for "Zkusit znovu" — one typo shouldn't
                # mean typing it all again; leaving the screens forgets it
            else:
                self.phase = "ok"
                self.result = value if isinstance(value, Connected) else None
                self._forget_password()
            self.next_status = now  # the addresses have probably changed

    def _log_status(self, took_ms: int) -> None:
        """Stav sítě do logu jen při změně (dotaz běží každých 5–30 s)."""
        st = self.status
        if st is None:
            return
        wifi, eth = st.link("wifi"), st.link("ethernet")
        sig = (
            st.error,
            eth.ip4 if eth is not None and eth.up else "",
            wifi.ip4 if wifi is not None and wifi.up else "",
            wifi.ssid if wifi is not None and wifi.up else "",
        )
        if sig == self._status_sig:
            return
        self._status_sig = sig
        emit(
            "panel.net_status", error=st.error or None, eth_ip=sig[1] or None, wifi_ip=sig[2] or None,
            wifi_ssid=sig[3] or None, wifi_signal=wifi.signal if wifi is not None and wifi.up else None,
            mdns=st.mdns, took_ms=took_ms,
        )

    def _error_text(self, code: str) -> str:
        return self.s.get(f"err_{code}", code)

    # ---- navigation ----

    def open(self, now: float) -> None:
        self.page = "overview"
        self.last_touch = now
        self.pressed = None
        self.refresh()

    def close(self) -> None:
        self.page = None
        self.pressed = None
        self._forget_password()
        self.next_status = time.monotonic() + STATUS_EVERY_IDLE

    def _forget_password(self) -> None:
        self.password = ""
        self.show_pw = False

    def _go(self, page: str) -> None:
        self.page = page
        self.pressed = None

    def _open_list(self, rescan: bool) -> None:
        self._go("list")
        self.scan_error = ""
        self._job("scan", lambda: self.backend.scan(rescan))

    def _open_keys(self, net: WifiNet) -> None:
        self.ssid = net.ssid
        self.secure = net.secure
        self._forget_password()
        self.kb_page, self.shift, self.hint = "abc", 0, ""
        if not net.secure:
            self._connect()
            return
        self._go("keys")

    def _connect(self) -> None:
        pw = self.password if self.secure else None
        backend, ssid = self.backend, self.ssid
        self.connect_attempts += 1
        emit("panel.net_connect", ssid=ssid, secure=self.secure, attempt=self.connect_attempts)
        self.phase = "busy"
        self.result = None
        self.result_error = ""
        self.conn_started = time.monotonic()
        self._go("connect")
        self._job("connect", lambda: backend.connect(ssid, pw))

    # ---- view ----

    def urls(self) -> tuple[str, ...]:
        st = self.status
        if st is None:
            return ()
        out = []
        # Wi-Fi first: a phone that scans the code is on Wi-Fi, not on a cable
        for kind in ("wifi", "ethernet"):
            link = st.link(kind)
            if link is not None and link.up:
                url = f"http://{link.ip4}:{self.web_port}"
                if url not in out:
                    out.append(url)
        if out and st.mdns and st.hostname:
            out.append(f"http://{st.hostname}.local:{self.web_port}")
        return tuple(out)

    def icon(self) -> tuple:
        """What the status strip's network button shows."""
        st = self.status
        if st is None:
            return ("?",)
        wifi = st.link("wifi")
        if wifi is not None and wifi.up:
            return ("wifi", wifi.signal)
        eth = st.link("ethernet")
        if eth is not None and eth.up:
            return ("eth",)
        return ("off",)

    def view(self, now: float, note: str = "") -> NetView:
        st = self.status or NetStatus()
        pressed = self.pressed if self.inside else None
        conn_url = ""
        if self.result and self.result.ip4:
            conn_url = f"http://{self.result.ip4}:{self.web_port}"
        can_connect = (not self.secure) or 8 <= len(self.password) <= MAX_PASSWORD
        return NetView(
            page=self.page or "overview",
            pressed=pressed,
            note=note,
            loaded=self.status is not None,
            hostname=st.hostname,
            mdns=st.mdns,
            eth=st.link("ethernet"),
            wifi=st.link("wifi"),
            error=self._error_text(st.error) if st.error else "",
            urls=self.urls(),
            nets=tuple(self.nets),
            scroll=self.scroll,
            scanning="scan" in self.inflight,
            scanned=self.scanned,
            scan_error=self.scan_error,
            ssid=self.ssid,
            password=self.password,
            show_pw=self.show_pw,
            kb_page=self.kb_page,
            shift=self.shift,
            hint=self.hint,
            can_connect=can_connect,
            phase=self.phase,
            elapsed=int(now - self.conn_started) if self.phase == "busy" else 0,
            result_ip=self.result.ip4 if self.result else "",
            result_url=conn_url,
            result_error=self.result_error,
        )

    # ---- timing ----

    def deadline(self, now: float) -> float:
        deadlines = [now + 60.0]
        if not self.inflight & {"status", "connect"}:
            deadlines.append(self.next_status)  # otherwise the result reschedules it
        if self.page == "connect" and self.phase == "busy":
            e = now - self.conn_started
            deadlines.append(now + (int(e) + 1 - e) + 0.005)
        if self.page and self.page != "connect":
            deadlines.append(self.last_touch + IDLE_CLOSE)
        return min(deadlines)

    def timers(self, now: float) -> bool:
        """Periodic work; True when the net screen closed itself."""
        if now >= self.next_status and "status" not in self.inflight and "connect" not in self.inflight:
            self.next_status = now + (STATUS_EVERY if self.page else STATUS_EVERY_IDLE)
            self.refresh()
        if self.page and self.page != "connect" and now - self.last_touch >= IDLE_CLOSE and not self.pressed:
            log.info("síť: nikdo nesahá, zpět na přehrávač")
            emit("panel.net_action", page=self.page, button="idle_close")
            self.close()
            return True
        return False

    # ---- touch ----

    def _targets(self) -> dict[str, Box]:
        return targets(self.view(time.monotonic()), self.lang)

    def touch(self, ev: TouchEvent, now: float) -> None:
        self.last_touch = now
        if ev.kind == "down":
            tg = self._targets()
            name, _ = nearest(tg, ev.x, ev.y)
            self.pressed, self.inside = name, name is not None
            self.press = Press(name, tg[name]) if name else None
            self.press_at = now
            self.last_xy = (ev.x, ev.y)
            if name is None:
                self.count("net_miss")
                cn, dist, dx, dy = closest(tg, ev.x, ev.y)
                emit("panel.touch_miss", page=f"net:{self.page}", x=ev.x, y=ev.y, near=cn, dist=dist,
                     dx=dx, dy=dy)
            if name:
                log.debug("síť: dotyk %s", "klávesa" if self.page == "keys" else name)
        elif ev.kind == "move":
            if not self.pressed:
                return
            self.last_xy = (ev.x, ev.y)
            box = self._targets().get(self.pressed)
            self.inside = box is not None and self.press is not None and self.press.move(ev.x, ev.y, box)
        elif ev.kind == "up":
            name = self.pressed
            self.pressed = None
            if not name:
                return
            inside, self.inside = self.inside, False
            box = self._targets().get(name)
            # counts unless the finger clearly went away (touchpress) — the
            # last positions before a lift drift on resistive glass
            if box is None or not inside:
                self.count("net_slid_out")
                return
            if now - self.press_at < MIN_PRESS:
                self.count("net_too_short")
                return
            if self.page != "keys" or name in LOGGED_KEYS:
                emit(
                    "panel.net_action", page=self.page, button=name,
                    press_ms=int((now - self.press_at) * 1000),
                )
            self._fire(name, now)

    def _fire(self, name: str, now: float) -> None:
        page = self.page
        if page == "overview":
            if name == "back":
                self.close()
            elif name == "wifi_list":
                self._open_list(rescan=False)
        elif page == "list":
            if name == "back":
                self._go("overview")
                self.refresh()
            elif name == "rescan":
                self.scan_error = ""
                self._job("scan", lambda: self.backend.scan(True))
            elif name == "up":
                self.scroll = max(0, self.scroll - SCROLL_STEP)
            elif name == "down":
                if self.scroll + ROWS < len(self.nets):
                    self.scroll = min(self.scroll + SCROLL_STEP, max(0, len(self.nets) - ROWS))
            elif name.startswith("row"):
                k = self.scroll + int(name[3:])
                if k < len(self.nets):
                    self._open_keys(self.nets[k])
        elif page == "keys":
            self._key(name, now)
        elif page == "connect":
            if name == "done":
                self._go("overview")
                self.refresh()
            elif name == "list":
                self._open_list(rescan=False)
            elif name == "retry":
                self.kb_page, self.shift, self.hint = "abc", 0, ""
                if self.secure:
                    self._go("keys")
                else:
                    self._connect()

    def _key(self, name: str, now: float) -> None:
        self.hint = ""
        if name.startswith("c:"):
            if len(self.password) >= MAX_PASSWORD:
                self.hint = self.s["err_bad_password"]
                return
            ch = name[2:]
            if self.kb_page == "abc" and self.shift:
                ch = ch.upper()
                if self.shift == 1:
                    self.shift = 0
            self.password += ch
        elif name == "space":
            if len(self.password) < MAX_PASSWORD:
                self.password += " "
        elif name == "bksp":
            self.password = self.password[:-1]
        elif name == "shift":
            if self.shift == 0:
                self.shift = 1
            elif self.shift == 1 and now - self.shift_at < CAPS_TAP:
                self.shift = 2
            else:
                self.shift = 0
            self.shift_at = now
        elif name == "sym":
            self.kb_page = "#+=" if self.kb_page == "123" else "123"
        elif name == "mode":
            self.kb_page = "123" if self.kb_page == "abc" else "abc"
        elif name == "eye":
            self.show_pw = not self.show_pw
        elif name == "cancel":
            self._forget_password()
            self._go("list")
        elif name == "ok":
            if self.secure and not 8 <= len(self.password) <= MAX_PASSWORD:
                self.hint = self.s["pw_short"]
                return
            self._connect()
