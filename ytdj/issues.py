"""Chyby a nápady kolegů — stránka „Chyby a nápady" na webu jukeboxu.

Vlastník (POZADAVKY #52): „Udělat tam issues, aby kolegové mohli ty věci
reportovat a navrhovat, aniž bych to musel přepisovat… všechno tam dej a pak
k tomu piš, jak to bylo vyřešeno."

Kdo je kdo, určuje id prohlížeče jako u přání (přezdívka je popisek). Kolega
přidá položku (Chyba / Nápad), komentář a „+1"; stav a text „Jak to bylo
vyřešeno" mění jen správce s PINem (web/issues_api.py). Pravidla jsou
v docs/FUNKCE.md, oblast „Chyby a nápady" (F-HLASENI-01…).

Paměť a disk: všechno drží `IssueBook` v paměti (stropy níž — pár MB
nanejvýš), čtení i zápisy v hlavní smyčce jsou jen práce s pamětí. Na kartu
jde každá změna přes vlákno zapisovače `Store` (state.db, tabulky `issues`,
`issue_comments`, `issue_plus`); při startu se čte mimo smyčku (`aload`).

Seed — položky, které jdou s kódem (`ytdj/data/issues_seed.json`): při startu
se podle stálého klíče (`key`) založí, nebo — když se jejich zápis v seedu
od minula změnil — přepíšou název, text, druh, stav a řešení. Komentáře
a +1 od lidí se nikdy nepřepisují. Vývojář tak s každým nasazením zapíše,
jak to bylo vyřešeno.

Z terminálu (proti běžícímu ytdj, PIN ze souboru správce nebo --pin):

    python -m ytdj.issues list [--state open] [--kind chyba]
    python -m ytdj.issues show 12
    python -m ytdj.issues resolve 12 --state vyreseno --text "Opraveno v …"

Jen standardní knihovna.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sys
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

SEED_FILE = Path(__file__).resolve().parent / "data" / "issues_seed.json"

KINDS = {"chyba": "Chyba", "napad": "Nápad"}
# nové → řeší se → čeká na nasazení → vyřešeno / zamítnuto
STATES = {
    "nove": "nové",
    "resi_se": "řeší se",
    "nasazeni": "čeká na nasazení",
    "vyreseno": "vyřešeno",
    "zamitnuto": "zamítnuto",
}
OPEN_STATES = frozenset({"nove", "resi_se", "nasazeni"})

TITLE_MIN = 3
TITLE_MAX = 120
BODY_MAX = 2000
COMMENT_MAX = 1000
RESOLUTION_MAX = 2000
WHO_MAX = 24

# Stropy: SD karta ani 1 GB RAM nesmí trpět, ať do sítě píše kdokoli.
MAX_ITEMS = 300  # položek celkem (plno → vypadne nejstarší uzavřená)
MAX_COMMENTS = 100  # komentářů u jedné položky
MAX_COMMENTS_TOTAL = 3000
MAX_PLUS = 200  # +1 u jedné položky
PAGE_DEFAULT = 20
PAGE_MAX = 50
EXCERPT = 180

# Brzda na člověka (id prohlížeče) jako u hlasů; adresa má strop 4× vyšší
# (střídání id z jednoho stroje brzdu neobejde).
RATE_WINDOW = 600.0
RATE_CREATE = 5
RATE_COMMENT = 10
RATE_PLUS = 30
RATE_IP_FACTOR = 4


class IssueError(ValueError):
    """Požadavek neprošel — text je věta pro člověka, `status` kód HTTP."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# řídicí znaky (kromě nového řádku) do textu nepatří
_CTRL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f  ​-‏‪-‮⁦-⁩]")


def clean_line(raw: Any) -> str:
    """Jeden řádek: NFC, bez řídicích znaků, mezery srovnané."""
    text = unicodedata.normalize("NFC", str(raw if raw is not None else ""))
    return " ".join(_CTRL.sub(" ", text.replace("\n", " ")).split())


