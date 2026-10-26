"""AI tvorba: interpreti, které jukebox sám nenabízí (hlášení z jukeboxu #37).

Vlastník 26. 10. 2026: „Ty AI songy jsou jeden jako druhej a je to nuda.
Bylo by fajn, kdybychom je uměli vyřadit z našeho repertoáru a prostě je
nenabízet."

Seznam interpretů (klíč = votes.artist_key, sedí na každého uvedeného
interpreta skladby jako vyřazení hlasováním). Každý záznam má stav:

    ai        AI tvorba — podkres, rádio, „pusť oblíbené" ani DJ ho samy
              nenabídnou (VoteBook.blocked / pool_reject → "ai")
    navrzeno  návrh (seznam ze state analýzy nebo člověk bez jistoty) —
              jen se ukazuje, hraje se dál; potvrdit / zamítnout jde jedním
              klikem
    neni_ai   člověk řekl „není AI" — automatika ho znovu neoznačí

Kdo rozhoduje: kdokoli s přezdívkou, jedním klikem, vždy vidět kdo a kdy;
kdokoli to jedním klikem vrátí. AI není věc vkusu (na to je 👎 od tří lidí),
ale faktu — a ten jde ověřit (důvod u záznamu).

Automatika (LabelWatch): YouTube ukazuje u videa „Made with AI", když to
autor při nahrání uvedl (sekce „How this was made" a štítek „AI"). Je to
v odpovědi webového klienta YouTube (`/youtubei/v1/next`, klient MWEB),
ne v YouTube Music (WEB_REMIX) ani v ytmusicapi / yt-dlp. Jukebox se
zeptá jednou na každou skladbu podkresu, která čeká ve frontě (ne na přání),
mimo hlavní smyčku; když štítek najde, interpreta označí jako `ai` s důvodem
„YouTube: autor uvedl Made with AI" a skladbu z fronty vyřadí (votes.enforce).
Bez štítku neříká nic — chybějící štítek AI nevylučuje.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import urllib.request
from collections import OrderedDict, deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable

from . import telemetry
from .votes import VoteError, artist_key, artist_keys, credits

log = logging.getLogger(__name__)

AI, SUGGESTED, NOT_AI = "ai", "navrzeno", "neni_ai"
STATES = (AI, SUGGESTED, NOT_AI)
PERSON, YOUTUBE, LIST = "clovek", "youtube", "seznam"
SEED_FILE = Path(__file__).parent / "data" / "ai_seed.json"
RATE_MAX = 20  # rozhodnutí na člověka…
RATE_WINDOW = 600.0  # …za tolik sekund
NAME_MAX = 120
REASON_MAX = 200

# automatika: štítek YouTube „Made with AI"
NEXT_URL = "https://www.youtube.com/youtubei/v1/next?prettyPrint=false"
CLIENT = {"clientName": "MWEB", "clientVersion": "2.20251020.01.00", "hl": "en", "gl": "CZ"}
FETCH_TIMEOUT = 10.0  # s na jednu skladbu
WATCH_EVERY = 20.0  # s mezi koly
PER_ROUND = 3  # nejvýš tolik dotazů na kolo
CHECKED_MAX = 3000
LABEL_REASON = "YouTube: autor u videa uvedl „Made with AI“"


@dataclass(frozen=True, slots=True)
class Flag:
    key: str
    name: str
    state: str
    source: str
    reason: str = ""
    voter: str = ""
    who: str = ""
    ts: float = 0.0
    video_id: str = ""
    title: str = ""

    def row(self) -> tuple:
        return (self.key, self.name, self.state, self.source, self.reason, self.voter,
                self.who, self.ts, self.video_id, self.title)


def _artist(track: Any) -> str:
    return str(getattr(track, "artist", "") or (track.get("artist") if isinstance(track, dict) else "") or "")


def load_seed(path: Path = SEED_FILE) -> list[Flag]:
    """Návrhy, které jdou s kódem (stav `navrzeno`, dokud nerozhodne člověk)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for it in data.get("artists", []):
        name = str(it.get("name") or "").strip()
        if not name:
            continue
        out.append(Flag(artist_key(name), name, SUGGESTED, LIST, str(it.get("reason") or "")[:400],
                        ts=float(it.get("ts") or 0.0), video_id=str(it.get("video_id") or "")))
    return out


