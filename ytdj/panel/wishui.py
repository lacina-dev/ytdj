"""Drawing the wish screens: quick picks → keyboard → the DJ's answer.

The same machinery as the network screens (`netui.NetRenderer`: regions with
signatures, only changed pixels go to the glass) and the same keyboard, with
one more page for Czech letters with háčky and čárky. Fonts are borrowed from
the network renderer, so the extra screen costs one retained frame and no
second copy of the typefaces.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PIL import ImageDraw

from .hw import Box
from .netui import (
    BACK,
    BTN_FULL,
    BTN_L,
    BTN_R,
    KB_AREA,
    STRINGS as NET_STRINGS,
    NetRenderer,
    icon_check,
    icon_cross,
    kb_keys,
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

STRINGS = {
    "cs": {
        "title": "Přání pro DJe",
        "type": "Napsat vlastní přání…",
        "draft": "Pokračovat: ",
        "placeholder": "např. písničky od Olympicu",
        "cancel": "Zpět",
        "send": "Poslat",
        "empty": "napiš, co chceš slyšet",
        "too_long": "nejvýš {n} znaků",
        "busy_other": "DJ vyřizuje jiné přání",
        "yours": "Tvoje přání",
        "picking": "DJ vybírá hudbu…",
        "typical": "{s} s · obvykle 20–30 s",
        "waiting": "Čekám, až DJ dořeší jiné přání…",
        "waiting_sub": "pošlu ho hned, jak bude volno · {s} s",
        "dj_says": "DJ:",
        "failed": "Přání se nepovedlo poslat",
        "leave": "Zpět k přehrávání",
        "again": "Další přání",
        "done": "Hotovo",
        "retry": "Zkusit znovu",
        "err_busy": "DJ má pořád práci s jiným přáním. Zkus to za chvíli.",
        "err_offline": "ytdj teď neodpovídá (možná se restartuje).",
        "err_server": "DJ narazil na chybu: {e}",
    },
    "en": {
        "title": "Wish for the DJ",
        "type": "Type your own wish…",
        "draft": "Continue: ",
        "placeholder": "e.g. songs by Olympic",
        "cancel": "Back",
        "send": "Send",
        "empty": "type what you want to hear",
        "too_long": "{n} characters at most",
        "busy_other": "the DJ is on another wish",
        "yours": "Your wish",
        "picking": "The DJ is picking music…",
        "typical": "{s} s · usually 20–30 s",
        "waiting": "Waiting for the DJ to finish another wish…",
        "waiting_sub": "it goes out as soon as the DJ is free · {s} s",
        "dj_says": "DJ:",
        "failed": "The wish did not get through",
        "leave": "Back to the player",
        "again": "Another wish",
        "done": "Done",
        "retry": "Try again",
        "err_busy": "The DJ is still busy with another wish. Try again in a moment.",
        "err_offline": "ytdj is not answering (restarting, maybe).",
        "err_server": "The DJ ran into an error: {e}",
    },
}

# Quick picks: (label on the button, what goes to the DJ). The texts are the
# phrasings the DJ's intent reader knows (PLAN C2), so one tap is as good as
# typing them.
CHIPS = {
    "cs": (
        ("Víc takového", "víc takového"),
        ("Něco jiného", "něco jiného"),
        ("Jen česky", "jen česky"),
        ("Klidnější", "něco klidnějšího"),
        ("Živější", "něco živějšího"),
        ("Překvap mě", "překvap mě"),
    ),
    "en": (
        ("More like this", "more like this"),
        ("Something else", "something different"),
        ("Czech only", "only Czech songs"),
        ("Calmer", "something calmer"),
        ("Livelier", "something livelier"),
        ("Surprise me", "surprise me"),
    ),
}

# ---- layout ----

TITLE = (BACK[2], 0, W, 44)
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


@dataclass(frozen=True)
class WishView:
    page: str = "home"  # "home" | "keys" | "sent"
    pressed: str | None = None
    note: str = ""  # header toast (volume from the speaker's wheel)
    busy_other: bool = False  # the DJ is working on somebody else's wish
    # keyboard (field names shared with NetView: the keyboard drawing reads them)
    text: str = ""
    kb_page: str = "abc"
    shift: int = 0
    hint: str = ""
    can_connect: bool = False  # the "Poslat" key is live
    # the answer
    phase: str = ""  # "busy" | "wait" | "ok" | "error"
    wish: str = ""
    elapsed: int = 0
    reply: str = ""
    error: str = ""


def targets(v: WishView, lang: str = "cs") -> dict[str, Box]:
    if v.page == "home":
        t = {"back": BACK, "field": FIELD_BTN}
        for i in range(len(CHIPS.get(lang, CHIPS["cs"]))):
            t[f"chip{i}"] = chip_box(i)
        return t
    if v.page == "keys":
        return dict(kb_keys(v.kb_page, lang, accents=True))
    if v.page == "sent":
        if v.phase in ("busy", "wait"):
            return {"leave": BTN_FULL}
        if v.phase == "ok":
            return {"again": BTN_L, "done": BTN_R}
        if v.phase == "error":
            return {"done": BTN_L, "retry": BTN_R}
    return {}


Region = tuple[str, Box, Callable[[WishView], tuple], Callable, bool]


class WishRenderer(NetRenderer):
    def __init__(self, lang: str = "cs", share: NetRenderer | None = None) -> None:
        super().__init__(lang, share=share)
        base = NET_STRINGS[self.lang]
        self.s = {**base, **STRINGS[self.lang]}
        self.ok_label = self.s["send"]
        self.chips = CHIPS[self.lang]

    def _regions(self, v) -> list[Region]:  # type: ignore[override]
        if v.page == "home":
            regs: list[Region] = [
                ("back", BACK, lambda v: (v.pressed == "back",), self._draw_back, False),
                ("wtitle", TITLE, lambda v: (v.note, v.busy_other), self._draw_home_title, False),
                ("field", FIELD_BTN, lambda v: (v.text, v.pressed == "field"), self._draw_field_btn, False),
            ]
            for i in range(len(self.chips)):
                regs.append((f"chip{i}", chip_box(i), self._chip_sig(i), self._chip_draw(i), False))
            return regs
        if v.page == "keys":
            regs = [
                ("ktitle", K_TITLE, lambda v: (v.hint, v.note, v.busy_other), self._draw_keys_title, False),
                ("kfield", K_FIELD, lambda v: (v.text,), self._draw_text_field, False),
                ("kbbg", KB_AREA, lambda v: (v.kb_page,), lambda d, s, v: None, True),
            ]
            for kid, box in kb_keys(v.kb_page, self.lang, accents=True):
                regs.append((f"k{v.kb_page}:{kid}", box, self._key_sig(kid), self._key_draw(kid), False))
            return regs
        if v.page == "sent":
            return [
                ("stitle", S_TITLE, lambda v: (v.note,), lambda d, s, v: self._header(d, s, self.s["title"], v.note), False),
                ("swish", S_WISH, lambda v: (v.wish,), self._draw_wish_text, False),
                ("smain", S_MAIN, lambda v: (v.phase, v.elapsed, v.reply, v.error), self._draw_main, False),
                ("sbtns", S_BTNS, lambda v: (v.phase, v.pressed), self._draw_sent_btns, False),
            ]
        return []

    # ---- home: quick picks ----

    def _draw_home_title(self, d, size, v: WishView) -> None:
        note = v.note or (self.s["busy_other"] if v.busy_other else "")
        self._header(d, size, self.s["title"], note)

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

    # ---- keyboard ----

    def _draw_keys_title(self, d, size, v: WishView) -> None:
        w, h = size
        right, color = "", ACCENT_TEXT
        if v.hint:
            right, color = v.hint, ERR
        elif v.note:
            right = v.note
        elif v.busy_other:
            right = self.s["busy_other"]
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
        d.text((14, 6), self.s["yours"], font=self.small, fill=FAINT, anchor="la")
        y = 26
        for line in wrap(f"„{v.wish.strip()}“", self.value, w - 28, 2):
            d.text((14, y), line, font=self.value, fill=TEXT, anchor="la")
            y += 22
        d.line((14, size[1] - 1, w - 14, size[1] - 1), fill=LINE)

    def _draw_main(self, d, size, v: WishView) -> None:
        w, h = size
        x = 14
        if v.phase in ("busy", "wait"):
            waiting = v.phase == "wait"
            title = self.s["waiting"] if waiting else self.s["picking"]
            d.text((x, 14), ellipsize(title, self.ssid, w - 28), font=self.ssid, fill=ACCENT_TEXT, anchor="la")
            # a bar that creeps toward the usual length of a turn: it moves, so
            # it's alive, and it never claims to be done before the answer is
            bx0, bx1, by = x, w - x, 64
            d.rounded_rectangle((bx0, by, bx1, by + 8), radius=4, fill=SURFACE_HI)
            if not waiting:
                frac = min(0.95, v.elapsed / 25.0)
                if frac > 0:
                    d.rounded_rectangle((bx0, by, bx0 + max(8, (bx1 - bx0) * frac), by + 8), radius=4, fill=ACCENT)
            sub = (self.s["waiting_sub"] if waiting else self.s["typical"]).format(s=v.elapsed)
            d.text((x, 88), ellipsize(sub, self.label, w - 28), font=self.label, fill=DIM, anchor="la")
        elif v.phase == "ok":
            d.ellipse((x, 12, x + 26, 38), fill=OK)
            icon_check(d, x + 13, 25, 16, (14, 16, 19))
            d.text((x + 36, 25), self.s["dj_says"], font=self.value, fill=DIM, anchor="lm")
            y = 46
            lines = wrap(v.reply.strip() or self.s["done"], self.f.artist, w - 28, 4)
            for line in lines:
                d.text((x, y), line, font=self.f.artist, fill=TEXT, anchor="la")
                y += 24
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
                d.rounded_rectangle(b, radius=14, fill=SURFACE)
                color = ACCENT_TEXT if pressed else TEXT
            d.text(((b[0] + b[2]) / 2, (b[1] + b[3]) / 2), label, font=self.f.button, fill=color, anchor="mm")

        if v.phase in ("busy", "wait"):
            btn(BTN_FULL, "leave", self.s["leave"], False)
        elif v.phase == "ok":
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

