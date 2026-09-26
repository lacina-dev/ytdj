"""Když Codex nejede: jistič, poctivé zprávy a DJ bez modelu.

26. 9. na Pi: Codex vracel 401 a posluchač viděl syrové "(Codex selhal: Codex
není přihlášen: unexpected status 401 Unauthorized: Incorrect API key
provided: sk-svcac…)". A každé přání přitom znovu čekalo na model — při
limitu (429) až 240 s pod jediným zámkem Codexu.

Jistič (`Breaker`):
  - po chybě přihlášení / limitu / sítě se "rozpojí": model se nezkouší,
    dokud neuplyne čekání (roste: síť 30 s → 5 min, limit 5 → 30 min,
    přihlášení 10 min — a hned, jakmile se změní ~/.codex/auth.json),
  - pak pustí jeden zkušební tah; povede-li se, je zase zapnutý,
  - `status()` je malé API pro web a panel (viz docstring).

Bez modelu (`local_mood`) umí DJ čipy klidnější / živější / jen česky /
víc takového / něco jiného / překvap mě — z toho, co hrálo, a z nálad
YouTube Music.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .intent import norm

# ---- druh výpadku ----

LOGIN, LIMIT, NETWORK, ERROR = "login", "limit", "network", "error"
# Tah nestihl rozpočet (náš timeout). NENÍ to síť: 26. 9. 9:23 studený start
# app-serveru (~9 s) + model (~14 s) přetáhl 25 s a posluchač četl "síť".
SLOW = "slow"

MESSAGES = {
    LOGIN: "DJ teď nemá přístup k mozku (Codex není přihlášený). Hraju dál a na "
           "jednoduchá přání (klidnější, živější, jen česky, víc takového, něco "
           "jiného, konkrétní interpret) stačím i sám. Správce: packaging/rpi/codex-login.sh.",
    LIMIT: "DJ vyčerpal limit předplatného — chvíli si oddechne. Hraju dál a jednoduchá "
           "přání (klidnější, živější, jen česky, víc takového, něco jiného, konkrétní "
           "interpret) zvládnu i bez něj.",
    NETWORK: "DJ se teď nedovolá ke svému mozku (síť). Hraju dál; jednoduchá přání "
             "zvládnu i sám, na složitější to zkusím za chvíli znovu.",
    ERROR: "DJ má teď potíž s mozkem. Hraju dál; jednoduchá přání zvládnu i sám.",
    SLOW: "DJ nestihl odpovědět včas — zkus to prosím znovu, hudba hraje dál. "
          "Jednoduchá přání (interpret, klidnější, živější) zvládnu i bez něj.",
}

_LOGIN = re.compile(r"\b(401|403)\b|unauthori[sz]ed|not logged in|není přihlášen|login|"
                    r"incorrect api key|refresh token", re.I)
_LIMIT = re.compile(r"\b429\b|rate.?limit|usage limit|quota|too many requests|limit reached", re.I)
_NETWORK = re.compile(r"timeout|timed out|connection|network|dns|resolve host|"
                      r"\b50[234]\b|unreachable|reset by peer|stream disconnected", re.I)
# náš vlastní strop tahu (asyncio.timeout, TURN_TIMEOUT) — model nestihl, síť jela
_SLOW = re.compile(r"neodpověděl do|nestihl", re.I)


def classify(exc_or_text: object) -> str | None:
    """Druh výpadku z chyby Codexu; None = jednorázová chyba (neotevírat hned)."""
    text = str(exc_or_text)
    if isinstance(exc_or_text, TimeoutError) or _SLOW.search(text):
        return SLOW  # vypršel NÁŠ rozpočet — o síti to nic neříká
    if _LOGIN.search(text):
        return LOGIN
    if _LIMIT.search(text):
        return LIMIT
    if _NETWORK.search(text):
        return NETWORK
    return None


# čekání před dalším pokusem podle počtu neúspěchů za sebou (s)
BACKOFF = {
    LOGIN: [600, 1800, 3600],
    LIMIT: [300, 900, 1800],
    NETWORK: [30, 120, 300],
    ERROR: [60, 300, 600],
    SLOW: [30, 120, 300],
}
ERRORS_TO_OPEN = 2  # jednorázové chyby (bez druhu) — kolik za sebou otevře jistič


class CodexOffline(RuntimeError):
    """Model teď nejde použít; `str()` je vlídná zpráva pro posluchače."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or MESSAGES.get(reason, MESSAGES[ERROR]))


