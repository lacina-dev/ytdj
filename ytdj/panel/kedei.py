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
import hashlib
import json
import logging
import math
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


def _source_digest() -> str:
    return hashlib.sha256(SOURCE.read_bytes()).hexdigest()


def _build_lib() -> Path:
    """Přeloží kedei.c, když se změnil jeho obsah (ne čas: rsync -a přenáší
    čas z notebooku, a starší zdroj s novějším .so na Pi — 26. 9. 23:50 —
    nechal běžet starou knihovnu bez kd_touch_ex; dotyk minutu nešel)."""
    so = LIB_DIR / "libkedei.so"
    stamp = LIB_DIR / "libkedei.so.sha256"
    digest = _source_digest()
    try:
        current = stamp.read_text().strip() if so.exists() else ""
    except OSError:
        current = ""
    if current == digest:
        return so
    LIB_DIR.mkdir(parents=True, exist_ok=True)
    tmp = so.with_suffix(".so.tmp")
    log.info("kompiluji %s", so)
    subprocess.run(
        ["cc", "-O2", "-Wall", "-shared", "-fPIC", "-o", str(tmp), str(SOURCE)],
        check=True,
    )
    tmp.replace(so)
    stamp.write_text(digest + "\n")
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
        if hasattr(lib, "kd_touch_ex"):  # diagnostics: why a sample was refused, raw Z1/Z2
            lib.kd_touch_ex.argtypes = [ctypes.POINTER(ctypes.c_int)]
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

    def touch_ex(self) -> tuple[int, tuple[int, ...]]:
        """(kód, (x, y, z1, z2, rozptyl x, rozptyl y)) — viz kd_touch_ex v kedei.c."""
        v = (ctypes.c_int * 6)()
        with self.lock:
            code = self.lib.kd_touch_ex(v)
        return code, tuple(v)

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
    i převrácené osy a mírné natočení vrstvy vůči displeji.

    Nahoře volitelně mřížka 3×3 oprav (`grid`): odporová vrstva se neprohýbá
    všude stejně — po kalibraci 5 body na Pi (26. 9. 16:42) zůstalo 12 px,
    vlevo dole se četlo výš, vpravo dole níž. Mřížka z 9 bodů se mezi uzly
    bilineárně interpoluje.
    """

    def __init__(self, ax: float, bx: float, cx: float, ay: float, by: float, cy: float,
                 grid: dict | None = None):
        self.coef = (ax, bx, cx, ay, by, cy)
        self.grid = grid  # {"xs": [3], "ys": [3], "d": [[dx, dy] × 9, po řádcích]}

    def affine(self, rx: float, ry: float) -> tuple[float, float]:
        ax, bx, cx, ay, by, cy = self.coef
        return ax * rx + bx * ry + cx, ay * rx + by * ry + cy

    def offset(self, x: float, y: float) -> tuple[float, float]:
        """Oprava z mřížky v bodě (x, y) obrazovky; bez mřížky (0, 0)."""
        g = self.grid
        if not g:
            return 0.0, 0.0
        xs, ys, d = g["xs"], g["ys"], g["d"]
        i = 0 if x < xs[1] else 1
        j = 0 if y < ys[1] else 1
        # za krajními uzly jen mírně dál (prodloužení krajní buňky), ne do nekonečna
        tx = min(max((x - xs[i]) / (xs[i + 1] - xs[i]), -0.3), 1.3)
        ty = min(max((y - ys[j]) / (ys[j + 1] - ys[j]), -0.3), 1.3)

        def node(a: int, b: int) -> list[float]:
            return d[b * 3 + a]

        out = []
        for k in (0, 1):
            top = node(i, j)[k] * (1 - tx) + node(i + 1, j)[k] * tx
            bottom = node(i, j + 1)[k] * (1 - tx) + node(i + 1, j + 1)[k] * tx
            out.append(top * (1 - ty) + bottom * ty)
        return out[0], out[1]

    def map_float(self, rx: float, ry: float) -> tuple[float, float]:
        x, y = self.affine(rx, ry)
        dx, dy = self.offset(x, y)
        return x + dx, y + dy

    def map(self, rx: int, ry: int) -> tuple[int, int]:
        x, y = self.map_float(rx, ry)
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
        """Kalibrace ze souboru; cokoli špatně → výchozí (nebo bez mřížky), nikdy výjimka.

        Poškozený soubor (výpadek proudu při zápisu, Pi hlásí podpětí) nesmí
        panel shodit ani umrtvit dotyk — log a náhradní kalibrace.
        """
        try:
            data = json.loads(path.read_text())
            coef = [float(c) for c in data["coef"]]
            if len(coef) != 6 or not all(math.isfinite(c) for c in coef):
                raise ValueError("coef musí být 6 konečných čísel")
        except FileNotFoundError:
            log.info("kalibrace dotyku %s neexistuje — používám výchozí", path)
            cal = cls.default()
            cal.source = "default"
            return cal
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            log.warning("kalibrace dotyku %s je poškozená (%s) — používám výchozí", path, exc)
            cal = cls.default()
            cal.source = "default:corrupt"
            return cal
        grid = data.get("grid") if isinstance(data, dict) else None
        source = f"file:{path}"
        if grid:
            why = grid_problem(grid)
            if why:
                log.warning("mřížka kalibrace dotyku %s je vadná (%s) — beru jen afinní část", path, why)
                grid, source = None, f"file:{path}:no-grid"
        cal = cls(*coef, grid=grid or None)
        cal.source = source
        return cal

    def save(self, path: Path = CALIBRATION) -> None:
        """Atomicky: dočasný soubor, fsync, přejmenování — výpadek proudu nenechá půl souboru."""
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict = {"coef": list(self.coef)}
        if self.grid:
            data["grid"] = self.grid
        tmp = path.with_name(f".{path.name}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(data) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        try:  # the rename itself on disk too
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass

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


def grid_problem(grid: object) -> str:
    """"" when the grid is usable: 3 strictly increasing xs and ys, 9 pairs of finite numbers."""
    if not isinstance(grid, dict):
        return "není slovník"
    xs, ys, d = grid.get("xs"), grid.get("ys"), grid.get("d")
    for name, axis in (("xs", xs), ("ys", ys)):
        if not isinstance(axis, list) or len(axis) != 3:
            return f"{name} nejsou 3 čísla"
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in axis):
            return f"{name} nejsou čísla"
        if not axis[0] < axis[1] < axis[2]:
            return f"{name} nerostou"
    if not isinstance(d, list) or len(d) != 9:
        return "d nemá 9 dvojic"
    for v in d:
        if not (isinstance(v, (list, tuple)) and len(v) == 2
                and all(isinstance(c, (int, float)) and not isinstance(c, bool) and math.isfinite(c) for c in v)):
            return "d nejsou dvojice čísel"
    return ""


MAX_GRID = 35  # px — větší zbytek v jednom z 9 bodů = sklouzlý prst, nic se neuloží


def recalibrate(old: Calibration, pairs: list[tuple[tuple[int, int], tuple[int, int]]]) -> tuple[Calibration, dict]:
    """Nová kalibrace z párů (kam dotyk padl se starou kalibrací, kde byl křížek).

    Nejdřív afinní oprava navrch té staré (5 i 9 bodů); z 9 bodů v mřížce 3×3
    navíc mřížka zbytků, takže sedí i tam, kde se vrstva prohýbá jinak.
    ValueError, když body nedávají smysl — pak se nic nemění.
    """
    # co naměřila stará kalibrace bez své mřížky (mřížka je hladká: stačí odečíst)
    base = [((m[0] - old.offset(*m)[0], m[1] - old.offset(*m)[1]), t) for m, t in pairs]
    corr, affine_err = screen_correction(base)
    affine = Calibration(*old.coef).then(corr)
    txs = sorted({t[0] for _, t in pairs})
    tys = sorted({t[1] for _, t in pairs})
    info = {"points": len(pairs), "affine_error": round(affine_err, 1)}
    if len(pairs) < 9 or len(txs) != 3 or len(tys) != 3:
        if affine_err > MAX_CAL_ERROR:
            raise ValueError(f"odchylka {affine_err:.0f} px")
        info["error"] = round(affine_err, 1)
        return affine, info
    # zbytky po afinní části v uzlech mřížky; pár kol, ať sedí i s interpolací
    after = [(corr.affine(*m), t) for m, t in base]
    d = [[0.0, 0.0] for _ in range(9)]
    new = Calibration(*affine.coef, grid={"xs": txs, "ys": tys, "d": d})
    for _ in range(6):
        for (a, t) in after:
            k = tys.index(t[1]) * 3 + txs.index(t[0])
            ox, oy = new.offset(*a)
            d[k][0] += t[0] - (a[0] + ox)
            d[k][1] += t[1] - (a[1] + oy)
    worst = max(max(abs(v[0]), abs(v[1])) for v in d)
    if worst > MAX_GRID:
        raise ValueError(f"jeden bod je mimo o {worst:.0f} px")
    new.grid = {"xs": txs, "ys": tys, "d": [[round(v[0], 2), round(v[1], 2)] for v in d]}
    err = 0.0
    for a, t in after:
        ox, oy = new.offset(*a)
        err = max(err, abs(a[0] + ox - t[0]), abs(a[1] + oy - t[1]))
    info.update(error=round(err, 1), grid_max=round(worst, 1))
    return new, info


INVALID_REASONS = {-1: "invalid_lifted", -2: "invalid_rest", -3: "invalid_spread"}


def touch_resistance(v: tuple) -> float | None:
    """Odpor dotyku v poměrných jednotkách (XPT2046: Rx·x/4096·(z2/z1 − 1), bez Rx).

    Jeden prst a dva dotyky naráz (prst + rámeček) by se tu měly lišit —
    ověří se to z dat „Testu prstem"."""
    if len(v) < 4 or v[2] <= 0:
        return None
    return v[0] / 4096 * (v[3] / v[2] - 1)


