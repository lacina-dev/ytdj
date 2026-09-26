#!/bin/bash
# temporary test runner (player agent) — not part of the change
cd /home/lacina/Projects/ytdj/.claude/worktrees/rpi-panel
YTDJ_EVENTS_FILE=/tmp/ytdj-player-agent-events.jsonl
export YTDJ_EVENTS_FILE
for f in tests/test_*.py; do
  n=$(basename $f .py)
  case $n in test_panel*) py=python3;; *) py=/home/lacina/Projects/ytdj/.venv/bin/python;; esac
  out=$(timeout 300 $py -m unittest tests.$n 2>&1 | tail -3 | tr '\n' ' ')
  echo "$n: $out"
done
