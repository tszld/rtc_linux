from __future__ import annotations

import hmac
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import timedelta

from .audit import insert_audit
from .db import Database
from .security import (
    hash_password,
    hash_secret,
    isoformat,
    new_secret,
    parse_timestamp,
    utc_now,
    utc_now_text,
    verify_password,
)


class AuthError(RuntimeError):
    def __init__(self, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class Principal:
    principal_type: str
    id: str
    role: str
    display_name: str

    def to_public_dict(self) -> dict[str, str]:
        return {
            "principalType": self.principal_type,
            "id": self.id,
            "role": self.role,
            "displayName": self.display_name,
        }


def _issue_token(
    connection: sqlite3.Connection,
    *,
    principal: Principal,
    ttl_seconds: int,
) -> dict[str, str]:
    raw_token = new_secret()
    expires_at = utc_now() + timedelta(seconds=ttl_seconds)
    created_at = utc_now_text()
    connection.execute(
        """
        INSERT INTO access_tokens (
            id, principal_type, principal_id, token_hash,
            expires_at, revoked_at, created_at
        ) VALUES (?, ?, ?, ?, ?, NULL, ?)
        """,
        (
            str(uuid.uuid4()),
            principal.principal_type,
            principal.id,
            hash_secret(raw_token),
            isoformat(expires_at),
            created_at,
        ),
    )
    return {
        "accessToken": raw_token,
        "tokenType": "Bearer",
        "expiresAt": isoformat(expires_at),
    }


def authenticate_agent(
    db: Database,
    *,
    username: str,
    password: str,
    ttl_seconds: int,
    require_admin: bool = False,
) -> dict[str, object]:
    with db.connection() as connection:
        row = connection.execute(
            "SELECT * FROM agents WHERE username = ?",
            (username.strip(),),
        ).fetchone()

    if row is None or not verify_password(password, row["password_hash"]):
        with db.transaction() as connection:
            insert_audit(
                connection,
                actor_type="anonymous",
                actor_id=None,
                action="login_failed",
                target_type="agent",
                target_id=row["id"] if row else None,
                metadata={"username": username.strip()},
            )
        raise AuthError("用户名或密码错误")
    if not row["enabled"]:
        raise AuthError("账号已停用", 403)
    if require_admin and row["role"] != "admin":
        raise AuthError("需要管理员权限", 403)

    principal = Principal(
        principal_type="agent",
        id=row["id"],
        role=row["role"],
        display_name=row["display_name"],
    )
    with db.transaction() as connection:
        token = _issue_token(
            connection,
            principal=principal,
            ttl_seconds=ttl_seconds,
        )
        insert_audit(
            connection,
            actor_type="agent",
            actor_id=principal.id,
            action="login",
            target_type="agent",
            target_id=principal.id,
        )
    return {
        **token,
        "principal": principal.to_public_dict(),
    }


def authenticate_robot(
    db: Database,
    *,
    robot_id: str,
    credential: str,
    ttl_seconds: int,
) -> dict[str, object]:
    with db.connection() as connection:
        row = connection.execute(
            "SELECT * FROM robots WHERE id = ?",
            (robot_id,),
        ).fetchone()

    supplied_hash = hash_secret(credential)
    if row is not None and row["deleted_at"] is not None:
        raise AuthError("机器人条目已删除")
    if row is None or not hmac.compare_digest(
        row["credential_hash"],
        supplied_hash,
    ):
        with db.transaction() as connection:
            insert_audit(
                connection,
                actor_type="anonymous",
                actor_id=None,
                action="robot_login_failed",
                target_type="robot",
                target_id=robot_id,
            )
        raise AuthError("机器人凭证无效")
    if not row["approved"]:
        raise AuthError("机器人尚未审批", 403)
    if not row["enabled"]:
        raise AuthError("机器人已停用", 403)

    principal = Principal(
        principal_type="robot",
        id=row["id"],
        role="robot",
        display_name=row["display_name"],
    )
    with db.transaction() as connection:
        token = _issue_token(
            connection,
            principal=principal,
            ttl_seconds=ttl_seconds,
        )
        insert_audit(
            connection,
            actor_type="robot",
            actor_id=principal.id,
            action="login",
            target_type="robot",
            target_id=principal.id,
        )
    return {
        **token,
        "principal": principal.to_public_dict(),
        "robot": {
            "id": row["id"],
            "displayName": row["display_name"],
            "scenarioId": row["scenario_id"],
            "channelId": row["channel_id"],
        },
    }


def _principal_for_token_row(
    connection: sqlite3.Connection,
    *,
    principal_type: str,
    principal_id: str,
) -> Principal:
    if principal_type == "agent":
        row = connection.execute(
            "SELECT * FROM agents WHERE id = ? AND enabled = 1",
            (principal_id,),
        ).fetchone()
        if row is None:
            raise AuthError("账号已停用或不存在")
        return Principal(
            principal_type="agent",
            id=row["id"],
            role=row["role"],
            display_name=row["display_name"],
        )
    if principal_type == "robot":
        row = connection.execute(
            """
            SELECT * FROM robots
            WHERE id = ? AND enabled = 1 AND approved = 1
              AND deleted_at IS NULL
            """,
            (principal_id,),
        ).fetchone()
        if row is None:
            raise AuthError("机器人已停用、未审批或不存在")
        return Principal(
            principal_type="robot",
            id=row["id"],
            role="robot",
            display_name=row["display_name"],
        )
    raise AuthError("未知身份类型")


def resolve_access_token(db: Database, raw_token: str) -> Principal:
    if not raw_token:
        raise AuthError("缺少访问 Token")
    token_hash = hash_secret(raw_token)
    with db.connection() as connection:
        row = connection.execute(
            "SELECT * FROM access_tokens WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if row is None or row["revoked_at"] is not None:
            raise AuthError("访问 Token 无效")
        if parse_timestamp(row["expires_at"]) <= utc_now():
            raise AuthError("访问 Token 已过期")
        return _principal_for_token_row(
            connection,
            principal_type=row["principal_type"],
            principal_id=row["principal_id"],
        )


def revoke_access_token(db: Database, raw_token: str) -> None:
    with db.transaction() as connection:
        connection.execute(
            """
            UPDATE access_tokens
            SET revoked_at = COALESCE(revoked_at, ?)
            WHERE token_hash = ?
            """,
            (utc_now_text(), hash_secret(raw_token)),
        )


def revoke_principal_tokens(
    connection: sqlite3.Connection,
    *,
    principal_type: str,
    principal_id: str,
) -> None:
    connection.execute(
        """
        UPDATE access_tokens
        SET revoked_at = COALESCE(revoked_at, ?)
        WHERE principal_type = ? AND principal_id = ? AND revoked_at IS NULL
        """,
        (utc_now_text(), principal_type, principal_id),
    )


def issue_ws_ticket(
    db: Database,
    *,
    principal: Principal,
    ttl_seconds: int,
) -> dict[str, str]:
    raw_ticket = new_secret()
    expires_at = utc_now() + timedelta(seconds=ttl_seconds)
    with db.transaction() as connection:
        connection.execute(
            """
            INSERT INTO ws_tickets (
                id, principal_type, principal_id, ticket_hash,
                expires_at, consumed_at, created_at
            ) VALUES (?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                str(uuid.uuid4()),
                principal.principal_type,
                principal.id,
                hash_secret(raw_ticket),
                isoformat(expires_at),
                utc_now_text(),
            ),
        )
    return {
        "ticket": raw_ticket,
        "expiresAt": isoformat(expires_at),
    }


def consume_ws_ticket(db: Database, raw_ticket: str) -> Principal:
    if not raw_ticket:
        raise AuthError("缺少 WebSocket 票据")
    ticket_hash = hash_secret(raw_ticket)
    with db.transaction(immediate=True) as connection:
        row = connection.execute(
            "SELECT * FROM ws_tickets WHERE ticket_hash = ?",
            (ticket_hash,),
        ).fetchone()
        if row is None or row["consumed_at"] is not None:
            raise AuthError("WebSocket 票据无效或已使用")
        if parse_timestamp(row["expires_at"]) <= utc_now():
            raise AuthError("WebSocket 票据已过期")
        consumed_at = utc_now_text()
        connection.execute(
            "UPDATE ws_tickets SET consumed_at = ? WHERE id = ?",
            (consumed_at, row["id"]),
        )
        return _principal_for_token_row(
            connection,
            principal_type=row["principal_type"],
            principal_id=row["principal_id"],
        )


def create_or_update_admin(
    db: Database,
    *,
    username: str,
    password: str,
    display_name: str | None = None,
    reset_existing: bool = False,
) -> Principal:
    username = username.strip()
    if not username:
        raise ValueError("管理员用户名不能为空")
    if len(password) < 8:
        raise ValueError("管理员密码至少 8 个字符")
    now = utc_now_text()
    with db.transaction(immediate=True) as connection:
        existing = connection.execute(
            "SELECT * FROM agents WHERE username = ?",
            (username,),
        ).fetchone()
        if existing is not None:
            if not reset_existing:
                raise ValueError("用户名已存在")
            connection.execute(
                """
                UPDATE agents
                SET password_hash = ?, role = 'admin', enabled = 1, updated_at = ?
                WHERE id = ?
                """,
                (hash_password(password), now, existing["id"]),
            )
            revoke_principal_tokens(
                connection,
                principal_type="agent",
                principal_id=existing["id"],
            )
            principal = Principal(
                principal_type="agent",
                id=existing["id"],
                role="admin",
                display_name=existing["display_name"],
            )
        else:
            principal = Principal(
                principal_type="agent",
                id=str(uuid.uuid4()),
                role="admin",
                display_name=display_name or username,
            )
            connection.execute(
                """
                INSERT INTO agents (
                    id, username, password_hash, display_name,
                    role, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'admin', 1, ?, ?)
                """,
                (
                    principal.id,
                    username,
                    hash_password(password),
                    principal.display_name,
                    now,
                    now,
                ),
            )
        insert_audit(
            connection,
            actor_type="system",
            actor_id=None,
            action="admin_password_reset" if existing else "admin_created",
            target_type="agent",
            target_id=principal.id,
        )
    return principal
