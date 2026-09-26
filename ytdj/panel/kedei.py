"""KeDei 3.5" v6.2 (480×320 SPI LCD + XPT2046 dotyk) pro panel ytdj.

Deska není kompatibilní s ILI9486 overlayi a hotový ovladač pro dnešní
64bitový kernel neexistuje. Protokol je ale jednoduchý (viz kedei.c) — jen
vyžaduje přepnout dva chip selecty kolem každého tříbajtového slova, což je
přes spidev nesnesitelně pomalé. Obraz i dotyk proto obsluhuje malá knihovna
v C přímo přes registry; tenhle modul ji zkompiluje, načte a obalí rozhraním
z hw.py.

Potřebuje roota (/dev/mem) a vypnutý kernelový ovladač SPI0 — pokud je
navázaný, odpojíme ho sami. Natrvalo patří do config.txt `dtparam=spi=off`.

    python -m ytdj.panel.kedei --test        # testovací obrazec + výpis dotyků
    python -m ytdj.panel.kedei --calibrate   # kalibrace dotyku do CALIBRATION
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from PIL import Image, ImageDraw

from .hw import Box, TouchEvent

log = logging.getLogger(__name__)

WIDTH, HEIGHT = 480, 320
SOURCE = Path(__file__).with_name("kedei.c")
LIB_DIR = Path(os.environ.get("YTDJ_PANEL_LIB_DIR", "/var/cache/ytdj-panel"))
CALIBRATION = Path(os.environ.get("YTDJ_PANEL_TOUCH", "/etc/ytdj/panel-touch.json"))
SPI_DRIVER = Path("/sys/bus/platform/drivers/spi-bcm2835")
SPI_DEVICE = "3f204000.spi"

# MADCTL pro orientaci na šířku: 0 = konektor GPIO dole, 180 = nahoře
# (ověřeno kamerou na skutečném kusu).
MADCTL = {0: 0x6A, 180: 0xAA}

# Pod tímhle tlakem (z1 z převodníku) je to spíš šum než prst.
MIN_PRESSURE = 120


def _build_lib() -> Path:
    so = LIB_DIR / "libkedei.so"
    if so.exists() and so.stat().st_mtime >= SOURCE.stat().st_mtime:
        return so
    LIB_DIR.mkdir(parents=True, exist_ok=True)
    tmp = so.with_suffix(".so.tmp")
    log.info("kompiluji %s", so)
    subprocess.run(
        ["cc", "-O2", "-Wall", "-shared", "-fPIC", "-o", str(tmp), str(SOURCE)],
        check=True,
    )
    tmp.replace(so)
    return so


def _release_kernel_spi() -> None:
    """Kernelový ovladač SPI0 by nám přepisoval piny i registry — odpojit."""
    if (SPI_DRIVER / SPI_DEVICE).exists():
        log.warning("kernelový ovladač SPI0 je navázaný, odpojuji ho "
                    "(natrvalo: dtparam=spi=off v config.txt)")
        (SPI_DRIVER / "unbind").write_text(SPI_DEVICE)


class _Device:
    """Jediný přístup ke sběrnici pro obraz i dotyk — volají ho dvě vlákna."""

    _instance: _Device | None = None
    _guard = threading.Lock()

    def __init__(self) -> None:
        if os.geteuid() != 0:
            raise PermissionError("KeDei panel potřebuje roota (/dev/mem)")
        _release_kernel_spi()
        lib = ctypes.CDLL(str(_build_lib()))
        lib.kd_open.argtypes = [ctypes.c_int]
        lib.kd_init.argtypes = [ctypes.c_int]
        lib.kd_madctl.argtypes = [ctypes.c_int]
        lib.kd_blit_rgb.argtypes = [ctypes.c_int] * 4 + [ctypes.c_char_p, ctypes.c_int]
        lib.kd_touch.argtypes = [ctypes.POINTER(ctypes.c_int)] * 3
        lib.kd_pen_down.argtypes = []
        rc = lib.kd_open(0)
        if rc != 0:
            raise OSError(f"KeDei: nepodařilo se namapovat registry (kd_open={rc})")
        self.lib = lib
        self.lock = threading.Lock()
        self.madctl: int | None = None

    @classmethod
    def get(cls) -> _Device:
        with cls._guard:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def init(self, madctl: int) -> None:
        with self.lock:
            if self.madctl is None:
                self.lib.kd_init(madctl)
            elif self.madctl != madctl:
                self.lib.kd_madctl(madctl)
            self.madctl = madctl

    def blit(self, img: Image.Image, box: Box) -> None:
        left, top, right, bottom = box
        data = img.crop(box).tobytes()
        with self.lock:
            self.lib.kd_blit_rgb(left, top, right - 1, bottom - 1, data, (right - left) * 3)

    def touch_raw(self) -> tuple[int, int, int] | None:
        x, y, z = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        with self.lock:
            ok = self.lib.kd_touch(ctypes.byref(x), ctypes.byref(y), ctypes.byref(z))
        return (x.value, y.value, z.value) if ok else None

    def pen_down(self) -> bool:
        """Jen úroveň PENIRQ (jedno čtení GPIO) — pro statistiku anomálií."""
        return bool(self.lib.kd_pen_down())


def _check_rotate(rotate: int) -> int:
    if rotate not in MADCTL:
        raise ValueError(f"KeDei umí jen orientaci na šířku: rotate 0 nebo 180, ne {rotate}")
    return MADCTL[rotate]


class KedeiScreen:
    def __init__(self, rotate: int = 0) -> None:
        self.size = (WIDTH, HEIGHT)
        self._dev = _Device.get()
        self._dev.init(_check_rotate(rotate))

    def show(self, img: Image.Image, box: Box | None = None) -> None:
        if img.mode != "RGB":
            img = img.convert("RGB")
        box = box or (0, 0, WIDTH, HEIGHT)
        left, top = max(box[0], 0), max(box[1], 0)
        right, bottom = min(box[2], WIDTH), min(box[3], HEIGHT)
        if right > left and bottom > top:
            self._dev.blit(img, (left, top, right, bottom))

    def close(self) -> None:
        pass


# ---- dotyk ----


class Calibration:
    """Afinní převod surových hodnot převodníku na pixely: pokryje prohozené
    i převrácené osy a mírné natočení vrstvy vůči displeji."""

    def __init__(self, ax: float, bx: float, cx: float, ay: float, by: float, cy: float):
        self.coef = (ax, bx, cx, ay, by, cy)

    def map(self, rx: int, ry: int) -> tuple[int, int]:
        ax, bx, cx, ay, by, cy = self.coef
        x = ax * rx + bx * ry + cx
        y = ay * rx + by * ry + cy
        return (min(max(int(round(x)), 0), WIDTH - 1),
                min(max(int(round(y)), 0), HEIGHT - 1))

    @classmethod
    def fit(cls, pairs: list[tuple[tuple[int, int], tuple[int, int]]]) -> Calibration:
        """Nejmenší čtverce přes (raw, screen) páry — aspoň tři body."""
        rows = [(rx, ry, 1.0) for (rx, ry), _ in pairs]

        def solve(targets: list[float]) -> list[float]:
            # normální rovnice A^T A c = A^T t, 3×3 Gaussem
            m = [[sum(r[i] * r[j] for r in rows) for j in range(3)] +
                 [sum(r[i] * t for r, t in zip(rows, targets))] for i in range(3)]
            for col in range(3):
                piv = max(range(col, 3), key=lambda k: abs(m[k][col]))
                m[col], m[piv] = m[piv], m[col]
                if abs(m[col][col]) < 1e-9:
                    raise ValueError("kalibrační body jsou degenerované")
                for k in range(3):
                    if k != col:
                        f = m[k][col] / m[col][col]
                        m[k] = [a - f * b for a, b in zip(m[k], m[col])]
            return [m[i][3] / m[i][i] for i in range(3)]

        cx = solve([float(s[0]) for _, s in pairs])
        cy = solve([float(s[1]) for _, s in pairs])
        return cls(*cx, *cy)

    source = "custom"  # odkud kalibrace je — do provozního logu

    def then(self, other: "Calibration") -> "Calibration":
        """Nejdřív self, pak other (skládání afinních map): other(self(raw))."""
        ax, bx, cx, ay, by, cy = self.coef
        ox, px, qx, oy, py, qy = other.coef
        return Calibration(
            ox * ax + px * ay, ox * bx + px * by, ox * cx + px * cy + qx,
            oy * ax + py * ay, oy * bx + py * by, oy * cx + py * cy + qy,
        )



    @classmethod
    def load(cls, path: Path = CALIBRATION) -> Calibration:
        try:
            cal = cls(*json.loads(path.read_text())["coef"])
            cal.source = f"file:{path}"
            return cal
        except FileNotFoundError:
            log.info("kalibrace dotyku %s neexistuje — používám výchozí", path)
            cal = cls.default()
            cal.source = "default"
            return cal

    def save(self, path: Path = CALIBRATION) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"coef": self.coef}) + "\n")

    @classmethod
    def default(cls) -> Calibration:
        # Změřeno na skutečném kusu (orientace 0, GPIO dole): osy vrstvy jsou
        # vůči displeji prohozené — surové y jde zleva doprava, surové x
        # zdola nahoru. Odchylka do ~10 px, na tlačítka panelu to stačí.
        return cls(0.0031, 0.1317, -31.9, -0.0897, -0.0003, 332.3)


# z pixelů naměřených se starou kalibrací na pixely, kam prst mířil
MAX_CAL_ERROR = 25  # px — horší zbytek po proložení = body nesedí, nic se neuloží
FLIP = Calibration(-1, 0, WIDTH - 1, 0, -1, HEIGHT - 1)  # otočení o 180°


def screen_correction(pairs: list[tuple[tuple[int, int], tuple[int, int]]]) -> tuple[Calibration, float]:
    """Oprava v souřadnicích obrazovky z párů (kam dotyk padl, kde byl křížek); (oprava, největší zbytek px)."""
    if len(pairs) < 3:
        raise ValueError("na kalibraci jsou potřeba aspoň tři body")
    corr = Calibration.fit(pairs)
    err = 0.0
    for (mx, my), (tx, ty) in pairs:
        ax, bx, cx, ay, by, cy = corr.coef
        err = max(err, abs(ax * mx + bx * my + cx - tx), abs(ay * mx + by * my + cy - ty))
    # rozumná oprava jen posouvá, mírně natahuje a natáčí — ne zrcadlí ani nemačká
    ax, bx, _, ay, by, _ = corr.coef
    if not (0.6 < ax < 1.6 and 0.6 < by < 1.6 and abs(bx) < 0.4 and abs(ay) < 0.4):
        raise ValueError(f"kalibrační body nedávají smysl ({ax:.2f}, {bx:.2f}, {ay:.2f}, {by:.2f})")
    return corr, err


class KedeiTouch:
    """Vzorkuje převodník, filtruje šum a skládá z něj down/move/up."""

    DOWN_INTERVAL = 0.015  # s — vzorkování při držení
    IDLE_INTERVAL = 0.025  # s — hlídání PENIRQ
    RELEASE_SAMPLES = 3    # tolik prázdných vzorků za sebou (PENIRQ nahoře) = prst je pryč
    # Stisk platí, když aspoň PRESS_SAMPLES z posledních PRESS_WINDOW platných
    # vzorků leží do PRESS_SPREAD px od nejnovějšího; poloha = jejich medián.
    # Dřív 3 po sobě do 12 px a každý vadný vzorek stisk zahodil: na Pi
    # 565× spread_rejected a 155× aborted_press na 347 stisků (25.–26. 9.),
    # od 11:30 250× spread_rejected na 115 stisků.
    PRESS_SAMPLES = 2
    PRESS_WINDOW = 3
    PRESS_SPREAD = 24      # px
    JUMP = 60              # px — větší skok při držení musí potvrdit další vzorek
    JUMP_SPREAD = 12       # px — jak blízko musí být potvrzující vzorek skoku
    MOVE_THRESHOLD = 3     # px
    # PENIRQ dole, ale převodník nedal použitelný vzorek (šum, slabý tlak):
    # nic neruší ani neukončuje; teprve tolik za sebou při držení = konec
    # (kdyby PENIRQ zůstal viset dole).
    NOISY_RELEASE = 12

    def __init__(self, rotate: int = 0, calibration: Calibration | None = None) -> None:
        self._dev = _Device.get()
        self._dev.init(_check_rotate(rotate))
        self._rotate = rotate
        self._cal = calibration or Calibration.load()
        self._down = False
        self._pos = (0, 0)
        self._misses = 0
        self._candidates: list[tuple[int, int]] = []
        self._recent: list[tuple[int, int]] = []  # poslední platné vzorky při držení (medián)
        self._noisy = 0  # vadné vzorky za sebou při držení
        self._jump: tuple[int, int] | None = None
        # Anomálie převodníku za poslední souhrn (panel.touch_driver). Jen
        # přičítání v tomhle vlákně; `take_stats()` z hlavního vlákna vymění
        # celý slovník — případná ztráta jednoho přičtení nevadí.
        self._stats: dict[str, int] = {}

    def apply_correction(self, pairs: list[tuple[tuple[int, int], tuple[int, int]]],
                         path: Path | None = None) -> float:
        """Kalibrace z panelu: páry (kam dotyk padl, kde byl křížek) v pixelech
        obrazovky. Opraví a uloží kalibraci, platí hned; vrátí největší zbytek (px).
        ValueError, když body nesedí — pak se nic nemění."""
        corr, err = screen_correction(pairs)
        if err > MAX_CAL_ERROR:
            raise ValueError(f"odchylka {err:.0f} px")
        if self._rotate == 180:
            corr = FLIP.then(corr).then(FLIP)  # oprava v souřadnicích před otočením
        new = self._cal.then(corr)
        target = path or CALIBRATION
        new.save(target)
        new.source = f"file:{target}"
        self._cal = new
        log.info("kalibrace dotyku uložena do %s (odchylka %.0f px)", target, err)
        return err

    @property
    def calibration_source(self) -> str:
        return getattr(self._cal, "source", "custom")

    def _bump(self, key: str) -> None:
        st = self._stats
        st[key] = st.get(key, 0) + 1

    def take_stats(self) -> dict[str, int]:
        st, self._stats = self._stats, {}
        return st

    def _sample(self) -> tuple[int, int] | str:
        """Poloha (x, y), nebo "noisy" (prst tam je, vzorek ale nepoužitelný), nebo "up"."""
        raw = self._dev.touch_raw()
        if raw is None:
            # PENIRQ hlásí prst, ale převodník nedal použitelné vzorky
            # (zvedl se uprostřed měření, klidové hodnoty, rozptyl)
            if self._dev.pen_down():
                self._bump("invalid_samples")
                return "noisy"
            return "up"
        if raw[2] < MIN_PRESSURE:
            self._bump("low_pressure")
            return "noisy"
        x, y = self._cal.map(raw[0], raw[1])
        if self._rotate == 180:
            x, y = WIDTH - 1 - x, HEIGHT - 1 - y
        return x, y

    @staticmethod
    def _median(points: list[tuple[int, int]]) -> tuple[int, int]:
        xs = sorted(p[0] for p in points)
        ys = sorted(p[1] for p in points)
        return xs[len(xs) // 2], ys[len(ys) // 2]

    def _landing(self, pos: tuple[int, int]) -> TouchEvent | None:
        """Prst dosedá: stisk, jakmile se pár vzorků shodne."""
        self._candidates = (self._candidates + [pos])[-self.PRESS_WINDOW:]
        near = [p for p in self._candidates
                if max(abs(p[0] - pos[0]), abs(p[1] - pos[1])) <= self.PRESS_SPREAD]
        if len(near) >= self.PRESS_SAMPLES:
            self._down = True
            self._pos = self._median(near)
            self._recent = near[-3:]
            self._candidates, self._misses, self._noisy = [], 0, 0
            self._bump("downs")
            return TouchEvent("down", *self._pos)
        if len(self._candidates) >= self.PRESS_SAMPLES:
            self._bump("spread_rejected")  # dosedající prst, vzorky rozházené
        return None

    def _holding(self, pos: tuple[int, int]) -> TouchEvent | None:
        """Prst drží: pohyb z mediánu posledních vzorků, osamělé skoky pryč."""
        dist = max(abs(pos[0] - self._pos[0]), abs(pos[1] - self._pos[1]))
        if dist > self.JUMP and (
            self._jump is None
            or max(abs(pos[0] - self._jump[0]), abs(pos[1] - self._jump[1])) > self.JUMP_SPREAD
        ):
            if self._jump is not None:
                self._bump("jump_dropped")  # předchozí skok nikdo nepotvrdil
            self._jump = pos  # počkat, jestli to potvrdí další vzorek
            return None
        if dist > self.JUMP:
            self._recent = [self._jump, pos]  # potvrzený skok: prst se opravdu přesunul
        else:
            self._recent = (self._recent + [pos])[-3:]
        self._jump = None
        new = self._median(self._recent)
        if max(abs(new[0] - self._pos[0]), abs(new[1] - self._pos[1])) >= self.MOVE_THRESHOLD:
            self._pos = new
            return TouchEvent("move", *new)
        return None

    def poll(self, timeout: float) -> TouchEvent | None:
        deadline = time.monotonic() + timeout
        while True:
            pos = self._sample()
            ev: TouchEvent | None = None
            if isinstance(pos, tuple):
                self._misses = self._noisy = 0
                ev = self._holding(pos) if self._down else self._landing(pos)
            elif pos == "noisy":
                # šum při dosedání ani při držení stisk neruší
                if self._down:
                    self._noisy += 1
                    if self._noisy >= self.NOISY_RELEASE:
                        self._bump("noisy_release")
                        ev = self._release()
            else:  # PENIRQ nahoře: prst se zvedá
                if self._candidates:
                    # prst "dosedl", ale stisk se nepotvrdil
                    self._bump("aborted_press")
                self._candidates = []
                self._jump = None
                if self._down:
                    self._misses += 1
                    if self._misses >= self.RELEASE_SAMPLES:
                        ev = self._release()
            if ev is not None:
                return ev
            now = time.monotonic()
            if now >= deadline:
                return None
            interval = self.DOWN_INTERVAL if (self._down or self._candidates) else self.IDLE_INTERVAL
            time.sleep(min(interval, deadline - now))

    def _release(self) -> TouchEvent:
        self._down, self._misses, self._noisy = False, 0, 0
        self._jump = None
        self._recent = []
        return TouchEvent("up", *self._pos)

    def close(self) -> None:
        pass


# ---- nástroje: test a kalibrace ----


def _raw_press(dev: _Device, settle: float = 0.15) -> tuple[int, int]:
    """Počká na stisk, zprůměruje ho a počká na uvolnění."""
    while (r := dev.touch_raw()) is None or r[2] < MIN_PRESSURE:
        time.sleep(0.02)
    time.sleep(settle)
    xs, ys = [], []
    misses = 0
    while misses < 3:
        r = dev.touch_raw()
        if r is None or r[2] < MIN_PRESSURE:
            misses += 1
        else:
            misses = 0
            xs.append(r[0])
            ys.append(r[1])
        time.sleep(0.015)
    if not xs:
        return _raw_press(dev, settle)
    xs.sort()
    ys.sort()
    return xs[len(xs) // 2], ys[len(ys) // 2]


def _target(screen: KedeiScreen, img: Image.Image, x: int, y: int, text: str) -> None:
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, WIDTH - 1, HEIGHT - 1], fill="black")
    d.line([x - 15, y, x + 15, y], fill="white", width=3)
    d.line([x, y - 15, x, y + 15], fill="white", width=3)
    d.ellipse([x - 5, y - 5, x + 5, y + 5], outline="#e0a84a", width=2)
    d.text((WIDTH // 2 - 110, HEIGHT // 2 - 8), text, fill="#cccccc")
    screen.show(img)


def calibrate(rotate: int, max_error: int = 25) -> Calibration:
    screen = KedeiScreen(rotate)
    dev = _Device.get()
    img = Image.new("RGB", (WIDTH, HEIGHT))
    m = 40  # úplné rohy staré odporové vrstvy bývají hluché
    points = [(m, m), (WIDTH - m, m), (WIDTH - m, HEIGHT - m), (m, HEIGHT - m),
              (WIDTH // 2, HEIGHT // 2)]
    while True:
        pairs = []
        for i, (x, y) in enumerate(points, 1):
            _target(screen, img, x, y, f"Kalibrace: podrz krizek ({i}/{len(points)})")
            raw = _raw_press(dev)
            log.info("bod %d: obrazovka %s <- surove %s", i, (x, y), raw)
            # ukládáme pro orientaci 0, otočení řeší KedeiTouch
            s = (x, y) if rotate == 0 else (WIDTH - 1 - x, HEIGHT - 1 - y)
            pairs.append((raw, s))
        cal = Calibration.fit(pairs)
        err = max(abs(a - b) for r, s in pairs for a, b in zip(cal.map(*r), s))
        log.info("největší odchylka po kalibraci: %d px", err)
        if err <= max_error:
            return cal
        log.warning("odchylka je moc velká, znovu")


def _test(rotate: int) -> None:
    screen = KedeiScreen(rotate)
    img = Image.new("RGB", (WIDTH, HEIGHT))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, WIDTH - 1, 19], fill="#00c000")
    d.rectangle([0, 0, 19, HEIGHT - 1], fill="#0000ff")
    d.rectangle([0, 0, 59, 59], fill="#ff0000")
    d.line([0, HEIGHT // 2, WIDTH, HEIGHT // 2], fill="white", width=3)
    d.line([WIDTH // 2, 0, WIDTH // 2, HEIGHT], fill="white", width=3)
    t = time.monotonic()
    screen.show(img)
    log.info("celý snímek %.3f s", time.monotonic() - t)
    touch = KedeiTouch(rotate)
    while True:
        ev = touch.poll(1.0)
        if ev:
            print(ev, flush=True)
            if ev.kind != "up":
                d.ellipse([ev.x - 7, ev.y - 7, ev.x + 7, ev.y + 7], fill="#e0a84a")
                screen.show(img, (ev.x - 8, ev.y - 8, ev.x + 8, ev.y + 8))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m ytdj.panel.kedei")
    p.add_argument("--rotate", type=int, default=0, choices=sorted(MADCTL))
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--test", action="store_true", help="obrazec + výpis dotyků")
    g.add_argument("--calibrate", action="store_true", help=f"kalibrace do {CALIBRATION}")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.calibrate:
        cal = calibrate(args.rotate)
        cal.save()
        print(f"uloženo do {CALIBRATION}")
    else:
        _test(args.rotate)
    return 0


if __name__ == "__main__":
    sys.exit(main())
