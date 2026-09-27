# Funkce ytdj — závazný popis

Tohle je seznam všeho, co aplikace umí (stav 26. 9. 2026, commit e713074).
**Tato pravidla musí platit dál.** „Aby to, co aplikace už umí, zůstávalo."

## Jak s tímhle souborem zacházet

- **Pravidla musí platit dál.** Úkol, kterému by se hodilo pravidlo obejít
  nebo změnit, k tomu nestačí.
- **Změnu pravidla schvaluje jen vlastník, výslovně.** Souhlas se zapíše
  přímo k pravidlu, řádkem
  `Změněno <datum> se souhlasem vlastníka: <co a proč>`, a do tabulky
  „Změny se souhlasem vlastníka" na konci.
- **Nová funkce = nové pravidlo** s novým ID a s testem. ID se nikdy znovu
  nepoužije; zrušené pravidlo zůstane s poznámkou „zrušeno … se souhlasem".
- Každé pravidlo má ID (`F-OBLAST-NN`), jednu větu srozumitelnou bez
  programování a hlídací test(y) ve tvaru `tests/soubor.py::Třída::test`.
- `tests/test_funkce.py` ověří, že každé pravidlo má aspoň jeden test a že
  každý uvedený test existuje. Výjimka `(bez testu: <důvod>)` jen pro
  hardware, který unit test neprověří (seznam na konci).
- „⚙️ mění se (26. 9.)" = na pravidle se právě pracuje; test platí pro
  dnešní stav, konečné znění doplní ta změna.
- Čísla (limity, časy) jsou součástí pravidla — změna čísla je změna pravidla.

## Přehrávání a zvuk

- **F-ZVUK-01** Fronta, kterou ukazuje web i displej, je přesně to, co opravdu zahraje přehrávač (playlist mpv), žádná vlastní kopie.
  Testy: `tests/test_player_queue.py::TrueOrder::test_status_queue_is_mpv_playlist`, `tests/test_player_queue.py::TrueOrder::test_random_mutations_keep_mirror_true`
- **F-ZVUK-02** Tah DJe a doplňování podkresu se v playlistu nepromíchají a hrající skladba se nikdy nezařadí znovu.
  Testy: `tests/test_player_queue.py::TrueOrder::test_dj_turn_and_filler_do_not_interleave`
- **F-ZVUK-03** Skladba začne hrát až se 2 s proudu v zásobě (bez lupnutí na začátku skladby).
  Testy: `tests/test_funkce_guards.py::StartsWithBufferedStream::test_cache_pause_wait`
- **F-ZVUK-04** Dokud skladba opravdu nehraje (načítá se), čas stojí a web i displej píšou „načítám…"; pauza není načítání.
  Testy: `tests/test_funkce_guards.py::TimeStandsWhileLoading::test_status_reports_buffering_only_while_really_waiting`, `tests/test_funkce_guards.py::TimeStandsWhileLoading::test_web_page_holds_the_clock_while_loading`
- **F-ZVUK-05** Dopředu se připravují skladby v pořadí fronty postupně: nejdřív 3 nejbližší, pak po jedné až 10, kolik jich fronta má (nastavení `prefetch_first`, `prefetch_max`; hloubku fronty dál určuje `queue_target`), vždy až po naléhavých (skladba, na kterou se čeká, Další, první skladby přání) a jen hlavním vláknem resolveru; po každém přeskočení hned nový seznam.
  Změněno 26. 9. 2026 se souhlasem vlastníka: postupná příprava — nejdřív 3 nejbližší, pak po jedné až 10, nikdy na úkor naléhavé skladby (návrh vlastníka)
  Testy: `tests/test_player_queue.py::Prefetch::test_ahead_is_next_n_in_mpv_order`, `tests/test_player_queue.py::Prefetch::test_skip_resends_window_immediately`, `tests/test_player_queue.py::ProgressivePrefetch::test_player_sends_window_of_ten_with_near_three`, `tests/test_player_queue.py::ProgressivePrefetch::test_nearest_three_first_then_extends_to_ten_when_idle`, `tests/test_player_queue.py::ProgressivePrefetch::test_urgent_and_wish_preempt_extension`, `tests/test_player_queue.py::ProgressivePrefetch::test_window_reshuffles_when_a_wish_is_inserted`, `tests/test_player_queue.py::ProgressivePrefetch::test_ten_skips_in_a_row_all_land_on_prepared`
- **F-ZVUK-06** V podkresu nejvýš 2 skladby téhož interpreta na jedno doplnění (ne v režimu interpreta, kde je to smysl).
  Testy: `tests/test_funkce_guards.py::ArtistCapInBackground::test_two_per_artist_unless_artist_mode`
- **F-ZVUK-07** Když se o nic nežádá, hraje se kanonická nahrávka (studiová, oficiální), ne cover, live, remix ani výběrová kompilace; mezi platnými rozhoduje počet přehrání.
  Testy: `tests/test_music_match.py::RankingOnRecordedResultsTest::test_praminek_vlasu_canonical_not_bargain_bin_compilation`, `tests/test_music_match.py::RankingOnRecordedResultsTest::test_prince_kiss_studio_not_live`, `tests/test_music_match.py::RankingOnRecordedResultsTest::test_eva_cassidy_studio_not_live`, `tests/test_music_match.py::RankingOnRecordedResultsTest::test_official_without_diacritics_beats_live_and_remix_with_them`, `tests/test_music_match.py::TitleTest::test_version_tags`
- **F-ZVUK-08** Kdo si řekne o live nebo remix, dostane live nebo remix.
  Testy: `tests/test_music_match.py::RankingOnRecordedResultsTest::test_asked_live_gets_live`, `tests/test_music_match.py::RankingOnRecordedResultsTest::test_asked_remix_gets_a_remix`, `tests/test_music_match.py::TitleTest::test_asked_version_wins_and_unasked_loses`
- **F-ZVUK-09** Jiný interpret se stejným názvem písně je vyloučen (radši nic než jiná kapela); správný interpret s jinou písní taky.
  Testy: `tests/test_music_match.py::RankingOnRecordedResultsTest::test_right_title_by_wrong_artist_is_vetoed`, `tests/test_music_match.py::RankingOnRecordedResultsTest::test_wrong_song_by_right_artist_is_vetoed`, `tests/test_music_match.py::RankingOnRecordedResultsTest::test_named_band_not_the_more_famous_namesake`, `tests/test_music_match.py::ArtistTest::test_different_person_with_same_surname_is_vetoed`, `tests/test_music_match.py::SearchSongReplayTest::test_unknown_title_falls_back_to_the_artist_never_to_another_band`
- **F-ZVUK-10** Na diakritice, mezerách a interpunkci v názvech nezáleží („Deda Mladek" = „Děda Mládek").
  Testy: `tests/test_music_match.py::NormTest::test_diacritics_and_punctuation`, `tests/test_music_match.py::NormTest::test_spacing_does_not_matter`, `tests/test_music_match.py::VideoTest::test_czech_quotes`
- **F-ZVUK-11** Vulgární (explicitní) skladby se samy do podkresu nedostanou; výslovně vyžádaný interpret nebo skladba ano.
  Testy: `tests/test_outage.py::RadioResilience::test_explicit_kept_out_of_background_only`, `tests/test_outage.py::RadioResilience::test_to_track_reads_explicit`
- **F-ZVUK-12** Nepřehratelná skladba se blokuje jen na 7 dní (smazaná natrvalo); staré záznamy dostanou lhůtu.
  Testy: `tests/test_outage.py::BlacklistTtl::test_ttl_and_permanent`, `tests/test_outage.py::BlacklistTtl::test_legacy_rows_get_a_ttl`
- **F-ZVUK-13** Přehrávač (mpv) má přednost před přípravou dalších skladeb: mpv běží s prioritou −11, příprava (resolver, yt-dlp) s nižší; priorita platí pro všechna vlákna.
  Testy: `tests/test_player_start.py::NicedWrapper::test_absolute_priority_for_all_threads`, `tests/test_player_start.py::NicedWrapper::test_relative_delta_is_computed_from_own_nice`, `tests/test_player_start.py::ShimFallbackNice::test_real_ytdlp_does_not_inherit_parent_offset`, `tests/test_player_start.py::PriorityTelemetry::test_priority_waits_for_mpv_and_resolver_listen`
- **F-ZVUK-14** Lupání v USB repráku se měří na Pi (xruny PipeWire), ne v testu.
  (bez testu: skutečný zvukový výstup Pi a USB soundbaru; měří se telemetrií `audio.xrun`)
- **F-ZVUK-15** Automatika nikdy sama nepřeskočí skladbu bez důvodu: skladba odsunutá novým rádiem se nepočítá jako přeskočení a přeseedování spustí až 3 skutečná přeskočení během 10 min.
  Testy: `tests/test_dj_intent.py::Skips::test_three_skips_trigger`, `tests/test_dj_intent.py::Skips::test_window_slide_does_not_trigger`, `tests/test_dj_intent.py::Skips::test_skips_before_turn_dont_count`
- **F-ZVUK-16** Automatické přeseedování nepřijde po restartu, do 180 s po přání posluchače ani v režimu interpreta.
  Testy: `tests/test_dj_intent.py::Skips::test_no_trigger_after_restart`, `tests/test_dj_intent.py::Skips::test_quiet_after_user_request`, `tests/test_dj_intent.py::SkipBurstInArtistMode::test_artist_mode_ignores_skip_burst`, `tests/test_dj_intent.py::SkipBurstInArtistMode::test_without_artist_mode_burst_reseeds`
- **F-ZVUK-17** Při změně skladby DJem hraje stará skladba, dokud není nová připravená (žádné ticho navíc).
  Testy: `tests/test_dj_fastpath.py::Switch::test_old_track_plays_until_new_is_ready`, `tests/test_dj_fastpath.py::Switch::test_no_double_skip_when_track_ended_meanwhile`
- **F-ZVUK-18** Režim interpreta hraje nejdřív jeho dosud nepřehrané skladby (i přes restarty), bez opakování, a po projetí všech začne těmi nejdéle nehranými.
  Testy: `tests/test_radio_artist.py::ArtistRotation::test_new_turn_continues_where_the_last_one_stopped`, `tests/test_radio_artist.py::ArtistRotation::test_after_whole_pool_the_least_recent_comes_first`, `tests/test_radio_artist.py::ArtistRotation::test_restart_inside_session_also_rotates`, `tests/test_radio_artist.py::ArtistRotation::test_given_back_tracks_are_not_lost`
- **F-ZVUK-19** Na konci skladby drží přehrávač 2 s zvuku v zásobě, takže otevření další skladby do 2 s neudělá ticho s výpadky (xruny) mezi skladbami. Začátek skladby hlásí formát zvuku a odhad ticha (`gap_ms`), xruny se hlásí s přesným časem (pw-top po řádcích).
  Testy: `tests/test_telemetry.py::AudioGapTest::test_mpv_keeps_two_seconds_of_audio_gapless`, `tests/test_telemetry.py::AudioGapTest::test_track_start_has_audio_format_and_gap`, `tests/test_telemetry.py::AudioGapTest::test_pwtop_is_line_buffered`
- **F-ZVUK-20** Příprava skladby je rychlá a Premium zůstává: resolver řeší napřed klientem `web_music` (Premium opus 774 s PO tokenem), a když s ním skladba selže, hned znovu výchozím výběrem yt-dlp; JS výzvy YouTube řeší trvale běžící node (přeložený přehrávač drží, po 10 min nečinnosti skončí) a při jeho chybě jednorázový node jako dřív; pluginy yt-dlp se načtou jednou a YoutubeDL se nestaví ve dvou vláknech naráz.
  Testy: `tests/test_resume_cache.py::FastPreparationTest::test_fast_client_falls_back_to_default_choice`, `tests/test_resume_cache.py::FastPreparationTest::test_build_sets_fast_client_only_when_template_does_not`, `tests/test_resume_cache.py::FastPreparationTest::test_youtubedl_is_never_built_twice_at_once`, `tests/test_resume_cache.py::FastPreparationTest::test_persistent_node_solves_and_keeps_compiled_player`
- **F-ZVUK-21** Připravené skladby zůstanou připravené: nejbližší se samy obnoví dřív, než zestárnou (i během dlouhé pauzy), po pauze se okno hned pošle znovu, za „hotové“ se počítají jen čerstvé a skladby odsunuté přáním za okno (dál ve frontě, nejvýš 50) se nezahodí.
  Testy: `tests/test_player_queue.py::PreparedStayFresh::test_near_items_refresh_by_themselves_during_a_pause`, `tests/test_player_queue.py::PreparedStayFresh::test_only_fresh_entries_count_as_ready`, `tests/test_player_queue.py::PreparedStayFresh::test_unpause_resends_window`, `tests/test_player_queue.py::PreparedStayFresh::test_queued_tracks_behind_the_window_are_kept`
- **F-ZVUK-22** Dokud zní konec skladby ze zásoby (~2,5 s po jejím konci v přehrávači), displej, web i historie ukazují pořád ji (s běžícím časem, další je první ve frontě); Další (i přeskočení DJem nebo hlasováním) v té chvíli utne jen její doznívání, další skladbu nepřeskočí.
  Testy: `tests/test_player_queue.py::AudibleTail::test_track_stays_shown_until_its_end_is_heard`, `tests/test_player_queue.py::AudibleTail::test_skip_during_tail_skips_the_audible_track`
- **F-ZVUK-23** Premium se nevzdá potichu: když rychlý klient dá s přihlášením jiný než Premium formát (774/141), skladba se hned vyřeší znovu výchozím výběrem a zapíše se `resolver.quality_retry`; zaseknutý trvalý node se vzdá do 10 s a výzva se vyřeší postaru.
  Testy: `tests/test_resume_cache.py::FastPreparationTest::test_non_premium_result_is_retried_with_default_choice`, `tests/test_resume_cache.py::FastPreparationTest::test_persistent_node_solves_and_keeps_compiled_player`

