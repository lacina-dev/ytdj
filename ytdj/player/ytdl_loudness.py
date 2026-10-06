"""Srovnání hlasitosti skladeb: hodnota od YouTube → pevný zisk na skladbu.

YouTube má u každé skladby změřenou integrovanou hlasitost (odpověď
přehrávače, `playerConfig.audioConfig.trackAbsoluteLoudnessLkfs`; podle ní
dělá svou „stabilní hlasitost"). yt-dlp ji do svého výsledku nedává, takže si
ji resolver bere sám (install) a připíše do JSONu pro mpv. Měření 6. 10. na
18 skladbách (ffmpeg ebur128 proti hodnotě YouTube): rozdíl nejvýš 0,06 dB.

Zisk je jeden na celou skladbu (dynamika se nemění): cíl − hlasitost.
Ztlumit jde vždy; zesílit nejvýš o MAX_BOOST a jen přes omezovač špiček
v mpv (ytdj_gain.lua) — jinak by tichá nahrávka se špičkami u 0 dBFS
přebuzovala.

Bez importů z ytdj: modul načítá i resolver, který běží pod interpretem
yt-dlp (viz ytdl_resolver.py).
"""

from __future__ import annotations

import math
import re
from typing import Any, Callable

KEY_LOUD = "ytdj_loudness_lufs"  # změřená hlasitost skladby (LUFS), v JSONu pro mpv
KEY_GAIN = "ytdj_gain_db"  # zisk, který má mpv nasadit (dB)
DEFAULT_TARGET = -14  # LUFS — jako streamovací služby (config loudness_target)
MAX_BOOST = 6.0  # dB — víc se tichá skladba nezesílí (šum, omezovač by pumpoval)
MAX_CUT = 24.0  # dB — pojistka proti nesmyslné hodnotě
# mimo tenhle rozsah to není hlasitost hudby, ale chyba v datech (ticho, výplň)
PLAUSIBLE = (-50.0, 6.0)
TAIL = 200  # v tolika posledních znacích JSONu se klíče hledají (jsou na konci)

_NUM = r"\s*(-?\d+(?:\.\d+)?)\s*[,}]"
_RE = {k: re.compile('"' + k + '":' + _NUM) for k in (KEY_LOUD, KEY_GAIN)}


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def summarize(player_responses: Any) -> list[dict]:
    """Z odpovědí přehrávače jen to, co se týká hlasitosti (pár čísel)."""
    out: list[dict] = []
    for pr in player_responses if isinstance(player_responses, (list, tuple)) else ():
        if not isinstance(pr, dict):
            continue
        ac = (pr.get("playerConfig") or {}).get("audioConfig")
        ac = ac if isinstance(ac, dict) else {}
        formats: dict[str, float] = {}
        sd = pr.get("streamingData")
        for f in (sd.get("adaptiveFormats") or ()) if isinstance(sd, dict) else ():
            if not isinstance(f, dict) or not str(f.get("mimeType") or "").startswith("audio"):
                continue
            db = _num(f.get("loudnessDb"))
            if db is not None and f.get("itag") is not None:
                formats[str(f["itag"]) + ("-drc" if f.get("isDrc") else "")] = db
        item = {
            "absolute": _num(ac.get("trackAbsoluteLoudnessLkfs")),
            "perceptual": _num(ac.get("perceptualLoudnessDb")),
            "target": _num(ac.get("loudnessTargetLkfs")),
            "formats": formats,
        }
        if formats or any(item[k] is not None for k in ("absolute", "perceptual")):
            out.append(item)
    return out


def pick(summary: list[dict], format_id: Any = None) -> float | None:
    """Hlasitost skladby v LUFS, nebo None, když ji YouTube neřekl.

    `loudnessDb` formátu je rozdíl proti cíli *toho klienta* (web −14,
    web_music −7), proto se počítá jen s jeho vlastním `loudnessTargetLkfs`;
    nikdy se cíl nehádá. Přednost má hodnota vybraného formátu, pak absolutní
    hodnota skladby.
    """
    fid = str(format_id or "")
    for item in summary:
        db, target = item["formats"].get(fid), item["target"]
        if fid and db is not None and target is not None:
            return _plausible(target + db)
    for key in ("absolute", "perceptual"):
        for item in summary:
            if item[key] is not None:
                return _plausible(item[key])
    return None


def _plausible(lufs: float) -> float | None:
    return round(lufs, 2) if PLAUSIBLE[0] <= lufs <= PLAUSIBLE[1] else None


def gain_db(lufs: Any, target: Any, max_boost: float = MAX_BOOST) -> float | None:
    """Zisk (dB, na desetiny) pro skladbu s hlasitostí `lufs` na cíl `target`.

    None = hlasitost není známá (nebo je to nesmysl) — skladba hraje beze
    změny. Kladný zisk je omezený `max_boost`, záporný MAX_CUT.
    """
    lufs, target = _num(lufs), _num(target)
    if lufs is None or target is None or _plausible(lufs) is None:
        return None
    gain = max(-MAX_CUT, min(max(0.0, float(max_boost)), target - lufs))
    return round(gain, 1) + 0.0  # + 0.0: žádné "-0.0"


def tag(data: str, key: str, value: float) -> str:
    """Připíše `"key": value` na konec JSON objektu (bez nového parsování)."""
    if not data.endswith("}") or not math.isfinite(value):
        return data
    sep = "" if data.rstrip("} \n").endswith("{") else ", "
    return f'{data[:-1]}{sep}"{key}": {value:.2f}}}'


def read(data: str | None, key: str) -> float | None:
    """Hodnota připsaná přes tag(), nebo None. Hledá jen na konci JSONu."""
    if not data:
        return None
    found = _RE[key].findall(data[-TAIL:])
    return float(found[-1]) if found else None


def install(store: Callable[[str, list[dict]], None]) -> bool:
    """Naučí yt-dlp podat hlasitost z odpovědi přehrávače: `store(videoId,
    souhrn)` se zavolá při každém řešení skladby (ve vlákně, které ji řeší).
    Když se yt-dlp změní a tohle nepůjde, hraje se dál — jen bez srovnání."""
    from yt_dlp.extractor.youtube import YoutubeIE

    orig = YoutubeIE._extract_player_responses
    if getattr(orig, "_ytdj_loudness", False):
        orig._ytdj_store[0] = store
        return True
    holder = [store]

    def wrapped(self, clients, video_id, *args, **kwargs):
        result = orig(self, clients, video_id, *args, **kwargs)
        try:
            holder[0](str(video_id), summarize(result[0]))
        except Exception:
            pass
        return result

    wrapped._ytdj_loudness = True  # type: ignore[attr-defined]
    wrapped._ytdj_store = holder  # type: ignore[attr-defined]
    YoutubeIE._extract_player_responses = wrapped
    return True
