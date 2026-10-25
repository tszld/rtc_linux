from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


SCHEMA_VERSION = 3


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS scenarios (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('agent', 'admin')),
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_scenarios (
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    scenario_id TEXT NOT NULL REFERENCES scenarios(id) ON DELETE CASCADE,
    PRIMARY KEY (agent_id, scenario_id)
);

CREATE TABLE IF NOT EXISTS robots (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    scenario_id TEXT NOT NULL REFERENCES scenarios(id),
    channel_id TEXT UNIQUE NOT NULL,
    credential_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'offline'
        CHECK (status IN ('offline', 'online', 'degraded')),
    enabled INTEGER NOT NULL DEFAULT 1,
    approved INTEGER NOT NULL DEFAULT 0,
    last_seen_at TEXT,
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS robot_sessions (
    id TEXT PRIMARY KEY,
    robot_id TEXT NOT NULL REFERENCES robots(id),
    status TEXT NOT NULL
        CHECK (status IN ('active', 'agent_takeover', 'ended')),
    claimed_by_agent_id TEXT,
    claimed_at TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    end_reason TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_robot_unfinished_session
ON robot_sessions(robot_id)
WHERE status IN ('active', 'agent_takeover');

CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_active_session_claim
ON robot_sessions(claimed_by_agent_id)
WHERE claimed_by_agent_id IS NOT NULL
  AND status IN ('active', 'agent_takeover');

CREATE TABLE IF NOT EXISTS takeover_leases (
    id TEXT PRIMARY KEY,
    robot_id TEXT NOT NULL REFERENCES robots(id),
    session_id TEXT NOT NULL REFERENCES robot_sessions(id),
    agent_id TEXT NOT NULL REFERENCES agents(id),
    status TEXT NOT NULL CHECK (status IN ('active', 'released', 'expired')),
    microphone_published INTEGER NOT NULL DEFAULT 0,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    released_at TEXT,
    end_reason TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_robot_active_lease
ON takeover_leases(robot_id)
WHERE status = 'active';

CREATE TABLE IF NOT EXISTS robot_media_state (
    robot_id TEXT PRIMARY KEY REFERENCES robots(id) ON DELETE CASCADE,
    session_id TEXT REFERENCES robot_sessions(id),
    microphone_published INTEGER NOT NULL DEFAULT 0,
    camera_published INTEGER NOT NULL DEFAULT 0,
    automatic_speech_paused INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_listeners (
    session_id TEXT NOT NULL REFERENCES robot_sessions(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (session_id, agent_id)
);

CREATE TABLE IF NOT EXISTS access_tokens (
    id TEXT PRIMARY KEY,
    principal_type TEXT NOT NULL CHECK (principal_type IN ('agent', 'robot')),
    principal_id TEXT NOT NULL,
    token_hash TEXT UNIQUE NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_access_token_hash
ON access_tokens(token_hash);

CREATE TABLE IF NOT EXISTS ws_tickets (
    id TEXT PRIMARY KEY,
    principal_type TEXT NOT NULL CHECK (principal_type IN ('agent', 'robot')),
    principal_id TEXT NOT NULL,
    ticket_hash TEXT UNIQUE NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ws_ticket_hash
ON ws_tickets(ticket_hash);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_type TEXT NOT NULL,
    actor_id TEXT,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_created_at
ON audit_events(created_at DESC);
"""


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser().resolve()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version == 0:
                connection.executescript(SCHEMA_SQL)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                connection.commit()
            elif version in {1, 2}:
                if version == 1:
                    self._migrate_v1_to_v2(connection)
                self._migrate_v2_to_v3(connection)
            elif version != SCHEMA_VERSION:
                raise RuntimeError(
                    f"不支持的数据库版本 {version}，期望 {SCHEMA_VERSION}"
                )

    @staticmethod
    def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "ALTER TABLE robots ADD COLUMN deleted_at TEXT"
            )
            connection.execute(
                "ALTER TABLE robot_sessions "
                "ADD COLUMN claimed_by_agent_id TEXT"
            )
            connection.execute(
                "ALTER TABLE robot_sessions ADD COLUMN claimed_at TEXT"
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX uq_agent_active_session_claim
                ON robot_sessions(claimed_by_agent_id)
                WHERE claimed_by_agent_id IS NOT NULL
                  AND status IN ('active', 'agent_takeover')
                """
            )
            connection.execute("PRAGMA user_version = 2")
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    @staticmethod
    def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "ALTER TABLE takeover_leases "
                "ADD COLUMN microphone_published INTEGER NOT NULL DEFAULT 0"
            )
            connection.execute("PRAGMA user_version = 3")
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=5,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._open_connection()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._open_connection()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
