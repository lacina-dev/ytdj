"""Renders the TV screen's states to PNGs, for eyeballing the design.

    python3 tests/tv_shots.py [out_dir]          # default /tmp/ytdj-shots/tv

Every state at 720×480 (the Pi's firmware framebuffer without a TV), 1280×720
and 1920×1080. Also prints what a full frame and a progress tick cost and how
much memory the renderer needs. Covers are fetched once into /tmp/ytdj-art
(without a network the fallback tile shows).
"""

from __future__ import annotations

import resource
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ytdj.panel.art import _http_get  # noqa: E402
from ytdj.tv.app import ART_URL  # noqa: E402
from ytdj.tv.fb import pack  # noqa: E402
from ytdj.tv.screen import Renderer, cover_tile, view_from  # noqa: E402

SIZES = ((720, 480), (1280, 720), (1920, 1080))
ART_DIR = Path("/tmp/ytdj-art")
NOW = time.mktime((2026, 10, 6, 14, 37, 0, 0, 0, -1))

QUEUE = [
    {"id": "aaaaaaaaaa1", "title": "Jasná zpráva", "artist": "Olympic",
     "req": {"id": "w2", "who": "Jana"}},
    {"id": "aaaaaaaaaa2", "title": "Dej mi víc své lásky", "artist": "Olympic"},
    {"id": "aaaaaaaaaa3", "title": "Okno mé lásky", "artist": "Olympic"},
    {"id": "aaaaaaaaaa4", "title": "Želva", "artist": "Olympic"},
    {"id": "aaaaaaaaaa5", "title": "Slzy tvý mámy", "artist": "Olympic"},
]
WISH = {
    "playing": True, "paused": False, "buffering": False, "position": 83.0, "duration": 214.0,
    "current": {"id": "qRIsEHxcgDY", "title": "Dej mi jen pár minut", "artist": "Ivan Hlas",
                "duration": 214,
                "reason": {"kind": "wish", "who": "Kolega", "text": "něco od Ivana Hlase"}},
    "queue": QUEUE, "mood": "český rock",
    "dj": {"busy": False, "last": {"reply": "Zařazuju Ivana Hlase — první čtyři jako tvoje "
                                            "přání, pak podobná hudba.", "who": "Kolega",
                                   "at": NOW - 60}},
}
BACKGROUND = {
    **WISH, "position": 12.0,
    "current": {"id": "W20gcM6_UtQ", "title": "The Fox (What Does the Fox Say?)",
                "artist": "Ylvis", "duration": 214,
                "reason": {"kind": "radio", "who": "", "from_who": "Kolega"}},
    "queue": [{k: v for k, v in q.items() if k != "req"} for q in QUEUE[:2]],
    "dj": {"busy": True, "text": "pusť něco veselého k práci", "last": None},
}
LONG = {
    **WISH, "position": 3 * 3600 + 125.0, "duration": 4 * 3600.0,
    "current": {"id": "zzzzzzzzzzz", "duration": 14400,
                "title": "Symfonie č. 9 d moll „S ódou na radost“, op. 125: IV. Presto – "
                         "Allegro assai – Allegro assai vivace (Alla marcia) – finále, živě",
                "artist": "Česká filharmonie, Pražský filharmonický sbor & Jiří Bělohlávek",
                "reason": {"kind": "wish", "who": "Vladimíra-Anežka",
                           "text": "pusť mi prosím celou devátou Beethovenovu symfonii živě"}},
    "queue": [{"id": "x", "title": "Supercalifragilisticexpialidocious (From „Mary Poppins“ / "
                                   "Soundtrack Version)", "artist": "Julie Andrews, Dick Van Dyke "
                                                             "& The Pearlie Chorus",
               "req": {"who": "Vladimíra-Anežka"}}] + QUEUE[:2],
    "dj": {"busy": False, "last": {"reply": "Devátou mám jen v několika nahrávkách; beru tu "
                                            "živou s Českou filharmonií, celá má přes hodinu, "
                                            "takže ostatní přání přijdou na řadu až po ní.",
                                   "at": NOW - 30}},
}
STATES = {
    "playing-wish": dict(state=WISH),
    "playing-background": dict(state=BACKGROUND),
    "paused": dict(state={**WISH, "paused": True, "playing": False, "dj": {
        "last": {"reply": "Po zapnutí jsem obnovil, co tu bylo (1 přání, podkres „český rock“). "
                          "Hudba čeká na ▶ a pokračuje tam, kde přestala.", "at": NOW - 5}}}),
    "nothing-playing": dict(state={"current": None, "queue": [], "dj": {}}),
    "nothing-playing-dimmed": dict(state={"current": None, "queue": [], "dj": {}}, dim=True),
    "starting": dict(state={"current": None, "queue": [], "starting": True,
                            "dj": {"busy": True, "text": "rozjezd podle času a dne"}}),
    "offline": dict(state=None, offline="spojení odmítnuto"),
    "outage": dict(state={**WISH, "outage": {"reason": "YouTube neodpovídá", "since": NOW - 40}}),
    "long-titles": dict(state=LONG),
    "no-cover": dict(state={**BACKGROUND, "current": {**BACKGROUND["current"], "id": "nocover0000"}}),
    # péče o panel telky: bloky na druhé straně, krajní polohy posunu, spořič
    "care-swapped": dict(state=WISH, swap=True, shift=6),
    "care-drift-far": dict(state=WISH, shift=17),
    "care-saver-idle": dict(state={"current": None, "queue": [], "dj": {}}, saver=True, shift=3),
    "care-saver-paused": dict(state={**WISH, "paused": True, "playing": False}, saver=True, shift=11),
}


