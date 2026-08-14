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

-- What the listener asked for by name. Separate from `plays`: a track that
-- merely came up on the radio says nothing, one that someone asked for by
-- name says a lot — and asking twice says twice as much.
CREATE TABLE IF NOT EXISTS requests (
    video_id TEXT NOT NULL,
    title    TEXT,
    artist   TEXT,
    ts       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS requests_video ON requests(video_id);
"""


@dataclass(slots=True)
class PlayRecord:
    video_id: str
    title: str
    artist: str
    outcome: str


@dataclass(slots=True)
class RequestCount:
    video_id: str
    title: str
    artist: str
    count: int

    def label(self) -> str:
        name = f"{self.artist} — {self.title}" if self.artist else self.title
        return f"{self.count}× {name}"


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

    def record_request(self, video_id: str, title: str, artist: str) -> None:
        """The listener asked for this one by name."""
        self.db.execute(
            "INSERT INTO requests(video_id,title,artist,ts) VALUES(?,?,?,?)",
            (video_id, title, artist, time.time()),
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

    def top_requested(self, limit: int = 10, days: int = 0) -> list[RequestCount]:
        """Most-asked-for tracks, the most requested first.

        `days` = 0 means all of it. The title is taken from the newest request,
        so a track that was once saved under a mangled name eventually corrects
        itself.
        """
        where, params = "", []
        if days:
            where = "WHERE ts > ?"
            params = [time.time() - days * 86400]
        rows = self.db.execute(
            f"""SELECT video_id,
                       (SELECT title  FROM requests r2 WHERE r2.video_id = r.video_id
                         ORDER BY ts DESC LIMIT 1),
                       (SELECT artist FROM requests r2 WHERE r2.video_id = r.video_id
                         ORDER BY ts DESC LIMIT 1),
                       COUNT(*) AS n
                  FROM requests r {where}
                 GROUP BY video_id
                 ORDER BY n DESC, MAX(ts) DESC
                 LIMIT ?""",
            (*params, limit),
        ).fetchall()
        return [RequestCount(*r) for r in rows]

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
