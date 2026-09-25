"""Fronta přání pro víc lidí v kanceláři (PLAN D1–D6, C7).

Každé přání (web, displej, terminál) je `Wish` se jménem autora a stavem:

    čeká → DJ vybírá → ve frontě → hraje → hotovo | nenašel | chyba | odebráno

Přání se přijmou hned (web dostane 202 a id) a DJ je vyřizuje jedno po
druhém — Codex má jeden zámek, rychlá cesta pro "pusť Kabát" ho nepotřebuje.
Vyřízené přání má konkrétní skladby; ty se *zhmotní* do playlistu mpv (fronta
pořád JE playlist mpv) v tomhle pořadí:

  1. "zařadit hned" (play_next) — hned za hrající, nejvýš jedno na člověka;
  2. dohrání rozehraného kola;
  3. spravedlivé střídání lidí: na řadě je ten, kdo nejdéle nic neslyšel
     (nováček první), a dostane jedno kolo ze svého nejstaršího přání —
     nejvýš SHARED_BLOCK (2) skladby, když čekají i jiní, jinak BLOCK (3);
  4. podkres (rádio z poolů) — až za všemi přáními; plní ho plnič fronty.

Příklad (P = Petr "pusť Kabát", J = Jana "Holky z naší školky",
K = Karel "něco klidnějšího", hraje podkres R):

    9:00:00  P pošle (J a K už čekají na DJe), rychlá cesta za 3 s: P1 hraje
             (R se utne, když je přání první na řadě — podkres není ničí přání),
             fronta  P2 | …   (kolo po 2, protože čekají i jiní)
    9:00:20  J vyřízena → fronta  P2 J1 | …
    9:00:35  K vyřízen (nálada, 3 skladby) → fronta  P2 J1 K1 K2 P3 P4 K3 | R(K) …
    …        když už nikdo jiný nečeká, jede Petr po 3 a zbytek Kabátu přejde
             do podkresu.

Podkres mění jen přání nálady/žánru (a interpret, když už nikdo jiný nečeká);
automatické přeseedování po sérii přeskočení sahá jen na podkres a přání nikdy
nemaže.

Když nic nehraje a nic nečeká, "Hrát" spustí AUTO tah podle
`agent.context.start_instruction` (čas, den, kancelář, historie) — ne jako
přání posluchače (důvod "start").
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import telemetry
from .agent.intent import local_command, norm
from .music.catalog import RE_URL, Track
from .player.base import queue_transaction

log = logging.getLogger(__name__)

BLOCK = 3  # skladeb jednoho přání za jedno kolo, když nikdo jiný nečeká
SHARED_BLOCK = 2  # …a když čekají i jiní (kolegové se dostanou na řadu dřív)
ARTIST_MAX = 12  # interpret při souběhu přání: nejvýš tolik skladeb, pak je hotovo
HORIZON = 8  # kolik skladeb z přání držet zhmotněných v playlistu mpv
STABLE = 2  # první dvě čekající položky se kvůli přeřazení nehýbou (připravené)
MAX_ACTIVE = 5  # rozpracovaných přání na člověka (ochrana před zahlcením)
KEEP_DONE = 8  # kolik vyřízených přání ještě ukazovat
DONE_TTL = 15 * 60  # s
WHO_MAX = 24
TEXT_MAX = 500
LEGACY_WAIT = 240.0  # s — staří klienti čekají na odpověď jako dřív
# Přání, které je první na řadě, utne hrající skladbu podkresu (ničí přání to
# není); cizí přání se neutíná nikdy, leda posluchač výslovně řekne "hned teď".
CUT_BACKGROUND = True

VOLUME_MAX = 100  # strop hlasitosti ve všech cestách (web, displej, přání, povely)
PAUSE_RESPECT = 10 * 60  # s — úmyslnou pauzu mladší než tohle přání samo nezruší
TRACK_GUESS = 210  # s — délka skladby, když ji katalog nezná (odhad ETA)

# ---- obnova po restartu ----
RESUME_MAX_AGE = 15 * 60  # s — starší stav = studený start (Pi bylo vypnuté)
RESTORE_MAX_AGE = 2 * 3600  # s — přání se po restartu služby obnoví, i když se nehraje
QUIET_FROM, QUIET_TO = 22, 7  # v noci se sám nikdy nerozjede

ACTIVE = ("waiting", "thinking", "queued", "playing")
FINAL = ("done", "notfound", "error", "removed", "replaced", "skipped")
STATE_CS = {
    "waiting": "čeká",
    "thinking": "DJ vybírá",
    "queued": "ve frontě",
    "playing": "hraje",
    "done": "hotovo",
    "notfound": "nenašel",
    "error": "chyba",
    "removed": "odebráno",
    "replaced": "nahrazeno",
    "skipped": "přeskočeno",
}

# "hned teď" = utnout, co hraje; "hned po téhle" = zařadit hned (play_next)
_CUT = re.compile(r"\b(hned ted|ted hned|okamzite|ihned|utni|prerus\w*|right now)\b")
_NEXT = re.compile(
    r"\b(hned po (teto|tehle|tomhle|tom|te)|po tehle|jako dalsi|hned potom|zarad hned|"
    r"hned za|dalsi na rade|play next)\b"
)


# "Překvap mě" / "něco jiného" = jasná změna směru: pryč od hrajícího interpreta
# i nálady (25. 9. to model vyložil jako "dál Kabát"). "Víc takového" sem nepatří.
_CHANGE = re.compile(
    r"\b(prekvap\w*|(neco|uplne|dej|hraj|pust)\s+(uplne\s+)?jin\w*|zmen\w*\s+(to|styl|hudbu|naladu|smer)"
    r"|jiny styl|jinou hudbu|jina hudba|jiny smer|something (else|different)|surprise me)\b"
)
CHANGE_HINT = (
    " (Posluchač chce jasnou změnu: opusť hrajícího interpreta i současnou náladu — "
    "jiní interpreti, jiný styl; focus_artists nech prázdné, dej start_radio se seedy.)"
)


def wants_change(text: str) -> bool:
    return bool(_CHANGE.search(norm(text)))


# Nové přání téhož člověka nahrazuje jeho starší — pokud výslovně nepřidává
# ("a pak Kometu", "přidej Olympic", "zařaď taky …").
_ADDITIVE = re.compile(
    r"^(a\s+)?(pak|potom|pote|jeste|navic|taky|take)\b"
    r"|\b(pridej\w*|pridat|zarad\w*|k tomu|navic|a potom|then add|add)\b"
)


def additive(text: str) -> bool:
    return bool(_ADDITIVE.search(norm(text)))


# Co posluchač uvidí, když model nejede — nikdy text výjimky (klíče, URL…).
DJ_OFFLINE_TEXT = ("DJ teď nerozumí volnému textu — funguje „pusť <interpret>“, "
                   "název písničky nebo odkaz z YouTube.")
DJ_FAILED_TEXT = "DJ teď neodpověděl — zkus to prosím za chvíli znovu. Hudba hraje dál."


class TooMany(Exception):
    """Člověk má rozpracovaných přání až po strop."""


_CID = re.compile(r"^[A-Za-z0-9_-]{6,40}$")


def clean_cid(cid: Any) -> str:
    """Id klienta (náhodné, z prohlížeče / displeje); "" = neznámý klient."""
    cid = str(cid or "").strip()
    return cid if _CID.match(cid) else ""


def clean_who(who: Any, source: str, cid: str = "") -> str:
    """Jméno je jen popisek — kdo je kdo, určuje id klienta (Wish.key)."""
    name = " ".join(str(who or "").split())[:WHO_MAX]
    if name:
        return name
    if source == "repl":
        return "terminál"
    if source == "panel":
        return "displej"
    # bez jména: krátká značka z id klienta, ať se dva anonymní liší
    # (dřív poslední oktet IP — za NAT byli všichni jeden "host ·1")
    return f"host ·{cid[-3:]}" if cid else "host"


def minutes(seconds: float) -> str:
    m = max(1, round(seconds / 60))
    return f"~{m} min"


def plural_tracks(n: int) -> str:
    if n == 1:
        return "1 skladbu"
    if 2 <= n <= 4:
        return f"{n} skladby"
    return f"{n} skladeb"


def eta_text(ahead: int | None, seconds: float | None = None) -> str:
    """0 = hned po hrající; n = kolik skladeb je před ním (+ odhad v minutách)."""
    if ahead is None:
        return ""
    when = f" ({minutes(seconds)})" if seconds is not None and seconds >= 45 else ""
    if ahead <= 0:
        return "hned po téhle" + when
    return f"za ~{plural_tracks(ahead)}" + when


def _track_json(t: Track) -> dict:
    return {"id": t.id, "title": t.title, "artist": t.artist, "album": t.album,
            "duration": t.duration}


def _track_from(d: Any) -> Track | None:
    if not isinstance(d, dict) or not isinstance(d.get("id"), str):
        return None
    dur = d.get("duration")
    return Track(d["id"], str(d.get("title") or ""), str(d.get("artist") or ""),
                 d.get("album") if isinstance(d.get("album"), str) else None,
                 int(dur) if isinstance(dur, (int, float)) else None)


@dataclass
class Wish:
    id: str
    token: str
    who: str
    source: str
    text: str
    created: float  # epoch
    mono: float  # monotonic (pořadí a čekání)
    state: str = "waiting"
    play_next: bool = False
    cut: bool = False
    kind: str = ""  # artist | song | songs | mood | link | control | none | local
    summary: str = ""
    reply: str = ""
    via: str = ""  # fast | codex | link | local
    tracks: list[Track] = field(default_factory=list)
    artist: str = ""  # interpret (kind artist) — pro předání do podkresu
    artists: list[str] = field(default_factory=list)
    rest: list[Track] = field(default_factory=list)  # interpret nad ARTIST_MAX
    done_ids: set[str] = field(default_factory=set)
    current: str | None = None  # videoId, které z přání právě hraje
    played: int = 0  # kolik skladeb začalo hrát
    errors: int = 0
    started: bool = False  # zazněla už nějaká jeho skladba
    handed: bool = False  # zbytek interpreta předán podkresu
    first_sound: float | None = None  # monotonic
    done_at: float = 0.0  # epoch
    note: str = ""  # proč bylo zařazení upraveno ("jedno hned na osobu")
    cid: str = ""  # id klienta (prohlížeč, relace displeje) — kdo to je
    chip: str = ""  # tlačítko nálady (calmer, livelier…) — DJ ho zvládne i bez modelu
    skipped_by: str = ""  # kdo přeskočil jeho skladbu
    restored: bool = False  # obnoveno po restartu ytdj
    last_end: str = ""  # jak skončila poslední jeho skladba (finished/skipped/…)
    client: dict = field(default_factory=dict)  # ip, ua (zkrácený) — jen do logu
    settled: asyncio.Event | None = field(default=None, repr=False, compare=False)

    def pending(self) -> list[Track]:
        return [t for t in self.tracks if t.id not in self.done_ids and t.id != self.current]

    @property
    def key(self) -> str:
        """Kdo to je (spravedlnost, nahrazování, strop přání): id klienta.

        Jméno je jen popisek — dva lidé za jednou IP nebo dva "displej" nejsou
        jeden člověk a kdokoli, kdo napíše "jana", není Jana. Bez id klienta
        (starý klient, skript) je každé přání samo za sebe.
        """
        return self.cid or f"wish:{self.id}"

    @property
    def active(self) -> bool:
        return self.state in ACTIVE

    def settle(self) -> None:
        if self.settled is not None:
            self.settled.set()

    def public(self, ahead: int | None = None, eta_s: float | None = None) -> dict:
        out: dict[str, Any] = {
            "id": self.id,
            "who": self.who,
            "source": self.source,
            "text": self.text[:200],
            "state": self.state,
            "state_cs": STATE_CS.get(self.state, self.state),
            "kind": self.kind,
            "summary": self.summary,
            "reply": self.reply[:400],
            "play_next": self.play_next,
            "created": round(self.created, 1),
            "n_tracks": len(self.tracks),
            "n_played": self.played,
        }
        if ahead is not None and self.state in ("queued",):
            out["ahead"] = ahead
            out["eta"] = eta_text(ahead, eta_s)
            if eta_s is not None:
                out["eta_s"] = int(eta_s)
        if self.restored:
            out["restored"] = True
        if self.skipped_by:
            out["skipped_by"] = self.skipped_by
        nxt = self.pending()[:1]
        if nxt and self.state in ("queued", "playing"):
            out["next"] = {"title": nxt[0].title, "artist": nxt[0].artist}
        return out

    def to_json(self) -> dict:
        return {
            "id": self.id, "token": self.token, "who": self.who, "source": self.source,
            "text": self.text, "created": self.created, "state": self.state,
            "play_next": self.play_next, "kind": self.kind, "summary": self.summary,
            "reply": self.reply, "via": self.via, "artist": self.artist,
            "artists": self.artists, "played": self.played, "started": self.started,
            "handed": self.handed, "cid": self.cid,
            "tracks": [_track_json(t) for t in self.tracks],
            "rest": [_track_json(t) for t in self.rest[:100]],
            "done": sorted(self.done_ids),
        }

    @classmethod
    def from_json(cls, d: dict) -> "Wish | None":
        try:
            w = cls(
                id=str(d["id"]), token=str(d["token"]), who=str(d.get("who") or "host"),
                source=str(d.get("source") or "web"), text=str(d.get("text") or ""),
                created=float(d.get("created") or time.time()),
                mono=time.monotonic() - max(0.0, time.time() - float(d.get("created") or 0)),
                state=str(d.get("state") or "queued"),
                play_next=bool(d.get("play_next")), kind=str(d.get("kind") or ""),
                summary=str(d.get("summary") or ""), reply=str(d.get("reply") or ""),
                via=str(d.get("via") or ""), artist=str(d.get("artist") or ""),
                artists=[str(a) for a in d.get("artists") or []],
                played=int(d.get("played") or 0), started=bool(d.get("started")),
                handed=bool(d.get("handed")), cid=clean_cid(d.get("cid")),
            )
        except (KeyError, TypeError, ValueError):
            return None
        w.tracks = [t for t in (_track_from(x) for x in d.get("tracks") or []) if t]
        w.rest = [t for t in (_track_from(x) for x in d.get("rest") or []) if t]
        w.done_ids = {str(x) for x in d.get("done") or []}
        return w


# --------------------------------------------------------------------------
# spravedlivé pořadí — čistá funkce, testovatelná bez přehrávače
# --------------------------------------------------------------------------


@dataclass
class Turns:
    """Kdo kdy byl naposledy na řadě a který blok se právě hraje."""

    turn_no: int = 0
    last: dict[str, int] = field(default_factory=dict)  # who → číslo kola
    block: tuple[str, int] | None = None  # (id přání, kolik z bloku už zaznělo)

    def copy(self) -> "Turns":
        return Turns(self.turn_no, dict(self.last), self.block)

    def start(self, w: Wish, size: int = BLOCK) -> bool:
        """Začala skladba přání `w`; True = začalo nové kolo (ne pokračování bloku).

        `size` = jak velký smí blok teď být (BLOCK, nebo SHARED_BLOCK, když
        čekají i jiní) — počítá se v okamžiku startu, takže se blok zkrátí,
        i když někdo přibude uprostřed něj."""
        if self.block and self.block[0] == w.id and self.block[1] < size:
            self.block = (w.id, self.block[1] + 1)
            return False
        self.turn_no += 1
        self.last[w.key] = self.turn_no
        self.block = (w.id, 1)
        return True


def block_size(w: Wish, others_waiting: bool) -> int:
    return SHARED_BLOCK if others_waiting else BLOCK


def fair_order(
    wishes: list[Wish],
    turns: Turns,
    prefix: list[tuple[Wish, Track]] | None = None,
    limit: int = 60,
    waiting: set[str] | None = None,
) -> list[tuple[Wish, Track]]:
    """Pořadí skladeb z přání za hrající skladbou (a za pevným `prefix`).

    Deterministické vůči stavu: stejné přání + stejná historie kol = stejné
    pořadí. Proto se nové přání do fronty jen *vloží* a už zhmotněné položky
    se nepřehazují. `waiting` = kdo (Wish.key) má přání, o kterém DJ teprve
    rozhoduje — i kvůli němu se kolo zkrátí na SHARED_BLOCK.
    """
    sim = turns.copy()
    used: set[str] = set()
    waiting = set(waiting or ())
    live = [w for w in wishes if w.state in ("queued", "playing")]
    queues: dict[str, list[Track]] = {}

    def size(w: Wish) -> int:
        others = any(k != w.key for k in waiting) or any(
            x.key != w.key and any(t.id not in used for t in queues.get(x.id, x.pending()))
            for x in live)
        return block_size(w, others)

    for w, t in prefix or []:
        sim.start(w, size(w))
        used.add(t.id)
    for w in live:
        q = []
        for t in w.pending():
            if t.id not in used:
                q.append(t)
        queues[w.id] = q
    by_id = {w.id: w for w in live}
    out: list[tuple[Wish, Track]] = []

    def emit(w: Wish, n: int) -> None:
        for _ in range(max(0, n)):
            q = queues.get(w.id) or []
            while q and q[0].id in used:  # tatáž skladba v jiném přání — zazní jednou
                q.pop(0)
            if not q or len(out) >= limit:
                return
            t = q.pop(0)
            used.add(t.id)
            out.append((w, t))
            sim.start(w, size(w))

    # 1. zařadit hned — kdo o to požádal (a ještě nic nehrálo), v pořadí přání
    for w in sorted((w for w in live if w.play_next and not w.started), key=lambda w: w.mono):
        emit(w, size(w))
    # 2. dohrát rozehraný blok
    if sim.block and sim.block[0] in by_id:
        cur = by_id[sim.block[0]]
        emit(cur, size(cur) - sim.block[1])
    # 3. střídání lidí: nejdéle neobsloužený první, nováček před všemi
    while len(out) < limit:
        heads: dict[str, Wish] = {}
        for w in sorted(live, key=lambda w: w.mono):
            q = queues.get(w.id)
            if q and any(t.id not in used for t in q) and w.key not in heads:
                heads[w.key] = w
        if not heads:
            break
        who = min(heads, key=lambda p: (sim.last.get(p, 0), heads[p].mono))
        before = len(out)
        emit(heads[who], size(heads[who]))
        if len(out) == before:
            break
    return out


# --------------------------------------------------------------------------
# obnova po restartu
# --------------------------------------------------------------------------


def boot_id() -> str:
    """Id tohohle běhu jádra — mění se s každým startem Pi.

    Pi nemá hodiny s baterií: po startu (před NTP) ukazuje čas posledního
    vypnutí, takže "stav je 5 minut starý" po nočním výpadku proudu lže.
    Jiné boot_id = Pi se restartovalo → stáří stavu se nevěří.
    """
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def can_restore(saved: dict | None, wall: float | None = None,
                boot: str | None = None) -> tuple[bool, str]:
    """Obnovit po startu ytdj přání (ne přehrávání)? Jen po restartu služby
    (stejné boot_id) a když stav není starší než RESTORE_MAX_AGE."""
    if not saved or not isinstance(saved, dict):
        return False, "no_state"
    boot = boot_id() if boot is None else boot
    if saved.get("boot") and boot and saved.get("boot") != boot:
        return False, "reboot"
    wall = time.time() if wall is None else wall
    age = wall - float(saved.get("saved") or 0)
    if age < 0 or age > RESTORE_MAX_AGE:
        return False, "stale"
    return True, "fresh"


def should_resume(saved: dict | None, now: datetime | None = None,
                  wall: float | None = None, boot: str | None = None) -> tuple[bool, str]:
    """Rozjet hudbu sám po startu ytdj? (ano/ne, proč)

    Ano jen když: před restartem hrála (ne pauza, ne ticho), stav je čerstvý
    (≤ RESUME_MAX_AGE — restart služby, nasazení, pád), a není noc
    (QUIET_FROM–QUIET_TO). Studený start Pi (stav z večera) ani noc hudbu
    nespustí; na to je tlačítko Hrát.
    """
    if not saved or not isinstance(saved, dict):
        return False, "no_state"
    boot = boot_id() if boot is None else boot
    if saved.get("boot") and boot and saved.get("boot") != boot:
        return False, "reboot"  # studený start Pi — hodiny bez RTC nejsou k ničemu
    if not saved.get("playing"):
        return False, "was_not_playing"
    wall = time.time() if wall is None else wall
    age = wall - float(saved.get("saved") or 0)
    if age < 0 or age > RESUME_MAX_AGE:
        return False, "stale"
    now = now or datetime.now()
    if now.hour >= QUIET_FROM or now.hour < QUIET_TO:
        return False, "night"
    return True, "fresh"


# --------------------------------------------------------------------------
# fronta
# --------------------------------------------------------------------------


class WishQueue:
    """Přání všech, DJ, který je vyřizuje, a jejich místo v playlistu mpv."""

    def __init__(
        self,
        dj: Any,
        player: Any,
        pools: Any,
        store: Any,
        cfg: Any,
        lock: asyncio.Lock | None = None,
        state_file: Path | None = None,
        on_change: Callable[[], None] | None = None,
        on_listener: Callable[[], Any] | None = None,
        catalog: Any = None,
    ) -> None:
        self.dj = dj
        self.player = player
        self.pools = pools
        self.store = store
        self.cfg = cfg
        self.catalog = catalog
        self.lock = lock or asyncio.Lock()
        self.state_file = state_file
        self.on_change = on_change
        # posluchač promluvil: zrušit automatické přeseedování, klid pro SkipWatch
        self.on_listener = on_listener
        self.wishes: list[Wish] = []
        self.turns = Turns()
        self.owner: dict[str, str] = {}  # videoId zhmotněné položky → id přání
        # proč hraje podkres: start (chytrý rozjezd) | radio; kdo ho naposledy určil
        self.bg_reason: dict[str, str] = {"kind": "radio", "who": "", "text": ""}
        self.starting = False
        self._inflight: dict[str, asyncio.Task] = {}
        # Na Codex se čeká v pořadí příchodu přání — i když se to dřívější
        # zdrželo v rychlé cestě (Pi 25. 9.: Jana přišla před Karlem, ale
        # její rychlá cesta vypršela za 3,5 s a Karel ji na Codexu předběhl
        # o celý tah, 31 s).
        self._codex_order: list[str] = []
        self._codex_moved: asyncio.Event | None = None
        # kolik tahů Codexu kdo dostal, dokud se fronta k Codexu nevyprázdní
        self._codex_served: dict[str, int] = {}
        # vyložená přání se do fronty a podkresu promítají jedno po druhém
        self._apply_lock = asyncio.Lock()
        self.current_vid: str | None = None
        self._order: list[tuple[Wish, Track]] = []  # poslední spočtené pořadí (ETA)
        self._wake = asyncio.Event()
        self._worker: asyncio.Task | None = None
        self._replan_task: asyncio.Task | None = None
        self._replan_again = False
        self._saver: asyncio.Task | None = None
        self._saved_sig = ""
        self._saved_at = 0.0
        self._playing = False  # naposledy zjištěno: hraje (ne pauza, ne ticho)
        self._switch: Callable | None = getattr(dj, "_switch_when_ready", None)
        # poslední odpověď DJe pro všechny (status dj.last) — i rozjezd a chyby
        self.last: dict | None = None
        self._paused_at: float | None = None  # kdy někdo úmyslně dal pauzu
        self._skip_note: tuple[str | None, str, float] | None = None  # (vid, kdo, kdy)
        self._boot = boot_id()
        self._pause_note = False
        self._cur_left: tuple[float, float] | None = None  # (zbývá s, kdy změřeno)
        self._censor_key: tuple | None = None
        self._censor_obj = None

    @property
    def censor(self):
        from .display import censor_for

        key = (bool(getattr(self.cfg, "display_filter", True)),
               tuple(getattr(self.cfg, "display_blocklist", None) or ()))
        if key != self._censor_key:
            self._censor_key, self._censor_obj = key, censor_for(self.cfg)
        return self._censor_obj

    # ---- pohled ----

    def by_id(self, wid: str) -> Wish | None:
        return next((w for w in self.wishes if w.id == wid), None)

    def active(self) -> list[Wish]:
        return [w for w in self.wishes if w.active]

    @property
    def busy(self) -> bool:
        """DJ na něčem pracuje (přání nebo rozjezd)."""
        return any(w.state == "thinking" for w in self.wishes) or self.starting

    def has_requests(self) -> bool:
        return any(w.active for w in self.wishes)

    def ahead_of(self, w: Wish) -> int | None:
        for i, (x, _) in enumerate(self._order):
            if x.id == w.id:
                return i
        return None

    def reason_for(self, vid: str | None) -> dict:
        if not vid:
            return {}
        wid = self.owner.get(vid)
        w = self.by_id(wid) if wid else None
        if w is None:
            w = next((x for x in self.wishes if x.current == vid), None)
        c = self.censor
        if w is not None:
            return {"kind": "wish", "who": c.clean(w.who), "text": c.clean(w.text[:200]), "id": w.id}
        out = {k: v for k, v in self.bg_reason.items() if k != "id"}
        out["who"] = c.clean(out.get("who", ""))
        # "nálada z přání X" jen dokud to přání trvá; pak je to prostě výběr DJe
        src = self.by_id(self.bg_reason.get("id", ""))
        if src is None or not src.active:
            out["who"] = ""
        return out

    def queue_tag(self, vid: str) -> dict | None:
        wid = self.owner.get(vid)
        w = self.by_id(wid) if wid else None
        if w is None or not w.active:
            return None
        return {"id": w.id, "who": self.censor.clean(w.who)}

    def eta_seconds(self, ahead: int | None) -> float | None:
        """Odhad, za kolik sekund začne skladba na místě `ahead` fronty."""
        if ahead is None or self._cur_left is None:
            return None
        left, at = self._cur_left
        total = max(0.0, left - (time.monotonic() - at))
        for _, t in self._order[:ahead]:
            total += t.duration or TRACK_GUESS
        return total

    def public(self) -> list[dict]:
        now = time.time()
        live = [w for w in self.wishes if w.active]
        done = [w for w in self.wishes if not w.active and now - (w.done_at or now) < DONE_TTL]
        done = sorted(done, key=lambda w: w.done_at, reverse=True)[:KEEP_DONE]

        def rank(w: Wish) -> tuple:
            order = {"playing": 0, "queued": 1, "thinking": 2, "waiting": 3}
            ahead = self.ahead_of(w)
            return (order.get(w.state, 9), ahead if ahead is not None else 999, w.mono)

        out = []
        for w in sorted(live, key=rank):
            ahead = self.ahead_of(w)
            out.append(w.public(ahead, self.eta_seconds(ahead)))
        out += [w.public() for w in done]
        c = self.censor
        for d in out:  # displej a web vidí všichni — sprostá slova ne
            d["who"] = c.clean(d["who"])
            d["text"] = c.clean(d["text"])
            if d.get("skipped_by"):
                d["skipped_by"] = c.clean(d["skipped_by"])
        return out

    def people(self) -> list[str]:
        """Jména z posledních přání (výběr jména na displeji)."""
        out: list[str] = []
        for w in sorted(self.wishes, key=lambda w: w.created, reverse=True):
            if w.who not in out and w.who not in ("displej", "terminál") \
                    and not w.who.startswith("host") and not self.censor.clean(w.who) != w.who:
                out.append(w.who)
        return out[:6]

    def _changed(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                log.debug("on_change selhal", exc_info=True)

    # ---- přijetí ----

    def submit(self, text: str, who: Any = "", source: str = "web",
               play_next: bool = False, client: dict | None = None) -> Wish:
        text = " ".join(str(text or "").split())[:TEXT_MAX]
        source = source if source in ("web", "panel", "repl") else "web"
        cid = clean_cid((client or {}).get("id")) or ("repl" if source == "repl" else "")
        who = clean_who(who, source, cid)
        n = norm(text)
        if cid:
            # Starší přání téhož klienta, na která DJ ještě ani nesáhl, se
            # zahodí hned — neplýtvat na ně tahem Codexu (5 přání za sebou
            # zdrželo cizí přání o 5 tahů). Výslovně přidávající zůstanou.
            if not additive(text):
                for x in self.wishes:
                    if x.cid == cid and x.state == "waiting":
                        self._replace_waiting(x)
            mine = [w for w in self.wishes if w.cid == cid and w.active]
            if len(mine) >= MAX_ACTIVE:
                raise TooMany(f"Máš rozpracovaných {len(mine)} přání — počkej, až některé dohraje.")
        cut = bool(_CUT.search(n))
        play_next = bool(play_next) or cut or bool(_NEXT.search(n))
        w = Wish(
            id=secrets.token_hex(4), token=secrets.token_urlsafe(12), who=who, source=source,
            text=text, created=time.time(), mono=time.monotonic(), play_next=play_next, cut=cut,
            cid=cid,
        )
        w.settled = asyncio.Event()
        w.client = {k: str(v)[:60] for k, v in (client or {}).items() if k in ("ip", "ua")}
        if cid:
            w.client["cid"] = cid[-6:]
        if play_next and self._has_play_next(w.key, exclude=w):
            w.play_next = False
            w.note = "Jedno „hned“ na člověka — tohle jde do fronty normálně."
        self.wishes.append(w)
        self._codex_order.append(w.id)
        self._trim()
        # kdo, odkud a čím — ať se v logu vždycky pozná panel, prohlížeč, skript
        telemetry.event("dj.request", source=source, requester=w.who, id=w.id,
                        text=telemetry.clip(text, 300), **w.client)
        telemetry.event(
            "request.created", id=w.id, who=w.who, source=source, text=telemetry.clip(text, 300),
            len=len(text), play_next=w.play_next or None, cut=cut or None, **w.client,
            waiting=sum(1 for x in self.wishes if x.state in ("waiting", "thinking")),
            active_people=len({x.who for x in self.wishes if x.active}),
        )
        log.info("posluchač: %s (přání %s, %s/%s%s)", text, w.id, who, source,
                 f", {w.client.get('ua')}" if w.client.get("ua") else "")
        self._wake.set()
        self._ensure_worker()
        self._changed()
        return w

    def _has_play_next(self, key: str, exclude: Wish | None = None) -> bool:
        return any(
            x.key == key and x.play_next and x.active and not x.started and x is not exclude
            for x in self.wishes
        )

    def _replace_waiting(self, x: Wish) -> None:
        """Čekající (ještě nezpracované) přání zahodit — nahradilo ho novější."""
        x.state = "replaced"
        x.reply = "Nahrazeno novějším přáním."
        x.done_at = time.time()
        x.settle()
        self._codex_done(x.id)
        telemetry.event("request.replaced", id=x.id, who=x.who, source=x.source, by="submit",
                        played=0)

    def _trim(self) -> None:
        now = time.time()
        keep = [w for w in self.wishes if w.active or now - (w.done_at or now) < DONE_TTL]
        done = sorted((w for w in keep if not w.active), key=lambda w: w.done_at, reverse=True)
        drop = {id(w) for w in done[KEEP_DONE * 2:]}
        self.wishes = [w for w in keep if id(w) not in drop]

    async def wait(self, w: Wish, timeout: float = LEGACY_WAIT) -> None:
        """Až je přání vyřízené (zařazené nebo zamítnuté) — pro staré klienty."""
        if w.settled is None or w.settled.is_set():
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(w.settled.wait(), timeout)

    def _authorized(self, w: Wish, token: Any) -> bool:
        return isinstance(token, str) and bool(token) and hmac.compare_digest(w.token, token)

    async def remove(self, wid: str, token: Any, by: str = "owner") -> tuple[bool, str]:
        w = self.by_id(wid)
        if w is None:
            return False, "Takové přání neznám."
        if by == "owner" and not self._authorized(w, token):
            return False, "Tohle přání ti nepatří."
        if not w.active:
            return False, "Přání už je vyřízené."
        await self._drop(w, "removed")
        telemetry.event("request.removed", id=w.id, who=w.who, source=w.source, by=by, state_before=w.state,
                        played=w.played)
        return True, "Odebráno."

    async def _drop(self, w: Wish, state: str, replan: bool = True) -> None:
        self._codex_done(w.id)
        was = w.state
        w.state = state
        w.done_at = time.time()
        w.settle()
        mine = {t.id for t in w.pending()}
        for vid in list(self.owner):
            if self.owner[vid] == w.id:
                self.owner.pop(vid, None)
        if was in ("queued", "playing") and mine:
            async with queue_transaction(self.player):
                await self.player.remove_upcoming(mine)
        self._changed()
        if replan:
            self.kick()

    async def set_play_next(self, wid: str, token: Any) -> tuple[bool, str]:
        w = self.by_id(wid)
        if w is None:
            return False, "Takové přání neznám."
        if not self._authorized(w, token):
            return False, "Tohle přání ti nepatří."
        if not w.active or w.started:
            return False, "Tohle přání už hraje nebo je vyřízené."
        if w.play_next:
            return True, "Už je zařazené hned po téhle."
        if self._has_play_next(w.key, exclude=w):
            return False, "Jedno „hned“ na člověka — tvoje předchozí ještě nezačalo."
        w.play_next = True
        telemetry.event("request.play_next", id=w.id, who=w.who, source=w.source, state=w.state)
        self._changed()
        await self.replan()
        return True, "Hraje hned po téhle skladbě."

    async def cancel_all(self, why: str = "stop") -> int:
        n = 0
        for w in self.active():
            w.state = "removed"
            w.reply = w.reply or "Zrušeno — někdo zastavil hudbu."
            w.done_at = time.time()
            w.settle()
            n += 1
            telemetry.event("request.removed", id=w.id, who=w.who, source=w.source, by=why, played=w.played)
        self.owner.clear()
        self.turns.block = None
        self._order = []
        self._changed()
        return n

    # ---- DJ: jedno přání po druhém ----

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="ytdj-wishes")

    def start(self) -> None:
        self._ensure_worker()
        if self.state_file is not None and (self._saver is None or self._saver.done()):
            self._saver = asyncio.create_task(self._save_loop(), name="ytdj-session")

    async def stop(self) -> None:
        tasks = [self._worker, self._replan_task, self._saver, *self._inflight.values()]
        for task in tasks:
            if task and not task.done():
                task.cancel()
        for task in tasks:
            if task:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def try_local(self, text: str, by: str = "") -> str | None:
        """ "další", "hlasitěji"… hned a bez fronty; None = není to povel."""
        cmd = local_command(text)
        return None if cmd is None else await self.local(cmd, by)

    async def _run(self) -> None:
        """Každé přání hned do práce: rychlá cesta a odkazy nečekají na cizí
        tah Codexu; na Codex (jeden zámek, FIFO) se čeká v pořadí příchodu."""
        while True:
            self._wake.clear()
            for w in sorted((w for w in self.wishes if w.state == "waiting"),
                            key=lambda w: w.mono):
                if w.id not in self._inflight:
                    task = asyncio.create_task(self._guarded(w), name=f"ytdj-wish-{w.id}")
                    self._inflight[w.id] = task
            await self._wake.wait()

    def _codex_done(self, wid: str) -> None:
        """Přání už na Codex nečeká (rozhodla rychlá cesta, dostal tah, zmizelo)."""
        if wid in self._codex_order:
            self._codex_order.remove(wid)
            if not self._codex_order:
                self._codex_served.clear()
            if self._codex_moved is not None:
                self._codex_moved.set()
                self._codex_moved = None

    def _codex_next(self) -> str | None:
        """Kdo je na řadě u Codexu: v pořadí příchodu, ale každý klient nejdřív
        s jedním přáním (druhé přání téhož člověka až po prvních ostatních)."""
        seen: dict[str, int] = dict(self._codex_served)
        best: tuple | None = None
        for i, wid in enumerate(self._codex_order):
            x = self.by_id(wid)
            key = x.key if x else wid
            rank = seen.get(key, 0)
            seen[key] = rank + 1
            if best is None or (rank, i) < best[:2]:
                best = (rank, i, wid)
        return best[2] if best else None

    async def _codex_wait(self, w: Wish) -> None:
        while self._codex_order and self._codex_next() != w.id and w.id in self._codex_order:
            if self._codex_moved is None:
                self._codex_moved = asyncio.Event()
            await self._codex_moved.wait()

    async def _guarded(self, w: Wish) -> None:
        cancelled = False
        try:
            await self._process(w)
        except asyncio.CancelledError:
            cancelled = True  # ytdj končí — přání zůstává, jak je (obnova po restartu)
            raise
        except Exception:
            log.exception("přání %s se nepodařilo vyřídit", w.id)
            if w.active:
                w.reply = DJ_FAILED_TEXT
                self._finish(w, "error")
        finally:
            self._inflight.pop(w.id, None)
            self._codex_done(w.id)
            self._nudge_prefetch()  # přání vyřízeno — resolver zase smí dopředu
            if not cancelled and w.state in ("waiting", "thinking"):  # nic ho nevyřídilo
                w.reply = w.reply or "Přání se nepodařilo vyřídit."
                self._finish(w, "error")
            self._changed()

    async def _process(self, w: Wish) -> None:
        t0 = time.monotonic()
        if self.on_listener:
            res = self.on_listener()
            if asyncio.iscoroutine(res):
                await res
        w.state = "thinking"
        self._changed()
        # Resolver teď nezačíná nic nového dopředu (busy_check → hold): až se
        # rozhodne, co hrát, bude volný pro tu skladbu. Jeden yt-dlp na Pi
        # trvá ~6 s a nedá se přerušit (26. 9.: Kometa čekala 6 s na skladbu
        # Kabátu, kterou její přání vzápětí vyřadilo).
        self._nudge_prefetch()

        if (cmd := local_command(w.text)) is not None:
            w.via, w.kind = "local", "control"
            w.reply = await self.local(cmd)
            self._finish(w, "done", t0)
            return

        plan = None
        if RE_URL.search(w.text):
            plan = await self._link_plan(w)
        if plan is None:
            fast = getattr(self.dj, "fast_plan", None)
            if fast is not None:
                plan = await fast(w.text)
                if plan is not None:
                    w.via = "fast"
        if plan is None and w.chip and getattr(self.dj, "offline", False):
            # mozek DJe nejede (jistič): tlačítko nálady jde i bez něj
            local = getattr(self.dj, "local_intent", None)
            intent = await local(w.chip, w.text) if local is not None else None
            if intent is not None:
                w.via = "local"
                plan = await self.dj.resolve(intent)
        if plan is not None:
            self._codex_done(w.id)  # rozhodnuto bez Codexu — další na řadě nečeká
        if plan is None:
            w.via = "codex"
            if self.lock.locked() or (self._codex_order and self._codex_next() != w.id):
                w.state = "waiting"  # na řadě u Codexu, až doběhne cizí tah
                self._changed()
            await self._codex_wait(w)
            if not w.active:  # mezitím nahrazeno novějším nebo odebráno
                return
            async with self.lock:
                self._codex_served[w.key] = self._codex_served.get(w.key, 0) + 1
                self._codex_done(w.id)
                if not w.active:
                    return
                w.state = "thinking"
                self._changed()
                change = wants_change(w.text)
                try:
                    intent = await self.dj.interpret(w.text + (CHANGE_HINT if change else ""))
                except Exception as exc:
                    log.warning("Codex pro přání %s selhal: %s", w.id, exc)
                    w.reply = self._failure_text(exc)
                    self._finish(w, "error", t0)
                    return
            steered = False
            if change:
                before = intent
                intent = self._steer_change(w, intent)
                steered = intent is not before
            plan = await self.dj.resolve(intent)
            if steered and plan.failed:
                seeds = await asyncio.to_thread(self._history_seeds, before.artists)
                if seeds:
                    plan.failed, plan.seeds = "", seeds
        self._prioritise(plan)
        async with self._apply_lock:
            if not w.active:  # mezitím ho autor odebral
                telemetry.event("request.interpreted", id=w.id, discarded=True,
                                took_ms=int((time.monotonic() - t0) * 1000))
                return
            await self._apply(w, plan, t0)

    def _nudge_prefetch(self) -> None:
        nudge = getattr(self.player, "_schedule_prefetch", None)
        if nudge is not None:
            with contextlib.suppress(Exception):
                nudge(now=True)

    def _prioritise(self, plan: Any) -> None:
        """První skladbu přání řešit hned, ještě než se přání zařadí (podkres,
        rádio a přeplánování trvají další vteřinu)."""
        wait = getattr(self.player, "wait_ready", None)
        if wait is None or plan is None or getattr(plan, "failed", ""):
            return
        first = (plan.requested or plan.artist_tracks or [None])[0]
        if first is not None:
            task = asyncio.create_task(wait(first.id, timeout=20.0))
            task.add_done_callback(lambda t: t.cancelled() or t.exception())

    def _steer_change(self, w: Wish, intent: Any) -> Any:
        """Změna směru nesmí skončit u stejného interpreta (režim interpreta)."""
        from dataclasses import replace

        from .agent.intent import mentions

        if intent.kind != "artist":
            return intent
        named = [a for a in intent.artists if mentions(w.text, a, any_word=True)]
        if named:
            return intent  # "překvap mě něčím od Olympicu" — jmenoval ho
        telemetry.event("request.steer", id=w.id, who=w.who, source=w.source, was="artist",
                        artists=list(intent.artists), to="mood")
        log.info("přání %s: změna směru, režim interpreta %s ruším", w.id, intent.artists)
        seeds = [s for s in intent.seeds if not any(
            mentions(s[0], a, any_word=True) for a in intent.artists)]
        return replace(intent, kind="mood", artists=[], seeds=seeds,
                       mood=intent.mood if intent.mood not in intent.artists else "něco jiného",
                       reply="" if any(a in intent.reply for a in intent.artists) else intent.reply,
                       note=(intent.note + "; " if intent.note else "") + "změna směru")

    def _failure_text(self, exc: BaseException | None = None) -> str:
        """Srozumitelná věta místo chyby — text výjimky se posluchači neukazuje."""
        friendly = getattr(exc, "user_text", None) or getattr(exc, "friendly", None)
        if isinstance(friendly, str) and friendly.strip():
            return friendly.strip()
        if type(exc).__name__ == "CodexOffline" and str(exc).strip():
            return str(exc).strip()  # jistič DJe: jeho zpráva je psaná pro posluchače
        offline = bool(getattr(self.dj, "offline", False)) or type(exc).__name__ == "CodexUnavailable"
        return DJ_OFFLINE_TEXT if offline else DJ_FAILED_TEXT

    def note_last(self, w: Wish | None = None, text: str = "", reply: str = "", ok: bool = True,
                  source: str = "") -> None:
        """Poslední odpověď DJe pro všechny obrazovky (status dj.last)."""
        c = self.censor
        if w is not None:
            text, reply, source = w.text, w.reply, w.source
            ok = w.state not in ("error", "notfound")
        self.last = {"text": c.clean(telemetry.clip(text, 200)), "reply": telemetry.clip(reply, 400),
                     "ok": ok, "source": source,
                     "who": c.clean(w.who) if w is not None else "", "at": time.time()}

    def _finish(self, w: Wish, state: str, t0: float | None = None) -> None:
        w.state = state
        w.done_at = time.time()
        w.settle()
        if state not in ("replaced", "removed") and w.reply:
            self.note_last(w)
        telemetry.event(
            "request.done", id=w.id, who=w.who, source=w.source, state=state, via=w.via or None, intent_kind=w.kind or None,
            played=w.played, reply=telemetry.clip(w.reply, 200) or None,
            took_ms=int((time.monotonic() - t0) * 1000) if t0 else None,
            total_s=int(time.time() - w.created),
        )

    async def local(self, cmd: tuple[str, int], by: str = "") -> str:
        """Jednoznačný povel ("další", "hlasitěji") — hned, bez DJe."""
        action, value = cmd
        telemetry.event("dj.apply", via="local_command", action=action, value=value or None)
        p = self.player
        if action == "skip":
            self.note_skip(by)
            await p.skip()
            return "Přeskakuju."
        if action == "pause":
            self.note_pause(True)
            await p.toggle_pause(True)
            return "Pozastaveno."
        if action == "resume":
            self.note_pause(False)
            await p.toggle_pause(False)
            return "Hraju dál."
        if action == "stop":
            # V kanceláři "stop" nikomu nemaže přání — jen pauza.
            self.note_pause(True)
            await p.toggle_pause(True)
            return "Pozastaveno — fronta i přání zůstávají."
        st = await p.status()
        if action == "louder":
            value = st.volume + 10
        elif action == "quieter":
            value = st.volume - 10
        value = max(0, min(VOLUME_MAX, value))
        await p.set_volume(value)
        return f"Hlasitost {value}."

    def note_skip(self, by: str = "") -> None:
        """Někdo stiskl Další — kdo to byl, pro vlastníka přeskočeného přání."""
        self._skip_note = (self.current_vid, by or "někdo", time.monotonic())

    def note_pause(self, paused: bool) -> None:
        """Úmyslná pauza (tlačítko, povel) — přání ji pár minut nezruší."""
        self._paused_at = time.monotonic() if paused else None

    def _respect_pause(self) -> bool:
        return self._paused_at is not None and time.monotonic() - self._paused_at < PAUSE_RESPECT

    async def stop_all(self) -> None:
        """Stop z webu / API: jen pauza. Cizí přání se nikdy nemažou (25. 9.
        stará stránka v cache poslala "stop" a smazala přání tří lidí);
        vlastní přání si každý odebere křížkem (token)."""
        self.note_pause(True)
        await self.player.toggle_pause(True)

    async def _link_plan(self, w: Wish):
        """Odkaz na YouTube: skladba = přání; playlist/kanál = přání + podkres."""
        from .agent.codex import Plan
        from .agent.intent import Intent

        catalog = self.catalog or getattr(self.dj, "catalog", None)
        if catalog is None:
            return None
        try:
            target = await catalog.resolve_link(RE_URL.search(w.text).group(0))
        except Exception:
            log.exception("odkaz se nepodařilo zpracovat")
            return None
        w.via = "link"
        if not target:
            p = Plan(intent=Intent(kind="songs", text=w.text))
            p.failed = "Tenhle odkaz jsem nerozluštil — zkus název skladby nebo interpreta."
            return p
        if target.kind == "track":
            intent = Intent(kind="songs", text=w.text, reply=f"Zařazuju {target.tracks[0].label()}.")
            return Plan(intent=intent, requested=target.tracks[:1])
        intent = Intent(kind="song", text=w.text, mood=target.label,
                        reply=f"Jedu podle odkazu — {target.label}.")
        return Plan(intent=intent, requested=target.tracks[:ARTIST_MAX],
                    seeds=target.tracks[:4])

    # ---- plán → přání se skladbami ----

    async def _apply(self, w: Wish, plan: Any, t0: float) -> None:
        intent = plan.intent
        w.kind = intent.kind
        newer = [x for x in self.wishes if w.cid and x.cid == w.cid and x.mono > w.mono
                 and x.state not in ("removed", "replaced") and not additive(x.text)]
        if newer and intent.changes_music:
            # mezitím si týž člověk řekl o něco jiného — tohle už nechce
            w.reply = "Nahrazeno novějším přáním."
            self._finish(w, "replaced", t0)
            telemetry.event("request.replaced", id=w.id, who=w.who, source=w.source,
                            by=newer[-1].id, played=0)
            return
        telemetry.event(
            "request.interpreted", id=w.id, who=w.who, source=w.source, via=w.via, intent_kind=intent.kind,
            took_ms=int((time.monotonic() - t0) * 1000),
            artists=list(intent.artists) or None,
            n_requested=len(plan.requested) or None, n_artist=len(plan.artist_tracks) or None,
            failed=telemetry.clip(plan.failed, 200) or None,
        )
        if intent.remember.strip():
            with contextlib.suppress(Exception):
                self.store.remember(intent.remember)
        notes = list(plan.notes)
        if plan.failed:
            w.reply = plan.failed
            self._finish(w, "notfound", t0)
            return

        dj = self.dj
        if intent.kind in ("artist", "song", "mood"):
            # nový výslovný pokyn: výjimky ("ale ne Pohodu") podle něj
            dj.avoid = list(intent.exclude)
        allowed = getattr(dj, "_allowed", lambda ts: ts)

        if intent.kind == "control":
            await self._control(intent)
            w.summary = f"povel: {intent.control}"
            w.reply = (intent.reply or "Hotovo.").strip()
            self._finish(w, "done", t0)
            return
        if intent.kind == "none":
            w.reply = " ".join([intent.reply.strip(), *notes]).strip() or "Rozumím."
            self._finish(w, "done", t0)
            return

        if intent.kind == "artist":
            label = ", ".join(intent.artists)
            every = allowed(list(plan.artist_tracks))
            w.artist, w.artists = label, list(intent.artists)
            w.tracks, w.rest = every[:ARTIST_MAX], every[ARTIST_MAX:]
            w.summary = f"interpret: {label}"
        elif intent.kind == "songs":
            w.tracks = list(plan.requested)
            w.summary = "skladba: " + "; ".join(t.label() for t in plan.requested[:3])
        elif intent.kind == "song":
            w.tracks = list(plan.requested) or []
            w.summary = "skladba: " + "; ".join(t.label() for t in plan.requested[:3])
            seeds = plan.seeds or plan.requested[:3]
            # "X a pak podobné" smí přeladit podkres, jen když nikdo jiný nečeká —
            # jinak by jedna písnička přebila náladu, kterou si řekl někdo jiný
            others = any(x.key != w.key and x.active for x in self.wishes)
            if seeds and not others:
                await self._set_background(seeds=seeds, mood=intent.mood, who=w.who, wid=w.id,
                                           allow_long=True)
        elif intent.kind == "mood":
            w.summary = f"nálada: {intent.mood}" if intent.mood else "nálada"
            await self._set_background(seeds=plan.seeds, mood=intent.mood, who=w.who, wid=w.id,
                                       replace=False)
            # blok nálady patří autorovi — ať ji uslyší, i když čekají jiní
            w.tracks = await dj.next_tracks(BLOCK)
            await self._replace_background()
        if not w.tracks:
            w.reply = " ".join(notes) or "Nic z toho se nepodařilo najít."
            self._finish(w, "notfound", t0)
            return

        record = getattr(dj, "_record_requests", None)
        if record is not None and intent.kind in ("songs", "song"):
            record(w.tracks)  # vyžádané jménem → evidence + nehrát znovu z poolů
        else:
            self.pools.remember_tracks(w.tracks)
            self.pools.session_seen.update(t.id for t in w.tracks)
        self._remember_wish(intent)
        replaced = await self._supersede(w, intent)
        w.state = "queued"
        if w.note:
            notes.append(w.note)
        await self.replan(lock=not replaced)
        paused = self._respect_pause()
        cut = False if paused else await self._maybe_cut(w, own=bool(replaced))
        if not paused:
            await self.player.toggle_pause(False)  # přání = chce slyšet hudbu
        ahead = self.ahead_of(w)
        if paused:
            # Někdo před chvílí dal pauzu (porada, telefon) — přání ji samo nezruší.
            when = "Hudba je pozastavená — pustit? (▶) Přání čeká ve frontě."
        elif w.state == "playing" or cut:
            when = "Hraje hned."
        elif ahead == 0:
            when = "Hraje hned po téhle."
        elif ahead is not None:
            when = f"Na řadě {eta_text(ahead, self.eta_seconds(ahead))}."
        else:
            when = ""
        reply = intent.reply.strip()
        if intent.kind == "artist":
            reply = reply or f"Hraju {w.artist}."
            if any(x.key != w.key and x.active for x in self.wishes):
                notes.append(f"Čekají i další přání, tak se střídáme po {SHARED_BLOCK} skladbách.")
        w.reply = " ".join(x for x in [reply, *notes, when] if x).strip()
        w.settle()
        self.note_last(w)
        telemetry.event(
            "request.queued", id=w.id, who=w.who, source=w.source, intent_kind=w.kind, n_tracks=len(w.tracks),
            ahead=ahead, play_next=w.play_next or None, cut=cut or None,
            took_ms=int((time.monotonic() - t0) * 1000),
            people=len({x.who for x in self.wishes if x.state in ("queued", "playing")}),
        )
        self._changed()

    def _remember_wish(self, intent: Any) -> None:
        dj = self.dj
        wish = getattr(dj, "wish", None)
        if wish is None or intent.auto or not intent.changes_music:
            return
        try:
            dj.wish = wish.then(intent.text, time.time(), list(intent.artists), intent.mood)
            # zápis na SD kartu mimo event loop
            asyncio.get_running_loop().run_in_executor(None, dj.wish.save, dj._wish_file)
        except Exception:
            log.debug("poslední přání se neuložilo", exc_info=True)

    async def _control(self, intent: Any) -> None:
        p = self.player
        if intent.control == "skip":
            await p.skip()
        elif intent.control in ("pause", "stop"):
            # "stop" z přání = pauza; cizí přání se kvůli tomu nemažou
            await p.toggle_pause(True)
        elif intent.control == "resume":
            await p.toggle_pause(False)
        elif intent.control == "volume" and intent.volume:
            await p.set_volume(max(0, min(VOLUME_MAX, int(intent.volume))))

    async def _supersede(self, w: Wish, intent: Any) -> list[Wish]:
        """Nové přání člověka nahradí jeho starší, která ještě hrají nebo čekají.

        Jeden posluchač, který si řekne o něco jiného, nechce dohrávat svoje
        předchozí přání (Pi 26. 9.: "pusť Kometu" čekalo za dvěma skladbami
        Kabátu, o který si řekl on sám). Výjimka: výslovně přidává ("a pak
        …", "přidej …") nebo "zařadit hned". Místo v kole mu zůstane —
        rozehrané kolo převezme nové přání.
        """
        if not intent.changes_music or w.play_next or additive(w.text):
            return []
        old = [x for x in self.wishes
               if x is not w and x.key == w.key and x.active and x.mono < w.mono]
        for x in old:
            if self.turns.block and self.turns.block[0] == x.id:
                self.turns.block = (w.id, self.turns.block[1])
            await self._drop(x, "replaced", replan=False)  # přeplánuje _apply hned potom
            x.reply = (x.reply + " " if x.reply else "") + "Nahrazeno novějším přáním."
            telemetry.event("request.replaced", id=x.id, who=x.who, source=x.source, by=w.id,
                            played=x.played)
        # podkres, který určilo jeho starší přání (třeba zbytek Kabátu po
        # předání), taky patří k tomu, co už nechce — ať po nové písničce
        # nejede dál Kabát
        src = self.by_id(self.bg_reason.get("id", ""))
        if src is not None and src is not w and src.key == w.key and w.tracks \
                and intent.kind in ("songs", "song") \
                and not any(x.key != w.key and x.active for x in self.wishes):
            await self._set_background(seeds=w.tracks[:3], mood=f"{w.tracks[0].artist} a podobné",
                                       who=w.who, wid=w.id, allow_long=True, replace=True)
        return old

    async def _maybe_cut(self, w: Wish, own: bool = False) -> bool:
        """Utnout hrající skladbu? Podkres, když je přání první na řadě;
        výslovné "hned teď"; a když si člověk řekl o něco jiného, než mu
        hraje, a nikdo jiný nečeká (`own`)."""
        st = await self.player.status()
        if st.current is None:
            return False
        if not st.queue or self.owner.get(st.queue[0].id) != w.id:
            return False
        reason = self.reason_for(st.current.id)
        background = reason.get("kind") != "wish"
        others = any(x.key != w.key and x.active for x in self.wishes)
        own_track = self._key_of(reason.get("id")) == w.key
        mine = own and not others and (background or own_track)
        # "hned teď" utne podkres nebo vlastní skladbu — cizí přání nikdy
        explicit = w.cut and (background or own_track)
        if not (explicit or (CUT_BACKGROUND and background) or mine):
            return False
        if self._switch is not None:
            await self._switch(st.queue[0], st.current)
        else:
            await self.player.skip(by_user=False)
        return True

    # ---- podkres ----

    async def _set_background(self, seeds: list[Track] | None = None, mood: str = "",
                              who: str = "", allow_long: bool = False, kind: str = "radio",
                              artist: str = "", artist_tracks: list[Track] | None = None,
                              artists: list[str] | None = None, replace: bool = True,
                              wid: str = "") -> None:
        if artist_tracks:
            await self.pools.set_artist(artist, artist_tracks, mood=mood or artist)
            focus = list(artists or [artist])
        else:
            if not seeds:
                return
            await self.pools.set_seeds(seeds, mood=mood, allow_long=allow_long)
            focus = []
        with contextlib.suppress(AttributeError):
            self.dj._focus_artists = focus
        self.bg_reason = {"kind": kind, "who": who, "text": mood or artist, "id": wid}
        telemetry.event("request.background", reason=kind, who=who or None,
                        mood=telemetry.clip(mood or artist, 120),
                        artist=artist or None, requests=len(self.active()))
        if replace:
            await self._replace_background()

    async def _replace_background(self) -> None:
        """Podkres staré nálady z fronty pryč (přání zůstávají) a nový hned za ně."""
        async with queue_transaction(self.player):
            st = await self.player.status()
            stale = {t.id for t in st.queue if t.id not in self.owner}
            if stale:
                await self.player.remove_upcoming(stale)
            # plnič, který si mezitím vybral skladby staré nálady, je zahodí
            self.player.generation = getattr(self.player, "generation", 0) + 1
            ahead = getattr(self.player, "prefetch_depth", 0) or 0
            target = max(self.cfg.queue_target, ahead + 1 if ahead else 0)
            need = target - self.player.queue_depth
            if need > 0:
                fresh = await self.dj.next_tracks(need)
                if fresh:
                    await self.player.enqueue(fresh)

    async def background_turn(self, instruction: str, kind: str = "radio") -> str:
        """Tah DJe, o který nikdo nežádal (rozjezd, přeseedování) — jen podkres."""
        async with self.lock:
            intent = await self.dj.interpret(instruction, auto=True)
        plan = await self.dj.resolve(intent)
        if plan.failed:
            return plan.failed
        if intent.kind == "artist" and plan.artist_tracks:
            await self._set_background(artist=", ".join(intent.artists), mood=intent.mood,
                                       artist_tracks=plan.artist_tracks, kind=kind,
                                       artists=intent.artists)
        elif intent.kind in ("mood", "song", "songs"):
            seeds = plan.seeds or plan.requested[:4]
            if not seeds:
                return "Ani jednu z navržených skladeb se nepodařilo najít."
            await self._set_background(seeds=seeds, mood=intent.mood, kind=kind)
        else:
            return intent.reply or ""
        return intent.reply or ""

    async def start_idle(self, now: datetime | None = None) -> str:
        """C7: nic nehraje ani nečeká a někdo stiskl Hrát → rozjezd podle situace."""
        from .agent.context import start_context, start_instruction

        if self.starting:
            return "DJ už vybírá."
        self.starting = True
        self._changed()
        t0 = time.monotonic()
        ok, via = False, "codex"
        try:
            ctx = await asyncio.to_thread(start_context, self.store, now, self.cfg)
            instruction = start_instruction(ctx)
            try:
                reply = await self.background_turn(instruction, kind="start")
                ok = bool(self.pools.pools)
            except Exception as exc:
                log.warning("chytrý rozjezd selhal (%s) — jedu z historie", exc)
                reply, ok = "", False
            if not ok:
                via = "history"
                reply = await self._start_from_history()
                ok = bool(self.pools.pools)
            self.note_pause(False)
            await self.player.toggle_pause(False)
            # výsledek rozjezdu vidí všichni (web i displej), ne jen log
            self.note_last(text="▶ rozjezd podle času a dne", source="start", ok=ok,
                           reply=reply or ("Hraju." if ok else "Nemám z čeho začít — napiš DJovi, "
                                                             "co chceš slyšet."))
            return reply
        except asyncio.CancelledError:
            # přišlo přání posluchače — rozjezd ustoupil (jeho hudbu určí přání)
            via = "cancelled"
            raise
        finally:
            self.starting = False
            telemetry.event("request.start", ok=ok, via=via,
                            took_ms=int((time.monotonic() - t0) * 1000),
                            mood=telemetry.clip(getattr(self.pools, "mood", ""), 120) or None)
            self._changed()

    def _history_seeds(self, exclude: list[str] | None = None) -> list[Track]:
        """Seedy z toho, co se tu dohrávalo (jiní interpreti než `exclude`)."""
        from .agent.intent import artist_match

        seen: set[str] = set()
        seeds: list[Track] = []
        with contextlib.suppress(Exception):
            for rec in self.store.recent_history(80):
                if rec.outcome != "finished" or rec.artist in seen:
                    continue
                if any(artist_match(rec.artist, a) for a in exclude or []):
                    continue
                seen.add(rec.artist)
                seeds.append(Track(rec.video_id, rec.title, rec.artist))
                if len(seeds) >= 4:
                    break
        return seeds

    async def _start_from_history(self) -> str:
        """Záchrana, když model neodpoví: rádio z toho, co se tu dohrávalo."""
        seeds = await asyncio.to_thread(self._history_seeds)
        if not seeds:
            return "Nemám z čeho začít — napiš DJovi, co chceš slyšet."
        await self._set_background(seeds=seeds, mood="jako minule", kind="start")
        return "Navazuju na to, co se tu hrálo."

    # ---- plánování: přání → playlist mpv ----

    def kick(self) -> None:
        """Přeplánovat brzy (z obsluhy událostí přehrávače — nečekat na IPC)."""
        if self._replan_task is not None and not self._replan_task.done():
            self._replan_again = True
            return
        self._replan_task = asyncio.create_task(self._replan_soon(), name="ytdj-replan")

    async def _replan_soon(self) -> None:
        while True:
            self._replan_again = False
            try:
                await self.replan()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("přeplánování fronty selhalo")
            if not self._replan_again:
                return

    def needs_top_up(self) -> bool:
        """Plnič: jsou přání se skladbami, které ještě nejsou v playlistu?"""
        live = [w for w in self.wishes if w.state in ("queued", "playing")]
        if not live:
            return False
        placed = sum(1 for v, wid in self.owner.items())
        pending = sum(1 for w in live for t in w.pending() if self.owner.get(t.id) != w.id)
        return pending > 0 and placed < HORIZON

    async def replan(self, lock: bool = True) -> None:
        # předání interpreta mění podkres (vlastní transakce) — ještě před naší
        await self._maybe_hand_over()
        if not self.owner and not any(w.state in ("queued", "playing") for w in self.wishes):
            self._order = []  # žádná přání: na playlist se nesahá (každý start skladby)
            return
        async with queue_transaction(self.player):
            st = await self.player.status()
            self._playing = st.current is not None and not st.paused
            upcoming = list(st.queue)
            live = [w for w in self.wishes if w.state in ("queued", "playing")]
            by_id = {w.id: w for w in live}
            # na začátku fronty zhmotněná přání: nepřehazovat (resolver je chystá)
            prefix: list[tuple[Wish, Track]] = []
            eager = any(w.play_next and not w.started for w in live)
            # `lock=False`: člověk právě nahradil své přání — jeho místo v kole
            # nesmí zabrat ten, kdo se na první místo posunul jen odebráním
            if not eager and lock:
                sim = self.turns.copy()
                for i, t in enumerate(upcoming[:STABLE]):
                    w = by_id.get(self.owner.get(t.id, ""))
                    if w is None or t.id in w.done_ids:
                        break
                    # Další položka zůstává vždy (resolver ji chystá); druhá jen,
                    # když dohrává rozehrané kolo — nové kolo téhož člověka by
                    # jinak předběhlo ty, kdo mezitím přišli na řadu.
                    if sim.start(w, self._turn_size(w)) and i > 0:
                        break
                    prefix.append((w, t))
            if st.current is not None:
                self._cur_left = (max(0.0, float(st.duration or TRACK_GUESS) - float(st.position or 0)),
                                  time.monotonic())
            order = fair_order(live, self.turns, prefix, limit=max(0, HORIZON - len(prefix)),
                               waiting=self._deciding())
            self._order = prefix + order
            desired = [t for _, t in self._order]
            want = {t.id for t in desired}
            stale = {t.id for t in upcoming if t.id in self.owner and t.id not in want}
            if stale:
                await self.player.remove_upcoming(stale)
                for vid in stale:
                    self.owner.pop(vid, None)
            new = [t.id for w, t in self._order if self.owner.get(t.id) != w.id]
            for w, t in self._order:
                self.owner[t.id] = w.id
            if desired:
                await self.player.arrange_front(desired)
            if new:
                mark = getattr(self.player, "mark_requested", None)
                if mark:
                    mark(new)
        self._changed()

    async def _maybe_hand_over(self) -> None:
        """Interpret, na kterého už nikdo jiný nečeká, přejde do podkresu.

        Přání interpreta hraje po blocích, dokud čekají i jiní; když je sám,
        zbytek jeho skladeb pokračuje jako podkres (režim interpreta) — přání
        je tím vyřízené a dál se hraje, dokud někdo neřekne jinak.
        """
        live = [w for w in self.wishes if w.active]
        for w in live:
            if w.kind != "artist" or w.handed or w.state not in ("queued", "playing"):
                continue
            others = [x for x in live if x is not w and (
                x.state in ("waiting", "thinking") or x.pending() or x.current)]
            if others:
                continue
            placed = [t for t in w.pending() if self.owner.get(t.id) == w.id]
            keep_ids = {t.id for t in placed}
            if not placed and not w.started:
                keep_ids = {t.id for t in w.pending()[:BLOCK]}
            every = list(w.tracks) + list(w.rest)
            w.tracks = [t for t in w.tracks
                        if t.id in w.done_ids or t.id == w.current or t.id in keep_ids]
            w.rest = []
            w.handed = True
            self.pools.session_seen.update(t.id for t in w.tracks)
            telemetry.event("request.handover", id=w.id, who=w.who, source=w.source, artist=w.artist,
                            kept=len(w.tracks), background=len(every) - len(w.tracks))
            await self._set_background(artist=w.artist, mood=w.artist, who=w.who, wid=w.id,
                                       artist_tracks=every, artists=w.artists)

    # ---- události přehrávače ----

    async def on_event(self, ev: Any) -> None:
        kind = ev.kind
        vid = ev.track.id if ev.track else None
        if kind == "start":
            self.current_vid = vid
            w = self._owner_of(vid)
            if w is not None:
                new_turn = self.turns.start(w, self._turn_size(w))
                for x in self._holders(vid):
                    x.current = vid
                    x.played += 1
                    x.state = "playing"
                    if not x.started:
                        x.started = True
                        x.play_next = False
                w.play_next = False
                if new_turn:
                    telemetry.event("request.turn", id=w.id, who=w.who, source=w.source, turn=self.turns.turn_no,
                                    people=len({x.who for x in self.wishes
                                                if x.state in ("queued", "playing")}))
            else:
                self.turns.block = None
            self.kick()
            self._changed()
        elif kind == "sound":
            for x in self._holders(vid):
                if x.first_sound is None:
                    x.first_sound = time.monotonic()
                    telemetry.event(
                        "request.started", id=x.id, who=x.who, source=x.source, intent_kind=x.kind or None,
                        video_id=vid, wait_ms=int((x.first_sound - x.mono) * 1000),
                        via=x.via or None, play_next=x.play_next or None,
                    )
        elif kind in ("finished", "skipped", "replaced", "error") and vid:
            changed = False
            by = ""
            if kind == "skipped":
                note = self._skip_note
                if note and note[0] in (vid, None) and time.monotonic() - note[2] < 30:
                    by = note[1]
                self._skip_note = None
            for x in [w for w in self.wishes if w.active and any(t.id == vid for t in w.tracks)]:
                x.done_ids.add(vid)
                x.last_end = kind
                if x.current == vid:
                    x.current = None
                if kind == "error":
                    x.errors += 1
                if kind == "skipped":
                    # vlastník uvidí, kdo mu skladbu přeskočil
                    x.skipped_by = by or "někdo"
                    telemetry.event("request.skipped", id=x.id, who=x.who, source=x.source,
                                    by=x.skipped_by, video_id=vid)
                if not x.pending() and x.current is None and x.state in ("queued", "playing"):
                    if x.errors and x.errors >= x.played:
                        x.reply = (x.reply + " Skladbu se nepodařilo přehrát.").strip()
                        self._finish(x, "error")
                    elif kind == "skipped":
                        x.reply = f"Přeskočil {x.skipped_by}."
                        self._finish(x, "skipped")
                    else:
                        self._finish(x, "done")
                elif x.state == "playing" and x.current is None:
                    x.state = "queued"
                changed = True
            if self.owner.get(vid):
                self.owner.pop(vid, None)
            if vid == self.current_vid:
                self.current_vid = None
            if changed:
                self._changed()
                self.kick()

    def _owner_of(self, vid: str | None) -> Wish | None:
        if not vid:
            return None
        w = self.by_id(self.owner.get(vid, ""))
        if w is not None and w.active:
            return w
        return next((x for x in self.wishes if x.state in ("queued", "playing")
                     and any(t.id == vid for t in x.pending())), None)

    def _holders(self, vid: str | None) -> list[Wish]:
        if not vid:
            return []
        return [x for x in self.wishes if x.state in ("queued", "playing")
                and any(t.id == vid and t.id not in x.done_ids for t in x.tracks)]

    def _key_of(self, wid: Any) -> str | None:
        x = self.by_id(wid) if isinstance(wid, str) and wid else None
        return x.key if x else None

    def _deciding(self) -> set[str]:
        """Kdo má přání, o kterém DJ teprve rozhoduje."""
        return {w.key for w in self.wishes if w.state in ("waiting", "thinking")}

    def _turn_size(self, w: Wish) -> int:
        others = any(k != w.key for k in self._deciding()) or any(
            x.key != w.key and x.state in ("queued", "playing") and x.pending()
            for x in self.wishes)
        return block_size(w, others)

    def can_seed_background(self) -> bool:
        """Smí plnič postavit podkres z hrající skladby? Jen když žádné přání
        nečeká na DJe ani na zařazení svých skladeb do playlistu."""
        for w in self.wishes:
            if w.state in ("waiting", "thinking"):
                return False
            if w.state in ("queued", "playing") and any(
                    self.owner.get(t.id) != w.id for t in w.pending()):
                return False
        return True

    def is_request_track(self, vid: str | None) -> bool:
        return self._owner_of(vid) is not None or any(x.current == vid for x in self.wishes if vid)

    # ---- obnova po restartu ----

    def session_state(self) -> dict:
        pools = self.pools
        bg: dict[str, Any] = {"mood": getattr(pools, "mood", ""), "reason": dict(self.bg_reason)}
        if getattr(pools, "artist", "") and getattr(pools, "_artist_all", None):
            bg.update(mode="artist", artist=pools.artist,
                      artists=list(getattr(self.dj, "_focus_artists", []) or []),
                      tracks=[_track_json(t) for t in pools._artist_all[:100]])
        elif getattr(pools, "pools", None):
            bg.update(mode="seeds", allow_long=bool(getattr(pools, "allow_long", False)),
                      seeds=[_track_json(p.seed) for p in pools.pools[:5]])
        return {
            "v": 1,
            "saved": time.time(),
            "boot": self._boot,
            "playing": self._playing,
            "bg": bg,
            # i rozpracovaná (čeká / DJ vybírá) — po restartu se zpracují znovu
            "wishes": [w.to_json() for w in self.wishes if w.active],
            "turns": {"turn_no": self.turns.turn_no, "last": self.turns.last},
        }

    def load_state(self) -> dict | None:
        if self.state_file is None:
            return None
        try:
            data = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _snapshot_for_save(self) -> tuple[dict, str] | None:
        """Stav k zápisu (v event loopu — jen paměť), None = není co psát."""
        if self.state_file is None:
            return None
        data = self.session_state()
        sig = json.dumps({k: v for k, v in data.items() if k != "saved"}, sort_keys=True,
                         ensure_ascii=False)
        # I beze změny občas zapsat, dokud hraje: stáří stavu rozhoduje o tom,
        # jestli se po restartu naváže (RESUME_MAX_AGE).
        if sig == self._saved_sig and not (
                self._playing and time.time() - self._saved_at > RESUME_MAX_AGE / 5):
            return None
        return data, sig

    def save(self) -> None:
        snap = self._snapshot_for_save()
        if snap is not None:
            self._write_state(*snap)

    async def save_async(self) -> None:
        """Stav se složí v event loopu, na SD kartu se píše ve vlákně."""
        snap = self._snapshot_for_save()
        if snap is not None:
            await asyncio.to_thread(self._write_state, *snap)

    def _write_state(self, data: dict, sig: str) -> None:
        assert self.state_file is not None
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False))
            tmp.replace(self.state_file)
            self._saved_sig = sig
            self._saved_at = time.time()
        except OSError:
            log.warning("stav pro obnovu po restartu se nepodařilo uložit", exc_info=True)

    async def refresh_playing(self) -> None:
        with contextlib.suppress(Exception):
            st = await self.player.status()
            self._playing = st.current is not None and not st.paused

    async def _save_loop(self) -> None:
        while True:
            await asyncio.sleep(5.0)
            await self.refresh_playing()
            await self.save_async()

    async def resume(self, saved: dict | None = None, now: datetime | None = None,
                     wall: float | None = None) -> str:
        """Po startu ytdj: přání obnovit vždy, když je stav čerstvý a Pi se
        mezitím nerestartovalo (can_restore); hudbu rozjet jen podle
        should_resume — jinak zůstane pozastavená a přání čekají ve frontě."""
        saved = self.load_state() if saved is None else saved
        restore, why_r = can_restore(saved, wall)
        ok, why = should_resume(saved, now, wall)
        telemetry.event("request.resume", resume=ok, reason=why, restore=restore,
                        restore_reason=why_r,
                        wishes=len((saved or {}).get("wishes") or []) or None)
        if not restore or saved is None:
            log.info("po startu nic neobnovuji (%s)", why_r)
            return why_r if why_r != "fresh" else why
        if not ok:
            # přání ano, hudba ne (noc, byla pauza…) — pozastavit dřív, než se
            # do mpv něco dostane, ať se samo nerozehraje
            await self.player.toggle_pause(True)
        turns = saved.get("turns") or {}
        with contextlib.suppress(TypeError, ValueError):
            self.turns = Turns(int(turns.get("turn_no") or 0),
                               {str(k): int(v) for k, v in (turns.get("last") or {}).items()})
        rethink = False
        for d in saved.get("wishes") or []:
            w = Wish.from_json(d) if isinstance(d, dict) else None
            if w is None:
                continue
            w.restored = True
            w.settled = asyncio.Event()
            w.settled.set()
            if w.state in ("waiting", "thinking") or not w.tracks:
                # DJ ho nestihl vyřídit — znovu do fronty k DJovi
                w.state = "waiting"
                w.tracks, w.done_ids = [], set()
                self.wishes.append(w)
                self._codex_order.append(w.id)
                rethink = True
                continue
            w.state = "queued" if w.pending() else "done"
            if w.state == "queued":
                self.wishes.append(w)
                self.pools.remember_tracks(w.tracks)
                self.pools.session_seen.update(t.id for t in w.tracks)
        if rethink:
            self._wake.set()
            self._ensure_worker()
        await self.replan()  # přání první — podkres se řadí až za ně
        bg = saved.get("bg") or {}
        reason = bg.get("reason") if isinstance(bg.get("reason"), dict) else {}
        try:
            if bg.get("mode") == "artist":
                tracks = [t for t in (_track_from(x) for x in bg.get("tracks") or []) if t]
                if tracks:
                    await self._set_background(
                        artist=str(bg.get("artist") or ""), mood=str(bg.get("mood") or ""),
                        artist_tracks=tracks, artists=bg.get("artists") or None,
                        who=str(reason.get("who") or ""), kind=str(reason.get("kind") or "radio"))
            elif bg.get("mode") == "seeds":
                seeds = [t for t in (_track_from(x) for x in bg.get("seeds") or []) if t]
                if seeds:
                    await self._set_background(
                        seeds=seeds, mood=str(bg.get("mood") or ""),
                        allow_long=bool(bg.get("allow_long")),
                        who=str(reason.get("who") or ""), kind=str(reason.get("kind") or "radio"))
        except Exception:
            log.exception("podkres se po restartu nepodařilo obnovit")
        if not self.pools.pools and not self.has_requests():
            return "nothing_to_resume"
        if ok:
            await self.player.toggle_pause(False)
        log.info("po restartu obnoveno: %d přání, podkres %s, hraje: %s", len(self.active()),
                 getattr(self.pools, "mood", ""), ok)
        self._changed()
        return why
