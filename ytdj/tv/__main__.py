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

def _size(text: str) -> tuple[int, int]:
    w, _, h = text.lower().partition("x")
    return int(w), int(h)


def main(argv: list[str] | None = None) -> int:
    cfg = _config()
    p = argparse.ArgumentParser(prog="python -m ytdj.tv", description=__doc__.splitlines()[0])
    p.add_argument("--url", default=_default_url(cfg), help="ytdj web API (default: %(default)s)")
    p.add_argument("--fb", default="/dev/fb0", help="framebuffer device (default: %(default)s)")
    p.add_argument("--address", default=os.environ.get("YTDJ_TV_ADDRESS") or "",
                   help="a fixed web address to show with the QR code; by default the screen "
                        "shows the one that really answers (the alias, <hostname>.local "
                        "or the IP — without a port when port 80 is served)")
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

    director = None
    if not (args.sim_out or args.sim_fb):
        # Klipy (POZADAVKY #71): jen na skutečné telce. Jestli to stroj umí
        # (KMS, HDMI, dekodér), zjišťuje se za běhu — nic se nepředpokládá.
        from .video import Director

        uid_dir = os.environ.get("YTDJ_TV_VIDEO_DIR") or f"/run/user/{os.getuid()}/ytdj/tv-video"
        run_dir = os.environ.get("RUNTIME_DIRECTORY", "/run/ytdj-tv").split(":")[0]

        def fb_size():
            try:
                with open("/sys/class/graphics/fb0/virtual_size") as fh:
                    w, h = fh.read().strip().split(",")
                return int(w), int(h)
            except (OSError, ValueError):
                return None

        from pathlib import Path

        director = Director(Path(uid_dir), os.path.join(run_dir, "video.sock"), fb_size, emit=emit,
                            status_file=Path(run_dir) / "status.json")
    # web_name v config.toml: jméno z DNS kanceláře (jen ze souboru; ověří se jako ostatní)
    power = None
    if director is not None:
        # Vypínání telky, když se nehraje (HDMI-CEC) — řídí se nastavením jukeboxu,
        # které sem chodí ve stavu; samo od sebe nic neposílá.
        from .cec import Cec, Listener, TvPower

        logs = os.environ.get("LOGS_DIRECTORY", "").split(":")[0]
        cec = Cec()
        # co telka ukazuje, se pozná poslechem sběrnice (dotaz nefunguje, viz cec.py)
        power = TvPower(cec, emit=emit,
                        state_file=os.path.join(logs, "cec-state.json") if logs else None,
                        listener=Listener(cec.listen_command, answer=cec.answer))
    app = TvApp(open_screen, args.url, args.address, director=director,
                web_name=str(cfg.get("web_name") or ""), power=power)
    signal.signal(signal.SIGTERM, lambda *_: app.shutdown())
    signal.signal(signal.SIGINT, lambda *_: app.shutdown())
    log.info("obrazovka „právě hraje“ běží (%s), ytdj na %s",
             args.sim_out or args.sim_fb or args.fb, args.url)
    emit("tv.startup", fb=None if args.sim_out or args.sim_fb else args.fb,
         sim=bool(args.sim_out or args.sim_fb) or None,
         url=args.url, address=args.address or "auto", pid=os.getpid())
    # Na telce má být jen jukebox: textová konzole (login, kurzor, hlášky jádra)
    # kreslí do téhož framebufferu, tak ji po dobu běhu přepneme do grafického
    # režimu. Jde to jen s terminálem od systemd (TTYPath v unitě), jinak nic.
    grabbed = False
    if not (args.sim_out or args.sim_fb):
        from . import console

        signal.signal(signal.SIGHUP, signal.SIG_IGN)  # zavěšení terminálu nás neukončí
        grabbed, why = console.grab()
        if grabbed:
            log.info("textová konzole je po dobu běhu skrytá")
        else:
            log.info("textovou konzoli neskrývám (%s) — může být vidět na telce", why)
        emit("tv.console", hidden=grabbed, reason=why or None)
    started = time.monotonic()
    try:
        app.run()
    finally:
        emit("tv.stop", uptime_s=int(time.monotonic() - started), reconnects=app.reconnects)
        if director is not None:
            director.close()
        app._drop_screen()
        if grabbed:
            console.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
