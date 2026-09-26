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

from ytdj.panel.art import square_tile, _http_get  # noqa: E402
from ytdj.panel.ui import ART_SIDE, QrRenderer, Renderer, View  # noqa: E402
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
    qr_url="http://192.168.0.24:8765",
)
# real tracks with real covers (YouTube Music "Art Tracks"); fetched once into ART_DIR
ART_DIR = Path("/tmp/ytdj-art")
WITH_ART = replace(PLAYING, title="Dej mi jen pár minut", artist="Ivan Hlas", track_id="qRIsEHxcgDY",
                   art_ready=True, next_title="Zastav mě", next_artist="Marek Ztracený")

WISH = WishView(page="home", count=3)
ROWS = (
    QueueRow("a", "Petr", "písničky od Kabátu", "playing", "hraje · dál: Burlaci", False),
    QueueRow("b", "displej", "Holky z naší školky", "queued", "ve frontě · hned po téhle", True),
    QueueRow("c", "Karel", "něco klidnějšího na odpoledne", "queued", "ve frontě · za ~1 skladbu", False,
             "favourite"),
    QueueRow("f", "Petr", "Pohoda od Kabátu", "queued", "ve frontě · za ~2 skladby", False, "banned"),
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
    "wish-home-offline": replace(WISH, offline=True),
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
    "radio-from-wish": replace(PLAYING, mood="veselý punk", now_from="Robert"),
    "radio-from-wish-long": replace(PLAYING, now_from="Maximilián Veliký"),
    "radio-start": replace(PLAYING, mood="ranní klid", now_start=True),
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
    "outage": replace(PLAYING, outage=True, running=False, outage_reason="network"),
    "outage-login": replace(PLAYING, outage=True, running=False, outage_reason="youtube_login"),
    "medium-title-wish": replace(PLAYING, title="Až se jednou budem smát", artist="Lucie", now_who="Kateřina",
                                 next_title="Amerika", next_artist="Lucie", next_who="Tomáš", more_wishes=1),
    "two-line-title-wish": replace(PLAYING, title="Bohemian Rhapsody (Remastered 2011) — Live at Wembley",
                                   artist="Queen", now_who="Honza"),
    "idle-wishes-waiting": replace(PLAYING, has_track=False, running=False, mood="", can_next=False,
                                   busy=True, more_wishes=2),
    "dj-offline": replace(PLAYING, dj_offline=True),
    "wishes-queued": replace(PLAYING, title="Malá dáma", artist="Kabát", now_who="Petr", wishes=3,
                             next_title="Holky z naší školky", next_artist="Olympic", next_who="Jana",
                             more_wishes=1),
    "art-playing": WITH_ART,
    "art-wish-next": replace(WITH_ART, title="Zastav mě", artist="Marek Ztracený", track_id="F6ZazYXzfXg",
                             now_who="Kateřina", next_title="September", next_artist="Earth, Wind & Fire",
                             next_who="Tomáš", more_wishes=2, wishes=3),
    "art-toast": replace(WITH_ART, toast=("Petr", "něco od Kabátu", "DJ vybírá…"), wishes=2),
    "art-toast-queued": replace(WITH_ART, toast=("Jana", "Dancing Queen", "za ~2 skladby"), wishes=2),
    "art-busy": replace(WITH_ART, title="Re", artist="Nils Frahm", track_id="DVvgl0amAMw", busy=True),
    "art-fallback": replace(WITH_ART, art_ready=False),
    # office votes (current.votes / queue[0].votes)
    "vote-favourite": replace(WITH_ART, vote_up=3, vote_status="favourite", next_vote="favourite"),
    "vote-mixed": replace(WITH_ART, title="Zastav mě", artist="Marek Ztracený", track_id="F6ZazYXzfXg",
                          vote_up=2, vote_down=1, vote_status="neutral",
                          next_title="Pohoda", next_artist="Kabát", next_vote="banned"),
    "vote-two-line": replace(WITH_ART, vote_up=1, vote_down=2, vote_status="downweighted"),
    "vote-banned-wish": replace(WITH_ART, title="Pohoda", artist="Kabát", track_id="", art_ready=False,
                                vote_down=2, vote_status="banned", now_who="Petr"),
    "vote-artist-banned": replace(WITH_ART, title="Wonderwall", artist="Oasis", track_id="", art_ready=False,
                                  vote_status="neutral", artist_banned=True, now_who="Jana"),
    "rest-paused-qr": replace(WITH_ART, running=False, paused=True, rest=True),
    "pressed-wish": replace(PLAYING, pressed="wish"),
    "pressed-next": replace(PLAYING, pressed="next", note="povel selhal"),
    "pressed-play": replace(PLAYING, pressed="play"),
}


