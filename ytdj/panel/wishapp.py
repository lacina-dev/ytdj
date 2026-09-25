"""The wish screens' state machine: quick picks → keyboard → the DJ's answer.

Lives inside `PanelApp` and runs on its main thread, like `NetController`.
A wish goes out on a short-lived thread (a DJ turn takes 20–30 s on the Pi);
its outcome comes back through the app's event queue as ("wish", …). When
the DJ is busy with somebody else's wish (HTTP 409), the thread waits and
tries again, so a wish from the panel is never just refused.
"""

from __future__ import annotations

import http.client
import json
import logging
import threading
import time
from typing import Callable

from .hw import Box, TouchEvent
from .stats import emit
from .wishui import CHIPS, STRINGS, WishRenderer, WishView, targets

log = logging.getLogger(__name__)

MAX_WISH = 120  # characters — a wish, not an essay; the field shows its tail
IDLE_CLOSE = 120.0  # s without a touch on the picks or the keyboard → back to the player
ANSWER_CLOSE = 30.0  # s the DJ's answer stays up before the player comes back
ERROR_CLOSE = 60.0
PROMPT_TIMEOUT = 240.0  # a Codex turn on the Pi can take over a minute
BUSY_RETRY = 2.0  # s between attempts while the DJ works on another wish
BUSY_WAIT_MAX = 150.0  # s of waiting for a busy DJ before giving up
CAPS_TAP = 0.45
TOUCH_SLOP = 14
TOUCH_GRAB = 4
MIN_PRESS = 0.02
SOURCE = "panel"  # who asked — the request queue will show it next to the wish


def post_prompt(api, body: dict, timeout: float = PROMPT_TIMEOUT) -> tuple[int, dict, str]:
    """POST /api/prompt → (HTTP status, JSON body, error). Status 0 = no answer at all."""
    conn = api.connection(timeout)
    try:
        conn.request("POST", api.prefix + "/api/prompt", body=json.dumps(body).encode(),
                     headers={"Content-Type": "application/json", "User-Agent": "ytdj-panel"})
        resp = conn.getresponse()
        raw = resp.read()
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        return resp.status, data, str(data.get("error") or "")
    except (OSError, http.client.HTTPException) as exc:
        return 0, {}, str(exc) or type(exc).__name__
    finally:
        conn.close()


