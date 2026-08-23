from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def connect(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, check_same_thread=False, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_runtime(path: Path) -> None:
    with connect(path) as db:
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
              password_hash BLOB NOT NULL, role TEXT NOT NULL,
              account_id TEXT, display_name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS sessions (
              id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
              csrf_token TEXT NOT NULL, expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending_actions (
              id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
              action_type TEXT NOT NULL, payload_json TEXT NOT NULL,
              expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS actions (
              id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
              action_type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_events (
              id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id), event_type TEXT NOT NULL,
              detail TEXT NOT NULL, created_at TEXT NOT NULL
            );
            """
        )
        db.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at);
            CREATE INDEX IF NOT EXISTS idx_pending_actions_user_id ON pending_actions(user_id);
            CREATE INDEX IF NOT EXISTS idx_pending_actions_expires_at ON pending_actions(expires_at);
            CREATE INDEX IF NOT EXISTS idx_audit_events_created_at ON audit_events(created_at);
            """
        )
