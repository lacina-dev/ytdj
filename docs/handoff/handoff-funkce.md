# Předávka: docs/FUNKCE.md (přerušeno 26. 9.)

## Hotovo
- docs/FUNKCE.md: hlavička smlouvy + oblasti Hlasitost (5 pravidel), Fronta a férovost (9), Přeskakování (2), vše částečné. Nesepsané oblasti jsou v souboru označené „⚙️ nedokončeno".
- Nic není commitnuto a aplikační kód je nedotčený.

## Zbývá
- Oblasti: Přehrávání a zvuk; Přání a výklad (DJ); Přezdívky; Hlasování; Displej; Web; Síť a Wi-Fi; Výpadky; Restart a obnova; Chytrý start; Filtr slov; Provoz; Bezpečnost (otevřené body z POZADAVKY jako poznámky bez ID).
- tests/test_funkce.py (meta-test) není napsaný. Návrh: parsovat `**F-...**` a odkazy v backticku `tests/x.py::Třída::test`, ověřit grepem `class Třída` a `def test` uvnitř (bez importu panelu), povolit `(bez testu: …)` a pravidla s ⚙️.
- tests/test_funkce_guards.py není napsaný. Kandidáti (dnes bez testu, ale testovatelné):
  1. Hlasitost se ukládá do configu (MpvPlayer._remember_volume/_save_volume_later, SAVE_DELAY 2 s; patch mpvmod.save_values). `_args()` obsahuje `--volume=<cfg>`, `--volume-max=100`, `--cache-pause-wait=2`.
  2. status().buffering = core-idle a zároveň nepauza a nějaká skladba (fake_mpv vrací core-idle = cur is None, takže patchnout _prop).
  3. Odkaz na YouTube (watch/youtu.be) = přání té jedné skladby bez modelu (Rig z test_wishes + Catalog.resolve_link). Nerozluštěný odkaz = poctivá hláška.
  4. Strop 2 skladby na interpreta v podkresu (radio._reject_reason „artist_cap"), neplatí v režimu interpreta.
  5. web index.html: `s.buffering` / „načítám", dialog přezdívky při prvním otevření (`openNick`).
- Mapování oblastí na testy:
  - Přání: test_dj_apply, test_dj_fastpath (Znouzectnost: SpacedAndMisspelled), test_dj_intent, test_music_match (RankingOnRecordedResultsTest = kanonická nahrávka).
  - Hlasování: test_votes, test_web_votes, test_panel VotesTest.
  - Displej: test_panel.
  - Síť: test_panel_net.
  - Výpadky: test_outage, test_dj_offline, test_dj_appserver.
  - Restart: test_wishes ResumeRule/test_restart_*, test_review Restart, test_resume_cache PlayerResumeTest, test_pi0926 test_restart_does_not_resurrect_stale_artist_mode.
  - Chytrý start: test_dj_context, test_wishes test_idle_start_*, test_review StartYields.
  - Filtr slov: test_review Display, test_nicks.
  - Provoz: test_telemetry, test_loop, test_dj_appserver KeepWarm.
- Bez testu (hardware): protokol KeDei LCD/XPT2046 (kedei.c), skutečný USB soundbar/HID, priority PipeWire, lupání.
- ⚙️ mění se:
  - dedup přeskočení (SKIP_REPEAT 1,5 s, žádný test);
  - detekce otázek (test_pi0926 MetaQuestions);
  - oblíbený interpret od 2 lidí (votes.py změněný jiným agentem);
  - časy restartu (test_resume_cache změněný jiným agentem);
  - dotyk (panel/touchpress.py je nový).

## Nesoulady dokumentace × kód (zatím)
- README.md ~ř. 403 (tabulka /api/prompt) tvrdí „a newer wish replaces the same client's older one". Kód a PLAN D8: nové přání jde před starší a starší zůstává (tests/test_wishes.py::Supersede::test_single_person_new_wish_first_old_stays; ytdj/wishes.py _apply ~ř. 1383).
- README.md ~ř. 444–447 tvrdí, že navázání po restartu je „only when it was playing, ≤15 min". Přání se ale obnoví i bez hraní, do 2 h při stejném bootu (ytdj/wishes.py:109 RESTORE_MAX_AGE, can_restore ~ř. 575).
- README.md ~ř. 287: „Everything else is a full Codex request (16–21 s)". Podle commitu 8419f71 trvá tah přes app-server 5,6–9,8 s a rychlá cesta ~3 s.
- docs/PLAN.md má D1, D3, D4, D5, D6 „📋", C7 „🔄", H1 „📋 UI webu", H6 „📋 čip na webu". V kódu je to hotové a otestované (test_wishes WebApi/Queue, test_web_votes Page).
- Pozn.: wishes.py, votes.py, mpv.py a index.html mají neuložené změny jiných agentů, takže se čísla řádků posunou.
