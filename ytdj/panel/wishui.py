"""Drawing the wish screens: quick picks → keyboard → the DJ's answer, and the queue.

The same machinery as the network screens (`netui.NetRenderer`: regions with
signatures, only changed pixels go to the glass) and the same keyboard, with
one more page for Czech letters with háčky and čárky. Fonts are borrowed from
the network renderer, so the extra screen costs one retained frame and no
second copy of the typefaces.

Pages: "home" (quick picks, who is wishing, the queue button), "keys", "sent"
(how the wish is doing: the DJ picking → in the queue "za ~2 skladby" → playing,
with "Hned po téhle"), "queue" (everybody's wishes, four rows at a time,
× only on the panel's own).
"""

from __future__ import annotations

import colorsys
from dataclasses import dataclass
from typing import Callable

from PIL import ImageDraw

from .hw import Box
from .netui import (
    BACK,
    BTN_FULL,
    BTN_L,
    BTN_R,
    DOWN,
    KB_AREA,
    ROWS,
    STRINGS as NET_STRINGS,
    UP,
    NetRenderer,
    icon_check,
    icon_cross,
    kb_keys,
    list_row,
)
from .ui import (
    ACCENT,
    ACCENT_TEXT,
    DIM,
    ERR,
    FAINT,
    LINE,
    ON_ACCENT,
    PRESSED_ON_ACCENT,
    SURFACE,
    SURFACE_HI,
    TEXT,
    W,
    ellipsize,
    wrap,
)

OK = (120, 190, 130)
WARN = (217, 140, 95)

STRINGS = {
    "cs": {
        "title": "Přání pro DJe",
        "title_short": "Přání",
        "who": "Kdo: {who}",
        "queue_btn": "Fronta",
        "queue_title": "Fronta přání",
        "queue_empty": "Nikdo si zrovna nic nepřeje.",
        "type": "Napsat vlastní přání…",
        "offline": "DJ teď rozumí jen „pusť <interpret>“ a tlačítkům",
        "draft": "Pokračovat: ",
        "placeholder": "např. písničky od Olympicu",
        "cancel": "Zpět",
        "send": "Poslat",
        "empty": "napiš, co chceš slyšet",
        "too_long": "nejvýš {n} znaků",
        "yours": "Tvoje přání",
        "picking": "DJ vybírá hudbu…",
        "typical": "{s} s · obvykle 3–25 s · jiná přání nečekají",
        "queued": "Ve frontě",
        "queued_eta": "Ve frontě · {eta}",
        "playing": "Hraje!",
        "notfound": "DJ to nenašel",
        "dj_says": "DJ:",
        "failed": "Přání se nepovedlo poslat",
        "leave": "Zpět k přehrávání",
        "again": "Další přání",
        "done": "Hotovo",
        "retry": "Zkusit znovu",
        "next_btn": "Hned po téhle",
        "removed": "Přání bylo odebráno.",
        "err_offline": "ytdj teď neodpovídá (možná se restartuje).",
        "err_server": "DJ narazil na chybu: {e}",
        "err_busy": "DJ má pořád práci s jiným přáním. Zkus to za chvíli.",
    },
    "en": {
        "title": "Wish for the DJ",
        "title_short": "Wishes",
        "who": "Who: {who}",
        "queue_btn": "Queue",
        "queue_title": "Wish queue",
        "queue_empty": "Nobody is wishing for anything.",
        "type": "Type your own wish…",
        "offline": "The DJ only gets “play <artist>”, a song title and the buttons now",
        "draft": "Continue: ",
        "placeholder": "e.g. songs by Olympic",
        "cancel": "Back",
        "send": "Send",
        "empty": "type what you want to hear",
        "too_long": "{n} characters at most",
        "yours": "Your wish",
        "picking": "The DJ is picking music…",
        "typical": "{s} s · usually 3–25 s · other wishes don't wait",
        "queued": "Queued",
        "queued_eta": "Queued · {eta}",
        "playing": "Playing!",
        "notfound": "The DJ didn't find it",
        "dj_says": "DJ:",
        "failed": "The wish did not get through",
        "leave": "Back to the player",
        "again": "Another wish",
        "done": "Done",
        "retry": "Try again",
        "next_btn": "Right after this",
        "removed": "The wish was removed.",
        "err_offline": "ytdj is not answering (restarting, maybe).",
        "err_server": "The DJ ran into an error: {e}",
        "err_busy": "The DJ is still busy with another wish. Try again in a moment.",
    },
}