def clean_text(raw: Any) -> str:
    """Volný text: řádky zůstanou, řídicí znaky ne, nejvýš jeden prázdný řádek za sebou."""
    text = unicodedata.normalize("NFC", str(raw if raw is not None else ""))
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    lines = [_CTRL.sub("", ln).rstrip() for ln in text.split("\n")]
    out: list[str] = []
    for ln in lines:
        if ln or (out and out[-1]):
            out.append(ln)
    return "\n".join(out).strip()


def excerpt(text: str, limit: int = EXCERPT) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def parse_date(value: Any) -> float | None:
    """Datum ze seedu: "2026-10-06" (poledne místního času) nebo číslo (epoch)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if value > 0 else None
    if isinstance(value, str):
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", value.strip())
        if m:
            try:
                return time.mktime((int(m[1]), int(m[2]), int(m[3]), 12, 0, 0, 0, 0, -1))
            except (ValueError, OverflowError):
                return None
    return None


@dataclass
class Issue:
    id: int
    kind: str
    title: str
    body: str
    state: str = "nove"
    resolution: str = ""
    resolved_at: float | None = None
    author: str = ""  # id klienta
    who: str = ""  # přezdívka v době zápisu
    created: float = 0.0
    updated: float = 0.0
    seed_key: str | None = None
    seed_sig: str | None = None
    comments: list[dict] = field(default_factory=list)  # {id, author, who, body, ts}
    plus: dict[str, tuple[str, float]] = field(default_factory=dict)  # klient → (kdo, kdy)

    def row(self) -> tuple:
        return (self.id, self.seed_key, self.seed_sig, self.kind, self.title, self.body, self.state,
                self.resolution, self.resolved_at, self.author, self.who, self.created, self.updated)


class _Rate:
    """Nejvýš `limit` akcí za `window` sekund na klíč (jako VoteBook._rate_check)."""

    def __init__(self, limit: int, window: float, mono: Callable[[], float]) -> None:
        self.limit, self.window, self.mono = limit, window, mono
        self.seen: dict[str, deque[float]] = {}

    def full(self, key: str, factor: int = 1) -> bool:
        now = self.mono()
        q = self.seen.get(key)
        if q is None:
            return False
        while q and now - q[0] > self.window:
            q.popleft()
        return len(q) >= self.limit * factor

    def note(self, key: str) -> None:
        now = self.mono()
        self.seen.setdefault(key, deque()).append(now)
        if len(self.seen) > 1000:  # zapomenout ty, kdo dlouho nic nepsali
            for k in [k for k, d in self.seen.items() if not d or now - d[-1] > self.window]:
                self.seen.pop(k, None)


def seed_sig(entry: dict) -> str:
    """Otisk toho, co seed o položce říká — změna = promítnout znovu."""
    fields = {k: entry.get(k) for k in ("kind", "title", "body", "state", "resolution",
                                        "created", "resolved", "who")}
    return hashlib.sha1(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def check_seed(raw: Any) -> list[dict]:
    """Zkontrolovaný seed: seznam položek se stálým `key`. Vadná položka se
    vynechá (se zápisem do logu), start aplikace kvůli ní nespadne."""
    items = raw.get("items") if isinstance(raw, dict) else raw
    out: list[dict] = []
    seen: set[str] = set()
    for e in items if isinstance(items, list) else []:
        try:
            key = clean_line(e["key"])
            kind, state = e.get("kind"), e.get("state", "nove")
            title = clean_line(e["title"])
            body = clean_text(e.get("body", ""))
            resolution = clean_text(e.get("resolution", ""))
            if (not key or key in seen or kind not in KINDS or state not in STATES
                    or not TITLE_MIN <= len(title) <= TITLE_MAX or len(body) > BODY_MAX
                    or len(resolution) > RESOLUTION_MAX):
                raise ValueError("neplatná hodnota")
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            log.warning("seed chyb a nápadů: položka vynechána (%s): %r", exc,
                        e.get("key") if isinstance(e, dict) else e)
            continue
        seen.add(key)
        out.append({"key": key, "kind": kind, "state": state, "title": title, "body": body,
                    "resolution": resolution, "created": e.get("created"),
                    "resolved": e.get("resolved"), "who": clean_line(e.get("who", ""))[:WHO_MAX]})
    return out


def read_seed(path: Path = SEED_FILE) -> list[dict]:
    """Seed ze souboru (blokuje — disk; ze smyčky jen přes vlákno)."""
    try:
        return check_seed(json.loads(Path(path).read_text(encoding="utf-8")))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        log.warning("seed chyb a nápadů se nepodařilo načíst z %s: %s", path, exc)
        return []


class IssueBook:
    """Položky v paměti; změny jdou na disk přes vlákno zapisovače `Store`."""

    def __init__(self, store: Any = None, label: Callable[[str, str], str] | None = None,
                 seed_path: Path | None = SEED_FILE, clock: Callable[[], float] = time.time,
                 mono: Callable[[], float] = time.monotonic) -> None:
        self.store = store
        self.label_fn = label
        self.seed_path = seed_path
        self.clock = clock
        self.items: dict[int, Issue] = {}
        self._next_id = 1
        self._next_comment = 1
        self._rates = {"create": _Rate(RATE_CREATE, RATE_WINDOW, mono),
                       "comment": _Rate(RATE_COMMENT, RATE_WINDOW, mono),
                       "plus": _Rate(RATE_PLUS, RATE_WINDOW, mono)}
        self.loaded = False

    # ---- načtení ----

    def load(self, rows: tuple | None = None, seed: Iterable[dict] | None = None) -> "IssueBook":
        """Řádky `Store.all_issues()` a seed; bez argumentů je přečte samo —
        synchronně (testy, nástroje). Aplikace volá `aload`."""
        if rows is None:
            rows = self.store.all_issues() if self.store is not None else ([], [], [])
        if seed is None:
            seed = read_seed(self.seed_path) if self.seed_path is not None else []
        items, comments, plus = rows
        for (iid, skey, ssig, kind, title, body, state, resolution, resolved_at, author, who,
             created, updated) in items:
            if kind not in KINDS or state not in STATES:
                continue
            self.items[int(iid)] = Issue(
                int(iid), kind, title, body, state, resolution or "", resolved_at, author or "",
                who or "", float(created), float(updated), skey or None, ssig or None)
        for cid, iid, author, who, body, ts in comments:
            it = self.items.get(int(iid))
            if it is not None:
                it.comments.append({"id": int(cid), "author": author or "", "who": who or "",
                                    "body": body, "ts": float(ts)})
            self._next_comment = max(self._next_comment, int(cid) + 1)
        for iid, voter, who, ts in plus:
            it = self.items.get(int(iid))
            if it is not None and voter:
                it.plus[voter] = (who or "", float(ts))
        if self.items:
            self._next_id = max(self._next_id, max(self.items) + 1)
        self.loaded = True
        self.apply_seed(seed)
        return self

    async def aload(self) -> "IssueBook":
        """Načtení mimo hlavní smyčku: state.db i soubor seedu čtou vlákna."""
        aread = getattr(self.store, "aread", None)
        rows = await aread("all_issues") if aread is not None else None
        seed = await asyncio.to_thread(read_seed, self.seed_path) if self.seed_path is not None else []
        self.load(rows, seed)
        return self

    def apply_seed(self, seed: Iterable[dict]) -> int:
        """Založí / aktualizuje položky ze seedu podle klíče; vrací počet změn.
        Komentáře a +1 zůstávají, jak jsou."""
        by_key = {it.seed_key: it for it in self.items.values() if it.seed_key}
        now = self.clock()
        changed = 0
        for e in seed:
            sig = seed_sig(e)
            it = by_key.get(e["key"])
            if it is not None and it.seed_sig == sig:
                continue
            created = parse_date(e.get("created"))
            resolved = parse_date(e.get("resolved")) if e["resolution"] else None
            if it is None:
                it = Issue(self._next_id, e["kind"], e["title"], e["body"], author="",
                           who=e.get("who") or "", created=created or now, seed_key=e["key"])
                self._next_id += 1
                self.items[it.id] = it
            else:
                it.kind, it.title, it.body = e["kind"], e["title"], e["body"]
                if e.get("who"):
                    it.who = e["who"]
                if created:
                    it.created = created
            if e["resolution"]:
                if resolved or e["resolution"] != it.resolution or it.resolved_at is None:
                    it.resolved_at = resolved or now
            else:
                it.resolved_at = None
            it.state, it.resolution = e["state"], e["resolution"]
            it.seed_sig, it.updated = sig, now
            self._save(it)
            changed += 1
        return changed

    # ---- pomocné ----

    def _save(self, it: Issue) -> None:
        if self.store is not None:
            self.store.save_issue(it.row())

    def _name(self, cid: str, who: str) -> str:
        if self.label_fn is not None:
            try:
                return self.label_fn(cid, who)
            except Exception:
                log.exception("jméno k hlášení se nepodařilo určit")
        return who or "Host"

    def _ready(self) -> None:
        if not self.loaded:
            raise IssueError("Chyby a nápady se ještě načítají — zkus to za chvilku.", 503)

    def _get(self, iid: Any) -> Issue:
        self._ready()
        try:
            it = self.items.get(int(iid))
        except (TypeError, ValueError):
            it = None
        if it is None:
            raise IssueError("Takovou položku tu nemám.", 404)
        return it

    def _brake(self, what: str, cid: str, ip: str) -> None:
        r = self._rates[what]
        if r.full("c:" + cid) or (ip and r.full("i:" + ip, RATE_IP_FACTOR)):
            raise IssueError("Moc příspěvků najednou — zkus to prosím za pár minut.", 429)

    def _note(self, what: str, cid: str, ip: str) -> None:
        r = self._rates[what]
        r.note("c:" + cid)
        if ip:
            r.note("i:" + ip)

    def _make_room(self) -> None:
        """Plno: vypadne nejdéle neměněná uzavřená položka od kolegů (seed nikdy)."""
        if len(self.items) < MAX_ITEMS:
            return
        closed = [it for it in self.items.values() if it.state not in OPEN_STATES and not it.seed_key]
        if not closed:
            raise IssueError("Seznam je plný — než přibude další, musí správce něco uzavřít.", 429)
        old = min(closed, key=lambda it: (it.updated, it.id))
        self._drop(old)

    def _drop(self, it: Issue) -> None:
        self.items.pop(it.id, None)
        if self.store is not None:
            self.store.delete_issue(it.id)

    # ---- co vidí web ----

    def public(self, it: Issue, viewer: str = "", full: bool = False) -> dict:
        out: dict[str, Any] = {
            "id": it.id, "kind": it.kind, "title": it.title, "state": it.state,
            "open": it.state in OPEN_STATES,
            "who": self._name(it.author, it.who) if (it.author or it.who) else "",
            "created": it.created, "updated": it.updated,
            "plus": len(it.plus), "plus_mine": bool(viewer) and viewer in it.plus,
            "comments": len(it.comments), "resolved": bool(it.resolution),
            "resolved_at": it.resolved_at if it.resolution else None,
            "mine": bool(viewer) and viewer == it.author, "seed": bool(it.seed_key),
        }
        if not full:
            out["excerpt"] = excerpt(it.body)
            return out
        out["body"] = it.body
        out["resolution"] = it.resolution
        out["comment_list"] = [
            {"id": c["id"], "who": self._name(c["author"], c["who"]), "body": c["body"],
             "at": c["ts"], "mine": bool(viewer) and viewer == c["author"]}
            for c in it.comments]
        out["plus_by"] = [self._name(v, w) for v, (w, _) in sorted(it.plus.items(), key=lambda x: x[1][1])]
        return out

    def counts(self) -> dict:
        by_state = {s: 0 for s in STATES}
        by_kind = {k: 0 for k in KINDS}
        for it in self.items.values():
            by_state[it.state] += 1
            by_kind[it.kind] += 1
        n_open = sum(by_state[s] for s in OPEN_STATES)
        return {"all": len(self.items), "open": n_open, "closed": len(self.items) - n_open,
                "states": by_state, "kinds": by_kind}

    def listing(self, viewer: str = "", kind: str = "", state: str = "", offset: Any = 0,
                limit: Any = PAGE_DEFAULT) -> dict:
        """Stránka seznamu, nejnovější první. `state`: jméno stavu, "open", "closed", "" = vše."""
        self._ready()
        if kind and kind not in KINDS:
            raise IssueError("Druh je „chyba“, nebo „napad“.", 400)
        if state and state not in STATES and state not in ("open", "closed"):
            raise IssueError("Neznámý stav.", 400)
        try:
            offset, limit = max(0, int(offset)), min(PAGE_MAX, max(0, int(limit)))
        except (TypeError, ValueError):
            raise IssueError("Stránkování chce čísla.", 400) from None

        def keep(it: Issue) -> bool:
            if kind and it.kind != kind:
                return False
            if state == "open":
                return it.state in OPEN_STATES
            if state == "closed":
                return it.state not in OPEN_STATES
            return not state or it.state == state

        found = sorted((it for it in self.items.values() if keep(it)),
                       key=lambda it: (it.created, it.id), reverse=True)
        page = found[offset: offset + limit]
        return {"items": [self.public(it, viewer) for it in page], "total": len(found),
                "offset": offset, "limit": limit, "more": offset + len(page) < len(found),
                "counts": self.counts()}

    def detail(self, iid: Any, viewer: str = "") -> dict:
        return self.public(self._get(iid), viewer, full=True)

    # ---- kolegové ----

    def create(self, cid: str, who: str, kind: Any, title: Any, body: Any = "", ip: str = "") -> dict:
        self._ready()
        if not isinstance(kind, str) or kind not in KINDS:
            raise IssueError("Vyber, jestli je to Chyba, nebo Nápad.", 400)
        title, body = clean_line(title), clean_text(body)
        if len(title) < TITLE_MIN:
            raise IssueError("Napiš prosím krátký název (aspoň 3 znaky).", 400)
        if len(title) > TITLE_MAX:
            raise IssueError(f"Název může mít nejvýš {TITLE_MAX} znaků.", 400)
        if len(body) > BODY_MAX:
            raise IssueError(f"Popis může mít nejvýš {BODY_MAX} znaků.", 400)
        self._brake("create", cid, ip)
        self._make_room()
        now = self.clock()
        it = Issue(self._next_id, kind, title, body, author=cid, who=who[:WHO_MAX],
                   created=now, updated=now)
        self._next_id += 1
        self.items[it.id] = it
        self._note("create", cid, ip)
        self._save(it)
        return self.public(it, cid, full=True)

    def comment(self, iid: Any, cid: str, who: str, body: Any, ip: str = "") -> dict:
        it = self._get(iid)
        body = clean_text(body)
        if not body:
            raise IssueError("Komentář je prázdný.", 400)
        if len(body) > COMMENT_MAX:
            raise IssueError(f"Komentář může mít nejvýš {COMMENT_MAX} znaků.", 400)
        if len(it.comments) >= MAX_COMMENTS or \
                sum(len(x.comments) for x in self.items.values()) >= MAX_COMMENTS_TOTAL:
            raise IssueError("Tady už je komentářů až dost — další se nevejde.", 429)
        self._brake("comment", cid, ip)
        now = self.clock()
        c = {"id": self._next_comment, "author": cid, "who": who[:WHO_MAX], "body": body, "ts": now}
        self._next_comment += 1
        it.comments.append(c)
        it.updated = now
        self._note("comment", cid, ip)
        if self.store is not None:
            self.store.save_issue_comment((c["id"], it.id, cid, c["who"], body, now))
        self._save(it)
        return self.public(it, cid, full=True)

    def plus(self, iid: Any, cid: str, who: str, on: Any = None, ip: str = "") -> dict:
        """„+1 taky to chci / taky se mi to děje": jedno na člověka, jde stáhnout.
        `on` None = přepnout."""
        it = self._get(iid)
        want = (cid not in it.plus) if on is None else bool(on)
        if want == (cid in it.plus):
            return self.public(it, cid, full=True)
        self._brake("plus", cid, ip)
        now = self.clock()
        if want:
            if len(it.plus) >= MAX_PLUS:
                raise IssueError("Tolik +1 už stačí — díky.", 429)
            it.plus[cid] = (who[:WHO_MAX], now)
        else:
            it.plus.pop(cid, None)
        self._note("plus", cid, ip)
        if self.store is not None:
            self.store.set_issue_plus(it.id, cid, who[:WHO_MAX], now, want)
        return self.public(it, cid, full=True)

    # ---- správce (PIN hlídá web) ----

    def admin_update(self, iid: Any, state: Any = None, resolution: Any = None) -> dict:
        """Stav a / nebo text „Jak to bylo vyřešeno" (prázdný text ho smaže)."""
        it = self._get(iid)
        if state is None and resolution is None:
            raise IssueError("Chybí nový stav nebo text řešení.", 400)
        if state is not None and (not isinstance(state, str) or state not in STATES):
            raise IssueError("Neznámý stav — platí: " + ", ".join(STATES) + ".", 400)
        now = self.clock()
        if resolution is not None:
            text = clean_text(resolution)
            if len(text) > RESOLUTION_MAX:
                raise IssueError(f"Text řešení může mít nejvýš {RESOLUTION_MAX} znaků.", 400)
            if text != it.resolution:
                it.resolution = text
                it.resolved_at = now if text else None
        if state is not None:
            it.state = state
        it.updated = now
        self._save(it)
        return self.public(it, "", full=True)

    def delete(self, iid: Any) -> dict:
        it = self._get(iid)
        if it.seed_key:
            # po startu by se ze seedu založila znovu
            raise IssueError("Tahle položka jde s kódem (seed) — zamítni ji, nebo ji odeber "
                             "z issues_seed.json.", 409)
        self._drop(it)
        return {"id": it.id, "title": it.title}

    def delete_comment(self, iid: Any, comment_id: Any) -> dict:
        it = self._get(iid)
        try:
            cid = int(comment_id)
        except (TypeError, ValueError):
            cid = -1
        keep = [c for c in it.comments if c["id"] != cid]
        if len(keep) == len(it.comments):
            raise IssueError("Takový komentář tu není.", 404)
        it.comments = keep
        if self.store is not None:
            self.store.delete_issue_comment(cid)
        return self.public(it, "", full=True)


