"""Hardware seam between the panel UI and a concrete screen + touch driver.

The UI renders into a Pillow image of `size` and hands it over, optionally
with the rectangle that changed — on slow SPI panels a partial update is what
makes the difference between snappy and sluggish. Touch arrives as screen
coordinates already calibrated to the same orientation the UI draws in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from PIL import Image

Box = tuple[int, int, int, int]  # left, top, right (exclusive), bottom (exclusive)


@dataclass(frozen=True)
class TouchEvent:
    kind: str  # "down" | "move" | "up"
    x: int
    y: int


class Screen(Protocol):
    size: tuple[int, int]  # (width, height) in the orientation the UI draws

    def show(self, img: Image.Image, box: Box | None = None) -> None:
        """Push `img` (RGB, exactly `size`) to the glass; only `box` if given."""

    def close(self) -> None: ...


class Touch(Protocol):
    def poll(self, timeout: float) -> TouchEvent | None:
        """Block up to `timeout` s for the next event; None if nothing happened."""

    def close(self) -> None: ...