# Quick picks: (label on the button, what goes to the DJ). The texts are the
# phrasings the DJ's intent reader knows (PLAN C2), so one tap is as good as
# typing them — and unambiguous: "Něco jiného" / "Překvap mě" leave the
# current artist and mood (25. 9. "překvap mě" stayed with Kabát).
CHIPS = {
    "cs": (
        ("Víc takového", "víc takového"),
        ("Něco jiného", "něco úplně jiného než teď — jiné interprety i styl"),
        ("Jen česky", "jen česky"),
        ("Klidnější", "něco klidnějšího"),
        ("Živější", "něco živějšího"),
        ("Překvap mě", "překvap mě něčím úplně jiným"),
    ),
    "en": (
        ("More like this", "more like this"),
        ("Something else", "something completely different from now — other artists and style"),
        ("Czech only", "only Czech songs"),
        ("Calmer", "something calmer"),
        ("Livelier", "something livelier"),
        ("Surprise me", "surprise me with something completely different"),
    ),
}

# ---- layout ----

TITLE = (BACK[2], 0, W, 44)
H_TITLE = (BACK[2], 0, 206, 44)
WHO_BTN = (206, 0, 346, 44)
QUEUE_BTN = (346, 0, W, 44)
FIELD_BTN = (8, 52, 472, 104)
CHIP_TOP, CHIP_H, CHIP_GAP = 112, 62, 6


def chip_box(i: int) -> Box:
    col, row = i % 2, i // 2
    x0 = 8 if col == 0 else 244
    y0 = CHIP_TOP + row * (CHIP_H + CHIP_GAP)
    return (x0, y0, x0 + 228, y0 + CHIP_H)


# keyboard
K_TITLE = (0, 0, W, 32)
K_FIELD = (6, 34, W - 6, 80)

# the answer
S_TITLE = (0, 0, W, 44)
S_WISH = (0, 46, W, 108)
S_MAIN = (0, 108, W, 254)
S_BTNS = (0, 256, W, 320)

# the queue: four rows of 60 px (the network list's geometry), arrows on the right
Q_TITLE = (BACK[2], 0, W, 44)
RM_W = 56


def rm_box(i: int) -> Box:
    l, t, r, b = list_row(i)
    return (r - RM_W, t, r, b)


@dataclass(frozen=True)
class QueueRow:
    id: str
    who: str
    text: str
    state: str
    label: str  # "ve frontě · za ~2 skladby"
    mine: bool  # sent from this panel — can be removed here


@dataclass(frozen=True)
class WishView:
    page: str = "home"  # "home" | "keys" | "sent" | "queue"
    pressed: str | None = None
    note: str = ""  # header toast (volume from the speaker's wheel)
    busy_other: bool = False  # kept for older callers; the queue never refuses a wish
    # keyboard (field names shared with NetView: the keyboard drawing reads them)
    text: str = ""
    kb_page: str = "abc"
    shift: int = 0
    hint: str = ""
    can_connect: bool = False  # the "Poslat" key is live
    # the answer
    phase: str = ""  # "busy" | "queued" | "playing" | "ok" | "notfound" | "error"
    wish: str = ""
    elapsed: int = 0
    reply: str = ""
    error: str = ""
    eta: str = ""  # "za ~2 skladby" / "hned po téhle"
    can_next: bool = False  # "Hned po téhle" offered
    # who is wishing and the queue
    who: str = "displej"
    count: int = 0  # wishes waiting or playing
    rows: tuple[QueueRow, ...] = ()
    scroll: int = 0
    offline: bool = False  # mozek DJe nejede (jistič) — rozumí jen jednoduchým přáním


def targets(v: WishView, lang: str = "cs") -> dict[str, Box]:
    if v.page == "home":
        t = {"back": BACK, "field": FIELD_BTN, "who": WHO_BTN, "queue": QUEUE_BTN}
        for i in range(len(CHIPS.get(lang, CHIPS["cs"]))):
            t[f"chip{i}"] = chip_box(i)
        return t
    if v.page == "keys":
        return dict(kb_keys(v.kb_page, lang, accents=True))
    if v.page == "queue":
        t = {"back": BACK, "up": UP, "down": DOWN}
        for i in range(ROWS):
            k = v.scroll + i
            if k < len(v.rows) and v.rows[k].mine and v.rows[k].state in ACTIVE:
                t[f"rm{i}"] = rm_box(i)
        return t
    if v.page == "sent":
        if v.phase == "busy":
            return {"leave": BTN_FULL}
        if v.phase == "queued" and v.can_next:
            return {"next": BTN_L, "done": BTN_R}
        if v.phase in ("queued", "playing", "ok", "notfound"):
            return {"again": BTN_L, "done": BTN_R}
        if v.phase == "error":
            return {"done": BTN_L, "retry": BTN_R}
    return {}