class AiBook:
    """Seznam AI tvorby v paměti; zápis přes Store (vlákno zápisů)."""

    def __init__(self, store: Any = None, label: Callable[[str, str], str] | None = None,
                 clock: Callable[[], float] = time.time,
                 mono: Callable[[], float] = time.monotonic,
                 seed: Callable[[], list[Flag]] | None = load_seed) -> None:
        self.store = store
        self.label_fn = label
        self.clock = clock
        self.mono = mono
        self.seed = seed
        self.flags: dict[str, Flag] = {}
        self.active: frozenset[str] = frozenset()  # klíče ve stavu `ai`
        self._rate: dict[str, deque[float]] = {}
        self.version = 0
        self.watch: LabelWatch | None = None

    # ---- načtení ----

    def load(self, rows: Iterable[tuple] = (), seeds: Iterable[Flag] = ()) -> "AiBook":
        for s in seeds:
            if s.key and s.key not in self.flags:
                self.flags[s.key] = s
        for row in rows:
            f = Flag(*[("" if v is None else v) for v in row[:10]])
            if f.key and f.state in STATES:
                self.flags[f.key] = replace(f, ts=float(f.ts or 0.0))
        self._reindex()
        return self

    async def aload(self) -> "AiBook":
        """Mimo event loop: řádky ze state.db a návrhy ze seedu."""
        aread = getattr(self.store, "aread", None)
        rows = await aread("all_ai_flags") if aread is not None and hasattr(self.store, "all_ai_flags") else []
        seeds = await asyncio.to_thread(self.seed) if self.seed is not None else []
        return self.load(rows, seeds)

    def _reindex(self) -> None:
        self.active = frozenset(k for k, f in self.flags.items() if f.state == AI)
        self.version += 1

    # ---- dotazy pro podkres a přání ----

    def flagged(self, track: Any, exempt: Iterable[str] = ()) -> list[str]:
        """Klíče interpretů skladby na seznamu AI tvorby (bez `exempt` = vyžádaných jménem)."""
        if not self.active:
            return []
        ex: set[str] = set()
        for name in exempt:
            ex |= artist_keys(name)
        return [k for k in artist_keys(_artist(track)) if k in self.active and k not in ex]

    def note(self, track: Any, asked: Iterable[str] = ()) -> str:
        """Poznámka k výslovnému přání: „(pozn.: X je na seznamu AI tvorby — označil Petr)"."""
        asked = list(asked)
        for k in self.flagged(track):
            if asked and not any(k in artist_keys(a) for a in asked):
                continue
            f = self.flags[k]
            by = self.by_line(f)
            return f"(pozn.: {f.name} je na seznamu AI tvorby — {by})"
        return ""

    def by_line(self, f: Flag) -> str:
        if f.source == YOUTUBE and not f.voter:
            return "označil ho sám YouTube (autor uvedl „Made with AI“)"
        if f.source == LIST and not f.voter:
            return "návrh z rozboru historie"
        return f"označil(a) {self._label(f.voter, f.who)}"

    def describe(self, limit: int = 30) -> str:
        """Řádek pro DJ (Codex) — "" když nic není."""
        names = sorted(self.flags[k].name for k in self.active)
        if not names:
            return ""
        more = f" a dalších {len(names) - limit}" if len(names) > limit else ""
        return ("AI tvorba — nenavrhuj ani jako seedy (výslovné přání se zahraje): "
                + ", ".join(names[:limit]) + more)

    # ---- rozhodnutí ----

    def decide(self, state: str, voter: str = "", who: str = "", *, name: str = "",
               key: str = "", track: Any = None, reason: str = "",
               source: str = PERSON) -> tuple[Flag, Flag | None]:
        """Zapsat stav interpreta. VoteError s větou pro člověka, když neprojde.

        Cíl: `key` (ze seznamu), jinak `name`, jinak hlavní interpret skladby `track`.
        Vrací (nový záznam, předchozí)."""
        if state not in STATES:
            raise VoteError("Stav je „ai“, „navrzeno“, nebo „neni_ai“.", 400)
        title = video_id = ""
        if track is not None:
            title = str(getattr(track, "title", "") or "")[:200]
            video_id = str(getattr(track, "id", "") or "")[:64]
        if key and key in self.flags:
            display = self.flags[key].name
        else:
            display = " ".join(str(name or "").split())[:NAME_MAX]
            if not display and track is not None:
                cr = credits(_artist(track))
                display = cr[0] if cr else ""
            key = artist_key(display) if display else ""
        if not key:
            raise VoteError("Nevím, o kterého interpreta jde.", 404)
        if source == PERSON:
            if not voter:
                raise VoteError("Chybí id prohlížeče.", 400)
            self._rate_check(voter)
        prev = self.flags.get(key)
        flag = Flag(key, display or (prev.name if prev else key), state, source,
                    " ".join(str(reason or "").split())[:REASON_MAX], voter, who[:24],
                    round(self.clock(), 1), video_id or (prev.video_id if prev else ""),
                    title or (prev.title if prev else ""))
        self.flags[key] = flag
        self._reindex()
        if self.store is not None and hasattr(self.store, "save_ai_flag"):
            self.store.save_ai_flag(flag.row())
        telemetry.event("ai.flag", key=key, name=telemetry.clip(flag.name, 80), state=state,
                        prev=prev.state if prev else None, source=source,
                        voter=voter[-6:] if voter else None, who=who or None,
                        video_id=video_id or None)
        return flag, prev

    def _rate_check(self, voter: str) -> None:
        now = self.mono()
        q = self._rate.setdefault(voter, deque())
        while q and now - q[0] > RATE_WINDOW:
            q.popleft()
        if len(q) >= RATE_MAX:
            telemetry.event("ai.rate_limited", voter=voter[-6:], n=len(q))
            raise VoteError("Moc změn najednou — zkus to prosím za pár minut.", 429)
        q.append(now)

    # ---- zobrazení ----

    def _label(self, voter: str, who: str) -> str:
        if self.label_fn is not None and voter:
            with contextlib.suppress(Exception):
                name = self.label_fn(voter, who)
                if name:
                    return name
        return who or "Host"

    def item(self, f: Flag, viewer: str = "") -> dict:
        from .nicks import tag_of

        out = {"key": f.key, "name": f.name, "state": f.state, "source": f.source,
               "reason": f.reason, "by": self.by_line(f), "at": f.ts or None,
               "video_id": f.video_id, "title": f.title}
        if f.voter:
            out["nick"] = self._label(f.voter, f.who)
            out["tag"] = tag_of(f.voter)
            if viewer:
                out["mine"] = f.voter == viewer
        return out

    def lists(self, viewer: str = "") -> dict:
        rows = sorted(self.flags.values(), key=lambda f: (-f.ts, f.name.lower()))
        out: dict[str, list] = {AI: [], SUGGESTED: [], NOT_AI: []}
        for f in rows:
            out[f.state].append(self.item(f, viewer))
        return {"ai": out[AI], "suggested": out[SUGGESTED], "not_ai": out[NOT_AI],
                "counts": {k: len(v) for k, v in out.items()}}

    def state_of(self, track: Any) -> dict | None:
        """Stav interpretů hrající skladby pro web (první, který na seznamu je)."""
        for k in artist_keys(_artist(track)):
            f = self.flags.get(k)
            if f is not None:
                return self.item(f)
        return None