def wire(app: Any, seed_path: Path | None = SEED_FILE, **kw: Any) -> IssueBook:
    """Chyby a nápady do aplikace (App.__init__); načtou se v run() přes `aload_quietly`."""
    wq = getattr(app, "wishes", None)

    def label(cid: str, who: str) -> str:
        name = (wq.name_for(cid, who) if wq is not None and cid else who) or "Host"
        return wq.censor.clean(name) if wq is not None else name

    book = IssueBook(getattr(app, "store", None), label=label, seed_path=seed_path, **kw)
    app.issues = book
    return book


async def aload_quietly(book: IssueBook) -> None:
    try:
        await book.aload()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("chyby a nápady se nepodařilo načíst — seznam začíná prázdný")
        book.loaded = True


# --------------------------------------------------------------------------
# příkazová řádka: python -m ytdj.issues …  (mluví s běžícím ytdj přes jeho web)
# --------------------------------------------------------------------------

def _default_url() -> str:
    import os

    env = os.environ.get("YTDJ_URL")
    if env:
        return env.rstrip("/")
    port = 8765
    try:
        import tomllib

        from .config import CONFIG_FILE

        with CONFIG_FILE.open("rb") as f:
            port = int(tomllib.load(f).get("web_port") or port)
    except (OSError, ValueError, TypeError):
        pass
    return f"http://127.0.0.1:{port}"


