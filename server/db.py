from __future__ import annotations

import hashlib
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .config import ServerConfig


SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_sessions (
    token_hash TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    FOREIGN KEY(agent_id) REFERENCES agents(id)
);

CREATE INDEX IF NOT EXISTS idx_agent_sessions_agent
ON agent_sessions(agent_id, revoked_at, expires_at);

CREATE TABLE IF NOT EXISTS calls (
    id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL UNIQUE,
    caller_id TEXT NOT NULL,
    caller_name TEXT NOT NULL,
    caller_uid TEXT NOT NULL,
    caller_token_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    claimed_agent_id TEXT,
    interrupted_side TEXT,
    created_at TEXT NOT NULL,
    ready_at TEXT,
    claimed_at TEXT,
    connected_at TEXT,
    interrupted_at TEXT,
    ended_at TEXT,
    end_reason TEXT,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY(claimed_agent_id) REFERENCES agents(id)
);

CREATE INDEX IF NOT EXISTS idx_calls_status_created
ON calls(status, created_at);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    actor_id TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_call
ON audit_events(call_id, id);
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_time(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat()


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    iterations = 260_000
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("ascii"), iterations
    ).hex()
    return f"pbkdf2_sha256${iterations}${salt}${digest}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iteration_text, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("ascii"),
            int(iteration_text),
        ).hex()
        return secrets.compare_digest(digest, expected)
    except (TypeError, ValueError):
        return False


def connect(config: ServerConfig) -> sqlite3.Connection:
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(config.db_path, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


@contextmanager
def db_session(config: ServerConfig) -> Iterator[sqlite3.Connection]:
    connection = connect(config)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database(config: ServerConfig, *, end_stale_calls: bool = True) -> None:
    with db_session(config) as connection:
        connection.executescript(SCHEMA)
        existing = connection.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
        if existing == 0:
            now = iso_time()
            for item in config.seed_agents:
                connection.execute(
                    """
                    INSERT INTO agents
                    (id, username, password_hash, display_name, enabled, created_at)
                    VALUES (?, ?, ?, ?, 1, ?)
                    """,
                    (
                        "agent-" + secrets.token_hex(8),
                        item["username"],
                        hash_password(item["password"]),
                        item.get("display_name", item["username"]),
                        now,
                    ),
                )
        if end_stale_calls:
            now = iso_time()
            connection.execute(
                """
                UPDATE calls
                SET status = 'ended',
                    ended_at = ?,
                    end_reason = 'backend_restart',
                    updated_at = ?,
                    version = version + 1
                WHERE status IN ('created', 'waiting', 'connecting', 'active', 'interrupted')
                """,
                (now, now),
            )


def add_audit(
    connection: sqlite3.Connection,
    call_id: str,
    event_type: str,
    actor_type: str,
    actor_id: str | None,
    metadata_json: str = "{}",
) -> None:
    connection.execute(
        """
        INSERT INTO audit_events
        (call_id, event_type, actor_type, actor_id, metadata_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (call_id, event_type, actor_type, actor_id, metadata_json, iso_time()),
    )


def duration_deadline(seconds: int) -> str:
    return iso_time(utc_now() + timedelta(seconds=seconds))