# --------------------------------------------------------------------------
# automatika: štítek „Made with AI" od YouTube
# --------------------------------------------------------------------------


def parse_label(body: str) -> bool:
    """Odpověď /youtubei/v1/next → autor uvedl „Made with AI"?"""
    i = body.find('"howThisWasMadeSectionViewModel":{')
    if i < 0:
        return False
    return '"Made with AI"' in body[i:i + 3000]


def fetch_label(video_id: str, timeout: float = FETCH_TIMEOUT) -> bool:
    """Jeden dotaz (blokující — volat ve vlákně). Výjimka = nevím."""
    data = json.dumps({"context": {"client": CLIENT}, "videoId": video_id}).encode()
    req = urllib.request.Request(NEXT_URL, data=data, headers={
        "Content-Type": "application/json", "User-Agent": "Mozilla/5.0", "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (pevná adresa)
        return parse_label(resp.read().decode("utf-8", "replace"))


class LabelWatch:
    """Kontroluje skladby podkresu ve frontě (a hrající podkres), každou jednou."""

    def __init__(self, app: Any, book: AiBook,
                 fetch: Callable[[str], bool] = fetch_label,
                 every: float = WATCH_EVERY) -> None:
        self.app = app
        self.book = book
        self.fetch = fetch
        self.every = every
        self.checked: OrderedDict[str, bool | None] = OrderedDict()
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(), name="ytdj-ai-label")

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.every)
            try:
                await self.round()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("kontrola štítku AI selhala")

    def _todo(self, tracks: Iterable[Any]) -> list[Any]:
        wq = getattr(self.app, "wishes", None)
        is_wish = getattr(wq, "is_request_track", None) or (lambda vid: False)
        out = []
        for t in tracks:
            if t is None or not getattr(t, "id", "") or t.id in self.checked or is_wish(t.id):
                continue
            keys = artist_keys(_artist(t))
            # o interpretovi už rozhodl člověk (nebo už je označený) → nic se neptá
            if any(self.book.flags.get(k) is not None and self.book.flags[k].state in (AI, NOT_AI)
                   for k in keys):
                continue
            out.append(t)
        return out

    async def round(self) -> list[str]:
        """Jedno kolo; vrací klíče interpretů, které tím přibyly na seznam."""
        player = getattr(self.app, "player", None)
        if player is None:
            return []
        st = await player.status()
        added: list[str] = []
        for t in self._todo([st.current, *st.queue])[:PER_ROUND]:
            t0 = time.monotonic()
            try:
                hit: bool | None = await asyncio.to_thread(self.fetch, t.id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                hit = None
                telemetry.event("ai.label", video_id=t.id, ok=False,
                                error=telemetry.clip(f"{type(exc).__name__}: {exc}", 120),
                                took_ms=int((time.monotonic() - t0) * 1000))
            else:
                telemetry.event("ai.label", video_id=t.id, ok=True, ai=hit,
                                took_ms=int((time.monotonic() - t0) * 1000))
            if hit is not None:
                self.checked[t.id] = hit
                while len(self.checked) > CHECKED_MAX:
                    self.checked.popitem(last=False)
            if hit:
                cr = credits(_artist(t))
                if not cr or any(self.book.flags.get(k) is not None and self.book.flags[k].state == NOT_AI
                                 for k in artist_keys(_artist(t))):
                    continue
                flag, _ = self.book.decide(AI, name=cr[0], track=t, reason=LABEL_REASON,
                                           source=YOUTUBE)
                added.append(flag.key)
        if added:
            from .votes import enforce

            await enforce(self.app, reason="ai:" + ",".join(added))
            web = getattr(self.app, "web", None)
            with contextlib.suppress(Exception):
                if web is not None:
                    web.poke()
        return added


def wire(app: Any, votes: Any, label: Callable[[str, str], str] | None = None) -> AiBook:
    """Seznam AI tvorby do hlasování (votes.wire). Automatiku spouští až App._run."""
    book = AiBook(getattr(app, "store", None), label=label)
    votes.ai = book
    app.ai = book
    return book


def start_watch(app: Any) -> LabelWatch | None:
    """Spustit automatiku štítku (jen opravdová aplikace — testy síť nepotřebují)."""
    book = getattr(app, "ai", None)
    if book is None:
        return None
    book.watch = LabelWatch(app, book)
    book.watch.start()
    return book.watch
