"""Proč to hraje zrovna takhle — diagnostika zvukové cesty.

Degradace na 128 kb/s je němá: yt-dlp nabídne, co dostane, mpv zahraje
nejlepší z toho a nikdo se nic nedozví. Tenhle modul pustí přesně ty
argumenty, které dostává mpv, a přeloží výsledek do vět.
"""

from __future__ import annotations

import asyncio
import re

from .config import Config

# 774 = Opus 256k, 141 = AAC 256k. Obojí jen pro Premium účty.
PREMIUM_ITAGS = ("774", "141")
TEST_TRACK = "https://music.youtube.com/watch?v=ljUtuoFt-8c"

_FORMAT_ROW = re.compile(r"^(\d+)\s+(\w+)\s+audio only.*?(\d+)k\s", re.M)
_COOKIE_COUNT = re.compile(r"Extracted (\d+) cookies", re.I)


def yt_dlp_args(cfg: Config) -> list[str]:
    """Totéž, co dostane yt-dlp přes ytdl-raw-options z mpv."""
    args = [cfg.yt_dlp_path]
    if cfg.cookies_file:
        args += ["--cookies", cfg.cookies_file]
    elif cfg.cookies_browser and cfg.cookies_browser != "none":
        args += ["--cookies-from-browser", cfg.cookies_browser]
    if cfg.js_runtimes:
        args += ["--js-runtimes", cfg.js_runtimes]
    if cfg.remote_components:
        args += ["--remote-components", cfg.remote_components]
    if extractor := cfg.extractor_args():
        args += ["--extractor-args", extractor]
    return args


async def check_audio(cfg: Config, url: str = TEST_TRACK) -> str:
    """Vrátí čitelnou zprávu o tom, co je k dispozici a co tomu chybí."""
    args = yt_dlp_args(cfg) + ["-F", url]
    proc = await asyncio.create_subprocess_exec(
        *args,
        env=cfg.child_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    raw, _ = await proc.communicate()
    out = raw.decode(errors="replace")

    lines = [f"zdroj cookies:  {cfg.cookie_source()}"]
    if m := _COOKIE_COUNT.search(out):
        count = int(m.group(1))
        lines.append(f"načtené cookies: {count}")
        if count == 0:
            lines.append("  → profil je odhlášený nebo prázdný; jede se anonymně")
    lines.append(f"klient:         {cfg.player_client or '(výběr yt-dlp)'}")
    lines.append(f"PO token:       {'z configu' if cfg.po_token else 'z provideru nebo žádný'}")

    formats = _FORMAT_ROW.findall(out)
    if not formats:
        lines.append("\nyt-dlp nenabídl žádný zvukový formát:")
        lines += [f"  {l}" for l in out.splitlines() if "ERROR" in l][:3]
        return "\n".join(lines)

    best = max(int(b) for _, _, b in formats)
    lines.append("\nnabízené zvukové formáty:")
    lines += [f"  {itag:>4}  {ext:<5} {bitrate:>4} kb/s" for itag, ext, bitrate in formats]

    premium = [f for f in formats if f[0] in PREMIUM_ITAGS]
    if premium:
        lines.append(f"\nPremium JE k dispozici ({', '.join(f[0] for f in premium)}).")
        if not any(i in cfg.ytdl_format for i, _, _ in premium):
            lines.append("Ale ytdl_format je nemá na seznamu — doplň je.")
    else:
        lines.append(f"\nPremium NENÍ k dispozici, strop je {best} kb/s. Důvod:")
        lines += _why_no_premium(out, cfg)
    return "\n".join(lines)


def _why_no_premium(out: str, cfg: Config) -> list[str]:
    """Varování yt-dlp přeložená do toho, co s tím dělat."""
    why = []
    if "PO Token" in out and "not provided" in out:
        why.append(
            "  • klient s přihlášením nedostal PO token — nastartuj provider:\n"
            "    systemctl --user start ytdj-pot   (nebo vyplň YTDJ_PO_TOKEN v ~/.config/ytdj/env)"
        )
    if "SABR" in out:
        why.append(
            "  • účet je v experimentu SABR-only: YouTube posílá formáty bez URL\n"
            "    a yt-dlp je neumí stáhnout. Tady nepomůže nastavení, jen novější yt-dlp."
        )
    if "does not support cookies" in out:
        why.append(
            "  • zvolený klient cookies vůbec nenese, takže se účet neuplatní"
        )
    if not cfg.cookies_file and cfg.cookies_browser in ("", "none"):
        why.append("  • nepoužívají se žádné cookies, takže účet nemá jak vstoupit do hry")
    if not why:
        why.append(
            "  • cookies i token prošly, a YouTube přesto Premium formáty nenabídl\n"
            "    → ten účet Premium nejspíš nemá (nebo ne na tuhle skladbu)"
        )
    return why
