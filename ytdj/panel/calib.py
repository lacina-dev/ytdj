"""Touch calibration from the panel itself: "Kalibrace dotyku" on the network screen.

Five crosses (four corners 40 px in, the centre). For each one the finger's
positions while it rests are collected, the first (landing) and the last two
(lifting) are dropped, and their median is where the glass thinks the cross
is. A least-squares affine correction from those points to the crosses goes
on top of the current calibration (`touch.apply_correction`), is saved to the
file the driver loads at start (`/etc/ytdj/panel-touch.json`) and applies at
once. If the points don't fit together (a slipped finger), nothing changes and
it offers to try again.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass

from PIL import Image, ImageDraw

from .hw import Box, TouchEvent
from .stats import emit
from .touchpress import Press, nearest
from .ui import ACCENT, ACCENT_TEXT, BG, DIM, ERR, FAINT, ON_ACCENT, SURFACE, SURFACE_HI, TEXT, H, W, Fonts, wrap

log = logging.getLogger(__name__)

MARGIN = 40
POINTS = ((MARGIN, MARGIN), (W - MARGIN, MARGIN), (W - MARGIN, H - MARGIN), (MARGIN, H - MARGIN), (W // 2, H // 2))
IDLE_CANCEL = 45.0  # s without a touch in the middle of it: give up, change nothing
DONE_CLOSE = 20.0  # s the result stays up

BTN_FULL = (8, 258, 472, 314)
BTN_L = (8, 258, 236, 314)
BTN_R = (244, 258, 472, 314)

STRINGS = {
    "cs": {
        "title": "Kalibrace dotyku",
        "hint": "Ťukni přesně doprostřed křížku a chvilku podrž ({i}/{n})",
        "done": "Hotovo — dotyk je zkalibrovaný",
        "done_detail": "Posun byl až {shift} px, zbývá do {err} px. Platí hned a zůstane i po restartu.",
        "failed": "Body nesedí — nic jsem neměnil",
        "failed_detail": "{why} Zkus to znovu a drž prst na křížku klidně.",
        "unsupported": "Tenhle displej kalibraci neumí.",
        "ok": "Hotovo",
        "again": "Znovu",
        "cancel": "Zrušit",
    },
    "en": {
        "title": "Touch calibration",
        "hint": "Tap the very centre of the cross and hold it a moment ({i}/{n})",
        "done": "Done — touch is calibrated",
        "done_detail": "The shift was up to {shift} px, {err} px remain. It applies now and after a restart.",
        "failed": "The points don't fit — nothing changed",
        "failed_detail": "{why} Try again and keep the finger still on the cross.",
        "unsupported": "This display can't be calibrated.",
        "ok": "Done",
        "again": "Again",
        "cancel": "Cancel",
    },
}


@dataclass(frozen=True)
class CalibView:
    step: int = 0  # which cross (0..4), or len(POINTS) when finished
    phase: str = "points"  # "points" | "done" | "failed"
    detail: str = ""
    pressed: str | None = None
    touching: bool = False


class CalibController:
    def __init__(self, touch, lang: str = "cs", fonts: Fonts | None = None, count=lambda k: None) -> None:
        self.touch = touch
        self.lang = lang
        self.s = STRINGS.get(lang, STRINGS["cs"])
        self.count = count
        self.renderer = CalibRenderer(fonts or Fonts(), self.s)
        self.page: str | None = None  # None = closed, else "calib"
        self.phase = "points"
        self.step = 0
        self.pairs: list[tuple[tuple[int, int], tuple[int, int]]] = []
        self.samples: list[tuple[int, int]] = []
        self.detail = ""
        self.last_touch = 0.0
        self.done_at = 0.0
        self.pressed: str | None = None
        self.press: Press | None = None

    # ---- navigation ----

    def open(self, now: float) -> None:
        self.page = "calib"
        self.phase, self.step, self.pairs, self.samples, self.detail = "points", 0, [], [], ""
        self.last_touch = now
        self.pressed = self.press = None
        emit("panel.calibration", phase="start")

    def close(self) -> None:
        self.page = None
        self.pressed = self.press = None

    def deadline(self, now: float) -> float:
        if not self.page:
            return now + 60.0
        if self.phase == "points":
            return self.last_touch + IDLE_CANCEL
        return max(self.done_at, self.last_touch) + DONE_CLOSE

    def timers(self, now: float) -> None:
        if self.page and not self.pressed and now >= self.deadline(now):
            if self.phase == "points":
                emit("panel.calibration", phase="abandoned", step=self.step)
            self.close()

    def view(self) -> CalibView:
        return CalibView(self.step, self.phase, self.detail, self.pressed, bool(self.samples))

    def _buttons(self) -> dict[str, Box]:
        if self.phase == "done":
            return {"ok": BTN_FULL}
        if self.phase == "failed":
            return {"cancel": BTN_L, "again": BTN_R}
        return {}

    # ---- touch ----

    def touch_event(self, ev: TouchEvent, now: float) -> None:
        self.last_touch = now
        if self.phase == "points":
            self._point(ev, now)
            return
        buttons = self._buttons()
        if ev.kind == "down":
            name, _ = nearest(buttons, ev.x, ev.y)
            self.pressed = name
            self.press = Press(name, buttons[name]) if name else None
        elif ev.kind == "move" and self.press is not None:
            self.pressed = self.press.name if self.press.move(ev.x, ev.y) else None
        elif ev.kind == "up":
            press, self.press, self.pressed = self.press, None, None
            if press is None or not press.inside:
                return
            if press.name == "again":
                self.open(now)
            else:
                self.close()

    def _point(self, ev: TouchEvent, now: float) -> None:
        if ev.kind in ("down", "move"):
            self.samples.append((ev.x, ev.y))
            return
        if ev.kind != "up" or not self.samples:
            return
        pts = self.samples
        # the landing sample and the last two (the finger lifting) are the least trustworthy
        steady = pts[1:-2] if len(pts) >= 5 else pts
        measured = (int(statistics.median(p[0] for p in steady)), int(statistics.median(p[1] for p in steady)))
        self.samples = []
        self.pairs.append((measured, POINTS[self.step]))
        self.step += 1
        if self.step < len(POINTS):
            return
        self._finish(now)

    def _finish(self, now: float) -> None:
        self.done_at = now
        shift = max(max(abs(m[0] - t[0]), abs(m[1] - t[1])) for m, t in self.pairs)
        apply = getattr(self.touch, "apply_correction", None)
        if apply is None:
            self.phase, self.detail = "failed", self.s["unsupported"]
            return
        try:
            err = apply(self.pairs)
        except (ValueError, OSError) as exc:
            self.phase = "failed"
            self.detail = self.s["failed_detail"].format(why=str(exc).capitalize() + ".")
            log.warning("kalibrace dotyku se nepovedla: %s", exc)
            emit("panel.calibration", phase="failed", error=str(exc)[:120],
                 points=[list(m) for m, _ in self.pairs])
            return
        self.phase = "done"
        self.detail = self.s["done_detail"].format(shift=shift, err=max(1, round(err)))
        emit("panel.calibration", phase="saved", shift=shift, error=round(err, 1),
             points=[list(m) for m, _ in self.pairs])


class CalibRenderer:
    """Whole frames — the flow is rare and each step changes most of the screen."""

    def __init__(self, fonts: Fonts, s: dict) -> None:
        self.frame = Image.new("RGB", (W, H), BG)
        self.fonts = fonts
        self.s = s
        self._sig: CalibView | None = None

    def invalidate(self) -> None:
        self._sig = None

    def render(self, v: CalibView, full: bool = False) -> list[Box]:
        if not full and v == self._sig:
            return []
        prev, self._sig = self._sig, v
        d = ImageDraw.Draw(self.frame)
        fs = self.fonts
        n = len(POINTS)
        if (not full and prev is not None and v.phase == prev.phase == "points" and v.step == prev.step
                and v.step < n):
            # only the finger came or went: recolour the cross (instant feedback, a few hundred px)
            return [self._cross(d, v)]
        self.frame.paste(BG, (0, 0, W, H))
        if v.phase == "points":
            step = min(v.step, n - 1)
            hint = self.s["hint"].format(i=step + 1, n=n)
            ty = H // 2 + 44 if step == n - 1 else H // 2 - 40
            d.text((W // 2, ty), self.s["title"], font=fs.status_b, fill=TEXT, anchor="mm")
            for i, line in enumerate(wrap(hint, fs.status, W - 120, 2)):
                d.text((W // 2, ty + 26 + i * 20), line, font=fs.status, fill=DIM, anchor="mm")
            for px, py in POINTS[: v.step]:  # the ones already done, faint
                d.ellipse((px - 3, py - 3, px + 3, py + 3), fill=FAINT)
            self._cross(d, v)
            return [(0, 0, W, H)]
        ok = v.phase == "done"
        d.text((20, 40), self.s["done" if ok else "failed"], font=fs.title_sm, fill=TEXT if ok else ERR, anchor="la")
        for i, line in enumerate(wrap(v.detail, fs.hint, W - 40, 4)):
            d.text((20, 90 + i * 26), line, font=fs.hint, fill=DIM, anchor="la")
        if ok:
            self._btn(d, BTN_FULL, self.s["ok"], v.pressed == "ok", True)
        else:
            self._btn(d, BTN_L, self.s["cancel"], v.pressed == "cancel", False)
            self._btn(d, BTN_R, self.s["again"], v.pressed == "again", True)
        return [(0, 0, W, H)]

    def _cross(self, d, v: CalibView) -> Box:
        x, y = POINTS[min(v.step, len(POINTS) - 1)]
        box = (x - 22, y - 22, x + 23, y + 23)
        d.rectangle((box[0], box[1], box[2] - 1, box[3] - 1), fill=BG)
        color = ACCENT_TEXT if v.touching else TEXT
        if v.touching:
            d.ellipse((x - 20, y - 20, x + 20, y + 20), outline=ACCENT, width=2)
        d.line((x - 18, y, x + 18, y), fill=color, width=3)
        d.line((x, y - 18, x, y + 18), fill=color, width=3)
        d.ellipse((x - 6, y - 6, x + 6, y + 6), outline=ACCENT, width=2)
        return box

    def _btn(self, d, box: Box, label: str, pressed: bool, primary: bool) -> None:
        d.rounded_rectangle(box, radius=14, fill=ACCENT if primary else (SURFACE_HI if pressed else SURFACE))
        color = (ON_ACCENT if not pressed else (255, 250, 240)) if primary else (ACCENT_TEXT if pressed else TEXT)
        d.text(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), label, font=self.fonts.button, fill=color, anchor="mm")
