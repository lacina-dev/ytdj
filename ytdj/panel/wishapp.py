"""The wish screens' state machine: quick picks → keyboard → the DJ's answer, and the queue.

Lives inside `PanelApp` and runs on its main thread, like `NetController`.
A wish goes out on a short-lived thread; the server takes it at once (202 +
id + an owner token) and the DJ works on it in its queue. How it's doing —
the DJ picking, "ve frontě · za ~2 skladby", playing — comes with the status
stream (`state["requests"]`), so the answer screen follows it without asking.
There is no "DJ busy" any more: other people's wishes never block this one.

The panel remembers the tokens of the wishes sent from it: only those get a
× on the queue page, and only those can be put "Hned po téhle".
"""

from __future__ import annotations

import http.client
import json
import logging
import secrets
import threading
import time
from typing import Callable

from .hw import Box, TouchEvent
from .netui import ROWS
from .stats import emit
from .touchpress import Press, closest, nearest
from .ui import vote_mark
from .wishui import ACTIVE, CHIPS, STRINGS, QueueRow, WishRenderer, WishView, targets

log = logging.getLogger(__name__)

MAX_WISH = 120  # characters — a wish, not an essay; the field shows its tail
IDLE_CLOSE = 120.0  # s without a touch on the picks or the keyboard → back to the player
ANSWER_CLOSE = 30.0  # s the DJ's answer stays up before the player comes back
ERROR_CLOSE = 60.0
QUEUE_CLOSE = 60.0
PROMPT_TIMEOUT = 240.0  # the server answers at once (202); an older one only after the turn
CAPS_TAP = 0.45
MIN_PRESS = 0.02
SOURCE = "panel"
PANEL_WHO = "displej"  # the name on wishes typed here, unless somebody picks theirs

STATE_PHASE = {
    "waiting": "busy", "thinking": "busy", "queued": "queued", "playing": "playing",
    "done": "ok", "notfound": "notfound", "error": "error", "removed": "error", "replaced": "ok",
    "skipped": "ok",
}
# klíče čipů pro DJ bez modelu (agent/offline.py), ve stejném pořadí jako CHIPS
CHIP_KEYS = ("more", "other", "czech", "calmer", "livelier", "surprise")


def wish_votes(state: dict) -> dict[str, str]:
    """Request id → the vote marker of the track it plays now or plays next."""
    out: dict[str, str] = {}
    seen: set[str] = set()
    cur = state.get("current")
    if isinstance(cur, dict) and isinstance(cur.get("reason"), dict) and cur["reason"].get("kind") == "wish":
        rid = str(cur["reason"].get("id") or "")
        seen.add(rid)
        mark = vote_mark(cur.get("votes"))
        if mark:
            out[rid] = mark
    for item in state.get("queue") or []:
        if not isinstance(item, dict) or not isinstance(item.get("req"), dict):
            continue
        rid = str(item["req"].get("id") or "")
        if rid and rid not in seen:  # the wish's first track (playing or next) decides
            seen.add(rid)
            mark = vote_mark(item.get("votes"))
            if mark:
                out[rid] = mark
    return out


