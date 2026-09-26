"""Round 8: what a recalibration with 9 vs 12 crosses would leave, on the owner's Test prstem (27. 9. 01:13).

    python3 tests/panel_touch_sim3.py

NOT a measurement — a model. The 12 probe errors (after the 9-point
calibration of 26. 9. 23:30) are spread into an error field by inverse-distance
weighting; the owner's finger then "taps" the calibration crosses through that
field (with σ 4 px of finger noise), the real `kedei.recalibrate` fits a new
calibration on top, and the error left at the 12 probe spots is measured —
top edge vs. the rest, as `touchreport --probes` reports it (now 31.5 / 16.5).
"""

from __future__ import annotations

import math
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ytdj.panel import kedei  # noqa: E402
from ytdj.panel.calib import POINTS  # noqa: E402

# spot → (dx, dy) read by the owner's finger (touchreport --probes, 27. 9. 01:12:54–01:13:13)
PROBES = {
    (40, 10): (11, 38), (282, 10): (-29, 54), (366, 10): (-11, 8), (451, 10): (-10, 6),
    (240, 64): (-19, 6), (30, 160): (13, -5), (240, 160): (-4, -13), (450, 160): (10, -11),
    (120, 230): (4, -4), (30, 300): (20, -27), (240, 300): (4, 15), (450, 300): (-7, -13),
}
NINE = [(x, y) for y in (40, 160, 280) for x in (40, 240, 440)]


def field(x: float, y: float) -> tuple[float, float]:
    num_x = num_y = den = 0.0
    for (px, py), (dx, dy) in PROBES.items():
        d2 = (x - px) ** 2 + (y - py) ** 2
        if d2 < 1:
            return dx, dy
        w = 1 / d2
        num_x += w * dx
        num_y += w * dy
        den += w
    return num_x / den, num_y / den


def read(x, y, rng):
    fx, fy = field(x, y)
    return x + fx + rng.gauss(0, 4), y + fy + rng.gauss(0, 4)


def errors(cal):
    top, rest = [], []
    for (x, y) in PROBES:
        fx, fy = field(x, y)
        mx, my = cal.map_float(x + fx, y + fy) if cal else (x + fx, y + fy)
        e = math.hypot(mx - x, my - y)
        (top if y <= 20 else rest).append(e)
    return statistics.mean(top), statistics.mean(rest)


def main() -> None:
    ident = kedei.Calibration(1, 0, 0, 0, 1, 0)
    print(f"now (probe data):          top {errors(None)[0]:5.1f} px, rest {errors(None)[1]:5.1f} px")
    for name, points in (("recalibrate, 9 crosses", NINE), ("recalibrate, 12 crosses", list(POINTS))):
        res, refused = [], 0
        for seed in range(40):
            rng = random.Random(seed)
            pairs = [((round(read(x, y, rng)[0]), round(read(x, y, rng)[1])), (x, y)) for x, y in points]
            try:
                cal, _ = kedei.recalibrate(ident, pairs)
            except ValueError:
                refused += 1
                continue
            res.append(errors(cal))
        top = statistics.mean(r[0] for r in res)
        rest = statistics.mean(r[1] for r in res)
        print(f"{name:26} top {top:5.1f} px, rest {rest:5.1f} px   ({len(res)} runs, {refused} refused)")


if __name__ == "__main__":
    main()
