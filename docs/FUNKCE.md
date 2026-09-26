# Funkce ytdj — závazný popis (⚙️ nedokončeno, rozpracováno 26. 9.)

Tohle je seznam všeho, co aplikace umí. **Tato pravidla musí platit dál.**

- Změnit pravidlo smí jen vlastník výslovným souhlasem. Souhlas se zapíše
  přímo k pravidlu: datum, důvod (např. `Změněno 27. 9. se souhlasem
  vlastníka: …`). Úkol, kterému by se hodilo pravidlo obejít, nestačí.
- Nová funkce = nové pravidlo s vlastním ID a testem.
- Každé pravidlo má ID (`F-OBLAST-NN`), jednu větu srozumitelnou bez
  programování a hlídací test(y) `tests/soubor.py::Třída::test`.
  `tests/test_funkce.py` (⚙️ zatím nenapsán) ověří, že každé ID má existující test.
- `(bez testu: důvod)` jen pro hardware, který nejde otestovat v unit testu.
- „⚙️ mění se (26. 9.)" = na pravidle se právě pracuje; konečné znění doplní ta změna.

## Hlasitost

- **F-HLAS-01** Hlasitost nikdy nepřesáhne 100 — z webu, displeje, přání ani povelu.
  Testy: `tests/test_review.py::Display::test_volume_ceiling_is_100_everywhere`, `tests/test_outage.py::Volume::test_ceiling_100`
- **F-HLAS-02** Kolečko a tlačítka na repráku mění tutéž hlasitost jako displej a web, po 2 na cvaknutí.
  Testy: `tests/test_panel_net.py::NetFlowTest::test_media_keys_and_state_while_open`
- **F-HLAS-03** Ťuknutí na −/+ na displeji je jeden krok po 5.
  Testy: `tests/test_panel.py::EndToEndTest::test_volume_tap_is_one_step`
- **F-HLAS-04** Podržené −/+ mění hlasitost plynule (po 0,45 s každých 0,15 s, po 1,5 s každých 0,1 s), zastaví se na 0/100, po zvednutí nebo sjetí prstu.
  Testy: `tests/test_panel.py::EndToEndTest::test_volume_hold_repeats`, `tests/test_panel.py::EndToEndTest::test_volume_hold_clamps`, `tests/test_panel.py::EndToEndTest::test_volume_slide_out_stops_repeat`, `tests/test_panel.py::EndToEndTest::test_volume_hold_survives_glitch`
- **F-HLAS-05** Hlasitost si ytdj pamatuje přes restart (zápis do configu 2 s po poslední změně, mpv startuje rovnou s ní).
  Testy: ⚙️ nedokončeno — test chybí, plánován guard v `tests/test_funkce_guards.py`

## Fronta a férovost

- **F-FRONTA-01** Na řadě je ten, kdo nejdéle nic neslyšel (nováček první); kolo = 2 skladby, když čekají i jiní, jinak 3.
  Testy: `tests/test_wishes.py::FairOrder::test_round_robin_between_people`, `tests/test_wishes.py::FairOrder::test_least_recently_served_goes_first`, `tests/test_wishes.py::FairOrder::test_started_block_finishes_before_newcomer`
- **F-FRONTA-02** Přání s víc skladbami hraje, dokud mají jiní co hrát, nejvýš 4 skladby (z displeje 3); pak jde za ostatní a pokračuje, až nikdo nečeká.
  Testy: `tests/test_pi0926.py::ArtistWishBudget::test_fair_order_budget_puts_robert_before_the_long_wish`, `tests/test_pi0926.py::ArtistWishBudget::test_web_budget_is_larger_than_panel`
- **F-FRONTA-03** Displej je v pořadí jeden člověk, i když každé zavření přání je nová relace.
  Testy: `tests/test_pi0926.py::ArtistWishBudget::test_display_is_one_seat_across_sessions`
- **F-FRONTA-04** „Zařadit hned" dá přání hned za hrající skladbu (jedno na člověka) a nic nepřeruší; cizí přání se neutne nikdy, ani „hned teď".
  Testy: `tests/test_wishes.py::Queue::test_play_next_is_right_after_current_and_capped`, `tests/test_wishes.py::Queue::test_explicit_cut_never_cuts_somebody_elses_wish`, `tests/test_wishes.py::Queue::test_wish_never_cuts_somebody_elses_wish`
- **F-FRONTA-05** Vlastní přání odebere jen autor (token); automatika ani stop přání nikdy nemažou.
  Testy: `tests/test_wishes.py::Queue::test_owner_token_removes_and_others_cannot`, `tests/test_wishes.py::Queue::test_auto_reseed_never_removes_requests`, `tests/test_wishes.py::WebApi::test_stop_never_removes_anybodys_wishes`
- **F-FRONTA-06** Nové přání člověka jde před jeho starší, to zůstává; nahrazuje jen oprava, změna směru nebo skoro stejný text; „a pak…" jde za ně.
  Testy: `tests/test_pi0926.py::SamePerson::test_rules`, `tests/test_wishes.py::Supersede::test_single_person_new_wish_first_old_stays`, `tests/test_wishes.py::Supersede::test_additive_phrasing_queues_after`
- **F-FRONTA-07** Jeden člověk má nejvýš 5 rozpracovaných přání.
  Testy: `tests/test_wishes.py::Queue::test_too_many_from_one_person`
- **F-FRONTA-08** Tatáž skladba ve dvou přáních zazní jednou.
  Testy: `tests/test_wishes.py::FairOrder::test_same_track_in_two_wishes_plays_once`
- **F-FRONTA-09** Podkres po přáních jde za naposledy splněným přáním; interpret v podkresu nejvýš 10 skladeb / 40 min, pak „… a podobné".
  Testy: `tests/test_pi0926.py::BackgroundFollowsLatest::test_after_roberts_wish_background_is_not_old_artist`, `tests/test_pi0926.py::BackgroundFollowsLatest::test_artist_background_is_bounded`

## Přeskakování

- **F-PRESKOK-01** Cizí přeskočení ukončí kolo přání, druhé cizí celé přání („přeskočeno ostatními"); autorovo = „další z mých".
  Testy: `tests/test_pi0926.py::ArtistWishBudget::test_2x_skip_by_others_ends_the_display_wish`, `tests/test_pi0926.py::ArtistWishBudget::test_owner_skip_is_just_next_of_mine`, `tests/test_pi0926.py::ArtistWishBudget::test_skip_by_other_ends_the_turn_at_once`
- **F-PRESKOK-02** ⚙️ mění se (26. 9.) Dvojí ťuknutí na Další je jedno přeskočení (dnes: tentýž člověk, tatáž skladba do 1,5 s); přeskočení DJem a Další při výpadku se nepočítají.
  Testy: ⚙️ čeká na rozpracovanou změnu

## ⚙️ nedokončeno

Oblasti ještě nesepsané: Přehrávání a zvuk; Přání a jejich výklad (DJ);
Přezdívky a identita; Hlasování; Displej; Web; Síť a Wi-Fi; Výpadky;
Restart a obnova; Chytrý start; Kancelářská slušnost; Provoz; Bezpečnost.
Podklad (mapování na testy) je v předávce `handoff-funkce.md`.
