"""Test dotyku: where the panel reads the finger, live, over the player's buttons.

From the network screen. The player's touch areas are drawn faintly with
their names; a crosshair follows the reading while the finger is down, the
area under it lights up, and after the lift a ring marks where the press was
decided (touchpress: the median after the pressure ramp) and which button
that would have pressed. Nothing is pressed here. "Zpět" (in the cover
slot) leaves; 60 s without a touch leaves too.

Cheap on the glass: only the old and new crosshair, the lit area's outline
and the info line are sent, never the whole frame after the first one.
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageDraw

from .hw import Box, TouchEvent
from .stats import emit
from .touchpress import Gesture, Press, decide, nearest
from .ui import (ACCENT, ACCENT_TEXT, ART, BG, DIM, FAINT, LINE, NEXT, PLAY, SURFACE, SURFACE_HI, TARGETS,
                 TEXT, VOL, VOL_DOWN, VOL_UP, H, W, Fonts, ellipsize, merge_boxes)

IDLE_CLOSE = 60.0
BACK_BOX = (ART[0] + 8, ART[1] + 6, ART[0] + 152, ART[1] + 150)  # the cover slot
INFO = (0, 0, 240, 30)

NAMES = {
    "cs": {"play": "Hrát", "next": "Další", "vol_down": "−", "vol": "hlasitost", "vol_up": "+",
           "net": "Síť", "wish": "Přání", "phone": "Mobil", "queue": "Fronta", "back": "Zpět"},
    "en": {"play": "Play", "next": "Next", "vol_down": "−", "vol": "volume", "vol_up": "+",
           "net": "Network", "wish": "Wish", "phone": "Phone", "queue": "Queue", "back": "Back"},
}
STRINGS = {
    "cs": {"hint": "Ťukej a sleduj křížek", "reading": "{x}, {y} → {name}",
           "decided": "stisk → {name} ({x}, {y})", "none": "nic"},
    "en": {"hint": "Tap and watch the cross", "reading": "{x}, {y} → {name}",
           "decided": "press → {name} ({x}, {y})", "none": "nothing"},
}


@dataclass(frozen=True)
class TestView:
    cursor: tuple[int, int] | None = None  # the live reading while the finger is down
    decided: tuple[int, int] | None = None  # where the last press was decided
    lit: str | None = None  # the area under the finger / of the decided press
    info: str = ""
    back_pressed: bool = False


class TouchTestController:
    def __init__(self, lang: str = "cs", fonts: Fonts | None = None) -> None:
        self.names = NAMES.get(lang, NAMES["cs"])
        self.s = STRINGS.get(lang, STRINGS["cs"])
        self.renderer = TouchTestRenderer(fonts or Fonts(), self.names, self.s)
        self.page: str | None = None
        self.last_touch = 0.0
        self.pressed: str | None = None
        self._view = TestView()
        self.gesture: Gesture | None = None
        self.press: Press | None = None
        self.taps = 0

    def targets(self) -> dict[str, Box]:
        return {"back": BACK_BOX, **TARGETS}

    def open(self, now: float) -> None:
        self.page = "touchtest"
        self.last_touch = now
        self._view = TestView(info=self.s["hint"])
        self.taps = 0
        emit("panel.touch_test", phase="start")

    def close(self) -> None:
        if self.page:
            emit("panel.touch_test", phase="end", taps=self.taps)
        self.page = None
        self.pressed = self.gesture = self.press = None

    def deadline(self, now: float) -> float:
        return self.last_touch + IDLE_CLOSE if self.page else now + 60.0

    def timers(self, now: float) -> None:
        if self.page and not self.pressed and now >= self.last_touch + IDLE_CLOSE:
            self.close()

    def view(self) -> TestView:
        return self._view

    def touch_event(self, ev: TouchEvent, now: float) -> None:
        self.last_touch = now
        tg = self.targets()
        if ev.kind == "down":
            name, _ = nearest(tg, ev.x, ev.y)
            self.gesture = Gesture(ev.x, ev.y, now)
            self.press = Press(name, tg[name]) if name else None
            self.pressed = name or "?"
        elif ev.kind == "move" and self.gesture is not None:
            self.gesture.add(ev.x, ev.y, now)
            if self.press is not None:
                self.press.move(ev.x, ev.y)
        elif ev.kind == "up":
            g, press = self.gesture, self.press
            self.gesture = self.press = None
            self.pressed = None
            if g is None:
                return
            self.taps += 1
            final = decide(tg, g, press)
            rx, ry = g.robust()
            if final == "back":
                self.close()
                return
            self._view = TestView(decided=(rx, ry), lit=final,
                                  info=self.s["decided"].format(name=self.names.get(final, self.s["none"]),
                                                                x=rx, y=ry))
            return
        if self.gesture is not None:
            x, y = (ev.x, ev.y)
            name, _ = nearest(tg, x, y)
            self._view = TestView(cursor=(x, y), decided=self._view.decided if ev.kind == "move" else None,
                                  lit=name, back_pressed=name == "back",
                                  info=self.s["reading"].format(x=x, y=y, name=self.names.get(name, self.s["none"])))


class TouchTestRenderer:
    def __init__(self, fonts: Fonts, names: dict, s: dict) -> None:
        self.fonts = fonts
        self.names = names
        self.s = s
        self.base = self._base()
        self.frame = self.base.copy()
        self._last: TestView | None = None

    def _areas(self) -> dict[str, Box]:
        return {"back": BACK_BOX, **TARGETS}

    def _base(self) -> Image.Image:
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        # the drawn buttons, very faint, and the touch areas as outlines with their names
        for box in (PLAY, NEXT, VOL_DOWN, VOL, VOL_UP):
            d.rounded_rectangle(box, radius=14, fill=(22, 24, 27))
        for name, box in self._areas().items():
            if name == "back":
                d.rounded_rectangle(box, radius=14, fill=SURFACE)
                d.text(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), self.names["back"], font=self.fonts.button,
                       fill=TEXT, anchor="mm")
                continue
            d.rectangle((box[0], box[1], box[2] - 1, box[3] - 1), outline=LINE, width=1)
            d.text(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), self.names.get(name, name),
                   font=self.fonts.status, fill=FAINT, anchor="mm")
        return img

    def invalidate(self) -> None:
        self._last = None

    def _outline(self, name: str | None) -> Box | None:
        box = self._areas().get(name or "")
        return box

    def render(self, v: TestView, full: bool = False) -> list[Box]:
        prev = self._last
        if not full and v == prev:
            return []
        self._last = v
        self.frame = self.base.copy()
        d = ImageDraw.Draw(self.frame)
        lit = self._outline(v.lit)
        if lit is not None:
            if v.lit == "back":
                d.rounded_rectangle(lit, radius=14, fill=SURFACE_HI, outline=ACCENT, width=3)
                d.text(((lit[0] + lit[2]) / 2, (lit[1] + lit[3]) / 2), self.names["back"], font=self.fonts.button,
                       fill=ACCENT_TEXT, anchor="mm")
            else:
                d.rectangle((lit[0], lit[1], lit[2] - 1, lit[3] - 1), outline=ACCENT, width=3)
        if v.decided is not None:
            x, y = v.decided
            d.ellipse((x - 12, y - 12, x + 12, y + 12), outline=ACCENT_TEXT, width=3)
        if v.cursor is not None:
            x, y = v.cursor
            d.line((x - 16, y, x + 16, y), fill=TEXT, width=2)
            d.line((x, y - 16, x, y + 16), fill=TEXT, width=2)
            d.ellipse((x - 3, y - 3, x + 3, y + 3), fill=ACCENT)
        d.rectangle(INFO, fill=BG)
        d.text((10, 15), ellipsize(v.info, self.fonts.status_b, INFO[2] - 20), font=self.fonts.status_b,
               fill=DIM, anchor="lm")
        if full or prev is None:
            return [(0, 0, W, H)]
        # only what changed: the crosshairs, the rings, the lit outlines, the info line
        boxes = [INFO] if v.info != prev.info else []
        for view in (prev, v):
            for pt, old, r in ((view.cursor, prev.cursor != v.cursor, 18), (view.decided, prev.decided != v.decided, 14)):
                if pt is not None and old:
                    boxes.append((max(0, pt[0] - r), max(0, pt[1] - r), min(W, pt[0] + r + 1), min(H, pt[1] + r + 1)))
            box = self._outline(view.lit)
            if box is not None and (prev.lit != v.lit or prev.back_pressed != v.back_pressed):
                boxes.extend([box] if view.lit == "back" else _edges(box))
        return merge_boxes(boxes, slack=600)


def _edges(box: Box, w: int = 3) -> list[Box]:
    l, t, r, b = box
    return [(l, t, r, t + w), (l, b - w, r, b), (l, t, l + w, b), (r - w, t, r, b)]
