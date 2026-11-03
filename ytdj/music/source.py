"""Odkud skladba je: oficiální YouTube Music, nebo video přímo z YouTube.

Rozhoduje jen druh, který uvádí sám YouTube Music (`videoType` u výsledků
hledání, profilu interpreta, rádia, playlistů i jedné skladby):

    MUSIC_VIDEO_TYPE_ATV                      písnička s obalem      → YTM
    MUSIC_VIDEO_TYPE_OMV                      oficiální klip         → YTM
    MUSIC_VIDEO_TYPE_OFFICIAL_SOURCE_MUSIC    oficiální zdroj        → YTM
    MUSIC_VIDEO_TYPE_UGC (a jiný uvedený druh) nahrál někdo         → YT
    nic                                        neví se                → None

„Neví se" je poctivá odpověď: skladba bez uvedeného druhu (starý záznam
historie, hlas, import oblíbených) nedostane žádný odznak — nikdy se z toho,
že druh chybí, nehádá „YouTube". Bez závislostí: čte to web i telka.
"""

from __future__ import annotations

ATV = "MUSIC_VIDEO_TYPE_ATV"
OMV = "MUSIC_VIDEO_TYPE_OMV"
UGC = "MUSIC_VIDEO_TYPE_UGC"
OFFICIAL = frozenset({ATV, OMV, "MUSIC_VIDEO_TYPE_OFFICIAL_SOURCE_MUSIC"})
YTM, YT = "ytm", "yt"  # pole `src` ve stavu pro web a telku (F-WEB-12, F-TV-21)


def source(video_type: object) -> str | None:
    """YTM, YT, nebo None (druh neuveden — nic se netvrdí)."""
    if not isinstance(video_type, str) or not video_type:
        return None
    if video_type in OFFICIAL:
        return YTM
    if "PODCAST" in video_type:
        return None  # epizoda podcastu: nehraje se, nic se o ní netvrdí
    return YT