def art_source(vid: str):
    """The cover tile from ART_DIR (fetched there on first use), like the panel's ArtCache."""
    f = ART_DIR / f"{vid}.jpg"
    if not f.exists():
        try:
            ART_DIR.mkdir(exist_ok=True)
            f.write_bytes(_http_get(f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg", 5.0))
        except OSError:
            return None
    return square_tile(f.read_bytes(), ART_SIDE)


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/ytdj-panel-shots")
    out.mkdir(parents=True, exist_ok=True)
    for name, view in STATES.items():
        r = Renderer("cs")
        r.art_source = art_source
        r.render(view, full=True)
        if view.rest:
            from ytdj.panel.app import REST_LEVEL

            img = r.frame.point([int(i * REST_LEVEL) for i in range(256)] * 3)
            if r.qr_box:
                img.paste(r.frame.crop(r.qr_box), r.qr_box[:2])
            img.save(out / f"{name}.png")
            print(out / f"{name}.png")
            continue
        r.frame.save(out / f"{name}.png")
        print(out / f"{name}.png")
    # the rest state: the player dimmed after a while of silence (app.REST_LEVEL)
    from ytdj.panel.app import REST_LEVEL

    r = Renderer("cs")
    r.render(STATES["paused"], full=True)
    r.frame.point([int(i * REST_LEVEL) for i in range(256)] * 3).save(out / "rest-paused.png")
    print(out / "rest-paused.png")
    qr = QrRenderer(Renderer("cs").fonts)
    qr.render(("http://192.168.0.24:8765", "http://ytdj.local:8765"), full=True)
    qr.frame.save(out / "qr-page.png")
    print(out / "qr-page.png")
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

    # cover art: decode + crop + scale of one mqdefault JPEG, and what a tile costs in RAM
    data = (ART_DIR / "qRIsEHxcgDY.jpg").read_bytes() if (ART_DIR / "qRIsEHxcgDY.jpg").exists() else b""
    if data:
        t = time.perf_counter()
        for _ in range(20):
            tile = square_tile(data, ART_SIDE)
        dec = (time.perf_counter() - t) * 1000 / 20
        print(f"cover: {len(data)} B JPEG → {ART_SIDE}² tile in {dec:.1f} ms, {len(tile.tobytes())} B in RAM")
    r = Renderer("cs")
    r.art_source = art_source
    r.render(replace(WITH_ART, art_ready=False), full=True)
    boxes = r.render(WITH_ART)
    print(f"cover arrives: {sum((b[2] - b[0]) * (b[3] - b[1]) for b in boxes)} px in {boxes}")
    r.render(replace(WITH_ART, elapsed=90), full=True)
    boxes = r.render(replace(WITH_ART, elapsed=90, toast=("Petr", "něco od Kabátu", "DJ vybírá…")))
    print(f"wish banner in: {sum((b[2] - b[0]) * (b[3] - b[1]) for b in boxes)} px in {boxes}")
    boxes = r.render(replace(WITH_ART, elapsed=90))
    print(f"wish banner out: {sum((b[2] - b[0]) * (b[3] - b[1]) for b in boxes)} px in {boxes}")
    boxes = r.render(replace(WITH_ART, elapsed=90, title="Zastav mě", artist="Marek Ztracený",
                             track_id="F6ZazYXzfXg", next_title="September", next_artist="Earth, Wind & Fire"))
    print(f"track change (cover ready): {sum((b[2] - b[0]) * (b[3] - b[1]) for b in boxes)} px")

    r.render(WITH_ART, full=True)
    for label, v in (("vote ♥ 1 appears", replace(WITH_ART, vote_up=1, vote_status="favourite")),
                     ("♥ 1 → ♥ 2", replace(WITH_ART, vote_up=2, vote_status="favourite")),
                     ("→ vyřazená hlasováním", replace(WITH_ART, vote_down=3, vote_up=2, vote_status="banned",
                                                       now_who="Petr"))):
        boxes = r.render(v)
        print(f"{label}: {sum((b[2] - b[0]) * (b[3] - b[1]) for b in boxes)} px in {boxes}")

    # ten minutes of playback, as the glass sees it: the per-second ticks plus
    # the periodic re-send of the glass (old: a full frame every 60 s; now: one
    # 32-row band every 30 s, see app.REFRESH_BAND)
    from ytdj.panel.app import REFRESH_BAND, REFRESH_EVERY

    r = Renderer("cs")
    r.render(replace(PLAYING, elapsed=83, duration=720), full=True)
    tick_px, pushes, t_render = 0, 0, 0.0
    for s in range(84, 84 + 600):
        t = time.perf_counter()
        boxes = r.render(replace(PLAYING, elapsed=s, duration=720))
        t_render += time.perf_counter() - t
        tick_px += sum((b[2] - b[0]) * (b[3] - b[1]) for b in boxes)
        pushes += len(boxes)
    old_refresh = 10 * 480 * 320
    new_refresh = int(600 / REFRESH_EVERY) * 480 * REFRESH_BAND
    print(f"10 min playback: ticks {tick_px} px in {pushes} pushes, render {t_render * 1000:.0f} ms total")
    print(f"  re-send, old (full frame / 60 s): {old_refresh} px, largest push {480 * 320} px")
    print(f"  re-send, new (band / {REFRESH_EVERY:.0f} s): {new_refresh} px, largest push {480 * REFRESH_BAND} px")


if __name__ == "__main__":
    main()
