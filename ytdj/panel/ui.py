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

import colorsys
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image, ImageChops, ImageDraw, ImageFont

from .hw import Box
from .qr import encode as qr_encode

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
WARN = (217, 140, 95)

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
        "idle_hint": "Ťukni na Hrát a DJ vybere hudbu podle času a dne.",
        "qr_caption": "‹ Přání z mobilu: naskenuj kód",
        "voted_out": "vyřazená hlasováním",
        "artist_voted_out": "interpret vyřazen hlasováním",
        "next_voted_out": "vyřazená ",
        "toast_says": " si přeje: ",
        "toast_thinking": "DJ vybírá…",
        "toast_playing": "hraje",
        "qr_title": "Přání z mobilu",
        "qr_steps": ("Naskenuj kód telefonem", "Napiš, co chceš slyšet", "Hlasuj ▲ ▼ u písniček"),
        "qr_back": "Ťukni kamkoli pro návrat",
        "qr_none": "Síť zatím neznám — zkus to za chvíli.",
        "dj_picking": "DJ vybírá hudbu…",
        "next_up": "Pak: ",
        "wish_of": "přeje si {who}",
        "wish_by": "přeje si ",
        "more": "+{n} přání ›",
        "waiting_wishes": "Čeká {n} přání ›",
        "outage": "čekám na YouTube",
        "outage_line": "YouTube teď nehraje · zkouším znovu · přání počkají",
        "outage_network": "Bez spojení s YouTube · zkouším znovu · přání počkají",
        "outage_youtube_login": "YouTube chce znovu ověřit přihlášení · přání počkají",
        "outage_youtube_limit": "YouTube nás brzdí · zkouším znovu · přání počkají",
        "dj_offline": "DJ bez AI",
        "wish": "Přání",
        "unknown": "Neznámá skladba",
        "offline_title": "ytdj neodpovídá",
        "connecting_title": "Připojuji se k ytdj…",
        "waiting": "zkouším to znovu · {target}",
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
        "idle_hint": "Tap Play and the DJ picks music for the time and day.",
        "qr_caption": "‹ Wishes from a phone: scan the code",
        "voted_out": "voted out",
        "artist_voted_out": "artist voted out",
        "next_voted_out": "voted out ",
        "toast_says": " wishes: ",
        "toast_thinking": "DJ is picking…",
        "toast_playing": "playing",
        "qr_title": "Wishes from a phone",
        "qr_steps": ("Scan the code with a phone", "Type what you want to hear", "Vote ▲ ▼ on the songs"),
        "qr_back": "Tap anywhere to go back",
        "qr_none": "The network isn't known yet — try again in a moment.",
        "dj_picking": "the DJ is picking music…",
        "next_up": "Then: ",
        "wish_of": "{who}'s wish",
        "wish_by": "wish of ",
        "more": "+{n} wishes ›",
        "waiting_wishes": "{n} wishes waiting ›",
        "outage": "waiting for YouTube",
        "outage_line": "YouTube isn't playing — retrying, wishes will wait",
        "outage_network": "Can't reach YouTube — retrying, wishes will wait",
        "outage_youtube_login": "YouTube wants the login checked — wishes will wait",
        "outage_youtube_limit": "YouTube is throttling us — retrying, wishes will wait",
        "dj_offline": "DJ without AI",
        "wish": "Wish",
        "unknown": "Unknown track",
        "offline_title": "ytdj is not answering",
        "connecting_title": "Connecting to ytdj…",
        "waiting": "retrying · {target}",
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
PHONE_W = 54
STATUS = (0, 0, W - NET_W - WISH_W - PHONE_W, 32)
NET_BTN = (W - NET_W, 0, W, 32)  # the network button's drawing, in the status strip
# …and its touch target: taller than the strip it sits in, nothing else is there
NET_TARGET = (W - NET_W - 6, 0, W, 44)
# the wish button ("Přání" → the screen for typing a wish), left of the network one
WISH_BTN = (W - NET_W - WISH_W, 0, W - NET_W, 32)
WISH_TARGET = (W - NET_W - WISH_W, 0, W - NET_W - 6, 44)
# the phone button (→ a full-screen QR code to the web: wishes from a phone)
PHONE_BTN = (W - NET_W - WISH_W - PHONE_W, 0, W - NET_W - WISH_W, 32)
PHONE_TARGET = (W - NET_W - WISH_W - PHONE_W, 0, W - NET_W - WISH_W, 44)
# a banner over the whole strip for a few seconds when somebody wishes something
TOAST = (0, 0, W, 32)
# Cover art (or, in silence, the QR code for wishes from a phone) on the left;
# title, artist and "Pak:" right of it; the time under the text.
ART = (0, 34, 160, 198)
ART_SIDE = 144
ART_AT = (8, 6)  # the tile's top-left inside ART
TRACK = (160, 34, W, 166)
# ťuknutí na název/„Pak:“ otevře frontu přání (pod tlačítky v liště, bez překryvu)
TRACK_TARGET = (160, 46, W, 166)
ELAPSED = (160, 166, 232, 198)
BAR = (232, 166, 400, 198)
TOTAL = (400, 166, 474, 198)
PLAY = (8, 204, 236, 256)
NEXT = (244, 204, 472, 256)
VOL_DOWN = (8, 262, 80, 316)
VOL = (88, 262, 392, 316)
VOL_UP = (400, 262, 472, 316)

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
    "phone": PHONE_TARGET,
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
    outage_reason: str = ""  # "network" | "dns" | "youtube_login" | "youtube_limit" | …
    more_wishes: int = 0  # wishes waiting besides the one now playing and the next one
    dj_offline: bool = False  # mozek DJe nejede (jistič Codexu)
    track_id: str = ""  # YouTube video id of the current track — its cover art
    art_ready: bool = False  # the cover is fetched (Renderer.art_source has it)
    qr_url: str = ""  # the web on the LAN, for the QR code ("" = network not known yet)
    rest: bool = False  # nothing has happened for a while: the calm screen
    toast: tuple = ()  # (who, text, tail) — "Petr si přeje: …" for a few seconds
    # office votes on the current track (current.votes): 👍/👎 counts and the verdict
    vote_up: int = 0
    vote_down: int = 0
    vote_status: str = ""  # "favourite" | "downweighted" | "banned" | "neutral"
    artist_banned: bool = False  # one of its artists is voted out
    next_vote: str = ""  # queue[0]: "favourite" | "banned" | ""


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


