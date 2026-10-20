from __future__ import annotations

import json
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from .auth import AuthenticatedAgent
from .clients import ClientIdentity
from .config import ServerConfig
from .db import (
    add_audit,
    db_session,
    hash_token,
    iso_time,
    parse_time,
    utc_now,
)
from .rtc import make_rtc_auth


VISIBLE_STATUSES = ("waiting", "connecting", "active", "interrupted")
OCCUPIED_STATUSES = ("connecting", "active", "interrupted")


class CallError(Exception):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class CallerIdentity:
    call_id: str
    caller_id: str


def _short_id() -> str:
    return uuid.uuid4().hex[:16]


def call_to_dict(
    row: sqlite3.Row,
    *,
    queue_position: int | None = None,
    queue_size: int | None = None,
) -> dict[str, Any]:
    return {
        "id": row["id"],
        "roomId": row["room_id"],
        "callerId": row["caller_id"],
        "callerName": row["caller_name"],
        "status": row["status"],
        "queuePosition": queue_position,
        "queueSize": queue_size,
        "claimedAgentId": row["claimed_agent_id"],
        "interruptedSide": row["interrupted_side"],
        "createdAt": row["created_at"],
        "readyAt": row["ready_at"],
        "claimedAt": row["claimed_at"],
        "connectedAt": row["connected_at"],
        "interruptedAt": row["interrupted_at"],
        "endedAt": row["ended_at"],
        "endReason": row["end_reason"],
        "updatedAt": row["updated_at"],
        "version": row["version"],
    }


def _fetch_call(connection: sqlite3.Connection, call_id: str) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
    if not row:
        raise CallError("会话不存在", 404)
    return row


def _waiting_queue_positions(
    connection: sqlite3.Connection,
) -> dict[str, tuple[int, int]]:
    rows = connection.execute(
        """
        SELECT id
        FROM calls
        WHERE status = 'waiting'
        ORDER BY COALESCE(ready_at, created_at) ASC, created_at ASC, id ASC
        """
    ).fetchall()
    queue_size = len(rows)
    return {
        row["id"]: (position, queue_size)
        for position, row in enumerate(rows, start=1)
    }


def _call_with_queue(
    connection: sqlite3.Connection, row: sqlite3.Row
) -> dict[str, Any]:
    queue_position = None
    queue_size = None
    if row["status"] == "waiting":
        queue_position, queue_size = _waiting_queue_positions(connection).get(
            row["id"], (None, 0)
        )
    return call_to_dict(
        row,
        queue_position=queue_position,
        queue_size=queue_size,
    )


def get_call(config: ServerConfig, call_id: str) -> dict[str, Any]:
    with db_session(config) as connection:
        return _call_with_queue(connection, _fetch_call(connection, call_id))


def verify_caller_token(
    config: ServerConfig, call_id: str, caller_token: str
) -> CallerIdentity:
    if not caller_token:
        raise CallError("缺少呼叫方凭证", 401)
    with db_session(config) as connection:
        row = _fetch_call(connection, call_id)
        if not secrets.compare_digest(
            row["caller_token_hash"], hash_token(caller_token)
        ):
            raise CallError("呼叫方凭证无效", 403)
        return CallerIdentity(call_id=row["id"], caller_id=row["caller_id"])


def create_call(
    config: ServerConfig, *, client_id: str, client_name: str
) -> tuple[dict[str, Any], str, dict[str, str]]:
    now = iso_time()
    call_id = "call-" + _short_id()
    room_id = "room-" + _short_id()
    caller_uid = "caller-" + _short_id()
    caller_token = secrets.token_urlsafe(36)
    rtc_auth = make_rtc_auth(
        config,
        room_id=room_id,
        user_id=caller_uid,
        user_name=client_name,
    )
    with db_session(config) as connection:
        connection.execute(
            """
            INSERT INTO calls
            (id, room_id, caller_id, caller_name, caller_uid, caller_token_hash,
             status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'created', ?, ?)
            """,
            (
                call_id,
                room_id,
                client_id,
                client_name,
                caller_uid,
                hash_token(caller_token),
                now,
                now,
            ),
        )
        add_audit(connection, call_id, "created", "caller", client_id)
        row = _fetch_call(connection, call_id)
        return call_to_dict(row), caller_token, rtc_auth


