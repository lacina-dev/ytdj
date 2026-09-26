"""Import playlistu do oblíbených kanceláře (POZADAVKY #48, FUNKCE F-HLASY-12…).

Vlastník: „Mělo by to umět importovat playlisty a použít je pro oblíbené …
uživatel by tak předal svoje preference, aniž by to musel vypisovat."

Import = 👍 člověka (id klienta, jeho přezdívka) každé písničce z jeho
playlistu. Hlasy samotné počítá ytdj/votes.py (Ballot.src = id importu);
tady je všechno kolem:

    parse_ref   odkaz / id → id playlistu; mix (RD…), „To se mi líbí",
                odkaz na jednu skladbu a nesmysly odmítne česky
    normalize   odpověď ytmusicapi → písničky (Track); nedostupné,
                podcasty, hodinové mixy a videa bez interpreta vynechá
    Importer    přidat / obnovit / odebrat / seznam: limit písniček na
                člověka (`playlist_import_max`), jeden import naráz,
                načtení ve vlákně s časovým limitem, zápis jednou
                transakcí přes vlákno zápisů Store

Nic tu nečeká na disk ani na YouTube v event loopu: ytmusicapi běží ve
vlákně (asyncio.to_thread), zápis jde do fronty Store (`save_import`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from . import telemetry
from .music import match
from .music.catalog import RE_VIDEO, Track, to_track
from .votes import ImportItem, PlaylistImport, VoteBook, song_key, song_keys

log = logging.getLogger(__name__)

MAX_DEFAULT = 1000  # písniček z playlistů na člověka (config playlist_import_max)
MAX_IMPORTS = 10  # playlistů na člověka
FETCH_SLACK = 60  # načte se o tolik víc než limit — nepísničky se vyřadí, limit zůstane plný
FETCH_TIMEOUT = 40.0  # s — YouTube (i fronta za cizím importem)
RATE_MAX = 6  # importů a obnovení na člověka…
RATE_WINDOW = 600.0  # …za 10 min
LONG_S = 15 * 60  # delší „písnička" je mix nebo celé album
SHORT_S = 30  # kratší je znělka, ukázka

# id playlistu: PL…(18/34), OLAK5uy_…(41, album), RDCLAK5uy_…(43, výběr YouTube Music),
# UU…(nahraná videa kanálu); browse id má navíc VL na začátku
_ID = re.compile(r"^[A-Za-z0-9_-]{12,80}$")
_LIST = re.compile(r"[?&]list=([A-Za-z0-9_-]+)")
_BROWSE = re.compile(r"/browse/VL([A-Za-z0-9_-]+)")
_URLISH = re.compile(r"https?://|youtube\.|youtu\.be", re.I)
_PRIVATE_OWN = {"LL", "LM", "WL", "LRYR"}  # To se mi líbí, Později — patří účtu jukeboxu
_EDITORIAL = "RDCLAK5uy_"  # výběr YouTube Music: pevný seznam, ne mix

MSG_PRIVATE = ("Playlist je soukromý (nebo neexistuje) — nastav ho v YouTube Music jako "
               "Neveřejný (s odkazem) a zkus znovu.")


class ImportRefused(ValueError):
    """Import neprošel — text je věta pro člověka, `status` HTTP kód."""

    def __init__(self, message: str, status: int = 400, reason: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason or "refused"


# --------------------------------------------------------------------------
# odkaz → id playlistu
# --------------------------------------------------------------------------


def parse_ref(text: str) -> str:
    """Odkaz z YouTube Music / YouTube (playlist, skladba v playlistu,
    youtu.be s list=, browse/VL…) nebo holé id → id playlistu."""
    text = " ".join(str(text or "").split())
    if not text:
        raise ImportRefused("Vlož odkaz na playlist z YouTube Music nebo YouTube.", reason="empty")
    pid = ""
    if m := _LIST.search(text):
        pid = m.group(1)
    elif m := _BROWSE.search(text):
        pid = m.group(1)
    elif _URLISH.search(text):
        if RE_VIDEO.search(text):
            raise ImportRefused(
                "Tohle je odkaz na jednu skladbu, ne na playlist. V YouTube Music otevři "
                "playlist a zkopíruj jeho odkaz (Sdílet → Kopírovat odkaz).", reason="video")
    elif " " not in text:
        pid = text[2:] if text.startswith("VL") and len(text) > 14 else text
    if pid in _PRIVATE_OWN:
        raise ImportRefused(
            "„To se mi líbí“ a „Přehrát později“ vidíš jen ty — jukebox do nich nevidí. "
            "Vytvoř z písniček playlist, nastav ho jako Neveřejný (s odkazem) a pošli jeho odkaz.",
            reason="own_list")
    if pid.startswith("RD") and not pid.startswith(_EDITORIAL):
        raise ImportRefused(
            "Tohle je mix (rádio) YouTube, ne playlist — skládá se pokaždé jinak. "
            "Pošli odkaz na svůj playlist.", reason="mix")
    if not pid or not _ID.match(pid):
        raise ImportRefused(
            "Tohle nevypadá jako odkaz na playlist — v YouTube Music otevři playlist "
            "a zkopíruj jeho odkaz (Sdílet → Kopírovat odkaz).", reason="not_a_link")
    return pid


def playlist_url(pid: str) -> str:
    return f"https://music.youtube.com/playlist?list={pid}"


# --------------------------------------------------------------------------
# odpověď ytmusicapi → písničky
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Fetched:
    playlist_id: str
    title: str
    total: int | None  # skladeb podle YouTube (hlavička playlistu)
    tracks: list[Track] = field(default_factory=list)
    seen: int = 0  # položek, které přišly
    unavailable: int = 0
    non_music: int = 0
    took_ms: int = 0


_VIDEO_NOISE = match._VIDEO_NOISE  # "(Official Video)", "[Lyrics]"… do štítku nepatří


def normalize(res: dict, pid: str) -> Fetched:
    """Čistá funkce: `YTMusic.get_playlist` → Fetched (bez syrových slovníků)."""
    title = match.clean(str(res.get("title") or "")) or "playlist"
    total = res.get("trackCount")
    out = Fetched(pid, title[:120], total if isinstance(total, int) else None)
    for item in res.get("tracks") or []:
        out.seen += 1
        if not isinstance(item, dict):
            out.unavailable += 1
            continue
        vid, name = item.get("videoId"), item.get("title")
        if not vid or not name or item.get("isAvailable") is False:
            out.unavailable += 1  # smazané, v regionu nedostupné, soukromé video
            continue
        vtype = str(item.get("videoType") or "")
        dur = match.duration(item)
        if "PODCAST" in vtype or (dur is not None and not SHORT_S <= dur <= LONG_S):
            out.non_music += 1  # podcast, hodinový mix, znělka
            continue
        if vtype == "MUSIC_VIDEO_TYPE_UGC" or not match.artist_names(item):
            # video nahrané kýmkoli: interpret bývá v názvu („Kabát - Pohoda"),
            # kanál o něm nic neříká (match.from_video)
            cands = match.from_video(item)
            if not cands:
                out.non_music += 1
                continue
            c = cands[0]
            track = Track(vid, c.title, ", ".join(c.artists), duration=dur)
        else:
            track = to_track(item)
            if track is None:
                out.unavailable += 1
                continue
        if vtype != "MUSIC_VIDEO_TYPE_ATV":
            track.title = _VIDEO_NOISE.sub("", track.title).strip() or track.title
        if not track.artist.strip():
            out.non_music += 1
            continue
        out.tracks.append(track)
    return out


def _classify(exc: BaseException) -> ImportRefused:
    """Chyba ytmusicapi → věta pro člověka. Soukromý a neexistující playlist
    vypadají stejně (stránka bez obsahu: KeyError "Unable to find 'contents'")."""
    text = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, (KeyError, IndexError, TypeError)) or "Unable to find" in text \
            or "404" in text or "not found" in text.lower():
        return ImportRefused(MSG_PRIVATE, 404, reason="private")
    if isinstance(exc, (OSError, ConnectionError)) or "Connection" in text or "timed out" in text:
        return ImportRefused("YouTube teď nejde načíst (síť) — zkus to prosím za chvíli.", 502,
                             reason="network")
    return ImportRefused("Playlist se nepodařilo načíst — zkus to prosím znovu za chvíli.", 502,
                         reason="error")


async def fetch(catalog: Any, pid: str, limit: int, timeout: float = FETCH_TIMEOUT) -> Fetched:
    """Playlist z YouTube Music ve vlákně (ytmusicapi je synchronní)."""
    yt = getattr(catalog, "yt", None)
    if yt is None:
        raise ImportRefused("Katalog YouTube Music tu není.", 503, reason="no_catalog")

    def work() -> Fetched:
        t0 = time.monotonic()
        res = yt.get_playlist(pid, limit=limit)
        out = normalize(res if isinstance(res, dict) else {}, pid)
        # klíče skladeb (regexy, diakritika — na Pi ~0,4 ms za skladbu) se
        # spočítají tady ve vlákně; v event loopu je pak _apply najde v cache
        for t in out.tracks:
            song_key(t.artist, t.title)
            song_keys(t.artist, t.title)
        out.took_ms = int((time.monotonic() - t0) * 1000)
        return out

    try:
        return await asyncio.wait_for(asyncio.to_thread(work), timeout)
    except asyncio.TimeoutError:
        raise ImportRefused("YouTube neodpověděl včas — zkus to prosím za chvíli.", 504,
                            reason="timeout") from None
    except ImportRefused:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # ytmusicapi umí selhat na čemkoli
        log.info("playlist %s se nenačetl: %s", pid, type(exc).__name__)
        raise _classify(exc) from None


# --------------------------------------------------------------------------
# přidat / obnovit / odebrat
# --------------------------------------------------------------------------


def _n(n: int, one: str, few: str, many: str) -> str:
    return f"{n} {one if n == 1 else few if 2 <= n <= 4 else many}"


class Importer:
    """Importy playlistů nad VoteBook: limit, souběh, zápis, zprávy."""

    def __init__(self, book: VoteBook, store: Any = None, catalog: Any = None, cfg: Any = None,
                 clock: Callable[[], float] = time.time,
                 mono: Callable[[], float] = time.monotonic,
                 fetch_fn: Callable[..., Any] | None = None) -> None:
        self.book = book
        self.store = store
        self.catalog = catalog
        self.cfg = cfg
        self.clock = clock
        self.mono = mono
        self.fetch_fn = fetch_fn or fetch
        self._lock: asyncio.Lock | None = None  # jeden import naráz (Pi, YouTube)
        self._busy: set[str] = set()
        self._rate: dict[str, deque[float]] = {}

    def cap(self) -> int:
        return max(1, int(getattr(self.cfg, "playlist_import_max", MAX_DEFAULT) or MAX_DEFAULT))

    # ---- veřejné ----

    def listing(self, viewer: str = "") -> dict:
        """GET /api/votes/imports — všechny importy (kdo, co, kolik), moje první."""
        from .nicks import tag_of

        rows = []
        for imp in self.book.imports.values():
            rows.append({
                "id": imp.id,
                "title": imp.title,
                "who": self.book._label(imp.client, imp.who),
                "tag": tag_of(imp.client),
                "playlist_id": imp.playlist_id,
                "url": playlist_url(imp.playlist_id),
                "songs": len(imp.items),
                "active": self.book.import_active(imp),
                "skipped": imp.skipped,
                "cut": imp.cut,
                "created": round(imp.created, 1),
                "fetched": round(imp.fetched, 1),
                "mine": bool(viewer) and imp.client == viewer,
            })
        rows.sort(key=lambda r: (not r["mine"], -r["created"]))
        out: dict[str, Any] = {"imports": rows, "max": self.cap(), "max_playlists": MAX_IMPORTS}
        if viewer:
            out["mine_songs"] = len(self.book.import_keys(viewer))
        return out

    async def add(self, client: str, who: str, text: str) -> dict:
        pid = parse_ref(text)
        same = next((i for i in self.book.imports_of(client) if i.playlist_id == pid), None)
        if same is not None:
            # tentýž playlist podruhé = obnovit ho
            return await self.refresh(client, same.id, again=True)
        self._rate_check(client)
        if len(self.book.imports_of(client)) >= MAX_IMPORTS:
            raise ImportRefused(f"Máš už {MAX_IMPORTS} playlistů — nějaký odeber a zkus to znovu.",
                                409, reason="too_many")
        room = self.cap() - len(self.book.import_keys(client))
        if room <= 0:
            raise ImportRefused(
                f"Z playlistů už máš {self.cap()} písniček, víc na člověka nejde — "
                "odeber nějaký playlist, nebo ho obnov po úpravě.", 409, reason="full")
        fetched = await self._fetch(client, pid)
        now = self.clock()
        imp = PlaylistImport(secrets.token_hex(6), client, who, pid, fetched.title, now, now)
        return self._apply(imp, fetched, None, "add")

    async def refresh(self, client: str, import_id: str, again: bool = False) -> dict:
        old = self._own(client, import_id, "Obnovit")
        self._rate_check(client)
        fetched = await self._fetch(client, old.playlist_id)
        old = self._own(client, import_id, "Obnovit")  # mezitím ho mohl odebrat
        imp = PlaylistImport(old.id, client, old.who, old.playlist_id, fetched.title or old.title,
                             old.created, self.clock())
        return self._apply(imp, fetched, old, "again" if again else "refresh")

    def remove(self, client: str, import_id: str) -> dict:
        imp = self._own(client, import_id, "Odebrat")
        active = self.book.import_active(imp)
        self.book.drop_import(imp.id)
        if self.store is not None:
            with contextlib.suppress(Exception):
                self.store.delete_import(imp.id)
        telemetry.event("vote.import", action="remove", voter=client[-6:], who=imp.who or None,
                        playlist=imp.playlist_id, songs=len(imp.items), active=active)
        return {"ok": True, "removed": active, "songs": len(imp.items),
                "message": f"Playlist ‚{imp.title}‘ odebrán — {_n(active, 'písnička', 'písničky', 'písniček')} "
                           "už nemá tvůj 👍 (vlastní hlasy zůstaly)."}

    # ---- vnitřek ----

    def _own(self, client: str, import_id: str, what: str) -> PlaylistImport:
        imp = self.book.imports.get(str(import_id or ""))
        if imp is None:
            raise ImportRefused("Tenhle playlist tu už není.", 404, reason="missing")
        if imp.client != client:
            raise ImportRefused(f"{what} jde jen vlastní playlist.", 403, reason="not_owner")
        return imp

    def _rate_check(self, client: str) -> None:
        now = self.mono()
        q = self._rate.setdefault(client, deque())
        while q and now - q[0] > RATE_WINDOW:
            q.popleft()
        if len(q) >= RATE_MAX:
            raise ImportRefused("Moc importů najednou — zkus to prosím za pár minut.", 429,
                                reason="rate")
        q.append(now)

    async def _fetch(self, client: str, pid: str) -> Fetched:
        if client in self._busy:
            raise ImportRefused("Tvůj import ještě běží — počkej, až doběhne.", 409, reason="busy")
        if self._lock is None:
            self._lock = asyncio.Lock()
        self._busy.add(client)
        t0 = time.monotonic()
        try:
            try:
                await asyncio.wait_for(self._lock.acquire(), FETCH_TIMEOUT)
            except asyncio.TimeoutError:
                raise ImportRefused("Zrovna se načítá jiný playlist — zkus to prosím za chvíli.",
                                    503, reason="queue") from None
            try:
                fetched = await self.fetch_fn(self.catalog, pid, self.cap() + FETCH_SLACK)
            finally:
                self._lock.release()
        except ImportRefused as exc:
            telemetry.event("vote.import_failed", playlist=pid, voter=client[-6:],
                            reason=exc.reason, status=exc.status,
                            took_ms=int((time.monotonic() - t0) * 1000))
            raise
        finally:
            self._busy.discard(client)
        if not fetched.tracks:
            telemetry.event("vote.import_failed", playlist=pid, voter=client[-6:], reason="empty",
                            seen=fetched.seen, took_ms=int((time.monotonic() - t0) * 1000))
            if fetched.seen:
                raise ImportRefused(f"V playlistu ‚{fetched.title}‘ není nic, co by šlo pustit "
                                    "(jen nedostupná videa nebo nepísničky).", 422, reason="no_music")
            raise ImportRefused(f"Playlist ‚{fetched.title}‘ je prázdný.", 422, reason="empty")
        return fetched

    def _apply(self, imp: PlaylistImport, fetched: Fetched, old: PlaylistImport | None,
               action: str) -> dict:
        """Písničky do importu (limit, duplicity), 👍 do knihy, zápis, zpráva."""
        t0 = time.monotonic()
        book, client = self.book, imp.client
        others = book.import_keys(client, exclude=imp.id)
        room = self.cap() - len(others)
        used = doubled = over = 0
        for pos, t in enumerate(fetched.tracks):
            key = book.song_key_for(t)
            if key in imp.items:
                doubled += 1  # jiná nahrávka / verze téže písničky v playlistu podruhé
                continue
            if key not in others:
                if used >= room:
                    over += 1
                    continue
                used += 1
            prev = old.items.get(key) if old is not None else None
            imp.items[key] = ImportItem(key, t.id, t.artist, t.title,
                                        prev.added if prev is not None else imp.fetched, pos)
        not_fetched = max(0, (fetched.total or 0) - fetched.seen)
        imp.total = fetched.total or fetched.seen
        imp.skipped = fetched.unavailable + fetched.non_music
        imp.cut = over + not_fetched

        # co to udělá s hlasy toho člověka (před zápisem do knihy)
        had = own_down = 0
        for key in imp.items:
            b = book.items.get(("song", key), {}).get(client)
            if b is None or (b.src == imp.id):
                continue
            if b.src or b.vote > 0:
                had += 1  # už má 👍 (vlastní nebo z jiného playlistu)
            elif b.vote < 0:
                own_down += 1  # jeho 👎 platí
        added = set(imp.items) - set(old.items) if old is not None else set(imp.items)
        gone = set(old.items) - set(imp.items) if old is not None else set()

        book.put_import(imp)
        if self.store is not None:
            with contextlib.suppress(Exception):
                self.store.save_import(imp.meta(), imp.rows())
        active = book.import_active(imp)
        report = {
            "ok": True,
            "action": action,
            "title": imp.title,
            "songs": len(imp.items),
            "active": active,
            "new": len(added),
            "gone": len(gone),
            "had": had,
            "own_down": own_down,
            "doubled": doubled,
            "unavailable": fetched.unavailable,
            "non_music": fetched.non_music,
            "cut": imp.cut,
            "max": self.cap(),
        }
        report["message"] = self._message(report)
        report["import"] = next((r for r in self.listing(client)["imports"] if r["id"] == imp.id), None)
        telemetry.event("vote.import", action=action, voter=client[-6:], who=imp.who or None,
                        playlist=imp.playlist_id, title=telemetry.clip(imp.title, 80),
                        total=imp.total, seen=fetched.seen, songs=len(imp.items), active=active,
                        new=len(added), gone=len(gone), had=had, own_down=own_down,
                        doubled=doubled, unavailable=fetched.unavailable,
                        non_music=fetched.non_music, cut=imp.cut, fetch_ms=fetched.took_ms,
                        apply_ms=int((time.monotonic() - t0) * 1000))
        return report

    @staticmethod
    def _message(r: dict) -> str:
        songs = _n(r["songs"], "písnička", "písničky", "písniček")
        if r["action"] == "add":
            head = f"Hotovo: z playlistu ‚{r['title']}‘ je mezi tvými 👍 {songs}."
        elif not (r["new"] or r["gone"]):
            head = f"Playlist ‚{r['title']}‘ obnoven — beze změny ({songs})."
        else:
            parts = []
            if r["new"]:
                parts.append(f"+{r['new']} {'nová' if r['new'] == 1 else 'nové' if r['new'] <= 4 else 'nových'}")
            if r["gone"]:
                parts.append(f"{r['gone']} už v playlistu {'není' if r['gone'] == 1 else 'nejsou'}")
            head = f"Playlist ‚{r['title']}‘ obnoven: {', '.join(parts)} (teď {songs})."
        notes = []
        if r["had"]:
            notes.append(f"{r['had']} už jsi 👍 měl(a)")
        if r["own_down"]:
            notes.append(f"u {r['own_down']} platí tvůj 👎")
        if r["doubled"]:
            notes.append(f"{r['doubled']} v playlistu dvakrát")
        skipped = r["unavailable"] + r["non_music"]
        if skipped:
            notes.append(f"{skipped} vynecháno (nedostupné nebo to nejsou písničky)")
        if r["cut"]:
            notes.append(f"{r['cut']} se nevešlo — limit je {r['max']} písniček z playlistů na člověka")
        return head + (" " + "; ".join(notes)[:1].upper() + "; ".join(notes)[1:] + "." if notes else "")
