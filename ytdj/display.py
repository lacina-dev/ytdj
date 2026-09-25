"""Co se smí ukázat v kanceláři: jména a texty přání na displeji a webu.

Displej visí na zdi a web vidí všichni — jméno "píča" nebo přání plné
sprostých slov by bylo ostudou (PLAN: "nesmí dělat ostudu"). Filtruje se jen
zobrazení; DJ dostane text tak, jak ho posluchač napsal.

Seznam je schválně krátký (kořeny slov, porovnává se bez diakritiky a na
začátku slova). Rozšířit jde přes `display_blocklist` v config.toml (seznam
kořenů navíc), vypnout `display_filter = false`.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

# kořeny (bez diakritiky, malými písmeny); slovo, které jimi začíná, se skryje
BUILTIN = (
    "kurv", "pic", "pič", "prdel", "hovn", "sracka", "srac", "mrdk", "mrd", "jeb", "kokot",
    "curak", "zmrd", "debil", "kreten", "buzer", "cigos", "negr", "fuck", "shit",
    "bitch", "cunt", "nigg", "whore", "slut", "hitler", "nazi",
)
# kořeny, které by jinak chytily nevinná slova ("pic" → "picnic", "pichlavý")
_EXACTISH = {"pic": ("pica", "pice", "picu", "picou", "pico", "picus")}
MASK = "•••"


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


class Censor:
    def __init__(self, extra: Iterable[str] = (), enabled: bool = True) -> None:
        self.enabled = enabled
        roots = {_fold(r) for r in (*BUILTIN, *extra) if str(r).strip()}
        self.prefixes = sorted(r for r in roots if r not in _EXACTISH)
        self.words = {w for r in roots if r in _EXACTISH for w in _EXACTISH[r]}

    def bad(self, word: str) -> bool:
        w = _fold(word)
        return w in self.words or any(w.startswith(p) for p in self.prefixes)

    def clean(self, text: Any) -> str:
        text = str(text or "")
        if not self.enabled or not text:
            return text
        return re.sub(r"\w+", lambda m: MASK if self.bad(m.group(0)) else m.group(0), text)


def censor_for(cfg: Any) -> Censor:
    return Censor(getattr(cfg, "display_blocklist", None) or (),
                  enabled=bool(getattr(cfg, "display_filter", True)))
