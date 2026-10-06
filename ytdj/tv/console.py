"""Keeping the text console off the TV.

The firmware framebuffer is also where the kernel's text console (fbcon)
draws: a login prompt, a blinking cursor, kernel messages. The jukebox screen
must be the only thing on the TV, so while it runs the virtual terminal is
switched to graphics mode (KD_GRAPHICS) — the same thing a display server
does. In that mode fbcon draws nothing at all: no prompt, no cursor, no
messages over the picture.

No root is needed for it: systemd hands the service /dev/tty1 as its
controlling terminal (`TTYPath=` + `StandardInput=tty-force` in the unit) and
the kernel lets the terminal's own session change its mode. Run by hand or in
tests there is no such terminal, and nothing happens.
"""

from __future__ import annotations

import fcntl
import logging
import os

log = logging.getLogger(__name__)

KDSETMODE = 0x4B3A
KD_TEXT = 0
KD_GRAPHICS = 1


def grab(fd: int = 0) -> tuple[bool, str]:
    """Switches the terminal on `fd` to graphics mode: (done?, why not)."""
    try:
        if not os.isatty(fd):
            return False, "není terminál"
        fcntl.ioctl(fd, KDSETMODE, KD_GRAPHICS)
    except OSError as exc:
        # a pseudo-terminal (ssh), or a VT that isn't ours
        return False, exc.strerror or str(exc)
    return True, ""


def release(fd: int = 0) -> None:
    """Back to text mode, so a console works again when the service is stopped."""
    try:
        fcntl.ioctl(fd, KDSETMODE, KD_TEXT)
    except OSError:
        log.debug("konzoli se nepodařilo vrátit do textového režimu", exc_info=True)
