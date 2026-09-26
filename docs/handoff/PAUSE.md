---
name: ytdj-pause-2026-09-26
description: "Where ytdj work was paused on 2026-09-26 ~12:30 and exactly how to resume (WIP branch, handoffs, pending deploy/measurements)"
metadata:
  node_type: memory
  type: project
  originSessionId: 68b134dc-2d5a-4fb5-9ae9-8d2d77ec7aa6
  modified: 2026-09-26T10:00:06.369Z
---

The owner paused all ytdj work on 2026-09-26 around 12:30 ("za 5 minut končíme, budem pokračovat později") and switched the Pi off.

**State at pause:**
- Worktree `/home/lacina/Projects/ytdj/.claude/worktrees/rpi-panel`, branch `worktree-rpi-panel`. The last clean commit is e007256 (on GitHub); the Pi runs 448fb41.
- Uncommitted work of 4 agents is backed up on remote branch `wip/2026-09-26-pause`. It stays uncommitted in the worktree (soft reset).
- Agent handoffs are in `/home/lacina/.claude/jobs/68b134dc/tmp/handoff-{queue,player,touch,funkce}.md`. Copies are in the WIP commit under `docs/handoff/`.

**The 4 paused tasks:**
1. Queue and DJ fixes from review 2. Repros are in `tmp/review2/`:
   - double-tap Další;
   - meta swallowing wishes;
   - double play after restart;
   - empty "Zařadil jsem: .";
   - stale background;
   - old `session.json` panel key;
   - panel meta;
   - boost cost;
   - favourite artist needs 2 👍.
2. Player: shim fallback nice −6, `preexec_fn` safety, concurrent resolver start, skipping verify for fresh cache entries, why `player.start` takes 4.7 s. Goal: restart silence ≤ 5 s (now ~12 s).
3. Display touch: only ~40 % of touches register (347 downs → 141 actions, 155 aborted, many `slid_out`). Fixes: target at press-down, release hysteresis, looser filter, calibration flow.
4. `docs/FUNKCE.md` feature contract plus `tests/test_funkce.py` (owner: existing behaviour must stay).

**Resume steps:**
- Read the handoffs.
- Restore the WIP if the worktree was lost: `git checkout wip/2026-09-26-pause -- .`
- Finish each task, run all suites (`YTDJ_EVENTS_FILE` set; panel with system `python3`), then commit, deploy and measure.
- **Pi:** after the connector fix the Pi was on the laptop's shared ethernet at 10.42.0.149, and Wi-Fi was not configured. The office LAN address 192.168.0.24 only works on the router cable or Wi-Fi.
- **Owner-facing list:** `docs/POZADAVKY.md`.

**Pending measurements on the Pi:**
- xrun rate after the mpv nice −11 change (deployed 11:29); baseline is 466 xruns in 35 moments before 11:29 that day;
- restart silence;
- touch success rate;
- the model-path wish latency.

Related: [[ytdj-requirements-checklist]], [[ytdj-pi-security-todo]], [[visible-results-first]].
