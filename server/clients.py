from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from typing import Any

from .config import ServerConfig
from .db import db_session, hash_token, iso_time


CLIENT_STATUSES = ("pending", "approved", "rejected", "disabled")
ACTIVE_CALL_STATUSES = ("created", "waiting", "connecting", "active", "interrupted")


class ClientError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class ClientIdentity:
    id: str
    installation_uuid: str
    display_name: str
    status: str
    credential_hash: str


def client_to_dict(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "displayName": row["display_name"],
        "status": row["status"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "approvedAt": row["approved_at"],
        "disabledAt": row["disabled_at"],
        "statusReason": row["status_reason"],
    }


def _identity(row) -> ClientIdentity:
    return ClientIdentity(
        id=row["id"],
        installation_uuid=row["installation_uuid"],
        display_name=row["display_name"],
        status=row["status"],
        credential_hash=row["credential_hash"] or "",
    )


def register_client(
    config: ServerConfig,
    *,
    installation_uuid: str,
    display_name: str,
    credential: str | None,
) -> tuple[dict[str, Any], str | None]:
    now = iso_time()
    with db_session(config) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM clients WHERE installation_uuid = ?",
            (installation_uuid,),
        ).fetchone()
        if not row:
            client_id = "client-" + uuid.uuid4().hex[:16]
            issued_credential = secrets.token_urlsafe(36)
            connection.execute(
                """
                INSERT INTO clients
                (id, installation_uuid, display_name, status, credential_hash,
                 created_at, updated_at)
                VALUES (?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    client_id,
                    installation_uuid,
                    display_name,
                    hash_token(issued_credential),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM clients WHERE id = ?",
                (client_id,),
            ).fetchone()
            return client_to_dict(row), issued_credential

        if row["credential_hash"] is None:
            if row["status"] != "pending":
                raise ClientError("客户端凭证已失效，请联系管理员重新审批", 403)
            issued_credential = secrets.token_urlsafe(36)
            connection.execute(
                """
                UPDATE clients
                SET credential_hash = ?, updated_at = ?, status_reason = NULL
                WHERE id = ?
                """,
                (
                    hash_token(issued_credential),
                    now,
                    row["id"],
                ),
            )
            row = connection.execute(
                "SELECT * FROM clients WHERE id = ?",
                (row["id"],),
            ).fetchone()
            return client_to_dict(row), issued_credential

        if not credential or not secrets.compare_digest(
            row["credential_hash"], hash_token(credential)
        ):
            raise ClientError("客户端设备凭证无效", 403)

        return client_to_dict(row), None


def authenticate_client(
    config: ServerConfig,
    *,
    installation_uuid: str,
    credential: str,
) -> ClientIdentity:
    if not installation_uuid or not credential:
        raise ClientError("缺少客户端设备凭证", 401)
    with db_session(config) as connection:
        row = connection.execute(
            """
            SELECT *
            FROM clients
            WHERE installation_uuid = ?
              AND credential_hash = ?
            """,
            (installation_uuid, hash_token(credential)),
        ).fetchone()
        if not row:
            raise ClientError("客户端设备凭证无效", 403)
        return _identity(row)


def require_approved_client(client: ClientIdentity) -> ClientIdentity:
    if client.status != "approved":
        raise ClientError("客户端尚未通过管理员审批", 403)
    return client


def request_client_reregistration(
    config: ServerConfig,
    client: ClientIdentity,
) -> dict[str, Any]:
    now = iso_time()
    with db_session(config) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM clients WHERE id = ?",
            (client.id,),
        ).fetchone()
        if not row:
            raise ClientError("客户端不存在", 404)
        if row["status"] == "pending":
            return client_to_dict(row)
        if row["status"] == "approved":
            raise ClientError("客户端已经通过审批，无需重新注册", 409)
        connection.execute(
            """
            UPDATE clients
            SET status = 'pending', updated_at = ?, approved_at = NULL,
                approved_by = NULL, disabled_at = NULL,
                status_reason = 'client_reregister'
            WHERE id = ?
            """,
            (now, client.id),
        )
        return client_to_dict(
            connection.execute(
                "SELECT * FROM clients WHERE id = ?",
                (client.id,),
            ).fetchone()
        )


def client_self_payload(config: ServerConfig, client: ClientIdentity) -> dict[str, Any]:
    with db_session(config) as connection:
        row = connection.execute(
            "SELECT * FROM clients WHERE id = ?",
            (client.id,),
        ).fetchone()
        if not row:
            raise ClientError("客户端不存在", 404)
        active_call = connection.execute(
            """
            SELECT id, status, room_id
            FROM calls
            WHERE caller_id = ?
              AND status IN ('created', 'waiting', 'connecting', 'active', 'interrupted')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (client.id,),
        ).fetchone()
        payload = client_to_dict(row)
        payload["currentCall"] = (
            {
                "id": active_call["id"],
                "status": active_call["status"],
                "roomId": active_call["room_id"],
            }
            if active_call
            else None
        )
        return payload


def list_approved_client_directory(config: ServerConfig) -> list[dict[str, Any]]:
    with db_session(config) as connection:
        clients = connection.execute(
            """
            SELECT id, display_name
            FROM clients
            WHERE status = 'approved'
            ORDER BY display_name COLLATE NOCASE, id
            """
        ).fetchall()
        active_calls = connection.execute(
            """
            SELECT id, caller_id, status, room_id
            FROM calls
            WHERE status IN ('created', 'waiting', 'connecting', 'active', 'interrupted')
            ORDER BY created_at DESC
            """
        ).fetchall()
    by_client = {row["caller_id"]: row for row in active_calls}
    result = []
    for client in clients:
        call = by_client.get(client["id"])
        result.append(
            {
                "id": client["id"],
                "displayName": client["display_name"],
                "availability": "busy" if call else "idle",
                "currentCall": (
                    {
                        "id": call["id"],
                        "status": call["status"],
                        "roomId": call["room_id"],
                    }
                    if call
                    else None
                ),
            }
        )
    result.sort(
        key=lambda item: (
            item["availability"] != "idle",
            item["displayName"].casefold(),
            item["id"],
        )
    )
    return result
