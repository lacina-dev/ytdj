"""Jeden člověk na všech adresách jukeboxu (POZADAVKY #69, FUNKCE F-NICK-07…).

Web jukeboxu je vidět pod několika adresami téhož stroje (jméno v síti, druhé
jméno, číselná adresa, s portem i bez). Prohlížeč má pro každou z nich vlastní
úložiště, takže id klienta — to, podle čeho jukebox pozná člověka — by bylo na
každé adrese jiné. Tenhle modul drží serverovou půlku nápravy:

* `OwnOrigins` — seznam adres, které opravdu patří tomuhle stroji. Bere se
  ze stroje samotného (jeho adresy v síti, jména, která na něm vedou zpět na
  něj, a porty, na kterých web odpovídá), nikdy z hlavičky Host. Jen mezi
  těmito adresami smí stránka účet předávat.
* `Identity` — krátké jednorázové kódy pro předání účtu mezi adresami
  (id klienta se nikdy nepíše do adresy), spojení dvou účtů do staršího
  (`merge`) a paměť, které staré id vede na které (`alias`).

Všechno je v paměti; na kartu jde jen malý soubor a jen ve vlákně (jako
přezdívky). Hlavní smyčka tu na nic nečeká.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from . import telemetry
from .nicks import tag_of
from .panel import webaddr

log = logging.getLogger(__name__)

COOKIE = "ytdj_id"
COOKIE_AGE = 2 * 365 * 86400  # s — prohlížeč si účet pamatuje stejně dlouho jako dřív úložiště
CODE_TTL = 120.0  # s — kód pro předání účtu platí jen chvíli a jen jednou
CODES_MAX = 300
KEEP = 3000  # nejvýš tolik starých id / záznamů (nejstarší vypadnou)
TOKENS_MAX = 20
LOOK_SCHEDULE = (0.0, 20.0, 90.0)  # po startu ještě nabíhá síť a přesměrování portu 80
LOOK_EVERY = 300.0


def origin_of(host: str, port: int) -> str:
    host = host.strip().lower().rstrip(".")
    return f"http://{host}" if port == 80 else f"http://{host}:{port}"


def origin_from_host_header(value: str | None) -> str:
    """Hlavička Host → tvar jako `origin_of` ("" když nedává smysl). Jen k porovnání
    se seznamem vlastních adres — sama o sobě nic nedokazuje."""
    value = (value or "").strip().lower()
    if not value or any(c in value for c in "/@ \t\\"):
        return ""
    host, _, port = value.rpartition(":") if ":" in value else (value, "", "")
    if not host or not (port.isdigit() or port == ""):
        return ""
    return origin_of(host, int(port) if port else 80)


def own_ips() -> list[str]:
    """IPv4 adresy tohoto stroje v síti (blokuje krátce — volat ve vlákně)."""
    ips: list[str] = []
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                             capture_output=True, text=True, timeout=3).stdout
        for line in out.splitlines():
            parts = line.split()
            if "inet" in parts:
                ip = parts[parts.index("inet") + 1].split("/")[0]
                if ip not in ips:
                    ips.append(ip)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        pass
    first = webaddr.own_ip()
    if first and first not in ips:
        ips.insert(0, first)
    return ips


def points_here(name: str, ips: Iterable[str]) -> bool:
    """Vede jméno (na tomhle stroji) zpátky na tenhle stroj?"""
    try:
        found = {info[4][0] for info in socket.getaddrinfo(name, None, socket.AF_INET, socket.SOCK_STREAM)}
    except (OSError, UnicodeError):
        return False
    mine = set(ips)
    return bool(found) and all(ip in mine or ip.startswith("127.") for ip in found)


class OwnOrigins:
    """Adresy (http://jméno[:port]) tohoto jukeboxu, zjištěné ze stroje samotného.

    `list()` je levné čtení z paměti; zjišťování (`look`) běží ve vlastním
    vlákně a může pár vteřin trvat (jména v síti). `extra` jsou adresy, které
    přidal ten, kdo server pouští (testy, neobvyklé jméno v síti)."""

    def __init__(self, port: int, alias: str = webaddr.ALIAS, hostname: str | None = None,
                 extra: Iterable[str] = (),
                 own_ips: Callable[[], list[str]] = own_ips,
                 points_here: Callable[[str, Iterable[str]], bool] = points_here,
                 answers: Callable[[str, int], bool] = webaddr.answers) -> None:
        self.port = int(port)
        self.alias = alias
        self.hostname = socket.gethostname() if hostname is None else hostname
        self.extra = tuple(extra)
        self._own_ips, self._points_here, self._answers = own_ips, points_here, answers
        self._found: tuple[str, ...] = ()
        self._thread: threading.Thread | None = None
        self.looks = 0

    def look(self) -> tuple[str, ...]:
        ips = self._own_ips()
        names = []
        for name in (self.alias, f"{self.hostname}.local" if self.hostname else ""):
            name = name.strip().lower()
            if name and name not in names and self._points_here(name, ips):
                names.append(name)
        ports = [self.port]
        if self.port != 80 and ips and self._answers(ips[0], 80):
            ports.insert(0, 80)  # jádro přesměrovává port 80 na web (packaging/ytdj-port80.service)
        self._found = tuple(origin_of(h, p) for h in names + ips for p in ports)
        self.looks += 1
        return self.list()

    def list(self) -> tuple[str, ...]:
        out = list(self._found)
        for o in self.extra:
            if o not in out:
                out.append(o)
        return tuple(out)

    def ensure_started(self) -> None:
        """První volání pustí vlákno, které seznam drží čerstvý. Neblokuje."""
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="ytdj-origins", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        last = 0.0
        for at in LOOK_SCHEDULE:
            time.sleep(max(0.0, at - last))
            last = at
            self._look_quietly()
        while True:
            time.sleep(LOOK_EVERY)
            self._look_quietly()

    def _look_quietly(self) -> None:
        before = self._found
        try:
            self.look()
        except Exception:  # vlákno nesmí umřít
            log.debug("zjišťování vlastních adres selhalo", exc_info=True)
            return
        if self._found != before:
            log.info("adresy jukeboxu pro předání účtu: %s", ", ".join(self._found) or "žádné")


class Identity:
    """Kódy pro předání účtu, spojování účtů a paměť starých id."""

    def __init__(self, app: Any, path: Path | None = None,
                 clock: Callable[[], float] = time.time,
                 mono: Callable[[], float] = time.monotonic) -> None:
        self.app = app
        self.path = path
        self.clock, self.mono = clock, mono
        self.instance = secrets.token_hex(8)  # pozná se podle něj tentýž běžící jukebox
        self.alias: dict[str, str] = {}  # staré id → id, do kterého se spojilo
        self.since: dict[str, float] = {}  # id → kdy ho jukebox viděl poprvé
        self.linked: dict[str, list[str]] = {}  # id → adresy, mezi kterými už je účet předaný
        self.gone: dict[str, str] = {}  # staré id, které bylo účtem → jeho přezdívka (pro větu člověku)
        self.codes: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._gen = self._written = 0
        self._writer: threading.Thread | None = None
        # Soubor se čte ve vlákně (karta je pomalá); co přečetl, převezme hlavní
        # smyčka při nejbližším použití (`_take_loaded`). Do té doby se neukládá.
        self._loaded = threading.Event()
        self._pending: dict | None = None
        self._save_wanted = False
        if self.path is None:
            self._loaded.set()
        else:
            threading.Thread(target=self._load, daemon=True, name="ytdj-identity-load").start()

    # ---- soubor (malý; čtení i zápis jen ve vlákně) ----

    def _load(self) -> None:
        got: dict = {"alias": {}, "since": {}, "gone": {}, "linked": {}}
        try:
            assert self.path is not None
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in (raw.get("alias") or {}).items():
                    if isinstance(k, str) and isinstance(v, str):
                        got["alias"][k] = v
                for k, v in (raw.get("since") or {}).items():
                    if isinstance(k, str) and isinstance(v, (int, float)):
                        got["since"][k] = float(v)
                for k, v in (raw.get("gone") or {}).items():
                    if isinstance(k, str) and isinstance(v, str):
                        got["gone"][k] = v
                for k, v in (raw.get("linked") or {}).items():
                    if isinstance(k, str) and isinstance(v, list):
                        got["linked"][k] = [str(o) for o in v][:20]
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            log.warning("spojené účty se nepodařilo načíst z %s: %s", self.path, exc)
        finally:
            self._pending = got
            self._loaded.set()

    def wait_loaded(self, timeout: float = 5.0) -> bool:
        """Počká na přečtení souboru (vypínání, testy) — nevolat z hlavní smyčky."""
        return self._loaded.wait(timeout)

    def _take_loaded(self) -> None:
        got, self._pending = self._pending, None
        if got is None:
            return
        for name in ("alias", "since", "gone", "linked"):
            mine = getattr(self, name)
            for k, v in got[name].items():
                mine.setdefault(k, v)
        if self._save_wanted:
            self._save_wanted = False
            self.save()

    def save(self) -> None:
        if self.path is None:
            return
        self._take_loaded()
        if not self._loaded.is_set():
            self._save_wanted = True  # soubor ještě není přečtený — nepřepsat ho neúplným
            return
        for d in (self.alias, self.since, self.linked, self.gone):
            while len(d) > KEEP:
                d.pop(next(iter(d)))
        self._gen += 1
        text = json.dumps({"alias": self.alias, "since": self.since, "linked": self.linked,
                           "gone": self.gone}, ensure_ascii=False)
        self._writer = threading.Thread(target=self._write, args=(self._gen, text), daemon=True,
                                        name="ytdj-identity")
        self._writer.start()

    def _write(self, gen: int, text: str) -> None:
        assert self.path is not None
        with self._lock:
            if gen <= self._written:
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(text, encoding="utf-8")
                os.replace(tmp, self.path)
                self._written = gen
            except OSError:
                log.warning("spojené účty se nepodařilo uložit", exc_info=True)

    def flush(self, timeout: float = 5.0) -> None:
        w = self._writer
        if w is not None and w.is_alive():
            w.join(timeout)

    # ---- kdo je kdo ----

    @property
    def nicks(self) -> Any:
        return getattr(getattr(self.app, "wishes", None), "nicks", None)

    def resolve(self, cid: str) -> str:
        """Staré (spojené) id → platné id."""
        self._take_loaded()
        for _ in range(8):
            nxt = self.alias.get(cid)
            if not nxt or nxt == cid:
                break
            cid = nxt
        return cid

    def nick(self, cid: str) -> str:
        nicks = self.nicks
        return nicks.get(cid) if nicks is not None and cid else ""

    def first_seen(self, cid: str) -> float | None:
        """Nejstarší stopa účtu: první hlas nebo import, jinak kdy ho jukebox poznal."""
        seen = []
        votes = getattr(self.app, "votes", None)
        if votes is not None and hasattr(votes, "first_seen"):
            ts = votes.first_seen(cid)
            if ts is not None:
                seen.append(ts)
        if cid in self.since:
            seen.append(self.since[cid])
        return min(seen) if seen else None

    def known(self, cid: str) -> bool:
        """Je to účet (má přezdívku nebo hlasy), ne jen čerstvě vymyšlené id?"""
        if not cid:
            return False
        if self.nick(cid):
            return True
        votes = getattr(self.app, "votes", None)
        return bool(votes is not None and hasattr(votes, "first_seen") and votes.first_seen(cid) is not None)

    def note_seen(self, cid: str) -> None:
        """Zapamatuje, odkdy účet známe (jen u účtů, ne u prázdných id)."""
        if cid and cid not in self.since and self.known(cid):
            rec = getattr(self.nicks, "data", {}).get(cid) if self.nicks is not None else None
            at = float((rec or {}).get("at") or 0)
            now = self.clock()
            self.since[cid] = min(at, now) if at > 0 else now
            self.save()

    def me(self, cid: str) -> dict:
        return {"client": cid, "nick": self.nick(cid), "tag": tag_of(cid)}

    # ---- spojení účtů ----

    def merge(self, cids: Iterable[str], here: str = "") -> tuple[str, dict | None]:
        """Z několika id téhož prohlížeče udělá jedno. Vrací (platné id, poznámka pro člověka).

        První v `cids` je id adresy, na které člověk právě je. Vyhrává nejstarší
        účet (první hlas / import / první setkání); prázdná id (bez přezdívky
        a hlasů) nikdy. Hlasy, importy a čekající přání mladšího účtu se
        připíšou staršímu; kde hlasovaly oba, platí hlas staršího."""
        order: list[str] = []
        raw_local = ""
        for cid in cids:
            raw_local = raw_local or cid
            cid = self.resolve(cid)
            if cid and cid not in order:
                order.append(cid)
        if not order:
            return "", None
        local = order[0]
        accounts = [c for c in order if self.known(c)]
        if not accounts:
            winner = local
        else:
            for c in accounts:
                self.note_seen(c)
            winner = min(accounts, key=lambda c: (self.first_seen(c) or float("inf"), order.index(c)))
        note: dict | None = None
        changed = False
        for loser in order:
            if loser == winner:
                continue
            was_account = loser in accounts
            if was_account:
                moved = self._absorb(loser, winner)
                self.gone[loser] = moved["nick"]
                if loser == local:
                    note = {"kind": "merged", "dropped": moved["nick"], "kept": self.nick(winner)}
                telemetry.event("identity.merge", loser=loser[-6:], winner=winner[-6:], here=here or None,
                                votes=moved["moved"], dropped=moved["dropped"], imports=moved["imports"],
                                wishes=moved["wishes"])
                log.info("účty spojeny: …%s → …%s (%s hlasů, %s importů, %s přání)", loser[-6:],
                         winner[-6:], moved["moved"], moved["imports"], moved["wishes"])
            self.alias[loser] = winner
            self.since.pop(loser, None)
            for o in self.linked.pop(loser, []):
                self._link(winner, o)
            changed = True
        if note is None and raw_local in self.gone and raw_local != winner:
            # tahle adresa ještě drží id účtu, který se mezitím spojil jinde
            note = {"kind": "merged", "dropped": self.gone[raw_local], "kept": self.nick(winner)}
        if note is None and winner in accounts and raw_local != winner:
            note = {"kind": "adopted", "kept": self.nick(winner)}
        if changed:
            self.save()
        return winner, note

    def _absorb(self, loser: str, winner: str) -> dict:
        out = {"moved": 0, "dropped": 0, "imports": 0, "wishes": 0, "nick": self.nick(loser)}
        votes = getattr(self.app, "votes", None)
        if votes is not None and hasattr(votes, "merge_voter"):
            try:
                out.update(votes.merge_voter(loser, winner))
            except Exception:
                log.exception("hlasy se při spojení účtů nepodařilo převést")
        nicks = self.nicks
        if nicks is not None:
            if not nicks.get(winner) and out["nick"]:
                nicks.set(winner, out["nick"])
            if nicks.data.pop(loser, None) is not None:
                nicks._dirty = True
                nicks.save()
        wq = getattr(self.app, "wishes", None)
        name = self.nick(winner)
        for w in list(getattr(wq, "wishes", ()) or ()):
            if getattr(w, "cid", "") == loser:
                w.cid = winner
                if name:
                    w.who = name
                out["wishes"] += 1
        return out

    def _link(self, cid: str, origin: str) -> None:
        if cid and origin:
            lst = self.linked.setdefault(cid, [])
            if origin not in lst:
                lst.append(origin)
                del lst[:-20]

    # ---- předání mezi adresami: jednorázové kódy ----

    def offer(self, cid: str, origin: str, extras: dict | None = None) -> str:
        """Kód za účet na adrese `origin` ("" = na té adrese žádný účet není)."""
        now = self.mono()
        for code in [c for c, rec in self.codes.items() if rec["exp"] < now]:
            del self.codes[code]
        while len(self.codes) >= CODES_MAX:
            self.codes.pop(next(iter(self.codes)))
        cid = self.resolve(cid) if cid else ""
        code = secrets.token_urlsafe(18)
        self.codes[code] = {"cid": cid if self.known(cid) else "", "origin": origin,
                            "extras": extras or {}, "exp": now + CODE_TTL}
        return code

    def redeem(self, local: str, codes: Iterable[Any], here: str = "") -> dict:
        """Vyzvedne kódy (každý jen jednou) a spojí jejich účty s místním id."""
        now = self.mono()
        cids, origins, extras = [local], [here] if here else [], {}
        for code in list(codes)[:12]:
            rec = self.codes.pop(str(code), None)
            if rec is None or rec["exp"] < now:
                continue
            if rec["origin"]:
                origins.append(rec["origin"])
            if rec["cid"]:
                cids.append(rec["cid"])
                for k, v in rec["extras"].items():
                    extras.setdefault(k, v)
        winner, note = self.merge(cids, here=here)
        for o in origins:
            self._link(winner, o)
        if winner:
            self.save()
        out = self.me(winner)
        out.update(note=note, extras=extras, linked=list(self.linked.get(winner, [])))
        return out


def clean_extras(raw: Any) -> dict:
    """Co smí jít s účtem na druhou adresu: vzhled a značky vlastních čekajících přání."""
    out: dict[str, Any] = {}
    if not isinstance(raw, dict):
        return out
    if raw.get("theme") in ("light", "dark"):
        out["theme"] = raw["theme"]
    tokens = raw.get("tokens")
    if isinstance(tokens, dict):
        keep = {}
        for k, v in list(tokens.items())[:TOKENS_MAX]:
            if (isinstance(k, str) and len(k) <= 64 and isinstance(v, dict)
                    and isinstance(v.get("token"), str) and len(v["token"]) <= 128):
                keep[k] = {"token": v["token"], "at": v.get("at") if isinstance(v.get("at"), (int, float)) else 0}
        if keep:
            out["tokens"] = keep
    return out


def wire(app: Any, port: int = 8765) -> Identity:
    """Zapojí identitu do aplikace (volá web při startu; soubor vedle přezdívek)."""
    nicks = getattr(getattr(app, "wishes", None), "nicks", None)
    base = getattr(nicks, "path", None)
    ident = Identity(app, Path(base).parent / "identity.json" if base else None)
    ident.origins = OwnOrigins(port)  # type: ignore[attr-defined]
    app.identity = ident
    return ident
