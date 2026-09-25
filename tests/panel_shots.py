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
from ytdj.panel.wishui import QueueRow, WishRenderer, WishView  # noqa: E402

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
    next_title="Jasná zpráva",
    next_artist="Olympic",
)

WISH = WishView(page="home", count=3)
ROWS = (
    QueueRow("a", "Petr", "písničky od Kabátu", "playing", "hraje · dál: Burlaci", False),
    QueueRow("b", "displej", "Holky z naší školky", "queued", "ve frontě · hned po téhle", True),
    QueueRow("c", "Karel", "něco klidnějšího na odpoledne", "queued", "ve frontě · za ~1 skladbu", False),
    QueueRow("d", "Jana", "Dancing Queen", "thinking", "DJ vybírá", False),
    QueueRow("e", "Jana", "Jasná zpráva", "done", "hotovo · Zařazuju Jasnou zprávu.", False),
)
WISH_STATES = {
    "wish-home": WISH,
    "wish-home-draft-who": replace(WISH, text="něco od Čechomoru", who="Robert", pressed="chip2"),
    "wish-keys-empty": replace(WISH, page="keys"),
    "wish-keys-typing": replace(WISH, page="keys", text="písničky od Čechomor", can_connect=True, pressed="c:r"),
    "wish-keys-accents": replace(WISH, page="keys", text="Žlutý pes, pak Ch", kb_page="áč", can_connect=True),
    "wish-keys-accents-shift": replace(WISH, page="keys", text="", kb_page="áč", shift=1, hint="napiš, co chceš slyšet"),
    "wish-keys-123": replace(WISH, page="keys", text="hity z 90", kb_page="123", can_connect=True),
    "wish-sent-busy": replace(WISH, page="sent", phase="busy", wish="písničky od Čechomoru", elapsed=12),
    "wish-sent-queued-next": replace(
        WISH, page="sent", phase="queued", wish="Holky z naší školky", eta="za ~2 skladby", can_next=True,
        reply="Zařazuju Holky z naší školky od Olympicu. Na řadě za ~2 skladby.",
    ),
    "wish-sent-playing": replace(
        WISH, page="sent", phase="playing", wish="písničky od Čechomoru",
        reply="Hraju Čechomor: po 3 skladbách, střídám s ostatními přáními. Hraje hned.",
    ),
    "wish-sent-notfound": replace(
        WISH, page="sent", phase="notfound", wish="Wonderwall od Nobody",
        reply="Nenašel jsem, o co sis řekl (Nobody — Wonderwall).",
    ),
    "wish-queue": replace(WISH, page="queue", rows=ROWS, count=4),
    "wish-queue-pressed-x": replace(WISH, page="queue", rows=ROWS, count=4, pressed="rm1"),
    "wish-queue-empty": replace(WISH, page="queue", rows=(), count=0),
    "wish-sent-error": replace(
        WISH, page="sent", phase="error", wish="něco klidnějšího",
        error="ytdj teď neodpovídá (možná se restartuje).",
    ),
}

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
    "idle": replace(PLAYING, has_track=False, running=False, mood="", can_next=False),
    "no-next-up": replace(PLAYING, next_title="", next_artist=""),
    "wishes-queued": replace(PLAYING, title="Malá dáma", artist="Kabát", now_who="Petr", wishes=3,
                             next_title="Holky z naší školky", next_artist="Olympic", next_who="Jana"),
    "pressed-wish": replace(PLAYING, pressed="wish"),
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
    share = None
    for name, view in WISH_STATES.items():
        wr = WishRenderer("cs", share=share)
        share = share or wr
        wr.render(view, full=True)
        wr.frame.save(out / f"{name}.png")
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
