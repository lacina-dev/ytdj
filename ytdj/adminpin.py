"""PIN správce: nastavení a restart z webu jen s ním (F-BEZP-09, F-BEZP-10).

Šest náhodných číslic v ``~/.config/ytdj/admin-pin`` (vedle config.toml,
práva 0600). Kdo stojí u jukeboxu, uvidí ho na displeji na obrazovce Síť
(panel ten soubor čte sám — F-BEZP-11); kolegové v síti ho neznají, takže
hudbu ovládají dál, ale adresu webu, cookies ani restart ne.

Modul je schválně bez závislostí na webu: čte ho i proces panelu.
Nic tady nepíše PIN do logu.
"""

from __future__ import annotations

import hmac
import os
import secrets
import tempfile
import time
from pathlib import Path

from .config import CONFIG_DIR

PIN_FILE = CONFIG_DIR / "admin-pin"
PIN_DIGITS = 6
HEADER = "x-ytdj-pin"

# Brzda hádání: po 5 špatných PINech (od kohokoli) se správa na 5 min zavře.
MAX_FAILURES = 5
LOCK_SECONDS = 300


def _valid(pin: str) -> bool:
    return len(pin) == PIN_DIGITS and pin.isascii() and pin.isdigit()


def read_pin(path: Path | None = None) -> str:
    """PIN ze souboru, nebo "" (soubor chybí, je nečitelný nebo rozbitý)."""
    try:
        pin = Path(path or PIN_FILE).read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return ""
    return pin if _valid(pin) else ""


def ensure_pin(path: Path | None = None) -> str:
    """Vrátí PIN; když ještě není (nebo je rozbitý), vytvoří nový.

    Zápis je atomický (dočasný soubor 0600 ve stejné složce + rename), takže
    panel nikdy nepřečte napůl zapsaný soubor. Blokuje — z hlavní smyčky jen
    přes ``asyncio.to_thread``.
    """
    path = Path(path or PIN_FILE)
    pin = read_pin(path)
    if pin:
        return pin
    pin = f"{secrets.randbelow(10 ** PIN_DIGITS):0{PIN_DIGITS}d}"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".admin-pin.", dir=path.parent)  # mkstemp = 0600
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(pin + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return pin


class AdminGuard:
    """Rozhodne o jednom požadavku na správu; drží počítadlo špatných PINů.

    ``check`` vrací ``(stav, sekundy_zámku)``:
    ``"ok"`` · ``"missing"`` (PIN nepřišel) · ``"wrong"`` · ``"locked"``
    (zavřeno) · ``"lockout"`` (tenhle pokus to právě zavřel).
    Chybějící PIN se nepočítá jako pokus — stránka se tak nejdřív zeptá,
    jestli PIN vůbec potřebuje.
    """

    def __init__(self, path: Path | None = None, clock=time.monotonic) -> None:
        self.path = Path(path or PIN_FILE)
        self.clock = clock
        self.failures = 0
        self.locked_until = 0.0

    def remaining(self) -> int:
        left = self.locked_until - self.clock()
        return int(left + 0.999) if left > 0 else 0

    def check(self, given: str | None, pin: str) -> tuple[str, int]:
        left = self.remaining()
        if left:
            return "locked", left
        if self.locked_until:  # zámek vypršel — počítá se od nuly
            self.locked_until = 0.0
            self.failures = 0
        given = (given or "").strip()
        if not given:
            return "missing", 0
        if pin and hmac.compare_digest(given.encode(), pin.encode()):
            self.failures = 0
            return "ok", 0
        self.failures += 1
        if self.failures >= MAX_FAILURES:
            self.locked_until = self.clock() + LOCK_SECONDS
            return "lockout", LOCK_SECONDS  # právě se zavřelo (jinak jako "locked")
        return "wrong", 0
