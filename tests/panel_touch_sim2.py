"""Round 5: the button from the landing vs. from the whole press, on the Pi's logged hit distributions.

    python3 tests/panel_touch_sim2.py

NOT a measurement — a Monte Carlo model built from the `off` values logged on
the Pi after the 16:42 calibration (26. 9.): per button the median shift of
hits from the button's centre and their spread. Each simulated press rests
at centre + shift + N(0, spread); the first two samples (the pressure ramp)
are scattered more (σ 14 px), the resting ones by σ 5 px, the last one before
the lift drifts. The samples go through the real driver (`kedei.KedeiTouch`)
and then through
  * the round-4 rule — the button is where the finger *landed* (first stable
    samples), kept unless the finger clearly slid away (touchpress.Press);
  * the round-5 rule — the button is where the finger *rested* (median after
    the ramp, without the lift-off; touchpress.Gesture + decide).
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ytdj.panel import kedei  # noqa: E402
from ytdj.panel.touchpress import Gesture, Press, decide, nearest  # noqa: E402
from ytdj.panel.ui import NET_BTN, NEXT, PLAY, TARGETS, VOL_DOWN, VOL_UP, WISH_BTN  # noqa: E402

P = 4000
DT = 0.015
SIGMA_PRESS = math.log(947 / 162) / 1.2816

# button: (drawn box it's aimed at, median shift dx, dy, spread σ) — from the logged `off` (Pi, 26. 9. 16:42–)
LOGGED = {
    "vol_down": (VOL_DOWN, 12, -23, 9),   # n 5, dy −30…+3
    "vol_up": (VOL_UP, -12, 8, 5),        # n 7, dy +2…+19
    "play": (PLAY, -5, -5, 15),           # n 6, dy −31…+28
    "next": (NEXT, -2, 19, 10),           # n 1
    "wish": (WISH_BTN, 2, -16, 8),        # n 2
    "net": (NET_BTN, 7, -1, 8),           # n 2
}


class Dev:
    def __init__(self, samples):
        self.samples = list(samples)
        self.pen = False
        self.used = 0

    def init(self, madctl):
        pass

    def touch_raw(self):
        self.used += 1
        item = self.samples.pop(0) if self.samples else None
        self.pen = item is not None
        return item

    def pen_down(self):
        return self.pen


def press(rng, rest):
    rx, ry = rest
    n = max(4, int(162 * math.exp(rng.gauss(0, SIGMA_PRESS)) / 1000 / DT))
    out = []
    for i in range(n):
        s = 14 if i < 2 else 5
        out.append((max(0, round(rx + rng.gauss(0, s))), max(0, round(ry + rng.gauss(0, s))), P))
    a = rng.uniform(0, 2 * math.pi)
    d = abs(rng.gauss(0, 25))
    out.append((max(0, round(rx + d * math.cos(a))), max(0, round(ry + d * math.sin(a))), P))
    return out + [None] * 4


def events(samples):
    dev = Dev(samples)
    kedei._Device._instance = dev
    t = kedei.KedeiTouch(calibration=kedei.Calibration(1, 0, 0, 0, 1, 0))
    t.DOWN_INTERVAL = t.IDLE_INTERVAL = 0.0
    out = []
    for _ in range(len(samples) + 4):
        ev = t.poll(0.0)
        if ev:
            out.append((ev, dev.used * DT))
    return out


def landing_rule(evs):
    press_ = None
    for ev, _ in evs:
        if ev.kind == "down":
            name, _ = nearest(TARGETS, ev.x, ev.y)
            press_ = Press(name, TARGETS[name]) if name else None
        elif ev.kind == "move" and press_:
            press_.move(ev.x, ev.y)
        elif ev.kind == "up":
            return press_.name if press_ and press_.inside else None
    return None


def resting_rule(evs):
    g = press_ = None
    for ev, at in evs:
        if ev.kind == "down":
            name, _ = nearest(TARGETS, ev.x, ev.y)
            g = Gesture(ev.x, ev.y, at)
            press_ = Press(name, TARGETS[name]) if name else Press("none", (0, 0, 0, 0))
        elif ev.kind == "move" and g:
            g.add(ev.x, ev.y, at)
            press_.move(ev.x, ev.y)
        elif ev.kind == "up" and g:
            final = decide(TARGETS, g, press_)
            return None if final == "none" else final
    return None


def main() -> None:
    saved = kedei._Device._instance
    n = 2000
    print(f"{'button':9} {'shift':>9} {'σ':>3}   landing-rule  resting-rule   (after a good calibration: shift 0)")
    for name, (box, dx, dy, sd) in LOGGED.items():
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        row = []
        for shift in ((dx, dy), (0, 0)):
            rng = random.Random(7)
            ok = {"land": 0, "rest": 0}
            for _ in range(n):
                rest = (cx + shift[0] + rng.gauss(0, sd * 0.7), cy + shift[1] + rng.gauss(0, sd * 0.7))
                evs = events(press(rng, rest))
                ok["land"] += landing_rule(evs) == name
                ok["rest"] += resting_rule(evs) == name
            row.append((100 * ok["land"] / n, 100 * ok["rest"] / n))
        print(f"{name:9} {dx:+4d},{dy:+4d} {sd:3d}   {row[0][0]:5.0f} %      {row[0][1]:5.0f} %"
              f"        {row[1][0]:5.0f} % → {row[1][1]:5.0f} %")
    kedei._Device._instance = saved


if __name__ == "__main__":
    main()