def post_json(api, path: str, body: dict, timeout: float = PROMPT_TIMEOUT) -> tuple[int, dict, str]:
    """POST → (HTTP status, JSON body, error). Status 0 = no answer at all."""
    conn = api.connection(timeout)
    try:
        conn.request("POST", api.prefix + path, body=json.dumps(body).encode(),
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


def post_prompt(api, body: dict, timeout: float = PROMPT_TIMEOUT) -> tuple[int, dict, str]:
    return post_json(api, "/api/prompt", body, timeout)


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
        self.inflight = False  # the POST is on its way
        self.phase = ""
        self.wish = ""
        self.chip = ""  # which quick pick, for the log
        self.sent_at = 0.0
        self.done_at = 0.0
        self.reply = ""
        self.error = ""
        self.req_id = ""
        self.eta = ""
        self.play_next = False
        self.started = False
        # everybody's wishes (from the status stream) and the panel's own
        self.requests: list[dict] = []
        self.votes: dict[str, str] = {}  # request id → "favourite" | "banned" (its track's votes)
        self.mine: dict[str, str] = {}  # id → owner token
        self.people: list[str] = []
        self.who = PANEL_WHO
        # Kdo je kdo, určuje id klienta, ne jméno: u displeje se lidé střídají,
        # takže každá relace přání (otevření → zavření) je samostatný člověk.
        self.cid: str | None = None
        self.dj_offline = False
        self.scroll = 0
        self.note = ""
        # gesture
        self.pressed: str | None = None
        self.press: Press | None = None  # the press in progress (touchpress rules)
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

    # ---- the server's view ----

    def active_count(self) -> int:
        return sum(1 for r in self.requests if r.get("state") in ACTIVE)

    def on_state(self, state: dict) -> bool:
        """New status snapshot; True when the DJ has just decided about our wish."""
        reqs = state.get("requests")
        if isinstance(reqs, list):
            self.requests = [r for r in reqs if isinstance(r, dict)]
        self.dj_offline = bool(state.get("dj_offline"))
        self.votes = wish_votes(state)
        people = state.get("people")
        if isinstance(people, list):
            self.people = [str(p) for p in people if isinstance(p, str)][:6]
        if not self.req_id:
            return False
        r = next((x for x in self.requests if x.get("id") == self.req_id), None)
        if r is None:
            return False
        before = self.phase
        st = str(r.get("state") or "")
        phase = STATE_PHASE.get(st, before)
        self.eta = str(r.get("eta") or "")
        self.play_next = bool(r.get("play_next"))
        self.started = self.started or st in ("playing", "done")
        reply = str(r.get("reply") or "").strip()
        if phase == "error":
            self.error = self.s["removed"] if st == "removed" else (reply or self.s["err_server"].format(e="?"))
        elif reply:
            self.reply = reply
        if phase != before:
            self.phase = phase
            if before == "busy":
                self.done_at = time.monotonic()
                self.text = ""  # decided — the draft is done with
                emit("panel.wish", ok=phase not in ("error", "notfound"), state=st,
                     took_ms=int((time.monotonic() - self.sent_at) * 1000), len=len(self.wish),
                     chip=self.chip or None, id=self.req_id)
                return True
        return False

    def queue_rows(self) -> tuple[QueueRow, ...]:
        rows = []
        for r in self.requests:
            st = str(r.get("state") or "")
            label = str(r.get("state_cs") or st)
            if st == "queued" and r.get("eta"):
                label += f" · {r['eta']}"
            if r.get("play_next") and st in ACTIVE:
                label += " · hned"
            if st == "skipped" and r.get("skipped_by"):
                label += f" · přeskočil {r['skipped_by']}"
            elif st in ("done", "notfound", "error") and r.get("reply"):
                label += f" · {r['reply']}"
            if r.get("restored") and st in ACTIVE:
                label += " · obnoveno po restartu"
            rows.append(QueueRow(id=str(r.get("id") or ""), who=str(r.get("who") or "?"),
                                 text=str(r.get("text") or ""), state=st, label=label,
                                 mine=str(r.get("id") or "") in self.mine,
                                 vote=self.votes.get(str(r.get("id") or ""), "") if st in ACTIVE else ""))
        return tuple(rows)

    # ---- navigation ----

    def open(self, now: float) -> None:
        self.last_touch = now
        self.pressed = None
        # a wish still on its way: show how it's doing rather than a blank form
        self.page = "sent" if self.phase == "busy" else "home"

    def open_queue(self, now: float) -> None:
        self.last_touch = now
        self.pressed = None
        self.scroll = 0
        self.page = "queue"

    def close(self) -> None:
        self.page = None
        self.pressed = None
        self.hint = ""
        # další u displeje je jiný člověk: jméno zpět na "displej", nová relace
        self.who = PANEL_WHO
        self.cid = None
        self.note = ""

    def show_answer(self, now: float) -> None:
        """The answer came while the player was on screen — put it up."""
        self.page = "sent"
        self.pressed = None
        self.last_touch = now

    def _go(self, page: str) -> None:
        self.page = page
        self.pressed = None

    # ---- sending ----

    def send(self, text: str, now: float, chip: str = "", chip_key: str = "") -> None:
        text = " ".join(text.split())[:MAX_WISH]
        if not text:
            return
        if self.inflight:
            self._go("sent")  # the POST is on its way — a split second
            return
        self.inflight = True
        self.phase = "busy"
        self.wish, self.chip = text, chip
        self.reply = self.error = self.eta = ""
        self.req_id = ""
        self.play_next = self.started = False
        self.sent_at = now
        self._go("sent")
        log.info("přání z panelu (%d znaků%s, %s)", len(text), f", {chip}" if chip else "", self.who)
        api, post = self.api, self.post
        if self.cid is None:
            self.cid = "panel-" + secrets.token_hex(6)
        body = {"text": text, "source": SOURCE, "who": self.who, "play_next": False, "wait": False,
                "client": self.cid}
        if chip_key:
            body["chip"] = chip_key  # nálada funguje i bez mozku DJe

        def run() -> None:
            t0 = time.monotonic()
            status, data, error = post_prompt(api, body)
            post(("wish", "result", status, data, error, int((time.monotonic() - t0) * 1000)))

        threading.Thread(target=run, name="panel-wish", daemon=True).start()

    def _action(self, rid: str, action: str) -> None:
        token = self.mine.get(rid)
        if not token:
            return
        api, post = self.api, self.post

        def run() -> None:
            status, data, error = post_json(api, f"/api/requests/{rid}", {"action": action, "token": token}, 10.0)
            post(("wish", "action", action, rid, status, data, error))

        threading.Thread(target=run, name="panel-wish-action", daemon=True).start()

    def handle(self, msg: tuple) -> bool:
        """A message from a sending thread; True when there is an answer to show."""
        if msg[1] == "action":
            _, _, action, rid, status, data, error = msg
            ok = status == 200
            emit("panel.wish_request", action=action, ok=ok, status=status, error=None if ok else error[:120])
            if ok and action == "next" and rid == self.req_id:
                self.play_next = True
            if not ok:
                self.note = (error or f"HTTP {status}")[:60]
            return False
        _, _, status, data, error, took_ms = msg[:6]
        self.inflight = False
        if status == 202 and data.get("id"):
            # accepted; the rest comes with the status stream
            self.req_id = str(data["id"])
            if data.get("token"):
                self.mine[self.req_id] = str(data["token"])
            emit("panel.wish_sent", ok=True, status=status, took_ms=took_ms, len=len(self.wish),
                 chip=self.chip or None, id=self.req_id)
            return False
        self.done_at = time.monotonic()
        ok = status == 200
        if ok:
            # an older server answered only once the DJ was done — or a plain
            # command ("další", "hlasitěji") that needed no DJ at all
            self.phase = "ok"
            self.reply = str(data.get("reply") or "").strip()
            self.text = ""
        else:
            self.phase = "error"
            if status == 0:
                self.error = self.s["err_offline"]
            elif status == 409:
                self.error = self.s["err_busy"]
            elif status == 429:
                self.error = (error or "")[:160]
            else:
                self.error = self.s["err_server"].format(e=(error or f"HTTP {status}")[:160])
            log.warning("přání z panelu selhalo: %s %s", status, error)
        emit(
            "panel.wish", ok=ok, status=status, took_ms=took_ms, len=len(self.wish),
            chip=self.chip or None, error=None if ok else (error or "")[:120],
        )
        return True

    # ---- view ----

    def names(self) -> list[str]:
        """Who can be picked on the panel: the panel itself and the recent names."""
        out = [PANEL_WHO]
        for p in self.people:
            if p not in out:
                out.append(p)
        return out[:7]

    def view(self, now: float, note: str = "", server_busy: bool = False) -> WishView:
        elapsed = int(now - self.sent_at) if self.phase == "busy" else 0
        rows = self.queue_rows() if self.page == "queue" else ()
        return WishView(
            page=self.page or "home",
            pressed=self.pressed if self.inside else None,
            note=note or self.note,
            offline=self.dj_offline,
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
            eta=self.eta,
            can_next=self.phase == "queued" and not self.play_next and not self.started
            and self.req_id in self.mine,
            who=self.who,
            count=self.active_count(),
            rows=rows,
            scroll=self.scroll,
        )

    # ---- timing ----

    def deadline(self, now: float) -> float:
        deadlines = [now + 60.0]
        if self.page == "sent" and self.phase == "busy":
            e = now - self.sent_at
            deadlines.append(now + (int(e) + 1 - e) + 0.005)  # the seconds counter
        elif self.page:
            deadlines.append(self._close_at())
        return min(deadlines)

    def _close_at(self) -> float:
        if self.page == "sent":
            if self.phase in ("queued", "playing", "ok", "notfound"):
                return max(self.last_touch, self.done_at) + ANSWER_CLOSE
            if self.phase == "error":
                return max(self.last_touch, self.done_at) + ERROR_CLOSE
            return float("inf")
        if self.page == "queue":
            return self.last_touch + QUEUE_CLOSE
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
                self.count("wish_miss")
                cn, dist, dx, dy = closest(tg, ev.x, ev.y)
                emit("panel.touch_miss", page=f"wish:{self.page}", x=ev.x, y=ev.y, near=cn, dist=dist,
                     dx=dx, dy=dy)
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
        self.note = ""
        if page == "home":
            if name == "back":
                self.close()
            elif name == "field":
                self.kb_page, self.shift, self.hint = "abc", 0, ""
                self._go("keys")
            elif name == "who":
                names = self.names()
                i = names.index(self.who) if self.who in names else -1
                self.who = names[(i + 1) % len(names)]
            elif name == "queue":
                self.open_queue(now)
            elif name.startswith("chip"):
                i = int(name[4:])
                if i < len(self.chips):
                    label, text = self.chips[i]
                    self.send(text, now, chip=label,
                              chip_key=CHIP_KEYS[i] if i < len(CHIP_KEYS) else "")
        elif page == "keys":
            self._key(name, now)
        elif page == "queue":
            rows = self.queue_rows()
            if name == "back":
                self.close()
            elif name == "up":
                self.scroll = max(0, self.scroll - ROWS)
            elif name == "down":
                if self.scroll + ROWS < len(rows):
                    self.scroll += ROWS
            elif name.startswith("rm"):
                k = self.scroll + int(name[2:])
                if k < len(rows) and rows[k].mine:
                    self._action(rows[k].id, "remove")
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
            elif name == "next" and self.req_id:
                self.play_next = True  # optimistic; the stream confirms it
                self._action(self.req_id, "next")

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
