"""SQLite state: play history, ratings, seeds, long-term taste."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from .config import STATE_DB, TASTE_FILE

SCHEMA = """
CREATE TABLE IF NOT EXISTS plays (
    video_id TEXT NOT NULL,
    title    TEXT,
    artist   TEXT,
    ts       REAL NOT NULL,
    seed_id  TEXT,
    outcome  TEXT NOT NULL DEFAULT 'started'   -- started|finished|skipped|error
);
CREATE INDEX IF NOT EXISTS plays_video_ts ON plays(video_id, ts);

CREATE TABLE IF NOT EXISTS feedback (
    video_id TEXT NOT NULL,
    rating   TEXT NOT NULL,                    -- like|dislike
    ts       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS seeds (
    video_id TEXT NOT NULL,
    mood     TEXT,
    ts       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS blacklist (
    video_id TEXT PRIMARY KEY,
    reason   TEXT,
    ts       REAL NOT NULL
);
"""


@dataclass(slots=True)
class PlayRecord:
    video_id: str
    title: str
    artist: str
    outcome: str


class Store:
    def __init__(self, path=STATE_DB) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.executescript(SCHEMA)

    # ---- writes ----

    def record_start(self, video_id: str, title: str, artist: str, seed_id: str | None) -> None:
        self.db.execute(
            "INSERT INTO plays(video_id,title,artist,ts,seed_id,outcome) VALUES(?,?,?,?,?,'started')",
            (video_id, title, artist, time.time(), seed_id),
        )

    def record_outcome(self, video_id: str, outcome: str) -> None:
        """Fills in the outcome for the most recently started play of this track."""
        self.db.execute(
            """UPDATE plays SET outcome=?
               WHERE rowid = (SELECT rowid FROM plays WHERE video_id=? ORDER BY ts DESC LIMIT 1)""",
            (outcome, video_id),
        )

    def record_feedback(self, video_id: str, rating: str) -> None:
        self.db.execute(
            "INSERT INTO feedback(video_id,rating,ts) VALUES(?,?,?)",
            (video_id, rating, time.time()),
        )

    def record_seed(self, video_id: str, mood: str) -> None:
        self.db.execute(
            "INSERT INTO seeds(video_id,mood,ts) VALUES(?,?,?)",
            (video_id, mood, time.time()),
        )

    def blacklist(self, video_id: str, reason: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO blacklist(video_id,reason,ts) VALUES(?,?,?)",
            (video_id, reason, time.time()),
        )

    # ---- reads ----

    def recently_played(self, days: int) -> set[str]:
        cutoff = time.time() - days * 86400
        rows = self.db.execute(
            "SELECT DISTINCT video_id FROM plays WHERE ts > ?", (cutoff,)
        ).fetchall()
        return {r[0] for r in rows}

    def blacklisted(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT video_id FROM blacklist").fetchall()}

    def recent_history(self, limit: int = 40) -> list[PlayRecord]:
        rows = self.db.execute(
            "SELECT video_id,title,artist,outcome FROM plays ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [PlayRecord(*r) for r in rows]

    def skip_burst(self, minutes: int = 10) -> int:
        """How many skips in the last N minutes — a signal the vibe is off."""
        cutoff = time.time() - minutes * 60
        row = self.db.execute(
            "SELECT COUNT(*) FROM plays WHERE ts > ? AND outcome='skipped'", (cutoff,)
        ).fetchone()
        return row[0]

    # ---- long-term taste (plain text, the LLM may append to it) ----

    def taste(self) -> str:
        if TASTE_FILE.exists():
            return TASTE_FILE.read_text()[:4000]
        return ""

    def remember(self, note: str) -> None:
        TASTE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with TASTE_FILE.open("a") as f:
            f.write(f"- {note.strip()}\n")
        # cap at ~4 kB so it doesn't grow forever
        text = TASTE_FILE.read_text()
        if len(text) > 4000:
            TASTE_FILE.write_text(text[-4000:])

    def close(self) -> None:
        self.db.close()
