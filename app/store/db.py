"""
Application persistence layer (SQLite).

Stores workspaces (clients), connected email accounts, knowledge documents,
processed emails, AI drafts and the activity log. Each row is scoped to a
workspace so several clients can share one deployment without seeing each
other's data.
"""

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from app.config import Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    settings TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS email_accounts (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'gmail',
    email TEXT NOT NULL,
    token_encrypted TEXT,
    status TEXT NOT NULL DEFAULT 'connected',
    status_message TEXT DEFAULT '',
    connected_at TEXT NOT NULL,
    last_checked_at TEXT,
    UNIQUE (workspace_id, email)
);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    file_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'processing',
    status_message TEXT DEFAULT '',
    chunk_count INTEGER NOT NULL DEFAULT 0,
    uploaded_at TEXT NOT NULL,
    processed_at TEXT,
    UNIQUE (workspace_id, filename)
);

CREATE TABLE IF NOT EXISTS emails (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    account_id TEXT,
    account_email TEXT DEFAULT '',
    gmail_message_id TEXT NOT NULL,
    thread_id TEXT DEFAULT '',
    message_id_header TEXT DEFAULT '',
    references_header TEXT DEFAULT '',
    sender TEXT DEFAULT '',
    recipient TEXT DEFAULT '',
    subject TEXT DEFAULT '',
    body TEXT DEFAULT '',
    snippet TEXT DEFAULT '',
    received_at TEXT,
    category TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    status_message TEXT DEFAULT '',
    security_flag TEXT DEFAULT '',
    security_reason TEXT DEFAULT '',
    knowledge_sources TEXT DEFAULT '[]',
    meta TEXT DEFAULT '{}',
    processed_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (workspace_id, gmail_message_id)
);

CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    email_id TEXT NOT NULL,
    account_id TEXT,
    gmail_draft_id TEXT,
    gmail_message_id TEXT,
    thread_id TEXT DEFAULT '',
    recipient TEXT DEFAULT '',
    subject TEXT DEFAULT '',
    body TEXT DEFAULT '',
    original_body TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending_review',
    status_message TEXT DEFAULT '',
    needs_attention INTEGER NOT NULL DEFAULT 0,
    attention_reason TEXT DEFAULT '',
    reviewed_by TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    ref_type TEXT DEFAULT '',
    ref_id TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_tests (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    question TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'processing',
    stage TEXT DEFAULT '',
    answer TEXT DEFAULT '',
    evidence TEXT DEFAULT '[]',
    message TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    completed_at TEXT
);

-- Application-wide settings (not tied to a workspace), e.g. the AI provider choice
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ktests_ws ON knowledge_tests (workspace_id, created_at);
CREATE INDEX IF NOT EXISTS idx_emails_ws ON emails (workspace_id, received_at);
CREATE INDEX IF NOT EXISTS idx_drafts_ws ON drafts (workspace_id, status);
CREATE INDEX IF NOT EXISTS idx_drafts_email ON drafts (email_id);
CREATE INDEX IF NOT EXISTS idx_activity_ws ON activity (workspace_id, id);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex[:16]


class Database:
    """Thread-safe SQLite wrapper. One connection guarded by a re-entrant lock."""

    def __init__(self, path: Optional[str] = None):
        self.path = str(path or Config.APP_DB_PATH)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    # Columns added after the first release; added in place on existing databases.
    MIGRATIONS = {
        "emails": {"question": "TEXT DEFAULT ''", "evidence": "TEXT DEFAULT '[]'"},
        "knowledge_tests": {"follow_up_to": "TEXT DEFAULT ''", "search_question": "TEXT DEFAULT ''"},
    }

    def _migrate(self):
        for table, columns in self.MIGRATIONS.items():
            existing = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            for name, ddl in columns.items():
                if name not in existing:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    @contextmanager
    def transaction(self):
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self.transaction() as conn:
            cur = conn.execute(sql, tuple(params))
            return cur.rowcount

    def fetchone(self, sql: str, params: Iterable[Any] = ()) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
            return dict(row) if row else None

    def fetchall(self, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
            return row[0] if row else None

    def insert(self, table: str, values: Dict[str, Any]) -> None:
        cols = ", ".join(values.keys())
        marks = ", ".join("?" for _ in values)
        self.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", values.values())

    def update(self, table: str, row_id: str, values: Dict[str, Any]) -> int:
        if not values:
            return 0
        sets = ", ".join(f"{k} = ?" for k in values.keys())
        return self.execute(f"UPDATE {table} SET {sets} WHERE id = ?", [*values.values(), row_id])

    def close(self):
        with self._lock:
            self._conn.close()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def loads(value: Optional[str], default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


_db: Optional[Database] = None
_db_lock = threading.Lock()


def get_db() -> Database:
    global _db
    with _db_lock:
        if _db is None:
            _db = Database()
        return _db


def set_db(db: Optional[Database]) -> None:
    """Swap the global database (used by tests)."""
    global _db
    with _db_lock:
        _db = db
