from __future__ import annotations

import hmac
import sqlite3
import uuid
from typing import Any

from .audit import insert_audit
from .auth import Principal
from .db import Database
from .errors import ConflictError, ForbiddenError, NotFoundError
from .rtc import validate_identifier
from .scenarios import get_or_create_default_scenario
from .security import hash_secret, utc_now_text


def media_state_to_dict(
    row: sqlite3.Row | None,
) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "sessionId": row["session_id"],
        "microphonePublished": bool(row["microphone_published"]),
        "cameraPublished": bool(row["camera_published"]),
        "automaticSpeechPaused": bool(row["automatic_speech_paused"]),
        "updatedAt": row["updated_at"],
    }


def robot_to_dict(
    row: sqlite3.Row,
    media_row: sqlite3.Row | None = None,
) -> dict[str, Any]:
    return {
        "id": row["id"],
        "displayName": row["display_name"],
        "scenarioId": row["scenario_id"],
        "channelId": row["channel_id"],
        "status": row["status"],
        "enabled": bool(row["enabled"]),
        "approved": bool(row["approved"]),
        "lastSeenAt": row["last_seen_at"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "media": media_state_to_dict(media_row),
    }


def register_robot(
    db: Database,
    *,
    robot_id: str,
    credential: str,
    display_name: str,
) -> dict[str, Any]:
    robot_id = validate_identifier(robot_id.strip(), "RobotId")
    clean_display_name = display_name.strip()
    if not clean_display_name:
        raise ValueError("机器人显示名称不能为空")
    if len(clean_display_name) > 128:
        raise ValueError("机器人显示名称不能超过 128 个字符")
    if len(credential) < 16 or len(credential) > 256:
        raise ValueError("机器人设备凭证长度必须在 16-256 个字符之间")

    supplied_hash = hash_secret(credential)
    created = False
    with db.transaction(immediate=True) as connection:
        row = connection.execute(
            "SELECT * FROM robots WHERE id = ?",
            (robot_id,),
        ).fetchone()
        if row is not None:
            if row["deleted_at"] is not None:
                raise ConflictError("机器人条目已删除")
            if not hmac.compare_digest(row["credential_hash"], supplied_hash):
                raise ConflictError("机器人 ID 已被其他设备占用")
        else:
            scenario = get_or_create_default_scenario(connection)
            now = utc_now_text()
            connection.execute(
                """
                INSERT INTO robots (
                    id, display_name, scenario_id, channel_id,
                    credential_hash, status, enabled, approved,
                    last_seen_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'offline', 1, 0, NULL, ?, ?)
                """,
                (
                    robot_id,
                    clean_display_name,
                    scenario["id"],
                    "rtc-" + uuid.uuid4().hex,
                    supplied_hash,
                    now,
                    now,
                ),
            )
            insert_audit(
                connection,
                actor_type="anonymous",
                actor_id=None,
                action="robot_registered",
                target_type="robot",
                target_id=robot_id,
                metadata={"displayName": clean_display_name},
            )
            created = True
            row = connection.execute(
                "SELECT * FROM robots WHERE id = ?",
                (robot_id,),
            ).fetchone()

    payload = robot_to_dict(row)
    payload["registrationState"] = (
        "pending" if not row["approved"] else "approved"
    )
    payload["created"] = created
    return payload


def get_robot(db: Database, robot_id: str) -> sqlite3.Row:
    with db.connection() as connection:
        row = connection.execute(
            """
            SELECT robots.*, scenarios.name AS scenario_name
            FROM robots
            JOIN scenarios ON scenarios.id = robots.scenario_id
            WHERE robots.id = ? AND robots.deleted_at IS NULL
            """,
            (robot_id,),
        ).fetchone()
    if row is None:
        raise NotFoundError("机器人不存在")
    return row


def list_visible_robots(
    db: Database,
    *,
    actor: Principal,
) -> list[dict[str, Any]]:
    with db.connection() as connection:
        if actor.role == "admin":
            rows = connection.execute(
                """
                SELECT robots.*, scenarios.name AS scenario_name,
                       robot_media_state.session_id,
                       robot_media_state.microphone_published,
                       robot_media_state.camera_published,
                       robot_media_state.automatic_speech_paused,
                       robot_media_state.updated_at AS media_updated_at
                FROM robots
                JOIN scenarios ON scenarios.id = robots.scenario_id
                LEFT JOIN robot_media_state
                  ON robot_media_state.robot_id = robots.id
                WHERE robots.deleted_at IS NULL
                ORDER BY scenarios.name, robots.display_name
                """
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT robots.*, scenarios.name AS scenario_name,
                       robot_media_state.session_id,
                       robot_media_state.microphone_published,
                       robot_media_state.camera_published,
                       robot_media_state.automatic_speech_paused,
                       robot_media_state.updated_at AS media_updated_at
                FROM robots
                JOIN scenarios ON scenarios.id = robots.scenario_id
                JOIN agent_scenarios
                  ON agent_scenarios.scenario_id = scenarios.id
                LEFT JOIN robot_media_state
                  ON robot_media_state.robot_id = robots.id
                WHERE agent_scenarios.agent_id = ?
                  AND scenarios.enabled = 1
                  AND robots.approved = 1
                  AND robots.enabled = 1
                  AND robots.deleted_at IS NULL
                ORDER BY scenarios.name, robots.display_name
                """,
                (actor.id,),
            ).fetchall()
    result = []
    for row in rows:
        media = None
        if row["session_id"] is not None:
            media = row
        result.append(robot_to_dict(row, media))
    return result


def ensure_robot_access(
    connection: sqlite3.Connection,
    *,
    actor: Principal,
    robot_id: str,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT robots.*, scenarios.name AS scenario_name
        FROM robots
        JOIN scenarios ON scenarios.id = robots.scenario_id
        WHERE robots.id = ? AND robots.deleted_at IS NULL
        """,
        (robot_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError("机器人不存在")
    if actor.role == "admin":
        return row
    access = connection.execute(
        """
        SELECT 1
        FROM agent_scenarios
        WHERE agent_id = ? AND scenario_id = ?
        """,
        (actor.id, row["scenario_id"]),
    ).fetchone()
    if access is None:
        raise ForbiddenError("无权访问该机器人")
    return row


def robot_self_payload(
    connection: sqlite3.Connection,
    robot_id: str,
) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT robots.*, scenarios.name AS scenario_name
        FROM robots
        JOIN scenarios ON scenarios.id = robots.scenario_id
        WHERE robots.id = ? AND robots.deleted_at IS NULL
        """,
        (robot_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError("机器人不存在")
    media = connection.execute(
        "SELECT * FROM robot_media_state WHERE robot_id = ?",
        (robot_id,),
    ).fetchone()
    current = connection.execute(
        """
        SELECT * FROM robot_sessions
        WHERE robot_id = ? AND status IN ('active', 'agent_takeover')
        ORDER BY started_at DESC
        LIMIT 1
        """,
        (robot_id,),
    ).fetchone()
    lease = connection.execute(
        """
        SELECT takeover_leases.*, agents.display_name AS agent_display_name
        FROM takeover_leases
        JOIN agents ON agents.id = takeover_leases.agent_id
        WHERE takeover_leases.robot_id = ?
          AND takeover_leases.status = 'active'
        LIMIT 1
        """,
        (robot_id,),
    ).fetchone()
    microphone_requested = False
    if current is not None:
        microphone_requested = (
            connection.execute(
                """
                SELECT 1 FROM session_listeners
                WHERE session_id = ? AND enabled = 1
                LIMIT 1
                """,
                (current["id"],),
            ).fetchone()
            is not None
        )
    return {
        "robot": robot_to_dict(row, media),
        "scenarioName": row["scenario_name"],
        "currentSession": dict(current) if current else None,
        "activeLease": dict(lease) if lease else None,
        "robotMicrophoneRequested": microphone_requested,
    }