def contact_filter_from_env(value: str | None = None):
    """YTDJ_PANEL_CONTACT_R="0.05:0.9" → vzorky s odporem mimo rozsah se zahodí. Bez proměnné nic."""
    value = os.environ.get("YTDJ_PANEL_CONTACT_R", "") if value is None else value
    if not value:
        return None
    try:
        lo, hi = (float(p) for p in value.split(":"))
    except ValueError:
        log.warning("YTDJ_PANEL_CONTACT_R=%r nerozumím (čekám od:do) — filtr dvojího dotyku vypnutý", value)
        return None

    def ok(v: tuple) -> bool:
        r = touch_resistance(v)
        return r is None or lo <= r <= hi
    log.info("filtr dvojího dotyku zapnutý: odpor %s–%s", lo, hi)
    return ok


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
    # Poslední vzorky před zvednutím ujíždějí (tlak klesá). Pohyb se proto
    # hlásí o LIFT_SKIP vzorků (~30 ms) později a ty poslední se zahodí.
    LIFT_SKIP = 2

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
        self._pending: list[tuple[int, int]] = []  # čerstvé vzorky, které se ještě nehlásí (LIFT_SKIP)
        self._noisy = 0  # vadné vzorky za sebou při držení
        self._jump: tuple[int, int] | None = None
        # Anomálie převodníku za poslední souhrn (panel.touch_driver). Jen
        # přičítání v tomhle vlákně; `take_stats()` z hlavního vlákna vymění
        # celý slovník — případná ztráta jednoho přičtení nevadí.
        self._stats: dict[str, int] = {}
        # „Test prstem": všechny vzorky stisku (None = nenahrává se)
        self.recording: list[dict] | None = None
        self._rec_t0 = 0.0
        # Připravený, VYPNUTÝ filtr dvojího dotyku (prst + rámeček na horním
        # okraji): funkce (x, y, z1, z2, sx, sy) → vzorek platí? Zapne se jen
        # podle dat z panel.touch_probe — YTDJ_PANEL_CONTACT_R="od:do".
        self.contact_filter = contact_filter_from_env()

    def apply_correction(self, pairs: list[tuple[tuple[int, int], tuple[int, int]]],
                         path: Path | None = None) -> float:
        """Kalibrace z panelu: páry (kam dotyk padl, kde byl křížek) v pixelech
        obrazovky. Opraví a uloží kalibraci, platí hned; vrátí největší zbytek (px).
        ValueError, když body nesedí — pak se nic nemění."""
        if self._rotate == 180:
            # kalibrace platí před otočením obrazu: body do jejích souřadnic
            pairs = [((WIDTH - 1 - m[0], HEIGHT - 1 - m[1]), (WIDTH - 1 - t[0], HEIGHT - 1 - t[1]))
                     for m, t in pairs]
        new, info = recalibrate(self._cal, pairs)
        target = path or CALIBRATION
        new.save(target)
        new.source = f"file:{target}"
        self._cal = new
        self.last_calibration = info
        log.info("kalibrace dotyku uložena do %s (%s)", target, info)
        return info["error"]

    @property
    def calibration_source(self) -> str:
        return getattr(self._cal, "source", "custom")

    def _bump(self, key: str) -> None:
        st = self._stats
        st[key] = st.get(key, 0) + 1

    def take_stats(self) -> dict[str, int]:
        st, self._stats = self._stats, {}
        return st

    def _raw(self) -> tuple[tuple[int, int, int] | None, int, tuple[int, ...]]:
        """(x, y, z) nebo None; kód převodníku a surové hodnoty (když je ovladač umí dát)."""
        ex = getattr(self._dev, "touch_ex", None)
        if ex is None:
            raw = self._dev.touch_raw()
            return raw, (1 if raw else 0), ()
        code, v = ex()
        if code == 1:
            return (v[0], v[1], v[2] + 4095 - v[3]), code, v
        return None, code, v

    def _sample(self) -> tuple[int, int] | str:
        """Poloha (x, y), nebo "noisy" (prst tam je, vzorek ale nepoužitelný), nebo "up"."""
        raw, code, v = self._raw()
        if raw is None:
            # PENIRQ hlásí prst, ale převodník nedal použitelné vzorky
            # (zvedl se uprostřed měření, klidové hodnoty, rozptyl)
            if self._dev.pen_down():
                self._bump("invalid_samples")
                if code in INVALID_REASONS:
                    self._bump(INVALID_REASONS[code])  # proč: invalid_lifted / _rest / _spread
                self._note(code, v, None)
                return "noisy"
            if code == -1:
                self._note(code, v, None)
            return "up"
        if raw[2] < MIN_PRESSURE:
            self._bump("low_pressure")
            self._note(-4, v, None)
            return "noisy"
        x, y = self._cal.map(raw[0], raw[1])
        if self._rotate == 180:
            x, y = WIDTH - 1 - x, HEIGHT - 1 - y
        if self.contact_filter is not None and v and not self.contact_filter(v):
            # (připraveno, vypnuté) vzorek s podpisem dvou dotyků / rámečku
            self._bump("dual_rejected")
            self._note(-5, v, (x, y))
            return "noisy"
        self._note(1, v or (raw[0], raw[1]), (x, y))
        return x, y

    def _note(self, code: int, v: tuple, pos: tuple[int, int] | None) -> None:
        """Záznam vzorků pro „Test prstem" (panel.touch_probe) — jen když běží."""
        rec = self.recording
        if rec is None or len(rec) >= 400:
            return
        now = time.monotonic()
        if not rec:
            self._rec_t0 = now
        item = {"t": int((now - self._rec_t0) * 1000), "c": code}
        if v:
            item["raw"] = list(v)
        if pos is not None:
            item["x"], item["y"] = pos
        rec.append(item)

    def start_recording(self) -> None:
        self.recording = []

    def take_recording(self) -> list[dict]:
        """Vzorky od posledního vybrání (a nahrávání běží dál)."""
        rec, self.recording = self.recording or [], ([] if self.recording is not None else None)
        return rec

    def stop_recording(self) -> None:
        self.recording = None

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
                if self._down:
                    self._pending.append(pos)
                    if len(self._pending) > self.LIFT_SKIP:
                        ev = self._holding(self._pending.pop(0))
                else:
                    ev = self._landing(pos)
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
        self._pending = []  # vzorky ze zvedání prstu se nehlásí
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
