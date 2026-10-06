""""Now playing" on a TV over HDMI — `python -m ytdj.tv`.

A separate small process: it reads the jukebox's state over the local HTTP
API and draws to the Linux framebuffer (/dev/fb0). It never touches the
player or the sound, and the jukebox doesn't notice whether it runs.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time

from ..panel.__main__ import _config, _default_url
from ..panel.stats import emit

DEFAULT_NAME = "jukebox.local"


def default_address(cfg: dict) -> str:
    """What colleagues type into a browser: jukebox.local and the web's port."""
    port = cfg.get("web_port")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        port = 8765
    return DEFAULT_NAME if port == 80 else f"{DEFAULT_NAME}:{port}"


def _size(text: str) -> tuple[int, int]:
    w, _, h = text.lower().partition("x")
    return int(w), int(h)


def main(argv: list[str] | None = None) -> int:
    cfg = _config()
    p = argparse.ArgumentParser(prog="python -m ytdj.tv", description=__doc__.splitlines()[0])
    p.add_argument("--url", default=_default_url(cfg), help="ytdj web API (default: %(default)s)")
    p.add_argument("--fb", default="/dev/fb0", help="framebuffer device (default: %(default)s)")
    p.add_argument("--address", default=os.environ.get("YTDJ_TV_ADDRESS") or default_address(cfg),
                   help="the web address shown with the QR code (default: %(default)s)")
    p.add_argument("--sim-out", help="draw to this PNG instead of the framebuffer")
    p.add_argument("--sim-fb", help="draw RGB565 to this plain file as if it were a framebuffer "
                                    "(measuring the real drawing path without hardware)")
    p.add_argument("--size", type=_size, default=(1280, 720), help="WxH for --sim-out / --sim-fb")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else os.environ.get("YTDJ_LOG", "INFO").upper(),
        format="%(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("ytdj.tv")
    try:
        from .app import TvApp
        from .fb import FbInfo, Framebuffer, PngScreen
    except ImportError as exc:  # Pillow is an optional extra
        log.error("obrazovka potřebuje Pillow (apt install python3-pil): %s", exc)
        return 2

    if args.sim_out:
        def open_screen():
            return PngScreen(args.sim_out, args.size)
    elif args.sim_fb:
        w, h = args.size
        with open(args.sim_fb, "wb") as fh:
            fh.truncate(w * h * 2)

        def open_screen():
            return Framebuffer(args.sim_fb, info=FbInfo(w, h, 16, w * 2, (11, 5), (5, 6), (0, 5)))
    else:
        def open_screen():
            return Framebuffer(args.fb)

    app = TvApp(open_screen, args.url, args.address)
    signal.signal(signal.SIGTERM, lambda *_: app.shutdown())
    signal.signal(signal.SIGINT, lambda *_: app.shutdown())
    log.info("obrazovka „právě hraje“ běží (%s), ytdj na %s",
             args.sim_out or args.sim_fb or args.fb, args.url)
    emit("tv.startup", fb=None if args.sim_out or args.sim_fb else args.fb,
         sim=bool(args.sim_out or args.sim_fb) or None,
         url=args.url, address=args.address, pid=os.getpid())
    started = time.monotonic()
    try:
        app.run()
    finally:
        emit("tv.stop", uptime_s=int(time.monotonic() - started), reconnects=app.reconnects)
        app._drop_screen()
    return 0


if __name__ == "__main__":
    sys.exit(main())
