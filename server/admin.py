from __future__ import annotations

import json
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from .config import ServerConfig
from .db import (
    db_session,
    hash_password,
    hash_token,
    iso_time,
    verify_password,
)


class AdminError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class AdminIdentity:
    id: str
    username: str
    token_hash: str


def _admin_identity(row, token_hash: str) -> AdminIdentity:
    return AdminIdentity(
        id=row["id"],
        username=row["username"],
        token_hash=token_hash,
    )


def create_initial_admin(
    config: ServerConfig,
    *,
    username: str,
    password: str,
) -> AdminIdentity:
    username = username.strip()
    if not username:
        raise AdminError("管理员用户名不能为空")
    if not password:
        raise AdminError("管理员密码不能为空")
    now = iso_time()
    with db_session(config) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT COUNT(*) FROM admin_accounts").fetchone()[0]:
            raise AdminError("超级管理员已经存在", 409)
        admin_id = "admin-" + uuid.uuid4().hex[:16]
        connection.execute(
            """
            INSERT INTO admin_accounts
            (id, username, password_hash, enabled, created_at, updated_at)
            VALUES (?, ?, ?, 1, ?, ?)
            """,
            (admin_id, username, hash_password(password), now, now),
        )
        row = connection.execute(
            "SELECT id, username FROM admin_accounts WHERE id = ?",
            (admin_id,),
        ).fetchone()
        return AdminIdentity(id=row["id"], username=row["username"], token_hash="")


def reset_initial_admin_password(
    config: ServerConfig,
    *,
    username: str,
    password: str,
) -> None:
    username = username.strip()
    if not password:
        raise AdminError("管理员密码不能为空")
    now = iso_time()
    with db_session(config) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT id FROM admin_accounts WHERE username = ?",
            (username,),
        ).fetchone()
        if not row:
            raise AdminError("超级管理员不存在", 404)
        connection.execute(
            "UPDATE admin_accounts SET password_hash = ?, updated_at = ? WHERE id = ?",
            (hash_password(password), now, row["id"]),
        )
        connection.execute(
            """
            UPDATE admin_sessions
            SET revoked_at = COALESCE(revoked_at, ?)
            WHERE admin_id = ? AND revoked_at IS NULL
            """,
            (now, row["id"]),
        )


def login_admin(
    config: ServerConfig,
    *,
    username: str,
    password: str,
) -> tuple[str, AdminIdentity]:
    now = iso_time()
    with db_session(config) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT id, username, password_hash, enabled
            FROM admin_accounts
            WHERE username = ?
            """,
            (username,),
        ).fetchone()
        if not row or not row["enabled"] or not verify_password(
            password, row["password_hash"]
        ):
            raise AdminError("管理员账号或密码错误", 401)
        connection.execute(
            """
            UPDATE admin_sessions
            SET revoked_at = COALESCE(revoked_at, ?)
            WHERE admin_id = ? AND revoked_at IS NULL
            """,
            (now, row["id"]),
        )
        token = secrets.token_urlsafe(40)
        token_hash = hash_token(token)
        connection.execute(
            """
            INSERT INTO admin_sessions
            (token_hash, admin_id, created_at, last_seen_at)
            VALUES (?, ?, ?, ?)
            """,
            (token_hash, row["id"], now, now),
        )
        return token, _admin_identity(row, token_hash)


def authenticate_admin(
    config: ServerConfig,
    token: str,
    *,
    touch: bool = True,
) -> AdminIdentity:
    if not token:
        raise AdminError("缺少管理员访问凭证", 401)
    token_hash = hash_token(token)
    now = iso_time()
    with db_session(config) as connection:
        row = connection.execute(
            """
            SELECT a.id, a.username, a.enabled, s.revoked_at
            FROM admin_sessions s
            JOIN admin_accounts a ON a.id = s.admin_id
            WHERE s.token_hash = ?
            """,
            (token_hash,),
        ).fetchone()
        if not row or not row["enabled"] or row["revoked_at"] is not None:
            raise AdminError("管理员访问凭证已失效", 401)
        if touch:
            connection.execute(
                "UPDATE admin_sessions SET last_seen_at = ? WHERE token_hash = ?",
                (now, token_hash),
            )
        return _admin_identity(row, token_hash)


def logout_admin(config: ServerConfig, token_hash: str) -> None:
    with db_session(config) as connection:
        connection.execute(
            """
            UPDATE admin_sessions
            SET revoked_at = COALESCE(revoked_at, ?)
            WHERE token_hash = ?
            """,
            (iso_time(), token_hash),
        )


def _audit(
    connection: sqlite3.Connection,
    *,
    admin: AdminIdentity,
    action: str,
    target_type: str,
    target_id: str,
    details: dict[str, Any] | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO admin_audit_events
        (admin_id, action, target_type, target_id, result, details_json, created_at)
        VALUES (?, ?, ?, ?, 'success', ?, ?)
        """,
        (
            admin.id,
            action,
            target_type,
            target_id,
            json.dumps(details or {}, ensure_ascii=False, separators=(",", ":")),
            iso_time(),
        ),
    )


