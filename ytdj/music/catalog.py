"""Layer on top of ytmusicapi.

Rule: what leaves this module is always a compact `Track`, never a raw dict
from ytmusicapi — that one carries thumbnails and feedbackTokens, i.e.
hundreds of useless tokens per result.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from dataclasses import dataclass, asdict
from difflib import SequenceMatcher
from typing import Any

from ytmusicapi import YTMusic

from ..config import BROWSER_AUTH, Config

log = logging.getLogger(__name__)

# videoId má 11 znaků, playlist a kanál jsou delší a mají vlastní prefixy
RE_VIDEO = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/)([\w-]{11})")
RE_PLAYLIST = re.compile(r"[?&]list=([\w-]{13,})")
RE_CHANNEL = re.compile(r"/channel/(UC[\w-]{20,})")
RE_HANDLE = re.compile(r"youtube\.com/@([\w.\-]+)")
# stačí, aby v textu URL vůbec byla — uživatel ji obvykle vloží s komentářem
RE_URL = re.compile(r"https?://\S*(?:youtube\.com|youtu\.be)\S*", re.I)


@dataclass(slots=True)
class Track:
    id: str
    title: str
    artist: str
    album: str | None = None
    duration: int | None = None  # seconds

    def compact(self) -> dict:
        """What the LLM will see — without None fields."""
        return {k: v for k, v in asdict(self).items() if v is not None}

    def label(self) -> str:
        return f"{self.artist} — {self.title}" if self.artist else self.title


# ytmusicapi sometimes stuffs `artists` with the play count ("3,4 tis.
# přehrání"), the like count ("Líbí se 2,7 tis. lidem") or the year. Without
# this filter it drags all the way into the prompt and the history.
_NOT_AN_ARTIST = re.compile(
    r"(přehrání|zhlédnutí|views|plays|streams|likes?|lidem)\s*$"
    r"|^líbí\s+se\b"
    r"|^\d{4}$",
    re.I,
)


def _clean(text: str) -> str:
    return text.replace("\xa0", " ").strip()


def _artists(item: dict) -> str:
    arts = item.get("artists") or []
    names = [_clean(a.get("name", "")) for a in arts if isinstance(a, dict)]
    names = [n for n in names if n and not _NOT_AN_ARTIST.search(n)]
    return ", ".join(names)


def _duration(item: dict) -> int | None:
    if isinstance(item.get("duration_seconds"), int):
        return item["duration_seconds"]
    text = item.get("duration") or item.get("length")
    if not isinstance(text, str):
        return None
    parts = text.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    seconds = 0
    for n in nums:
        seconds = seconds * 60 + n
    return seconds


def _norm(text: str) -> str:
    """Bez diakritiky, bez interpunkce, malými písmeny.

    "Děda Mládek" a "Deda Mladek" musí padnout na sebe — YouTube Music vrací
    jednou tak, jednou onak.
    """
    text = unicodedata.normalize("NFKD", _clean(text).lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _similar(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # Kapely se píšou jednou dohromady, jednou zvlášť: "TribalNeed" a
    # "Tribal Need" je totéž jméno.
    if a.replace(" ", "") == b.replace(" ", ""):
        return 1.0
    # částečná shoda: "wonderwall" v "wonderwall remastered 2014"
    if a in b or b in a:
        return 0.9
    return SequenceMatcher(None, a, b).ratio()


# Pod tímhle už nejde o toho interpreta, ale o někoho jiného.
ARTIST_FLOOR = 0.5


@dataclass(slots=True)
class Artist:
    name: str
    browse_id: str


@dataclass(slots=True)
class LinkTarget:
    """Co se skrývá za odkazem, který uživatel poslal."""
    kind: str  # "track" | "playlist" | "artist"
    label: str
    tracks: list["Track"]


def to_track(item: dict) -> Track | None:
    vid = item.get("videoId")
    title = item.get("title")
    if not vid or not title:
        return None
    album = item.get("album")
    if isinstance(album, dict):
        album = album.get("name")
    return Track(
        id=vid,
        title=_clean(title),
        artist=_artists(item),
        album=album if isinstance(album, str) else None,
        duration=_duration(item),
    )


class Catalog:
    """YTMusic wrapped in asyncio — YTMusic is a synchronous `requests`
    client, so every call goes to a thread to avoid blocking the loop (and
    with it, playback).
    """

    def __init__(self, cfg: Config) -> None:
        auth = str(BROWSER_AUTH) if BROWSER_AUTH.exists() else None
        self.yt = YTMusic(auth, language=cfg.language, location=cfg.location)
        self.authenticated = auth is not None
        self._cfg = cfg

    async def _call(self, fn, *args, **kwargs) -> Any:
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def search(self, query: str, limit: int = 8) -> list[Track]:
        # CAUTION: ytmusicapi treats `limit` as a lower bound, not an upper
        # one — YTM paginates by 20. Always trim on our side.
        raw = await self._call(self.yt.search, query, filter="songs", limit=limit)
        tracks = [t for t in (to_track(i) for i in raw) if t]
        return tracks[:limit]

    async def search_videos(self, query: str, limit: int = 10) -> list[Track]:
        """Videa, ne skladby.

        Spousta interpretů v katalogu YouTube Music vůbec není — živé sety,
        looping, menší scéna. Existují jen jako kanál s videi, a ta se hrají
        stejně dobře jako cokoli jiného.
        """
        try:
            raw = await self._call(self.yt.search, query, filter="videos", limit=limit)
        except Exception as exc:
            log.warning("hledání videí %r selhalo: %s", query, exc)
            return []
        return [t for t in (to_track(i) for i in raw) if t][:limit]

    async def find_artist_tracks(self, name: str, limit: int = 10) -> list[Track]:
        """Skladby interpreta, ať už ho YouTube Music zná, nebo ne.

        Nejdřív hudební profil; když žádný nemá, tak videa z jeho kanálu.
        Přísně se hlídá, že kanál sedí na hledané jméno — jinak by z toho
        vypadly cizí skladby, které mají to jméno v názvu.
        """
        if artist := await self.find_artist(name):
            if tracks := await self.artist_top_songs(artist.browse_id, limit=limit):
                return tracks

        videos = await self.search_videos(name, limit=20)
        mine = [t for t in videos if _similar(t.artist, name) >= 0.75]
        if mine:
            log.info("interpret %r nalezen jako kanál s videi (%d)", name, len(mine))
        return mine[:limit]

    async def search_song(self, artist: str, title: str) -> Track | None:
        """Najde konkrétní skladbu — a hlídá, že je od toho interpreta.

        Slepě brát první výsledek je zdroj většiny "to jsem nechtěl": na
        "Wonderwall" vyskočí cover, na "Jez" kdeco. Kandidáti se proto
        ohodnotí zvlášť podle interpreta a podle názvu, a když ani nejlepší
        z nich nesedí, zkusí se to přes profil interpreta.
        """
        artist, title = _clean(artist), _clean(title)
        query = f"{artist} {title}".strip()
        if not query:
            return None

        # Bez názvu jde o interpreta jako takového ("zahraj TribalNeed").
        if artist and not title:
            tracks = await self.find_artist_tracks(artist, limit=1)
            return tracks[0] if tracks else None

        candidates = await self.search(query, limit=8)
        if best := self._best_match(candidates, artist, title):
            return best

        # Text hledání selhalo — projít, co interpret vlastně má.
        if artist:
            songs = await self.find_artist_tracks(artist, limit=30)
            if best := self._best_match(songs, artist, title, artist_known=True):
                return best
            if songs:
                return songs[0]  # jeho skladbu neznáme, ale jeho ano

        # Když bylo zadané jméno interpreta a nic mu neodpovídá, je lepší
        # nevrátit nic: pustit cizí kapelu jen proto, že má podobný název
        # skladby, je horší než ta skladba nezahrát.
        if artist:
            return None
        return candidates[0] if candidates else None

    def _best_match(
        self, tracks: list[Track], artist: str, title: str, artist_known: bool = False
    ) -> Track | None:
        """Nejlepší kandidát, nebo nic, když ani ten nesedí."""
        scored = []
        for t in tracks:
            title_score = _similar(t.title, title) if title else 0.5
            if not artist or artist_known:
                score = title_score
            else:
                # Cizí interpret se nebere vůbec, ani kdyby název seděl na
                # sto procent: na "TribalNeed — Tribal Need" jinak vyhraje
                # "Ballistic Noise — Tribal Need", což je úplně jiná kapela.
                artist_score = _similar(t.artist, artist)
                if artist_score < ARTIST_FLOOR:
                    continue
                score = 0.6 * artist_score + 0.4 * title_score
            scored.append((score, t))
        if not scored:
            return None
        score, track = max(scored, key=lambda s: s[0])
        log.debug("nejlepší shoda %.2f: %s", score, track.label())
        return track if score >= 0.55 else None

    async def find_artist(self, name: str) -> Artist | None:
        """Interpret podle jména — pro zadání typu "zahraj Kabát"."""
        try:
            hits = await self._call(self.yt.search, name, filter="artists", limit=5)
        except Exception as exc:
            log.warning("hledání interpreta %r selhalo: %s", name, exc)
            return None
        best: tuple[float, Artist] | None = None
        for h in hits:
            browse = h.get("browseId")
            found = _clean(h.get("artist") or h.get("title") or "")
            if not browse or not found:
                continue
            score = _similar(found, name)
            if best is None or score > best[0]:
                best = (score, Artist(found, browse))
        if best and best[0] >= 0.6:
            return best[1]
        return None

    async def artist_top_songs(self, browse_id: str, limit: int = 10) -> list[Track]:
        """Nejhranější skladby interpreta — dobré seedy i dobrá odpověď."""
        try:
            data = await self._call(self.yt.get_artist, browse_id)
        except Exception as exc:
            log.warning("profil interpreta %s selhal: %s", browse_id, exc)
            return []
        songs = (data.get("songs") or {}).get("results") or []
        tracks = [t for t in (to_track(i) for i in songs) if t]
        return tracks[:limit]

    async def radio(self, video_id: str, limit: int = 50) -> list[Track]:
        """Radio from a seed track. Returns a different mix every time — by design."""
        res = await self._call(
            self.yt.get_watch_playlist, videoId=video_id, radio=True, limit=limit
        )
        return [t for t in (to_track(i) for i in res.get("tracks", [])) if t]

    async def mood_categories(self) -> dict[str, list[dict]]:
        """params change over time — never hardcode them, fetch at runtime."""
        return await self._call(self.yt.get_mood_categories)

    async def mood_playlists(self, params: str) -> list[dict]:
        return await self._call(self.yt.get_mood_playlists, params)

    async def playlist_tracks(self, playlist_id: str, limit: int = 50) -> list[Track]:
        # shuffle=True crashes on RDCLAK5 playlists — ytmusicapi only sends it
        # for PL/OLA prefixes. Without shuffle it always goes through.
        res = await self._call(
            self.yt.get_watch_playlist, playlistId=playlist_id, limit=limit
        )
        return [t for t in (to_track(i) for i in res.get("tracks", [])) if t]

    # ---- odkazy ----

    async def track_by_id(self, video_id: str) -> Track | None:
        """Metadata k videoId bez hledání — rádio o jedné položce je vrátí."""
        try:
            res = await self._call(
                self.yt.get_watch_playlist, videoId=video_id, limit=1
            )
        except Exception as exc:
            log.warning("skladba %s se nenačetla: %s", video_id, exc)
            return None
        for item in res.get("tracks") or []:
            if track := to_track(item):
                return track
        return None

    async def resolve_link(self, url: str) -> LinkTarget | None:
        """Odkaz na YouTube → skladba, playlist, nebo interpret."""
        if m := RE_VIDEO.search(url):
            if track := await self.track_by_id(m.group(1)):
                return LinkTarget("track", track.label(), [track])
            return None

        if m := RE_PLAYLIST.search(url):
            tracks = await self.playlist_tracks(m.group(1), limit=50)
            return LinkTarget("playlist", "playlist", tracks) if tracks else None

        if not (RE_CHANNEL.search(url) or RE_HANDLE.search(url)):
            return None
        return await self._resolve_channel(url)

    async def _resolve_channel(self, url: str) -> LinkTarget | None:
        """Odkaz na kanál → interpret v YouTube Music.

        Kanál na YouTube a profil interpreta v YouTube Music jsou dvě různé
        věci: u řady interpretů (typicky těch, co nemají "Topic" kanál) na
        channelId z odkazu žádný hudební profil nevisí. Proto je tu ještě
        druhý pokus přes jméno kanálu.
        """
        channel_id = m.group(1) if (m := RE_CHANNEL.search(url)) else None
        name = ""

        if channel_id:
            if tracks := await self.artist_top_songs(channel_id, limit=15):
                return LinkTarget("artist", tracks[0].artist or "interpret", tracks)
        else:
            channel_id, name = await self._channel_info(url)
            if channel_id:
                if tracks := await self.artist_top_songs(channel_id, limit=15):
                    return LinkTarget("artist", tracks[0].artist or name, tracks)

        if not name:
            _, name = await self._channel_info(url)
        if not name:
            return None
        found = await self.find_artist(name)
        if not found:
            return None
        tracks = await self.artist_top_songs(found.browse_id, limit=15)
        return LinkTarget("artist", found.name, tracks) if tracks else None

    async def _channel_info(self, url: str) -> tuple[str | None, str]:
        """(channelId, jméno kanálu) přes yt-dlp — ytmusicapi tohle neumí."""
        try:
            # --flat-playlist by vrátil jen seznam videí, kde je channel_id
            # prázdné; jedno rozbalené video ho nese a stojí to ~2 s
            proc = await asyncio.create_subprocess_exec(
                self._cfg.yt_dlp_path, "--playlist-end", "1", "--skip-download",
                "--js-runtimes", self._cfg.js_runtimes or "node",
                "--print", "%(channel_id)s|%(channel)s", url,
                env=self._cfg.child_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=45)
        except (OSError, asyncio.TimeoutError) as exc:
            log.warning("překlad kanálu %r přes yt-dlp selhal: %s", url, exc)
            return None, ""

        for line in out.decode(errors="replace").splitlines():
            cid, _, cname = line.strip().partition("|")
            if cid.startswith("UC"):
                return cid, _clean(cname) if cname != "NA" else ""
        return None, ""

    async def rate(self, video_id: str, rating: str) -> None:
        """LIKE | DISLIKE | INDIFFERENT — only when logged in."""
        if not self.authenticated:
            raise RuntimeError("hodnocení vyžaduje přihlášení (ytmusicapi browser)")
        await self._call(self.yt.rate_song, video_id, rating)
