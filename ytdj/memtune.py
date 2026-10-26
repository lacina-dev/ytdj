"""Paměť procesů v Pythonu na Pi: glibc malloc bez „děravých“ arén.

Pi 26. 10. 2026 (smaps): hlavní ytdj měl 381 MB anonymní paměti, z toho
289 MB ve 12 arénách glibc (každé vlákno, které alokuje, dostane vlastní,
až 8 × počet jader); resolver 125 MB z 233 MB v 5 arénách. Většina z toho
je uvolněná paměť, kterou glibc systému nevrátí: velké bloky (odpovědi
YouTube Music, JSON yt-dlp) jdou nejdřív přes mmap, ale po jejich uvolnění
si glibc práh mmap sama zvedne (až na 32 MB) — další se pak berou z arén
a po uvolnění v nich zůstanou díry.

Změřeno na PC (ytmusicapi jako katalog, 8 vláken, 3 × 20 dotazů):
výchozí 246–259 MB, s pevným prahem 128 kB a 2 arénami 47–50 MB, živá
data po malloc_trim 45–48 MB v obou případech; CPU +0,6 s systémového
času na 180 požadavků. yt-dlp jako resolver (4 vlákna, 40 skladeb):
99 → 62 MB po práci, špička 111 → 62 MB.

`tune_malloc()` nastaví totéž za běhu (mallopt) — volá se jako první věc
v procesu, dřív než vznikne vlákno. `malloc_env()` přidá proměnné
prostředí pro potomka v Pythonu (resolver), kterému glibc nastaví totéž při
startu. Hudební mpv se to netýká (u něj to změřeno nepomohlo).
`YTDJ_MALLOC=0` vypne obojí.
"""

from __future__ import annotations

import os

ARENA_MAX = 2
MMAP_THRESHOLD = 128 * 1024  # B — pevný práh: velké bloky vždy přes mmap (vrací se hned)
# mallopt(3): M_ARENA_MAX = -8, M_MMAP_THRESHOLD = -3
_M_ARENA_MAX, _M_MMAP_THRESHOLD = -8, -3
ENV = {"MALLOC_ARENA_MAX": str(ARENA_MAX), "MALLOC_MMAP_THRESHOLD_": str(MMAP_THRESHOLD)}


def enabled() -> bool:
    return os.environ.get("YTDJ_MALLOC", "1") != "0"


def tune_malloc(libc=None) -> bool:
    """Nastaví glibc tomuto procesu; False tam, kde to nejde (jiná libc)."""
    if not enabled():
        return False
    try:
        if libc is None:
            import ctypes

            libc = ctypes.CDLL("libc.so.6")
        return bool(libc.mallopt(_M_ARENA_MAX, ARENA_MAX)) and \
            bool(libc.mallopt(_M_MMAP_THRESHOLD, MMAP_THRESHOLD))
    except (OSError, AttributeError):
        return False


def malloc_env(env: dict[str, str]) -> dict[str, str]:
    """Prostředí pro potomka v Pythonu; vlastní nastavení (už v env) vyhrává."""
    if enabled():
        for k, v in ENV.items():
            env.setdefault(k, v)
    return env
