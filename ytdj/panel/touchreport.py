"""How well touch works, from the panel's telemetry — and where fingers land.

    python3 -m ytdj.panel.touchreport /var/log/ytdj-panel/events.jsonl [--since 2026-09-26T11:30]

Prints:
* the driver's counters (`panel.touch_driver`): confirmed presses, landings
  that never became a press, noisy samples;
* what came of the presses: actions (`panel.action` source=touch,
  `panel.net_action`, `panel.wish_action`) vs. rejects (`panel.touch_rejects`);
* where fingers land relative to the button centres (`off` on actions,
  since 26. 9.) and where misses land relative to the closest button
  (`panel.touch_miss`) — a consistent shift means the calibration is off,
  and "Kalibrace dotyku" on the network screen fixes it.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from datetime import datetime


def _ts(e: dict) -> float:
    t = e.get("ts") or e.get("t") or e.get("time")
    if isinstance(t, (int, float)):
        return float(t)
    if isinstance(t, str):
        try:
            return datetime.fromisoformat(t).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def load(path: str, since: float = 0.0) -> list[dict]:
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict) and str(e.get("kind", "")).startswith("panel.") and _ts(e) >= since:
                out.append(e)
    return out


def report(events: list[dict]) -> str:
    drv: Counter[str] = Counter()
    rej: Counter[str] = Counter()
    actions = 0
    offs: list[tuple[int, int]] = []
    press_ms: list[int] = []
    misses: list[dict] = []
    for e in events:
        k = e.get("kind")
        if k == "panel.touch_driver":
            drv.update({key: v for key, v in e.items() if isinstance(v, int) and key != "period_s"})
        elif k == "panel.touch_rejects":
            rej.update({key: v for key, v in e.items() if isinstance(v, int) and key != "period_s"})
        elif (k == "panel.action" and e.get("source") == "touch") or k in ("panel.net_action", "panel.wish_action"):
            if e.get("button") == "idle_close":
                continue
            actions += 1
            if isinstance(e.get("press_ms"), int):
                press_ms.append(e["press_ms"])
            off = e.get("off")
            if isinstance(off, list) and len(off) == 2:
                offs.append((off[0], off[1]))
        elif k == "panel.touch_miss":
            misses.append(e)

    lines = []
    downs = drv.get("downs", 0)
    landings = downs + drv.get("aborted_press", 0)
    lines.append(f"driver: {downs} presses from {landings} landings "
                 f"({100 * downs / landings:.0f} %)" if landings else "driver: no presses")
    for key in ("aborted_press", "spread_rejected", "invalid_samples", "low_pressure", "jump_dropped",
                "noisy_release"):
        if drv.get(key):
            lines.append(f"  {key}: {drv[key]}")
    lines.append(f"actions from touch: {actions}" + (f" ({100 * actions / downs:.0f} % of presses)" if downs else ""))
    if press_ms:
        press_ms.sort()
        lines.append(f"  press: median {press_ms[len(press_ms) // 2]} ms, "
                     f"p90 {press_ms[int(0.9 * (len(press_ms) - 1))]} ms")
    if rej:
        lines.append("rejects: " + ", ".join(f"{k} {v}" for k, v in rej.most_common()))
    if offs:
        mx = statistics.median(o[0] for o in offs)
        my = statistics.median(o[1] for o in offs)
        lines.append(f"hits vs. button centres ({len(offs)}): median shift x {mx:+.0f} px, y {my:+.0f} px")
    if misses:
        near = Counter(str(m.get("near")) for m in misses)
        dx = statistics.median(int(m.get("dx", 0)) for m in misses)
        dy = statistics.median(int(m.get("dy", 0)) for m in misses)
        lines.append(f"misses ({len(misses)}): closest to " + ", ".join(f"{k} {v}" for k, v in near.most_common(5))
                     + f"; median offset from its centre x {dx:+.0f}, y {dy:+.0f} px")
    if offs and len(offs) >= 10:
        mx = statistics.median(o[0] for o in offs)
        my = statistics.median(o[1] for o in offs)
        if max(abs(mx), abs(my)) >= 8:
            lines.append("→ a consistent shift: run \"Kalibrace dotyku\" on the network screen")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m ytdj.panel.touchreport")
    ap.add_argument("events", nargs="?", default="/var/log/ytdj-panel/events.jsonl")
    ap.add_argument("--since", default="", help="ISO time, e.g. 2026-09-26T11:30")
    a = ap.parse_args(argv)
    since = datetime.fromisoformat(a.since).timestamp() if a.since else 0.0
    print(report(load(a.events, since)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