def create_call_for_client(
    config: ServerConfig,
    *,
    client: ClientIdentity,
) -> tuple[dict[str, Any], str, dict[str, str]]:
    now = iso_time()
    caller_token = secrets.token_urlsafe(36)
    with db_session(config) as connection:
        connection.execute("BEGIN IMMEDIATE")
        client_row = connection.execute(
            "SELECT id, display_name, status FROM clients WHERE id = ?",
            (client.id,),
        ).fetchone()
        if not client_row or client_row["status"] != "approved":
            raise CallError("客户端尚未通过管理员审批", 403)
        client_name = client_row["display_name"]
        existing = connection.execute(
            """
            SELECT *
            FROM calls
            WHERE caller_id = ?
              AND status IN ('created', 'waiting', 'connecting', 'active', 'interrupted')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (client.id,),
        ).fetchone()
        if existing:
            rtc_auth = make_rtc_auth(
                config,
                room_id=existing["room_id"],
                user_id=existing["caller_uid"],
                user_name=client_name,
            )
            connection.execute(
                """
                UPDATE calls
                SET caller_token_hash = ?, caller_name = ?, updated_at = ?,
                    version = version + 1
                WHERE id = ?
                """,
                (
                    hash_token(caller_token),
                    client_name,
                    now,
                    existing["id"],
                ),
            )
            add_audit(
                connection,
                existing["id"],
                "caller_reauthenticated",
                "caller",
                client.id,
            )
            row = _fetch_call(connection, existing["id"])
            return _call_with_queue(connection, row), caller_token, rtc_auth

        call_id = "call-" + _short_id()
        room_id = "room-" + _short_id()
        caller_uid = "caller-" + _short_id()
        rtc_auth = make_rtc_auth(
            config,
            room_id=room_id,
            user_id=caller_uid,
            user_name=client_name,
        )
        connection.execute(
            """
            INSERT INTO calls
            (id, room_id, caller_id, caller_name, caller_uid, caller_token_hash,
             status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'created', ?, ?)
            """,
            (
                call_id,
                room_id,
                client.id,
                client_name,
                caller_uid,
                hash_token(caller_token),
                now,
                now,
            ),
        )
        add_audit(connection, call_id, "created", "caller", client.id)
        return call_to_dict(_fetch_call(connection, call_id)), caller_token, rtc_auth


def mark_caller_ready(
    config: ServerConfig, call_id: str, caller_token: str
) -> dict[str, Any]:
    verify_caller_token(config, call_id, caller_token)
    now = iso_time()
    with db_session(config) as connection:
        changed = connection.execute(
            """
            UPDATE calls
            SET status = 'waiting',
                ready_at = COALESCE(ready_at, ?),
                updated_at = ?,
                version = version + 1
            WHERE id = ?
              AND status IN ('created', 'waiting')
            """,
            (now, now, call_id),
        ).rowcount
        if not changed:
            row = _fetch_call(connection, call_id)
            if row["status"] != "waiting":
                raise CallError("当前会话不能进入等待状态")
        add_audit(connection, call_id, "caller_ready", "caller", None)
        return _call_with_queue(connection, _fetch_call(connection, call_id))


def list_visible_calls(config: ServerConfig) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in VISIBLE_STATUSES)
    with db_session(config) as connection:
        rows = connection.execute(
            f"""
            SELECT *
            FROM calls
            WHERE status IN ({placeholders})
            ORDER BY
                CASE status
                    WHEN 'waiting' THEN 0
                    WHEN 'connecting' THEN 1
                    WHEN 'active' THEN 2
                    ELSE 3
                END,
                CASE
                    WHEN status = 'waiting' THEN COALESCE(ready_at, created_at)
                    ELSE created_at
                END ASC,
                id ASC
            """,
            VISIBLE_STATUSES,
        ).fetchall()
        queue_positions = _waiting_queue_positions(connection)
        return [
            call_to_dict(
                row,
                queue_position=queue_positions.get(row["id"], (None, None))[0],
                queue_size=queue_positions.get(row["id"], (None, None))[1],
            )
            for row in rows
        ]


def assign_waiting_calls(
    config: ServerConfig,
    agent_ids: list[str],
) -> list[dict[str, Any]]:
    connected_agent_ids = list(
        dict.fromkeys(agent_id for agent_id in agent_ids if agent_id)
    )
    if not connected_agent_ids:
        return []

    placeholders = ",".join("?" for _ in connected_agent_ids)
    now = iso_time()
    assigned: list[dict[str, Any]] = []
    with db_session(config) as connection:
        connection.execute("BEGIN IMMEDIATE")
        agent_rows = connection.execute(
            f"""
            SELECT a.id, a.display_name
            FROM agents AS a
            WHERE a.id IN ({placeholders})
              AND a.enabled = 1
              AND NOT EXISTS (
                  SELECT 1
                  FROM calls AS busy
                  WHERE busy.claimed_agent_id = a.id
                    AND busy.status IN ('connecting', 'active', 'interrupted')
              )
            """,
            tuple(connected_agent_ids),
        ).fetchall()
        agents_by_id = {row["id"]: row for row in agent_rows}
        agents = [
            agents_by_id[agent_id]
            for agent_id in connected_agent_ids
            if agent_id in agents_by_id
        ]

        for agent in agents:
            waiting = connection.execute(
                """
                SELECT *
                FROM calls
                WHERE status = 'waiting'
                ORDER BY COALESCE(ready_at, created_at) ASC, created_at ASC, id ASC
                LIMIT 1
                """
            ).fetchone()
            if not waiting:
                break
            changed = connection.execute(
                """
                UPDATE calls
                SET status = 'connecting',
                    claimed_agent_id = ?,
                    claimed_at = ?,
                    connected_at = NULL,
                    interrupted_side = NULL,
                    interrupted_at = NULL,
                    updated_at = ?,
                    version = version + 1
                WHERE id = ? AND status = 'waiting'
                """,
                (agent["id"], now, now, waiting["id"]),
            ).rowcount
            if not changed:
                continue
            add_audit(
                connection,
                waiting["id"],
                "assigned",
                "system",
                agent["id"],
            )
            assigned.append(call_to_dict(_fetch_call(connection, waiting["id"])))
    return assigned


def get_assigned_call(
    config: ServerConfig,
    call_id: str,
    agent: AuthenticatedAgent,
) -> tuple[dict[str, Any], dict[str, str]]:
    with db_session(config) as connection:
        row = _fetch_call(connection, call_id)
        if row["claimed_agent_id"] != agent.id:
            raise CallError("当前客服未被分配到该会话", 403)
        if row["status"] != "connecting":
            raise CallError("该派单已失效")
        rtc_auth = make_rtc_auth(
            config,
            room_id=row["room_id"],
            user_id=agent.id,
            user_name=agent.display_name,
        )
        return call_to_dict(row), rtc_auth


def claim_call(
    config: ServerConfig, call_id: str, agent: AuthenticatedAgent
) -> tuple[dict[str, Any], dict[str, str]]:
    now = iso_time()
    with db_session(config) as connection:
        connection.execute("BEGIN IMMEDIATE")
        busy = connection.execute(
            """
            SELECT id FROM calls
            WHERE claimed_agent_id = ?
              AND status IN ('connecting', 'active', 'interrupted')
            LIMIT 1
            """,
            (agent.id,),
        ).fetchone()
        if busy:
            raise CallError("当前客服已有正在处理的会话")
        row = _fetch_call(connection, call_id)
        if row["status"] != "waiting":
            raise CallError("该会话已被其他客服接听")
        rtc_auth = make_rtc_auth(
            config,
            room_id=row["room_id"],
            user_id=agent.id,
            user_name=agent.display_name,
        )
        changed = connection.execute(
            """
            UPDATE calls
            SET status = 'connecting',
                claimed_agent_id = ?,
                claimed_at = ?,
                connected_at = NULL,
                interrupted_side = NULL,
                interrupted_at = NULL,
                updated_at = ?,
                version = version + 1
            WHERE id = ? AND status = 'waiting'
            """,
            (agent.id, now, now, call_id),
        ).rowcount
        if not changed:
            raise CallError("该会话已被其他客服接听")
        add_audit(connection, call_id, "claimed", "agent", agent.id)
        return call_to_dict(_fetch_call(connection, call_id)), rtc_auth


def mark_agent_connected(
    config: ServerConfig, call_id: str, agent: AuthenticatedAgent
) -> dict[str, Any]:
    now = iso_time()
    with db_session(config) as connection:
        changed = connection.execute(
            """
            UPDATE calls
            SET status = 'active',
                connected_at = COALESCE(connected_at, ?),
                interrupted_side = NULL,
                interrupted_at = NULL,
                updated_at = ?,
                version = version + 1
            WHERE id = ?
              AND claimed_agent_id = ?
              AND status IN ('connecting', 'interrupted')
            """,
            (now, now, call_id, agent.id),
        ).rowcount
        if not changed:
            raise CallError("会话不处于可连接状态")
        add_audit(connection, call_id, "rtc_connected", "agent", agent.id)
        return call_to_dict(_fetch_call(connection, call_id))


def agent_hangup(
    config: ServerConfig, call_id: str, agent: AuthenticatedAgent
) -> dict[str, Any]:
    now = iso_time()
    with db_session(config) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _fetch_call(connection, call_id)
        if row["claimed_agent_id"] != agent.id:
            raise CallError("当前客服未占用该会话", 403)
        if row["status"] not in OCCUPIED_STATUSES:
            raise CallError("当前会话不能挂断")
        changed = connection.execute(
            """
            UPDATE calls
            SET status = 'ended',
                interrupted_side = NULL,
                interrupted_at = NULL,
                ended_at = ?,
                end_reason = 'agent_hangup',
                updated_at = ?,
                version = version + 1
            WHERE id = ?
              AND claimed_agent_id = ?
              AND status IN ('connecting', 'active', 'interrupted')
            """,
            (now, now, call_id, agent.id),
        ).rowcount
        if not changed:
            raise CallError("当前会话不能挂断")
        add_audit(connection, call_id, "agent_hangup", "agent", agent.id)
        return call_to_dict(_fetch_call(connection, call_id))


def resume_agent_call(
    config: ServerConfig, call_id: str, agent: AuthenticatedAgent
) -> tuple[dict[str, Any], dict[str, str]]:
    now = iso_time()
    with db_session(config) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _fetch_call(connection, call_id)
        if (
            row["claimed_agent_id"] != agent.id
            or row["status"] != "interrupted"
            or row["interrupted_side"] != "agent"
        ):
            raise CallError("当前会话不能由该客服恢复", 403)
        rtc_auth = make_rtc_auth(
            config,
            room_id=row["room_id"],
            user_id=agent.id,
            user_name=agent.display_name,
        )
        connection.execute(
            """
            UPDATE calls
            SET status = 'connecting',
                interrupted_side = NULL,
                interrupted_at = NULL,
                updated_at = ?,
                version = version + 1
            WHERE id = ?
            """,
            (now, call_id),
        )
        add_audit(connection, call_id, "agent_resume", "agent", agent.id)
        return call_to_dict(_fetch_call(connection, call_id)), rtc_auth


def end_call_by_caller(
    config: ServerConfig, call_id: str, caller_token: str
) -> dict[str, Any]:
    verify_caller_token(config, call_id, caller_token)
    return end_call(config, call_id, "caller_hangup", "caller", None)


def end_call(
    config: ServerConfig,
    call_id: str,
    reason: str,
    actor_type: str,
    actor_id: str | None,
) -> dict[str, Any]:
    now = iso_time()
    with db_session(config) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _fetch_call(connection, call_id)
        if row["status"] == "ended":
            return call_to_dict(row)
        connection.execute(
            """
            UPDATE calls
            SET status = 'ended',
                interrupted_side = NULL,
                interrupted_at = NULL,
                ended_at = ?,
                end_reason = ?,
                updated_at = ?,
                version = version + 1
            WHERE id = ?
            """,
            (now, reason, now, call_id),
        )
        add_audit(connection, call_id, reason, actor_type, actor_id)
        return call_to_dict(_fetch_call(connection, call_id))


def mark_agent_disconnected(config: ServerConfig, agent_id: str) -> list[str]:
    now = iso_time()
    changed_ids: list[str] = []
    with db_session(config) as connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            """
            SELECT id FROM calls
            WHERE claimed_agent_id = ?
              AND status IN ('connecting', 'active')
            """,
            (agent_id,),
        ).fetchall()
        for row in rows:
            changed = connection.execute(
                """
                UPDATE calls
                SET status = 'interrupted',
                    interrupted_side = 'agent',
                    interrupted_at = ?,
                    updated_at = ?,
                    version = version + 1
                WHERE id = ?
                  AND claimed_agent_id = ?
                  AND status IN ('connecting', 'active')
                """,
                (now, now, row["id"], agent_id),
            ).rowcount
            if changed:
                add_audit(
                    connection,
                    row["id"],
                    "agent_disconnected",
                    "system",
                    agent_id,
                )
                changed_ids.append(row["id"])
    return changed_ids


def mark_caller_disconnected(config: ServerConfig, call_id: str) -> bool:
    now = iso_time()
    with db_session(config) as connection:
        connection.execute("BEGIN IMMEDIATE")
        changed = connection.execute(
            """
            UPDATE calls
            SET status = 'ended',
                interrupted_side = NULL,
                interrupted_at = NULL,
                ended_at = ?,
                end_reason = 'caller_disconnected',
                updated_at = ?,
                version = version + 1
            WHERE id = ?
              AND status IN ('created', 'waiting', 'connecting', 'active', 'interrupted')
            """,
            (now, now, call_id),
        ).rowcount
        if changed:
            add_audit(connection, call_id, "caller_disconnected", "system", None)
        return bool(changed)


def resume_caller(config: ServerConfig, call_id: str) -> dict[str, Any] | None:
    now = iso_time()
    with db_session(config) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _fetch_call(connection, call_id)
        if row["status"] != "interrupted" or row["interrupted_side"] != "caller":
            return None
        if row["claimed_agent_id"] and row["connected_at"]:
            next_status = "active"
        elif row["claimed_agent_id"]:
            next_status = "connecting"
        else:
            next_status = "waiting"
        connection.execute(
            """
            UPDATE calls
            SET status = ?,
                interrupted_side = NULL,
                interrupted_at = NULL,
                updated_at = ?,
                version = version + 1
            WHERE id = ?
            """,
            (next_status, now, call_id),
        )
        add_audit(connection, call_id, "caller_resume", "system", None)
        return call_to_dict(_fetch_call(connection, call_id))


def expire_calls(config: ServerConfig) -> list[str]:
    now = utc_now()
    now_text = iso_time(now)
    changed: list[str] = []
    with db_session(config) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connecting_rows = connection.execute(
            """
            SELECT id, claimed_at
            FROM calls
            WHERE status = 'connecting' AND claimed_at IS NOT NULL
            """
        ).fetchall()
        for row in connecting_rows:
            if parse_time(row["claimed_at"]) + timedelta(
                seconds=config.connecting_timeout_seconds
            ) <= now:
                updated = connection.execute(
                    """
                    UPDATE calls
                    SET status = 'waiting',
                        claimed_agent_id = NULL,
                        claimed_at = NULL,
                        connected_at = NULL,
                        updated_at = ?,
                        version = version + 1
                    WHERE id = ? AND status = 'connecting'
                    """,
                    (now_text, row["id"]),
                ).rowcount
                if updated:
                    add_audit(
                        connection, row["id"], "connecting_timeout", "system", None
                    )
                    changed.append(row["id"])

        interrupted_rows = connection.execute(
            """
            SELECT id, interrupted_at
            FROM calls
            WHERE status = 'interrupted' AND interrupted_at IS NOT NULL
            """
        ).fetchall()
        for row in interrupted_rows:
            if parse_time(row["interrupted_at"]) + timedelta(
                seconds=config.interrupted_timeout_seconds
            ) <= now:
                updated = connection.execute(
                    """
                    UPDATE calls
                    SET status = 'ended',
                        ended_at = ?,
                        end_reason = 'interrupted_timeout',
                        interrupted_side = NULL,
                        interrupted_at = NULL,
                        updated_at = ?,
                        version = version + 1
                    WHERE id = ? AND status = 'interrupted'
                    """,
                    (now_text, now_text, row["id"]),
                ).rowcount
                if updated:
                    add_audit(
                        connection, row["id"], "interrupted_timeout", "system", None
                    )
                    changed.append(row["id"])

        created_rows = connection.execute(
            """
            SELECT id, created_at
            FROM calls
            WHERE status = 'created'
            """
        ).fetchall()
        for row in created_rows:
            if parse_time(row["created_at"]) + timedelta(
                seconds=config.created_timeout_seconds
            ) <= now:
                updated = connection.execute(
                    """
                    UPDATE calls
                    SET status = 'ended',
                        ended_at = ?,
                        end_reason = 'caller_join_timeout',
                        updated_at = ?,
                        version = version + 1
                    WHERE id = ? AND status = 'created'
                    """,
                    (now_text, now_text, row["id"]),
                ).rowcount
                if updated:
                    add_audit(
                        connection, row["id"], "caller_join_timeout", "system", None
                    )
                    changed.append(row["id"])
    return changed


def audit_json(metadata: dict[str, Any]) -> str:
    return json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
