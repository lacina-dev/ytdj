"""The "now playing" screen for a TV: what it shows and how it is drawn.

One retained frame in the size of the framebuffer, cut into regions (header,
track, progress, what is next, the DJ's note, the web address). A region is
redrawn only when its signature changes, and only the pixels that really
differ are pushed — a progress bar growing by a few pixels is a few pixels.

Everything scales from one unit (1 at 1280×720), so 720×480, 720p and 1080p
get the same layout. Text is big enough to read across an office; long titles
step down in size, then wrap, then get an ellipsis. Dark by default. A TV left
on all day gets two kinds of care: the whole picture moves by a few pixels
every ten minutes, and when nothing has played for a while it dims.
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass, field
from typing import Callable

from PIL import Image, ImageChops, ImageDraw, ImageFont

from ..panel.hw import Box
from ..panel.qr import encode as qr_encode
from ..panel.ui import FONT_DIR, draw_qr, ellipsize, fmt_time, who_color, wrap

BG = (10, 11, 14)
PANEL = (22, 24, 29)
LINE = (44, 47, 54)
TEXT = (240, 238, 232)
MUTED = (160, 164, 172)
FAINT = (104, 108, 118)
ACCENT = (255, 122, 26)  # the jukebox's orange
OK = (86, 196, 120)
WARN = (240, 180, 60)
BAD = (232, 92, 80)

NEXT_ITEMS = 3
NOTE_MAX_AGE = 15 * 60  # s — the DJ's last reply stays on screen this long
PROGRESS_STEP = 5  # s — how often the progress bar and the elapsed time move
SHIFT_EVERY = 600  # s — burn-in care: the picture moves a little this often
SHIFTS = ((0, 0), (1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1))
DIM_AFTER = 5 * 60  # s without music before the screen dims
DIM = 0.45


@dataclass(frozen=True)
class NextItem:
    title: str
    artist: str = ""
    who: str = ""


@dataclass(frozen=True)
class TvView:
    """Everything the screen shows, as plain values (no clock, no network)."""

    state: str = "idle"  # playing | paused | loading | outage | starting | idle | offline
    vid: str = ""
    title: str = ""
    artist: str = ""
    position: int = 0  # s, already stepped to PROGRESS_STEP
    duration: int = 0
    who: str = ""  # whose wish plays now
    wish: str = ""  # what they asked for
    origin: str = ""  # where the background music comes from
    next: tuple[NextItem, ...] = ()
    more: int = 0  # queue items beyond the ones shown
    note: str = ""  # the DJ's short note
    note_who: str = ""
    detail: str = ""  # outage / offline reason
    clock: str = ""
    clip: bool = False  # the playing track is a clip and its picture is on its way
    address: str = ""  # what to type into a browser; "" = not known (nothing is shown)
    address_ip: str = ""  # the same place as a bare address (no name lookup needed): the QR code
    art: bool = False  # the cover is ready
    dim: bool = False
    shift: int = 0


def _s(value: object, limit: int = 300) -> str:
    return " ".join(str(value or "").split())[:limit]


def view_from(state: dict | None, *, offline: str = "", now: float | None = None,
              mono: float | None = None, got_at: float | None = None, address: str = "",
              address_ip: str = "",
              idle_since: float | None = None, art_ready: Callable[[str], bool] | None = None,
              clock: str | None = None) -> TvView:
    """The server's status (or the lack of one) as a TvView.

    `got_at`/`mono` are monotonic times: when this status arrived and now —
    while music plays, the position is carried forward between updates so the
    bar keeps moving without asking the server. `idle_since` = since when
    nothing has played (for dimming).
    """
    now = time.time() if now is None else now
    mono = time.monotonic() if mono is None else mono
    base = dict(address=address, address_ip=address_ip,
                clock=time.strftime("%H:%M", time.localtime(now)) if clock is None else clock,
                shift=int(now // SHIFT_EVERY) % len(SHIFTS),
                dim=idle_since is not None and mono - idle_since >= DIM_AFTER)
    if not isinstance(state, dict) or offline:
        return TvView(state="offline", detail=_s(offline, 80), **base)
    cur = state.get("current") if isinstance(state.get("current"), dict) else None
    queue = [q for q in state.get("queue") or [] if isinstance(q, dict)] \
        if isinstance(state.get("queue"), list) else []
    dj = state.get("dj") if isinstance(state.get("dj"), dict) else {}
    note = note_who = ""
    last = dj.get("last") if isinstance(dj.get("last"), dict) else None
    if dj.get("busy") and _s(dj.get("text")):
        note = f"DJ vybírá: „{_s(dj.get('text'), 120)}“"
    elif last and _s(last.get("reply")):
        try:
            fresh = now - float(last.get("at") or 0) <= NOTE_MAX_AGE
        except (TypeError, ValueError):
            fresh = False
        if fresh:
            note, note_who = _s(last.get("reply"), 240), _s(last.get("who"), 24)
    nxt = tuple(NextItem(_s(q.get("title"), 120) or "?", _s(q.get("artist"), 80),
                         _s((q.get("req") or {}).get("who") if isinstance(q.get("req"), dict)
                            else "", 24))
                for q in queue[:NEXT_ITEMS])
    common = dict(next=nxt, more=max(0, len(queue) - len(nxt)), note=note, note_who=note_who,
                  **base)
    if cur is None:
        starting = bool(state.get("starting"))
        return TvView(state="starting" if starting else "idle", **common)
    reason = cur.get("reason") if isinstance(cur.get("reason"), dict) else {}
    who = wish = origin = ""
    if reason.get("kind") == "wish":
        who, wish = _s(reason.get("who"), 24), _s(reason.get("text"), 120)
    elif _s(reason.get("from_who")):
        origin = f"rádio podle přání {_s(reason.get('from_who'), 24)}"
    elif reason.get("kind") == "start":
        origin = "rádio podle času a dne"
    elif _s(state.get("mood")):
        origin = f"rádio · {_s(state.get('mood'), 60)}"
    paused = bool(state.get("paused"))
    outage = state.get("outage") if isinstance(state.get("outage"), dict) else None
    if outage:
        kind, detail = "outage", _s(outage.get("reason") or outage.get("detail"), 80)
    elif paused:
        kind, detail = "paused", ""
    elif state.get("buffering"):
        kind, detail = "loading", ""
    else:
        kind, detail = "playing", ""
    try:
        pos = float(state.get("position") or 0.0)
        dur = float(state.get("duration") or cur.get("duration") or 0.0)
    except (TypeError, ValueError):
        pos = dur = 0.0
    if kind == "playing" and got_at is not None:
        pos += max(0.0, mono - got_at)
    if dur > 0:
        pos = min(pos, dur)
    vid = _s(cur.get("id"), 16)
    tv = state.get("tv") if isinstance(state.get("tv"), dict) else {}
    # klipy zapnuté a tahle skladba je klip: obraz se chystá (nebo právě nabíhá)
    clip = tv.get("on") is True and not tv.get("blocked") and kind != "outage" \
        and (tv.get("pending") is True or (bool(vid) and tv.get("video") == vid))
    return TvView(state=kind, vid=vid, clip=clip, title=_s(cur.get("title"), 200) or "?",
                  artist=_s(cur.get("artist"), 120), duration=int(dur),
                  position=int(max(0.0, pos)) // PROGRESS_STEP * PROGRESS_STEP,
                  who=who, wish=wish, origin=origin, detail=detail,
                  art=bool(art_ready and vid and art_ready(vid)), **common)


def _more(n: int) -> str:
    return "další" if n == 1 else "další" if 2 <= n <= 4 else "dalších"


def cover_tile(data: bytes, side: int) -> Image.Image:
    """A YouTube thumbnail → the square cover, scaled to side×side.

    The 4:3 thumbnails (hqdefault, 480×360) carry the 16:9 picture between two
    black bands; the cover of a song is the centre square of that picture.
    """
    im = Image.open(io.BytesIO(data))
    im = im.convert("RGB")
    w, h = im.size
    if w * 3 == h * 4:  # 4:3 → the 16:9 band in the middle
        band = w * 9 // 16
        im = im.crop((0, (h - band) // 2, w, (h - band) // 2 + band))
        w, h = im.size
    s = min(w, h)
    box = ((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s)
    return im.resize((side, side), Image.LANCZOS, box=box)


STATE_LABEL = {
    "playing": ("HRAJE", OK), "paused": ("PAUZA", WARN), "loading": ("NAČÍTÁ SE", WARN),
    "outage": ("VÝPADEK", BAD), "starting": ("DJ VYBÍRÁ", WARN), "idle": ("TICHO", FAINT),
    "offline": ("NEODPOVÍDÁ", BAD),
}


@dataclass
class _Region:
    name: str
    box: Box
    sig: Callable[[TvView], tuple]
    draw: Callable[[ImageDraw.ImageDraw, Image.Image, TvView], None]
    last: tuple | None = field(default=None)


class Renderer:
    """Keeps the frame; `render(view)` returns the boxes that changed."""

    def __init__(self, size: tuple[int, int],
                 art: Callable[[str], Image.Image | None] | None = None) -> None:
        self.size = size
        w, h = size
        self.u = u = min(w / 1280, h / 720)
        self.frame = Image.new("RGB", size, BG)
        self.art = art
        self._fonts: dict[tuple[bool, int], ImageFont.FreeTypeFont] = {}
        self._qr: tuple[str, list[list[bool]] | None] = ("", None)
        self._shift = -1
        self._dim: bool | None = None
        # margins: TVs cut the edges off (overscan), and the picture shifts a little
        self.mx, self.my = round(w * 0.05), round(h * 0.048)
        self.step = max(2, round(4 * u))  # one burn-in step in pixels
        self.head_h = round(58 * u)
        self.foot_h = round(184 * u)
        self.bar_h = round(54 * u)
        self.note_h = round(64 * u)
        self.art_side = self._art_side()
        self.regions: list[_Region] = []
        self._layout(0, 0)

    # ---- geometry ----

    def _art_side(self) -> int:
        w, h = self.size
        main_h = h - 2 * self.my - self.head_h - self.bar_h - self.note_h - self.foot_h \
            - round(40 * self.u)
        return max(64, min(main_h, round(w * 0.3)))

    def _layout(self, dx: int, dy: int) -> None:
        w, h = self.size
        u = self.u
        x0, x1 = self.mx + dx, w - self.mx + dx
        y = self.my + dy
        gap = round(14 * u)
        head = (x0, y, x1, y + self.head_h)
        foot_y = h - self.my + dy - self.foot_h
        note = (x0, foot_y - gap - self.note_h, x1, foot_y - gap)
        bar = (x0, note[1] - self.bar_h, x1, note[1])
        main = (x0, head[3] + gap, x1, bar[1] - gap)
        qr_w = round(460 * u)
        nxt = (x0, foot_y, x1 - qr_w - gap, foot_y + self.foot_h)
        web = (x1 - qr_w, foot_y, x1, foot_y + self.foot_h)
        old = {r.name: r.last for r in self.regions}
        self.regions = [
            _Region("head", head, lambda v: (v.state, v.clock, v.clip), self._draw_head),
            _Region("main", main, lambda v: (v.state, v.vid, v.title, v.artist, v.who, v.wish,
                                             v.origin, v.art, v.detail), self._draw_main),
            _Region("bar", bar, lambda v: (v.state, v.position, v.duration, bool(v.vid)),
                    self._draw_bar),
            _Region("note", note, lambda v: (v.note, v.note_who), self._draw_note),
            _Region("next", nxt, lambda v: (v.next, v.more, v.state == "offline"),
                    self._draw_next),
            _Region("web", web, lambda v: (v.address, v.address_ip), self._draw_web),
        ]
        for r in self.regions:
            r.last = old.get(r.name)

    def font(self, px: float, bold: bool = False) -> ImageFont.FreeTypeFont:
        size = max(9, round(px * self.u))
        key = (bold, size)
        f = self._fonts.get(key)
        if f is None:
            name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
            try:
                f = ImageFont.truetype(str(FONT_DIR / name), size)
            except OSError:
                try:
                    f = ImageFont.truetype(name, size)
                except OSError:
                    f = ImageFont.load_default()  # type: ignore[assignment]
            self._fonts[key] = f
        return f

    # ---- rendering ----

    def render(self, v: TvView, full: bool = False) -> list[Box]:
        """Redraws what changed; returns the boxes to push to the screen."""
        if v.shift != self._shift or v.dim != self._dim:
            # the picture moved (burn-in) or dimmed: everything is redrawn
            dx, dy = SHIFTS[v.shift % len(SHIFTS)]
            self._layout(dx * self.step, dy * self.step)
            self._shift, self._dim = v.shift, v.dim
            full = True
        if full:
            self.frame.paste(BG, (0, 0, *self.size))
            for r in self.regions:
                r.last = None
        boxes: list[Box] = []
        for r in self.regions:
            sig = r.sig(v)
            if sig == r.last:
                continue
            r.last = sig
            x0, y0, x1, y1 = r.box
            tile = Image.new("RGB", (x1 - x0, y1 - y0), BG)
            r.draw(ImageDraw.Draw(tile), tile, v)
            if v.dim:
                tile = tile.point(lambda c: int(c * DIM))
            if full:
                self.frame.paste(tile, (x0, y0))
                continue
            diff = ImageChops.difference(tile, self.frame.crop(r.box)).getbbox()
            if diff is None:
                continue
            self.frame.paste(tile, (x0, y0))
            boxes.append((x0 + diff[0], y0 + diff[1], x0 + diff[2], y0 + diff[3]))
        return [(0, 0, *self.size)] if full else boxes

    # ---- regions (each draws into its own tile, origin 0,0) ----

    def _draw_head(self, d: ImageDraw.ImageDraw, tile: Image.Image, v: TvView) -> None:
        w, h = tile.size
        u = self.u
        cy = h // 2
        d.text((0, cy), "JUKEBOX", font=self.font(26, True), fill=ACCENT, anchor="lm")
        x = d.textlength("JUKEBOX", font=self.font(26, True)) + round(26 * u)
        label, color = STATE_LABEL.get(v.state, ("", FAINT))
        r = round(9 * u)
        d.ellipse((x, cy - r, x + 2 * r, cy + r), fill=color)
        d.text((x + 2 * r + round(12 * u), cy), label, font=self.font(26, True), fill=color,
               anchor="lm")
        if v.clip:
            # honest while waiting: the picture of this track is being prepared
            x += 2 * r + round(12 * u) + d.textlength(label, font=self.font(26, True))
            d.text((x + round(22 * u), cy), "·  klip se načítá…", font=self.font(24), fill=MUTED,
                   anchor="lm")
        d.text((w, cy), v.clock, font=self.font(40, True), fill=MUTED, anchor="rm")
        d.line((0, h - 1, w, h - 1), fill=LINE, width=max(1, round(2 * u)))

    def _fit_title(self, text: str, max_w: float, max_h: float
                   ) -> tuple[list[str], ImageFont.FreeTypeFont]:
        """As big as fits: one line, then two, then three smaller with an ellipsis."""
        for px, lines in ((76, 1), (66, 1), (56, 1), (56, 2), (48, 2), (42, 2), (42, 3), (36, 3)):
            f = self.font(px, True)
            if lines * round(f.size * 1.18) > max_h:
                continue
            out = wrap(text, f, max_w, lines)
            if len(out) <= lines and "".join(out).replace(" ", "") == text.replace(" ", ""):
                return out, f
        f = self.font(36, True)
        fit = max(1, min(3, int(max_h // round(f.size * 1.18))))
        return wrap(text, f, max_w, fit), f

    def _draw_main(self, d: ImageDraw.ImageDraw, tile: Image.Image, v: TvView) -> None:
        w, h = tile.size
        u = self.u
        if v.state in ("idle", "starting", "offline"):
            big, small = {
                "idle": ("Nic nehraje", "Napiš DJovi, co chceš slyšet — adresa je vpravo dole."),
                "starting": ("DJ vybírá hudbu…", "Podle času, dne a toho, co se tu hrálo."),
                "offline": ("Jukebox neodpovídá",
                            "Zkouším to znovu" + (f" ({v.detail})." if v.detail else ".")),
            }[v.state]
            d.text((0, h * 0.40), big, font=self.font(84, True),
                   fill=TEXT if v.state != "offline" else BAD, anchor="lm")
            for i, line in enumerate(wrap(small.strip(), self.font(36), w, 2)):
                d.text((0, h * 0.40 + round((84 + i * 46) * u)), line, font=self.font(36),
                       fill=MUTED, anchor="lm")
            return
        side = min(self.art_side, h)
        ay = (h - side) // 2
        cover = self.art(v.vid) if self.art and v.vid else None
        if cover is not None:
            if cover.size != (side, side):
                cover = cover.resize((side, side), Image.LANCZOS)
            tile.paste(cover, (0, ay))
        else:
            fill, text = who_color(v.artist or v.title)
            d.rectangle((0, ay, side - 1, ay + side - 1), fill=fill)
            initials = "".join(p[0] for p in (v.artist or v.title).split()[:2]).upper() or "♪"
            d.text((side / 2, ay + side / 2), initials, font=self.font(side / self.u * 0.36, True),
                   fill=text, anchor="mm")
        if v.state != "playing":
            # paused / loading / outage: the cover steps back
            shade = Image.new("RGB", (side, side), BG)
            tile.paste(Image.blend(tile.crop((0, ay, side, ay + side)), shade, 0.55), (0, ay))
            if v.state == "paused":
                # two bars over the cover: readable from the other end of the office
                bw, bh, cx, cy = side * 0.11, side * 0.36, side / 2, ay + side / 2
                for x in (cx - bw * 1.4, cx + bw * 0.4):
                    d.rounded_rectangle((x, cy - bh / 2, x + bw, cy + bh / 2), radius=bw * 0.2,
                                        fill=WARN)
        tx = side + round(44 * u)
        tw = w - tx
        fa, fo = self.font(42), self.font(30)
        has_from = bool(v.who or v.origin or v.state == "outage")
        rest = round(10 * u) + fa.size + (round(26 * u) + fo.size + round(14 * u) if has_from else 0)
        lines, ft = self._fit_title(v.title, tw, h - rest)
        lh = round(ft.size * 1.18)
        total = len(lines) * lh + rest
        y = max(0, (h - total) // 2)
        color = TEXT if v.state == "playing" else MUTED
        for line in lines:
            d.text((tx, y), line, font=ft, fill=color, anchor="la")
            y += lh
        y += round(10 * u)
        d.text((tx, y), ellipsize(v.artist, fa, tw), font=fa, fill=MUTED, anchor="la")
        y += fa.size + round(26 * u)
        if v.state == "outage":
            d.text((tx, y + round(7 * u)), ellipsize(
                f"Výpadek{': ' + v.detail if v.detail else ''} — fronta čeká", fo, tw),
                font=fo, fill=BAD, anchor="la")
        elif v.who:
            cy = y + fo.size // 2 + round(7 * u)
            x = tx + self._chip(d, tx, cy, v.who, self.font(28, True))
            if v.wish:
                rest = tw - (x - tx) - round(16 * u)
                d.text((x + round(16 * u), cy), ellipsize(f"„{v.wish}“", fo, rest), font=fo,
                       fill=MUTED, anchor="lm")
        elif v.origin:
            d.text((tx, y + round(7 * u)), ellipsize(v.origin, fo, tw), font=fo, fill=FAINT,
                   anchor="la")

    def _chip(self, d: ImageDraw.ImageDraw, x: float, cy: float, name: str,
              font: ImageFont.FreeTypeFont, max_w: float | None = None) -> float:
        """The coloured name tag of whoever wished it (same colours as the panel)."""
        fill, text = who_color(name)
        pad = round(font.size * 0.5)
        label = ellipsize(name, font, max_w or round(300 * self.u))
        tw = d.textlength(label, font=font)
        half = round(font.size * 0.78)
        d.rounded_rectangle((x, cy - half, x + tw + 2 * pad, cy + half), radius=half, fill=fill)
        d.text((x + pad, cy), label, font=font, fill=text, anchor="lm")
        return tw + 2 * pad

    def _draw_bar(self, d: ImageDraw.ImageDraw, tile: Image.Image, v: TvView) -> None:
        if not v.vid:
            return
        w, h = tile.size
        u = self.u
        f = self.font(30)
        tw = round(d.textlength("0:00:00", font=f)) if v.duration >= 3600 else \
            round(d.textlength("00:00", font=f))
        gap = round(20 * u)
        cy = h // 2
        d.text((tw, cy), fmt_time(v.position), font=f, fill=MUTED, anchor="rm")
        if v.duration:
            d.text((w, cy), fmt_time(v.duration), font=f, fill=FAINT, anchor="rm")
        x0, x1 = tw + gap, w - tw - gap
        th = max(4, round(10 * u))
        d.rounded_rectangle((x0, cy - th // 2, x1, cy + th // 2), radius=th // 2, fill=PANEL)
        if v.duration:
            px = round((x1 - x0) * min(1.0, v.position / v.duration))
            if px >= th:
                d.rounded_rectangle((x0, cy - th // 2, x0 + px, cy + th // 2), radius=th // 2,
                                    fill=ACCENT if v.state == "playing" else FAINT)

    def _draw_note(self, d: ImageDraw.ImageDraw, tile: Image.Image, v: TvView) -> None:
        if not v.note:
            return
        w, h = tile.size
        f = self.font(26)
        x = 0
        d.text((0, h // 2), "DJ", font=self.font(26, True), fill=ACCENT, anchor="lm")
        x += round(d.textlength("DJ", font=self.font(26, True)) + 18 * self.u)
        lines = wrap(v.note, f, w - x, 2)
        lh = round(f.size * 1.2)
        y = h // 2 - (len(lines) - 1) * lh // 2
        for line in lines:
            d.text((x, y), line, font=f, fill=MUTED, anchor="lm")
            y += lh

    def _draw_next(self, d: ImageDraw.ImageDraw, tile: Image.Image, v: TvView) -> None:
        w, h = tile.size
        u = self.u
        d.line((0, 0, w, 0), fill=LINE, width=max(1, round(2 * u)))
        if v.state == "offline":
            return  # what was queued a while ago says nothing now
        fl = self.font(22, True)
        d.text((0, round(30 * u)), "DÁL HRAJE", font=fl, fill=FAINT, anchor="lm")
        if v.more:
            d.text((d.textlength("DÁL HRAJE", font=fl) + round(18 * u), round(30 * u)),
                   f"a {v.more} {_more(v.more)}", font=self.font(22), fill=FAINT, anchor="lm")
        if not v.next:
            d.text((0, round(76 * u)), "Fronta je prázdná.", font=self.font(30), fill=FAINT,
                   anchor="lm")
            return
        ft, fa, fc = self.font(30, True), self.font(30), self.font(22, True)
        row = round(44 * u)
        y = round(76 * u)
        for i, it in enumerate(v.next):
            x = 0
            d.text((x, y), f"{i + 1}", font=fa, fill=FAINT, anchor="lm")
            x += round(36 * u)
            room = w - x - round(12 * u)
            if it.who:
                chip_w = d.textlength(ellipsize(it.who, fc, round(200 * u)), font=fc) + fc.size
                room -= chip_w + round(16 * u)
            title = ellipsize(it.title, ft, room * (0.62 if it.artist else 1.0))
            d.text((x, y), title, font=ft, fill=TEXT, anchor="lm")
            x += d.textlength(title, font=ft)
            if it.artist:
                artist = ellipsize(f"  ·  {it.artist}", fa, max(0.0, room - d.textlength(title, font=ft)))
                d.text((x, y), artist, font=fa, fill=MUTED, anchor="lm")
                x += d.textlength(artist, font=fa)
            if it.who:
                self._chip(d, x + round(16 * u), y, it.who, fc, round(200 * u))
            y += row

    def _draw_web(self, d: ImageDraw.ImageDraw, tile: Image.Image, v: TvView) -> None:
        w, h = tile.size
        u = self.u
        d.line((0, 0, w, 0), fill=LINE, width=max(1, round(2 * u)))
        if not v.address:
            # no address is confirmed to work yet — better none than a wrong one
            d.text((w, h * 0.45), "Adresu webu zjišťuji…", font=self.font(24), fill=FAINT,
                   anchor="rm")
            return
        pad = round(16 * u)
        side = h - 2 * pad
        # The QR code carries the bare address when there is one: it needs no
        # name lookup, so it works on every phone and on a network that loses
        # multicast (.local). The name is for typing — with the address under it.
        target = v.address_ip or v.address
        if self._qr[0] != target:
            try:
                self._qr = (target, qr_encode(f"http://{target}"))
            except ValueError:
                self._qr = (target, None)
        used = 0
        if self._qr[1] is not None:
            used = draw_qr(d, w - side, pad, side, self._qr[1])
        tw = w - (used or 0) - round(20 * u) if used else w
        second = v.address_ip if v.address_ip and v.address_ip != v.address else ""
        f = self.font(24, True)
        addr = v.address
        split = d.textlength(addr, font=f) > tw and ":" in addr
        if second and not split:
            d.text((tw, h * 0.24), "Pusť si svoje", font=self.font(26, True), fill=TEXT, anchor="rm")
            d.text((tw, h * 0.50), ellipsize(addr, f, tw), font=f, fill=ACCENT, anchor="rm")
            f2 = self.font(22)
            d.text((tw, h * 0.74), ellipsize(f"nebo {second}", f2, tw), font=f2, fill=MUTED,
                   anchor="rm")
            return
        d.text((tw, h * 0.34), "Pusť si svoje", font=self.font(26, True), fill=TEXT, anchor="rm")
        if split:
            # too long for one line: the port goes under the name
            host, port = addr.rsplit(":", 1)
            d.text((tw, h * 0.56), ellipsize(host, f, tw), font=f, fill=ACCENT, anchor="rm")
            d.text((tw, h * 0.74), f":{port}", font=f, fill=ACCENT, anchor="rm")
        else:
            d.text((tw, h * 0.58), addr, font=f, fill=ACCENT, anchor="rm")
