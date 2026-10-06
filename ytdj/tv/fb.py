"""The Linux framebuffer (/dev/fb0) as a screen — whatever size and depth it reports.

On the jukebox this is the firmware framebuffer of the Raspberry Pi (HDMI or
composite, sized at boot). Nothing here assumes a TV is attached: the device
is probed, and a framebuffer that is missing, tiny or in a pixel format we
don't know is reported as unusable instead of being drawn to.

Pixels are packed with Pillow alone (no numpy: it would cost ~20 MB of RAM on
a 1 GB Pi): RGB565/BGR565 through four lookup tables in C, 24- and 32-bit
through Pillow's raw packers.
"""

from __future__ import annotations

import fcntl
import logging
import mmap
import os
import struct
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops

from ..panel.hw import Box

log = logging.getLogger(__name__)

FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602
# below this there is nothing to lay out (and it is most likely not a display)
MIN_W, MIN_H = 320, 200
STRIP_PX = 96 * 1024  # pixels packed at a time (memory, not speed)


class FbError(Exception):
    """The framebuffer cannot be used (missing, no permission, unknown format)."""


@dataclass(frozen=True)
class FbInfo:
    width: int
    height: int
    bpp: int
    stride: int  # bytes per line
    red: tuple[int, int]  # bit offset, length
    green: tuple[int, int]
    blue: tuple[int, int]

    @property
    def fmt(self) -> str:
        """Our name for the pixel layout, "" when we can't pack it."""
        r, g, b = self.red, self.green, self.blue
        if self.bpp == 16 and (r[1], g[1], b[1]) == (5, 6, 5) and g[0] == 5:
            return {(11, 0): "RGB565", (0, 11): "BGR565"}.get((r[0], b[0]), "")
        if self.bpp in (24, 32) and (r[1], g[1], b[1]) == (8, 8, 8) and g[0] == 8:
            # little-endian: red at bit 16 means the bytes run B, G, R
            order = {(16, 0): "BGR", (0, 16): "RGB"}.get((r[0], b[0]), "")
            return order + ("X" if order and self.bpp == 32 else "")
        return ""

    def usable(self) -> str:
        """"" when we can draw here, otherwise why not."""
        if self.width < MIN_W or self.height < MIN_H:
            return f"moc malý ({self.width}×{self.height})"
        if not self.fmt:
            return (f"neznámý formát bodů ({self.bpp} bpp, R{self.red} G{self.green} "
                    f"B{self.blue})")
        if self.stride < self.width * self.bpp // 8:
            return f"řádek {self.stride} B je kratší než {self.width} bodů"
        return ""