def cover(vid: str, side: int):
    ART_DIR.mkdir(parents=True, exist_ok=True)
    path = ART_DIR / f"{vid}-hq.jpg"
    if not path.exists():
        try:
            path.write_bytes(_http_get(ART_URL.format(id=vid), 5.0))
        except Exception:
            return None
    try:
        return cover_tile(path.read_bytes(), side)
    except Exception:
        return None


def view(spec: dict, renderer: Renderer, position: float | None = None):
    state = spec.get("state")
    if position is not None and state:
        state = {**state, "position": position}
    tiles = {}

    def art(vid):
        if vid not in tiles:
            tiles[vid] = cover(vid, renderer.art_side) if vid in ("qRIsEHxcgDY", "W20gcM6_UtQ") else None
        return tiles[vid]

    renderer.art = art
    v = view_from(state, offline=spec.get("offline", ""), now=NOW, mono=100.0, got_at=100.0,
                  idle_since=100.0 - 150 if spec.get("dim") else None,
                  **_view_kw(art))
    return replace(v, shift=spec.get("shift", 0), swap=bool(spec.get("swap")),
                   saver=bool(spec.get("saver")))


def _view_kw(art):
    return dict(
address="jukebox.local", address_ip="192.168.0.24",
                art_ready=lambda vid: art(vid) is not None)


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/ytdj-shots/tv")
    for w, h in SIZES:
        folder = out / f"{w}x{h}"
        folder.mkdir(parents=True, exist_ok=True)
        r = Renderer((w, h))
        for name, spec in STATES.items():
            r.render(view(spec, r), full=True)
            r.frame.save(folder / f"{name}.png")
        # what it costs: a full frame, then a progress tick five seconds later
        t0 = time.perf_counter()
        r.render(view(STATES["playing-wish"], r), full=True)
        packed = pack(r.frame, "RGB565")
        full_ms = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        boxes = r.render(view(STATES["playing-wish"], r, position=88.0))
        px = 0
        for b in boxes:
            pack(r.frame.crop(b), "RGB565")
            px += (b[2] - b[0]) * (b[3] - b[1])
        tick_ms = (time.perf_counter() - t0) * 1000
        print(f"{w}x{h}: celý snímek {full_ms:.0f} ms ({len(packed) // 1024} kB), "
              f"krok průběhu {tick_ms:.1f} ms / {px} bodů v {len(boxes)} výřezech")
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
    print(f"paměť procesu (max RSS, všechny tři velikosti): {rss} MB")
    print(f"snímky: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