class WishController:
    def __init__(
        self,
        api,
        post: Callable[[tuple], None],
        lang: str = "cs",
        share=None,
        count: Callable[[str], None] | None = None,
        stop: threading.Event | None = None,
    ) -> None:
        self.api = api
        self.post = post
        self.lang = lang if lang in STRINGS else "cs"
        self.s = STRINGS[self.lang]
        self.chips = CHIPS[self.lang]
        self.count = count or (lambda key: None)
        self.stop = stop or threading.Event()
        # made on first use: a panel nobody wishes from doesn't pay for the frame
        self._share = share
        self._renderer: WishRenderer | None = None

        self.page: str | None = None  # None = not shown
        self.last_touch = 0.0
        # keyboard
        self.text = ""
        self.kb_page = "abc"
        self.shift = 0
        self.shift_at = 0.0
        self.hint = ""
        # the wish in flight / its answer
        self.inflight = False
        self.phase = ""
        self.wish = ""
        self.chip = ""  # which quick pick, for the log
        self.sent_at = 0.0
        self.done_at = 0.0
        self.reply = ""
        self.error = ""
        # gesture
        self.pressed: str | None = None
        self.press_at = 0.0
        self.last_xy = (0, 0)
        self.inside = False

    @property
    def renderer(self) -> WishRenderer:
        if self._renderer is None:
            self._renderer = WishRenderer(self.lang, share=self._share)
        return self._renderer

    def invalidate(self) -> None:
        if self._renderer is not None:
            self._renderer.invalidate()

    # ---- navigation ----

    def open(self, now: float) -> None:
        self.last_touch = now
        self.pressed = None
        # a wish still on its way: show how it's doing rather than a blank form
        self.page = "sent" if self.inflight else "home"

    def close(self) -> None:
        self.page = None
        self.pressed = None
        self.hint = ""

    def show_answer(self, now: float) -> None:
        """The answer came while the player was on screen — put it up."""
        self.page = "sent"
        self.pressed = None
        self.last_touch = now

    def _go(self, page: str) -> None:
        self.page = page
        self.pressed = None

    # ---- sending ----

    def send(self, text: str, now: float, chip: str = "") -> None:
        text = " ".join(text.split())[:MAX_WISH]
        if not text:
            return
        if self.inflight:
            self._go("sent")  # one at a time; show the one that's on its way
            return
        self.inflight = True
        self.phase = "busy"
        self.wish, self.chip = text, chip
        self.reply = self.error = ""
        self.sent_at = now
        self._go("sent")
        log.info("přání z panelu (%d znaků%s)", len(text), f", {chip}" if chip else "")
        api, post, stop = self.api, self.post, self.stop

        def run() -> None:
            t0 = time.monotonic()
            waited = False
            while True:
                status, data, error = post_prompt(api, {"text": text, "source": SOURCE})
                if status == 409 and time.monotonic() - t0 < BUSY_WAIT_MAX and not stop.is_set():
                    if not waited:
                        waited = True
                        post(("wish", "wait"))
                    stop.wait(BUSY_RETRY)
                    continue
                break
            post(("wish", "result", status, data, error, int((time.monotonic() - t0) * 1000), waited))

        threading.Thread(target=run, name="panel-wish", daemon=True).start()

    def handle(self, msg: tuple) -> bool:
        """A message from the sending thread; True when the answer has arrived."""
        if msg[1] == "wait":
            if self.inflight:
                self.phase = "wait"
            return False
        _, _, status, data, error, took_ms, waited = msg
        self.inflight = False
        self.done_at = time.monotonic()
        ok = status == 200
        if ok:
            self.phase = "ok"
            self.reply = str(data.get("reply") or "").strip()
            self.text = ""  # sent — the draft is done with
        else:
            self.phase = "error"
            if status == 409:
                self.error = self.s["err_busy"]
            elif status == 0:
                self.error = self.s["err_offline"]
            else:
                self.error = self.s["err_server"].format(e=(error or f"HTTP {status}")[:160])
            log.warning("přání z panelu selhalo: %s %s", status, error)
        emit(
            "panel.wish", ok=ok, status=status, took_ms=took_ms, len=len(self.wish),
            chip=self.chip or None, waited=waited or None, error=None if ok else (error or "")[:120],
        )
        return True

    # ---- view ----

    def view(self, now: float, note: str = "", server_busy: bool = False) -> WishView:
        if self.phase in ("busy", "wait"):
            elapsed = int(now - self.sent_at)
        else:
            elapsed = 0
        return WishView(
            page=self.page or "home",
            pressed=self.pressed if self.inside else None,
            note=note,
            busy_other=server_busy and not self.inflight,
            text=self.text,
            kb_page=self.kb_page,
            shift=self.shift,
            hint=self.hint,
            can_connect=bool(self.text.strip()),
            phase=self.phase,
            wish=self.wish,
            elapsed=elapsed,
            reply=self.reply,
            error=self.error,
        )

    # ---- timing ----

    def deadline(self, now: float) -> float:
        deadlines = [now + 60.0]
        if self.page == "sent" and self.phase in ("busy", "wait"):
            e = now - self.sent_at
            deadlines.append(now + (int(e) + 1 - e) + 0.005)  # the seconds counter
        elif self.page:
            deadlines.append(self._close_at())
        return min(deadlines)

    def _close_at(self) -> float:
        if self.page == "sent":
            if self.phase == "ok":
                return max(self.last_touch, self.done_at) + ANSWER_CLOSE
            if self.phase == "error":
                return max(self.last_touch, self.done_at) + ERROR_CLOSE
            return float("inf")
        return self.last_touch + IDLE_CLOSE

    def timers(self, now: float) -> bool:
        """True when the wish screen closed itself."""
        if self.page and not self.pressed and now >= self._close_at():
            emit("panel.wish_action", page=self.page, button="idle_close")
            self.close()
            return True
        return False

    # ---- touch ----

    def _targets(self) -> dict[str, Box]:
        return targets(self.view(time.monotonic()), self.lang)

    def _hit(self, x: int, y: int, slop: int) -> str | None:
        best, best_d = None, None
        for name, (l, t, r, b) in self._targets().items():
            if l - slop <= x < r + slop and t - slop <= y < b + slop:
                dx = max(l - x, 0, x - (r - 1))
                dy = max(t - y, 0, y - (b - 1))
                dist = dx * dx + dy * dy
                if best_d is None or dist < best_d:
                    best, best_d = name, dist
        return best

    @staticmethod
    def _in(box: Box, x: int, y: int, slop: int) -> bool:
        return box[0] - slop <= x < box[2] + slop and box[1] - slop <= y < box[3] + slop

    def touch(self, ev: TouchEvent, now: float) -> None:
        self.last_touch = now
        if ev.kind == "down":
            name = self._hit(ev.x, ev.y, TOUCH_GRAB)
            self.pressed, self.inside = name, name is not None
            self.press_at = now
            self.last_xy = (ev.x, ev.y)
            if name is None:
                self.count("wish_miss")
        elif ev.kind == "move":
            if not self.pressed:
                return
            self.last_xy = (ev.x, ev.y)
            box = self._targets().get(self.pressed)
            self.inside = box is not None and self._in(box, ev.x, ev.y, TOUCH_SLOP)
        elif ev.kind == "up":
            name = self.pressed
            self.pressed, self.inside = None, False
            if not name:
                return
            x, y = self.last_xy  # release coordinates on resistive glass are junk
            box = self._targets().get(name)
            if box is None or not self._in(box, x, y, TOUCH_SLOP):
                self.count("wish_slid_out")
                return
            if now - self.press_at < MIN_PRESS:
                self.count("wish_too_short")
                return
            if self.page != "keys" or name in ("ok", "cancel"):
                # the letters of a wish don't go to the log key by key
                emit("panel.wish_action", page=self.page, button=name,
                     press_ms=int((now - self.press_at) * 1000))
            self._fire(name, now)

    def _fire(self, name: str, now: float) -> None:
        page = self.page
        if page == "home":
            if name == "back":
                self.close()
            elif name == "field":
                self.kb_page, self.shift, self.hint = "abc", 0, ""
                self._go("keys")
            elif name.startswith("chip"):
                i = int(name[4:])
                if i < len(self.chips):
                    label, text = self.chips[i]
                    self.send(text, now, chip=label)
        elif page == "keys":
            self._key(name, now)
        elif page == "sent":
            if name == "leave":
                self.close()  # the wish carries on; its answer comes back up
            elif name == "done":
                self.close()
            elif name == "again":
                self.text, self.hint = "", ""
                self._go("home")
            elif name == "retry":
                self.send(self.wish, now, chip=self.chip)

    def _key(self, name: str, now: float) -> None:
        self.hint = ""
        if name.startswith("c:"):
            if len(self.text) >= MAX_WISH:
                self.hint = self.s["too_long"].format(n=MAX_WISH)
                return
            ch = name[2:]
            if self.kb_page in ("abc", "áč") and self.shift:
                ch = ch.upper()
                if self.shift == 1:
                    self.shift = 0
            self.text += ch
            if self.kb_page == "áč":
                self.kb_page = "abc"  # one accented letter, then back to the plain ones
        elif name == "space":
            if self.text and not self.text.endswith(" ") and len(self.text) < MAX_WISH:
                self.text += " "
        elif name == "bksp":
            self.text = self.text[:-1]
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
            self.kb_page = "123" if self.kb_page in ("abc", "áč") else "abc"
        elif name == "acc":
            self.kb_page = "abc" if self.kb_page == "áč" else "áč"
        elif name == "cancel":
            self._go("home")  # the draft stays for "Pokračovat"
        elif name == "ok":
            if not self.text.strip():
                self.hint = self.s["empty"]
                return
            self.send(self.text, now)