def vote_mark(votes: object) -> str:
    """queue[].votes / current.votes → "banned" | "favourite" | "" (the small marker)."""
    if not isinstance(votes, dict):
        return ""
    if votes.get("status") == "banned" or votes.get("artist_status") == "banned":
        return "banned"
    arts = votes.get("artists")
    if isinstance(arts, list) and any(isinstance(a, dict) and a.get("status") == "banned" for a in arts):
        return "banned"
    return "favourite" if votes.get("status") == "favourite" else ""


def who_color(name: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """A name → (fill, text) colours; the same person looks the same everywhere."""
    h = 0
    for ch in name or "?":
        h = (h * 31 + ord(ch)) % 360
    fill = colorsys.hls_to_rgb(h / 360, 0.26, 0.45)
    text = colorsys.hls_to_rgb(h / 360, 0.78, 0.6)
    return (tuple(int(c * 255) for c in fill), tuple(int(c * 255) for c in text))  # type: ignore[return-value]


def name_chip(d: ImageDraw.ImageDraw, x: float, cy: float, name: str, font: ImageFont.FreeTypeFont,
              max_w: float = 110, pad: int = 7) -> float:
    """The coloured name tag of whoever wished something; returns its width."""
    fill, text = who_color(name)
    label = ellipsize(name, font, max_w)
    tw = font.getlength(label)
    half = font.size // 2 + 4
    d.rounded_rectangle((x, cy - half, x + tw + 2 * pad, cy + half), radius=half, fill=fill)
    d.text((x + pad, cy), label, font=font, fill=text, anchor="lm")
    return tw + 2 * pad


class Fonts:
    def __init__(self) -> None:
        self.status = self._load("DejaVuSans.ttf", 16)
        self.status_b = self._load("DejaVuSans-Bold.ttf", 16)
        # the title steps down until it fits: one line as big as possible,
        # then two lines, then two smaller ones (only then an ellipsis)
        self.title_xl = self._load("DejaVuSans-Bold.ttf", 34)
        self.title_big = self._load("DejaVuSans-Bold.ttf", 30)
        self.title = self._load("DejaVuSans-Bold.ttf", 27)
        self.title_sm = self._load("DejaVuSans-Bold.ttf", 24)
        self.artist = self._load("DejaVuSans.ttf", 22)
        self.artist_sm = self._load("DejaVuSans.ttf", 20)
        self.hint = self._load("DejaVuSans.ttf", 18)
        self.chip = self._load("DejaVuSans-Bold.ttf", 14)
        self.initials = self._load("DejaVuSans-Bold.ttf", 52)
        self.artist_b = self._load("DejaVuSans-Bold.ttf", 22)
        self.artist_sm_b = self._load("DejaVuSans-Bold.ttf", 20)
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


def icon_phone(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float, fill) -> None:
    """A phone with a QR-ish pattern on its screen — "wishes from your phone"."""
    w, h = s * 0.62, s
    x0, y0 = cx - w / 2, cy - h / 2
    d.rounded_rectangle((x0, y0, x0 + w, y0 + h), radius=3, outline=fill, width=2)
    q = w * 0.22
    for qx, qy in ((x0 + 3.5, y0 + 4), (x0 + w - 3.5 - q, y0 + 4), (x0 + 3.5, y0 + 4 + q + 2)):
        d.rectangle((qx, qy, qx + q, qy + q), fill=fill)
    d.rectangle((cx - 2, y0 + h - 5, cx + 2, y0 + h - 3), fill=fill)


def icon_note(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float, fill) -> None:
    """A beamed pair of eighth notes."""
    r = s * 0.16
    xa, xb = cx - s * 0.3, cx + s * 0.25
    ya, yb = cy + s * 0.32, cy + s * 0.22
    d.ellipse((xa - r * 1.2, ya - r, xa + r * 1.2, ya + r), fill=fill)
    d.ellipse((xb - r * 1.2, yb - r, xb + r * 1.2, yb + r), fill=fill)
    t = max(2, s * 0.07)
    d.rectangle((xa + r * 1.2 - t, cy - s * 0.4, xa + r * 1.2, ya), fill=fill)
    d.rectangle((xb + r * 1.2 - t, cy - s * 0.5, xb + r * 1.2, yb), fill=fill)
    d.polygon([(xa + r * 1.2 - t, cy - s * 0.4), (xb + r * 1.2, cy - s * 0.5),
               (xb + r * 1.2, cy - s * 0.36), (xa + r * 1.2 - t, cy - s * 0.26)], fill=fill)


QR_LIGHT = (236, 233, 228)
QR_DARK = (14, 16, 19)


def draw_qr(d: ImageDraw.ImageDraw, x0: int, y0: int, max_side: int, m: list[list[bool]]) -> int:
    """A QR code as big as fits in max_side, whole-pixel modules, quiet zone included; its side."""
    n = len(m)
    quiet = 2
    scale = max(1, max_side // (n + 2 * quiet))
    side = scale * (n + 2 * quiet)
    off = (max_side - side) // 2
    x0, y0 = x0 + off, y0 + off
    d.rectangle((x0, y0, x0 + side - 1, y0 + side - 1), fill=QR_LIGHT)
    ox, oy = x0 + quiet * scale, y0 + quiet * scale
    for y, row in enumerate(m):
        x = 0
        while x < n:  # runs of dark modules as one rectangle — far fewer calls
            if row[x]:
                x1 = x
                while x1 + 1 < n and row[x1 + 1]:
                    x1 += 1
                d.rectangle((ox + x * scale, oy + y * scale, ox + (x1 + 1) * scale - 1, oy + (y + 1) * scale - 1),
                            fill=QR_DARK)
                x = x1 + 1
            else:
                x += 1
    return side + off


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
        # video id → the cover tile (ART_SIDE² RGB) or None; set by the app (art.ArtCache.get)
        self.art_source: Callable[[str], Image.Image | None] = lambda vid: None
        self._qr_cache: tuple[str, list[list[bool]] | None] = ("", None)
        self.qr_box: Box | None = None  # where the QR code is on the frame (kept bright in rest)
        self._art_mask: Image.Image | None = None
        # name, box, signature, draw — the header regions give way to the wish banner
        self._regions: list[tuple[str, Box, Callable[[View], tuple], Draw]] = [
            ("status", STATUS, self._sig_status, self._draw_status),
            ("phone", PHONE_BTN, lambda v: (v.pressed == "phone", v.closed, bool(v.qr_url)), self._draw_phone),
            ("wish", WISH_BTN, lambda v: (v.online, v.pressed == "wish", v.closed, v.wishes), self._draw_wish),
            ("net", NET_BTN, lambda v: (v.net, v.pressed == "net", v.closed), self._draw_net),
            ("toast", TOAST, lambda v: (v.toast,), self._draw_toast),
            ("art", ART, self._sig_art, self._draw_art),
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

    HEADER = ("status", "phone", "wish", "net")

    def invalidate(self) -> None:
        self._sigs.clear()

    def render(self, v: View, full: bool = False) -> list[Box]:
        """Brings the frame up to `v`; returns the boxes that must go to the glass."""
        dirty: list[Box] = []
        toast = bool(v.toast) and not v.closed
        for name, box, sig_fn, draw_fn in self._regions:
            # the wish banner covers the header strip: while it shows, the
            # header regions stand still (and don't paint over it); the banner
            # itself paints only while there is one
            if name in self.HEADER and toast:
                sig: tuple = ("under-toast",)
                self._sigs[name] = sig
                continue
            if name == "toast" and not toast:
                self._sigs[name] = ("none",)
                continue
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

    # ---- status strip: one clear line ----

    def _sig_status(self, v: View) -> tuple:
        return (v.online, v.connecting, v.has_track, v.running, v.loading, v.paused, v.mood, v.busy, v.note,
                v.closed, v.now_who, v.outage, v.dj_offline)

    def _status_detail(self, v: View) -> tuple[str, tuple, str]:
        """The one thing said after the state: (text, colour, name for a tag)."""
        if v.note:
            return v.note, ERR, ""
        if not v.online or v.outage:
            return "", DIM, ""
        if v.busy and v.has_track:
            return self.s["busy"], ACCENT_TEXT, ""  # (in silence the text under "Ticho" says it)
        if v.now_who and v.has_track:
            return self.s["wish_by"], DIM, v.now_who
        if v.dj_offline:
            # the music plays on; only free-text wishes suffer — a warning, not an error
            return self.s["dj_offline"], WARN, ""
        return v.mood, DIM, ""

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

        text, tcolor, who = self._status_detail(v)
        sep = f.getlength(" · ")
        room = w - 8 - x - sep
        if not text or room < 40:
            return
        need = f.getlength(text) + (self.fonts.chip.getlength(who) + 14 if who else 0)
        if need > room and not (v.note or who):
            return  # the mood only when it fits whole — no "klidný v…" stubs
        d.text((x, cy), " · ", font=f, fill=FAINT, anchor="lm")
        x += sep
        if who:
            if f.getlength(text) + 50 <= room:
                d.text((x, cy), text, font=f, fill=tcolor, anchor="lm")
                room -= f.getlength(text)
                x += f.getlength(text)
            name_chip(d, x, cy, who, self.fonts.chip, max_w=room - 14)
        else:
            d.text((x, cy), ellipsize(text, f, room), font=f, fill=tcolor, anchor="lm")

    def _draw_phone(self, d: ImageDraw.ImageDraw, size: tuple[int, int], v: View) -> None:
        w, h = size
        d.line((0, h - 1, w, h - 1), fill=LINE)
        if v.closed:
            return
        pressed = v.pressed == "phone"
        d.rounded_rectangle((4, 3, w - 4, h - 6), radius=8, fill=SURFACE_HI if pressed else SURFACE)
        icon_phone(d, w / 2, (h - 3) / 2, 20, ACCENT_TEXT if pressed else (TEXT if v.qr_url else FAINT))

    def _draw_toast(self, d: ImageDraw.ImageDraw, size: tuple[int, int], v: View) -> None:
        """"[Petr] si přeje: Kabát · DJ vybírá…" — a few seconds, in accent, over the strip."""
        w, h = size
        who, text, tail = v.toast
        d.rectangle((0, 0, w, h), fill=ACCENT)
        f, fb = self.fonts.status, self.fonts.status_b
        cy = h // 2
        icon_bubble(d, 22, cy, 22, ON_ACCENT)
        x = 42.0
        x += name_chip(d, x, cy, who, self.fonts.chip, max_w=120) + 6
        says = self.s["toast_says"]
        d.text((x, cy), says, font=f, fill=ON_ACCENT, anchor="lm")
        x += f.getlength(says)
        tail_w = f.getlength(tail) if tail else 0
        room = w - 10 - x - tail_w
        d.text((x, cy), ellipsize(text, fb, room), font=fb, fill=ON_ACCENT, anchor="lm")
        if tail:
            d.text((w - 10, cy), tail, font=f, fill=ON_ACCENT, anchor="rm")

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

    # ---- cover art / QR ----

    def _shows_qr(self, v: View) -> bool:
        """Silence or rest: the art slot invites wishes from a phone instead."""
        return bool(v.qr_url) and v.online and (not v.has_track or v.rest)

    def _sig_art(self, v: View) -> tuple:
        if self._shows_qr(v):
            return ("qr", v.qr_url)
        if not v.online or not v.has_track:
            return ("none",)
        return ("art", v.track_id, v.art_ready, v.artist if not v.art_ready else "", v.skipping)

    def _qr(self, text: str) -> list[list[bool]] | None:
        if self._qr_cache[0] != text:
            try:
                m = qr_encode(text, "M")
            except ValueError:
                m = None
            self._qr_cache = (text, m)
        return self._qr_cache[1]

    def _draw_art(self, d: ImageDraw.ImageDraw, size: tuple[int, int], v: View) -> None:
        ax, ay = ART_AT
        side = ART_SIDE
        self.qr_box = None
        if self._shows_qr(v):
            m = self._qr(v.qr_url)
            if m is not None:
                # the whole slot: a module more per side makes it easier to catch from a step away
                qx, qy, qs = 4, 4, min(ART[2] - ART[0], ART[3] - ART[1]) - 8
                drawn = draw_qr(d, qx, qy, qs, m)
                self.qr_box = (ART[0] + qx, ART[1] + qy, ART[0] + qx + drawn, ART[1] + qy + drawn)
                return
        if not v.online or not v.has_track:
            d.rounded_rectangle((ax, ay, ax + side - 1, ay + side - 1), radius=12, fill=SURFACE)
            icon_note(d, ax + side / 2, ay + side / 2, side * 0.42, SURFACE_HI)
            return
        tile = self.art_source(v.track_id) if v.art_ready else None
        if tile is not None and tile.size == (side, side):
            if self._art_mask is None:  # rounded corners, like every other tile on the screen
                self._art_mask = Image.new("L", (side, side), 0)
                ImageDraw.Draw(self._art_mask).rounded_rectangle((0, 0, side - 1, side - 1), radius=12, fill=255)
            d._image.paste(tile, (ax, ay), self._art_mask)  # type: ignore[attr-defined]
        else:
            self._fallback_tile(d, ax, ay, side, v.artist or v.title)

    def _fallback_tile(self, d, x: int, y: int, side: int, name: str) -> None:
        """No cover (offline, not found): the artist's initials on their own colour."""
        fill, text = who_color(name.strip().lower() or "?")
        d.rounded_rectangle((x, y, x + side - 1, y + side - 1), radius=12, fill=fill)
        words = [w for w in name.replace("&", " ").split() if w[:1].isalnum()]
        initials = "".join(w[0] for w in words[:2]).upper() or "♪"
        d.text((x + side / 2, y + side / 2 - 4), initials, font=self.fonts.initials, fill=text, anchor="mm")

    # ---- title + artist ----

    def _sig_track(self, v: View) -> tuple:
        if not v.online:
            return ("off", v.connecting, v.target)
        if not v.has_track:
            return ("idle", v.busy, v.more_wishes, bool(v.qr_url))
        return ("track", v.title, v.artist, v.skipping, v.next_title, v.next_artist, v.next_who,
                v.outage, v.outage_reason, v.more_wishes, self._shows_qr(v),
                self._vote_badge(v), self._voted_out(v), v.now_who, v.next_vote)

    def _voted_out(self, v: View) -> str:
        """"vyřazená hlasováním" — a banned track plays only because somebody wished it."""
        if v.vote_status == "banned":
            return self.s["voted_out"]
        return self.s["artist_voted_out"] if v.artist_banned else ""

    @staticmethod
    def _vote_badge(v: View) -> tuple[str, str]:
        """(text, kind) after the artist: "♥ 3" for a favourite, "▲ 2  ▼ 1" otherwise, nothing without votes."""
        if v.vote_status == "banned" or v.artist_banned:
            return ("", "")
        if v.vote_status == "favourite" and v.vote_up:
            return (f"♥ {v.vote_up}", "fav")
        parts = ([f"▲ {v.vote_up}"] if v.vote_up else []) + ([f"▼ {v.vote_down}"] if v.vote_down else [])
        return ("  ".join(parts), "plain") if parts else ("", "")

    def _fit_title(self, title: str, max_w: float) -> tuple[list[str], ImageFont.FreeTypeFont]:
        """As big as fits: one line at 34 or 30 px, else two lines at 27 or 24 px."""
        fs = self.fonts
        for font in (fs.title_xl, fs.title_big):
            if font.getlength(title) <= max_w:
                return [title], font
        lines: list[str] = []
        for font in (fs.title, fs.title_sm):
            lines = wrap(title, font, max_w, 2)
            if not lines[-1].endswith("…") or title.endswith("…"):
                return lines, font
        return lines, fs.title_sm

    def _outage_line(self, v: View) -> str:
        reason = "network" if v.outage_reason == "dns" else v.outage_reason
        return self.s.get(f"outage_{reason}", self.s["outage_line"])

    LINE_Y = 114  # the bottom line of the text block ("Pak:", outage, QR caption), region-local centre

    def _draw_track(self, d: ImageDraw.ImageDraw, size: tuple[int, int], v: View) -> None:
        w, h = size
        x, max_w = 8, w - 20
        fs = self.fonts
        cy = self.LINE_Y
        if not v.online or not v.has_track:
            if not v.online:
                title = self.s["connecting_title"] if v.connecting else self.s["offline_title"]
                sub = self.s["waiting"].format(target=v.target)
                title_color, sub_color = DIM if v.connecting else TEXT, FAINT
            else:
                title, title_color = self.s["silence"], DIM
                # what the two big buttons do when nothing plays — "Hrát" starts
                # the DJ, which isn't obvious from a play icon
                sub = self.s["dj_picking"] if v.busy else self.s["idle_hint"]
                sub_color = ACCENT_TEXT if v.busy else DIM
            lines, font = self._fit_title(title, max_w)
            y = 2
            for line in lines:
                d.text((x, y), line, font=font, fill=title_color, anchor="la")
                y += font.size + 4
            y = max(y + 4, 46)
            for line in wrap(sub, fs.hint, max_w, 2 if len(lines) == 1 else 1):
                d.text((x, y), line, font=fs.hint, fill=sub_color, anchor="la")
                y += 23
            if v.online and v.more_wishes:
                self._more(d, w, cy, v.more_wishes, alone=True)
            elif v.online and v.qr_url:
                d.text((x, cy), ellipsize(self.s["qr_caption"], fs.status_b, max_w), font=fs.status_b,
                       fill=ACCENT_TEXT, anchor="lm")
            return

        title = v.title.strip() or self.s["unknown"]
        sub = v.artist.strip()
        title_color = FAINT if v.skipping else TEXT
        sub_color = FAINT if v.skipping else DIM
        lines, font = self._fit_title(title, max_w)
        y = 2 if font.size >= 30 else 3
        step = font.size + 4
        for line in lines:
            d.text((x, y), line, font=font, fill=title_color, anchor="la")
            y += step
        one = len(lines) == 1
        af = fs.artist if one else fs.artist_sm
        badge, kind = self._vote_badge(v)
        badge_w = 0
        if badge:
            # right on the artist line, big enough to read from the door
            bf = fs.artist_b if one else fs.artist_sm_b
            badge_w = int(bf.getlength(badge)) + 14
            by = (46 if one else y + 1) + af.size // 2 + 2
            d.text((w - 12, by), badge, font=bf, fill=ACCENT_TEXT if kind == "fav" else DIM, anchor="rm")
        if sub:
            y = 46 if one else y + 1
            d.text((x, y), ellipsize(sub, af, max_w - badge_w), font=af, fill=sub_color, anchor="la")
        f = fs.status
        if v.outage:
            lines = wrap(self._outage_line(v), f, max_w, 2 if one else 1)
            top = cy - 10 * (len(lines) - 1)
            for i, line in enumerate(lines):
                d.text((x, top + i * 20), line, font=f, fill=ERR, anchor="lm")
            return
        if self._shows_qr(v):
            d.text((x, cy), ellipsize(self.s["qr_caption"], fs.status_b, max_w), font=fs.status_b,
                   fill=ACCENT_TEXT, anchor="lm")
            return
        if v.skipping:
            return
        out = self._voted_out(v)
        if out:
            # the office voted it out — it plays only because somebody asked for it by name
            cw = self._red_chip(d, x, cy, out)
            who = v.now_who.strip()
            if who and max_w - cw > 90:
                lx = x + cw + 8
                by = self.s["wish_by"]
                if f.getlength(by) + 60 <= max_w - (lx - x):
                    d.text((lx, cy), by, font=f, fill=DIM, anchor="lm")
                    lx += f.getlength(by)
                name_chip(d, lx, cy, who, fs.chip, max_w=max_w - (lx - x) - 14)
            return
        right = w - 12
        if v.more_wishes:
            right -= self._more(d, w, cy, v.more_wishes) + 10
        if not v.next_title:
            return
        # what "Další" brings — and whose wish it is, with the same name tag as the queue
        label = self.s["next_up"]
        d.text((x, cy), label, font=f, fill=FAINT, anchor="lm")
        lx = x + f.getlength(label)
        if v.next_vote == "favourite":
            d.text((lx, cy), "♥ ", font=fs.status_b, fill=ACCENT_TEXT, anchor="lm")
            lx += fs.status_b.getlength("♥ ")
        elif v.next_vote == "banned":
            d.text((lx, cy), self.s["next_voted_out"], font=f, fill=ERR, anchor="lm")
            lx += f.getlength(self.s["next_voted_out"])
        who = v.next_who.strip()
        if who:
            lx += name_chip(d, lx, cy, who, fs.chip, max_w=min(100, (right - lx) / 3)) + 6
            nxt = v.next_title.strip()
        else:
            nxt = v.next_title.strip() + (f" · {v.next_artist.strip()}" if v.next_artist.strip() else "")
        if right - lx > 30:
            d.text((lx, cy), ellipsize(nxt, f, right - lx), font=f, fill=DIM, anchor="lm")

    def _red_chip(self, d: ImageDraw.ImageDraw, x: float, cy: float, text: str) -> float:
        f = self.fonts.chip
        tw = f.getlength(text)
        d.rounded_rectangle((x, cy - 12, x + tw + 16, cy + 12), radius=12, fill=(92, 30, 30))
        d.text((x + 8, cy), text, font=f, fill=(255, 196, 190), anchor="lm")
        return tw + 16

    def _more(self, d: ImageDraw.ImageDraw, w: int, cy: int, n: int, alone: bool = False) -> int:
        """"+2 přání ›" at the right of the "Pak:" line — tapping there opens the queue."""
        f = self.fonts.status_b
        text = self.s["waiting_wishes" if alone else "more"].format(n=n)
        tw = int(f.getlength(text))
        if alone:
            d.text((8, cy), text, font=f, fill=ACCENT_TEXT, anchor="lm")
        else:
            d.text((w - 12, cy), text, font=f, fill=ACCENT_TEXT, anchor="rm")
        return tw

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
        if not self._has_time(v):
            return  # nothing plays: no empty track across the screen
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


# ---- the full-screen QR page ("📱" in the status strip) ----


class QrRenderer:
    """"Přání z mobilu": a big code to the web, three steps, the address. Static —
    one full frame when it opens, nothing after that."""

    def __init__(self, fonts: Fonts, lang: str = "cs") -> None:
        self.frame = Image.new("RGB", (W, H), BG)
        self.fonts = fonts
        self.s = STRINGS.get(lang, STRINGS["cs"])
        self._sig: tuple | None = None

    def invalidate(self) -> None:
        self._sig = None

    def render(self, urls: tuple[str, ...], full: bool = False) -> list[Box]:
        sig = (urls,)
        if not full and sig == self._sig:
            return []
        self._sig = sig
        self.frame.paste(BG, (0, 0, W, H))
        d = ImageDraw.Draw(self.frame)
        fs = self.fonts
        side = 0
        if urls:
            try:
                m = qr_encode(urls[0], "M")
            except ValueError:
                m = None
            if m is not None:
                side = draw_qr(d, 12, 12, 250, m)
        x = side + 30 if side else 20
        room = W - x - 12
        tf = next((f for f in (fs.title, fs.title_sm, fs.artist) if f.getlength(self.s["qr_title"]) <= room), fs.hint)
        d.text((x, 16), self.s["qr_title"], font=tf, fill=TEXT, anchor="la")
        if not urls:
            for i, line in enumerate(wrap(self.s["qr_none"], fs.hint, room, 3)):
                d.text((x, 70 + i * 24), line, font=fs.hint, fill=DIM, anchor="la")
        else:
            y = 66
            for i, step in enumerate(self.s["qr_steps"], 1):
                d.ellipse((x, y, x + 24, y + 24), fill=ACCENT)
                d.text((x + 12, y + 12), str(i), font=fs.chip, fill=ON_ACCENT, anchor="mm")
                lines = wrap(step, fs.status, room - 34, 2)
                for j, line in enumerate(lines):
                    d.text((x + 34, y + 12 + j * 19), line, font=fs.status, fill=TEXT, anchor="lm")
                y += 26 + 19 * len(lines)
            url = urls[0].removeprefix("http://")
            d.text((x, max(y + 6, 226)), ellipsize(url, fs.status_b, room), font=fs.status_b, fill=ACCENT_TEXT,
                   anchor="la")
        d.text((W // 2, H - 16), self.s["qr_back"], font=fs.status, fill=FAINT, anchor="mm")
        return [(0, 0, W, H)]
