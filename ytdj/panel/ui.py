"""Drawing the panel: one retained 480×320 frame, redrawn region by region.

The SPI glass is the bottleneck — a full frame takes seconds, a few hundred
pixels take milliseconds. So the screen is cut into fixed regions, each with a
signature of what it shows. A region is redrawn only when its signature
changes, and even then only the pixels that actually differ are reported: the
new tile is diffed against the retained frame, so a clock going from 1:23 to
1:24 pushes a single digit and a progress bar growing by a pixel pushes a
one-pixel column.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image, ImageChops, ImageDraw, ImageFont

from .hw import Box

log = logging.getLogger(__name__)

W, H = 480, 320
FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")

# the web UI's dark palette
BG = (14, 16, 19)
SURFACE = (30, 32, 36)
SURFACE_HI = (52, 54, 59)
LINE = (46, 48, 52)
TEXT = (236, 233, 228)
DIM = (163, 160, 153)
FAINT = (104, 102, 97)
ACCENT = (216, 166, 87)
PRESSED_ON_ACCENT = (255, 250, 240)
ACCENT_TEXT = (232, 183, 106)
ON_ACCENT = (20, 17, 11)
ERR = (217, 111, 111)

STRINGS = {
    "cs": {
        "playing": "hraje",
        "loading": "načítám…",
        "paused": "pozastaveno",
        "idle": "ticho",
        "offline": "odpojeno",
        "connecting": "připojuji…",
        "busy": "DJ přemýšlí…",
        "silence": "Ticho",
        "nothing": "nic nehraje",
        "idle_hint": "Ťukni na Hrát, nebo si řekni o Přání",
        "dj_picking": "DJ vybírá hudbu…",
        "next_up": "Pak: ",
        "wish_of": "přeje si {who}",
        "outage": "výpadek spojení",
        "outage_line": "Vypadl YouTube — čekám, fronta i přání zůstávají",
        "dj_offline": "DJ bez mozku",
        "wish": "Přání",
        "unknown": "Neznámá skladba",
        "offline_title": "ytdj neběží",
        "connecting_title": "Připojuji se k ytdj…",
        "waiting": "čekám na {target}",
        "play": "Hrát",
        "pause": "Pauza",
        "next": "Další",
        "failed": "povel selhal",
        "closed": "panel vypnut",
    },
    "en": {
        "playing": "playing",
        "loading": "loading…",
        "paused": "paused",
        "idle": "idle",
        "offline": "offline",
        "connecting": "connecting…",
        "busy": "DJ is thinking…",
        "silence": "Silence",
        "nothing": "nothing playing",
        "idle_hint": "Tap Play, or make a Wish",
        "dj_picking": "the DJ is picking music…",
        "next_up": "Then: ",
        "wish_of": "{who}'s wish",
        "outage": "connection lost",
        "outage_line": "Lost the connection to YouTube — waiting, the queue and wishes stay",
        "dj_offline": "DJ offline",
        "wish": "Wish",
        "unknown": "Unknown track",
        "offline_title": "ytdj is not running",
        "connecting_title": "Connecting to ytdj…",
        "waiting": "waiting for {target}",
        "play": "Play",
        "pause": "Pause",
        "next": "Next",
        "failed": "command failed",
        "closed": "panel stopped",
    },
}


# ---- layout (boxes are left, top, right, bottom — right/bottom exclusive) ----

NET_W = 58
WISH_W = 112
STATUS = (0, 0, W - NET_W - WISH_W, 32)
NET_BTN = (W - NET_W, 0, W, 32)  # the network button's drawing, in the status strip
# …and its touch target: taller than the strip it sits in, nothing else is there
NET_TARGET = (W - NET_W - 6, 0, W, 44)
# the wish button ("Přání" → the screen for typing a wish), left of the network one
WISH_BTN = (W - NET_W - WISH_W, 0, W - NET_W, 32)
WISH_TARGET = (W - NET_W - WISH_W, 0, W - NET_W - 6, 44)
TRACK = (0, 34, W, 126)
# ťuknutí na název/„Pak:“ otevře frontu přání (pod tlačítky v liště, bez překryvu)
TRACK_TARGET = (0, 46, W, 126)
ELAPSED = (6, 128, 82, 160)
BAR = (82, 128, 398, 160)
TOTAL = (398, 128, 474, 160)
PLAY = (8, 166, 236, 240)
NEXT = (244, 166, 472, 240)
VOL_DOWN = (8, 248, 80, 316)
VOL = (88, 248, 392, 316)
VOL_UP = (400, 248, 472, 316)

# the volume track inside VOL, in VOL-local x; the knob must fit at both ends
VOL_NUM_W = 62
KNOB_R = 13
VOL_TRACK_X = (VOL_NUM_W + KNOB_R + 4, VOL[2] - VOL[0] - KNOB_R - 10)

# what can be touched; the order doesn't matter, they don't overlap
TARGETS: dict[str, Box] = {
    "play": PLAY,
    "next": NEXT,
    "vol_down": VOL_DOWN,
    "vol": VOL,
    "vol_up": VOL_UP,
    "net": NET_TARGET,
    "wish": WISH_TARGET,
    "queue": TRACK_TARGET,
}


def volume_at(x: int, vol_max: int) -> int:
    """Screen x on the volume bar → volume, clamped to 0..vol_max."""
    x0, x1 = VOL[0] + VOL_TRACK_X[0], VOL[0] + VOL_TRACK_X[1]
    frac = (x - x0) / (x1 - x0)
    return max(0, min(vol_max, round(frac * vol_max)))


@dataclass(frozen=True)
class View:
    """Everything the screen shows — the renderer draws nothing else."""

    online: bool = False
    connecting: bool = True  # never been online yet
    target: str = ""  # host:port, for the offline message
    has_track: bool = False
    title: str = ""
    artist: str = ""
    running: bool = False  # playing and not paused
    loading: bool = False  # running, but the stream hasn't started yet
    paused: bool = False
    skipping: bool = False  # Next pressed, new track not here yet
    elapsed: int = 0
    duration: int = 0
    volume: int = 0
    vol_max: int = 100
    mood: str = ""
    busy: bool = False
    note: str = ""  # short-lived error
    pressed: str | None = None
    can_next: bool = False
    closed: bool = False
    net: tuple = ("?",)  # ("wifi", signal) | ("eth",) | ("off",) | ("?",) — the network button
    next_title: str = ""  # the first track in the queue, shown under the artist when there's room
    next_artist: str = ""
    next_who: str = ""  # whose wish the next track is ("" = the DJ's own pick)
    now_who: str = ""  # whose wish plays now — in the status strip instead of the mood
    wishes: int = 0  # wishes waiting or playing — "Přání 3" on the button
    outage: bool = False  # YouTube / síť vypadly — nic nehraje, fronta čeká
    dj_offline: bool = False  # mozek DJe nejede (jistič Codexu)


# ---- helpers ----


def fmt_time(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def ellipsize(text: str, font: ImageFont.FreeTypeFont, max_w: float) -> str:
    if font.getlength(text) <= max_w:
        return text
    lo, hi = 0, len(text)
    while lo < hi:  # longest prefix that still fits together with "…"
        mid = (lo + hi + 1) // 2
        if font.getlength(text[:mid].rstrip() + "…") <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + "…"


def wrap(text: str, font: ImageFont.FreeTypeFont, max_w: float, lines: int) -> list[str]:
    """Word-wraps into at most `lines` lines, the last one ellipsized."""
    words = text.split()
    out: list[str] = []
    while words and len(out) < lines - 1:
        line = words[0]
        if font.getlength(line) > max_w:
            # one word longer than the line: break it by characters
            cut = len(line)
            while cut > 1 and font.getlength(line[:cut]) > max_w:
                cut -= 1
            out.append(line[:cut])
            words[0] = line[cut:]
            continue
        n = 1
        while n < len(words) and font.getlength(line + " " + words[n]) <= max_w:
            line += " " + words[n]
            n += 1
        out.append(line)
        words = words[n:]
    if words:
        out.append(ellipsize(" ".join(words), font, max_w))
    return out


def merge_boxes(boxes: list[Box], slack: int = 1500) -> list[Box]:
    """Merges boxes that overlap, or whose union costs little extra area.

    Every show() has a fixed cost (addressing a window on the controller), so
    two nearby boxes are cheaper as one; two far-apart ones are not.
    """
    boxes = list(boxes)
    changed = True
    while changed:
        changed = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                u = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))
                overlap = a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]
                if overlap or _area(u) <= _area(a) + _area(b) + slack:
                    boxes[i] = u
                    del boxes[j]
                    changed = True
                    break
            if changed:
                break
    return boxes


def _area(b: Box) -> int:
    return (b[2] - b[0]) * (b[3] - b[1])


class Fonts:
    def __init__(self) -> None:
        self.status = self._load("DejaVuSans.ttf", 16)
        self.status_b = self._load("DejaVuSans-Bold.ttf", 16)
        self.title_big = self._load("DejaVuSans-Bold.ttf", 30)
        self.title = self._load("DejaVuSans-Bold.ttf", 25)
        self.artist = self._load("DejaVuSans.ttf", 20)
        self.time = self._load("DejaVuSans.ttf", 17)
        self.button = self._load("DejaVuSans-Bold.ttf", 21)
        self.volume = self._load("DejaVuSans-Bold.ttf", 22)

    @staticmethod
    def _load(name: str, size: int) -> ImageFont.FreeTypeFont:
        try:
            return ImageFont.truetype(str(FONT_DIR / name), size)
        except OSError:
            # without DejaVu (fonts-dejavu-core) at least something shows;
            # the bitmap fallback has no diacritics
            log.warning("font %s chybí, používám náhradní", name)
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                return ImageFont.load_default()  # type: ignore[return-value]


# ---- icons (drawn, not glyphs: crisp at any size, no font coverage worries) ----


def icon_play(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float, fill) -> None:
    d.polygon([(cx - s * 0.36, cy - s * 0.5), (cx - s * 0.36, cy + s * 0.5), (cx + s * 0.5, cy)], fill=fill)


def icon_pause(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float, fill) -> None:
    bw, gap = s * 0.3, s * 0.2
    d.rounded_rectangle((cx - gap / 2 - bw, cy - s / 2, cx - gap / 2, cy + s / 2), radius=2, fill=fill)
    d.rounded_rectangle((cx + gap / 2, cy - s / 2, cx + gap / 2 + bw, cy + s / 2), radius=2, fill=fill)


def icon_next(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float, fill) -> None:
    x0 = cx - s * 0.45
    d.polygon([(x0, cy - s * 0.48), (x0, cy + s * 0.48), (x0 + s * 0.72, cy)], fill=fill)
    d.rectangle((cx + s * 0.3, cy - s * 0.48, cx + s * 0.45, cy + s * 0.48), fill=fill)


def icon_minus(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float, fill) -> None:
    d.rectangle((cx - s / 2, cy - 2, cx + s / 2, cy + 2), fill=fill)


def icon_plus(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float, fill) -> None:
    icon_minus(d, cx, cy, s, fill)
    d.rectangle((cx - 2, cy - s / 2, cx + 2, cy + s / 2), fill=fill)


def icon_bubble(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float, fill) -> None:
    """A speech bubble — "say what you want to hear"."""
    w, h = s, s * 0.72
    x0, y0 = cx - w / 2, cy - h / 2 - s * 0.08
    d.rounded_rectangle((x0, y0, x0 + w, y0 + h), radius=s * 0.22, fill=fill)
    d.polygon([(x0 + w * 0.22, y0 + h - 1), (x0 + w * 0.22, y0 + h + s * 0.26), (x0 + w * 0.5, y0 + h - 1)], fill=fill)
    for i in (-1, 0, 1):  # three dots of "…"
        r = s * 0.06
        px, py = cx + i * s * 0.24, y0 + h / 2
        d.ellipse((px - r, py - r, px + r, py + r), fill=BG)


def icon_wifi(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float, lit: int, on, off) -> None:
    """A Wi-Fi fan with `lit` of its 4 levels (dot + 3 arcs) coloured `on`; cy is the dot."""
    r = s * 0.13
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=on if lit >= 1 else off)
    for i in range(1, 4):
        rr = s * (0.12 + 0.3 * i)
        d.arc((cx - rr, cy - rr, cx + rr, cy + rr), 225, 315, fill=on if lit > i else off, width=max(2, round(s * 0.13)))


def icon_eth(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float, fill) -> None:
    """An RJ45 socket seen from the front."""
    w, h = s * 0.9, s * 0.7
    x0, y0 = cx - w / 2, cy - h / 2
    d.rectangle((x0, y0, x0 + w, y0 + h), outline=fill, width=2)
    d.rectangle((cx - w * 0.2, y0 + h - 1, cx + w * 0.2, y0 + h + s * 0.15), fill=fill)
    for i in range(4):
        x = x0 + w * (0.2 + i * 0.2)
        d.line((x, y0 + 3, x, y0 + h * 0.45), fill=fill, width=2)


# ---- renderer ----


Draw = Callable[[ImageDraw.ImageDraw, tuple[int, int], View], None]


class Renderer:
    def __init__(self, lang: str = "cs") -> None:
        self.frame = Image.new("RGB", (W, H), BG)
        self.fonts = Fonts()
        self.s = STRINGS.get(lang, STRINGS["cs"])
        self._sigs: dict[str, tuple] = {}
        # name, box, signature, draw
        self._regions: list[tuple[str, Box, Callable[[View], tuple], Draw]] = [
            ("status", STATUS, self._sig_status, self._draw_status),
            ("net", NET_BTN, lambda v: (v.net, v.pressed == "net", v.closed), self._draw_net),
            ("wish", WISH_BTN, lambda v: (v.online, v.pressed == "wish", v.closed, v.wishes), self._draw_wish),
            ("track", TRACK, self._sig_track, self._draw_track),
            ("elapsed", ELAPSED, lambda v: (self._has_time(v), v.elapsed), self._draw_elapsed),
            ("bar", BAR, self._sig_bar, self._draw_bar),
            ("total", TOTAL, lambda v: (self._has_time(v), v.duration), self._draw_total),
            ("play", PLAY, lambda v: (v.online, v.running, v.pressed == "play"), self._draw_play),
            ("next", NEXT, lambda v: (v.online and v.can_next, v.pressed == "next"), self._draw_next),
            ("vol_down", VOL_DOWN, lambda v: (v.online, v.pressed == "vol_down"), self._draw_vol_down),
            ("vol", VOL, lambda v: (v.online, v.volume, v.vol_max, v.pressed == "vol"), self._draw_vol),
            ("vol_up", VOL_UP, lambda v: (v.online, v.pressed == "vol_up"), self._draw_vol_up),
        ]

    def invalidate(self) -> None:
        self._sigs.clear()

    def render(self, v: View, full: bool = False) -> list[Box]:
        """Brings the frame up to `v`; returns the boxes that must go to the glass."""
        dirty: list[Box] = []
        for name, box, sig_fn, draw_fn in self._regions:
            sig = sig_fn(v)
            if not full and self._sigs.get(name) == sig:
                continue
            self._sigs[name] = sig
            tile = Image.new("RGB", (box[2] - box[0], box[3] - box[1]), BG)
            draw_fn(ImageDraw.Draw(tile), tile.size, v)
            if not full:
                bb = ImageChops.difference(tile, self.frame.crop(box)).getbbox()
                if bb:
                    dirty.append((box[0] + bb[0], box[1] + bb[1], box[0] + bb[2], box[1] + bb[3]))
            self.frame.paste(tile, box[:2])
        if full:
            return [(0, 0, W, H)]
        return merge_boxes(dirty)

    # ---- status strip ----

    def _sig_status(self, v: View) -> tuple:
        return (v.online, v.connecting, v.has_track, v.running, v.loading, v.paused, v.mood, v.busy, v.note,
                v.closed, v.now_who, v.outage, v.dj_offline)

    def _draw_status(self, d: ImageDraw.ImageDraw, size: tuple[int, int], v: View) -> None:
        w, h = size
        f, fb = self.fonts.status, self.fonts.status_b
        cy = h // 2
        d.line((0, h - 1, w, h - 1), fill=LINE)

        if v.closed:
            # the process is going away; the rest of the screen stays as it was
            d.ellipse((12, cy - 5, 22, cy + 5), fill=FAINT)
            d.text((32, cy), self.s["closed"], font=fb, fill=FAINT, anchor="lm")
            return

        right, right_color = "", DIM
        if v.note:
            right, right_color = v.note, ERR
        elif v.online and v.busy:
            right, right_color = self.s["busy"], ACCENT_TEXT
        elif v.online and v.dj_offline:
            right, right_color = self.s["dj_offline"], ERR
        right_w = 0
        if right:
            right = ellipsize(right, f, w * 0.45)
            right_w = int(f.getlength(right)) + 18
            d.text((w - 12, cy), right, font=f, fill=right_color, anchor="rm")

        if not v.online:
            label = self.s["connecting"] if v.connecting else self.s["offline"]
            dot, color = (FAINT if v.connecting else ERR), (DIM if v.connecting else ERR)
        elif v.outage:
            # nic nehraje, i když by mpv tvrdilo "hraje" — čeká se na síť
            label, dot, color = self.s["outage"], ERR, ERR
        elif not v.has_track:
            label, dot, color = self.s["idle"], FAINT, DIM
        elif v.running and v.loading:
            label, dot, color = self.s["loading"], FAINT, DIM
        elif v.running:
            label, dot, color = self.s["playing"], ACCENT, ACCENT_TEXT
        else:
            label, dot, color = self.s["paused"], DIM, DIM
        d.ellipse((12, cy - 5, 22, cy + 5), fill=dot)
        x = 32
        d.text((x, cy), label, font=fb, fill=color, anchor="lm")
        x += fb.getlength(label)
        # whose wish plays — or, for the DJ's own picks, the mood
        extra = self.s["wish_of"].format(who=v.now_who) if (v.now_who and v.has_track) else v.mood
        if v.online and extra:
            room = w - right_w - 12 - x - fb.getlength(" · ")
            if room > 40:
                d.text((x, cy), " · ", font=f, fill=FAINT, anchor="lm")
                x += f.getlength(" · ")
                d.text((x, cy), ellipsize(extra, f, room), font=f,
                       fill=ACCENT_TEXT if v.now_who and v.has_track else DIM, anchor="lm")

    def _draw_net(self, d: ImageDraw.ImageDraw, size: tuple[int, int], v: View) -> None:
        w, h = size
        d.line((0, h - 1, w, h - 1), fill=LINE)
        if v.closed:
            return
        pressed = v.pressed == "net"
        d.rounded_rectangle((4, 3, w - 8, h - 6), radius=8, fill=SURFACE_HI if pressed else SURFACE)
        cx, cy = (w - 4) / 2, (h - 3) / 2
        kind = v.net[0] if v.net else "?"
        on = ACCENT_TEXT if pressed else TEXT
        if kind == "wifi":
            sig = v.net[1] if len(v.net) > 1 else -1
            lit = 4 if sig < 0 else 1 + min(3, max(0, sig) // 25)
            icon_wifi(d, cx, cy + 7, 17, lit, on, FAINT)
        elif kind == "eth":
            icon_eth(d, cx, cy - 1, 20, on)
        else:
            icon_wifi(d, cx, cy + 7, 17, 0, on, FAINT if kind == "?" else ERR)

    def _draw_wish(self, d: ImageDraw.ImageDraw, size: tuple[int, int], v: View) -> None:
        w, h = size
        d.line((0, h - 1, w, h - 1), fill=LINE)
        if v.closed:
            return
        pressed = v.pressed == "wish"
        d.rounded_rectangle((4, 3, w - 4, h - 6), radius=8, fill=SURFACE_HI if pressed else SURFACE)
        f = self.fonts.status_b
        label = self.s["wish"] + (f" {v.wishes}" if v.wishes else "")
        icon_w = 20
        x = (w - (icon_w + 8 + f.getlength(label))) / 2
        cy = (h - 3) / 2
        if v.online:
            icon_color, color = ACCENT, (ACCENT_TEXT if pressed else TEXT)
        else:
            icon_color = color = FAINT
        icon_bubble(d, x + icon_w / 2, cy, icon_w, icon_color)
        d.text((x + icon_w + 8, cy), label, font=f, fill=color, anchor="lm")

    # ---- title + artist ----

    def _sig_track(self, v: View) -> tuple:
        if not v.online:
            return ("off", v.connecting, v.target)
        if not v.has_track:
            return ("idle", v.busy)
        return ("track", v.title, v.artist, v.skipping, v.next_title, v.next_artist, v.next_who,
                v.outage)

    def _draw_track(self, d: ImageDraw.ImageDraw, size: tuple[int, int], v: View) -> None:
        w, h = size
        x, max_w = 14, w - 28
        if not v.online:
            title = self.s["connecting_title"] if v.connecting else self.s["offline_title"]
            sub = self.s["waiting"].format(target=v.target)
            title_color, sub_color = DIM if v.connecting else TEXT, FAINT
        elif not v.has_track:
            title, title_color = self.s["silence"], DIM
            # what the two big buttons do when nothing plays — "Hrát" starts
            # the DJ, which isn't obvious from a play icon
            sub = self.s["dj_picking"] if v.busy else self.s["idle_hint"]
            sub_color = ACCENT_TEXT if v.busy else DIM
        else:
            title = v.title.strip() or self.s["unknown"]
            sub = v.artist.strip()
            title_color = FAINT if v.skipping else TEXT
            sub_color = FAINT if v.skipping else DIM

        big = self.fonts.title_big
        if big.getlength(title) <= max_w:
            lines, font, step = [title], big, 38
        else:
            font, step = self.fonts.title, 31
            lines = wrap(title, font, max_w, 2)
        y = 6
        for line in lines:
            d.text((x, y), line, font=font, fill=title_color, anchor="la")
            y += step
        if sub:
            y = max(y + 2, 48) if len(lines) == 1 else y + 1
            d.text((x, y), ellipsize(sub, self.fonts.artist, max_w), font=self.fonts.artist, fill=sub_color, anchor="la")
        if v.online and v.outage and len(lines) == 1:
            d.text((x, 74), ellipsize(self.s["outage_line"], self.fonts.status, max_w),
                   font=self.fonts.status, fill=ERR, anchor="la")
        elif v.online and v.has_track and v.next_title and len(lines) == 1 and not v.skipping:
            # what "Další" would bring — only when the title leaves room for it
            f = self.fonts.status
            label = self.s["next_up"]
            by = self.s["wish_of"].format(who=v.next_who.strip()) if v.next_who.strip() else v.next_artist.strip()
            nxt = v.next_title.strip() + (f" · {by}" if by else "")
            d.text((x, 74), label, font=f, fill=FAINT, anchor="la")
            lx = x + f.getlength(label)
            d.text((lx, 74), ellipsize(nxt, f, max_w - (lx - x)), font=f, fill=DIM, anchor="la")

    # ---- progress ----

    @staticmethod
    def _has_time(v: View) -> bool:
        return v.online and v.has_track

    def _draw_elapsed(self, d: ImageDraw.ImageDraw, size: tuple[int, int], v: View) -> None:
        if self._has_time(v):
            d.text((8, size[1] // 2), fmt_time(v.elapsed), font=self.fonts.time, fill=DIM, anchor="lm")

    def _draw_total(self, d: ImageDraw.ImageDraw, size: tuple[int, int], v: View) -> None:
        if self._has_time(v) and v.duration > 0:
            d.text((size[0] - 8, size[1] // 2), fmt_time(v.duration), font=self.fonts.time, fill=DIM, anchor="rm")

    def _bar_px(self, v: View) -> int:
        span = BAR[2] - BAR[0] - 16
        if not self._has_time(v) or v.duration <= 0:
            return -1
        return round(span * min(1.0, max(0.0, v.elapsed / v.duration)))

    def _sig_bar(self, v: View) -> tuple:
        # pixels, not seconds: a long track moves the bar less than once a second
        return (self._has_time(v), self._bar_px(v), v.running)

    def _draw_bar(self, d: ImageDraw.ImageDraw, size: tuple[int, int], v: View) -> None:
        w, h = size
        cy = h // 2
        x0, x1 = 8, w - 8
        d.rounded_rectangle((x0, cy - 3, x1, cy + 3), radius=3, fill=LINE)
        px = self._bar_px(v)
        if px > 0:
            d.rounded_rectangle((x0, cy - 3, x0 + px, cy + 3), radius=3, fill=ACCENT if v.running else DIM)

    # ---- buttons ----

    @staticmethod
    def _button(d: ImageDraw.ImageDraw, size: tuple[int, int], fill) -> None:
        d.rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=14, fill=fill)

    def _icon_label(self, d, size, icon, label: str, color) -> None:
        w, h = size
        f = self.fonts.button
        icon_s = 26
        total = icon_s + 14 + f.getlength(label)
        x = (w - total) / 2
        icon(d, x + icon_s / 2, h / 2, icon_s, color)
        d.text((x + icon_s + 14, h / 2), label, font=f, fill=color, anchor="lm")

    def _draw_play(self, d: ImageDraw.ImageDraw, size: tuple[int, int], v: View) -> None:
        # Pressing recolours only the icon and label, never the whole button:
        # on the SPI glass a label is a few thousand pixels, a button 17 000.
        if v.online:
            self._button(d, size, ACCENT)
            color = PRESSED_ON_ACCENT if v.pressed == "play" else ON_ACCENT
        else:
            self._button(d, size, SURFACE)
            color = FAINT
        if v.running:
            self._icon_label(d, size, icon_pause, self.s["pause"], color)
        else:
            self._icon_label(d, size, icon_play, self.s["play"], color)

    def _plain_button(self, d, size, v: View, name: str, enabled: bool) -> object:
        self._button(d, size, SURFACE)
        if not enabled:
            return FAINT
        return ACCENT_TEXT if v.pressed == name else TEXT

    def _draw_next(self, d: ImageDraw.ImageDraw, size: tuple[int, int], v: View) -> None:
        color = self._plain_button(d, size, v, "next", v.online and v.can_next)
        self._icon_label(d, size, icon_next, self.s["next"], color)

    def _draw_vol_down(self, d: ImageDraw.ImageDraw, size: tuple[int, int], v: View) -> None:
        color = self._plain_button(d, size, v, "vol_down", v.online)
        icon_minus(d, size[0] / 2, size[1] / 2, 26, color)

    def _draw_vol_up(self, d: ImageDraw.ImageDraw, size: tuple[int, int], v: View) -> None:
        color = self._plain_button(d, size, v, "vol_up", v.online)
        icon_plus(d, size[0] / 2, size[1] / 2, 26, color)

    def _draw_vol(self, d: ImageDraw.ImageDraw, size: tuple[int, int], v: View) -> None:
        w, h = size
        cy = h // 2
        dragging = v.pressed == "vol"
        self._button(d, size, SURFACE)
        num_color = (ACCENT_TEXT if dragging else TEXT) if v.online else FAINT
        d.text((VOL_NUM_W - 6, cy), str(v.volume) if v.online else "–", font=self.fonts.volume, fill=num_color, anchor="rm")

        x0, x1 = VOL_TRACK_X
        d.rounded_rectangle((x0, cy - 4, x1, cy + 4), radius=4, fill=SURFACE_HI)
        if not v.online:
            return
        frac = min(1.0, max(0.0, v.volume / max(1, v.vol_max)))
        kx = round(x0 + (x1 - x0) * frac)
        d.rounded_rectangle((x0, cy - 4, kx, cy + 4), radius=4, fill=ACCENT)
        r = KNOB_R + (2 if dragging else 0)
        d.ellipse((kx - r, cy - r, kx + r, cy + r), fill=ACCENT_TEXT if dragging else TEXT)
