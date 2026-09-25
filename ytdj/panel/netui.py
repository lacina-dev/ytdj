"""Drawing the network screens: overview, Wi-Fi list, keyboard, connecting.

Same idea as `ui.Renderer` — fixed regions with signatures, only changed
pixels go to the glass — with one addition: a page may change its own
geometry (the keyboard's letters vs. symbols), so a region can be a
background that, when redrawn, forces everything drawn after it to be drawn
again, and the dirty rectangles are found by comparing the finished frame
with the one before, not tile by tile.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from PIL import Image, ImageChops, ImageDraw

from .hw import Box
from .net import Link, WifiNet
from .qr import encode as qr_encode
from .ui import (
    ACCENT,
    ACCENT_TEXT,
    BG,
    DIM,
    ERR,
    FAINT,
    LINE,
    ON_ACCENT,
    PRESSED_ON_ACCENT,
    SURFACE,
    SURFACE_HI,
    TEXT,
    H,
    W,
    Fonts,
    ellipsize,
    icon_eth,
    icon_wifi,
    merge_boxes,
    wrap,
)

OK = (120, 190, 130)
QR_LIGHT = (236, 233, 228)
QR_DARK = (14, 16, 19)

STRINGS = {
    "cs": {
        "back": "Zpět",
        "net_title": "Síť",
        "host": "Název",
        "eth": "Ethernet",
        "wifi": "Wi-Fi",
        "not_connected": "nepřipojeno",
        "connecting": "připojuji…",
        "no_cable": "kabel nezapojen",
        "wifi_off": "vypnuto",
        "loading": "zjišťuji…",
        "open_in": "Otevři v prohlížeči:",
        "offline_hint": "Pi není v žádné síti. Zapoj kabel, nebo vyber Wi-Fi.",
        "scan_qr": "naskenuj telefonem",
        "wifi_btn": "Wi-Fi sítě",
        "list_title": "Wi-Fi sítě",
        "rescan": "Hledat",
        "scanning": "hledám…",
        "searching": "Hledám sítě…",
        "no_networks": "Žádné sítě v dosahu.",
        "secured": "zabezpečená",
        "open": "otevřená, bez hesla",
        "in_use": "připojeno",
        "page": "{a}–{b} z {n}",
        "pw_for": "Heslo pro {ssid}",
        "pw_short": "aspoň 8 znaků",
        "space": "mezera",
        "cancel": "Zrušit",
        "connect": "Připojit",
        "conn_title": "Wi-Fi · {ssid}",
        "conn_busy": "Připojuji…",
        "conn_wait": "může to trvat až minutu",
        "conn_ok": "Připojeno",
        "conn_ip": "adresa {ip}",
        "conn_web": "web: {url}",
        "conn_fail": "Nepodařilo se připojit",
        "done": "Hotovo",
        "retry": "Zkusit znovu",
        "volume": "hlasitost {v}",
        "err_wrong_password": "Špatné heslo.",
        "err_bad_password": "Heslo pro WPA musí mít 8 až 63 znaků.",
        "err_not_found": "Síť není v dosahu.",
        "err_timeout": "Vypršel čas — síť neodpověděla.",
        "err_denied": "Chybí oprávnění k NetworkManageru.",
        "err_no_nm": "NetworkManager (nmcli) není k dispozici.",
        "err_unknown": "Neznámá chyba.",
    },
    "en": {
        "back": "Back",
        "net_title": "Network",
        "host": "Name",
        "eth": "Ethernet",
        "wifi": "Wi-Fi",
        "not_connected": "not connected",
        "connecting": "connecting…",
        "no_cable": "no cable",
        "wifi_off": "off",
        "loading": "checking…",
        "open_in": "Open in a browser:",
        "offline_hint": "The Pi is not on any network. Plug in a cable or pick a Wi-Fi.",
        "scan_qr": "scan with a phone",
        "wifi_btn": "Wi-Fi networks",
        "list_title": "Wi-Fi networks",
        "rescan": "Scan",
        "scanning": "scanning…",
        "searching": "Looking for networks…",
        "no_networks": "No networks in range.",
        "secured": "secured",
        "open": "open, no password",
        "in_use": "connected",
        "page": "{a}–{b} of {n}",
        "pw_for": "Password for {ssid}",
        "pw_short": "at least 8 characters",
        "space": "space",
        "cancel": "Cancel",
        "connect": "Connect",
        "conn_title": "Wi-Fi · {ssid}",
        "conn_busy": "Connecting…",
        "conn_wait": "this can take up to a minute",
        "conn_ok": "Connected",
        "conn_ip": "address {ip}",
        "conn_web": "web: {url}",
        "conn_fail": "Could not connect",
        "done": "Done",
        "retry": "Try again",
        "volume": "volume {v}",
        "err_wrong_password": "Wrong password.",
        "err_bad_password": "A WPA password has 8 to 63 characters.",
        "err_not_found": "The network is out of range.",
        "err_timeout": "Timed out — the network did not answer.",
        "err_denied": "Not allowed to use NetworkManager.",
        "err_no_nm": "NetworkManager (nmcli) is not available.",
        "err_unknown": "Unknown error.",
    },
}

# ---- layout ----

BACK = (0, 0, 118, 44)
TITLE = (118, 0, W, 44)

# overview
ROW_HOST = (0, 48, 324, 76)
ROW_ETH = (0, 76, 324, 104)
ROW_WIFI = (0, 104, 324, 156)
URLS = (0, 158, 324, 254)
QR = (324, 48, W, 254)
WIFI_BTN = (8, 262, 472, 314)

# list
LIST_TITLE = (118, 0, W - 124, 44)
RESCAN = (W - 124, 0, W, 44)
ROWS = 4
ROW_H, ROW_PITCH, ROW_Y = 60, 66, 52
LIST_X = (8, 400)
UP = (408, 52, 472, 178)
DOWN = (408, 186, 472, 312)


def list_row(i: int) -> Box:
    y = ROW_Y + i * ROW_PITCH
    return (LIST_X[0], y, LIST_X[1], y + ROW_H)


# keyboard
KB_TITLE = (0, 0, W, 32)
FIELD = (6, 34, 414, 80)
EYE = (420, 34, 474, 80)
KB_AREA = (0, 82, W, H)
KB_X0, KB_UNIT = 6, 46.8
KB_Y0, KB_PITCH, KB_KEY_H = 86, 58, 54

LETTERS = {
    "cs": ("qwertzuiop", "asdfghjkl", "yxcvbnm"),
    "en": ("qwertyuiop", "asdfghjkl", "zxcvbnm"),
}
SYMBOLS = (
    ("1234567890", "@#$%&*-+()", ".,!?:;/"),
    ("1234567890", "_=[]{}<>\\|", "\"'~^`"),
)

# connecting
C_TITLE = (0, 0, W, 44)
C_BODY = (0, 46, W, 226)
C_ELAPSED = (0, 226, W, 254)
C_BTNS = (0, 256, W, H)
BTN_FULL = (8, 258, 472, 314)
BTN_L = (8, 258, 236, 314)
BTN_R = (244, 258, 472, 314)


def kb_keys(page: str, lang: str = "cs") -> list[tuple[str, Box]]:
    """(key id, box) for one keyboard page: "abc", "123" or "#+=".

    Ids: "c:<char>" types the character, then "shift" (letters) or "sym"
    (the other symbol page), "bksp", "mode", "space", "cancel", "ok".
    """
    def box(row: int, start: float, width: float) -> Box:
        y = KB_Y0 + row * KB_PITCH
        return (
            round(KB_X0 + start * KB_UNIT + 2), y,
            round(KB_X0 + (start + width) * KB_UNIT - 2), y + KB_KEY_H,
        )

    if page == "abc":
        r1, r2, r3 = LETTERS.get(lang, LETTERS["cs"])
    else:
        r1, r2, r3 = SYMBOLS[0 if page == "123" else 1]
    keys: list[tuple[str, Box]] = []
    for i, ch in enumerate(r1):
        keys.append((f"c:{ch}", box(0, i, 1)))
    off = (10 - len(r2)) / 2
    for i, ch in enumerate(r2):
        keys.append((f"c:{ch}", box(1, off + i, 1)))
    keys.append(("shift" if page == "abc" else "sym", box(2, 0, 1.5)))
    w3 = 7 / len(r3)
    for i, ch in enumerate(r3):
        keys.append((f"c:{ch}", box(2, 1.5 + i * w3, w3)))
    keys.append(("bksp", box(2, 8.5, 1.5)))
    keys.append(("cancel", box(3, 0, 2)))
    keys.append(("mode", box(3, 2, 1.5)))
    keys.append(("space", box(3, 3.5, 3.5)))
    keys.append(("ok", box(3, 7, 3)))
    return keys


@dataclass(frozen=True)
class NetView:
    page: str = "overview"  # "overview" | "list" | "keys" | "connect"
    pressed: str | None = None
    note: str = ""  # header toast (volume from the speaker's wheel)
    # overview
    loaded: bool = False
    hostname: str = ""
    mdns: bool = False
    eth: Link | None = None
    wifi: Link | None = None
    error: str = ""  # translated
    urls: tuple[str, ...] = ()
    # list
    nets: tuple[WifiNet, ...] = ()
    scroll: int = 0
    scanning: bool = False
    scanned: bool = False
    scan_error: str = ""
    # keyboard
    ssid: str = ""
    password: str = field(default="", repr=False)
    show_pw: bool = False
    kb_page: str = "abc"
    shift: int = 0  # 0 off, 1 next letter, 2 caps lock
    hint: str = ""
    can_connect: bool = False
    # connecting
    phase: str = ""  # "busy" | "ok" | "error"
    elapsed: int = 0
    result_ip: str = ""
    result_url: str = ""
    result_error: str = ""  # translated


def targets(v: NetView, lang: str = "cs") -> dict[str, Box]:
    """What can be touched on the current page."""
    if v.page == "overview":
        return {"back": BACK, "wifi_list": WIFI_BTN}
    if v.page == "list":
        t = {"back": BACK, "rescan": RESCAN, "up": UP, "down": DOWN}
        for i in range(ROWS):
            if v.scroll + i < len(v.nets):
                t[f"row{i}"] = list_row(i)
        return t
    if v.page == "keys":
        t = {"eye": EYE}
        t.update(dict(kb_keys(v.kb_page, lang)))
        return t
    if v.page == "connect":
        if v.phase == "ok":
            return {"done": BTN_FULL}
        if v.phase == "error":
            return {"list": BTN_L, "retry": BTN_R}
    return {}


# ---- icons ----


def icon_back(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float, fill) -> None:
    d.line([(cx + s * 0.25, cy - s * 0.5), (cx - s * 0.25, cy), (cx + s * 0.25, cy + s * 0.5)], fill=fill, width=4, joint="curve")


def icon_chevron(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float, fill, up: bool) -> None:
    k = -1 if up else 1
    d.line([(cx - s * 0.5, cy - k * s * 0.25), (cx, cy + k * s * 0.25), (cx + s * 0.5, cy - k * s * 0.25)], fill=fill, width=5, joint="curve")


def icon_bars(d: ImageDraw.ImageDraw, x0: float, y1: float, s: float, signal: int, on, off) -> None:
    lit = 0 if signal <= 0 else 1 + min(3, signal // 25)
    bw = s / 4 - 2
    for i in range(4):
        h = s * (0.3 + 0.7 * (i + 1) / 4)
        x = x0 + i * (bw + 2)
        d.rectangle((x, y1 - h, x + bw, y1), fill=on if i < lit else off)


def icon_lock(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float, fill) -> None:
    bw, bh = s * 0.7, s * 0.5
    d.arc((cx - bw * 0.33, cy - s * 0.5, cx + bw * 0.33, cy + s * 0.05), 180, 360, fill=fill, width=3)
    d.line((cx - bw * 0.33 + 1, cy - s * 0.22, cx - bw * 0.33 + 1, cy), fill=fill, width=3)
    d.line((cx + bw * 0.33 - 1, cy - s * 0.22, cx + bw * 0.33 - 1, cy), fill=fill, width=3)
    d.rounded_rectangle((cx - bw / 2, cy - s * 0.05, cx + bw / 2, cy - s * 0.05 + bh), radius=2, fill=fill)


def icon_shift(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float, fill, solid: bool) -> None:
    pts = [
        (cx, cy - s * 0.5), (cx + s * 0.45, cy), (cx + s * 0.2, cy), (cx + s * 0.2, cy + s * 0.45),
        (cx - s * 0.2, cy + s * 0.45), (cx - s * 0.2, cy), (cx - s * 0.45, cy),
    ]
    if solid:
        d.polygon(pts, fill=fill)
    else:
        d.polygon(pts, outline=fill, width=2)


def icon_bksp(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float, fill) -> None:
    w, h = s * 1.1, s * 0.7
    x0 = cx - w / 2
    pts = [(x0, cy), (x0 + h / 2, cy - h / 2), (x0 + w, cy - h / 2), (x0 + w, cy + h / 2), (x0 + h / 2, cy + h / 2)]
    d.polygon(pts, outline=fill, width=2)
    mx, r = x0 + h / 2 + (w - h / 2) / 2, h * 0.2
    d.line((mx - r, cy - r, mx + r, cy + r), fill=fill, width=2)
    d.line((mx - r, cy + r, mx + r, cy - r), fill=fill, width=2)


def icon_eye(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float, fill, crossed: bool) -> None:
    w, h = s, s * 0.58
    d.ellipse((cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2), outline=fill, width=2)
    r = s * 0.17
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=fill)
    if crossed:
        a, b = (cx - w * 0.42, cy + s * 0.42), (cx + w * 0.42, cy - s * 0.42)
        d.line((a, b), fill=SURFACE, width=7)
        d.line((a, b), fill=fill, width=2)


def icon_check(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float, fill) -> None:
    d.line([(cx - s * 0.4, cy), (cx - s * 0.1, cy + s * 0.3), (cx + s * 0.45, cy - s * 0.35)], fill=fill, width=6, joint="curve")


def icon_cross(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float, fill) -> None:
    r = s * 0.35
    d.line((cx - r, cy - r, cx + r, cy + r), fill=fill, width=6)
    d.line((cx - r, cy + r, cx + r, cy - r), fill=fill, width=6)


# ---- renderer ----

Region = tuple[str, Box, Callable[[NetView], tuple], Callable[[ImageDraw.ImageDraw, tuple[int, int], NetView], None], bool]


class NetRenderer:
    def __init__(self, lang: str = "cs") -> None:
        self.lang = lang if lang in STRINGS else "cs"
        self.s = STRINGS[self.lang]
        self.frame = Image.new("RGB", (W, H), BG)
        f = Fonts()
        self.f = f
        self.small = f._load("DejaVuSans.ttf", 14)
        self.label = f._load("DejaVuSans.ttf", 16)
        self.value = f._load("DejaVuSans-Bold.ttf", 17)
        self.url = f._load("DejaVuSans-Bold.ttf", 16)
        self.head = f._load("DejaVuSans-Bold.ttf", 20)
        self.ssid = f._load("DejaVuSans-Bold.ttf", 19)
        self.key = f._load("DejaVuSans.ttf", 23)
        self.key_small = f._load("DejaVuSans-Bold.ttf", 15)
        self.field = f._load("DejaVuSansMono.ttf", 22)
        self.big = f._load("DejaVuSans-Bold.ttf", 26)
        self._sigs: dict[str, tuple] = {}
        self._qr_cache: tuple[str, list[list[bool]] | None] = ("", None)

    def invalidate(self) -> None:
        self._sigs.clear()

    # ---- the diffing loop ----

    def _regions(self, v: NetView) -> list[Region]:
        back = ("back", BACK, lambda v: (v.pressed == "back",), self._draw_back, False)
        if v.page == "overview":
            return [
                back,
                ("title", TITLE, lambda v: (self.s["net_title"], v.note), self._draw_title, False),
                ("host", ROW_HOST, lambda v: (v.loaded, v.hostname, v.mdns, v.error), self._draw_host, False),
                ("eth", ROW_ETH, lambda v: (v.loaded, v.eth, v.error), self._draw_eth, False),
                ("wifi", ROW_WIFI, lambda v: (v.loaded, v.wifi, v.error), self._draw_wifi, False),
                ("urls", URLS, lambda v: (v.loaded, v.urls), self._draw_urls, False),
                ("qr", QR, lambda v: (v.urls[:1],), self._draw_qr, False),
                ("wifi_btn", WIFI_BTN, lambda v: (v.pressed == "wifi_list",), self._draw_wifi_btn, False),
            ]
        if v.page == "list":
            regs: list[Region] = [
                back,
                ("ltitle", LIST_TITLE, lambda v: (v.note, v.scroll, len(v.nets)), self._draw_list_title, False),
                ("rescan", RESCAN, lambda v: (v.scanning, v.pressed == "rescan"), self._draw_rescan, False),
            ]
            for i in range(ROWS):
                regs.append((f"row{i}", list_row(i), self._row_sig(i), self._row_draw(i), False))
            regs.append(("up", UP, lambda v: (v.scroll > 0, v.pressed == "up"), self._draw_up, False))
            regs.append(("down", DOWN, lambda v: (v.scroll + ROWS < len(v.nets), v.pressed == "down"), self._draw_down, False))
            return regs
        if v.page == "keys":
            regs = [
                ("ktitle", KB_TITLE, lambda v: (v.ssid, v.hint, v.note), self._draw_kb_title, False),
                ("field", FIELD, lambda v: (v.password if v.show_pw else len(v.password), v.show_pw), self._draw_field, False),
                ("eye", EYE, lambda v: (v.show_pw, v.pressed == "eye"), self._draw_eye, False),
                ("kbbg", KB_AREA, lambda v: (v.kb_page,), lambda d, s, v: None, True),
            ]
            for kid, box in kb_keys(v.kb_page, self.lang):
                regs.append((f"k{v.kb_page}:{kid}", box, self._key_sig(kid), self._key_draw(kid), False))
            return regs
        if v.page == "connect":
            return [
                ("ctitle", C_TITLE, lambda v: (v.ssid, v.note), self._draw_conn_title, False),
                ("cbody", C_BODY, lambda v: (v.phase, v.ssid, v.result_ip, v.result_url, v.result_error), self._draw_conn_body, False),
                ("celapsed", C_ELAPSED, lambda v: (v.phase, v.elapsed), self._draw_conn_elapsed, False),
                ("cbtns", C_BTNS, lambda v: (v.phase, v.pressed), self._draw_conn_btns, False),
            ]
        return []

    def render(self, v: NetView, full: bool = False) -> list[Box]:
        """Brings the frame up to `v`; returns the boxes that must go to the glass."""
        if full:
            self._sigs.clear()
            self.frame.paste(BG, (0, 0, W, H))
        before: Image.Image | None = None
        changed: list[Box] = []
        force = False
        for name, box, sig_fn, draw_fn, is_bg in self._regions(v):
            sig = sig_fn(v)
            if not full and not force and self._sigs.get(name) == sig:
                continue
            if is_bg:
                force = True  # whatever lies on top of it has to come back
            self._sigs[name] = sig
            if before is None and not full:
                before = self.frame.copy()
            tile = Image.new("RGB", (box[2] - box[0], box[3] - box[1]), BG)
            draw_fn(ImageDraw.Draw(tile), tile.size, v)
            self.frame.paste(tile, box[:2])
            changed.append(box)
        if full:
            return [(0, 0, W, H)]
        dirty: list[Box] = []
        for box in merge_boxes(changed, slack=0):
            assert before is not None
            bb = ImageChops.difference(self.frame.crop(box), before.crop(box)).getbbox()
            if bb:
                dirty.append((box[0] + bb[0], box[1] + bb[1], box[0] + bb[2], box[1] + bb[3]))
        return merge_boxes(dirty)

    # ---- shared pieces ----

    @staticmethod
    def _button(d: ImageDraw.ImageDraw, size: tuple[int, int], fill, radius: int = 12) -> None:
        d.rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=fill)

    def _draw_back(self, d, size, v: NetView) -> None:
        w, h = size
        d.line((0, h - 1, w, h - 1), fill=LINE)
        d.rounded_rectangle((6, 5, w - 8, h - 7), radius=10, fill=SURFACE)
        color = ACCENT_TEXT if v.pressed == "back" else TEXT
        icon_back(d, 24, (h - 2) / 2, 16, color)
        d.text((40, (h - 2) / 2), self.s["back"], font=self.value, fill=color, anchor="lm")

    def _header(self, d, size, title: str, note: str, right_pad: int = 12) -> None:
        w, h = size
        d.line((0, h - 1, w, h - 1), fill=LINE)
        x = 10
        room = w - x - right_pad
        if note:
            note_w = self.label.getlength(note)
            d.text((w - right_pad, (h - 2) / 2), note, font=self.label, fill=ACCENT_TEXT, anchor="rm")
            room -= note_w + 12
        d.text((x, (h - 2) / 2), ellipsize(title, self.head, max(20, room)), font=self.head, fill=TEXT, anchor="lm")

    def _draw_title(self, d, size, v: NetView) -> None:
        self._header(d, size, self.s["net_title"], v.note)

    # ---- overview ----

    def _kv(self, d, h: int, key: str, value: str, color, dot=None, w: int = ROW_HOST[2]) -> None:
        cy = h // 2
        d.text((14, cy), key, font=self.label, fill=DIM, anchor="lm")
        x = 104
        if dot is not None:
            d.ellipse((x, cy - 5, x + 10, cy + 5), fill=dot)
            x += 18
        d.text((x, cy), ellipsize(value, self.value, w - x - 6), font=self.value, fill=color, anchor="lm")

    def _draw_host(self, d, size, v: NetView) -> None:
        if not v.loaded:
            self._kv(d, size[1], self.s["host"], self.s["loading"], FAINT)
            return
        name = v.hostname + (".local" if v.mdns and v.hostname else "")
        self._kv(d, size[1], self.s["host"], name or "?", TEXT)

    def _link_value(self, link: Link | None, kind: str) -> tuple[str, tuple, tuple]:
        if link is None:
            return self.s["not_connected"], FAINT, FAINT
        if link.up:
            return link.ip4, TEXT, OK
        if link.state == "connecting":
            return self.s["connecting"], DIM, ACCENT
        if link.state == "unavailable":
            return (self.s["no_cable"] if kind == "eth" else self.s["wifi_off"]), FAINT, FAINT
        return self.s["not_connected"], FAINT, FAINT

    def _draw_eth(self, d, size, v: NetView) -> None:
        if not v.loaded:
            return
        if v.error:
            self._kv(d, size[1], self.s["eth"], "–", FAINT)
            return
        text, color, dot = self._link_value(v.eth, "eth")
        self._kv(d, size[1], self.s["eth"], text, color, dot)

    def _draw_wifi(self, d, size, v: NetView) -> None:
        if not v.loaded:
            return
        w, h = size
        if v.error:
            self._kv(d, 28, self.s["wifi"], "–", FAINT)
            d.text((104, 40), ellipsize(v.error, self.small, w - 110), font=self.small, fill=ERR, anchor="lm")
            return
        link = v.wifi
        if link is not None and link.up and link.ssid:
            cy = 14
            d.text((14, cy), self.s["wifi"], font=self.label, fill=DIM, anchor="lm")
            d.ellipse((104, cy - 5, 114, cy + 5), fill=OK)
            right = w - 6
            if link.signal >= 0:
                icon_bars(d, right - 20, cy + 8, 18, link.signal, ACCENT_TEXT, SURFACE_HI)
                right -= 30
            d.text((122, cy), ellipsize(link.ssid, self.value, right - 122), font=self.value, fill=TEXT, anchor="lm")
            d.text((122, 40), link.ip4, font=self.value, fill=TEXT, anchor="lm")
            x = 122 + self.value.getlength(link.ip4)
            pct = f"  ·  {link.signal} %"
            if link.signal >= 0 and x + self.label.getlength(pct) <= w - 4:
                d.text((x, 40), pct, font=self.label, fill=DIM, anchor="lm")
        else:
            text, color, dot = self._link_value(link, "wifi")
            self._kv(d, 28, self.s["wifi"], text, color, dot)

    def _draw_urls(self, d, size, v: NetView) -> None:
        if not v.loaded:
            return
        w, h = size
        d.line((14, 0, w - 8, 0), fill=LINE)
        if not v.urls:
            y = 14
            for line in wrap(self.s["offline_hint"], self.label, w - 28, 4):
                d.text((14, y), line, font=self.label, fill=DIM, anchor="la")
                y += 22
            return
        d.text((14, 8), self.s["open_in"], font=self.label, fill=DIM, anchor="la")
        y = 34
        for url in v.urls[:3]:
            d.text((14, y), ellipsize(url, self.url, w - 20), font=self.url, fill=ACCENT_TEXT, anchor="la")
            y += 21

    def _qr(self, text: str) -> list[list[bool]] | None:
        if self._qr_cache[0] != text:
            try:
                m = qr_encode(text, "M")
            except ValueError:
                m = None
            self._qr_cache = (text, m)
        return self._qr_cache[1]

    def _draw_qr(self, d, size, v: NetView) -> None:
        if not v.urls:
            return
        m = self._qr(v.urls[0])
        if m is None:
            return
        w, h = size
        n = len(m)
        quiet = 3
        avail = min(w - 8, h - 30)
        scale = max(2, avail // (n + 2 * quiet))
        side = scale * (n + 2 * quiet)
        x0 = (w - side) // 2
        y0 = 2
        d.rectangle((x0, y0, x0 + side - 1, y0 + side - 1), fill=QR_LIGHT)
        ox, oy = x0 + quiet * scale, y0 + quiet * scale
        for y, row in enumerate(m):
            x = 0
            while x < n:  # runs of dark modules as one rectangle — far fewer calls
                if row[x]:
                    x1 = x
                    while x1 + 1 < n and row[x1 + 1]:
                        x1 += 1
                    d.rectangle((ox + x * scale, oy + y * scale, ox + (x1 + 1) * scale - 1, oy + (y + 1) * scale - 1), fill=QR_DARK)
                    x = x1 + 1
                else:
                    x += 1
        d.text((w // 2, y0 + side + 12), self.s["scan_qr"], font=self.small, fill=FAINT, anchor="mm")

    def _draw_wifi_btn(self, d, size, v: NetView) -> None:
        w, h = size
        self._button(d, size, SURFACE, 14)
        color = ACCENT_TEXT if v.pressed == "wifi_list" else TEXT
        label = self.s["wifi_btn"]
        lw = self.f.button.getlength(label)
        x = (w - (lw + 38)) / 2
        icon_wifi(d, x + 13, h / 2 + 8, 26, 4, color, color)
        d.text((x + 38, h / 2), label, font=self.f.button, fill=color, anchor="lm")
        icon_back_right(d, w - 30, h / 2, 16, FAINT if v.pressed != "wifi_list" else color)

    # ---- list ----

    def _draw_list_title(self, d, size, v: NetView) -> None:
        note = v.note
        if not note and len(v.nets) > ROWS:
            a = v.scroll + 1
            b = min(len(v.nets), v.scroll + ROWS)
            note = self.s["page"].format(a=a, b=b, n=len(v.nets))
        self._header(d, size, self.s["list_title"], note, right_pad=8)

    def _draw_rescan(self, d, size, v: NetView) -> None:
        w, h = size
        d.line((0, h - 1, w, h - 1), fill=LINE)
        d.rounded_rectangle((4, 5, w - 8, h - 7), radius=10, fill=SURFACE)
        if v.scanning:
            label, color = self.s["scanning"], FAINT
        else:
            label, color = self.s["rescan"], ACCENT_TEXT if v.pressed == "rescan" else TEXT
        d.text(((w - 4) / 2, (h - 2) / 2), label, font=self.value, fill=color, anchor="mm")

    def _row_sig(self, i: int) -> Callable[[NetView], tuple]:
        def sig(v: NetView) -> tuple:
            k = v.scroll + i
            net = v.nets[k] if k < len(v.nets) else None
            empty = None
            if net is None and i == 0:
                empty = (v.scanning, v.scanned, v.scan_error)
            return (net, empty, v.pressed == f"row{i}")
        return sig

    def _row_draw(self, i: int):
        def draw(d, size, v: NetView) -> None:
            w, h = size
            k = v.scroll + i
            if k >= len(v.nets):
                if i == 0:
                    if v.scan_error:
                        msg, color = v.scan_error, ERR
                    elif v.scanning or not v.scanned:
                        msg, color = self.s["searching"], DIM
                    else:
                        msg, color = self.s["no_networks"], DIM
                    d.text((14, h // 2), ellipsize(msg, self.label, w - 20), font=self.label, fill=color, anchor="lm")
                return
            net = v.nets[k]
            pressed = v.pressed == f"row{i}"
            self._button(d, size, SURFACE, 12)
            icon_bars(d, 14, h / 2 + 10, 22, net.signal, ACCENT_TEXT if net.in_use else TEXT, SURFACE_HI)
            color = ACCENT_TEXT if pressed else TEXT
            d.text((50, 9), ellipsize(net.ssid, self.ssid, w - 100), font=self.ssid, fill=color, anchor="la")
            if net.in_use:
                sub, sub_color = self.s["in_use"], OK
            elif net.secure:
                sub, sub_color = f"{self.s['secured']} · {net.security}", FAINT
            else:
                sub, sub_color = self.s["open"], FAINT
            d.text((50, h - 10), ellipsize(sub, self.small, w - 100), font=self.small, fill=sub_color, anchor="ls")
            if net.secure:
                icon_lock(d, w - 26, h / 2 - 2, 22, DIM)
        return draw

    def _arrow(self, d, size, enabled: bool, pressed: bool, up: bool) -> None:
        w, h = size
        self._button(d, size, SURFACE, 12)
        color = FAINT if not enabled else (ACCENT_TEXT if pressed else TEXT)
        icon_chevron(d, w / 2, h / 2, 26, color, up)

    def _draw_up(self, d, size, v: NetView) -> None:
        self._arrow(d, size, v.scroll > 0, v.pressed == "up", True)

    def _draw_down(self, d, size, v: NetView) -> None:
        self._arrow(d, size, v.scroll + ROWS < len(v.nets), v.pressed == "down", False)

    # ---- keyboard ----

    def _draw_kb_title(self, d, size, v: NetView) -> None:
        w, h = size
        x = 10
        right = v.note or v.hint
        room = w - 20
        if right:
            rw = self.label.getlength(right)
            d.text((w - 10, h / 2), right, font=self.label, fill=ACCENT_TEXT if v.note else ERR, anchor="rm")
            room -= rw + 14
        prefix, _, _ = self.s["pw_for"].partition("{ssid}")
        d.text((x, h / 2), prefix, font=self.label, fill=DIM, anchor="lm")
        x += self.label.getlength(prefix)
        d.text((x, h / 2), ellipsize(v.ssid, self.value, max(20, room - x + 10)), font=self.value, fill=TEXT, anchor="lm")

    def _draw_field(self, d, size, v: NetView) -> None:
        w, h = size
        d.rounded_rectangle((0, 0, w - 1, h - 1), radius=10, fill=SURFACE, outline=SURFACE_HI)
        cy = h / 2
        room = w - 28
        if v.show_pw:
            text, font = v.password, self.field
            # the end is what you're typing — keep it in view
            while text and font.getlength("…" + text) > room and font.getlength(text) > room:
                text = text[1:]
            if text != v.password:
                text = "…" + text
            d.text((12, cy), text, font=font, fill=TEXT, anchor="lm")
            end = 12 + font.getlength(text)
        else:
            n = len(v.password)
            r, step = 5, 17
            fit = max(1, int((room - 10) // step))
            shown = min(n, fit)
            x = 12
            if n > shown:
                d.text((x, cy), "…", font=self.field, fill=DIM, anchor="lm")
                x += self.field.getlength("…") + 2
                shown = min(shown, int((room - (x - 12)) // step))
            for i in range(shown):
                cx = x + r + i * step
                d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=TEXT)
            end = x + shown * step
        d.rectangle((end + 2, cy - 13, end + 4, cy + 13), fill=ACCENT)

    def _draw_eye(self, d, size, v: NetView) -> None:
        self._button(d, size, SURFACE, 10)
        color = ACCENT_TEXT if v.pressed == "eye" or v.show_pw else TEXT
        icon_eye(d, size[0] / 2, size[1] / 2, 30, color, crossed=not v.show_pw)

    def _key_sig(self, kid: str) -> Callable[[NetView], tuple]:
        if kid.startswith("c:"):
            return lambda v: (v.shift > 0 and v.kb_page == "abc", v.pressed == kid)
        if kid == "shift":
            return lambda v: (v.shift, v.pressed == kid)
        if kid == "ok":
            return lambda v: (v.can_connect, v.pressed == kid)
        return lambda v: (v.kb_page, v.pressed == kid)

    def _key_draw(self, kid: str):
        def draw(d, size, v: NetView) -> None:
            w, h = size
            pressed = v.pressed == kid
            cx, cy = w / 2, h / 2
            if kid == "ok":
                on = v.can_connect
                self._button(d, size, ACCENT if on else SURFACE, 10)
                color = (PRESSED_ON_ACCENT if pressed else ON_ACCENT) if on else FAINT
                d.text((cx, cy), self.s["connect"], font=self.f.button, fill=color, anchor="mm")
                return
            special = not kid.startswith("c:") and kid != "space"
            fill = SURFACE_HI if pressed else (SURFACE if not special else (40, 42, 47))
            if kid == "shift" and v.shift == 2:
                fill = ACCENT
            self._button(d, size, fill, 10)
            color = ACCENT_TEXT if pressed else TEXT
            if kid.startswith("c:"):
                ch = kid[2:]
                if v.kb_page == "abc" and v.shift:
                    ch = ch.upper()
                d.text((cx, cy + 1), ch, font=self.key, fill=color, anchor="mm")
            elif kid == "shift":
                if v.shift == 2:
                    icon_shift(d, cx, cy, 24, ON_ACCENT, True)
                else:
                    icon_shift(d, cx, cy, 24, ACCENT_TEXT if v.shift == 1 or pressed else TEXT, v.shift == 1)
            elif kid == "bksp":
                icon_bksp(d, cx, cy, 26, color)
            elif kid == "sym":
                d.text((cx, cy), "#+=" if v.kb_page == "123" else "123", font=self.key_small, fill=color, anchor="mm")
            elif kid == "mode":
                d.text((cx, cy), "123" if v.kb_page == "abc" else "abc", font=self.key_small, fill=color, anchor="mm")
            elif kid == "space":
                d.text((cx, cy), self.s["space"], font=self.label, fill=DIM if not pressed else color, anchor="mm")
            elif kid == "cancel":
                d.text((cx, cy), self.s["cancel"], font=self.value, fill=color, anchor="mm")
        return draw

    # ---- connecting ----

    def _draw_conn_title(self, d, size, v: NetView) -> None:
        self._header(d, size, self.s["conn_title"].format(ssid=v.ssid), v.note)

    def _draw_conn_body(self, d, size, v: NetView) -> None:
        w, h = size
        cx = w // 2
        if v.phase == "busy":
            icon_wifi(d, cx, 76, 60, 4, ACCENT, SURFACE_HI)
            d.text((cx, 112), self.s["conn_busy"], font=self.big, fill=TEXT, anchor="mm")
            d.text((cx, 146), self.s["conn_wait"], font=self.label, fill=FAINT, anchor="mm")
        elif v.phase == "ok":
            d.ellipse((cx - 28, 8, cx + 28, 64), fill=OK)
            icon_check(d, cx, 36, 36, BG)
            d.text((cx, 90), self.s["conn_ok"], font=self.big, fill=TEXT, anchor="mm")
            if v.result_ip:
                d.text((cx, 124), self.s["conn_ip"].format(ip=v.result_ip), font=self.value, fill=DIM, anchor="mm")
            if v.result_url:
                d.text((cx, 152), ellipsize(v.result_url, self.url, w - 30), font=self.url, fill=ACCENT_TEXT, anchor="mm")
        elif v.phase == "error":
            d.ellipse((cx - 24, 6, cx + 24, 54), fill=ERR)
            icon_cross(d, cx, 30, 30, BG)
            d.text((cx, 78), self.s["conn_fail"], font=self.big, fill=TEXT, anchor="mm")
            y = 102
            for line in wrap(v.result_error, self.value, w - 40, 3):
                d.text((cx, y), line, font=self.value, fill=ERR, anchor="ma")
                y += 24

    def _draw_conn_elapsed(self, d, size, v: NetView) -> None:
        if v.phase == "busy":
            d.text((size[0] // 2, size[1] // 2), f"{v.elapsed} s", font=self.label, fill=DIM, anchor="mm")

    def _draw_conn_btns(self, d, size, v: NetView) -> None:
        if v.phase not in ("ok", "error"):
            return
        ox, oy = C_BTNS[0], C_BTNS[1]

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

        if v.phase == "ok":
            btn(BTN_FULL, "done", self.s["done"], True)
        else:
            btn(BTN_L, "list", self.s["back"], False)
            btn(BTN_R, "retry", self.s["retry"], True)


def icon_back_right(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float, fill) -> None:
    d.line([(cx - s * 0.25, cy - s * 0.5), (cx + s * 0.25, cy), (cx - s * 0.25, cy + s * 0.5)], fill=fill, width=4, joint="curve")