def list_agents(config: ServerConfig) -> list[dict[str, Any]]:
    with db_session(config) as connection:
        rows = connection.execute(
            """
            SELECT id, username, display_name, enabled, created_at
            FROM agents
            ORDER BY created_at, username
            """
        ).fetchall()
        return [
            {
                "id": row["id"],
                "username": row["username"],
                "displayName": row["display_name"],
                "enabled": bool(row["enabled"]),
                "createdAt": row["created_at"],
            }
            for row in rows
        ]


def create_agent(
    config: ServerConfig,
    admin: AdminIdentity,
    *,
    username: str,
    display_name: str,
    password: str,
) -> dict[str, Any]:
    if not password:
        raise AdminError("客服密码不能为空")
    now = iso_time()
    agent_id = "agent-" + uuid.uuid4().hex[:16]
    with db_session(config) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT id FROM agents WHERE username = ?",
            (username,),
        ).fetchone()
        if existing:
            raise AdminError("客服用户名已存在", 409)
        connection.execute(
            """
            INSERT INTO agents
            (id, username, password_hash, display_name, enabled, created_at)
            VALUES (?, ?, ?, ?, 1, ?)
            """,
            (agent_id, username, hash_password(password), display_name, now),
        )
        _audit(
            connection,
            admin=admin,
            action="agent_create",
            target_type="agent",
            target_id=agent_id,
            details={"username": username},
        )
        return {
            "id": agent_id,
            "username": username,
            "displayName": display_name,
            "enabled": True,
            "createdAt": now,
        }


def update_agent_display_name(
    config: ServerConfig,
    admin: AdminIdentity,
    *,
    agent_id: str,
    display_name: str,
) -> None:
    with db_session(config) as connection:
        connection.execute("BEGIN IMMEDIATE")
        changed = connection.execute(
            "UPDATE agents SET display_name = ? WHERE id = ?",
            (display_name, agent_id),
        ).rowcount
        if not changed:
            raise AdminError("客服不存在", 404)
        _audit(
            connection,
            admin=admin,
            action="agent_update",
            target_type="agent",
            target_id=agent_id,
            details={"displayName": display_name},
        )


def reset_agent_password(
    config: ServerConfig,
    admin: AdminIdentity,
    *,
    agent_id: str,
    password: str,
) -> None:
    if not password:
        raise AdminError("客服密码不能为空")
    now = iso_time()
    with db_session(config) as connection:
        connection.execute("BEGIN IMMEDIATE")
        changed = connection.execute(
            "UPDATE agents SET password_hash = ? WHERE id = ?",
            (hash_password(password), agent_id),
        ).rowcount
        if not changed:
            raise AdminError("客服不存在", 404)
        connection.execute(
            """
            UPDATE agent_sessions
            SET revoked_at = COALESCE(revoked_at, ?)
            WHERE agent_id = ? AND revoked_at IS NULL
            """,
            (now, agent_id),
        )
        _audit(
            connection,
            admin=admin,
            action="agent_password_reset",
            target_type="agent",
            target_id=agent_id,
        )


def set_agent_enabled(
    config: ServerConfig,
    admin: AdminIdentity,
    *,
    agent_id: str,
    enabled: bool,
) -> None:
    now = iso_time()
    with db_session(config) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT id FROM agents WHERE id = ?",
            (agent_id,),
        ).fetchone()
        if not row:
            raise AdminError("客服不存在", 404)
        if not enabled:
            busy = connection.execute(
                """
                SELECT id FROM calls
                WHERE claimed_agent_id = ?
                  AND status IN ('connecting', 'active', 'interrupted')
                LIMIT 1
                """,
                (agent_id,),
            ).fetchone()
            if busy:
                raise AdminError("客服正在处理会话，不能停用", 409)
        connection.execute(
            "UPDATE agents SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, agent_id),
        )
        if not enabled:
            connection.execute(
                """
                UPDATE agent_sessions
                SET revoked_at = COALESCE(revoked_at, ?)
                WHERE agent_id = ? AND revoked_at IS NULL
                """,
                (now, agent_id),
            )
        _audit(
            connection,
            admin=admin,
            action="agent_enable" if enabled else "agent_disable",
            target_type="agent",
            target_id=agent_id,
        )


