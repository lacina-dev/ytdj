"""Touch control panel for a running ytdj — `python -m ytdj.panel`.

A separate process: it talks to ytdj only over the local HTTP API, so it
survives ytdj restarting (and the other way round).
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
import tomllib

from .stats import emit


def _config() -> dict:
    """ytdj's own config.toml, for the port and the UI language — best effort."""
    try:
        from ..config import CONFIG_FILE

        with open(CONFIG_FILE, "rb") as fh:
            return tomllib.load(fh)
    except Exception:
        return {}


def _default_url(cfg: dict) -> str:
    host = str(cfg.get("web_host") or "127.0.0.1")
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"  # listening everywhere includes localhost
    port = cfg.get("web_port")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        port = 8765  # 0 = random port; nothing to guess there
    return f"http://{host}:{port}"


def main(argv: list[str] | None = None) -> int:
    cfg = _config()
    lang = str(cfg.get("language") or "cs").lower()
    p = argparse.ArgumentParser(prog="python -m ytdj.panel", description=__doc__.splitlines()[0])
    p.add_argument("--driver", choices=("kedei", "sim"), default="kedei")
    p.add_argument("--url", default=_default_url(cfg), help="ytdj web API (default: %(default)s)")
    p.add_argument("--rotate", type=int, choices=(0, 90, 180, 270), default=0)
    p.add_argument("--sim-out", default="/tmp/ytdj-panel.png", help="PNG for --driver sim")
    p.add_argument("--vol-max", type=int, default=100, help="volume ceiling for the panel (API allows up to 130)")
    p.add_argument("--lang", default="cs" if lang.startswith("cs") else "en", choices=("cs", "en"))
    p.add_argument("--no-media-keys", action="store_true",
                   help="ignore volume/play keys of USB speakers and keyboards (kedei only)")
    p.add_argument("-v", "--verbose", action="store_true", help="log timings of every redraw")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else os.environ.get("YTDJ_LOG", "INFO").upper(),
        format="%(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("ytdj.panel")

    try:
        from .app import PanelApp
    except ImportError as exc:  # Pillow is an optional extra
        log.error("panel potřebuje Pillow (pip install 'ytdj[panel]' nebo apt install python3-pil): %s", exc)
        return 2

    try:
        if args.driver == "kedei":
            from .kedei import KedeiScreen, KedeiTouch

            screen = KedeiScreen(rotate=args.rotate)
            touch = KedeiTouch(rotate=args.rotate)
        else:
            from .sim import SimScreen, SimTouch

            screen = SimScreen(args.sim_out)
            touch = SimTouch(sys.stdin)
    except Exception as exc:
        log.error("displej se nepodařilo otevřít (%s): %s", args.driver, exc)
        emit("panel.driver_error", where="open", driver=args.driver,
                        error=f"{type(exc).__name__}: {exc}"[:200])
        return 1

    if tuple(screen.size) != (480, 320):
        log.warning("displej hlásí %s, rozložení je pro 480×320 na šířku", screen.size)

    app = PanelApp(screen, touch, args.url, lang=args.lang, vol_max=args.vol_max,
                   media_keys=args.driver == "kedei" and not args.no_media_keys)
    signal.signal(signal.SIGTERM, lambda *_: app.shutdown())
    signal.signal(signal.SIGINT, lambda *_: app.shutdown())
    log.info("panel běží (%s), ytdj na %s", args.driver, args.url)
    emit(
        "panel.startup", driver=args.driver, rotate=args.rotate, size=list(screen.size),
        calibration=getattr(touch, "calibration_source", "n/a"), url=args.url, lang=args.lang,
        vol_max=app.vol_max, media_keys=app.media_keys, pid=os.getpid(),
    )
    started = time.monotonic()
    try:
        app.run()
    finally:
        emit("panel.stop", uptime_s=int(time.monotonic() - started),
                        frames=app.frames, reconnects=app.reconnects)
        app.close_screen()
        for dev in (touch, screen):
            try:
                dev.close()
            except Exception:
                log.debug("zavření zařízení selhalo", exc_info=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
