from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import timedelta

from .config import ServerConfig
from .db import (
    db_session,
    hash_token,
    iso_time,
    utc_now,
    verify_password,
)


class AuthError(Exception):
    def __init__(self, message: str, status_code: int = 401):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


NO_EXPIRY_AT = "9999-12-31T23:59:59+00:00"


@dataclass(frozen=True)
class AuthenticatedAgent:
    id: str
    username: str
    display_name: str
    token_hash: str


def login_agent(
    config: ServerConfig, username: str, password: str
) -> tuple[str, AuthenticatedAgent, str]:
    now = utc_now()
    now_text = iso_time(now)
    stale_cutoff = now - timedelta(seconds=config.agent_presence_ttl_seconds)

    with db_session(config) as connection:
        # 登录检查与写入必须处于同一个写事务中，避免两个窗口同时通过重复登录检查。
        connection.execute("BEGIN IMMEDIATE")
        agent = connection.execute(
            """
            SELECT id, username, password_hash, display_name, enabled
            FROM agents
            WHERE username = ?
            """,
            (username,),
        ).fetchone()
        if not agent or not agent["enabled"]:
            raise AuthError("账号或密码错误")
        if not verify_password(password, agent["password_hash"]):
            raise AuthError("账号或密码错误")

        active = connection.execute(
            """
            SELECT token_hash
            FROM agent_sessions
            WHERE agent_id = ?
              AND revoked_at IS NULL
              AND last_seen_at > ?
            LIMIT 1
            """,
            (agent["id"], iso_time(stale_cutoff)),
        ).fetchone()
        if active:
            raise AuthError("该客服账号已在其他窗口登录", 409)

        connection.execute(
            """
            UPDATE agent_sessions
            SET revoked_at = COALESCE(revoked_at, ?)
            WHERE agent_id = ?
              AND revoked_at IS NULL
            """,
            (now_text, agent["id"]),
        )

        access_token = secrets.token_urlsafe(40)
        token_hash = hash_token(access_token)
        connection.execute(
            """
            INSERT INTO agent_sessions
            (token_hash, agent_id, created_at, last_seen_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (token_hash, agent["id"], now_text, now_text, NO_EXPIRY_AT),
        )
        authenticated = AuthenticatedAgent(
            id=agent["id"],
            username=agent["username"],
            display_name=agent["display_name"],
            token_hash=token_hash,
        )
        return access_token, authenticated, NO_EXPIRY_AT


def authenticate_access_token(
    config: ServerConfig, access_token: str, *, touch: bool = True
) -> AuthenticatedAgent:
    if not access_token:
        raise AuthError("缺少访问凭证")
    token_hash = hash_token(access_token)
    now_text = iso_time()
    with db_session(config) as connection:
        row = connection.execute(
            """
            SELECT a.id, a.username, a.display_name, a.enabled,
                   s.token_hash, s.revoked_at
            FROM agent_sessions s
            JOIN agents a ON a.id = s.agent_id
            WHERE s.token_hash = ?
            """,
            (token_hash,),
        ).fetchone()
        if not row or not row["enabled"] or row["revoked_at"] is not None:
            raise AuthError("访问凭证已失效")
        if touch:
            connection.execute(
                "UPDATE agent_sessions SET last_seen_at = ? WHERE token_hash = ?",
                (now_text, token_hash),
            )
        return AuthenticatedAgent(
            id=row["id"],
            username=row["username"],
            display_name=row["display_name"],
            token_hash=row["token_hash"],
        )


def logout_agent(config: ServerConfig, token_hash: str) -> None:
    with db_session(config) as connection:
        connection.execute(
            """
            UPDATE agent_sessions
            SET revoked_at = COALESCE(revoked_at, ?)
            WHERE token_hash = ?
            """,
            (iso_time(), token_hash),
        )


def revoke_agent_tokens(config: ServerConfig, agent_id: str) -> None:
    with db_session(config) as connection:
        connection.execute(
            """
            UPDATE agent_sessions
            SET revoked_at = COALESCE(revoked_at, ?)
            WHERE agent_id = ? AND revoked_at IS NULL
            """,
            (iso_time(), agent_id),
        )
