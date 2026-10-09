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

_TX = object()  # značka transakce ve frontě zápisů

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

-- Hlasování kanceláře (PLAN H, ytdj/votes.py): jeden řádek na hlasujícího
-- a cíl — změna hlasu řádek přepíše, stažení nechá vote=0 (kdo a kdy zůstane).
-- Stav (oblíbená / vyřazená) se neukládá, počítá se z hlasů. Odděleně od
-- `feedback` (bez hlasujícího) i od technického `blacklist` (nepřehratelné).
CREATE TABLE IF NOT EXISTS votes (
    target   TEXT NOT NULL,                    -- song|artist
    key      TEXT NOT NULL,                    -- kanonický klíč (votes.song_key / artist_key)
    voter    TEXT NOT NULL,                    -- id klienta (Wish.key)
    vote     INTEGER NOT NULL,                 -- 1|-1|0 (staženo)
    who      TEXT,                             -- přezdívka v době hlasu
    video_id TEXT,
    artist   TEXT,
    title    TEXT,
    ts       REAL NOT NULL,
    PRIMARY KEY (target, key, voter)
);

-- Import playlistu do oblíbených (ytdj/imports.py, FUNKCE F-HLASY-12…):
-- playlist člověka = jeho 👍 každé písničce v něm. 👍 z importu se do `votes`
-- nezapisují — počítají se z `import_items` (vlastní hlas člověka má
-- přednost, Odebrat = smazat import a jeho 👍 zmizí).
CREATE TABLE IF NOT EXISTS imports (
    id          TEXT PRIMARY KEY,
    client      TEXT NOT NULL,                 -- id klienta toho, kdo importoval
    who         TEXT,                          -- přezdívka v době importu
    playlist_id TEXT NOT NULL,
    title       TEXT,
    created     REAL NOT NULL,
    fetched     REAL NOT NULL,                 -- poslední načtení (Obnovit)
    total       INTEGER,                       -- skladeb v playlistu podle YouTube
    skipped     INTEGER,                       -- nepísničky a nedostupné
    cut         INTEGER,                       -- nad limit playlist_import_max
    label       TEXT                           -- vlastní název od člověka ('' / NULL = název z YouTube)
);
CREATE TABLE IF NOT EXISTS import_items (
    import_id TEXT NOT NULL,
    key       TEXT NOT NULL,                   -- klíč skladby (votes.song_key_for)
    video_id  TEXT,
    artist    TEXT,
    title     TEXT,
    added     REAL NOT NULL,                   -- kdy se písnička v importu objevila
    pos       INTEGER,
    PRIMARY KEY (import_id, key)
);
-- Písničky, které člověk ze svého importu vyřadil (F-HLASY-22): nejsou v
-- `import_items`, takže jeho 👍 z playlistu nenesou; Obnovit je nevrátí,
-- ani když z playlistu na YouTube zmizí a zase se objeví. Vrátit = řádek
-- zpátky do `import_items`.
CREATE TABLE IF NOT EXISTS import_removed (
    import_id TEXT NOT NULL,
    key       TEXT NOT NULL,
    video_id  TEXT,
    artist    TEXT,
    title     TEXT,
    removed   REAL NOT NULL,                   -- kdy ji člověk vyřadil
    pos       INTEGER,
    PRIMARY KEY (import_id, key)
);

