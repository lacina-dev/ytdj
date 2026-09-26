"""Přezdívky: kdo je kdo v kanceláři.

Web se při první návštěvě zeptá na přezdívku a server si ji pamatuje k id
klienta (náhodné id z prohlížeče). Jméno u přání, u "přeskočil" i na displeji
je pak všude stejné — nezáleží na tom, co zrovna poslala stránka. Identitu
(spravedlnost, nahrazování přání) dál určuje id klienta, přezdívka je jen
popisek; dva lidé se stejnou přezdívkou jsou pořád dva lidé (web jen
upozorní, ať si ji rozliší).

Zápis na SD kartu jen při změně přezdívky; "naposledy viděn" se ukládá
nejvýš jednou za SAVE_EVERY.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

NICK_MIN = 2
NICK_MAX = 20
KEEP = 500  # nejvýš tolik klientů (nejdéle nevidění vypadnou)
SAVE_EVERY = 600.0  # s — "naposledy viděn" na kartu nejvýš takhle často
SHARED_WINDOW = 14 * 86400  # s — přezdívku "používá někdo jiný", když ho tu byl vidět

# jména, která patří systému — u přání by mátla ("displej" = kdokoli u displeje)
RESERVED = {"displej", "terminal", "host", "dj", "nekdo", "ytdj", "admin", "system", "ty", "tvoje"}
# písmena (i česká), číslice, mezera a pár znaků do jmen ("Petr K.", "Jana-Marie", "O'Neil")
_ALLOWED = re.compile(r"^[^\W_][\w .\-'&+]*$")


class NickError(ValueError):
    """Přezdívka neprošla — text je věta pro člověka."""


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def clean_nick(raw: Any, censor: Any = None) -> str:
    """Ořízne a zkontroluje přezdívku; NickError s vlídnou větou, když neprojde."""
    nick = unicodedata.normalize("NFC", " ".join(str(raw or "").split()))
    if len(nick) < NICK_MIN:
        raise NickError(f"Přezdívka potřebuje aspoň {NICK_MIN} znaky.")
    if len(nick) > NICK_MAX:
        raise NickError(f"Přezdívka může mít nejvýš {NICK_MAX} znaků.")
    if not any(c.isalpha() for c in nick):
        raise NickError("Přezdívka potřebuje aspoň jedno písmeno.")
    if not _ALLOWED.match(nick):
        raise NickError("Použij prosím písmena, číslice a mezery (třeba „Petr K.“).")
    if fold(nick) in RESERVED or fold(nick).startswith("host "):
        raise NickError("Tohle jméno patří jukeboxu — vyber si prosím jiné.")
    if censor is not None and getattr(censor, "enabled", True) and censor.clean(nick) != nick:
        raise NickError("Tahle přezdívka by v kanceláři neobstála — zkus prosím jinou.")
    return nick


def tag_of(cid: str) -> str:
    """Značka klienta pro "tvoje" ve veřejném seznamu přání — z ní id nevyčteš."""
    return hashlib.sha1(("ytdj-nick:" + cid).encode()).hexdigest()[:10] if cid else ""


class Nicks:
    """id klienta → {"nick", "at" (naposledy viděn, epoch)}; volitelně v souboru."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.data: dict[str, dict] = {}
        self._saved_at = 0.0
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("přezdívky se nepodařilo načíst z %s: %s", self.path, exc)
            return
        if isinstance(raw, dict):
            for cid, rec in raw.items():
                if isinstance(rec, dict) and isinstance(rec.get("nick"), str) and rec["nick"]:
                    self.data[str(cid)] = {"nick": rec["nick"][:NICK_MAX],
                                           "at": float(rec.get("at") or 0)}

    def save(self, force: bool = False) -> None:
        if self.path is None or not (self._dirty or force):
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
            self._dirty = False
            self._saved_at = time.monotonic()
        except OSError:
            log.warning("přezdívky se nepodařilo uložit", exc_info=True)

    def get(self, cid: str) -> str:
        rec = self.data.get(cid) if cid else None
        return rec["nick"] if rec else ""

    def touch(self, cid: str) -> None:
        rec = self.data.get(cid) if cid else None
        if rec is None:
            return
        rec["at"] = time.time()
        self._dirty = True
        if time.monotonic() - self._saved_at > SAVE_EVERY:
            self.save()

    def shared(self, cid: str, nick: str) -> bool:
        """Používá stejnou přezdívku (bez ohledu na velikost a diakritiku) někdo jiný?"""
        key, now = fold(nick), time.time()
        return any(c != cid and fold(r["nick"]) == key and now - r["at"] < SHARED_WINDOW
                   for c, r in self.data.items())

    def set(self, cid: str, nick: str) -> bool:
        """Uloží (už zkontrolovanou) přezdívku; vrátí, jestli ji používá i někdo jiný."""
        shared = self.shared(cid, nick)
        self.data[cid] = {"nick": nick, "at": time.time()}
        if len(self.data) > KEEP:
            for old in sorted(self.data, key=lambda c: self.data[c]["at"])[: len(self.data) - KEEP]:
                self.data.pop(old, None)
        self._dirty = True
        self.save()
        return shared

    def recent(self, seconds: float = 12 * 3600) -> list[str]:
        """Přezdívky lidí viděných v posledních hodinách, nejčerstvější první."""
        now = time.time()
        out: list[str] = []
        for rec in sorted(self.data.values(), key=lambda r: -r["at"]):
            if now - rec["at"] > seconds:
                break
            if rec["nick"] not in out:
                out.append(rec["nick"])
        return out
