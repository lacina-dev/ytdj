# ytdj — pravidla pro každého, kdo mění kód

Kancelářský jukebox na Raspberry Pi 3B (1 GB RAM): AI DJ pro YouTube Music,
web pro kolegy, dotykový displej, repro s kolečkem. Vlastník je náročný:
„nesmí dělat ostudu", „plnit přání přesně".

## Co aplikace umí, musí umět dál

- `docs/FUNKCE.md` je závazný popis funkcí. Každé pravidlo má test.
- **Změnit chování, které tam je popsané, smí jen se souhlasem vlastníka.**
  Souhlas se zapíše do FUNKCE.md k pravidlu (datum, důvod). Úkol, kterému
  by se hodilo pravidlo obejít, to nestačí — zeptej se.
- Nová funkce = nové pravidlo ve FUNKCE.md + test.
- `tests/test_funkce.py` hlídá, že každé pravidlo má existující test.

## Požadavky

- `docs/POZADAVKY.md` — všechno, co vlastník chtěl, jeho slovy, se stavem.
  Nový požadavek se zapíše hned, stav se obnoví po každém nasazení.
- `docs/PLAN.md` — podrobná pravidla, cíle a fáze.

## Jak se pracuje

- Tvrdit jen to, co doloží data (test, log, telemetrie, měření na Pi).
- Testy: `YTDJ_EVENTS_FILE=<dočasný soubor> .venv/bin/python -m unittest tests.<název>`
  (bez proměnné testy zapisují do skutečného logu). Panel (`tests.test_panel*`)
  se pouští systémovým `python3`.
- Na Pi jen commitnutý kód; před restartem zkušební import celého ytdj přímo
  na Pi. Restartů hudby co nejméně. Při testech na Pi neměnit hlasitost
  a testovací přání po měření uklidit.
- Hlavní smyčka (asyncio) se nesmí blokovat: SQLite přes vlákno zapisovače,
  čtení mimo smyčku. SD karta je pomalá.
- Nikdy nevypisovat tokeny, cookies ani celé adresy streamů.
