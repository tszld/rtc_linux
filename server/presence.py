from __future__ import annotations

from datetime import timedelta
from typing import Any

from .audit import insert_audit
from .config import ServerConfig
from .db import Database
from .errors import ForbiddenError, NotFoundError
from .security import parse_timestamp, utc_now, utc_now_text


def heartbeat(db: Database, *, robot_id: str) -> dict[str, Any]:
    now = utc_now_text()
    with db.transaction(immediate=True) as connection:
        robot = connection.execute(
            "SELECT * FROM robots WHERE id = ?",
            (robot_id,),
        ).fetchone()
        if robot is None:
            raise NotFoundError("机器人不存在")
        if not robot["approved"] or not robot["enabled"]:
            raise ForbiddenError("机器人未审批或已停用")
        old_status = robot["status"]
        connection.execute(
            """
            UPDATE robots
            SET status = 'online', last_seen_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, now, robot_id),
        )
        if old_status != "online":
            insert_audit(
                connection,
                actor_type="robot",
                actor_id=robot_id,
                action="robot_online",
                target_type="robot",
                target_id=robot_id,
                metadata={"previousStatus": old_status},
            )
        updated = connection.execute(
            "SELECT * FROM robots WHERE id = ?",
            (robot_id,),
        ).fetchone()
    return {
        "robotId": updated["id"],
        "status": updated["status"],
        "previousStatus": old_status,
        "lastSeenAt": updated["last_seen_at"],
        "presenceTimeoutSeconds": None,
    }


def sweep_presence(
    db: Database,
    *,
    config: ServerConfig,
) -> list[dict[str, str]]:
    now = utc_now()
    degraded_before = utc_now() - timedelta(
        seconds=config.presence_degraded_seconds
    )
    offline_before = utc_now() - timedelta(seconds=config.presence_timeout_seconds)
    events: list[dict[str, str]] = []
    with db.transaction(immediate=True) as connection:
        rows = connection.execute(
            """
            SELECT * FROM robots
            WHERE approved = 1 AND enabled = 1 AND status != 'offline'
            """
        ).fetchall()
        for row in rows:
            last_seen = row["last_seen_at"]
            if last_seen is None:
                new_status = "offline"
                previous = row["status"]
            else:
                seen_at = parse_timestamp(last_seen)
                if seen_at <= offline_before:
                    new_status = "offline"
                elif seen_at <= degraded_before:
                    new_status = "degraded"
                else:
                    new_status = "online"
                previous = row["status"]
            if new_status == previous:
                continue
            timestamp = utc_now_text()
            connection.execute(
                "UPDATE robots SET status = ?, updated_at = ? WHERE id = ?",
                (new_status, timestamp, row["id"]),
            )
            insert_audit(
                connection,
                actor_type="system",
                actor_id=None,
                action="robot_status_changed",
                target_type="robot",
                target_id=row["id"],
                metadata={"from": previous, "to": new_status},
            )
            events.append(
                {
                    "robotId": row["id"],
                    "from": previous,
                    "to": new_status,
                    "lastSeenAt": last_seen or "",
                    "occurredAt": timestamp,
                }
            )
    return events


def set_media_state(
    db: Database,
    *,
    robot_id: str,
    session_id: str | None,
    microphone_published: bool,
    camera_published: bool,
    automatic_speech_paused: bool,
) -> dict[str, Any]:
    now = utc_now_text()
    with db.transaction(immediate=True) as connection:
        if session_id is not None:
            session = connection.execute(
                """
                SELECT * FROM robot_sessions
                WHERE id = ? AND robot_id = ?
                  AND status IN ('active', 'agent_takeover')
                """,
                (session_id, robot_id),
            ).fetchone()
            if session is None:
                raise NotFoundError("当前业务会话不存在")
        connection.execute(
            """
            INSERT INTO robot_media_state (
                robot_id, session_id, microphone_published,
                camera_published, automatic_speech_paused, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(robot_id) DO UPDATE SET
                session_id = excluded.session_id,
                microphone_published = excluded.microphone_published,
                camera_published = excluded.camera_published,
                automatic_speech_paused = excluded.automatic_speech_paused,
                updated_at = excluded.updated_at
            """,
            (
                robot_id,
                session_id,
                int(microphone_published),
                int(camera_published),
                int(automatic_speech_paused),
                now,
            ),
        )
        insert_audit(
            connection,
            actor_type="robot",
            actor_id=robot_id,
            action="media_state_changed",
            target_type="robot",
            target_id=robot_id,
            metadata={
                "sessionId": session_id,
                "microphonePublished": microphone_published,
                "cameraPublished": camera_published,
                "automaticSpeechPaused": automatic_speech_paused,
            },
        )
        row = connection.execute(
            "SELECT * FROM robot_media_state WHERE robot_id = ?",
            (robot_id,),
        ).fetchone()
    return {
        "robotId": robot_id,
        "sessionId": row["session_id"],
        "microphonePublished": bool(row["microphone_published"]),
        "cameraPublished": bool(row["camera_published"]),
        "automaticSpeechPaused": bool(row["automatic_speech_paused"]),
        "updatedAt": row["updated_at"],
    }


def mark_robot_offline(
    db: Database,
    *,
    robot_id: str,
    reason: str,
) -> dict[str, str] | None:
    now = utc_now_text()
    with db.transaction(immediate=True) as connection:
        robot = connection.execute(
            "SELECT * FROM robots WHERE id = ?",
            (robot_id,),
        ).fetchone()
        if robot is None:
            raise NotFoundError("机器人不存在")
        if robot["status"] == "offline":
            return None
        connection.execute(
            """
            UPDATE robots
            SET status = 'offline', updated_at = ?
            WHERE id = ?
            """,
            (now, robot_id),
        )
        insert_audit(
            connection,
            actor_type="robot",
            actor_id=robot_id,
            action="robot_offline",
            target_type="robot",
            target_id=robot_id,
            metadata={"reason": reason},
        )
    return {
        "robotId": robot_id,
        "from": robot["status"],
        "to": "offline",
        "occurredAt": now,
    }
