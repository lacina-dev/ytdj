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

## Návod a dokumentace na webu

- Změna, kterou uživatel uvidí nebo pozná (nové přání, jiné chování fronty,
  displeje, hlasování…) = uprav i **Nápovědu** (`ytdj/web/static/napoveda.html`),
  pokud se jí týká. Příklady přání v ní mají `data-example` a
  `tests/test_manual.py` ověří, že jim aplikace opravdu rozumí.
- Stránka **Jak to funguje** (`/jak-to-funguje`) se skládá z FUNKCE.md sama.
  Nová oblast (`##`) ve FUNKCE.md potřebuje úvod v `docs/JAK-TO-FUNGUJE.md`;
  automatika, o které kolegové nevědí, patří do jeho výběru „Automatiky…".

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
- Po nasazení a ověření na Pi se nasazený stav vždy sloučí do `main` a pushne
  (vlastník 9. 10.: „finálně po nasazení vždy mergovat do masteru"). V `main`
  je to, co běží na Pi; rozpracované větve se do něj neslučují.
- Hlavní smyčka (asyncio) se nesmí blokovat: SQLite přes vlákno zapisovače,
  čtení mimo smyčku. SD karta je pomalá.
- Nikdy nevypisovat tokeny, cookies ani celé adresy streamů.