-- Chyby a nápady kolegů (ytdj/issues.py, FUNKCE F-HLASENI-01…): položka,
-- komentáře a „+1". Všechno drží IssueBook v paměti, sem se jen zapisuje.
-- `seed_key` / `seed_sig` mají položky, které jdou s kódem (issues_seed.json).
CREATE TABLE IF NOT EXISTS issues (
    id          INTEGER PRIMARY KEY,
    seed_key    TEXT,                          -- stálý klíč ze seedu, jinak NULL
    seed_sig    TEXT,                          -- otisk seedu, který je promítnutý
    kind        TEXT NOT NULL,                 -- chyba|napad
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    state       TEXT NOT NULL,                 -- nove|resi_se|nasazeni|vyreseno|zamitnuto
    resolution  TEXT NOT NULL DEFAULT '',      -- „Jak to bylo vyřešeno"
    resolved_at REAL,
    author      TEXT,                          -- id klienta (jako u přání)
    who         TEXT,                          -- přezdívka v době zápisu
    created     REAL NOT NULL,
    updated     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS issue_comments (
    id       INTEGER PRIMARY KEY,
    issue_id INTEGER NOT NULL,
    author   TEXT,
    who      TEXT,
    body     TEXT NOT NULL,
    ts       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS issue_comments_issue ON issue_comments(issue_id);
CREATE TABLE IF NOT EXISTS issue_plus (
    issue_id INTEGER NOT NULL,
    voter    TEXT NOT NULL,                    -- id klienta
    who      TEXT,
    ts       REAL NOT NULL,
    PRIMARY KEY (issue_id, voter)
);
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


BLACKLIST_DAYS = 7  # jak dlouho se nepřehratelná skladba nenabízí (smazaná natrvalo)


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
        self._migrate_blacklist()
        self._migrate_imports()
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
                elif item[0] is _TX:
                    self._run_tx(db, item[1])
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

    def _write_tx(self, statements: list[tuple[str, Any]]) -> None:
        """Víc zápisů jako jedna transakce ve vlákně zápisů — jeden commit
        (na SD kartě jeden zápis WAL místo stovek). Parametry jako seznam
        n-tic = executemany."""
        if self._writes is not None:
            self._writes.put((_TX, list(statements)))
            return
        with self._lock:
            self._run_tx(self.db, statements)

    @staticmethod
    def _run_tx(db: sqlite3.Connection, statements: list[tuple[str, Any]]) -> None:
        db.execute("BEGIN")
        try:
            for sql, params in statements:
                if isinstance(params, list):
                    db.executemany(sql, params)
                else:
                    db.execute(sql, params)
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")

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

    def save_vote(self, target: str, key: str, voter: str, vote: int, who: str,
                  video_id: str, artist: str, title: str, ts: float) -> None:
        """Hlas kanceláře (přepíše předchozí hlas téhož člověka na týž cíl)."""
        self._write(
            """INSERT OR REPLACE INTO votes(target,key,voter,vote,who,video_id,artist,title,ts)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (target, key, voter, int(vote), who, video_id, artist, title, ts),
        )

    def save_import(self, meta: tuple, items: list[tuple]) -> None:
        """Import playlistu (nový i obnovený) celý najednou: `meta` = řádek
        `imports` (PlaylistImport.meta), `items` = (key, video_id, artist,
        title, added, pos). Vyřazené písničky (`import_removed`) nechává být."""
        iid = meta[0]
        self._write_tx([
            ("""INSERT OR REPLACE INTO imports(id,client,who,playlist_id,title,created,fetched,
                                                total,skipped,cut,label)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
             (*meta[:10], meta[10] if len(meta) > 10 else "")),
            ("DELETE FROM import_items WHERE import_id=?", (iid,)),
            ("""INSERT INTO import_items(import_id,key,video_id,artist,title,added,pos)
                VALUES(?,?,?,?,?,?,?)""", [(iid, *it) for it in items]),
        ])

    def remove_import_song(self, import_id: str, row: tuple, removed: float) -> None:
        """Písnička z importu mezi vyřazené (jedna transakce, dva řádky):
        `row` = (key, video_id, artist, title, added, pos)."""
        key, vid, artist, title, _added, pos = row
        self._write_tx([
            ("DELETE FROM import_items WHERE import_id=? AND key=?", (import_id, key)),
            ("""INSERT OR REPLACE INTO import_removed(import_id,key,video_id,artist,title,removed,pos)
                VALUES(?,?,?,?,?,?,?)""", (import_id, key, vid, artist, title, removed, pos)),
        ])

    def restore_import_song(self, import_id: str, row: tuple) -> None:
        """Vyřazená písnička zpátky do importu: `row` jako u `save_import`."""
        self._write_tx([
            ("DELETE FROM import_removed WHERE import_id=? AND key=?", (import_id, row[0])),
            ("""INSERT OR REPLACE INTO import_items(import_id,key,video_id,artist,title,added,pos)
                VALUES(?,?,?,?,?,?,?)""", (import_id, *row)),
        ])

    def rename_import(self, import_id: str, label: str) -> None:
        self._write("UPDATE imports SET label=? WHERE id=?", (label, import_id))

    def delete_import(self, import_id: str) -> None:
        self._write_tx([
            ("DELETE FROM import_items WHERE import_id=?", (import_id,)),
            ("DELETE FROM import_removed WHERE import_id=?", (import_id,)),
            ("DELETE FROM imports WHERE id=?", (import_id,)),
        ])

    # ---- chyby a nápady (ytdj/issues.py) ----

    def save_issue(self, row: tuple) -> None:
        """Celá položka (nová i změněná): id, seed_key, seed_sig, kind, title,
        body, state, resolution, resolved_at, author, who, created, updated."""
        self._write(
            """INSERT OR REPLACE INTO issues(id,seed_key,seed_sig,kind,title,body,state,resolution,
                                              resolved_at,author,who,created,updated)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", tuple(row))

    def save_issue_comment(self, row: tuple) -> None:
        """Komentář: id, issue_id, author, who, body, ts."""
        self._write(
            "INSERT OR REPLACE INTO issue_comments(id,issue_id,author,who,body,ts) VALUES(?,?,?,?,?,?)",
            tuple(row))

    def delete_issue_comment(self, comment_id: int) -> None:
        self._write("DELETE FROM issue_comments WHERE id=?", (int(comment_id),))

    def set_issue_plus(self, issue_id: int, voter: str, who: str, ts: float, on: bool) -> None:
        if on:
            self._write("INSERT OR REPLACE INTO issue_plus(issue_id,voter,who,ts) VALUES(?,?,?,?)",
                        (int(issue_id), voter, who, ts))
        else:
            self._write("DELETE FROM issue_plus WHERE issue_id=? AND voter=?", (int(issue_id), voter))

    def delete_issue(self, issue_id: int) -> None:
        iid = (int(issue_id),)
        self._write_tx([
            ("DELETE FROM issue_comments WHERE issue_id=?", iid),
            ("DELETE FROM issue_plus WHERE issue_id=?", iid),
            ("DELETE FROM issues WHERE id=?", iid),
        ])

    def all_issues(self) -> tuple[list[tuple], list[tuple], list[tuple]]:
        """(položky, komentáře, +1) — nejvýš stovky řádků, čte se při startu."""
        items = self._read(
            """SELECT id, seed_key, seed_sig, kind, title, body, state, COALESCE(resolution,''),
                      resolved_at, COALESCE(author,''), COALESCE(who,''), created, updated
                 FROM issues ORDER BY id""")
        comments = self._read(
            """SELECT id, issue_id, COALESCE(author,''), COALESCE(who,''), body, ts
                 FROM issue_comments ORDER BY id""")
        plus = self._read("SELECT issue_id, voter, COALESCE(who,''), ts FROM issue_plus ORDER BY ts")
        return items, comments, plus

    def _migrate_blacklist(self) -> None:
        """Sloupec `until` (platnost záznamu). Staré záznamy bez něj vznikaly
        i z výpadků sítě ("nepřehratelné" u všeho, co mpv zkusilo) — dostanou
        stejnou lhůtu jako nové, ať se samy vyčistí."""
        with self._lock, contextlib.suppress(sqlite3.Error):
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(blacklist)")}
            if "until" not in cols:
                self.db.execute("ALTER TABLE blacklist ADD COLUMN until REAL")
                self.db.execute(
                    "UPDATE blacklist SET until = ts + ? WHERE until IS NULL",
                    (BLACKLIST_DAYS * 86400,),
                )

    def _migrate_imports(self) -> None:
        """Sloupec `label` (vlastní název importu, F-HLASY-23) do `imports`
        založené před 9. 10. 2026. Řádky se nemění — bez vlastního názvu
        platí dál název z YouTube. Tabulku `import_removed` zakládá SCHEMA."""
        with self._lock, contextlib.suppress(sqlite3.Error):
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(imports)")}
            if "label" not in cols:
                self.db.execute("ALTER TABLE imports ADD COLUMN label TEXT")

    def blacklist(self, video_id: str, reason: str, days: float | None = BLACKLIST_DAYS) -> None:
        """Skladbu nenabízet. `days=None` = natrvalo (smazané video); jinak
        jen na čas — "nepřehratelné dnes" (věk, region, dočasně nedostupné)
        nemusí platit navždy a omyl (výpadek sítě) se tak sám napraví."""
        now = time.time()
        until = None if days is None else now + days * 86400
        self._write(
            "INSERT OR REPLACE INTO blacklist(video_id,reason,ts,until) VALUES(?,?,?,?)",
            (video_id, reason, now, until),
        )

    # ---- reads ----

    def recently_played(self, days: int) -> set[str]:
        cutoff = time.time() - days * 86400
        rows = self._read("SELECT DISTINCT video_id FROM plays WHERE ts > ?", (cutoff,))
        return {r[0] for r in rows}

    def blacklisted(self) -> set[str]:
        return {r[0] for r in self._read(
            "SELECT video_id FROM blacklist WHERE until IS NULL OR until > ?", (time.time(),))}

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

    def all_votes(self) -> list[tuple]:
        """Všechny hlasy (i stažené), nejstarší první — pár set řádků, čte se při startu."""
        return self._read(
            """SELECT target, key, voter, vote, COALESCE(who,''), COALESCE(video_id,''),
                      COALESCE(artist,''), COALESCE(title,''), ts
                 FROM votes ORDER BY ts"""
        )

    def all_imports(self) -> tuple[list[tuple], list[tuple]]:
        """(importy, jejich písničky) — pár tisíc řádků, čte se při startu.
        Vyřazené písničky v tom nejsou (`import_removed_rows`)."""
        meta = self._read(
            """SELECT id, client, COALESCE(who,''), playlist_id, COALESCE(title,''), created,
                      fetched, COALESCE(total,0), COALESCE(skipped,0), COALESCE(cut,0),
                      COALESCE(label,'')
                 FROM imports ORDER BY created"""
        )
        items = self._read(
            """SELECT import_id, key, COALESCE(video_id,''), COALESCE(artist,''),
                      COALESCE(title,''), added, COALESCE(pos,0)
                 FROM import_items ORDER BY import_id, pos"""
        )
        return meta, items

    def import_removed_rows(self) -> list[tuple]:
        """Písničky, které lidé ze svých importů vyřadili (čte se při startu)."""
        return self._read(
            """SELECT import_id, key, COALESCE(video_id,''), COALESCE(artist,''),
                      COALESCE(title,''), removed, COALESCE(pos,0)
                 FROM import_removed ORDER BY import_id, pos"""
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
