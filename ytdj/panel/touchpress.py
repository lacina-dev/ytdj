"""One finger press on a button, decided the way a resistive glass needs it.

What the panel's telemetry showed (Pi, 25.–26. 9.): 347 confirmed touches but
only 141 actions, with `slid_out` among the most frequent rejects. The old
rule — the press counts only if the *last* reported position is still inside
the button (+14 px) — loses presses to the glass itself: the coordinates of a
resting finger wander by ±10–20 px and the last samples before a lift drift
further as the pressure drops.

The rules here:

* the button is chosen at the first stable position (down), the nearest one
  within `GRAB` px, so a touch in the gap between two buttons still counts;
* the finger may wander up to `STICKY` px outside it and the press stays on;
* only a clear drag away cancels it: `CANCEL_SAMPLES` positions in a row more
  than `CANCEL` px outside the button (one wild sample is noise, a lift-off
  drift is the last one or two samples — a finger that slides away produces
  many);
* coming back within `STICKY` px re-arms it, like on a phone.
"""

from __future__ import annotations

from .hw import Box

GRAB = 10  # px around a button that still selects it on down (nearest wins)
STICKY = 28  # px outside the button the finger may wander without losing the press
CANCEL = 44  # px outside the button that, sustained, means "dragged away"
CANCEL_SAMPLES = 2  # consecutive positions beyond CANCEL that cancel the press


def outside(box: Box, x: int, y: int) -> int:
    """How far (px, Chebyshev) the point is outside the box; 0 inside."""
    l, t, r, b = box
    return max(l - x, 0, x - (r - 1), t - y, 0, y - (b - 1))


def nearest(targets: dict[str, Box], x: int, y: int, grab: int = GRAB) -> tuple[str | None, int]:
    """The target under the finger, or the nearest one within `grab` px; (name, distance)."""
    best, best_d = None, None
    for name, box in targets.items():
        d = outside(box, x, y)
        if d <= grab and (best_d is None or d < best_d):
            best, best_d = name, d
    return best, (best_d if best_d is not None else -1)


def closest(targets: dict[str, Box], x: int, y: int) -> tuple[str | None, int, int, int]:
    """For a miss: the closest target, its distance, and the touch's offset from its centre."""
    best, best_d = None, None
    for name, box in targets.items():
        d = outside(box, x, y)
        if best_d is None or d < best_d:
            best, best_d = name, d
    if best is None:
        return None, -1, 0, 0
    dx, dy = centre_offset(targets[best], x, y)
    return best, int(best_d or 0), dx, dy


def centre_offset(box: Box, x: int, y: int) -> tuple[int, int]:
    """Where the finger landed relative to the button's centre — calibration bias shows here."""
    return int(x - (box[0] + box[2]) / 2), int(y - (box[1] + box[3]) / 2)


class Press:
    """The state of one press: which button, and whether it still counts."""

    def __init__(self, name: str, box: Box) -> None:
        self.name = name
        self.box = box
        self.inside = True
        self._far = 0  # consecutive positions beyond CANCEL
        self.max_out = 0  # how far the finger got from the button (for the log)

    def move(self, x: int, y: int, box: Box | None = None) -> bool:
        """A new position; returns whether the press still counts (for the highlight)."""
        if box is not None:
            self.box = box
        d = outside(self.box, x, y)
        self.max_out = max(self.max_out, d)
        if d <= STICKY:
            self._far = 0
            self.inside = True
        elif d > CANCEL:
            self._far += 1
            if self._far >= CANCEL_SAMPLES:
                self.inside = False
        # between STICKY and CANCEL: keep the state — no flicker on the border
        return self.inside


# ---- the button from the whole press, not from the landing ----
#
# Pi, 26. 9. after calibrating: hits on the same button scattered ±30 px
# vertically (play: dy −31…+28), and the owner: "prst mám přes celé −volume,
# ale když jsem moc nahoře, aktivuje se Play". The landing position (the first
# two samples, while the pressure ramps up) is the noisiest part of a press;
# the finger resting afterwards is where it really is.

RAMP = 0.06  # s — the first part of a press, while the pressure builds up
DRAG = 70  # px — the resting part of a press spread wider than this is a drag, not a press


class Gesture:
    """Every position of one press, with the time it came."""

    def __init__(self, x: int, y: int, at: float) -> None:
        self.at = at
        self.pts: list[tuple[float, int, int]] = [(at, x, y)]

    def add(self, x: int, y: int, at: float) -> None:
        self.pts.append((at, x, y))

    def _steady(self) -> list[tuple[float, int, int]]:
        # the last position is the lift-off's (drivers that don't drop it themselves)
        return self.pts[:-1] if len(self.pts) >= 3 else self.pts

    def robust(self) -> tuple[int, int]:
        """Where the finger rested: the median after the pressure ramp, without the lift-off."""
        pts = self._steady()
        late = [p for p in pts if p[0] - self.at >= RAMP]
        use = late if len(late) >= 2 else pts
        xs = sorted(p[1] for p in use)
        ys = sorted(p[2] for p in use)
        return xs[len(xs) // 2], ys[len(ys) // 2]

    def settled(self, now: float) -> bool:
        return now - self.at >= RAMP

    def dragged(self) -> bool:
        pts = self._steady()
        xs = [p[1] for p in pts]
        ys = [p[2] for p in pts]
        return max(max(xs) - min(xs), max(ys) - min(ys)) > DRAG


def decide(targets: dict[str, Box], gesture: Gesture, press: Press | None) -> str | None:
    """The button a finished press means.

    A still press: the button under where the finger rested (the median),
    or, if that's in nobody's area, the one it landed on (if it never left
    it). A drag: only the button it started on, and only if it stayed there.
    """
    if press is None:
        return None
    if gesture.dragged():
        return press.name if press.inside else None
    name, _ = nearest(targets, *gesture.robust())
    if name is None:
        return press.name if press.inside else None
    return name