def list_admin_clients(
    config: ServerConfig,
    *,
    status: str | None = None,
) -> list[dict[str, Any]]:
    where = ""
    params: tuple[str, ...] = ()
    if status:
        where = "WHERE c.status = ?"
        params = (status,)
    with db_session(config) as connection:
        rows = connection.execute(
            f"""
            SELECT c.*, (
                SELECT call.id FROM calls AS call
                WHERE call.caller_id = c.id
                  AND call.status IN ('created', 'waiting', 'connecting', 'active', 'interrupted')
                ORDER BY call.created_at DESC
                LIMIT 1
            ) AS active_call_id
            FROM clients AS c
            {where}
            ORDER BY
                CASE c.status
                    WHEN 'pending' THEN 0
                    WHEN 'approved' THEN 1
                    WHEN 'disabled' THEN 2
                    ELSE 3
                END,
                c.created_at
            """,
            params,
        ).fetchall()
        return [
            {
                "id": row["id"],
                "installationUuid": row["installation_uuid"],
                "displayName": row["display_name"],
                "status": row["status"],
                "availability": "busy" if row["active_call_id"] else "idle",
                "currentCallId": row["active_call_id"],
                "credentialConfigured": row["credential_hash"] is not None,
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
                "statusReason": row["status_reason"],
            }
            for row in rows
        ]


def update_client_display_name(
    config: ServerConfig,
    admin: AdminIdentity,
    *,
    client_id: str,
    display_name: str,
) -> None:
    with db_session(config) as connection:
        connection.execute("BEGIN IMMEDIATE")
        changed = connection.execute(
            "UPDATE clients SET display_name = ?, updated_at = ? WHERE id = ?",
            (display_name, iso_time(), client_id),
        ).rowcount
        if not changed:
            raise AdminError("客户端不存在", 404)
        _audit(
            connection,
            admin=admin,
            action="client_update",
            target_type="client",
            target_id=client_id,
            details={"displayName": display_name},
        )


def set_client_status(
    config: ServerConfig,
    admin: AdminIdentity,
    *,
    client_id: str,
    status: str,
    reason: str | None = None,
) -> None:
    if status not in {"approved", "rejected", "disabled"}:
        raise AdminError("不支持的客户端状态")
    now = iso_time()
    with db_session(config) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT id FROM clients WHERE id = ?",
            (client_id,),
        ).fetchone()
        if not row:
            raise AdminError("客户端不存在", 404)
        if status in {"rejected", "disabled"}:
            busy = connection.execute(
                """
                SELECT id FROM calls
                WHERE caller_id = ?
                  AND status IN ('created', 'waiting', 'connecting', 'active', 'interrupted')
                LIMIT 1
                """,
                (client_id,),
            ).fetchone()
            if busy:
                raise AdminError("客户端存在未结束会话，不能拒绝或停用", 409)
        connection.execute(
            """
            UPDATE clients
            SET status = ?, updated_at = ?, status_reason = ?,
                approved_at = CASE WHEN ? = 'approved' THEN ? ELSE NULL END,
                approved_by = CASE WHEN ? = 'approved' THEN ? ELSE NULL END,
                disabled_at = CASE WHEN ? = 'disabled' THEN ? ELSE NULL END
            WHERE id = ?
            """,
            (
                status,
                now,
                reason,
                status,
                now,
                status,
                admin.id,
                status,
                now,
                client_id,
            ),
        )
        _audit(
            connection,
            admin=admin,
            action=f"client_{status}",
            target_type="client",
            target_id=client_id,
            details={"reason": reason} if reason else None,
        )


def reset_client_credential(
    config: ServerConfig,
    admin: AdminIdentity,
    *,
    client_id: str,
) -> None:
    now = iso_time()
    with db_session(config) as connection:
        connection.execute("BEGIN IMMEDIATE")
        busy = connection.execute(
            """
            SELECT id FROM calls
            WHERE caller_id = ?
              AND status IN ('created', 'waiting', 'connecting', 'active', 'interrupted')
            LIMIT 1
            """,
            (client_id,),
        ).fetchone()
        if busy:
            raise AdminError("客户端存在未结束会话，不能重置凭证", 409)
        changed = connection.execute(
            """
            UPDATE clients
            SET credential_hash = NULL, status = 'pending', updated_at = ?,
                approved_at = NULL, approved_by = NULL, disabled_at = NULL,
                status_reason = 'credential_reset'
            WHERE id = ?
            """,
            (now, client_id),
        ).rowcount
        if not changed:
            raise AdminError("客户端不存在", 404)
        _audit(
            connection,
            admin=admin,
            action="client_credential_reset",
            target_type="client",
            target_id=client_id,
        )


def list_admin_audit(
    config: ServerConfig,
    *,
    limit: int = 200,
) -> list[dict[str, Any]]:
    with db_session(config) as connection:
        rows = connection.execute(
            """
            SELECT id, admin_id, action, target_type, target_id,
                   result, details_json, created_at
            FROM admin_audit_events
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, min(limit, 1000)),),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "adminId": row["admin_id"],
                "action": row["action"],
                "targetType": row["target_type"],
                "targetId": row["target_id"],
                "result": row["result"],
                "details": json.loads(row["details_json"]),
                "createdAt": row["created_at"],
            }
            for row in rows
        ]
