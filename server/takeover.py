from __future__ import annotations

import sqlite3
import uuid
from datetime import timedelta
from typing import Any

from .audit import insert_audit
from .auth import Principal
from .db import Database
from .errors import ConflictError, ForbiddenError, NotFoundError
from .scenarios import ensure_scenario_access
from .security import isoformat, parse_timestamp, utc_now, utc_now_text
from .sessions import ensure_session_claim, get_session_row, session_to_dict


def lease_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = {
        "id": row["id"],
        "robotId": row["robot_id"],
        "sessionId": row["session_id"],
        "agentId": row["agent_id"],
        "status": row["status"],
        "microphoneEnabled": bool(row["microphone_published"]),
        "acquiredAt": row["acquired_at"],
        "expiresAt": row["expires_at"],
        "releasedAt": row["released_at"],
        "endReason": row["end_reason"],
    }
    if "agent_display_name" in row.keys():
        result["agentDisplayName"] = row["agent_display_name"]
    return result


def _active_lease(
    connection: sqlite3.Connection,
    *,
    robot_id: str,
) -> sqlite3.Row | None:
    return connection.execute(
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


def _expire_due_locked(
    connection: sqlite3.Connection,
    *,
    now_text: str,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT takeover_leases.*, agents.display_name AS agent_display_name
        FROM takeover_leases
        JOIN agents ON agents.id = takeover_leases.agent_id
        WHERE takeover_leases.status = 'active'
          AND takeover_leases.expires_at <= ?
        """,
        (now_text,),
    ).fetchall()
    events = []
    for row in rows:
        connection.execute(
            """
            UPDATE takeover_leases
            SET status = 'expired', microphone_published = 0,
                released_at = ?, end_reason = 'lease_expired'
            WHERE id = ?
            """,
            (now_text, row["id"]),
        )
        connection.execute(
            """
            UPDATE robot_sessions
            SET status = 'active'
            WHERE id = ? AND status = 'agent_takeover'
            """,
            (row["session_id"],),
        )
        insert_audit(
            connection,
            actor_type="system",
            actor_id=None,
            action="takeover_expired",
            target_type="takeover_lease",
            target_id=row["id"],
            metadata={
                "robotId": row["robot_id"],
                "sessionId": row["session_id"],
                "agentId": row["agent_id"],
            },
        )
        expired = dict(row)
        expired["status"] = "expired"
        expired["microphone_published"] = 0
        expired["released_at"] = now_text
        expired["end_reason"] = "lease_expired"
        events.append(lease_to_dict(expired))
    return events


def expire_leases(db: Database) -> list[dict[str, Any]]:
    with db.transaction(immediate=True) as connection:
        return _expire_due_locked(
            connection,
            now_text=utc_now_text(),
        )


def acquire_lease(
    db: Database,
    *,
    actor: Principal,
    session_id: str,
    requested_seconds: int,
    max_seconds: int,
) -> dict[str, Any]:
    if requested_seconds <= 0 or requested_seconds > max_seconds:
        raise ValueError(f"租约时长必须在 1-{max_seconds} 秒之间")
    now = utc_now()
    now_text = isoformat(now)
    lease_id = str(uuid.uuid4())
    expires_at = isoformat(now + timedelta(seconds=requested_seconds))
    expired_events: list[dict[str, Any]]
    with db.transaction(immediate=True) as connection:
        expired_events = _expire_due_locked(connection, now_text=now_text)
        session = get_session_row(connection, session_id)
        ensure_scenario_access(
            connection,
            actor=actor,
            scenario_id=session["scenario_id"],
        )
        ensure_session_claim(
            connection,
            actor=actor,
            session=session,
        )
        robot = connection.execute(
            "SELECT * FROM robots WHERE id = ?",
            (session["robot_id"],),
        ).fetchone()
        if robot["status"] != "online":
            raise ConflictError("机器人当前不在线，不能人工接管")
        if session["status"] == "ended":
            raise ConflictError("业务会话已经结束")
        active = _active_lease(connection, robot_id=session["robot_id"])
        if active is not None:
            if active["agent_id"] == actor.id:
                raise ConflictError("当前客服已经持有接管租约")
            raise ConflictError("机器人已被其他客服人工接管")
        if session["status"] != "active":
            raise ConflictError("当前业务会话不能申请人工接管")
        connection.execute(
            """
            INSERT INTO takeover_leases (
                id, robot_id, session_id, agent_id, status,
                acquired_at, expires_at, released_at, end_reason
            ) VALUES (?, ?, ?, ?, 'active', ?, ?, NULL, NULL)
            """,
            (
                lease_id,
                session["robot_id"],
                session_id,
                actor.id,
                now_text,
                expires_at,
            ),
        )
        connection.execute(
            """
            UPDATE robot_sessions
            SET status = 'agent_takeover'
            WHERE id = ? AND status = 'active'
            """,
            (session_id,),
        )
        insert_audit(
            connection,
            actor_type="agent",
            actor_id=actor.id,
            action="takeover_acquired",
            target_type="takeover_lease",
            target_id=lease_id,
            metadata={
                "robotId": session["robot_id"],
                "sessionId": session_id,
                "expiresAt": expires_at,
            },
        )
        lease = connection.execute(
            """
            SELECT takeover_leases.*, agents.display_name AS agent_display_name
            FROM takeover_leases
            JOIN agents ON agents.id = takeover_leases.agent_id
            WHERE takeover_leases.id = ?
            """,
            (lease_id,),
        ).fetchone()
    return {
        "lease": lease_to_dict(lease),
        "expiredLeases": expired_events,
    }


def renew_lease(
    db: Database,
    *,
    actor: Principal,
    session_id: str,
    requested_seconds: int,
    max_seconds: int,
) -> dict[str, Any]:
    if requested_seconds <= 0 or requested_seconds > max_seconds:
        raise ValueError(f"租约时长必须在 1-{max_seconds} 秒之间")
    now = utc_now()
    now_text = isoformat(now)
    expires_at = isoformat(now + timedelta(seconds=requested_seconds))
    expired_events: list[dict[str, Any]]
    with db.transaction(immediate=True) as connection:
        expired_events = _expire_due_locked(connection, now_text=now_text)
        session = get_session_row(connection, session_id)
        ensure_scenario_access(
            connection,
            actor=actor,
            scenario_id=session["scenario_id"],
        )
        lease = connection.execute(
            """
            SELECT takeover_leases.*, agents.display_name AS agent_display_name
            FROM takeover_leases
            JOIN agents ON agents.id = takeover_leases.agent_id
            WHERE takeover_leases.session_id = ?
              AND takeover_leases.agent_id = ?
              AND takeover_leases.status = 'active'
            LIMIT 1
            """,
            (session_id, actor.id),
        ).fetchone()
        if lease is None:
            raise ConflictError("没有可续租的有效接管租约")
        connection.execute(
            "UPDATE takeover_leases SET expires_at = ? WHERE id = ?",
            (expires_at, lease["id"]),
        )
        insert_audit(
            connection,
            actor_type="agent",
            actor_id=actor.id,
            action="takeover_renewed",
            target_type="takeover_lease",
            target_id=lease["id"],
            metadata={"expiresAt": expires_at},
        )
        updated = connection.execute(
            """
            SELECT takeover_leases.*, agents.display_name AS agent_display_name
            FROM takeover_leases
            JOIN agents ON agents.id = takeover_leases.agent_id
            WHERE takeover_leases.id = ?
            """,
            (lease["id"],),
        ).fetchone()
    return {
        "lease": lease_to_dict(updated),
        "expiredLeases": expired_events,
    }


def set_lease_microphone(
    db: Database,
    *,
    actor: Principal,
    session_id: str,
    enabled: bool,
) -> dict[str, Any]:
    now_text = utc_now_text()
    with db.transaction(immediate=True) as connection:
        expired_events = _expire_due_locked(connection, now_text=now_text)
        session = get_session_row(connection, session_id)
        ensure_scenario_access(
            connection,
            actor=actor,
            scenario_id=session["scenario_id"],
        )
        lease = connection.execute(
            """
            SELECT takeover_leases.*, agents.display_name AS agent_display_name
            FROM takeover_leases
            JOIN agents ON agents.id = takeover_leases.agent_id
            WHERE takeover_leases.session_id = ?
              AND takeover_leases.agent_id = ?
              AND takeover_leases.status = 'active'
            LIMIT 1
            """,
            (session_id, actor.id),
        ).fetchone()
        if lease is None:
            raise ConflictError("没有可更新麦克风状态的有效接管租约")
        connection.execute(
            """
            UPDATE takeover_leases
            SET microphone_published = ?
            WHERE id = ?
            """,
            (int(enabled), lease["id"]),
        )
        insert_audit(
            connection,
            actor_type="agent",
            actor_id=actor.id,
            action=(
                "takeover_microphone_enabled"
                if enabled
                else "takeover_microphone_disabled"
            ),
            target_type="takeover_lease",
            target_id=lease["id"],
            metadata={"sessionId": session_id, "enabled": enabled},
        )
        updated = connection.execute(
            """
            SELECT takeover_leases.*,
                   agents.display_name AS agent_display_name
            FROM takeover_leases
            JOIN agents ON agents.id = takeover_leases.agent_id
            WHERE takeover_leases.id = ?
            """,
            (lease["id"],),
        ).fetchone()
    return {
        "lease": lease_to_dict(updated),
        "expiredLeases": expired_events,
    }


def _release_lease_locked(
    connection: sqlite3.Connection,
    *,
    lease: sqlite3.Row,
    status: str,
    reason: str,
    released_at: str,
) -> dict[str, Any]:
    connection.execute(
        """
        UPDATE takeover_leases
        SET status = ?, microphone_published = 0,
            released_at = ?, end_reason = ?
        WHERE id = ?
        """,
        (status, released_at, reason, lease["id"]),
    )
    connection.execute(
        """
        UPDATE robot_sessions
        SET status = 'active'
        WHERE id = ? AND status = 'agent_takeover'
        """,
        (lease["session_id"],),
    )
    released = dict(lease)
    released["status"] = status
    released["microphone_published"] = 0
    released["released_at"] = released_at
    released["end_reason"] = reason
    return lease_to_dict(released)


def release_lease(
    db: Database,
    *,
    actor: Principal,
    session_id: str,
) -> dict[str, Any]:
    now_text = utc_now_text()
    with db.transaction(immediate=True) as connection:
        expired_events = _expire_due_locked(connection, now_text=now_text)
        session = get_session_row(connection, session_id)
        ensure_scenario_access(
            connection,
            actor=actor,
            scenario_id=session["scenario_id"],
        )
        lease = connection.execute(
            """
            SELECT takeover_leases.*, agents.display_name AS agent_display_name
            FROM takeover_leases
            JOIN agents ON agents.id = takeover_leases.agent_id
            WHERE takeover_leases.session_id = ?
              AND takeover_leases.status = 'active'
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        if lease is None:
            raise ConflictError("当前没有有效接管租约")
        if actor.role != "admin" and lease["agent_id"] != actor.id:
            raise ForbiddenError("不能释放其他客服的接管租约")
        released = _release_lease_locked(
            connection,
            lease=lease,
            status="released",
            reason="released_by_agent" if actor.id == lease["agent_id"] else "released_by_admin",
            released_at=now_text,
        )
        insert_audit(
            connection,
            actor_type="agent",
            actor_id=actor.id,
            action="takeover_released",
            target_type="takeover_lease",
            target_id=lease["id"],
            metadata={
                "robotId": lease["robot_id"],
                "sessionId": session_id,
                "ownerAgentId": lease["agent_id"],
            },
        )
    return {"lease": released, "expiredLeases": expired_events}


def release_all_for_agent(
    db: Database,
    *,
    agent_id: str,
    reason: str,
) -> dict[str, list[dict[str, Any]]]:
    now_text = utc_now_text()
    lease_events: list[dict[str, Any]] = []
    listener_updates: list[dict[str, Any]] = []
    claim_updates: list[dict[str, Any]] = []
    with db.transaction(immediate=True) as connection:
        claim_rows = connection.execute(
            """
            SELECT id FROM robot_sessions
            WHERE claimed_by_agent_id = ?
              AND status IN ('active', 'agent_takeover')
            """,
            (agent_id,),
        ).fetchall()
        listener_rows = connection.execute(
            """
            SELECT DISTINCT session_listeners.session_id,
                            robot_sessions.robot_id
            FROM session_listeners
            JOIN robot_sessions
              ON robot_sessions.id = session_listeners.session_id
            WHERE session_listeners.agent_id = ?
              AND session_listeners.enabled = 1
            """,
            (agent_id,),
        ).fetchall()
        leases = connection.execute(
            """
            SELECT takeover_leases.*, agents.display_name AS agent_display_name
            FROM takeover_leases
            JOIN agents ON agents.id = takeover_leases.agent_id
            WHERE takeover_leases.agent_id = ?
              AND takeover_leases.status = 'active'
            """,
            (agent_id,),
        ).fetchall()
        for lease in leases:
            lease_events.append(
                _release_lease_locked(
                    connection,
                    lease=lease,
                    status="released",
                    reason=reason,
                    released_at=now_text,
                )
            )
            insert_audit(
                connection,
                actor_type="system",
                actor_id=None,
                action="takeover_released",
                target_type="takeover_lease",
                target_id=lease["id"],
                metadata={"reason": reason, "agentId": agent_id},
            )
        connection.execute(
            "UPDATE session_listeners SET enabled = 0 WHERE agent_id = ?",
            (agent_id,),
        )
        connection.execute(
            """
            UPDATE robot_sessions
            SET claimed_by_agent_id = NULL, claimed_at = NULL
            WHERE claimed_by_agent_id = ?
              AND status IN ('active', 'agent_takeover')
            """,
            (agent_id,),
        )
        for claim in claim_rows:
            insert_audit(
                connection,
                actor_type="system",
                actor_id=None,
                action="session_claim_released",
                target_type="robot_session",
                target_id=claim["id"],
                metadata={"ownerAgentId": agent_id, "reason": reason},
            )
            claim_updates.append(
                session_to_dict(
                    get_session_row(connection, claim["id"])
                )
            )
        for listener in listener_rows:
            still_requested = connection.execute(
                """
                SELECT 1 FROM session_listeners
                WHERE session_id = ? AND enabled = 1
                LIMIT 1
                """,
                (listener["session_id"],),
            ).fetchone()
            listener_updates.append(
                {
                    "robotId": listener["robot_id"],
                    "sessionId": listener["session_id"],
                    "agentId": agent_id,
                    "enabled": False,
                    "robotMicrophoneRequested": still_requested is not None,
                    "occurredAt": now_text,
                }
            )
    return {
        "leases": lease_events,
        "listenerUpdates": listener_updates,
        "claimUpdates": claim_updates,
    }


def get_active_lease(
    db: Database,
    *,
    robot_id: str,
) -> dict[str, Any] | None:
    with db.connection() as connection:
        row = _active_lease(connection, robot_id=robot_id)
        if row is None:
            return None
        if parse_timestamp(row["expires_at"]) <= utc_now():
            return None
    return lease_to_dict(row)
