"""Renders the panel's main states to PNGs, for eyeballing the design.

    python tests/panel_shots.py [out_dir]

Also prints how long a full frame and a typical one-second tick take to
render (drawing + diffing only — the glass itself is the driver's business).
"""

from __future__ import annotations

import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ytdj.panel.ui import Renderer, View  # noqa: E402

PLAYING = View(
    online=True,
    connecting=False,
    target="127.0.0.1:8765",
    has_track=True,
    title="Holky z naší školky",
    artist="Olympic",
    running=True,
    elapsed=83,
    duration=214,
    volume=65,
    mood="klidný večer, český rock",
    can_next=True,
)

STATES = {
    "playing": PLAYING,
    "paused": replace(PLAYING, running=False, paused=True, elapsed=141),
    "connecting": View(target="127.0.0.1:8765"),
    "offline": View(connecting=False, target="127.0.0.1:8765"),
    "long-czech-title": replace(
        PLAYING,
        title="Příliš žluťoučký kůň úpěl ďábelské ódy — živě ze Šťastného Žďáru (remaster 2024)",
        artist="Žďárští čeští řezníci & Ústečtí ďáblové, Čechomor, Jaroslav Uhlíř",
        elapsed=3725,
        duration=5400,
        busy=True,
    ),
    "volume-dragging": replace(PLAYING, volume=42, pressed="vol"),
    "idle-dj-thinking": replace(PLAYING, has_track=False, running=False, busy=True, mood=""),
    "pressed-next": replace(PLAYING, pressed="next", note="povel selhal"),
    "pressed-play": replace(PLAYING, pressed="play"),
}


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/ytdj-panel-shots")
    out.mkdir(parents=True, exist_ok=True)
    for name, view in STATES.items():
        r = Renderer("cs")
        r.render(view, full=True)
        r.frame.save(out / f"{name}.png")
        print(out / f"{name}.png")

    # timings: first frame, then a second of playback
    r = Renderer("cs")
    t = time.perf_counter()
    r.render(PLAYING, full=True)
    full_ms = (time.perf_counter() - t) * 1000
    ticks = []
    boxes = []
    for s in range(84, 144):
        t = time.perf_counter()
        boxes = r.render(replace(PLAYING, elapsed=s))
        ticks.append((time.perf_counter() - t) * 1000)
        px = sum((b[2] - b[0]) * (b[3] - b[1]) for b in boxes)
    print(f"full frame render: {full_ms:.1f} ms")
    print(f"1 s tick render: avg {sum(ticks) / len(ticks):.2f} ms, max {max(ticks):.2f} ms; last boxes {boxes} ({px} px)")
    t = time.perf_counter()
    boxes = r.render(replace(PLAYING, elapsed=143, volume=70, pressed="vol"))
    print(f"volume step render: {(time.perf_counter() - t) * 1000:.2f} ms, boxes {boxes}")
    t = time.perf_counter()
    boxes = r.render(replace(PLAYING, elapsed=143, running=False, paused=True))
    print(f"pause render: {(time.perf_counter() - t) * 1000:.2f} ms, boxes {boxes}")


if __name__ == "__main__":
    main()
