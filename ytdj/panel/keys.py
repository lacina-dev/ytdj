"""Multimediální klávesy (tlačítka na USB repráku, klávesnice) → povely panelu.

USB sound bary a podobně posílají hlasitost jako klávesy KEY_VOLUMEUP/DOWN
přes vlastní HID zařízení a do mixéru samy nesahají. Bez plochy je nikdo
nezpracuje, takže je bere panel a jde s nimi stejnou cestou jako s dotykem —
hlasitost na displeji a v přehrávači je pak pořád ta samá.

Čte /dev/input/event* přímo (bez python-evdev), potřebuje k nim přístup
(root nebo skupina input). Zařízení se hledají znovu každých pár sekund,
takže odpojený a znovu zapojený repro se chytí sám.
"""

from __future__ import annotations

import errno
import logging
import os
import select
import struct
import threading
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

EV_KEY = 1
KEY_MUTE, KEY_VOLUMEDOWN, KEY_VOLUMEUP = 113, 114, 115
KEY_NEXTSONG, KEY_PLAYPAUSE = 163, 164
ACTIONS = {
    KEY_VOLUMEUP: "vol_up",
    KEY_VOLUMEDOWN: "vol_down",
    KEY_MUTE: "play",       # ztlumit = pozastavit; ticho, které jde vrátit
    KEY_PLAYPAUSE: "play",
    KEY_NEXTSONG: "next",
}

EVENT = struct.Struct("llHHi")  # struct input_event na 64bitovém kernelu
RESCAN = 5.0


def _devices() -> list[tuple[str, str]]:
    """(cesta, jméno) zařízení, která umí aspoň jednu z našich kláves."""
    found = []
    try:
        blocks = Path("/proc/bus/input/devices").read_text().split("\n\n")
    except OSError:
        return found
    for block in blocks:
        name, handlers, keybits = "", [], 0
        for line in block.splitlines():
            if line.startswith("N: Name="):
                name = line[8:].strip('"')
            elif line.startswith("H: Handlers="):
                handlers = line[12:].split()
            elif line.startswith("B: KEY="):
                words = line[7:].split()
                # nejvyšší slovo první, každé 64 bitů
                for i, word in enumerate(reversed(words)):
                    keybits |= int(word, 16) << (64 * i)
        ev = next((h for h in handlers if h.startswith("event")), None)
        if ev and any(keybits >> code & 1 for code in ACTIONS):
            found.append((f"/dev/input/{ev}", name))
    return found


class MediaKeys(threading.Thread):
    def __init__(self, on_action: Callable[[str], None], stop: threading.Event) -> None:
        super().__init__(name="panel-keys", daemon=True)
        self.on_action = on_action
        self.stop = stop
        self.open: dict[int, tuple[str, str]] = {}  # fd → (cesta, jméno)

    def _rescan(self) -> None:
        paths = {p for p, _ in self.open.values()}
        for path, name in _devices():
            if path in paths:
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError as exc:
                log.warning("klávesy: %s (%s) nejde otevřít: %s", name, path, exc)
                continue
            self.open[fd] = (path, name)
            log.info("klávesy: poslouchám %s", name)

    def _drop(self, fd: int) -> None:
        path, name = self.open.pop(fd)
        os.close(fd)
        log.info("klávesy: %s odpojeno", name)

    def run(self) -> None:
        while not self.stop.is_set():
            self._rescan()
            if not self.open:
                self.stop.wait(RESCAN)
                continue
            ready, _, _ = select.select(list(self.open), [], [], RESCAN)
            for fd in ready:
                try:
                    data = os.read(fd, EVENT.size * 32)
                except OSError as exc:
                    if exc.errno in (errno.ENODEV, errno.EIO):
                        self._drop(fd)
                        continue
                    if exc.errno == errno.EAGAIN:
                        continue
                    raise
                for off in range(0, len(data) - EVENT.size + 1, EVENT.size):
                    _, _, typ, code, value = EVENT.unpack_from(data, off)
                    if typ != EV_KEY or code not in ACTIONS:
                        continue
                    action = ACTIONS[code]
                    # 1 = stisk, 2 = autorepeat při držení (jen hlasitost —
                    # držená pauza nemá blikat), 0 = puštění ignorujeme
                    if value == 1 or (value == 2 and action.startswith("vol_")):
                        self.on_action(action)
