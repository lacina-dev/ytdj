"""SQLite state: play history, ratings, seeds, long-term taste."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .config import STATE_DB, TASTE_FILE

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS plays (
    video_id TEXT NOT NULL,
    title    TEXT,
    artist   TEXT,
    ts       REAL NOT NULL,
    seed_id  TEXT,
    outcome  TEXT NOT NULL DEFAULT 'started'   -- started|finished|skipped|replaced|error
);
CREATE INDEX IF NOT EXISTS plays_video_ts ON plays(video_id, ts);
-- time-range statistics (context for starting without a request)
CREATE INDEX IF NOT EXISTS plays_ts ON plays(ts);

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
class TimedPlay:
    """One row of `plays` with its time — for statistics by time of day."""

    ts: float
    video_id: str
    title: str
    artist: str
    outcome: str
    mood: str  # plays.seed_id: the app stores the pools' mood there


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
    """SQLite stav. `background=True` (aplikace): zápisy jdou do vlastního vlákna.

    25. 9. na Pi: INSERT při startu skladby (handler události přehrávače,
    tedy v event loopu) čekal 10,7 s na SD kartu při iowait 40 % (startoval
    Codex a resolver) — a celou tu dobu stál web, panel i fronta přání.
    Zápisy proto jdou přes frontu do vlákna s vlastním spojením (WAL: čtení
    zápis neblokuje) a v event loopu se nikdy nečeká na disk. Čtení na
    horkých cestách volají `aread()` (vlákno); zbytek čtení je vzácný.
    """

    def __init__(self, path=STATE_DB, background: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        # Zápisy běží v event loopu (handler události přehrávače). V režimu
        # DELETE + FULL dělá každý zápis několik fsync na SD kartu — na Pi 3
        # 0,1 s běžně a pod zátěží až 3 s (player.slow_handler 25. 9.), po
        # kterou stál i web a panel: zvuk hrál za 0,6 s, displej ukázal
        # přepnutí až za 3,3 s. WAL + NORMAL = žádný fsync při zápisu (jen při
        # checkpointu); při výpadku proudu se může ztratit posledních pár
        # záznamů historie, databáze zůstane celá.
        with contextlib.suppress(sqlite3.Error):
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self._writes: queue.Queue | None = None
        self._writer: threading.Thread | None = None
        self._pool = None  # ThreadPoolExecutor pro aread(), až bude potřeba
        if background:
            self._writes = queue.Queue()
            self._writer = threading.Thread(target=self._write_loop, name="ytdj-store", daemon=True)
            self._writer.start()

    # ---- plumbing ----

    def _write_loop(self) -> None:
        db = sqlite3.connect(self.path, isolation_level=None, timeout=60)
        with contextlib.suppress(sqlite3.Error):
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
        assert self._writes is not None
        while True:
            item = self._writes.get()
            try:
                if item is None:
                    return
                if callable(item):
                    item()
                else:
                    sql, params = item
                    db.execute(sql, params)
            except Exception:
                log.exception("zápis do state.db selhal")
            finally:
                self._writes.task_done()

    def _write(self, sql: str, params: tuple) -> None:
        if self._writes is not None:
            self._writes.put((sql, params))
            return
        with self._lock:
            self.db.execute(sql, params)

    def _later(self, fn: Callable[[], Any]) -> None:
        """Souborová práce (taste.md) mimo event loop, když běží vlákno zápisů."""
        if self._writes is not None:
            self._writes.put(fn)
        else:
            fn()

    def _read(self, sql: str, params: tuple = ()) -> list:
        with self._lock:
            return self.db.execute(sql, params).fetchall()

    def flush(self, timeout: float = 5.0) -> None:
        """Počkat, až jsou zápisy na disku (testy, ukončení)."""
        if self._writes is None:
            return
        done = threading.Event()
        self._writes.put(done.set)
        done.wait(timeout)

    async def aread(self, name: str, *args: Any) -> Any:
        """Čtení z event loopu bez čekání na disk: metoda `name` ve vlákně."""
        # vlastní vlákno: sdílený pool (katalog, rádia) bývá plný a čtení
        # historie pro web by na něj čekalo
        if self._pool is None:
            from concurrent.futures import ThreadPoolExecutor

            self._pool = ThreadPoolExecutor(1, thread_name_prefix="ytdj-store-read")
        fn = getattr(self, name)
        return await asyncio.get_running_loop().run_in_executor(self._pool, lambda: fn(*args))

    # ---- writes ----

    def record_start(self, video_id: str, title: str, artist: str, seed_id: str | None) -> None:
        self._write(
            "INSERT INTO plays(video_id,title,artist,ts,seed_id,outcome) VALUES(?,?,?,?,?,'started')",
            (video_id, title, artist, time.time(), seed_id),
        )

    def record_outcome(self, video_id: str, outcome: str) -> None:
        """Fills in the outcome for the most recently started play of this track."""
        self._write(
            """UPDATE plays SET outcome=?
               WHERE rowid = (SELECT rowid FROM plays WHERE video_id=? ORDER BY ts DESC LIMIT 1)""",
            (outcome, video_id),
        )

    def record_feedback(self, video_id: str, rating: str) -> None:
        self._write(
            "INSERT INTO feedback(video_id,rating,ts) VALUES(?,?,?)",
            (video_id, rating, time.time()),
        )

    def record_seed(self, video_id: str, mood: str) -> None:
        self._write(
            "INSERT INTO seeds(video_id,mood,ts) VALUES(?,?,?)",
            (video_id, mood, time.time()),
        )

    def record_request(self, video_id: str, title: str, artist: str) -> None:
        """The listener asked for this one by name."""
        self._write(
            "INSERT INTO requests(video_id,title,artist,ts) VALUES(?,?,?,?)",
            (video_id, title, artist, time.time()),
        )

    def blacklist(self, video_id: str, reason: str) -> None:
        self._write(
            "INSERT OR REPLACE INTO blacklist(video_id,reason,ts) VALUES(?,?,?)",
            (video_id, reason, time.time()),
        )

    # ---- reads ----

    def recently_played(self, days: int) -> set[str]:
        cutoff = time.time() - days * 86400
        rows = self._read("SELECT DISTINCT video_id FROM plays WHERE ts > ?", (cutoff,))
        return {r[0] for r in rows}

    def blacklisted(self) -> set[str]:
        return {r[0] for r in self._read("SELECT video_id FROM blacklist")}

    def recent_history(self, limit: int = 40) -> list[PlayRecord]:
        rows = self._read(
            "SELECT video_id,title,artist,outcome FROM plays ORDER BY ts DESC LIMIT ?", (limit,)
        )
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
        rows = self._read(
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
        )
        return [RequestCount(*r) for r in rows]

    def plays_in(
        self, ranges: list[tuple[float, float]], outcomes: tuple[str, ...] = ("finished", "skipped")
    ) -> list[TimedPlay]:
        """Plays inside any of the [start, end) time ranges, oldest first. Read-only.

        Meant for "the same time of day on previous days" — a few dozen ranges.
        """
        if not ranges or not outcomes:
            return []
        where = " OR ".join("(ts >= ? AND ts < ?)" for _ in ranges)
        marks = ",".join("?" for _ in outcomes)
        rows = self._read(
            f"""SELECT ts, video_id, COALESCE(title,''), COALESCE(artist,''),
                       outcome, COALESCE(seed_id,'')
                  FROM plays WHERE outcome IN ({marks}) AND ({where}) ORDER BY ts""",
            (*outcomes, *(t for r in ranges for t in r)),
        )
        return [TimedPlay(*r) for r in rows]

    def requests_in(self, ranges: list[tuple[float, float]]) -> list[tuple[float, str, str]]:
        """(ts, artist, title) of tracks asked for by name inside the ranges."""
        if not ranges:
            return []
        where = " OR ".join("(ts >= ? AND ts < ?)" for _ in ranges)
        return self._read(
            f"""SELECT ts, COALESCE(artist,''), COALESCE(title,'')
                  FROM requests WHERE {where} ORDER BY ts""",
            tuple(t for r in ranges for t in r),
        )

    def artist_counts(self, since: float, until: float | None = None) -> list[tuple[str, int]]:
        """(artist, plays) in [since, until), errors not counted. Read-only."""
        until = time.time() if until is None else until
        return self._read(
            """SELECT COALESCE(artist,''), COUNT(*) FROM plays
                WHERE ts >= ? AND ts < ? AND outcome != 'error'
                GROUP BY artist""",
            (since, until),
        )

    def ratings(self) -> dict[str, str]:
        """Latest like/dislike per track (the feedback table is small)."""
        rows = self._read("SELECT video_id, rating FROM feedback ORDER BY ts")
        return {vid: rating for vid, rating in rows}

    def last_mood(self, before: float | None = None) -> tuple[str, float] | None:
        """The mood of the newest seeding before `before`, with its timestamp."""
        before = time.time() if before is None else before
        rows = self._read(
            """SELECT mood, ts FROM seeds
                WHERE ts < ? AND mood IS NOT NULL AND mood != ''
                ORDER BY ts DESC LIMIT 1""",
            (before,),
        )
        return (rows[0][0], rows[0][1]) if rows else None

    def skip_burst(self, minutes: int = 10) -> int:
        """How many skips in the last N minutes — a signal the vibe is off."""
        cutoff = time.time() - minutes * 60
        return self._read(
            "SELECT COUNT(*) FROM plays WHERE ts > ? AND outcome='skipped'", (cutoff,)
        )[0][0]

    # ---- long-term taste (plain text, the LLM may append to it) ----

    def taste(self) -> str:
        if TASTE_FILE.exists():
            return TASTE_FILE.read_text()[:4000]
        return ""

    def remember(self, note: str) -> None:
        self._later(lambda: self._remember(note))

    def _remember(self, note: str) -> None:
        TASTE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with TASTE_FILE.open("a") as f:
            f.write(f"- {note.strip()}\n")
        # cap at ~4 kB so it doesn't grow forever
        text = TASTE_FILE.read_text()
        if len(text) > 4000:
            TASTE_FILE.write_text(text[-4000:])

    def close(self) -> None:
        if self._writes is not None and self._writer is not None:
            self._writes.put(None)
            self._writer.join(10)
            self._writes = None
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._pool = None
        with self._lock:
            self.db.close()
