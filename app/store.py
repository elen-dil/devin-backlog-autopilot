"""SQLite-backed run store.

The `runs` table is the queue: enqueued issues persist their title/body/url
here, so a restart loses nothing. A partial unique index enforces
"one active run per issue" at the database level, which keeps the webhook
and the fallback poller from double-dispatching the same issue. Once a run
leaves an active state, the issue can be re-triggered by re-applying the
trigger label.
"""

import sqlite3
import threading
from datetime import datetime, timezone

ACTIVE_STATES = ("queued", "running")
ALL_STATES = ("queued", "running", "pr_open", "merged", "needs_human", "failed")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_number INTEGER NOT NULL,
    title TEXT NOT NULL,
    issue_url TEXT NOT NULL,
    issue_body TEXT,
    state TEXT NOT NULL DEFAULT 'queued',
    session_id TEXT,
    session_url TEXT,
    pr_url TEXT,
    outcome TEXT,
    summary TEXT,
    tests_run INTEGER,
    tests_passed INTEGER,
    acus REAL,
    output_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    merged_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_run_per_issue
    ON runs (issue_number) WHERE state IN ('queued', 'running');
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunStore:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)

    def enqueue(self, issue_number: int, title: str, issue_url: str, issue_body: str):
        """Insert a queued run. Returns the run id, or None if one is already active."""
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO runs (issue_number, title, issue_url, issue_body, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (issue_number, title, issue_url, issue_body, _now()),
                )
                self._conn.commit()
                return cur.lastrowid
            except sqlite3.IntegrityError:
                return None

    def update(self, run_id: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{key} = ?" for key in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE runs SET {cols} WHERE id = ?", (*fields.values(), run_id)
            )
            self._conn.commit()

    def get(self, run_id: int):
        row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def by_state(self, *states: str):
        placeholders = ",".join("?" for _ in states)
        rows = self._conn.execute(
            f"SELECT * FROM runs WHERE state IN ({placeholders}) ORDER BY id", states
        ).fetchall()
        return [dict(r) for r in rows]

    def oldest_queued(self, limit: int):
        rows = self._conn.execute(
            "SELECT * FROM runs WHERE state = 'queued' ORDER BY id LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def count_state(self, state: str) -> int:
        (n,) = self._conn.execute(
            "SELECT COUNT(*) FROM runs WHERE state = ?", (state,)
        ).fetchone()
        return n

    def all(self):
        rows = self._conn.execute("SELECT * FROM runs ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]
