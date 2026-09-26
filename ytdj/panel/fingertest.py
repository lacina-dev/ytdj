"""Test prstem: 12 marked spots tapped with a finger, every sample logged (panel.touch_probe).

The owner (26. 9. evening): "tužka dobrá všude i úplně nahoře, prstem horší,
úplně horní část displeje opravdu problém — dotek se většinou ukázal až na
Frontě uprostřed". To see why (two contacts — finger and the bezel — that
the resistive layer averages? pressure-dependent drift near the edge?), each
tap logs the target, every raw converter sample of the press (x, y, Z1, Z2,
spread, and why a sample was refused), the calibrated positions, where the
press was decided and which button that would press. Nothing is pressed.
About a minute; "python3 -m ytdj.panel.touchreport --probes" reads it.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from PIL import Image, ImageDraw

from .hw import Box, TouchEvent
from .stats import emit
from .touchpress import Gesture, Press, decide, nearest
from .ui import ACCENT, ACCENT_TEXT, BG, DIM, FAINT, ON_ACCENT, SURFACE, SURFACE_HI, TARGETS, TEXT, H, W, Fonts, wrap

# 4 on the very top edge (where Mobil, Přání and Síť are), corners, middle, bottom
SPOTS = (
    (40, 10), (282, 10), (366, 10), (451, 10),
    (240, 64), (30, 160), (240, 160), (450, 160),
    (120, 230), (30, 300), (240, 300), (450, 300),
)
IDLE_CLOSE = 60.0
DONE_CLOSE = 30.0
BTN_DONE = (8, 258, 472, 314)

STRINGS = {
    "cs": {
        "title": "Test prstem",
        "hint": "Polož PRST (ne tužku) doprostřed kroužku, chvilku podrž a pusť ({i}/{n})",
        "done": "Hotovo, díky!",
        "summary": "Nahoře u okraje byl dotyk v průměru o {top} px vedle, jinde o {rest} px. "
                   "Všechny vzorky jsou v logu (panel.touch_probe).",
        "ok": "Hotovo",
    },
    "en": {
        "title": "Finger test",
        "hint": "Put a FINGER (not a stylus) on the ring, hold a moment and lift ({i}/{n})",
        "done": "Done, thanks!",
        "summary": "At the top edge the touch was {top} px off on average, elsewhere {rest} px. "
                   "Every sample is in the log (panel.touch_probe).",
        "ok": "Done",
    },
}


@dataclass(frozen=True)
class ProbeView:
    step: int = 0
    phase: str = "spots"  # "spots" | "done"
    touching: bool = False
    detail: str = ""
    pressed: bool = False


class FingerTestController:
    def __init__(self, touch, lang: str = "cs", fonts: Fonts | None = None) -> None:
        self.touch = touch
        self.s = STRINGS.get(lang, STRINGS["cs"])
        self.renderer = FingerTestRenderer(fonts or Fonts(), self.s)
        self.page: str | None = None
        self.phase = "spots"
        self.step = 0
        self.errors: list[tuple[int, float]] = []
        self.detail = ""
        self.last_touch = self.done_at = 0.0
        self.pressed: str | None = None
        self.gesture: Gesture | None = None
        self.press: Press | None = None
        self.landed: str | None = None
        self.events: list[list[int]] = []

    # ---- navigation ----

    def open(self, now: float) -> None:
        self.page, self.phase, self.step, self.errors, self.detail = "fingertest", "spots", 0, [], ""
        self.last_touch = now
        self.pressed = self.gesture = self.press = None
        start = getattr(self.touch, "start_recording", None)
        if callable(start):
            start()
        emit("panel.touch_probe_run", phase="start", spots=len(SPOTS),
             calibration=getattr(self.touch, "calibration_source", None))

    def close(self) -> None:
        if self.page and self.phase == "spots":
            emit("panel.touch_probe_run", phase="abandoned", step=self.step)
        stop = getattr(self.touch, "stop_recording", None)
        if callable(stop):
            stop()
        self.page = None
        self.pressed = self.gesture = self.press = None

    def deadline(self, now: float) -> float:
        if not self.page:
            return now + 60.0
        if self.phase == "spots":
            return self.last_touch + IDLE_CLOSE
        return max(self.last_touch, self.done_at) + DONE_CLOSE

    def timers(self, now: float) -> None:
        if self.page and not self.pressed and now >= self.deadline(now):
            self.close()

    def view(self) -> ProbeView:
        return ProbeView(self.step, self.phase, self.gesture is not None, self.detail, self.pressed == "done")

    # ---- touch ----

    def touch_event(self, ev: TouchEvent, now: float) -> None:
        self.last_touch = now
        if self.phase == "done":
            if ev.kind == "down":
                name, _ = nearest({"done": BTN_DONE}, ev.x, ev.y)
                self.pressed = name
                self.press = Press(name, BTN_DONE) if name else None
            elif ev.kind == "move" and self.press is not None:
                self.pressed = "done" if self.press.move(ev.x, ev.y) else None
            elif ev.kind == "up":
                press, self.press, self.pressed = self.press, None, None
                if press is not None and press.inside:
                    self.close()
            return
        if ev.kind == "down":
            # (the driver's samples since the previous lift are taken at this press's lift:
            # they include the landing ones, which come before the "down")
            self.gesture = Gesture(ev.x, ev.y, now)
            self.landed, _ = nearest(TARGETS, ev.x, ev.y)
            self.press = Press(self.landed, TARGETS[self.landed]) if self.landed else None
            self.events = [[0, ev.x, ev.y]]
            self.pressed = "spot"
        elif ev.kind == "move" and self.gesture is not None:
            self.gesture.add(ev.x, ev.y, now)
            if self.press is not None:
                self.press.move(ev.x, ev.y)
            self.events.append([int((now - self.gesture.at) * 1000), ev.x, ev.y])
        elif ev.kind == "up" and self.gesture is not None:
            self._probe(now)

    def _probe(self, now: float) -> None:
        g = self.gesture
        self.gesture, self.pressed = None, None
        take = getattr(self.touch, "take_recording", None)
        samples = take() if callable(take) else []
        tx, ty = SPOTS[self.step]
        rx, ry = g.robust()
        button = decide(TARGETS, g, self.press) if self.press is not None else nearest(TARGETS, rx, ry)[0]
        aim, _ = nearest(TARGETS, tx, ty, grab=0)
        err = ((rx - tx) ** 2 + (ry - ty) ** 2) ** 0.5
        self.errors.append((self.step, err))
        emit("panel.touch_probe", step=self.step, target=[tx, ty], aim=aim, landed=self.landed,
             decided=[rx, ry], err=[rx - tx, ry - ty], button=button,
             press_ms=int((now - g.at) * 1000), events=self.events[:200], samples=samples)
        self.step += 1
        if self.step >= len(SPOTS):
            self._finish(now)

    def _finish(self, now: float) -> None:
        self.phase, self.done_at = "done", now
        stop = getattr(self.touch, "stop_recording", None)
        if callable(stop):
            stop()
        top = [e for i, e in self.errors if SPOTS[i][1] <= 20]
        rest = [e for i, e in self.errors if SPOTS[i][1] > 20]
        self.detail = self.s["summary"].format(top=round(statistics.mean(top)) if top else 0,
                                               rest=round(statistics.mean(rest)) if rest else 0)
        emit("panel.touch_probe_run", phase="done", top_err=round(statistics.mean(top), 1) if top else None,
             rest_err=round(statistics.mean(rest), 1) if rest else None)


class FingerTestRenderer:
    def __init__(self, fonts: Fonts, s: dict) -> None:
        self.frame = Image.new("RGB", (W, H), BG)
        self.fonts = fonts
        self.s = s
        self._last: ProbeView | None = None

    def invalidate(self) -> None:
        self._last = None

    def render(self, v: ProbeView, full: bool = False) -> list[Box]:
        prev = self._last
        if not full and v == prev:
            return []
        self._last = v
        d = ImageDraw.Draw(self.frame)
        if (not full and prev is not None and v.phase == prev.phase == "spots" and v.step == prev.step):
            return [self._spot(d, v)]  # only the finger came or went
        self.frame.paste(BG, (0, 0, W, H))
        fs = self.fonts
        if v.phase == "spots":
            n = len(SPOTS)
            x, y = SPOTS[min(v.step, n - 1)]
            ty = 200 if 40 < y < 130 else (80 if 130 <= y <= 190 else 120)
            d.text((W // 2, ty), self.s["title"], font=fs.status_b, fill=TEXT, anchor="mm")
            for i, line in enumerate(wrap(self.s["hint"].format(i=v.step + 1, n=n), fs.status, W - 120, 2)):
                d.text((W // 2, ty + 26 + i * 20), line, font=fs.status, fill=DIM, anchor="mm")
            for px, py in SPOTS[: v.step]:
                d.ellipse((px - 3, py - 3, px + 3, py + 3), fill=FAINT)
            self._spot(d, v)
            return [(0, 0, W, H)]
        d.text((20, 40), self.s["done"], font=fs.title_sm, fill=TEXT, anchor="la")
        for i, line in enumerate(wrap(v.detail, fs.hint, W - 40, 5)):
            d.text((20, 90 + i * 26), line, font=fs.hint, fill=DIM, anchor="la")
        d.rounded_rectangle(BTN_DONE, radius=14, fill=ACCENT if not v.pressed else SURFACE_HI)
        d.text(((BTN_DONE[0] + BTN_DONE[2]) / 2, (BTN_DONE[1] + BTN_DONE[3]) / 2), self.s["ok"],
               font=fs.button, fill=ON_ACCENT if not v.pressed else ACCENT_TEXT, anchor="mm")
        return [(0, 0, W, H)]

    def _spot(self, d, v: ProbeView) -> Box:
        x, y = SPOTS[min(v.step, len(SPOTS) - 1)]
        box = (max(0, x - 24), max(0, y - 24), min(W, x + 25), min(H, y + 25))
        d.rectangle((box[0], box[1], box[2] - 1, box[3] - 1), fill=BG)
        d.ellipse((x - 20, y - 20, x + 20, y + 20), outline=ACCENT_TEXT if v.touching else TEXT, width=3)
        d.ellipse((x - 4, y - 4, x + 4, y + 4), fill=ACCENT if v.touching else SURFACE)
        return box