def probe(fd: int) -> FbInfo:
    """Asks the kernel what the framebuffer looks like right now."""
    var = fcntl.ioctl(fd, FBIOGET_VSCREENINFO, bytes(160))
    (xres, yres, _xv, _yv, _xo, _yo, bpp, _gray,
     ro, rl, _rm, go, gl, _gm, bo, bl, _bm) = struct.unpack_from("17I", var)
    fix = fcntl.ioctl(fd, FBIOGET_FSCREENINFO, bytes(88))
    # id[16], smem_start, smem_len, type, type_aux, visual, x/y/ywrap step, line_length
    stride = struct.unpack_from("16sLIIIIHHHI", fix)[9]
    return FbInfo(xres, yres, bpp, stride or xres * bpp // 8, (ro, rl), (go, gl), (bo, bl))


_LUT_HI5 = [v & 0xF8 for v in range(256)]  # top 5 bits, in place
_LUT_LO5 = [v >> 3 for v in range(256)]  # top 5 bits, moved down
_LUT_G_HI = [v >> 5 for v in range(256)]  # green's top 3 bits → low bits of the high byte
_LUT_G_LO = [(v << 3) & 0xE0 for v in range(256)]  # green's next 3 → high bits of the low byte


def pack(img: Image.Image, fmt: str) -> bytes:
    """An RGB image as framebuffer bytes, row after row without padding."""
    if img.mode != "RGB":
        img = img.convert("RGB")
    if fmt in ("RGB565", "BGR565"):
        r, g, b = img.split()
        if fmt == "BGR565":
            r, b = b, r
        hi = ImageChops.add(r.point(_LUT_HI5), g.point(_LUT_G_HI))
        lo = ImageChops.add(g.point(_LUT_G_LO), b.point(_LUT_LO5))
        return Image.merge("LA", (lo, hi)).tobytes()  # little-endian 16-bit
    if fmt in ("BGRX", "RGBX", "BGR", "RGB"):
        return img.tobytes("raw", fmt)
    raise FbError(f"formát {fmt!r} neumím")


class Framebuffer:
    """Draws regions of an RGB image to the framebuffer through mmap."""

    def __init__(self, path: str | os.PathLike = "/dev/fb0", info: FbInfo | None = None) -> None:
        self.path = Path(path)
        try:
            self.fd = os.open(self.path, os.O_RDWR)
        except OSError as exc:
            raise FbError(f"{self.path}: {exc.strerror or exc}") from exc
        try:
            self.info = info if info is not None else probe(self.fd)
            why = self.info.usable()
            if why:
                raise FbError(f"{self.path}: {why}")
            self.fmt = self.info.fmt
            self.bytes_pp = self.info.bpp // 8
            self.mm = mmap.mmap(self.fd, self.info.stride * self.info.height)
        except FbError:
            os.close(self.fd)
            raise
        except (OSError, ValueError, struct.error) as exc:
            os.close(self.fd)
            raise FbError(f"{self.path}: {exc}") from exc
        self._fixed = info is not None

    @property
    def size(self) -> tuple[int, int]:
        return self.info.width, self.info.height

    def changed(self) -> bool:
        """Did the mode change under us (another resolution after a hotplug)?"""
        if self._fixed:
            return False
        try:
            return probe(self.fd) != self.info
        except OSError:
            return True

    def show(self, img: Image.Image, box: Box | None = None) -> None:
        w, h = self.size
        x0, y0, x1, y1 = box or (0, 0, w, h)
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
        if x1 <= x0 or y1 <= y0:
            return
        line = (x1 - x0) * self.bytes_pp
        stride = self.info.stride
        # in strips: packing a whole 1080p frame at once would hold ~25 MB of
        # temporary copies; a strip of STRIP_PX pixels holds well under 1 MB
        rows = max(1, STRIP_PX // (x1 - x0))
        for top in range(y0, y1, rows):
            bottom = min(y1, top + rows)
            data = pack(img.crop((x0, top, x1, bottom)), self.fmt)
            if line == stride:
                self.mm[top * stride:bottom * stride] = data
                continue
            view = memoryview(data)
            at = top * stride + x0 * self.bytes_pp
            for row in range(bottom - top):
                self.mm[at:at + line] = view[row * line:(row + 1) * line]
                at += stride

    def close(self) -> None:
        for close in (self.mm.close, lambda: os.close(self.fd)):
            try:
                close()
            except (OSError, ValueError, BufferError):
                log.debug("zavření framebufferu selhalo", exc_info=True)


class PngScreen:
    """A stand-in for development and tests: the "glass" is a PNG file."""

    def __init__(self, out: str | os.PathLike, size: tuple[int, int]) -> None:
        self.out = Path(out)
        self.size = size
        self.glass = Image.new("RGB", size)
        self.pushed_px = 0
        self.calls = 0

    def changed(self) -> bool:
        return False

    def show(self, img: Image.Image, box: Box | None = None) -> None:
        box = box or (0, 0, *self.size)
        self.glass.paste(img.crop(box), box[:2])
        self.pushed_px += (box[2] - box[0]) * (box[3] - box[1])
        self.calls += 1
        tmp = self.out.with_name(f".{self.out.name}.tmp")
        self.glass.save(tmp, "PNG")
        os.replace(tmp, self.out)

    def close(self) -> None:
        pass