def _call(base: str, method: str, path: str, body: dict | None = None, pin: str = "") -> tuple[int, dict]:
    import urllib.error
    import urllib.request

    headers = {"Accept": "application/json", "User-Agent": "ytdj-issues-cli"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if pin:
        headers["X-YTDJ-PIN"] = pin
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {"error": f"HTTP {exc.code}"}


def _day(ts: Any) -> str:
    return time.strftime("%-d. %-m. %Y", time.localtime(ts)) if ts else ""


def _print_item(it: dict, out: Any) -> None:
    print(f"#{it['id']}  [{KINDS.get(it['kind'], it['kind'])} · {STATES.get(it['state'], it['state'])}]"
          f"  {it['title']}", file=out)
    print(f"    {it.get('who') or '—'}, {_day(it.get('created'))} · +1: {it.get('plus', 0)}"
          f" · komentářů: {it.get('comments', 0)}", file=out)
    if it.get("body"):
        print("", file=out)
        for ln in it["body"].splitlines():
            print("    " + ln, file=out)
    if it.get("resolution"):
        print(f"\n    Jak to bylo vyřešeno ({_day(it.get('resolved_at'))}):", file=out)
        for ln in it["resolution"].splitlines():
            print("    " + ln, file=out)
    for c in it.get("comment_list") or []:
        print(f"\n    — {c['who']}, {_day(c['at'])}:", file=out)
        for ln in c["body"].splitlines():
            print("      " + ln, file=out)


def main(argv: list[str] | None = None, out: Any = None) -> int:
    import argparse
    from urllib.parse import urlencode

    out = out or sys.stdout
    ap = argparse.ArgumentParser(
        prog="python -m ytdj.issues",
        description="Chyby a nápady z jukeboxu: výpis a zápis, jak to bylo vyřešeno.")
    ap.add_argument("--url", default=None, help="adresa webu ytdj (výchozí http://127.0.0.1:<web_port>)")
    ap.add_argument("--pin", default=None, help="PIN správce (výchozí: soubor admin-pin na tomhle stroji)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list", help="seznam položek")
    p.add_argument("--state", default="", choices=["", "open", "closed", *STATES])
    p.add_argument("--kind", default="", choices=["", *KINDS])
    p = sub.add_parser("show", help="jedna položka s komentáři")
    p.add_argument("id", type=int)
    p = sub.add_parser("resolve", help="změnit stav a zapsat, jak to bylo vyřešeno (chce PIN)")
    p.add_argument("id", type=int)
    p.add_argument("--state", choices=list(STATES), default=None)
    p.add_argument("--text", default=None, help="text „Jak to bylo vyřešeno“ (\"\" ho smaže)")
    args = ap.parse_args(argv)
    base = (args.url or _default_url()).rstrip("/")

    try:
        if args.cmd == "list":
            offset, shown = 0, 0
            while True:
                q = urlencode({"state": args.state, "kind": args.kind, "offset": offset, "limit": PAGE_MAX})
                status, data = _call(base, "GET", "/api/issues?" + q)
                if status != 200:
                    print(f"Chyba {status}: {data.get('error', '')}", file=sys.stderr)
                    return 1
                for it in data["items"]:
                    mark = " ✔" if it.get("resolved") else ""
                    print(f"#{it['id']:<4} {KINDS[it['kind']]:<6} {STATES[it['state']]:<17} "
                          f"+{it['plus']:<3} {it['title']}{mark}", file=out)
                shown += len(data["items"])
                offset += len(data["items"])
                if not data.get("more") or not data["items"]:
                    break
            c = data["counts"]
            print(f"— {shown} z {c['all']} (otevřených {c['open']})", file=out)
            return 0
        if args.cmd == "show":
            status, data = _call(base, "GET", f"/api/issues/{args.id}")
            if status != 200:
                print(f"Chyba {status}: {data.get('error', '')}", file=sys.stderr)
                return 1
            _print_item(data["item"], out)
            return 0
        # resolve
        if args.state is None and args.text is None:
            print("Zadej --state a / nebo --text.", file=sys.stderr)
            return 2
        pin = args.pin
        if pin is None:
            from . import adminpin

            pin = adminpin.read_pin()
        if not pin:
            print("Chybí PIN správce: zadej --pin (je na displeji jukeboxu: Síť).", file=sys.stderr)
            return 2
        body: dict[str, Any] = {}
        if args.state is not None:
            body["state"] = args.state
        if args.text is not None:
            body["resolution"] = args.text
        status, data = _call(base, "POST", f"/api/issues/{args.id}/resolve", body, pin=pin)
        if status != 200:
            print(f"Chyba {status}: {data.get('error', '')}", file=sys.stderr)
            return 1
        _print_item(data["item"], out)
        return 0
    except OSError as exc:  # spojení odmítnuto, časový limit
        print(f"Nedovolal jsem se na {base}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