@dataclass
class Breaker:
    """Jistič před Codexem. Čas je `time.time()` (kvůli API), testy dosadí `now`."""

    auth_file: Path = Path.home() / ".codex" / "auth.json"
    reason: str | None = None
    since: float = 0.0
    retry_at: float = 0.0
    failures: int = 0
    errors_in_row: int = 0
    last_error: str = ""
    _auth_mtime: float | None = None

    @property
    def offline(self) -> bool:
        return self.reason is not None

    def _auth_changed(self) -> bool:
        try:
            m = os.path.getmtime(self.auth_file)
        except OSError:
            return False
        return self._auth_mtime is not None and m != self._auth_mtime

    def allow(self, now: float | None = None) -> bool:
        """Smí se zkusit model? (zapnuto, nebo je čas na zkušební tah)"""
        if self.reason is None:
            return True
        now = time.time() if now is None else now
        if self.reason == LOGIN and self._auth_changed():
            return True  # správce se přihlásil znovu → zkusit hned
        return now >= self.retry_at

    def success(self) -> None:
        self.reason = None
        self.failures = 0
        self.errors_in_row = 0
        self.last_error = ""

    def failure(self, exc: object, now: float | None = None) -> str | None:
        """Zapíše neúspěch; vrací druh, když se jistič (znovu) otevřel."""
        now = time.time() if now is None else now
        reason = classify(exc)
        self.last_error = str(exc)[:300]
        if reason in (None, SLOW):
            # jeden pomalý tah (studený start) není výpadek — jistič až po druhém
            self.errors_in_row += 1
            if self.errors_in_row < ERRORS_TO_OPEN:
                return None
            reason = reason or ERROR
        if self.reason != reason:
            self.failures = 0
            self.since = now
        self.reason = reason
        steps = BACKOFF[reason]
        self.retry_at = now + steps[min(self.failures, len(steps) - 1)]
        self.failures += 1
        try:
            self._auth_mtime = os.path.getmtime(self.auth_file)
        except OSError:
            self._auth_mtime = None
        return reason

    def message(self) -> str:
        return MESSAGES.get(self.reason or ERROR, MESSAGES[ERROR])

    def status(self, now: float | None = None) -> dict:
        """Stav mozku DJ pro web/panel (`app.dj.status()["brain"]`).

        {"online": bool, "reason": "login"|"limit"|"network"|"error"|None,
         "since": epoch s|None, "retry_at": epoch s|None,
         "retry_in_s": int|None, "message": str|None}
        """
        now = time.time() if now is None else now
        if self.reason is None:
            return {"online": True, "reason": None, "since": None, "retry_at": None,
                    "retry_in_s": None, "message": None}
        return {
            "online": False,
            "reason": self.reason,
            "since": round(self.since),
            "retry_at": round(self.retry_at),
            "retry_in_s": max(0, int(self.retry_at - now)),
            "message": self.message(),
        }


# ---- DJ bez modelu ----

CALMER, LIVELIER, CZECH, MORE, OTHER, SURPRISE = (
    "calmer", "livelier", "czech", "more", "other", "surprise")

_LOCAL = [
    (re.compile(r"\b(klidnej\w*|klidnejsi\w*|klidn\w+|pomalej\w*|pomalejsi|uklidn\w*|zpomal\w*|"
                r"odpocin\w*|relax\w*|chill\w*|jemnej\w*|jemnejsi|calm\w*)\b"), CALMER),
    (re.compile(r"\b(zivej\w*|zivejsi|zivost\w*|rychlej\w*|rychlejsi|vesel\w*|energ\w*|"
                r"svizn\w*|nakopni|party|pařb\w*|parb\w*|tancov\w*|livel\w*|upbeat)\b"), LIVELIER),
    (re.compile(r"\b(jen|jenom|pouze)?\s*(cesk\w*|cesky|cz)\b"), CZECH),
    (re.compile(r"\b(vic|jeste|dalsi)\s+(neco\s+)?(takov\w*|podobn\w*)|\bv (tom|tomhle) duchu\b|"
                r"\bmore like (this|that)\b"), MORE),
    (re.compile(r"\bprekvap\w*|\bsurprise\b"), SURPRISE),
    (re.compile(r"\b(neco|dej|hraj|pust)?\s*(jin\w+)\b|\bzmen\w*\b|\bsomething else\b"), OTHER),
]

LOCAL_REPLY = {
    CALMER: "Beru to klidněji",
    LIVELIER: "Přidávám energii",
    CZECH: "Jen česky",
    MORE: "Víc takového",
    OTHER: "Zkusím něco jiného",
    SURPRISE: "Překvapím tě",
}

# nálady YouTube Music (kategorie "Moods & moments") pro klidnější / živější
MOOD_WORDS = {
    CALMER: ["chill", "relax", "odpočin", "klid", "calm", "sleep", "spánek", "romance"],
    LIVELIER: ["energize", "energi", "party", "večírek", "workout", "cvičen", "feel good",
               "dobrá nálada", "nálad"],
}

_CZ_CHARS = re.compile(r"[ěščřžůťďňĚŠČŘŽŮŤĎŇ]")


def local_mood(text: str) -> str | None:
    """Čip/nálada, kterou DJ zvládne bez modelu; jinak None.

    Jen krátká přání (do 6 slov) — "něco klidného na odpoledne, ale ne jazz"
    už je na model.
    """
    t = norm(text)
    if not t or len(t.split()) > 6:
        return None
    for pattern, key in _LOCAL:
        if pattern.search(t):
            return key
    return None


def looks_czech(artist: str, title: str) -> bool:
    """Heuristika bez modelu: česká diakritika v interpretu nebo názvu."""
    return bool(_CZ_CHARS.search(f"{artist} {title}"))
