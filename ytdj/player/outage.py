"""Chyby přehrávání: vadná skladba, nebo výpadek (síť, YouTube, cookies)?

Dřív šla každá chyba mpv (end-file reason=error) natrvalo na černou listinu
jako "nepřehratelné". Při výpadku Wi-Fi nebo YouTube ale mpv během pár
vteřin "přehraje" s chybou celou frontu — všechno skončilo na černé listině
a přání jako "Skladbu se nepodařilo přehrát". Tady se rozhoduje, co je
vlastnost skladby (soukromá, smazaná, věk, region) a co jen dočasný stav, a
jak zjistit, že je spojení zpátky.
"""

from __future__ import annotations

import re
import socket
import urllib.error
import urllib.request

# co je vlastnost videa (text chyby od yt-dlp)
_REMOVED = re.compile(
    r"has been removed|removed by the uploader|account associated with this video has been"
    r"|no longer available|video has been terminated|terms of service",
    re.I,
)
_CONTENT = re.compile(
    r"private video|video is private|video unavailable|video is unavailable"
    r"|sign in to confirm your age|age[- ]restricted|inappropriate for some users"
    r"|not made this video available in your country|blocked it in your country"
    r"|not available in your country|copyright|members[- ]only|join this channel"
    r"|requires payment|premiere",
    re.I,
)
# co vypadá jako obsah, ale je to dočasné (omezení rychlosti, ověření)
_TRANSIENT = re.compile(
    r"try again later|not a bot|rate.?limit|http error 429|http error 5\d\d"
    r"|timed out|temporary failure|name or service not known|network is unreachable"
    r"|connection|unable to download|getaddrinfo|ssl|vypršel čas|zrušeno",
    re.I,
)


def classify_error(text: str) -> str:
    """"removed" (natrvalo pryč), "content" (skladba se hrát nedá — na čas
    na černou listinu), nebo "transient" (výpadek; nic neblokovat).

    Neznámá chyba je "transient": omylem zablokovaná dobrá skladba je horší
    než jednou přeskočená vadná — ta se sama přeskočí znovu.
    """
    text = text or ""
    if _TRANSIENT.search(text):
        return "transient"
    if _REMOVED.search(text):
        return "removed"
    if _CONTENT.search(text):
        return "content"
    return "transient"


def outage_reason(text: str) -> str:
    """Krátký důvod výpadku pro stav a log."""
    t = (text or "").lower()
    if "not a bot" in t or "sign in" in t or "cookies" in t:
        return "youtube_login"
    if "429" in t or "try again later" in t or "rate" in t:
        return "youtube_limit"
    if "name resolution" in t or "getaddrinfo" in t or "name or service" in t:
        return "dns"
    if "unreachable" in t or "timed out" in t or "connection" in t or "unable to download" in t:
        return "network"
    return "playback"


PROBE_HOSTS = ("www.youtube.com", "music.youtube.com", "redirector.googlevideo.com")
PROBE_URL = "https://www.youtube.com/generate_204"


def probe_connectivity(timeout: float = 5.0) -> tuple[bool, str]:
    """Je YouTube dostupné? DNS pro YouTube i googlevideo + HEAD na generate_204.

    Blokující — volat přes asyncio.to_thread.
    """
    for host in PROBE_HOSTS:
        try:
            socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        except OSError as exc:
            return False, f"dns {host}: {exc}"
    try:
        req = urllib.request.Request(PROBE_URL, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout):
            pass
    except urllib.error.HTTPError as exc:
        if exc.code >= 500:
            return False, f"http {exc.code}"
    except Exception as exc:  # síť, TLS, timeout
        return False, f"http: {type(exc).__name__}: {exc}"[:200]
    return True, ""
