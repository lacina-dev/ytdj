"""Layer on top of ytmusicapi.

Rule: what leaves this module is always a compact `Track`, never a raw dict
from ytmusicapi — that one carries thumbnails and feedbackTokens, i.e.
hundreds of useless tokens per result.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, asdict
from typing import Any

from ytmusicapi import YTMusic

from .. import telemetry
from ..config import BROWSER_AUTH, Config
from . import match

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


# Porovnávání jmen a skórování kandidátů žije v match.py (čisté funkce,
# testované na nahraných odpovědích); tady zůstávají staré názvy.
_NOT_AN_ARTIST = match.NOT_AN_ARTIST
_clean = match.clean
_norm = match.norm
_similar = match.similar
_duration = match.duration
ARTIST_FLOOR = match.ARTIST_FLOOR


def _artists(item: dict) -> str:
    return ", ".join(match.artist_names(item))


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


# Viz Catalog.__init__ — proč ne `cfg.language`.
SEARCH_LANGUAGE = "en"


def _track(c: match.Candidate) -> Track:
    return Track(
        id=c.id, title=c.title, artist=c.artist, album=c.album, duration=c.duration
    )


def _candidate(t: Track, rank: int = 0) -> match.Candidate:
    artists = tuple(a for a in t.artist.split(", ") if a)
    return match.Candidate(
        t.id, t.title, artists, album=t.album, duration=t.duration, rank=rank
    )


class Catalog:
    """YTMusic wrapped in asyncio — YTMusic is a synchronous `requests`
    client, so every call goes to a thread to avoid blocking the loop (and
    with it, playback).
    """

    def __init__(self, cfg: Config) -> None:
        auth = str(BROWSER_AUTH) if BROWSER_AUTH.exists() else None
        # Katalog se čte vždy anglicky, `language` z konfigurace se sem
        # nepouští. ytmusicapi 1.12 u hledání s filtrem (songs/videos/artists)
        # zahodí každou poličku, jejíž lokalizovaný nadpis neobsahuje anglické
        # slovo filtru ("song" není ve "Skladby") — s language="cs" tak vrátí
        # prázdný seznam na úplně všechno. Dřívější pojistka to zjistila až
        # prvním prázdným hledáním po každém startu (varování "přepínám katalog
        # na 'en'") a stála jeden zbytečný dotaz. Na datech jazyk nic nemění:
        # názvy a jména jsou od vydavatele, ne přeložené; region hledání dává
        # `location`, a ten zůstává.
        self.yt = YTMusic(auth, language=SEARCH_LANGUAGE, location=cfg.location)
        self.authenticated = auth is not None
        self._cfg = cfg
        self._auth = auth

    async def _call(self, fn, *args, **kwargs) -> Any:
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def _search(self, query: str, **kwargs: Any) -> list[dict]:
        return await self._call(self.yt.search, query, **kwargs)

    async def search(self, query: str, limit: int = 8) -> list[Track]:
        # CAUTION: ytmusicapi treats `limit` as a lower bound, not an upper
        # one — YTM paginates by 20. Always trim on our side.
        raw = await self._search(query, filter="songs", limit=limit)
        tracks = [t for t in (to_track(i) for i in raw) if t]
        return tracks[:limit]

    async def search_videos(self, query: str, limit: int = 10) -> list[Track]:
        """Videa, ne skladby.

        Spousta interpretů v katalogu YouTube Music vůbec není — živé sety,
        looping, menší scéna. Existují jen jako kanál s videi, a ta se hrají
        stejně dobře jako cokoli jiného.
        """
        try:
            raw = await self._search(query, filter="videos", limit=limit)
        except Exception as exc:
            log.warning("hledání videí %r selhalo: %s", query, exc)
            return []
        return [t for t in (to_track(i) for i in raw) if t][:limit]

    async def find_artist_tracks(self, name: str, limit: int = 10) -> list[Track]:
        """Skladby interpreta, ať už ho YouTube Music zná, nebo ne.

        Nejdřív hudební profil; když žádný nemá, tak videa z jeho kanálu.
        Přísně se hlídá, že kanál sedí na hledané jméno — jinak by z toho
        vypadly cizí skladby, které mají to jméno v názvu.

        Totéž co `artist_tracks` (starší jméno). Dřív to bralo jen pět
        skladeb z profilu, ať byl `limit` jakýkoli — "hraj Midi Lidi" s
        limit=30 tak dostalo pět písniček.
        """
        return await self.artist_tracks(name, limit)

    async def search_song(
        self, artist: str, title: str, strict: bool = False
    ) -> Track | None:
        """Najde konkrétní skladbu — a hlídá, že je od toho interpreta.

        Slepě brát první výsledek je zdroj většiny "to jsem nechtěl": na
        "Wonderwall" vyskočí cover, na "Kiss" živák z Utrechtu. Kandidáti se
        skórují v match.py (interpret a název jsou veto, nevyžádané verze se
        trestají, mezi rovnocennými rozhoduje počet přehrání). Když v katalogu
        skladeb nic nesedí, jde se postupně na profil interpreta a na videa.

        Každé volání zapíše jednu událost `catalog.search` (co se chtělo, co
        se vybralo, odkud, se skóre vítěze i druhého v pořadí) a když nic
        nesedí, ještě `catalog.match_fail` s nejlepším odmítnutým kandidátem.

        `strict=True` je pro výslovné přání skladby: jen jedno hledání skladeb,
        bez profilu, videí a bez náhrady "aspoň něco od něj" — co nesedí, je
        None. Chybějící skladba tak stojí jeden dotaz místo čtyř.
        """
        artist, title = _clean(artist), _clean(title)
        query = f"{artist} {title}".strip()
        if not query:
            return None
        ev: dict[str, Any] = {"artist": artist, "title": title, "lang": SEARCH_LANGUAGE}
        if strict:
            ev["strict"] = True
        t0 = time.monotonic()
        track: Track | None = None
        try:
            track = await self._search_song(artist, title, query, ev, strict)
            return track
        except Exception as exc:
            ev["error"] = f"{type(exc).__name__}: {exc}"[:300]
            raise
        finally:
            ev["took_ms"] = int((time.monotonic() - t0) * 1000)
            if track:
                ev["video_id"] = track.id
                ev["chosen"] = track.label()
            telemetry.event("catalog.search", **ev)

    async def _search_song(
        self, artist: str, title: str, query: str, ev: dict[str, Any],
        strict: bool = False,
    ) -> Track | None:
        ev["steps"] = steps = []

        # Bez názvu jde o interpreta jako takového ("zahraj TribalNeed").
        if artist and not title:
            ev["source"] = "artist"
            tracks = await self.find_artist_tracks(artist, limit=1)
            return tracks[0] if tracks else None

        # Víc kandidátů nic nestojí (YTM stránkuje po 20) a kanonická verze
        # nebývá první: u "Pramínek vlasů" je originál ze Semaforu pátý.
        raw = await self._search(query, filter="songs", limit=20)
        songs = [c for c in (match.from_song(i, n) for n, i in enumerate(raw)) if c]
        steps.append({"source": "songs", "query": query, "n": len(songs)})
        if best := self._pick(songs, artist, title, "songs", ev):
            return best

        if strict:
            # výslovné přání: bez náhrad a dalších kol (viz docstring)
            self._match_fail(artist, title, songs, None, ev)
            ev["source"] = None
            return None

        if not artist:
            ev["source"] = "songs_first"
            self._match_fail(artist, title, songs, None, ev)
            return _track(songs[0]) if songs else None

        # 2. Co interpret oficiálně má (profil) — jen jeho, takže bez veta
        #    na jméno.
        own = await self.artist_tracks(artist, limit=100)
        cands = [_candidate(t, n) for n, t in enumerate(own)]
        steps.append({"source": "profile", "n": len(cands)})
        if best := self._pick(cands, artist, title, "profile", ev, artist_known=True):
            return best

        # 3. Videa: spousta písniček existuje jen jako klip nahraný někým
        #    cizím ("Monkey Business - Piece Of My Life", 2,7 mil. zhlédnutí).
        videos = await self._videos(query)
        steps.append({"source": "videos", "query": query, "n": len(videos)})
        if best := self._pick(videos, artist, title, "videos", ev):
            return best

        # 4. Skladbu neznáme, interpreta ano. Pro seed je to pořád dobrý
        #    začátek rádia; do logu, ať je vidět, že to není ono.
        self._match_fail(artist, title, songs, videos, ev)
        if own:
            log.info(
                "skladba %r od %r nenalezena, beru jeho nejznámější: %s",
                title, artist, own[0].label(),
            )
            ev["source"] = "artist_top_fallback"
            return own[0]
        # Když bylo zadané jméno interpreta a nic mu neodpovídá, je lepší
        # nevrátit nic: pustit cizí kapelu jen proto, že má podobný název
        # skladby, je horší než ta skladba nezahrát.
        ev["source"] = None
        return None

    def _match_fail(
        self,
        artist: str,
        title: str,
        songs: list[match.Candidate],
        videos: list[match.Candidate] | None,
        ev: dict[str, Any],
    ) -> None:
        """Proč nic neprošlo: první kandidát ze skladeb (a z videí), s důvodem."""
        rejected = []
        for source, cands in (("songs", songs), ("videos", videos or [])):
            if cands:
                c = cands[0]
                rejected.append({
                    "source": source,
                    "video_id": c.id,
                    "candidate": f"{c.artist} — {c.title}",
                    "why": match.why_rejected(c, artist, title),
                })
        telemetry.event(
            "catalog.match_fail", artist=artist, title=title,
            steps=ev.get("steps"), rejected=rejected,
        )

    async def _videos(self, query: str) -> list[match.Candidate]:
        try:
            raw = await self._search(query, filter="videos", limit=20)
        except Exception as exc:
            log.warning("hledání videí %r selhalo: %s", query, exc)
            return []
        out: list[match.Candidate] = []
        for n, item in enumerate(raw):
            out.extend(match.from_video(item, n))
        return out

    def _pick(
        self,
        cands: list[match.Candidate],
        artist: str,
        title: str,
        source: str,
        ev: dict[str, Any] | None = None,
        artist_known: bool = False,
    ) -> Track | None:
        ranked = match.rank(cands, artist, title, artist_known)
        if not ranked or ranked[0][0] < match.SCORE_FLOOR:
            return None
        score, c = ranked[0]
        log.info(
            "%r — %r → %s — %s [%s] (%s, skóre %.2f, přehrání %s)",
            artist, title, c.artist, c.title, c.id, source, score, c.views,
        )
        if ev is not None:
            ev["source"] = source
            ev["score"] = round(score, 3)
            ev["views"] = c.views
            ev["passed"] = len(ranked)
            if len(ranked) > 1:
                s2, c2 = ranked[1]
                ev["runner_up"] = {
                    "video_id": c2.id,
                    "label": f"{c2.artist} — {c2.title}",
                    "score": round(s2, 3),
                }
        return _track(c)

    def _best_match(
        self, tracks: list[Track], artist: str, title: str, artist_known: bool = False
    ) -> Track | None:
        """Nejlepší z hotových `Track`, nebo nic, když ani ten nesedí."""
        cands = [_candidate(t, n) for n, t in enumerate(tracks)]
        best = match.best(cands, artist, title, artist_known)
        return _track(best) if best else None

    async def find_artist(self, name: str) -> Artist | None:
        """Interpret podle jména — pro zadání typu "zahraj Kabát"."""
        try:
            hits = await self._search(name, filter="artists", limit=5)
        except Exception as exc:
            log.warning("hledání interpreta %r selhalo: %s", name, exc)
            return None
        best: tuple[float, Artist] | None = None
        for h in hits:
            browse = h.get("browseId")
            found = _clean(h.get("artist") or h.get("title") or "")
            if not browse or not found:
                continue
            score = match.artist_similar(found, name)
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

    async def artist_tracks(
        self, name: str, limit: int = 50, browse_id: str | None = None
    ) -> list[Track]:
        """Hodně skladeb jednoho interpreta — na "hraj Midi Lidi".

        Nejdřív hudební profil a jeho playlist "Songs" (u Midi Lidi 150
        skladeb, seřazené podle popularity; `artist_top_songs` dá jen prvních
        pět). Jen skladby, kde je ten interpret opravdu uvedený, každá píseň
        jednou, živáky a remixy na konci — viz match.artist_songs. Když
        profil není, jsou to videa z jeho kanálu (jako find_artist_tracks).
        Prázdný seznam znamená, že takového interpreta YouTube nezná.
        `browse_id` = profil už je známý (rychlá cesta DJe) — ušetří hledání.
        """
        name = _clean(name)
        if not name:
            return []
        with telemetry.timer("catalog.artist_tracks", artist=name, limit=limit) as ev:
            artist = Artist(name, browse_id) if browse_id else await self.find_artist(name)
            if artist:
                ev.update(found=artist.name, browse_id=artist.browse_id)
                items = await self._artist_song_items(artist.browse_id, limit)
                cands = match.artist_songs(items, artist.browse_id, artist.name, limit)
                ev["raw"] = len(items)
                if cands:
                    log.info(
                        "interpret %r → %s (%s): %d skladeb", name, artist.name,
                        artist.browse_id, len(cands),
                    )
                    ev.update(source="profile", n=len(cands), first=cands[0].title)
                    return [_track(c) for c in cands]
            tracks = await self._channel_tracks(name, limit)
            ev.update(source="channel_videos" if tracks else None, n=len(tracks))
            return tracks

    async def _artist_song_items(self, browse_id: str, limit: int) -> list[dict]:
        """Syrové skladby z profilu: celý playlist "Songs", jinak těch pět."""
        try:
            data = await self._call(self.yt.get_artist, browse_id)
        except Exception as exc:
            log.warning("profil interpreta %s selhal: %s", browse_id, exc)
            return []
        songs = data.get("songs") or {}
        top = songs.get("results") or []
        playlist = songs.get("browseId")
        if not playlist or limit <= len(top):
            return top
        try:
            res = await self._call(self.yt.get_playlist, playlist, limit=limit)
        except Exception as exc:
            log.warning("skladby interpreta %s (%s) selhaly: %s", browse_id, playlist, exc)
            return top
        # playlist chce někdy víc dotazů, a když selže uprostřed, pořád
        # máme aspoň nejznámější
        return (res.get("tracks") or []) or top

    async def _channel_tracks(self, name: str, limit: int) -> list[Track]:
        videos = await self.search_videos(name, limit=20)
        mine = [t for t in videos if match.artist_similar(t.artist, name) >= 0.75]
        if mine:
            log.info("interpret %r nalezen jako kanál s videi (%d)", name, len(mine))
        return mine[:limit]

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
