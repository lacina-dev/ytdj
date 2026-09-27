# Handoff: review2 queue fixes (worktree rpi-panel, not committed)

## Done: code, regression test in tests/test_review2.py, repro checked
1. Double-tap Další: `skip_current` ignores the same person + same vid within SKIP_REPEAT (1.5 s) and logs `request.skip_repeat`. `_skipped_by_other` counts each (wish, vid) once (`Wish.skip_counted`). index.html has a 1 s debounce on btnNext.
   Tests: Skips.test_double_tap_is_one_skip, test_same_track_counts_once_even_from_two_people, test_later_skip_of_the_next_track_still_counts, test_web_next_button_is_debounced.
   r01 before: `skipped | skips_others: 2`. After: `playing | skips_others: 1`, one skip.
2. Meta vs wish:
   - intent.py: `_MUSIC_VERB` now includes chci/dej/"proč ne X"; new `complaint_in`, `meta_wants_music`.
   - fastpath.py: new `meta_candidates`.
   - codex.py: new `CodexDJ.title_plan` (uses other_version).
   - wishes.py `_process`: `_meta_music` tries the fast path, then the title; `_queued_answer` gives the ETA if the track is already queued or playing; `META_LEAD` = "Beru to jako přání." goes in front of the confirmation for complaints.
   Tests: MetaOrWish.*.
   r03 (classifier only) went from 14 swallowed to 7. The remaining 7 (e.g. "proč nehraješ Kabát?") still get `meta_kind` = question by design; the pipeline queues them when the catalog confirms the name (test_question_with_a_known_name_is_queued). r03 itself can't show that because it only calls `meta_kind`.
3. Resume consistency:
   - __main__.py: `wishes.save()` now runs after `player.stop()`.
   - wishes.py: `to_json` saves "current"; `resume()` reconciles through `_ended_since` (state.db `plays_in`, plus playback.json being newer than the session) and `_reconcile`.
   Tests: ResumeConsistency.*.
   r02 before: X replayed = True. After: False; plays kab1, kab2, kab3, kab4.
4. `_confirm` builds from w.tracks minus done and never prints an empty list. Tests: Confirm.*.
   r08 before: "Zařadil jsem: . Hraje hned." After: the track is named in all 3 runs.
5. `_after_finish` fires on every ending. `_background_after` follows the latest fulfilled wish once no queued/playing wish has music left, and only if the background hasn't changed since (`_bg_at`). Tests: BackgroundFollow.*.
   r10 before: artist mode still 'Parni Valjak'. After: mode '', mood "Olympic a podobné".
6. `resume()` maps old `panel-*` turn keys to PANEL_SEAT, taking the max. Test: OldSessionFormat.
   r05 before: display played first. After: Robert first, `turns.last` = {'panel': 9, …}.
7. seat instead of key in `_answer_meta`, the starved logic, `_has_play_next` and `last_heard`. Tests: PanelSeat.*.
   r09 after: "Tvoje „Holky z naší školky“: na řadě hned po téhle".
8. Boost signature is now (pools.generation, votes version, per-pool (id, last id)); radio.py increments `generation` on every fill. Tests: BoostOncePerRefill.*.
   r04 before: boost ran 200×, 0.72 ms per track. After: 1×, 0.05 ms.
9. (a) A DJ-model skip is noted as "DJ" and doesn't count. (b) No foreign-skip count during an outage (`player._outage`). (c) appserver.py: memory is checked every MEM_CHECK = 60 s outside turns (`_turning` guard) and the app-server closes below 250 MB; the docstring now matches what's measured. Tests: Skips.test_dj_decided_skip_does_not_count, test_skip_during_outage_does_not_count, AppServerMemory.*.
10. `favourite_artist_votes` = 2:
    - added in config.py, server.py LIVE_KEYS and FIELD_META, and votes.py (`_status`, `rules()`, `fav_artist_threshold`);
    - votes_api message for a 👍 below the threshold;
    - index.html rule text and artist 👍 hint;
    - PLAN.md H3, "Pravidla fronty" 4, new 6–8, and F4.
    Existing tests in test_votes and test_web_votes were updated to the new policy. Test: FavouriteArtistNeedsTwo.*.
    r11 after: one 👍 gives 0× Kabát.

## In progress / not done
- The full suite has NOT been re-run after the last changes. Still to run: every venv suite except test_panel*, plus test_panel* under system python3, all with YTDJ_EVENTS_FILE set.
- Last results: test_review2 28 OK. test_votes, test_web_votes, test_dj_appserver, test_dj_fastpath, test_dj_intent and test_radio_artist: 114 OK. test_wishes and test_pi0926: 65 OK, but that run was before the meta, resume and background changes.
- tests/fake_ytdj.py still returns the old `rules` without `favourite_artist_votes`. The page falls back to 2, so this is optional.
- The before/after repro outputs are in /tmp/ytdj_r2/before.txt. The "after" outputs are the ones above; run `bash /tmp/ytdj_r2/runrepros.sh <names>` to reproduce them.