ACTIVE = ("waiting", "thinking", "queued", "playing")
Region = tuple[str, Box, Callable[[WishView], tuple], Callable, bool]


def who_color(name: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """A name → (fill, text) colours; the same person looks the same everywhere."""
    h = 0
    for ch in name or "?":
        h = (h * 31 + ord(ch)) % 360
    fill = colorsys.hls_to_rgb(h / 360, 0.26, 0.45)
    text = colorsys.hls_to_rgb(h / 360, 0.78, 0.6)
    return (tuple(int(c * 255) for c in fill), tuple(int(c * 255) for c in text))  # type: ignore[return-value]


class WishRenderer(NetRenderer):
    def __init__(self, lang: str = "cs", share: NetRenderer | None = None) -> None:
        super().__init__(lang, share=share)
        base = NET_STRINGS[self.lang]
        self.s = {**base, **STRINGS[self.lang]}
        self.ok_label = self.s["send"]
        self.chips = CHIPS[self.lang]

    def _regions(self, v) -> list[Region]:  # type: ignore[override]
        back = ("back", BACK, lambda v: (v.pressed == "back",), self._draw_back, False)
        if v.page == "home":
            regs: list[Region] = [
                back,
                ("wtitle", H_TITLE, lambda v: (v.note,), self._draw_home_title, False),
                ("who", WHO_BTN, lambda v: (v.who, v.pressed == "who"), self._draw_who_btn, False),
                ("qbtn", QUEUE_BTN, lambda v: (v.count, v.pressed == "queue"), self._draw_queue_btn, False),
                ("field", FIELD_BTN, lambda v: (v.text, v.pressed == "field", v.offline),
                 self._draw_field_btn, False),
            ]
            for i in range(len(self.chips)):
                regs.append((f"chip{i}", chip_box(i), self._chip_sig(i), self._chip_draw(i), False))
            return regs
        if v.page == "keys":
            regs = [
                ("ktitle", K_TITLE, lambda v: (v.hint, v.note), self._draw_keys_title, False),
                ("kfield", K_FIELD, lambda v: (v.text,), self._draw_text_field, False),
                ("kbbg", KB_AREA, lambda v: (v.kb_page,), lambda d, s, v: None, True),
            ]
            for kid, box in kb_keys(v.kb_page, self.lang, accents=True):
                regs.append((f"k{v.kb_page}:{kid}", box, self._key_sig(kid), self._key_draw(kid), False))
            return regs
        if v.page == "queue":
            regs = [
                back,
                ("qtitle", Q_TITLE, lambda v: (v.note, v.count, v.scroll, len(v.rows)), self._draw_queue_title, False),
            ]
            for i in range(ROWS):
                regs.append((f"qrow{i}", list_row(i), self._qrow_sig(i), self._qrow_draw(i), False))
            regs.append(("up", UP, lambda v: (v.scroll > 0, v.pressed == "up"),
                         lambda d, s, v: self._arrow(d, s, v.scroll > 0, v.pressed == "up", True), False))
            regs.append(("down", DOWN, lambda v: (v.scroll + ROWS < len(v.rows), v.pressed == "down"),
                         lambda d, s, v: self._arrow(d, s, v.scroll + ROWS < len(v.rows), v.pressed == "down",
                                                     False), False))
            return regs
        if v.page == "sent":
            return [
                ("stitle", S_TITLE, lambda v: (v.note,), lambda d, s, v: self._header(d, s, self.s["title"], v.note), False),
                ("swish", S_WISH, lambda v: (v.wish, v.who), self._draw_wish_text, False),
                ("smain", S_MAIN, lambda v: (v.phase, v.elapsed, v.reply, v.error, v.eta), self._draw_main, False),
                ("sbtns", S_BTNS, lambda v: (v.phase, v.pressed, v.can_next), self._draw_sent_btns, False),
            ]
        return []

    # ---- home: who, the queue, quick picks ----

    def _draw_home_title(self, d, size, v: WishView) -> None:
        w, h = size
        d.line((0, h - 1, w, h - 1), fill=LINE)
        if v.note:
            d.text((8, (h - 2) / 2), ellipsize(v.note, self.label, w - 12), font=self.label, fill=ACCENT_TEXT, anchor="lm")
        else:
            d.text((8, (h - 2) / 2), self.s["title_short"], font=self.head, fill=TEXT, anchor="lm")

    def _header_btn(self, d, size, label: str, pressed: bool, badge: str = "") -> None:
        w, h = size
        d.line((0, h - 1, w, h - 1), fill=LINE)
        d.rounded_rectangle((4, 5, w - 6, h - 7), radius=10, fill=SURFACE_HI if pressed else SURFACE)
        color = ACCENT_TEXT if pressed else TEXT
        f = self.value
        bw = (self.small.getlength(badge) + 14) if badge else 0
        room = w - 22 - bw
        text = ellipsize(label, f, room)
        tw = f.getlength(text) + (bw + 6 if badge else 0)
        x = (w - 2 - tw) / 2
        d.text((x, (h - 2) / 2), text, font=f, fill=color, anchor="lm")
        if badge:
            bx = x + f.getlength(text) + 6
            d.rounded_rectangle((bx, 12, bx + bw, h - 14), radius=8, fill=ACCENT)
            d.text((bx + bw / 2, (h - 2) / 2), badge, font=self.small, fill=ON_ACCENT, anchor="mm")

    def _draw_who_btn(self, d, size, v: WishView) -> None:
        self._header_btn(d, size, self.s["who"].format(who=v.who), v.pressed == "who")

    def _draw_queue_btn(self, d, size, v: WishView) -> None:
        self._header_btn(d, size, self.s["queue_btn"], v.pressed == "queue", str(v.count) if v.count else "")

    def _draw_field_btn(self, d, size, v: WishView) -> None:
        w, h = size
        pressed = v.pressed == "field"
        d.rounded_rectangle((0, 0, w - 1, h - 1), radius=12, fill=SURFACE_HI if pressed else SURFACE,
                            outline=ACCENT if pressed else SURFACE_HI, width=2)
        cy = h / 2
        icon_keyboard(d, 30, cy, 30, ACCENT_TEXT)
        x, room = 58, w - 58 - 14
        if v.text.strip():
            label = self.s["draft"]
            d.text((x, cy), label, font=self.label, fill=DIM, anchor="lm")
            lx = x + self.label.getlength(label)
            d.text((lx, cy), ellipsize(v.text.strip(), self.value, room - (lx - x)), font=self.value, fill=TEXT, anchor="lm")
        elif v.offline:
            # mozek DJe nejede — ať je jasné, co teď funguje
            d.text((x, cy), ellipsize(self.s["offline"], self.label, room), font=self.label,
                   fill=ERR, anchor="lm")
        else:
            d.text((x, cy), self.s["type"], font=self.value, fill=TEXT if not pressed else ACCENT_TEXT, anchor="lm")

    def _chip_sig(self, i: int) -> Callable[[WishView], tuple]:
        return lambda v: (v.pressed == f"chip{i}",)

    def _chip_draw(self, i: int):
        label = self.chips[i][0]

        def draw(d, size, v: WishView) -> None:
            pressed = v.pressed == f"chip{i}"
            self._button(d, size, SURFACE_HI if pressed else SURFACE, 14)
            d.text((size[0] / 2, size[1] / 2), label, font=self.f.button,
                   fill=ACCENT_TEXT if pressed else TEXT, anchor="mm")
        return draw

    # ---- the queue ----

    def _draw_queue_title(self, d, size, v: WishView) -> None:
        live = sum(1 for r in v.rows if r.state in ACTIVE)
        right = v.note or (f"{v.scroll + 1}–{min(len(v.rows), v.scroll + ROWS)} / {len(v.rows)}"
                           if len(v.rows) > ROWS else (str(live) if live else ""))
        self._header(d, size, self.s["queue_title"], right)

    def _qrow_sig(self, i: int) -> Callable[[WishView], tuple]:
        def sig(v: WishView) -> tuple:
            k = v.scroll + i
            row = v.rows[k] if k < len(v.rows) else None
            return (row, i == 0 and not v.rows, v.pressed == f"rm{i}")
        return sig

    def _chip(self, d, x: float, y: float, name: str) -> float:
        """The coloured name tag; returns its width."""
        fill, text = who_color(name)
        f = self.key_small
        label = ellipsize(name, f, 110)
        tw = f.getlength(label)
        d.rounded_rectangle((x, y - 11, x + tw + 14, y + 11), radius=10, fill=fill)
        d.text((x + 7, y), label, font=f, fill=text, anchor="lm")
        return tw + 14

    def _qrow_draw(self, i: int):
        def draw(d, size, v: WishView) -> None:
            w, h = size
            k = v.scroll + i
            if k >= len(v.rows):
                if i == 0 and not v.rows:
                    d.text((14, h // 2), self.s["queue_empty"], font=self.label, fill=DIM, anchor="lm")
                return
            row = v.rows[k]
            live = row.state in ACTIVE
            removable = row.mine and live
            self._button(d, size, SURFACE if live else (22, 24, 27), 12)
            room = w - 16 - (RM_W + 4 if removable else 0)
            x = 10
            x += self._chip(d, x, 19, row.who) + 8
            d.text((x, 19), ellipsize(row.text, self.value, room - x), font=self.value,
                   fill=TEXT if live else DIM, anchor="lm")
            color = {"playing": OK, "thinking": ACCENT_TEXT, "waiting": ACCENT_TEXT,
                     "notfound": ERR, "error": ERR}.get(row.state, DIM)
            d.text((10, 44), ellipsize(row.label, self.label, room - 10), font=self.label, fill=color, anchor="lm")
            if removable:
                pressed = v.pressed == f"rm{i}"
                bx = w - RM_W
                d.rounded_rectangle((bx + 4, 6, w - 6, h - 7), radius=10, fill=SURFACE_HI if pressed else (48, 34, 34))
                icon_cross(d, bx + (RM_W - 2) / 2, h / 2, 20, ACCENT_TEXT if pressed else ERR)
        return draw

    # ---- keyboard ----

    def _draw_keys_title(self, d, size, v: WishView) -> None:
        w, h = size
        right, color = "", ACCENT_TEXT
        if v.hint:
            right, color = v.hint, ERR
        elif v.note:
            right = v.note
        room = w - 20
        if right:
            right = ellipsize(right, self.label, w * 0.55)
            d.text((w - 10, h / 2), right, font=self.label, fill=color, anchor="rm")
            room -= self.label.getlength(right) + 14
        d.text((10, h / 2), ellipsize(self.s["title"], self.value, room), font=self.value, fill=DIM, anchor="lm")

    def _draw_text_field(self, d, size, v: WishView) -> None:
        w, h = size
        d.rounded_rectangle((0, 0, w - 1, h - 1), radius=10, fill=SURFACE, outline=SURFACE_HI)
        cy = h / 2
        room = w - 28
        font = self.key
        if not v.text:
            d.rectangle((12, cy - 13, 14, cy + 13), fill=ACCENT)
            d.text((22, cy), self.s["placeholder"], font=self.label, fill=FAINT, anchor="lm")
            return
        text = v.text
        # the end is what you're typing — keep it in view
        while text and font.getlength("…" + text) > room:
            text = text[1:]
        if text != v.text:
            text = "…" + text
        d.text((12, cy + 1), text, font=font, fill=TEXT, anchor="lm")
        end = 12 + font.getlength(text)
        d.rectangle((end + 2, cy - 13, end + 4, cy + 13), fill=ACCENT)

    # ---- the answer ----

    def _draw_wish_text(self, d, size, v: WishView) -> None:
        w, _ = size
        label = self.s["yours"]
        d.text((14, 6), label, font=self.small, fill=FAINT, anchor="la")
        if v.who:
            self._chip(d, 22 + self.small.getlength(label), 13, v.who)
        y = 26
        for line in wrap(f"„{v.wish.strip()}“", self.value, w - 28, 2):
            d.text((14, y), line, font=self.value, fill=TEXT, anchor="la")
            y += 22
        d.line((14, size[1] - 1, w - 14, size[1] - 1), fill=LINE)

    def _reply_lines(self, d, x: int, y: int, w: int, text: str, lines: int = 4) -> None:
        for line in wrap(text.strip(), self.f.artist, w - 28, lines):
            d.text((x, y), line, font=self.f.artist, fill=TEXT, anchor="la")
            y += 24

    def _draw_main(self, d, size, v: WishView) -> None:
        w, h = size
        x = 14
        if v.phase == "busy":
            d.text((x, 14), ellipsize(self.s["picking"], self.ssid, w - 28), font=self.ssid, fill=ACCENT_TEXT, anchor="la")
            # a bar that creeps toward the usual length of a turn: it moves, so
            # it's alive, and it never claims to be done before the answer is
            bx0, bx1, by = x, w - x, 64
            d.rounded_rectangle((bx0, by, bx1, by + 8), radius=4, fill=SURFACE_HI)
            frac = min(0.95, v.elapsed / 25.0)
            if frac > 0:
                d.rounded_rectangle((bx0, by, bx0 + max(8, (bx1 - bx0) * frac), by + 8), radius=4, fill=ACCENT)
            sub = self.s["typical"].format(s=v.elapsed)
            d.text((x, 88), ellipsize(sub, self.label, w - 28), font=self.label, fill=DIM, anchor="la")
        elif v.phase in ("queued", "playing", "ok"):
            d.ellipse((x, 12, x + 26, 38), fill=OK)
            icon_check(d, x + 13, 25, 16, (14, 16, 19))
            if v.phase == "queued":
                head = self.s["queued_eta"].format(eta=v.eta) if v.eta else self.s["queued"]
            elif v.phase == "playing":
                head = self.s["playing"]
            else:
                head = self.s["dj_says"]
            d.text((x + 36, 25), ellipsize(head, self.value, w - x - 50), font=self.value,
                   fill=OK if v.phase == "playing" else TEXT, anchor="lm")
            self._reply_lines(d, x, 46, w, v.reply or self.s["done"])
        elif v.phase == "notfound":
            d.ellipse((x, 12, x + 26, 38), fill=WARN)
            icon_cross(d, x + 13, 25, 16, (14, 16, 19))
            d.text((x + 36, 25), self.s["notfound"], font=self.value, fill=TEXT, anchor="lm")
            self._reply_lines(d, x, 46, w, v.reply)
        elif v.phase == "error":
            d.ellipse((x, 12, x + 26, 38), fill=ERR)
            icon_cross(d, x + 13, 25, 16, (14, 16, 19))
            d.text((x + 36, 25), self.s["failed"], font=self.value, fill=TEXT, anchor="lm")
            y = 50
            for line in wrap(v.error, self.value, w - 28, 3):
                d.text((x, y), line, font=self.value, fill=ERR, anchor="la")
                y += 24

    def _draw_sent_btns(self, d, size, v: WishView) -> None:
        ox, oy = S_BTNS[0], S_BTNS[1]

        def btn(box: Box, name: str, label: str, primary: bool) -> None:
            b = (box[0] - ox, box[1] - oy, box[2] - ox - 1, box[3] - oy - 1)
            pressed = v.pressed == name
            if primary:
                d.rounded_rectangle(b, radius=14, fill=ACCENT)
                color = PRESSED_ON_ACCENT if pressed else ON_ACCENT
            else:
                d.rounded_rectangle(b, radius=14, fill=SURFACE_HI if pressed else SURFACE)
                color = ACCENT_TEXT if pressed else TEXT
            d.text(((b[0] + b[2]) / 2, (b[1] + b[3]) / 2), label, font=self.f.button, fill=color, anchor="mm")

        if v.phase == "busy":
            btn(BTN_FULL, "leave", self.s["leave"], False)
        elif v.phase == "queued" and v.can_next:
            btn(BTN_L, "next", self.s["next_btn"], False)
            btn(BTN_R, "done", self.s["done"], True)
        elif v.phase in ("queued", "playing", "ok", "notfound"):
            btn(BTN_L, "again", self.s["again"], False)
            btn(BTN_R, "done", self.s["done"], True)
        elif v.phase == "error":
            btn(BTN_L, "done", self.s["cancel"], False)
            btn(BTN_R, "retry", self.s["retry"], True)


def icon_keyboard(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float, fill) -> None:
    """A little keyboard: a frame, two rows of keys and a space bar."""
    w, h = s, s * 0.66
    x0, y0 = cx - w / 2, cy - h / 2
    d.rounded_rectangle((x0, y0, x0 + w, y0 + h), radius=4, outline=fill, width=2)
    k = w / 7
    for row in range(2):
        for i in range(5):
            kx = x0 + k * (1 + i)
            ky = y0 + h * (0.22 + row * 0.24)
            d.rectangle((kx + 1, ky, kx + k - 2, ky + h * 0.14), fill=fill)
    d.rectangle((x0 + k * 2, y0 + h * 0.72, x0 + k * 5, y0 + h * 0.84), fill=fill)
