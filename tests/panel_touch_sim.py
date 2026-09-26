"""A model of finger presses on the resistive glass: old vs. new touch pipeline.

    python3 tests/panel_touch_sim.py [old_kedei.py]

NOT a measurement — a Monte Carlo model. Presses on "Další" are generated as
raw sample streams (landing samples scattered by σ_land with a chance of an
empty/noisy converter read, a resting finger with small jitter and the same
chance of noisy reads, a drifting sample at lift-off, then pen-up) and run
through the real driver code (old: `git show 9c5afcc:ytdj/panel/kedei.py`,
new: the current one) and the old / new button rules. The model's free
parameters are picked so that the OLD pipeline reproduces the Pi's
telemetry ratios from 25.–26. 9. (aborted_press/downs ≈ 0.45,
spread_rejected/downs ≈ 1.6, invalid_samples/landings ≈ 1.0); press lengths
follow the logged press_ms (median 162 ms, p90 947 ms).
"""

from __future__ import annotations

import importlib.util
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ytdj.panel import kedei  # noqa: E402
from ytdj.panel.touchpress import Press, nearest  # noqa: E402
from ytdj.panel.ui import NEXT, TARGETS  # noqa: E402

P = 4000
DT = 0.015
SIGMA_PRESS = math.log(947 / 162) / 1.2816


class Dev:
    def __init__(self, samples):
        self.samples = list(samples)
        self.pen = False

    def init(self, madctl):
        pass

    def touch_raw(self):
        item = self.samples.pop(0) if self.samples else None
        if item == "invalid":
            self.pen = True
            return None
        self.pen = item is not None
        return item

    def pen_down(self):
        return self.pen


def contact(rng, aim, n_land, s_land, p_inv, hold_noise=0.5, s_hold=4.0, s_lift=25.0):
    ax, ay = aim
    out = []
    for _ in range(n_land):
        out.append("invalid" if rng.random() < p_inv else
                   (round(ax + rng.gauss(0, s_land)), round(ay + rng.gauss(0, s_land)), P))
    n_hold = max(1, int(162 * math.exp(rng.gauss(0, SIGMA_PRESS)) / 1000 / DT) - n_land)
    for _ in range(n_hold):
        out.append("invalid" if rng.random() < p_inv * hold_noise else
                   (round(ax + rng.gauss(0, s_hold)), round(ay + rng.gauss(0, s_hold)), P))
    ang = rng.uniform(0, 2 * math.pi)
    d = abs(rng.gauss(0, s_lift))
    out.append((round(ax + d * math.cos(ang)), round(ay + d * math.sin(ang)), P))
    return out + [None] * 4


def run_driver(mod, samples):
    mod._Device._instance = Dev(samples)
    t = mod.KedeiTouch(calibration=mod.Calibration(1, 0, 0, 0, 1, 0))
    t.DOWN_INTERVAL = t.IDLE_INTERVAL = 0.0
    evs = []
    for _ in range(len(samples) + 4):
        ev = t.poll(0.0)
        if ev:
            evs.append(ev)
    return evs, t.take_stats()


def old_rules(evs) -> bool:
    """Before 26. 9.: the drawn button + 4 px at down, the last position within +14 px at up."""
    l, t, r, b = NEXT
    pressed, last = False, None
    for e in evs:
        if e.kind == "down":
            pressed = l - 4 <= e.x < r + 4 and t - 4 <= e.y < b + 4
            last = (e.x, e.y)
        elif e.kind == "move":
            last = (e.x, e.y)
        elif e.kind == "up" and pressed:
            x, y = last
            return l - 14 <= x < r + 14 and t - 14 <= y < b + 14
    return False


def new_rules(evs) -> bool:
    press = None
    for e in evs:
        if e.kind == "down":
            name, _ = nearest(TARGETS, e.x, e.y)
            press = Press(name, TARGETS[name]) if name == "next" else None
        elif e.kind == "move" and press:
            press.move(e.x, e.y)
        elif e.kind == "up" and press:
            return press.inside
    return False


def simulate(old, params, n=3000, bias=(0, 0), seed=1):
    rng = random.Random(seed)
    cx, cy = (NEXT[0] + NEXT[2]) / 2, (NEXT[1] + NEXT[3]) / 2
    tot = {"old": {}, "new": {}}
    ok = {"old": 0, "new": 0}
    for _ in range(n):
        # people aim at the label: σ 25 px across, 10 px down the 52 px button
        aim = (cx + bias[0] + rng.gauss(0, 25), cy + bias[1] + rng.gauss(0, 10))
        samples = contact(rng, aim, *params)
        for name, mod, rules in (("old", old, old_rules), ("new", kedei, new_rules)):
            evs, st = run_driver(mod, list(samples))
            for k, v in st.items():
                tot[name][k] = tot[name].get(k, 0) + v
            ok[name] += rules(evs)
    return tot, ok


def load_old(path: str):
    spec = importlib.util.spec_from_file_location("ytdj.panel.kedei_old", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ytdj.panel.kedei_old"] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> None:
    old = load_old(sys.argv[1] if len(sys.argv) > 1 else "/tmp/kedei_old.py")
    saved = kedei._Device._instance
    target = (0.45, 1.6, 1.0)
    best = None
    grid = [(n, s, p, h) for n in (2, 3, 4) for s in (6.0, 9.0, 12.0, 16.0)
            for p in (0.04, 0.08, 0.12, 0.2) for h in (0.1, 0.3, 0.6)]
    for params in grid:
        if True:
            if True:
                tot, _ = simulate(old, params, n=250)
                o = tot["old"]
                downs = max(1, o.get("downs", 0))
                ratios = (o.get("aborted_press", 0) / downs, o.get("spread_rejected", 0) / downs,
                          o.get("invalid_samples", 0) / (downs + o.get("aborted_press", 0)))
                err = sum(abs(a - b) / b for a, b in zip(ratios, target))
                if best is None or err < best[0]:
                    best = (err, params, ratios)
    err, params, ratios = best
    print(f"model fitted to the Pi's old ratios: n_land={params[0]}, σ_land={params[1]} px, "
          f"p_noisy landing={params[2]}, holding={params[2] * params[3]:.3f}")
    print(f"  old pipeline reproduces: aborted/downs {ratios[0]:.2f} (Pi 0.45), spread/downs {ratios[1]:.2f} (Pi 1.63),"
          f" invalid/landings {ratios[2]:.2f} (Pi 0.96)")
    for bias in ((0, 0), (8, 12)):
        tot, ok = simulate(old, params, n=3000, bias=bias)
        n = 3000
        print(f"calibration shift {bias}: presses on Další that act — old {100 * ok['old'] / n:.0f} %, "
              f"new {100 * ok['new'] / n:.0f} %  (driver downs old {tot['old'].get('downs', 0)}, "
              f"new {tot['new'].get('downs', 0)})")
    kedei._Device._instance = saved


if __name__ == "__main__":
    main()
