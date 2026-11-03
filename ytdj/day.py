"""Den jukeboxu — jedna definice pro obnovu po restartu i pro běh bez restartu.

Den = kalendářní den podle místních hodin (hranice o půlnoci). Tuhle volbu
udělal vlastník u obnovy po zapnutí Pi („Jen týž den“, 6. 10. 2026,
F-RESTART-11) a 3. 11. 2026 ji rozšířil i na běh bez restartu: nedohraná
přání a podkres, který spustila, končí s koncem dne (F-RESTART-16).

Půlnoc je v noci (22–7 h, F-RESTART-01), daleko od pracovní doby kanceláře:
kdo poslouchá ve 23 h, tomu se nic nemění.

Soudit den jde jen podle hodin seřízených ze sítě (Pi nemá hodiny s
baterií) — to hlídají volající (F-RESTART-12): neseřízené hodiny = den
neznámý, nic se nezahazuje.
"""

from __future__ import annotations

from datetime import date, datetime


def day_of(at: float | datetime | None = None) -> date:
    """Den jukeboxu pro čas `at` (epoch s nebo datetime; None = teď)."""
    if at is None:
        return datetime.now().date()
    if isinstance(at, datetime):
        return at.date()
    return datetime.fromtimestamp(float(at)).date()


def earlier_day(then: float | datetime, now: float | datetime | None = None) -> bool:
    """Patří `then` k dřívějšímu dni jukeboxu než `now`?"""
    return day_of(then) < day_of(now)