## Hlasitost

- **F-HLAS-01** Hlasitost nikdy nepřesáhne 100 — z webu, displeje, přání ani povelu.
  Testy: `tests/test_review.py::Display::test_volume_ceiling_is_100_everywhere`, `tests/test_outage.py::Volume::test_ceiling_100`
- **F-HLAS-02** Kolečko a tlačítka na repráku mění tutéž hlasitost jako displej a web, po 2 na cvaknutí.
  Testy: `tests/test_panel_net.py::NetFlowTest::test_media_keys_and_state_while_open`
- **F-HLAS-03** Ťuknutí na −/+ na displeji je jeden krok po 5. Hlasitost se z displeje nikdy nezmění skokem: lišta mění hlasitost jen tažením (prst na knoflíku ho vede, jinde se hlasitost změní o tolik, o kolik se prst posune), ťuknutí na lištu je jen jeden krok k prstu a stisk jiného tlačítka se do lišty nikdy nepřesune.
  Změněno 26. 9. 2026 se souhlasem vlastníka: hlasitost se z displeje nikdy nezmění skokem — lišta jen tažením, ťuknutí vedle ji nepřeskočí (vlastník: ‚jen hlasitost při tom neměň').
  Testy: `tests/test_panel.py::EndToEndTest::test_volume_tap_is_one_step`, `tests/test_panel.py::EndToEndTest::test_volume_step_and_drag`, `tests/test_panel_touch.py::VolumeNeverJumpsTest::test_pi_20_35_29_next_press_sliding_onto_the_bar`, `tests/test_panel_touch.py::VolumeNeverJumpsTest::test_pi_21_55_51_49ms_tap_on_the_bar_top_edge`, `tests/test_panel_touch.py::VolumeNeverJumpsTest::test_plus_pressed_at_its_left_edge_is_plus`, `tests/test_panel_touch.py::VolumeNeverJumpsTest::test_minus_pressed_at_its_right_edge_is_minus`, `tests/test_panel_touch.py::VolumeNeverJumpsTest::test_a_tap_on_the_bar_is_one_step_towards_the_finger`, `tests/test_panel_touch.py::VolumeNeverJumpsTest::test_drag_away_from_the_knob_is_relative`
- **F-HLAS-04** Podržené −/+ mění hlasitost plynule (po 0,45 s každých 0,15 s, po 1,5 s každých 0,1 s) a zastaví se na 0/100, po zvednutí nebo zřetelném sjetí prstu (2 vzorky za sebou dál než 44 px od tlačítka).
  Změněno 26. 9. 2026 se souhlasem vlastníka: „sjetí prstu" = zřetelné sjetí, ne šum rezistivní vrstvy (dřív stačilo 14 px a stisky se ztrácely — ráno 26. 9. jen ~40 % dotyků prošlo).
  Testy: `tests/test_panel.py::EndToEndTest::test_volume_hold_repeats`, `tests/test_panel.py::EndToEndTest::test_volume_hold_clamps`, `tests/test_panel.py::EndToEndTest::test_volume_slide_out_stops_repeat`, `tests/test_panel.py::EndToEndTest::test_volume_hold_survives_glitch`
- **F-HLAS-05** Hlasitost si ytdj pamatuje přes restart: zapíše se 2 s po poslední změně (ne při každém pohybu) a mpv startuje rovnou s ní.
  Testy: `tests/test_funkce_guards.py::VolumeRemembered::test_set_volume_is_written_to_config_once_after_a_pause`, `tests/test_funkce_guards.py::VolumeRemembered::test_mpv_starts_at_the_saved_volume_capped`
- **F-HLAS-06** Teploměr hlasitosti na displeji odpovídá hodnotě 0–100.
  Testy: `tests/test_panel.py::RenderTest::test_volume_mapping`
- **F-HLAS-07** Hlasitost jde nastavit slovy bez modelu („hlasitěji", „hlasitost 40", i s vatou jako „prosím").
  Testy: `tests/test_dj_intent.py::LocalCommands::test_commands`, `tests/test_dj_offline.py::Chips::test_commands_with_fillers`, `tests/test_wishes.py::WebApi::test_local_command_is_immediate`

## Přeskakování

- **F-PRESKOK-01** Další na připravenou skladbu je rychlé; série rychlých Další nečeká na načtení každé skladby a přeskočení během načítání není chyba.
  Testy: `tests/test_player_queue.py::SmartSkip::test_plain_next_when_next_is_ready`, `tests/test_player_queue.py::SkipBurst::test_rapid_skips_without_waiting`, `tests/test_player_queue.py::SkipBurst::test_skip_while_loading_cancels_and_is_not_an_error`, `tests/test_player_queue.py::SkipBurst::test_burst_requests_name_what_mpv_plays`
- **F-PRESKOK-02** Při sérii přeskočení v podkresu se skočí na už připravenou skladbu podkresu, ale nikdy přes přání a přání se nikdy neposune dopředu.
  Testy: `tests/test_player_queue.py::SmartSkip::test_jumps_to_prepared_background_track`, `tests/test_player_queue.py::SmartSkip::test_never_jumps_over_a_request`, `tests/test_player_queue.py::SmartSkip::test_never_moves_a_request_forward`, `tests/test_player_queue.py::SmartSkip::test_burst_plays_only_prepared_while_any_exists`
- **F-PRESKOK-03** Každé přeskočení se připíše tomu, kdo zmáčkl Další (web posílá id klienta).
  Testy: `tests/test_review.py::CutAndSkip::test_skip_is_attributed_to_whoever_skipped`, `tests/test_pi0926.py::ArtistWishBudget::test_web_control_next_attributes_the_skip`
- **F-PRESKOK-04** Přeskočí-li skladbu přání někdo jiný než autor, kolo toho přání hned končí; druhé cizí přeskočení (jiné skladby) přání ukončí („Přeskočeno ostatními") i s jeho podkresem.
  Testy: `tests/test_pi0926.py::ArtistWishBudget::test_skip_by_other_ends_the_turn_at_once`, `tests/test_pi0926.py::ArtistWishBudget::test_2x_skip_by_others_ends_the_display_wish`, `tests/test_review2.py::Skips::test_later_skip_of_the_next_track_still_counts`
- **F-PRESKOK-05** Autorovo přeskočení je jen „další z mých"; Další na displeji u přání z displeje je autor.
  Testy: `tests/test_pi0926.py::ArtistWishBudget::test_owner_skip_is_just_next_of_mine`
- **F-PRESKOK-06** Jedna skladba se počítá jednou: dvojí ťuknutí (tentýž člověk, tatáž skladba do 1,5 s) je jedno přeskočení a tutéž skladbu jednoho přání nezapočítají dvakrát ani dva lidé; web tlačítko Další 1 s po stisku ignoruje.
  Testy: `tests/test_review2.py::Skips::test_double_tap_is_one_skip`, `tests/test_review2.py::Skips::test_same_track_counts_once_even_from_two_people`, `tests/test_review2.py::Skips::test_web_next_button_is_debounced`
- **F-PRESKOK-07** Přeskočení rozhodnuté DJem a Další při výpadku YouTube/sítě se jako cizí přeskočení nepočítají.
  Testy: `tests/test_review2.py::Skips::test_dj_decided_skip_does_not_count`, `tests/test_review2.py::Skips::test_skip_during_outage_does_not_count`
- **F-PRESKOK-08** Další, když nic nehraje, není požadavek na skladbu.
  Testy: `tests/test_player_queue.py::SkipBurst::test_skip_with_nothing_playing_is_not_a_request`
- **F-PRESKOK-09** Další na skladbu, která ještě není připravená, nečeká na skladbu, kterou resolver zrovna chystá dopředu: tu vyřeší druhé vlákno (jen skladby, na které přehrávač čeká; dopředu dál jen jedno).
  Testy: `tests/test_player_queue.py::ResolverQueue::test_urgent_does_not_wait_for_track_being_prepared`, `tests/test_player_queue.py::ResolverQueue::test_second_lane_never_prepares_ahead`

## Přání a jejich výklad (DJ)

- **F-PRANI-01** Přání se přijme okamžitě (web dostane 202 a id), nikdy „DJ ještě dokončuje…"; průběh a odpověď přijdou potom.
  Testy: `tests/test_wishes.py::WebApi::test_prompt_is_accepted_at_once_and_progress_streams`, `tests/test_panel.py::WishTest::test_busy_dj_does_not_block`
- **F-PRANI-02** Povely (další, pauza, hlasitost…) jdou bez modelu a hned, mimo frontu přání.
  Testy: `tests/test_dj_intent.py::LocalCommands::test_commands`, `tests/test_dj_intent.py::LocalCommands::test_wishes_go_to_the_model`, `tests/test_wishes.py::Queue::test_local_commands_bypass_queue`
- **F-PRANI-03** Přání interpreta („pusť Kabát", „písničky od Midi Lidi", „chci slyšet X") hraje víc jeho skladeb (režim interpreta), ne jednu.
  Testy: `tests/test_dj_apply.py::PiReplays::test_2023_hraj_pisnicky_od_midi_lidi`, `tests/test_dj_apply.py::PiReplays::test_2004_hraj_davida_stypku`, `tests/test_dj_intent.py::HowIsItMeant::test_artist_phrasings`, `tests/test_dj_intent.py::HowIsItMeant::test_artist_request_needs_nothing_else`, `tests/test_music_match.py::ArtistModeTest::test_plays_only_the_artist_and_more_than_two_in_a_row`, `tests/test_dj_fastpath.py::FastTurn::test_plays_artist_and_records_wish`
- **F-PRANI-04** „Jednu od X" je jedna skladba; konkrétní píseň hraje hned (nebo po hrající, když se to řekne) a pak podobné.
  Testy: `tests/test_dj_apply.py::Scenarios::test_one_from_artist`, `tests/test_dj_intent.py::HowIsItMeant::test_one_from_artist`, `tests/test_dj_apply.py::Scenarios::test_specific_song_then_similar`, `tests/test_dj_apply.py::Scenarios::test_song_plays_now_or_after`, `tests/test_dj_fastpath.py::FastSong::test_plays_song_now_then_similar`
- **F-PRANI-05** Překlepy a rozdělená jména se poznají bez modelu („z nouze cnost" → Znouzectnost, „tata boys" → Tata Bojs, „Vojtano" → Vojtaano), ale sousední jméno s jiným písmenem se nepřijme.
  Testy: `tests/test_dj_fastpath.py::SpacedAndMisspelled::test_name_matches_fuzzy`, `tests/test_dj_fastpath.py::SpacedAndMisspelled::test_found_in_catalog`, `tests/test_dj_fastpath.py::SpacedAndMisspelled::test_one_letter_neighbour_is_not_accepted`, `tests/test_pi0926.py::MisspelledArtistSong::test_fast_path_finds_budulinek`
- **F-PRANI-06** Česká skloňování, ženská příjmení a kancelářské obraty („tam", „hoď", „šoupni", „a pak") se chápou správně a bez falešných shod.
  Testy: `tests/test_dj_intent.py::Mentions::test_czech_declension`, `tests/test_dj_intent.py::Repair::test_surname_only`, `tests/test_dj_intent.py::OfficePhrasings::test_artist_requests`, `tests/test_dj_intent.py::OfficePhrasings::test_not_artist_requests`
- **F-PRANI-07** „X od Y": hraje verze od Y; když ji Y nemá, hraje jinou existující verzi s poctivou poznámkou; „jen od Y"/„originál" pak znamená „nenašel jsem".
  Testy: `tests/test_dj_fastpath.py::Songs::test_named_artist_lacks_it_other_version_with_note`, `tests/test_dj_fastpath.py::Songs::test_insisting_on_the_artist_means_not_found`, `tests/test_dj_fastpath.py::Songs::test_surname_prefers_named_artist_over_other_version`, `tests/test_dj_apply.py::PiReplays::test_2346_other_version_is_said_out_loud`, `tests/test_dj_apply.py::PiReplays::test_2346_insisted_artist_is_not_replaced`, `tests/test_dj_fastpath.py::FastSong::test_other_version_reply_is_honest`
- **F-PRANI-08** Co se nenašlo, DJ nezahraje ani netvrdí, že hraje — řekne „nenašel jsem".
  Testy: `tests/test_dj_apply.py::PiReplays::test_2302_wrong_song_is_not_claimed`, `tests/test_dj_apply.py::Scenarios::test_unknown_artist_is_said_honestly`, `tests/test_dj_apply.py::Scenarios::test_missing_part_is_reported`, `tests/test_wishes.py::Queue::test_not_found_is_honest`
- **F-PRANI-09** Potvrzení skládá aplikace z toho, co se opravdu zařadilo („Zařadil jsem: …"), nikdy prázdný seznam; DJ nesmí slibovat režimy, které neexistují („nastavím střídání").
  Testy: `tests/test_pi0926.py::HonestReplies::test_nothing_reply_cannot_promise_a_mode`, `tests/test_review2.py::Confirm::test_one_song_that_cuts_in_is_named`, `tests/test_review2.py::Confirm::test_never_an_empty_list`
- **F-PRANI-10** „Střídej X a Y" je jedno přání se dvěma interprety střídavě; „oboje / i X i Y" = obojí (skladba i interpret).
  Testy: `tests/test_pi0926.py::HonestReplies::test_alternation_is_a_real_two_artist_wish`, `tests/test_dj_apply.py::Scenarios::test_two_artists_alternate`, `tests/test_pi0926.py::SamePerson::test_both_while_budulinek_plays_queues_depeche_mode`, `tests/test_dj_apply.py::Interleave::test_alternates_and_dedups`
- **F-PRANI-11** „Něco jako X" je nálada, „víc takového" navazuje na hrající skladbu, „X, ale ne Y" vynechá Y.
  Testy: `tests/test_dj_intent.py::Repair::test_like_is_a_mood`, `tests/test_dj_apply.py::Scenarios::test_more_like_this_starts_from_current`, `tests/test_wishes.py::ChangeOfDirection::test_more_like_this_stays`, `tests/test_dj_apply.py::Scenarios::test_artist_but_not_that_song`, `tests/test_dj_intent.py::HowIsItMeant::test_artist_with_exception`, `tests/test_dj_intent.py::Avoid::test_title_and_artist`
- **F-PRANI-12** „Něco jiného" a „Překvap mě" opravdu změní směr a ukončí režim interpreta; automatika režim interpreta ukončit nesmí.
  Testy: `tests/test_dj_apply.py::Scenarios::test_something_else_ends_artist_mode`, `tests/test_wishes.py::ChangeOfDirection::test_surprise_breaks_out_of_artist_mode`, `tests/test_wishes.py::ChangeOfDirection::test_chip_texts_are_unambiguous`, `tests/test_dj_apply.py::Scenarios::test_auto_turn_cannot_end_artist_mode`
- **F-PRANI-13** Otázka nebo stížnost na frontu („proč nehraje moje…", „to není demokracie", „kdy bude moje písnička?") není přání: pravdivá odpověď ze stavu (co hraje a čí to je, kde jsou moje přání), žádná hudba navíc, bez modelu; u displeje jsou „moje" všechna přání z displeje.
  Testy: `tests/test_pi0926.py::MetaQuestions::test_detection`, `tests/test_pi0926.py::MetaQuestions::test_why_not_my_songs_answers_from_state`, `tests/test_pi0926.py::MetaQuestions::test_no_alternation_complaint_is_answered_truthfully`, `tests/test_review2.py::MetaOrWish::test_pure_questions_stay_questions`, `tests/test_review2.py::PanelSeat::test_display_question_finds_display_wishes`
- **F-PRANI-14** Je-li v otázce nebo stížnosti jméno, které potvrdí katalog („proč nehraješ Kabát?", „nefér, chci Olympic", „kdy bude Bohemian Rhapsody"), je to přání a zařadí se (stížnost s „Beru to jako přání."); je-li už ve frontě, DJ řekne kdy.
  Testy: `tests/test_review2.py::MetaOrWish::test_text_level`, `tests/test_review2.py::MetaOrWish::test_question_with_a_known_name_is_queued`, `tests/test_review2.py::MetaOrWish::test_complaint_with_a_name_is_queued_with_an_honest_note`, `tests/test_review2.py::MetaOrWish::test_when_is_song_queues_it_or_tells_the_eta`
- **F-PRANI-15** Odkaz na YouTube se zpracuje bez modelu: video = přání té skladby, nerozluštěný odkaz = poctivá hláška.
  Testy: `tests/test_funkce_guards.py::LinkWish::test_video_link_queues_that_track_without_the_model`
- **F-PRANI-16** Poslední přání posluchače přežije restart (12 h) a model ho vidí („navaž"); automatický tah nepíše do vkusu ani nemaže vyžádané skladby.
  Testy: `tests/test_dj_apply.py::Wish::test_last_wish_is_in_the_prompt_and_survives_restart`, `tests/test_dj_apply.py::Wish::test_earlier_wishes_are_kept`, `tests/test_dj_intent.py::Intent::test_roundtrip_and_ttl`, `tests/test_dj_apply.py::PiReplays::test_auto_turn_does_not_write_taste`, `tests/test_dj_apply.py::PiReplays::test_2006_reseed_does_not_wipe_request`
- **F-PRANI-17** Tahy modelu jdou přísně v pořadí příchodu přání; rychlá cesta (bez modelu) na nikoho nečeká; hledání více skladeb běží souběžně (limit 7 s).
  Testy: `tests/test_wishes.py::Queue::test_codex_turns_go_in_arrival_order`, `tests/test_dj_fastpath.py::Songs::test_lookups_run_in_parallel`, `tests/test_dj_fastpath.py::Songs::test_time_budget`, `tests/test_dj_latency.py::ParallelSeeds::test_five_seeds_take_one_lookup_time`, `tests/test_dj_latency.py::NoWaitForExit::test_returns_at_turn_completed`
- **F-PRANI-18** Rychlá cesta si nejistá nic nezmění (radši model); nálada s „od" jde modelu.
  Testy: `tests/test_dj_fastpath.py::FastTurn::test_uncertain_leaves_everything_alone`, `tests/test_dj_fastpath.py::FastSong::test_mood_with_od_goes_to_model`, `tests/test_dj_fastpath.py::WithCatalog::test_fallbacks`, `tests/test_dj_fastpath.py::Songs::test_traps`

## Fronta a férovost

- **F-FRONTA-01** Na řadě je ten, kdo nejdéle nic neslyšel (nováček první); kolo = 2 skladby, když čekají i jiní, jinak 3; rozehrané kolo se dohraje.
  Testy: `tests/test_wishes.py::FairOrder::test_round_robin_between_people`, `tests/test_wishes.py::FairOrder::test_least_recently_served_goes_first`, `tests/test_wishes.py::FairOrder::test_started_block_finishes_before_newcomer`, `tests/test_wishes.py::Queue::test_three_people_concurrently_fair_and_nothing_lost`
- **F-FRONTA-02** Přání s víc skladbami hraje, dokud mají jiní co hrát, nejvýš 4 skladby (z displeje 3); pak jde za ostatní a pokračuje, až nikdo nečeká.
  Testy: `tests/test_pi0926.py::ArtistWishBudget::test_fair_order_budget_puts_robert_before_the_long_wish`, `tests/test_pi0926.py::ArtistWishBudget::test_web_budget_is_larger_than_panel`, `tests/test_wishes.py::FairOrder::test_artist_block_does_not_starve_others`, `tests/test_wishes.py::Queue::test_artist_block_interleaves_and_rest_goes_on`
- **F-FRONTA-03** Displej je v pořadí jeden člověk, i když každé zavření přání je nová relace (i starý zápis „panel-…" po restartu).
  Testy: `tests/test_pi0926.py::ArtistWishBudget::test_display_is_one_seat_across_sessions`, `tests/test_review2.py::OldSessionFormat::test_old_panel_key_maps_to_the_seat`
- **F-FRONTA-04** „Zařadit hned" dá přání hned za hrající skladbu (jedno na člověka, displej jako celek jedno) a nic nepřeruší.
  Testy: `tests/test_wishes.py::Queue::test_play_next_is_right_after_current_and_capped`, `tests/test_wishes.py::FairOrder::test_play_next_jumps_the_queue`, `tests/test_review2.py::PanelSeat::test_one_play_next_per_display`
- **F-FRONTA-05** Cizí přání se neutne nikdy, ani „hned teď"; přání první na řadě utne jen podkres nebo vlastní skladbu.
  Testy: `tests/test_wishes.py::Queue::test_explicit_cut_never_cuts_somebody_elses_wish`, `tests/test_wishes.py::Queue::test_wish_never_cuts_somebody_elses_wish`
- **F-FRONTA-06** Vlastní přání odebere jen autor (token); automatika ani stop přání nikdy nemažou.
  Testy: `tests/test_wishes.py::Queue::test_owner_token_removes_and_others_cannot`, `tests/test_wishes.py::WebApi::test_remove_needs_the_owner_token`, `tests/test_wishes.py::Queue::test_remove_while_dj_thinks_discards_result`, `tests/test_wishes.py::Queue::test_auto_reseed_never_removes_requests`, `tests/test_wishes.py::WebApi::test_stop_never_removes_anybodys_wishes`
- **F-FRONTA-07** Nové přání člověka jde před jeho starší, to zůstává; nahrazuje jen oprava („ne, radši…", „místo toho", „zruš"), změna směru nebo skoro stejný text; „a pak…"/„přidej…" jde za ně.
  Testy: `tests/test_pi0926.py::SamePerson::test_rules`, `tests/test_pi0926.py::SamePerson::test_second_wish_goes_first_and_both_stay`, `tests/test_wishes.py::Supersede::test_single_person_new_wish_first_old_stays`, `tests/test_wishes.py::Supersede::test_additive_phrasing_queues_after`
- **F-FRONTA-08** Když čekají jiní, nové přání člověka převezme jeho rozehrané kolo; když nikdo nečeká, hraje hned po hrající skladbě.
  Testy: `tests/test_wishes.py::Supersede::test_with_others_waiting_the_new_wish_takes_the_running_turn`, `tests/test_wishes.py::Supersede::test_single_person_new_wish_first_old_stays`
- **F-FRONTA-09** Jeden člověk má nejvýš 5 rozpracovaných přání (web pak 429); záplava přání jednoho člověka nezdrží ostatní.
  Testy: `tests/test_wishes.py::Queue::test_too_many_from_one_person`, `tests/test_review.py::Spam::test_one_persons_burst_does_not_delay_others`, `tests/test_review.py::Spam::test_additive_burst_takes_turns_with_others`
- **F-FRONTA-10** Tatáž skladba ve dvou přáních zazní jednou.
  Testy: `tests/test_wishes.py::FairOrder::test_same_track_in_two_wishes_plays_once`, `tests/test_wishes.py::FairOrder::test_prefix_is_accounted`
- **F-FRONTA-11** Kdo ≥ 10 min nic svého neslyšel a zeptá se/stěžuje si, jde jeho přání hned po hrající skladbě.
  Testy: `tests/test_pi0926.py::MetaQuestions::test_starved_listener_gets_the_next_turn`
- **F-FRONTA-12** Podkres (rádio) hraje až za všemi přáními; mění ho jen přání nálady/žánru, skladba jen když nikdo jiný nečeká.
  Testy: `tests/test_wishes.py::Queue::test_mood_wish_gets_a_block_and_sets_background`, `tests/test_wishes.py::SongSteersBackground::test_alone_steers`, `tests/test_wishes.py::SongSteersBackground::test_others_waiting_keep_the_background`, `tests/test_wishes.py::WebApi::test_background_after_a_mood_wish_is_the_djs`
- **F-FRONTA-13** Když už žádné přání nemá co hrát (jakkoli skončilo, i „nenašel"), jde podkres za naposledy splněným přáním — pokud se od té doby nezměnil jinak.
  Testy: `tests/test_pi0926.py::BackgroundFollowsLatest::test_after_roberts_wish_background_is_not_old_artist`, `tests/test_review2.py::BackgroundFollow::test_last_wish_notfound_still_moves_background`, `tests/test_review2.py::BackgroundFollow::test_later_background_change_is_not_overridden`
- **F-FRONTA-14** Interpret v podkresu po přání nejvýš 10 skladeb / 40 min, pak „… a podobné".
  Testy: `tests/test_pi0926.py::BackgroundFollowsLatest::test_artist_background_is_bounded`
- **F-FRONTA-15** Stav přání jde s přehráváním (čeká → DJ vybírá → ve frontě → hraje → hotovo) a odhad je v minutách.
  Testy: `tests/test_wishes.py::Queue::test_states_follow_playback`, `tests/test_review.py::Texts::test_eta_in_minutes`, `tests/test_wishes.py::Queue::test_first_sound_is_measured`
- **F-FRONTA-16** Odpověď na přání interpreta zmíní střídání jen, když opravdu čekají jiní; „zařadit hned" poctivě řekne, že čeká na cizí přání.
  Testy: `tests/test_review.py::Texts::test_artist_reply_mentions_turns_only_when_others_wait`, `tests/test_nicks.py::HonestNext::test_play_next_says_it_waits_for_someone_elses_wish`
- **F-FRONTA-17** Během rozhodování DJe má první skladba přání přednost v přípravě.
  Testy: `tests/test_wishes.py::Supersede::test_first_track_is_prioritised_while_deciding`, `tests/test_player_queue.py::Prefetch::test_codex_hold_keeps_list_and_first`
- **F-FRONTA-18** Počty skladeb přání jdou změnit v nastavení jukeboxu, platí hned: kolo (`wish_block`, i blok přání nálady), kolo, když čekají jiní (`wish_shared_block`, nejvýš jako kolo), rozpočet z webu / z displeje (`wish_budget`, `wish_budget_panel`), skladby interpreta v přání (`wish_artist_max`); výchozí hodnoty jsou ty z F-FRONTA-01 a F-FRONTA-02 (3 / 2, 4 / 3) a 12; odpovědi DJe říkají nastavená čísla. (Přidáno 26. 9. 2026 na přání vlastníka: „Proč jen tři? Jde to nastavit v nastavení?")
  Testy: `tests/test_wish_settings.py::WishAmounts::test_defaults_are_the_rules`, `tests/test_wish_settings.py::WishAmounts::test_bad_values_are_clamped`, `tests/test_wish_settings.py::WishAmounts::test_fair_order_follows_the_amounts`, `tests/test_wish_settings.py::WishAmounts::test_settings_change_the_queue_live`, `tests/test_wish_settings.py::WishAmounts::test_settings_form_offers_the_keys_with_bounds`
- **F-FRONTA-19** Rádio (podkres) řekne, odkud je: po přání „Rádio podle přání Robert · nálada" (na webu i na displeji, tlumeným textem, ne jmenovkou přání — ničí přání to není), po rozjezdu „Rádio podle času a dne", jinak „Rádio · vybral DJ"; jméno jde přes kancelářský filtr. (Přidáno 26. 9. 2026 na přání vlastníka: „Co je to, co hraje dál a nemá to už u sebe moje jméno?")
  Testy: `tests/test_wish_settings.py::RadioOrigin::test_radio_after_a_wish_says_whose_wish_it_follows`, `tests/test_wish_settings.py::RadioOrigin::test_background_nobody_asked_for_has_no_name`, `tests/test_wish_settings.py::RadioOrigin::test_name_goes_through_the_office_filter`, `tests/test_wish_settings.py::WebShowsRadioOrigin::test_radio_after_a_wish`, `tests/test_wish_settings.py::WebShowsRadioOrigin::test_other_origins`, `tests/test_panel.py::RadioOriginTest::test_status_strip_says_whose_wish_the_radio_follows`, `tests/test_panel.py::RadioOriginTest::test_panel_reads_the_origin_from_the_status`
- **F-FRONTA-20** Režim oblíbených: po přání oblíbených, které má hrát dál (tak ho DJ vyloží, když si posluchač neřekne jen o pár; „pusť oblíbené“ taky), hraje podkres oblíbené kanceláře (nebo toho člověka), dokud si někdo nepřeje něco jiného — bez limitu 10 skladeb / 40 min, který má interpret v podkresu po přání (F-FRONTA-14). Hraje napřeskáčku (nikdy dvakrát za sebou týž interpret), po lidech na střídačku (F-HLASY-17) a každou oblíbenou jednou, než se kolo zopakuje (pak znovu zamíchané, posledních 8 ne hned); vyřazené hlasováním ani to, co čeká ve frontě, ne. Cizí přání jdou před něj jako vždy a podkres pak jde za naposledy splněným přáním (F-FRONTA-13) — bylo-li to přání oblíbených, znovu oblíbené. Automatické přeseedování v něm nepřijde (jako v režimu interpreta), přežije restart a web i displej píšou „Rádio podle přání Robert · oblíbené kanceláře“ (F-FRONTA-19).
  Nové pravidlo 27. 9. 2026 na přání vlastníka (POZADAVKY #49: „ať hraje to, co máme rádi, napřeskáčku interprety … chtěl jsem, aby to hrál pořád, ale on naplánoval jen pár“).
  Testy: `tests/test_favourites_mode.py::Rotation::test_plays_every_favourite_once_alternating_then_a_new_round`, `tests/test_favourites_mode.py::Rotation::test_bans_apply_and_other_wishes_are_not_duplicated`, `tests/test_favourites_mode.py::Rotation::test_personal_favourites_and_leaving_the_mode`, `tests/test_favourites_mode.py::Rotation::test_boost_does_not_reorder_the_favourites_mode`, `tests/test_favourites_mode.py::OwnerReplay::test_owner_texts_play_favourites_alternating_and_keep_going`, `tests/test_favourites_mode.py::OwnerReplay::test_not_continuous_is_a_block_then_similar_music`

## Přezdívky a identita

- **F-NICK-01** Web se při prvním otevření zeptá na přezdívku a pamatuje si ji; server ji drží podle prohlížeče (id klienta).
  Testy: `tests/test_funkce_guards.py::NickOnFirstOpen::test_page_asks_for_a_nick_when_it_has_none`, `tests/test_nicks.py::Book::test_persisted_per_client`, `tests/test_nicks.py::Api::test_register_read_and_reject`
- **F-NICK-02** Přezdívka má 2–20 znaků, mezery se srovnají; nesmysly a rezervovaná jména („displej", „Host", „DJ") neprojdou.
  Testy: `tests/test_nicks.py::Validation::test_trimmed_and_collapsed`, `tests/test_nicks.py::Validation::test_length`, `tests/test_nicks.py::Validation::test_junk_and_reserved`
- **F-NICK-03** Přezdívka, kterou už někdo používá, se ohlásí (bez ohledu na velikost písmen a diakritiku), ale nezakáže.
  Testy: `tests/test_nicks.py::Book::test_shared_ignores_case_and_diacritics`
- **F-NICK-04** Přezdívka je popisek všude (fronta, hlasy, přeskočení); staří klienti bez přezdívky fungují dál.
  Testy: `tests/test_nicks.py::Api::test_nick_is_the_label_everywhere`, `tests/test_nicks.py::Api::test_old_clients_without_nick_still_work`
- **F-NICK-05** Člověk = klient (prohlížeč / relace displeje), ne jméno ani IP: dva kolegové za jednou sítí jsou dva lidé, cizí jméno nepřebere cizí přání.
  Testy: `tests/test_review.py::Identity::test_anonymous_colleagues_behind_one_nat_are_two_people`, `tests/test_review.py::Identity::test_two_people_at_the_panel_and_a_name_thief`, `tests/test_review.py::Identity::test_client_without_id_never_merges`, `tests/test_nicks.py::Telemetry::test_people_are_counted_by_client_not_by_label`
- **F-NICK-06** Zavření obrazovky přání na displeji = další člověk (nová relace).
  Testy: `tests/test_panel.py::WishTest::test_closing_the_wish_screen_starts_a_new_person`

## Hlasování

- **F-HLASY-01** 👍/👎 skladbě i celému interpretovi; jeden hlas na člověka a položku, jde změnit i stáhnout, u každého hlasu je vidět kdo a kdy; bez přezdívky hlasovat nejde.
  Testy: `tests/test_votes.py::ChangeAndWithdraw::test_change_withdraw_and_persist`, `tests/test_votes.py::Nicks::test_names_go_through_office_filter_and_registry`, `tests/test_votes.py::Api::test_vote_flow_and_status_block`
- **F-HLASY-02** Jiná nahrávka nebo verze téže písně je tatáž píseň; interpret platí pro každého uvedeného („A, B & C", „feat.").
  Testy: `tests/test_votes.py::Keys::test_other_upload_or_version_is_the_same_song`, `tests/test_votes.py::Keys::test_credits`, `tests/test_votes.py::ArtistCredits::test_ban_matches_any_credited_artist`
- **F-HLASY-03** Skladba je vyřazená až od 2 lidí 👎 a víc 👎 než 👍; jeden 👎 ji jen upozadí; 👍 s převahou = oblíbená. Prahy jsou v nastavení.
  Testy: `tests/test_votes.py::Thresholds::test_song_statuses`, `tests/test_votes.py::Thresholds::test_one_voter_cannot_ban_and_threshold_is_config`, `tests/test_review2.py::FavouriteArtistNeedsTwo::test_song_needs_one_and_config_is_live`
- **F-HLASY-04** Interpret je vyřazený od 3 lidí 👎; oblíbený až od 2 různých lidí 👍 s převahou — jeden 👍 z interpreta oblíbeného neudělá.
  Testy: `tests/test_votes.py::Thresholds::test_artist_thumbs_up_and_down`, `tests/test_review2.py::FavouriteArtistNeedsTwo::test_single_thumb_does_not_boost_the_artist`, `tests/test_web_votes.py::Page::test_artist_thumbs_up_from_history_row`
- **F-HLASY-05** Podkres vyřazené nepouští, upozaděné méně (polovina), oblíbené i dřív než po 30 dnech; oblíbený interpret dostává přednost jednou na doplnění.
  Testy: `tests/test_votes.py::Pools::test_banned_never_downweighted_half_favourite_again`, `tests/test_votes.py::FavouriteArtists::test_pool_boost_and_repeat_days`, `tests/test_review2.py::BoostOncePerRefill::test_boost_runs_once_per_refill_not_per_track`, `tests/test_review2.py::BoostOncePerRefill::test_radio_pools_count_refills`
- **F-HLASY-06** Při vyřazení zmizí z fronty podkres (přání nikdy) a hrající podkres se přeskočí; hrající přání ne.
  Testy: `tests/test_votes.py::WithQueue::test_ban_cleans_background_but_keeps_wishes`, `tests/test_votes.py::WithQueue::test_banned_background_playing_is_skipped_wish_is_not`
- **F-HLASY-07** Výslovné přání vyřazené skladby/interpreta se splní s poznámkou, kdo ji vyřadil; ze sady (interpret, oblíbené) vyřazené skladby vypadnou.
  Testy: `tests/test_votes.py::WithQueue::test_explicit_wish_for_banned_song_is_honoured_with_note`, `tests/test_votes.py::WithQueue::test_banned_artist_wish_note_and_set_drops_banned_songs`
- **F-HLASY-08** „Pusť oblíbené (kanceláře)" a „pusť moje oblíbené" jdou bez modelu (i tlačítky na webu); DJ a chytrý start znají oblíbené a vyřazené.
  Testy: `tests/test_votes.py::WithQueue::test_play_favourites_office_and_mine`, `tests/test_votes.py::ContextForDJ::test_favourites_phrases`, `tests/test_votes.py::ContextForDJ::test_describe_and_start_context`, `tests/test_votes.py::FavouriteArtists::test_favourite_mix_adds_artist_songs`, `tests/test_votes.py::FavouriteArtists::test_dj_context_names_favourite_artists`, `tests/test_web_votes.py::Page::test_quick_buttons_are_understood_without_the_model`
- **F-HLASY-09** Nejvýš 30 hlasů na člověka za 10 min (pak 429); 👍 z importu playlistu se nepočítají — import jen rozšíří oblíbené jako lajky, nic nepouští (má vlastní pojistku F-HLASY-13).
  Změněno 27. 9. 2026 se souhlasem vlastníka: „asi to neomezuj. Import nemá hned přehrát vše, má jen použít seznam skladeb pro rozšíření oblíbených, jako kdyby uživatel dal skladbě lajk."
  Testy: `tests/test_votes.py::RateLimit::test_30_per_10_minutes`, `tests/test_votes.py::Api::test_rate_limit_is_429`
- **F-HLASY-10** Web hlasuje u hrající, fronty i odehraných (hlas z Odehráno patří té skladbě), stránka Hlasování ukazuje oblíbené, čekající, vyřazené a moje hlasy.
  Testy: `tests/test_web_votes.py::History::test_vote_from_history_hits_that_song_not_the_playing_one`, `tests/test_web_votes.py::Page::test_page_uses_the_contract`, `tests/test_votes.py::Api::test_status_without_votes_module`
- **F-HLASY-11** Displej ukazuje hlasy: ♥ oblíbená, ▲/▼ počty, „vyřazená hlasováním", značky ve frontě; bez hlasů kreslí jako dřív.
  Testy: `tests/test_panel.py::VotesTest::test_vote_mark`, `tests/test_panel.py::VotesTest::test_badge_texts`, `tests/test_panel.py::VotesTest::test_no_votes_draws_exactly_as_before`, `tests/test_panel.py::VotesTest::test_queue_rows_carry_the_verdict_of_their_track`, `tests/test_panel.py::VotesEndToEndTest::test_votes_of_the_current_track_reach_the_player`
- **F-HLASY-12** Import playlistu (stránka Hlasování, jen s přezdívkou): odkaz na playlist z YouTube Music nebo YouTube (i skladba v playlistu, youtu.be s `list=`, holé id) = 👍 importujícího každé písničce v něm, pod jeho přezdívkou a u hlasu „z playlistu ‚Název‘“; nedostupná videa a nepísničky (podcast, delší než 15 min, kratší než 30 s, bez interpreta) se vynechají a import řekne kolik; mix/rádio YouTube (id `RD…` kromě výběrů YouTube Music `RDCLAK5uy_…`), „To se mi líbí“/„Přehrát později“ a odkaz na jednu skladbu se odmítnou s vysvětlením; soukromý nebo neexistující playlist dostane radu „nastav ho jako Neveřejný (s odkazem)“.
  Nové pravidlo 26. 9. 2026 na přání vlastníka (POZADAVKY #48: „importovat playlisty a použít je pro oblíbené“).
  Testy: `tests/test_playlist_import.py::ParseRef::test_url_variants`, `tests/test_playlist_import.py::ParseRef::test_refusals_are_czech_and_specific`, `tests/test_playlist_import.py::Normalize::test_skips_non_music_and_unavailable`, `tests/test_playlist_import.py::Normalize::test_private_or_missing_says_how_to_share`, `tests/test_playlist_import.py::Normalize::test_network_error_and_timeout`, `tests/test_playlist_import.py::ImportVotes::test_import_is_thumbs_up_under_the_nick_with_source`
- **F-HLASY-13** Z playlistů má jeden člověk nejvýš `playlist_import_max` (1000) písniček, všechny jeho playlisty dohromady, a nejvýš 10 playlistů; co se nevejde, import řekne i s limitem. Importovat a obnovovat jde nejvýš 6× za 10 min a jeden import člověka naráz.
  Nové pravidlo 26. 9. 2026 na přání vlastníka (POZADAVKY #48: „omezená velikost“).
  Testy: `tests/test_playlist_import.py::ImportVotes::test_cap_per_import_and_per_person`, `tests/test_playlist_import.py::ImportVotes::test_rate_limit_and_one_at_a_time`
- **F-HLASY-14** Vlastní hlas má přednost před playlistem: vlastní 👍 i 👎 importujícího zůstanou, jak jsou, vlastní stažení hlasu taky, když je novější než písnička v playlistu; 👍 dané ručně písničce z playlistu přežije odebrání playlistu. Jiná verze téže písně ani písnička, kterou už člověk má, se nezapočítá dvakrát a import to řekne.
  Nové pravidlo 26. 9. 2026 na přání vlastníka (POZADAVKY #48).
  Testy: `tests/test_playlist_import.py::ImportVotes::test_duplicates_and_own_votes_win`, `tests/test_playlist_import.py::ImportVotes::test_explicit_vote_on_imported_song_is_kept_after_removal`
- **F-HLASY-15** Seznam importovaných playlistů (kdo, název, kolik písniček, kdy) vidí všichni; Obnovit (načte playlist znovu: nové písničky přidá, ty, co v něm už nejsou, ubere) a Odebrat (stáhne všechny 👍 toho playlistu) jde jen u vlastního (id prohlížeče). Importy přežijí restart.
  Nové pravidlo 26. 9. 2026 na přání vlastníka (POZADAVKY #48: „vidět kdo co importoval, odebrat/obnovit“).
  Testy: `tests/test_playlist_import.py::ImportVotes::test_refresh_adds_and_removes`, `tests/test_playlist_import.py::ImportVotes::test_remove_withdraws_and_persists`, `tests/test_playlist_import.py::Api::test_only_own_imports_can_be_managed`
- **F-HLASY-16** 👍 z playlistu je 👍 písničce jako každý jiný (oblíbená, podkres ji smí i dřív než po 30 dnech, „pusť moje oblíbené“ ji hraje) a kolegové ji 👎 vyřadí podle F-HLASY-03; celému interpretovi import 👍 nikdy nedá, takže z playlistů oblíbený interpret nevznikne.
  Nové pravidlo 26. 9. 2026 na přání vlastníka (POZADAVKY #48: „bez 👍 celým interpretům“).
  Testy: `tests/test_playlist_import.py::ImportVotes::test_colleagues_thumbs_down_still_ban`, `tests/test_playlist_import.py::ImportVotes::test_imports_never_make_a_favourite_artist`, `tests/test_playlist_import.py::Fairness::test_radio_uses_imported_favourites_like_any_favourite`
- **F-HLASY-17** Oblíbené kanceláře jsou férové k lidem: „pusť oblíbené“, oblíbené pro DJe i chytrý start se berou po lidech na střídačku, takže každý, kdo dal 👍, přispěje stejným dílem, ať má 5 oblíbených, nebo playlist o 300 písničkách; v kole jde první skladba s nejvíc 👍 a u každého jeho vlastní 👍 před playlistem. V podkresu se písničky, které drží jen playlisty, posunou v rádiu dopředu nejvýš 2 za člověka na jedno doplnění.
  Změněno 27. 9. 2026 se souhlasem vlastníka: „pusť oblíbené" se střídá po lidech (dřív jen podle počtu 👍) — velký import nepřehlasuje ostatní.
  Nové pravidlo 26. 9. 2026 na přání vlastníka (POZADAVKY #48: „férově, každý člověk zhruba stejným dílem“).
  Testy: `tests/test_playlist_import.py::Fairness::test_each_person_gets_an_equal_share`, `tests/test_playlist_import.py::Fairness::test_most_liked_first_and_own_before_playlist`, `tests/test_playlist_import.py::Fairness::test_dj_summary_is_fair_and_stable`, `tests/test_playlist_import.py::Fairness::test_pool_boost_capped_for_playlist_only_songs`
- **F-HLASY-18** Import nezdrží hudbu ani web: YouTube se čte mimo hlavní smyčku s limitem 40 s, zápis jde jednou transakcí přes vlákno zápisů a stav webu (`/api/status`) seznam importů nenese.
  Nové pravidlo 26. 9. 2026 (POZADAVKY #48; SD karta je pomalá, F-PROVOZ-04).
  Testy: `tests/test_playlist_import.py::LoopNotBlocked::test_import_keeps_the_loop_responsive`, `tests/test_playlist_import.py::Api::test_only_own_imports_can_be_managed`
- **F-HLASY-19** Přání oblíbených pozná DJ podle smyslu, ne podle frází: „hraj, co máme rádi“, „naše srdcovky a nepřestávej“, „co mám v playlistu“ vyloží model (akce oblíbené: čí — kanceláře, nebo toho, kdo píše; jestli hrát dál; jestli střídat interprety) a skladby vybere aplikace z hlasování, nikdy z historie. Model k tomu dostává přehled oblíbených (kolik skladeb, od kolika lidí a interpretů, hlavní interpreti, kolik jich má ten, kdo píše). Bez modelu jen přesné „pusť (moje) oblíbené“ (F-HLASY-08); „oblíbené od Kabátu“ je interpret.
  Nové pravidlo 27. 9. 2026 na přání vlastníka (POZADAVKY #49; „nechci, aby se učil konkrétní fráze … chci, aby chápal, co mu user napíše“, PLAN Vize 5).
  Testy: `tests/test_favourites_mode.py::ModelDecision::test_schema_lets_the_model_say_scope_continuous_and_alternation`, `tests/test_favourites_mode.py::ModelDecision::test_app_server_decision_becomes_a_favourites_intent`, `tests/test_favourites_mode.py::ModelDecision::test_no_phrase_matching_only_the_exact_command`, `tests/test_favourites_mode.py::PromptKnowsFavourites::test_overview_line_counts_people_artists_and_the_askers_own`, `tests/test_manual.py::NapovedaIsTrue::test_model_understood_examples_are_measured`
- **F-HLASY-20** Blok přání oblíbených hraje napřeskáčku: nikdy dvakrát za sebou týž interpret (a když to jde, ani v posledních čtyřech), pokud si posluchač výslovně neřekne jinak.
  Nové pravidlo 27. 9. 2026 na přání vlastníka (POZADAVKY #49: „napřeskáčku interprety“).
  Testy: `tests/test_favourites_mode.py::PromptKnowsFavourites::test_block_of_favourites_alternates_artists`, `tests/test_favourites_mode.py::OwnerReplay::test_owner_texts_play_favourites_alternating_and_keep_going`

## Displej

- **F-DISPLEJ-01** Displej ukazuje, co hraje (název, interpret, čas), a ovládá hrát/pauza, další a hlasitost; ťuknutí dál než 10 px od tlačítka nic nedělá (mezera mezi tlačítky patří nejbližšímu). Horní lišta je 46 px vysoká a tlačítka Mobil, Přání a Síť jsou v ní nakreslená o 14 px níž, dál od rámečku (stará 32px lišta jen na vyžádání: `YTDJ_PANEL_TALL_STRIP=0`).
  Změněno 26. 9. 2026 se souhlasem vlastníka: dřív „ťuknutí mimo tlačítko nic nedělá"; mezery 6–8 px mezi tlačítky teď patří nejbližšímu tlačítku, aby se dalo trefit.
  Změněno 27. 9. 2026 se souhlasem vlastníka: vyšší horní lišta — horní tlačítka o 14 px níž, dál od rámečku (Test prstem: u okraje prst čte o 23–54 px níž).
  Testy: `tests/test_panel_touch.py::TallStripTest::test_tall_by_default_and_the_old_strip_on_request`, `tests/test_panel_touch.py::PressRulesTest::test_nearest_target_within_grab`, `tests/test_panel_touch.py::PressRulesTest::test_player_touch_areas_cover_the_gaps`, `tests/test_panel_touch.py::AppTouchTest::test_press_in_the_gap_between_play_and_next`
  Testy: `tests/test_panel.py::EndToEndTest::test_play_pause`, `tests/test_panel.py::EndToEndTest::test_next`, `tests/test_panel.py::EndToEndTest::test_tap_off_button_does_nothing`, `tests/test_panel.py::PlayerLookTest::test_title_as_big_as_fits`
- **F-DISPLEJ-02** Hrát v tichu (nic nehraje) rozjede DJe.
  Testy: `tests/test_panel.py::WishTest::test_play_in_silence_asks_the_server_to_start`
- **F-DISPLEJ-03** Když ytdj neběží, displej to řekne a sám se znovu připojí; bez SSE jede přes dotazování.
  Testy: `tests/test_panel.py::EndToEndTest::test_offline_and_back`, `tests/test_panel.py::PollingFallbackTest::test_without_sse`
- **F-DISPLEJ-04** Obrazovka Přání: rychlé volby, klávesnice s háčky a čárkami, prázdné přání se nepošle, „Hned po téhle skladbě", odpověď DJe (i po odchodu), při chybě „zkusit znovu"; po 120 s bez dotyku zpět na přehrávač.
  Testy: `tests/test_panel.py::WishTest::test_quick_pick_sends_and_answers`, `tests/test_panel.py::WishTest::test_czech_keyboard`, `tests/test_panel.py::WishTest::test_empty_wish_is_not_sent`, `tests/test_panel.py::WishTest::test_right_after_this`, `tests/test_panel.py::WishTest::test_answer_comes_back_after_leaving`, `tests/test_panel.py::WishTest::test_failure_offers_retry`, `tests/test_panel.py::WishLayoutTest::test_idle_close`, `tests/test_panel.py::WishLayoutTest::test_keyboard_keys_fit_and_do_not_overlap`
- **F-DISPLEJ-05** Fronta na displeji: jmenovky u přání, řádek „Pak: …", počet dalších přání; odebrat jde jen přání z displeje.
  Testy: `tests/test_panel.py::WishTest::test_queue_page_removes_only_own`, `tests/test_panel.py::PlayerLookTest::test_next_wish_line_changes_only_its_row`, `tests/test_panel.py::PanelBehaviourTest::test_more_wishes_counted_besides_next`
- **F-DISPLEJ-06** Po 5 min ticha klidový režim (ztlumený jas), první dotyk jen probudí; hudba ho drží vzhůru; v tichu a klidu se ukáže QR „Přání z mobilu".
  Testy: `tests/test_panel.py::PanelBehaviourTest::test_rest_dims_and_first_touch_only_wakes`, `tests/test_panel.py::PanelBehaviourTest::test_music_keeps_it_awake`, `tests/test_panel.py::NewLookTest::test_silence_and_rest_show_the_qr`
- **F-DISPLEJ-07** Tlačítko telefonu otevře velký QR kód s adresou webu.
  Testy: `tests/test_panel.py::PanelBehaviourTest::test_phone_button_opens_the_qr_page`
- **F-DISPLEJ-08** Nové přání se na pár vteřin ohlásí pruhem („Petr si přeje: …").
  Testy: `tests/test_panel.py::PanelBehaviourTest::test_new_wish_shows_a_banner_for_a_few_seconds`, `tests/test_panel.py::NewLookTest::test_wish_banner_is_one_strip_in_and_out`
- **F-DISPLEJ-09** Obal alba (nebo iniciály) se stáhne jednou na pozadí a drží se v malé cache; chyba se hned neopakuje.
  Testy: `tests/test_panel.py::ArtTest::test_centre_square_of_a_letterboxed_cover`, `tests/test_panel.py::ArtTest::test_fetched_once_in_the_background_then_cached`, `tests/test_panel.py::ArtTest::test_failure_is_not_retried_at_once`, `tests/test_panel.py::ArtTest::test_keeps_only_a_few_tiles`, `tests/test_panel.py::NewLookTest::test_cover_or_initials_in_the_art_slot`
- **F-DISPLEJ-10** Srozumitelné české texty: výpadek řekne proč a že přání počkají, jednořádkový stav, v tichu žádný prázdný pruh průběhu, české zkracování textů.
  Testy: `tests/test_panel.py::PlayerLookTest::test_outage_says_why_and_that_wishes_wait`, `tests/test_panel.py::PlayerLookTest::test_friendly_copy`, `tests/test_panel.py::PlayerLookTest::test_silence_has_no_empty_progress_bar`, `tests/test_panel.py::NewLookTest::test_status_strip_says_one_thing`, `tests/test_panel.py::TextTest::test_ellipsize_czech`, `tests/test_panel.py::TextTest::test_wrap_two_lines`, `tests/test_panel.py::TextTest::test_wrap_one_giant_word`
- **F-DISPLEJ-11** Na displej jde jen to, co se změnilo (tik hodin je pár pixelů), plus průběžné překreslení po pruzích, aby se případná chyba srovnala sama.
  Testy: `tests/test_panel.py::RenderTest::test_merge`, `tests/test_panel.py::RenderTest::test_tick_is_tiny`, `tests/test_panel.py::PanelBehaviourTest::test_rolling_refresh_sends_a_band_not_a_frame`, `tests/test_panel.py::VotesTest::test_a_vote_redraws_only_the_text_block`
- **F-DISPLEJ-12** Dotyk: stisk platí pro tlačítko, na kterém prst během stisku ležel (medián poloh po náběhu tlaku, bez posledních vzorků před zvednutím), ne pro to, kam jen dosedl; zruší ho jen zřetelné sjetí; šum převodníku stisk nepřeruší ani neukončí; hned při dotyku se tlačítko orámuje a rámeček se přesune, když prst leží jinde (nikdy ne do lišty hlasitosti); tlačítka v horní liště přehrávače berou dotyk až k hornímu okraji obrazovky; na vedlejších obrazovkách (přání, fronta, síť, Wi-Fi sítě, kalibrace, testy) je nahoře jen nadpis a „Zpět" je dole jako velké tlačítko (aspoň 60 px vysoké); minutí se logují s nejbližším tlačítkem. Dvojí ťuknutí přes změnu obrazovky nic nezmáčkne, cíle se nepřekrývají, anomálie převodníku se spočítají.
  Změněno 26. 9. 2026 se souhlasem vlastníka: přesnější znění po opravě dotyku (dosednutí = medián 2 ze 3 vzorků do 24 px, šum neukončí stisk).
  Změněno 26. 9. 2026 se souhlasem vlastníka: tlačítko podle toho, kde prst ležel, ne kam dosedl („prst mám přes celé −volume, ale aktivuje se Play"); horní tlačítka až k okraji („problém s tlačítky nahoře: síť, přání, zpět").
  Změněno 27. 9. 2026 se souhlasem vlastníka: Zpět dole jako velké tlačítko — prstem u horního okraje displej čte nespolehlivě (±35–113 px).
  Testy: `tests/test_panel_touch.py::AppTouchTest::test_owner_case_finger_on_volume_minus_reading_high`, `tests/test_panel_touch.py::AppTouchTest::test_top_strip_reading_above_the_screen_edge_still_hits`, `tests/test_panel_touch.py::BackAtTheBottomTest::test_back_is_big_and_at_the_bottom`, `tests/test_panel_touch.py::BackAtTheBottomTest::test_nothing_to_tap_in_the_top_band`, `tests/test_panel_touch.py::BackAtTheBottomTest::test_player_strip_still_reaches_the_top_edge`, `tests/test_panel_touch.py::PressRulesTest::test_jitter_and_a_lift_drift_keep_the_press`, `tests/test_panel_touch.py::PressRulesTest::test_sustained_drag_away_cancels`, `tests/test_panel_touch.py::DriverReplayTest::test_scattered_landing_with_a_noisy_sample_still_presses`, `tests/test_panel_touch.py::DriverReplayTest::test_noise_while_held_is_not_a_release`, `tests/test_panel_touch.py::DriverReplayTest::test_a_single_touchy_sample_is_not_a_press`, `tests/test_panel_touch.py::AppTouchTest::test_jittery_press_on_next_counts`, `tests/test_panel_touch.py::AppTouchTest::test_drag_away_does_not_skip`, `tests/test_panel_touch.py::AppTouchTest::test_feedback_ring_shows_before_release`, `tests/test_panel_touch.py::AppTouchTest::test_miss_is_logged_with_the_nearest_button`
  Testy: `tests/test_panel.py::PanelBehaviourTest::test_double_tap_through_a_page_switch_does_nothing`, `tests/test_panel.py::WishLayoutTest::test_player_targets_do_not_overlap`, `tests/test_panel.py::TouchDriverStatsTest::test_anomalies_are_counted_and_summarised`, `tests/test_panel.py::TouchDriverStatsTest::test_calibration_source`, `tests/test_panel.py::TouchDriverStatsTest::test_repeated_errors_are_throttled`
- **F-DISPLEJ-14** Kalibrace dotyku z displeje (síť → „Kalibrace dotyku"): 12 křížků v mřížce 3×4 s řádky přímo u horního a dolního okraje (y 14 a 306); křížky u okraje se ťukají dvakrát, a když se obě čtení liší o víc než 20 px, křížek se vynechá („Horní/Dolní okraj čte prst nespolehlivě — kalibruji bez něj") a převezme opravu vnitřního souseda; afinní oprava se prokládá jen vnitřními body (tam jsou tlačítka), mřížka zbytků je na vnitřních bodech přesná a u okraje smí od vnitřku odbočit nejvýš o 15 px (totéž se uplatní i na uloženou kalibraci při načtení); za mřížkou platí oprava krajních uzlů; uloží se a platí hned jen když vnitřní body sedí (žádný dál než 35 px po afinní opravě), jinak se nic nezmění a nabídne se „Znovu"; bez dotyku 45 s se vzdá. Uložené kalibrace z 5 bodů (bez mřížky) a z 9 bodů (mřížka 3×3) platí dál.
  Nové pravidlo 26. 9. 2026 se souhlasem vlastníka.
  Změněno 26. 9. 2026 se souhlasem vlastníka: 9 bodů místo 5 — po kalibraci 5 body zůstala na Pi nerovnoměrnost 12 px (vlevo dole se četlo výš, vpravo dole níž).
  Změněno 27. 9. 2026 se souhlasem vlastníka (spolehlivý dotyk): 12 bodů, řádky křížků přímo u horního a dolního okraje — Test prstem ukázal, že u okraje prst čte o 23–54 px k středu, a mřížka 40 px od okraje to nepokryla.
  Změněno 27. 9. 2026 se souhlasem vlastníka (spolehlivý dotyk): prst u okraje čte nahodile (týž křížek jednou y 73, podruhé y 21; kalibrace z 01:55 ohnula tlačítka o 20–46 px) — krajní křížky dvakrát, nespolehlivé se vynechají, oprava u okraje nejvýš 15 px od vnitřku, afinní část jen z vnitřních bodů.
  Testy: `tests/test_panel_touch.py::GridCalibrationTest::test_twelve_points_edges_follow_only_up_to_the_cap`, `tests/test_panel_touch.py::GridCalibrationTest::test_the_logged_attempts_no_longer_distort`, `tests/test_panel_touch.py::GridCalibrationTest::test_the_logged_double_reading_is_left_out`, `tests/test_panel_touch.py::GridCalibrationTest::test_the_loader_caps_an_existing_bad_grid`, `tests/test_panel_touch.py::CalibrationFlowTest::test_an_edge_cross_that_reads_differently_twice_is_left_out`, `tests/test_panel_touch.py::GridCalibrationTest::test_beyond_the_grid_the_edge_correction_holds`, `tests/test_panel_touch.py::GridCalibrationTest::test_nine_point_files_still_load`, `tests/test_panel_touch.py::GridCalibrationTest::test_a_slipped_edge_point_is_capped_not_refused`, `tests/test_panel_touch.py::GridCalibrationTest::test_a_slipped_inner_point_is_still_refused`, `tests/test_panel_touch.py::GridCalibrationTest::test_nine_points_fix_what_five_cannot`, `tests/test_panel_touch.py::GridCalibrationTest::test_grid_survives_save_and_load`, `tests/test_panel_touch.py::GridCalibrationTest::test_recalibrating_again_does_not_stack_grids`, `tests/test_panel_touch.py::GridCalibrationTest::test_a_slipped_point_among_nine_is_refused`, `tests/test_panel_touch.py::CalibrationFlowTest::test_nine_crosses_straighten_a_bent_glass`, `tests/test_panel_touch.py::CalibrationTest::test_composes_on_top_of_the_current_calibration_and_saves`, `tests/test_panel_touch.py::CalibrationTest::test_points_that_dont_fit_change_nothing`, `tests/test_panel_touch.py::CalibrationFlowTest::test_calibrate_then_taps_land_where_aimed`, `tests/test_panel_touch.py::CalibrationFlowTest::test_a_slipped_point_changes_nothing_and_offers_again`, `tests/test_panel_touch.py::CalibrationFlowTest::test_leaving_it_alone_gives_up`
- **F-DISPLEJ-15** Test dotyku z displeje (síť → „Test dotyku"): přes obrysy tlačítek přehrávače ukazuje živě, kde displej čte prst, a po zvednutí, které tlačítko by se stisklo; nic nemačká; „Zpět" nebo 60 s bez dotyku vrátí přehrávač.
  Nové pravidlo 26. 9. 2026 se souhlasem vlastníka (spolehlivý dotyk a jeho kontrola).
  Testy: `tests/test_panel_touch.py::TouchTestScreenTest::test_shows_the_reading_presses_nothing_and_goes_back`
- **F-DISPLEJ-16** Test prstem z displeje (síť → „Test prstem"): 12 označených míst (4 úplně u horního okraje) se ťukají prstem; ke každému se do logu zapíše cíl, všechny surové vzorky převodníku (x, y, Z1, Z2, rozptyl, proč byl vzorek odmítnut), kalibrované polohy, rozhodnutý bod a tlačítko, které by se stisklo (`panel.touch_probe`); nic se nemačká; na konci shrnutí chyby u okraje a jinde; 60 s bez dotyku se vzdá.
  Nové pravidlo 26. 9. 2026 se souhlasem vlastníka (spolehlivý dotyk: najít, proč prst u horního okraje čte níž).
  Testy: `tests/test_panel_touch.py::FingerTestFlowTest::test_twelve_spots_log_a_probe_each`, `tests/test_panel_touch.py::DriverDiagnosticsTest::test_recording_keeps_raw_pressure_and_positions`, `tests/test_panel_touch.py::DriverDiagnosticsTest::test_invalid_reasons_are_counted`, `tests/test_panel_touch.py::ProbeReportTest::test_per_spot_error_spread_and_pressure`
- **F-DISPLEJ-13** Ovladač displeje KeDei 3.5" v6.2 a dotykového čipu XPT2046 (přímý přístup na SPI/GPIO).
  (bez testu: hardwarový protokol displeje; ověřuje `python -m ytdj.panel.kedei --test` na Pi)

## Web

- **F-WEB-01** Všichni vidí jeden společný stav (SSE, jeden snímek za tik pro všechny); přání vidí každý hned; při pauze chodí viditelné pingy.
  Testy: `tests/test_web_sse.py::SseTest::test_one_snapshot_per_tick_for_many_clients`, `tests/test_web_sse.py::SseTest::test_wish_is_visible_to_everybody`, `tests/test_web_sse.py::SseTest::test_paused_player_gets_visible_pings`, `tests/test_web_sse.py::SseTest::test_unknown_source_is_web`
- **F-WEB-02** Stop je jen pauza a nikomu nemaže přání; tlačítko ■ na webu není.
  Testy: `tests/test_wishes.py::WebApi::test_stop_never_removes_anybodys_wishes`, `tests/test_wishes.py::WebApi::test_status_carries_build_and_index_is_not_cached`
- **F-WEB-03** Stará otevřená stránka se sama obnoví, když se na Pi změní verze (build ve stavu, stránka bez cache).
  Testy: `tests/test_wishes.py::WebApi::test_status_carries_build_and_index_is_not_cached`
- **F-WEB-04** ▶ v tichu rozjede DJe podle chytrého startu, i ze staré stránky; starý klient bez `wait` dostane odpověď jako dřív.
  Testy: `tests/test_wishes.py::WebApi::test_play_in_silence_starts_the_dj_with_context`, `tests/test_wishes.py::WebApi::test_stale_page_start_prompt_is_an_idle_start`, `tests/test_wishes.py::WebApi::test_old_client_still_gets_the_reply`
- **F-WEB-05** Stránka jde do telefonů zabalená (gzip, ~25 kB) a nezměněná jen jako 304; ikona bez 404.
  Testy: `tests/test_web_votes.py::IndexServing::test_gzip_etag_and_304`
- **F-WEB-06** Stav nese stav mozku DJe a výsledek chytrého startu.
  Testy: `tests/test_review.py::StartYields::test_status_carries_dj_brain`, `tests/test_review.py::StartYields::test_start_result_is_in_status`
- **F-WEB-07** Stránka Nápověda (`/napoveda`, tlačítko „?" v hlavičce webu a odkaz „Jak to funguje?" na uvítací kartě s přezdívkou) je rychlý návod pro kolegy: přání s příklady, fronta a férovost, hlasování, přezdívka, displej, co dělat při výpadku a tipy; příklady přání aplikace opravdu takhle chápe a čísla (kolo, rozpočet, prahy hlasování) jsou ta nastavená.
  Nové pravidlo 26. 9. 2026 na přání vlastníka (POZADAVKY #47: „stránka, která by sloužila jako návod k použití").
  Testy: `tests/test_manual.py::NapovedaIsTrue::test_examples_are_understood`, `tests/test_manual.py::NapovedaIsTrue::test_owner_example_pust_kabat_is_an_artist`, `tests/test_manual.py::NapovedaIsTrue::test_the_chips_it_names_are_on_the_page`, `tests/test_manual.py::NapovedaIsTrue::test_numbers_follow_the_settings`, `tests/test_manual.py::NapovedaIsTrue::test_night_rule_matches_funkce`, `tests/test_manual.py::NapovedaIsTrue::test_reachable_from_the_header_and_the_nick_card`, `tests/test_manual.py::Serving::test_pages_gzip_etag_and_304`
- **F-WEB-08** Stránka Jak to funguje (`/jak-to-funguje`) se skládá živě z tohoto souboru (bez odkazů na testy, s čísly a poznámkami o souhlasu vlastníka) a z úvodů v `docs/JAK-TO-FUNGUJE.md`: každá oblast má úvod a počet pravidel, hledání nedbá na diakritiku, oddíl „Automatiky, o kterých možná nevíš", u pravidel s nastavením jeho současná hodnota a tabulka změn se souhlasem vlastníka; po změně souboru nebo nastavení se složí znovu (bez restartu, mimo hlavní smyčku) a jde zabalená jako hlavní stránka.
  Nové pravidlo 26. 9. 2026 na přání vlastníka (POZADAVKY #47: „dokumentace, ve které budou kompletní informace o tom, jak to pracuje").
  Testy: `tests/test_manual.py::DocsFromFunkce::test_every_area_has_an_intro_and_is_on_the_page`, `tests/test_manual.py::DocsFromFunkce::test_test_references_are_hidden_numbers_and_consent_kept`, `tests/test_manual.py::DocsFromFunkce::test_parser_on_a_small_file`, `tests/test_manual.py::DocsFromFunkce::test_automatic_behaviours_section`, `tests/test_manual.py::DocsFromFunkce::test_settings_show_the_current_value`, `tests/test_manual.py::DocsFromFunkce::test_page_follows_funkce_without_a_restart`, `tests/test_manual.py::DocsFromFunkce::test_missing_funkce_is_said_not_crashed`, `tests/test_manual.py::DocsFromFunkce::test_search_box_and_collapsible_areas`, `tests/test_manual.py::DocsFromFunkce::test_deploy_ships_docs`, `tests/test_manual.py::Serving::test_pages_gzip_etag_and_304`, `tests/test_manual.py::Serving::test_build_runs_off_the_event_loop`

## Síť a Wi-Fi

- **F-SIT-01** Ikona sítě na displeji otevře přehled (jméno, Ethernet, Wi-Fi, adresy webu) i když ytdj neběží; tlačítka repráku a stav hrají dál.
  Testy: `tests/test_panel_net.py::NetFlowTest::test_open_and_back`, `tests/test_panel_net.py::NetFlowTest::test_net_button_works_while_ytdj_is_down`, `tests/test_panel_net.py::NetFlowTest::test_media_keys_and_state_while_open`, `tests/test_panel_net.py::NmcliBackendTest::test_status`
- **F-SIT-02** Přihlášení k Wi-Fi z displeje: seznam sítí, heslo na klávesnici, otevřená síť bez hesla, neúspěch jde zopakovat.
  Testy: `tests/test_panel_net.py::NetFlowTest::test_list_scroll_and_connect_with_password`, `tests/test_panel_net.py::NetFlowTest::test_open_network_and_cancel`, `tests/test_panel_net.py::NetFlowTest::test_connect_failure_and_retry`, `tests/test_panel_net.py::NmcliBackendTest::test_connect_ok`, `tests/test_panel_net.py::ParseTest::test_wifi_list`
- **F-SIT-03** QR kód adresy webu je čitelný (vlastní kodér).
  Testy: `tests/test_panel_net.py::QrTest::test_sizes_and_finders`, `tests/test_panel_net.py::QrTest::test_decodes`
- **F-SIT-04** Chybová hláška sítě je česky a srozumitelně; bez nmcli se displej nezhroutí.
  Testy: `tests/test_panel_net.py::ParseTest::test_friendly_error`, `tests/test_panel_net.py::NmcliBackendTest::test_no_nmcli`, `tests/test_panel_net.py::ParseTest::test_split_terse_escapes`, `tests/test_panel_net.py::ParseTest::test_dev_show`

## Výpadky (YouTube, Codex, síť)

- **F-VYPADEK-01** Když vypadne síť nebo YouTube (2 selhání za sebou), nic se nečerní: fronta i přání čekají, zkouší se spojení (2 s, pak dvojnásobek, nejvýš minuta) a po obnově se pokračuje.
  Testy: `tests/test_outage.py::Outage::test_network_outage_holds_queue_and_resumes`, `tests/test_outage.py::Outage::test_single_failure_is_not_an_outage`, `tests/test_outage.py::Classify::test_content_vs_transient`, `tests/test_outage.py::Classify::test_outage_reason`
- **F-VYPADEK-02** Vadná skladba je chyba skladby, ne výpadek; skladba, která za sítě pořád selhává, se vzdá; Další při výpadku jen posune místo navázání.
  Testy: `tests/test_outage.py::Outage::test_content_error_is_error_not_outage`, `tests/test_outage.py::Outage::test_track_that_keeps_failing_online_is_given_up`, `tests/test_outage.py::Outage::test_next_during_outage_moves_resume_point`
- **F-VYPADEK-03** Selhání rádia nesmaže náladu; zkusí se znovu později.
  Testy: `tests/test_outage.py::RadioResilience::test_pool_survives_radio_failure`
- **F-VYPADEK-04** Když nejede mozek DJe (přihlášení, limit, síť), jistič přestane čekat na model, zkouší s rostoucím odstupem a po novém přihlášení hned; posluchač nikdy nevidí surovou chybu.
  Testy: `tests/test_dj_offline.py::BreakerTest::test_opens_backs_off_and_recovers`, `tests/test_dj_offline.py::BreakerTest::test_single_odd_error_does_not_open`, `tests/test_dj_offline.py::BreakerTest::test_new_login_retries_at_once`, `tests/test_dj_offline.py::Classify::test_kinds`, `tests/test_dj_offline.py::OfflineDJ::test_raw_error_never_reaches_the_listener`, `tests/test_dj_offline.py::OfflineDJ::test_turn_reply_is_friendly`, `tests/test_dj_offline.py::OfflineDJ::test_recovers_after_probe`, `tests/test_review.py::Texts::test_codex_errors_are_never_shown_raw`
- **F-VYPADEK-05** Bez modelu fungují přání interpreta a písně a tlačítka nálad (klidnější, živější, jen česky, víc takového, něco jiného, překvap mě).
  Testy: `tests/test_dj_offline.py::Chips::test_local_moods`, `tests/test_dj_offline.py::OfflineDJ::test_more_like_this_works_without_the_model`, `tests/test_dj_offline.py::OfflineDJ::test_czech_from_history`, `tests/test_dj_offline.py::OfflineDJ::test_calmer_uses_youtube_moods`
- **F-VYPADEK-06** Tah posluchače má nejvýš 25 s (studený start +15 s); vypršení = „DJ nestihl odpovědět", ne „síť"; neplatné přihlášení, 401 a 429 končí hned.
  Testy: `tests/test_dj_offline.py::OfflineDJ::test_listener_budget`, `tests/test_dj_offline.py::OfflineDJ::test_cold_start_gets_extra_budget`, `tests/test_dj_appserver.py::Fake::test_auth_error_fails_fast_even_while_codex_retries`, `tests/test_dj_appserver.py::Fake::test_invalidated_token_on_stderr_fails_fast`, `tests/test_dj_appserver.py::Fake::test_rate_limit_with_retry_fails_fast`, `tests/test_dj_offline.py::ExecFallbackStopsOnFatal::test_no_fresh_exec_after_login_error`
- **F-VYPADEK-07** Spadlý nebo zaseknutý Codex (app-server) tah ukončí a příště se nastartuje znovu; při chybě app-serveru se použije `codex exec`.
  Testy: `tests/test_dj_appserver.py::Fake::test_crash_mid_turn_raises_and_next_turn_restarts`, `tests/test_dj_appserver.py::Fake::test_hang_times_out`, `tests/test_dj_appserver.py::Fake::test_error_notification_fails_the_turn`, `tests/test_dj_appserver.py::DJFallback::test_falls_back_to_exec`, `tests/test_dj_appserver.py::DJFallback::test_app_server_answers`

## Restart a obnova

- **F-RESTART-01** Po restartu služby hraje hudba sama dál jen když hrála, stav je nejvýš 15 min starý, je stejný boot a není noc (22–7 h); studený start Pi ani noc hudbu nespustí.
  Testy: `tests/test_wishes.py::ResumeRule::test_rules`, `tests/test_wishes.py::Queue::test_restart_persistence`
- **F-RESTART-02** Přání a jejich tokeny se po restartu služby (stejný boot, do 2 h) obnoví, i když se nehrálo.
  Testy: `tests/test_review.py::Restart::test_paused_restart_keeps_wishes_and_tokens`
- **F-RESTART-03** Skladba přerušená restartem pokračuje od místa (o 2 s dřív; skladba, které zbývá < 15 s, se nenavazuje).
  Testy: `tests/test_wishes.py::Queue::test_restart_resumes_interrupted_track`, `tests/test_resume_cache.py::PlayerResumeTest::test_snapshot_and_resume_at_position`, `tests/test_resume_cache.py::PlayerResumeTest::test_saved_playback_rejects`
- **F-RESTART-04** Co podle historie nebo uloženého přehrávání mezitím dohrálo, po obnově nezazní znovu; přání se ukládají až po přehrávači.
  Testy: `tests/test_review2.py::ResumeConsistency::test_playback_newer_than_session_marks_the_old_current_done`, `tests/test_review2.py::ResumeConsistency::test_history_marks_tracks_finished_after_the_save`, `tests/test_review2.py::ResumeConsistency::test_shutdown_saves_wishes_after_the_player`
- **F-RESTART-05** Vypršelý režim interpreta v podkresu se po restartu neobnoví; čerstvý jen se zbytkem svého omezení.
  Testy: `tests/test_pi0926.py::BackgroundFollowsLatest::test_restart_does_not_resurrect_stale_artist_mode`
- **F-RESTART-06** Připravené streamy přežijí restart (tmpfs): mladé se podají hned, staré se ověří, s jiným formátem se zahodí, mrtvá adresa se jednou načte znovu.
  Testy: `tests/test_resume_cache.py::DiskCacheTest::test_round_trip`, `tests/test_resume_cache.py::DiskCacheTest::test_expiry_and_age`, `tests/test_resume_cache.py::DiskCacheTest::test_young_disk_entry_served_without_probe`, `tests/test_resume_cache.py::DiskCacheTest::test_other_template_drops_disk`, `tests/test_resume_cache.py::DiskCacheTest::test_dead_cached_url_is_resolved_fresh`, `tests/test_resume_cache.py::PlayerResumeTest::test_dead_disk_url_replays_same_entry`, `tests/test_resume_cache.py::PlayerResumeTest::test_disk_retry_only_once`
- **F-RESTART-07** Start přehrávače: mpv před resolverem, čekání na socket, zastaralý socket se nepoužije, mpv, které hned skončí, se ohlásí hned; když resolver nejede, mpv jede přes yt-dlp.
  Testy: `tests/test_player_start.py::StartTest::test_start_breakdown_priority_and_resolver_first`, `tests/test_player_start.py::StartTest::test_stale_socket_is_reported_and_not_connected_to`, `tests/test_player_start.py::StartTest::test_mpv_that_exits_fails_fast`, `tests/test_resume_cache.py::ShimTest::test_waits_for_socket_that_appears_later`, `tests/test_resume_cache.py::ShimTest::test_startup_error_falls_back_to_real_ytdlp`
- **F-RESTART-08** Po startu služby se přehrávač (mpv a resolver) spustí hned po načtení konfigurace, souběžně s načítáním zbytku aplikace; navázání přerušené skladby nečeká na web, terminál ani katalog YouTube Music (ytmusicapi), ty se načtou až po něm.
  Testy: `tests/test_startup.py::StartOrder::test_player_starts_before_the_rest_of_the_app_and_resumes`, `tests/test_startup.py::StartOrder::test_failed_import_stops_the_started_player`, `tests/test_startup.py::LightStart::test_player_path_does_not_import_the_heavy_parts`, `tests/test_startup.py::LightStart::test_lazy_ytmusic_is_created_once_in_the_calling_thread`
- **F-RESTART-09** Tytéž volby od mpv v jiném pořadí (ytdl_hook je skládá pokaždé jinak) nejsou jiná šablona: hotové skladby z disku ani rozdělaná skladba se kvůli tomu nezahodí; jiný formát ano.
  Testy: `tests/test_resume_cache.py::TemplateOrderTest::test_reordered_options_keep_prepared_tracks`
- **F-RESTART-10** Ukončení služby (SIGTERM, restart) kdykoli — i během startu — řádně zastaví přehrávač a uloží co hraje (playback.json), přání a přezdívky včetně „naposledy viděn".
  Nové pravidlo 26. 9. 2026 (revize: SIGTERM během startu přeskočil celý úklid; „naposledy viděn" z posledních ≤ 10 min se při vypnutí ztrácel).
  Testy: `tests/test_startup.py::StopDuringStart::test_sigterm_while_starting_still_cleans_up_and_saves_nicks`, `tests/test_review2.py::ResumeConsistency::test_shutdown_saves_wishes_after_the_player`

## Chytrý start

- **F-START-01** Hrát bez přání a bez fronty vybere hudbu podle času, dne, českých svátků, kanceláře a toho, co tu v podobnou dobu (±90 min, 28 dní, aspoň 2 různé dny) dohrálo.
  Testy: `tests/test_dj_context.py::Calendar::test_czech_holidays`, `tests/test_dj_context.py::Calendar::test_easter`, `tests/test_dj_context.py::Situations::test_monday_morning`, `tests/test_dj_context.py::Situations::test_friday_afternoon_is_liveliest`, `tests/test_dj_context.py::Situations::test_christmas_eve`, `tests/test_dj_context.py::History::test_slot_stats`, `tests/test_dj_context.py::History::test_one_session_is_not_everyone`, `tests/test_wishes.py::Queue::test_idle_start_uses_context`
- **F-START-02** Bez historie nebo s rozbitou databází chytrý start stejně funguje (návrat k historii / výchozí).
  Testy: `tests/test_dj_context.py::Situations::test_empty_history`, `tests/test_dj_context.py::History::test_broken_store_degrades`, `tests/test_wishes.py::Queue::test_idle_start_falls_back_to_history`
- **F-START-03** Chytrý start je automatický tah, ne něčí přání; přání posluchače ho zruší.
  Testy: `tests/test_review.py::StartYields::test_wish_cancels_the_idle_start`, `tests/test_wishes.py::WebApi::test_play_in_silence_starts_the_dj_with_context`
- **F-START-04** Úmyslnou pauzu (mladší 10 min) přání samo nezruší.
  Testy: `tests/test_review.py::Texts::test_deliberate_pause_is_respected`

## Kancelářská slušnost (filtr slov)

- **F-SLUSNOST-01** Sprostá jména a texty přání se na displeji a webu neukážou (zamaskují se); filtr jde vypnout a doplnit v nastavení.
  Testy: `tests/test_review.py::Display::test_rude_names_and_texts_are_masked`
- **F-SLUSNOST-02** Sprostou přezdívku aplikace vlídně odmítne („zkus jinou"); nevinná slova (Picnic) projdou.
  Testy: `tests/test_nicks.py::Validation::test_office_filter_with_a_kind_message`, `tests/test_votes.py::Nicks::test_names_go_through_office_filter_and_registry`

## Provoz (logování, report, paměť)

- **F-PROVOZ-01** Provozní log (JSON řádky) se píše ve vlastním vlákně, nikdy nespadne, má identifikátor běhu a rotaci 5 MB × 5; jde vypnout.
  Testy: `tests/test_telemetry.py::TelemetryWriteTest::test_session_start_first_and_sid`, `tests/test_telemetry.py::TelemetryWriteTest::test_never_raises`, `tests/test_telemetry.py::TelemetryWriteTest::test_rotation_bounds_disk_use`, `tests/test_telemetry.py::TelemetryWriteTest::test_threads_do_not_interleave_lines`, `tests/test_telemetry.py::TelemetryWriteTest::test_kill_switch`, `tests/test_telemetry.py::TelemetryWriteTest::test_reopens_after_external_delete`
- **F-PROVOZ-02** Loguje se přání, výklad, skladby a jejich latence, resolver, zvuk (xruny), systém (CPU, paměť, teplota) a panel (akce, dotyky, výkon).
  Testy: `tests/test_telemetry.py::PlayerLifecycleTest::test_skip_to_prefetched_and_on_demand`, `tests/test_telemetry.py::ResolverEventTest::test_resolver_emits_parsable_events`, `tests/test_telemetry.py::SamplerParseTest::test_xrun_delta_event`, `tests/test_telemetry.py::SamplerParseTest::test_sample_runs_here`, `tests/test_panel.py::PanelEventsTest::test_actions_gestures_and_summaries`, `tests/test_music_match.py::TelemetryTest::test_search_records_choice_source_and_runner_up`, `tests/test_dj_context.py::Telemetry::test_event`
- **F-PROVOZ-03** `python -m ytdj.telemetry report` dá český přehled pro ladění (i JSON, od zadaného času), včetně oddílu přání.
  Testy: `tests/test_telemetry.py::ReportTest::test_aggregation`, `tests/test_telemetry.py::ReportTest::test_since_filter_and_render`, `tests/test_telemetry.py::ReportTest::test_cli_json_and_text`, `tests/test_telemetry.py::RequestReportTest::test_request_section`
- **F-PROVOZ-04** Hlavní smyčka se nezasekne: zápisy do databáze nečekají na disk, čtení jde mimo smyčku, každé zaseknutí > 0,5 s se zaloguje i s viníkem; stav webu odpovídá i při třech přáních naráz.
  Testy: `tests/test_loop.py::Watchdog::test_blocked_loop_is_reported_with_the_culprit`, `tests/test_loop.py::Watchdog::test_stuck_loop_is_reported_while_stuck`, `tests/test_loop.py::Watchdog::test_quiet_loop_reports_nothing`, `tests/test_loop.py::StoreWrites::test_writes_do_not_wait_for_the_disk`, `tests/test_loop.py::StoreWrites::test_reads_can_run_off_the_loop`, `tests/test_loop.py::ThreePeopleOnASlowPi::test_status_stays_responsive`
- **F-PROVOZ-05** Codex běží jako trvalý proces bez pluginů a nástrojů, vlákno se mění po 5 tazích; zahřátý zůstává v pracovní době (Po–Pá 7–19) a kdykoli 2 h po posledním přání posluchače (i po restartu podle uloženého přání), dokud má Pi ≥ 250 MB volné paměti (kontrola každou minutu), jinak se ukončí (jinak po 10 min nečinnosti).
  Změněno 26. 9. 2026 se souhlasem vlastníka: „Codex zahřátý vždy po přání" (2 h, i večer a o víkendu — 26. 9. platilo studený start 4,8–12,4 s 9 z 18 tahů posluchačů) a „5 přání na jedno vlákno" (dřív 3; tah v rozjetém vlákně je rychlejší — A/B na Pi: vstup 11,7k tokenů v 1. tahu → 25,1k v 5. tahu, z toho 21,2k z mezipaměti, model medián 7,3 s → 4,3 s, okno modelu 272k).
  Testy: `tests/test_dj_appserver.py::Fake::test_warm_turns_reuse_process_and_rotate_threads`, `tests/test_dj_appserver.py::Fake::test_prewarm_hides_startup_and_disables_plugins`, `tests/test_dj_appserver.py::Fake::test_idle_process_is_closed`, `tests/test_dj_appserver.py::KeepWarm::test_office_hours_policy`, `tests/test_dj_appserver.py::KeepWarm::test_two_hours_after_a_wish_at_any_time`, `tests/test_dj_appserver.py::KeepWarm::test_dj_keeps_it_warm_after_a_listener_wish_and_rotates_after_five`, `tests/test_dj_appserver.py::KeepWarm::test_kept_warm_while_policy_says_so`, `tests/test_review2.py::AppServerMemory::test_low_memory_closes_it_long_before_idle_ttl`, `tests/test_review2.py::AppServerMemory::test_docstring_says_what_is_measured`, `tests/test_dj_appserver.py::NativeBinary::test_finds_vendor_binary_next_to_npm_wrapper`
- **F-PROVOZ-06** Historie v playlistu mpv se prořezává (nejvýš 5 dohraných před hrající).
  Testy: `tests/test_player_queue.py::TrueOrder::test_history_is_pruned`
- **F-PROVOZ-07** Každý start služby zapíše do provozního logu událost `app.start` s časy fází od spuštění procesu (načtení, konfigurace, start přehrávače, aplikace, navázání, web) a časem spuštění procesu, aby šlo změřit ticho po restartu.
  Testy: `tests/test_startup.py::StartOrder::test_player_starts_before_the_rest_of_the_app_and_resumes`, `tests/test_startup.py::StartEvent::test_clock_measures_from_process_start`
- **F-PROVOZ-08** Codex se nahřívá dřív, než přání přijde: když někdo na webu začne psát přání nebo na displeji otevře obrazovku přání (kdykoli, nejvýš jednou za minutu) a po startu služby, když ho F-PROVOZ-05 drží zahřátý — nový proces jen s ≥ 330 MB volné paměti (běžící se zavírá pod 250 MB, F-PROVOZ-05 — hystereze, ať se nezavírá a nestartuje dokola), pokus nejvýš jednou za minutu pro celý jukebox a jen když mozek jede; odmítnutí kvůli paměti se zaloguje; nahřátí nic nepřehrává ani nezařazuje. Pravidla ukončení z F-PROVOZ-05 platí dál.
  Nové pravidlo 26. 9. 2026 (rychlejší volná přání: studený start Codexu platilo 9 z 18 tahů posluchačů, 4,8–12,4 s); displej a start podle F-PROVOZ-05 doplněny týž den se souhlasem vlastníka.
  Upřesněno 26. 9. 2026 po revizi (paměť Pi s resolverem, JS nodem a mpv ~470 MB): start dopředu až nad 330 MB (dřív 250) — start Codexu ubral MemAvailable medián 44 MB, 13 ze 14 startů ≤ 78 MB; jeden pokus za minutu pro celý jukebox. Týká se jen nahřívání dopředu, přání samo Codex spustí jako dřív. Změněno 26. 9. 2026 se souhlasem vlastníka: v rámci jeho zadání „vylepši funkčnost a spolehlivost, naimplementuj v nejlepší podobě" — přísnější jen nahřívání dopředu, aby Pi nešlo do nedostatku paměti.
  Testy: `tests/test_dj_appserver.py::WarmAhead::test_typing_a_wish_warms_the_brain_any_time`, `tests/test_dj_appserver.py::WarmAhead::test_memory_hysteresis_and_one_attempt_a_minute`, `tests/test_dj_appserver.py::WarmAhead::test_after_start_only_when_kept_warm`, `tests/test_panel.py::WishScreenWarmsTheDJ::test_opening_the_wish_screen_warms_once_a_minute`, `tests/test_dj_appserver.py::WarmAhead::test_not_with_little_memory_or_when_the_brain_is_down`, `tests/test_dj_appserver.py::WarmAhead::test_web_endpoint_only_warms`, `tests/test_dj_appserver.py::SpareThread::test_spare_thread_never_starts_a_process_and_close_stops_a_pending_start`
- **F-PROVOZ-09** Když tah vyčerpá vlákno Codexu (F-PROVOZ-05), další vlákno se založí hned na pozadí, ne až s dalším přáním; nikdy přitom nespustí nový proces.
  Nové pravidlo 26. 9. 2026 (nové vlákno čekalo na začátek odpovědi modelu medián 3,6 s, rozjeté 1,35 s).
  Testy: `tests/test_dj_appserver.py::SpareThread::test_next_thread_is_made_right_after_the_last_turn_of_a_thread`, `tests/test_dj_appserver.py::SpareThread::test_spare_thread_never_starts_a_process_and_close_stops_a_pending_start`
- **F-PROVOZ-10** DJ (Codex) uvažuje s nízkým úsilím („low"), přes app-server i `codex exec`; `YTDJ_CODEX_EFFORT=medium` vrátí dřívější.
  Nové pravidlo 26. 9. 2026 se souhlasem vlastníka („Zkusit nižší úsilí modelu", přijmout jen když to nezhorší porozumění): A/B na Pi, 16 skutečných přání × 2 běhy — low i medium vyložily přání stejně (30/32; zbylé 2 u obou stejně), model medián 7,2 → 5,8 s, p90 10,0 → 7,2 s.
  Testy: `tests/test_dj_appserver.py::SpareThread::test_dj_thinks_with_low_effort_unless_told_otherwise`, `tests/test_dj_appserver.py::SpareThread::test_reasoning_effort_goes_with_each_turn_only_when_set`, `tests/test_dj_appserver.py::KeepWarm::test_dj_keeps_it_warm_after_a_listener_wish_and_rotates_after_five`
- **F-PROVOZ-11** Provozní log ukazuje paměť Codexu (`rss_codex_mb`, `anon_codex_mb` a CPU v `sys.sample`) a každé jeho zavření s důvodem (paměť / nečinnost), volnou pamětí a RSS.
  Nové pravidlo 26. 9. 2026 (revize: rozhodovat o paměti podle měření, ne odhadu).
  Testy: `tests/test_dj_appserver.py::WarmAhead::test_codex_memory_is_in_the_system_sample_and_closes_are_logged`

## Bezpečnost (současný stav — k rozhodnutí)

Pravidla, která dnes platí:

- **F-BEZP-01** Heslo Wi-Fi se nikam neloguje a není v chybových hláškách.
  Testy: `tests/test_panel_net.py::NetFlowTest::test_error_text_never_carries_the_password`, `tests/test_panel_net.py::NmcliBackendTest::test_connect_fails_without_leaking`, `tests/test_panel_net.py::NetFlowTest::test_key_press_pushes_only_the_key`
- **F-BEZP-02** Tokeny a klíče z chyb Codexu se posluchači nikdy neukážou.
  Testy: `tests/test_review.py::Texts::test_codex_errors_are_never_shown_raw`, `tests/test_dj_offline.py::OfflineDJ::test_raw_error_never_reaches_the_listener`
- **F-BEZP-03** Přání odebere nebo posune jen ten, kdo má jeho token.
  Testy: `tests/test_wishes.py::WebApi::test_remove_needs_the_owner_token`
- **F-BEZP-04** Veřejná značka člověka (tag) neprozradí jeho id klienta.
  Testy: `tests/test_nicks.py::Book::test_tag_does_not_reveal_the_client`
- **F-BEZP-05** Codex nemá nástroje: každý požadavek serveru na schválení (shell, soubory) se odmítne.
  Testy: `tests/test_dj_appserver.py::Fake::test_server_requests_are_refused`

Otevřené body (nejsou pravidla, rozhodne vlastník na konci — viz POZADAVKY #41):
- web je bez hesla: kdokoli v síti ovládá hudbu, **nastavení i restart jsou otevřené**;
- heslo Wi-Fi z displeje je krátce vidět v seznamu procesů (`nmcli` argument);
- panel běží jako root (kvůli `/dev/mem`);
- YouTube na Pi zatím s cookies z notebooku; na Pi chybí bubblewrap (sandbox Codexu).

## Bez testu (hardware)

- F-ZVUK-14 — skutečný zvuk a lupání (USB soundbar, PipeWire): měří se na Pi telemetrií.
- F-DISPLEJ-13 — protokol displeje KeDei a dotykového čipu XPT2046.

## Nesoulady nalezené při sepsání (26. 9.)

Opraveno v dokumentaci (kód se neměnil):
- README tvrdilo, že nové přání klienta nahradí jeho starší — platí F-FRONTA-07.
- README tvrdilo, že se po restartu obnoví přání jen když se hrálo — platí F-RESTART-01/02.
- README uvádělo 16–21 s na každé přání přes Codex — dnes rychlá cesta ~3 s, Codex 5,6–9,8 s zahřátý.
- PLAN.md měl D1, D3–D6, C7, H1, H6 jako nehotové — jsou hotové a mají testy.

## Změny se souhlasem vlastníka

| Datum | Pravidlo | Co se změnilo a proč |
|---|---|---|
| 26. 9. 2026 | F-DISPLEJ-01 | Mezera mezi tlačítky (do 10 px) stiskne nejbližší tlačítko — ráno se trefovalo jen ~40 % dotyků. |
| 26. 9. 2026 | F-HLAS-04, F-DISPLEJ-12 | Stisk ruší jen zřetelné sjetí prstu (2× dál než 44 px), šum převodníku ho nepřeruší. |
| 26. 9. 2026 | F-DISPLEJ-14 (nové) | Kalibrace dotyku z displeje, 5 křížků. |
| 26. 9. 2026 | F-DISPLEJ-12 | Tlačítko podle toho, kde prst ležel (ne kam dosedl); horní tlačítka a „Zpět" až k hornímu okraji. |
| 26. 9. 2026 | F-DISPLEJ-14 | Kalibrace 9 body (mřížka 3×3) místo 5 — opraví i nerovnoměrnost skla. |
| 26. 9. 2026 | F-DISPLEJ-15 (nové) | Test dotyku z displeje. |
| 26. 9. 2026 | F-DISPLEJ-16 (nové) | Test prstem: 12 míst, všechny vzorky do logu (hledá se chyba prstu u horního okraje). |
| 27. 9. 2026 | F-DISPLEJ-01 | Vyšší horní lišta (46 px): tlačítka Mobil, Přání, Síť o 14 px níž, dál od rámečku. |
| 27. 9. 2026 | F-DISPLEJ-14 | Kalibrace 12 body (mřížka 3×4 s řádky u horního a dolního okraje) — pokryje, že prst u okraje čte k středu. |
| 27. 9. 2026 | F-DISPLEJ-14 | Odolná kalibrace: krajní křížky dvakrát, nespolehlivé vynechat, oprava u okraje nejvýš 15 px od vnitřku (i u uložené), afinní část z vnitřních bodů. |
| 27. 9. 2026 | F-DISPLEJ-12 | „Zpět" dole jako velké tlačítko na všech vedlejších obrazovkách, nahoře jen nadpisy. |
| 26. 9. 2026 | F-HLAS-03, F-DISPLEJ-12 | Hlasitost z displeje nikdy skokem: lišta jen tažením, ťuknutí na ni = krok 5, stisk jiného tlačítka se do lišty nepřesune (na Pi ťuknutí na „+"/„Další" dalo 100). |
| 26. 9. 2026 | F-PROVOZ-10 (nové) | DJ uvažuje s úsilím „low" — A/B na Pi: stejné porozumění, model o ~1,4 s rychlejší (medián). |
| 26. 9. 2026 | F-PROVOZ-05 | Codex zahřátý i 2 h po každém přání posluchače (kdykoli, s paměťovou pojistkou) a vlákno po 5 tazích místo 3 — volba vlastníka („Codex zahřátý vždy po přání, 5 přání na jedno vlákno"). |
| 26. 9. 2026 | F-ZVUK-05 | Postupná příprava: nejdřív 3 nejbližší, pak po jedné až 10 (dřív pevně 6), nikdy na úkor naléhavé skladby — návrh vlastníka („vždy udělat třeba tři hned a pak jich postupně přidávat"). |
| 26. 9. 2026 | F-PROVOZ-08 | Nahřívání Codexu dopředu až nad 330 MB volné paměti (dřív 250), jeden pokus za minutu — spolehlivost paměti Pi (zadání „v nejlepší podobě"). |
| 27. 9. 2026 | F-HLASY-09, F-HLASY-13 | 👍 z importu playlistu se nepočítají do 30 hlasů / 10 min; pojistka importu 1000 písniček na člověka (dřív navrženo 300). |
| 27. 9. 2026 | F-HLASY-17 | „Pusť oblíbené" a oblíbené pro DJe se střídají po lidech místo řazení jen podle počtu 👍. |
