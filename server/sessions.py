from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from .audit import insert_audit
from .auth import Principal
from .db import Database
from .errors import ConflictError, ForbiddenError, NotFoundError
from .security import utc_now_text


def session_to_dict(
    row: sqlite3.Row,
    *,
    lease: sqlite3.Row | None = None,
) -> dict[str, Any]:
    result = {
        "id": row["id"],
        "robotId": row["robot_id"],
        "status": row["status"],
        "startedAt": row["started_at"],
        "endedAt": row["ended_at"],
        "endReason": row["end_reason"],
    }
    for optional_name in (
        "display_name",
        "scenario_id",
        "scenario_name",
        "robot_status",
        "robot_enabled",
        "robot_approved",
        "channel_id",
        "claimed_agent_display_name",
    ):
        if optional_name in row.keys():
            result[
                {
                    "display_name": "robotDisplayName",
                    "scenario_id": "scenarioId",
                    "scenario_name": "scenarioName",
                    "robot_status": "robotStatus",
                    "robot_enabled": "robotEnabled",
                    "robot_approved": "robotApproved",
                    "channel_id": "channelId",
                    "claimed_agent_display_name": "claimedByAgentDisplayName",
                }[optional_name]
            ] = (
                bool(row[optional_name])
                if optional_name in {"robot_enabled", "robot_approved"}
                else row[optional_name]
            )
    if lease is not None:
        result["takeover"] = {
            "leaseId": lease["id"],
            "agentId": lease["agent_id"],
            "agentDisplayName": lease["agent_display_name"],
            "expiresAt": lease["expires_at"],
            "microphoneEnabled": bool(lease["microphone_published"]),
        }
    else:
        result["takeover"] = None
    if row["claimed_by_agent_id"]:
        result["claim"] = {
            "agentId": row["claimed_by_agent_id"],
            "agentDisplayName": (
                row["claimed_agent_display_name"]
                if "claimed_agent_display_name" in row.keys()
                else ""
            ),
            "claimedAt": row["claimed_at"],
        }
    else:
        result["claim"] = None
    return result


def start_session(
    db: Database,
    *,
    robot_id: str,
) -> dict[str, Any]:
    session_id = str(uuid.uuid4())
    now = utc_now_text()
    try:
        with db.transaction(immediate=True) as connection:
            robot = connection.execute(
                """
                SELECT robots.*, scenarios.enabled AS scenario_enabled
                FROM robots
                JOIN scenarios ON scenarios.id = robots.scenario_id
                WHERE robots.id = ? AND robots.deleted_at IS NULL
                """,
                (robot_id,),
            ).fetchone()
            if robot is None:
                raise NotFoundError("机器人不存在")
            if not robot["approved"] or not robot["enabled"]:
                raise ForbiddenError("机器人未审批或已停用")
            if not robot["scenario_enabled"]:
                raise ForbiddenError("机器人所属场景已停用")
            if robot["status"] == "offline":
                raise ConflictError("机器人在线后才能开始业务会话")
            existing = connection.execute(
                """
                SELECT * FROM robot_sessions
                WHERE robot_id = ? AND status IN ('active', 'agent_takeover')
                """,
                (robot_id,),
            ).fetchone()
            if existing is not None:
                raise ConflictError("机器人已有未结束的业务会话")
            connection.execute(
                """
                INSERT INTO robot_sessions (
                    id, robot_id, status, started_at, ended_at, end_reason
                ) VALUES (?, ?, 'active', ?, NULL, NULL)
                """,
                (session_id, robot_id, now),
            )
            connection.execute(
                """
                INSERT INTO robot_media_state (
                    robot_id, session_id, microphone_published,
                    camera_published, automatic_speech_paused, updated_at
                ) VALUES (?, ?, 0, 0, 0, ?)
                ON CONFLICT(robot_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    microphone_published = 0,
                    camera_published = 0,
                    automatic_speech_paused = 0,
                    updated_at = excluded.updated_at
                """,
                (robot_id, session_id, now),
            )
            insert_audit(
                connection,
                actor_type="robot",
                actor_id=robot_id,
                action="session_started",
                target_type="robot_session",
                target_id=session_id,
            )
            row = connection.execute(
                """
                SELECT robot_sessions.*, robots.display_name,
                       robots.scenario_id, scenarios.name AS scenario_name,
                       robots.status AS robot_status,
                       robots.enabled AS robot_enabled,
                       robots.approved AS robot_approved,
                       robots.channel_id,
                       claimed_agents.display_name
                           AS claimed_agent_display_name
                FROM robot_sessions
                JOIN robots ON robots.id = robot_sessions.robot_id
                JOIN scenarios ON scenarios.id = robots.scenario_id
                LEFT JOIN agents AS claimed_agents
                  ON claimed_agents.id = robot_sessions.claimed_by_agent_id
                WHERE robot_sessions.id = ?
                """,
                (session_id,),
            ).fetchone()
    except sqlite3.IntegrityError as exc:
        raise ConflictError("机器人已有未结束的业务会话") from exc
    return session_to_dict(row)


def get_session_row(
    connection: sqlite3.Connection,
    session_id: str,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT robot_sessions.*, robots.display_name,
               robots.scenario_id, scenarios.name AS scenario_name,
               robots.status AS robot_status,
               robots.enabled AS robot_enabled,
               robots.approved AS robot_approved,
               robots.channel_id,
               claimed_agents.display_name AS claimed_agent_display_name
        FROM robot_sessions
        JOIN robots ON robots.id = robot_sessions.robot_id
        JOIN scenarios ON scenarios.id = robots.scenario_id
        LEFT JOIN agents AS claimed_agents
          ON claimed_agents.id = robot_sessions.claimed_by_agent_id
        WHERE robot_sessions.id = ?
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError("业务会话不存在")
    return row


def get_session(
    db: Database,
    session_id: str,
) -> dict[str, Any]:
    with db.connection() as connection:
        row = get_session_row(connection, session_id)
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
    return session_to_dict(row, lease=lease)


def claim_session(
    db: Database,
    *,
    actor: Principal,
    session_id: str,
) -> dict[str, Any]:
    now = utc_now_text()
    with db.transaction(immediate=True) as connection:
        session = ensure_session_access(
            connection,
            actor=actor,
            session_id=session_id,
        )
        if session["status"] == "ended":
            raise ConflictError("业务会话已经结束")
        if session["robot_status"] == "offline":
            raise ConflictError("机器人不在线")
        claimed_by = session["claimed_by_agent_id"]
        if claimed_by == actor.id:
            return session_to_dict(session)
        if claimed_by is not None:
            display_name = (
                session["claimed_agent_display_name"]
                or "其他客服"
            )
            raise ConflictError(f"会话已被客服 {display_name} 占用")
        existing_claim = connection.execute(
            """
            SELECT id FROM robot_sessions
            WHERE claimed_by_agent_id = ?
              AND status IN ('active', 'agent_takeover')
              AND id <> ?
            LIMIT 1
            """,
            (actor.id, session_id),
        ).fetchone()
        if existing_claim is not None:
            raise ConflictError("当前客服已经接入另一个会话")
        updated = connection.execute(
            """
            UPDATE robot_sessions
            SET claimed_by_agent_id = ?, claimed_at = ?
            WHERE id = ? AND claimed_by_agent_id IS NULL
            """,
            (actor.id, now, session_id),
        )
        if updated.rowcount != 1:
            latest = get_session_row(connection, session_id)
            display_name = (
                latest["claimed_agent_display_name"]
                or "其他客服"
            )
            raise ConflictError(f"会话已被客服 {display_name} 占用")
        insert_audit(
            connection,
            actor_type="agent",
            actor_id=actor.id,
            action="session_claimed",
            target_type="robot_session",
            target_id=session_id,
        )
        row = get_session_row(connection, session_id)
    return session_to_dict(row)


def release_session_claim(
    db: Database,
    *,
    actor: Principal,
    session_id: str,
    reason: str = "agent_left",
) -> dict[str, Any]:
    now = utc_now_text()
    lease_event: dict[str, Any] | None = None
    listener_update: dict[str, Any] | None = None
    with db.transaction(immediate=True) as connection:
        session = ensure_session_access(
            connection,
            actor=actor,
            session_id=session_id,
        )
        claimed_by = session["claimed_by_agent_id"]
        if claimed_by is None:
            return {
                "session": session_to_dict(session),
                "releasedLease": None,
                "listenerUpdate": None,
            }
        if actor.role != "admin" and claimed_by != actor.id:
            raise ForbiddenError("不能释放其他客服接入的会话")

        lease = connection.execute(
            """
            SELECT takeover_leases.*,
                   agents.display_name AS agent_display_name
            FROM takeover_leases
            JOIN agents ON agents.id = takeover_leases.agent_id
            WHERE takeover_leases.session_id = ?
              AND takeover_leases.status = 'active'
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        if lease is not None:
            connection.execute(
                """
                UPDATE takeover_leases
                SET status = 'released', microphone_published = 0,
                    released_at = ?, end_reason = ?
                WHERE id = ?
                """,
                (now, reason, lease["id"]),
            )
            connection.execute(
                """
                UPDATE robot_sessions
                SET status = 'active'
                WHERE id = ? AND status = 'agent_takeover'
                """,
                (session_id,),
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
                    "reason": reason,
                },
            )
            lease_event = {
                "id": lease["id"],
                "robotId": lease["robot_id"],
                "sessionId": lease["session_id"],
                "agentId": lease["agent_id"],
                "agentDisplayName": lease["agent_display_name"],
                "status": "released",
                "microphoneEnabled": False,
                "acquiredAt": lease["acquired_at"],
                "expiresAt": lease["expires_at"],
                "releasedAt": now,
                "endReason": reason,
            }

        listener = connection.execute(
            """
            SELECT enabled FROM session_listeners
            WHERE session_id = ? AND agent_id = ?
            """,
            (session_id, claimed_by),
        ).fetchone()
        if listener is not None:
            connection.execute(
                """
                DELETE FROM session_listeners
                WHERE session_id = ? AND agent_id = ?
                """,
                (session_id, claimed_by),
            )
            still_requested = connection.execute(
                """
                SELECT 1 FROM session_listeners
                WHERE session_id = ? AND enabled = 1
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            listener_update = {
                "robotId": session["robot_id"],
                "sessionId": session_id,
                "agentId": claimed_by,
                "enabled": False,
                "robotMicrophoneRequested": still_requested is not None,
                "occurredAt": now,
            }

        connection.execute(
            """
            UPDATE robot_sessions
            SET claimed_by_agent_id = NULL, claimed_at = NULL
            WHERE id = ?
            """,
            (session_id,),
        )
        insert_audit(
            connection,
            actor_type="agent",
            actor_id=actor.id,
            action="session_claim_released",
            target_type="robot_session",
            target_id=session_id,
            metadata={"ownerAgentId": claimed_by, "reason": reason},
        )
        row = get_session_row(connection, session_id)
    return {
        "session": session_to_dict(row),
        "releasedLease": lease_event,
        "listenerUpdate": listener_update,
    }


def list_sessions_for_principal(
    db: Database,
    *,
    actor: Principal,
    include_ended: bool = False,
) -> list[dict[str, Any]]:
    status_clause = "" if include_ended else (
        "AND robot_sessions.status IN ('active', 'agent_takeover')"
    )
    with db.connection() as connection:
        params: list[object] = []
        access_clause = ""
        if actor.role != "admin":
            access_clause = "AND agent_scenarios.agent_id = ?"
            params.append(actor.id)
        rows = connection.execute(
            f"""
            SELECT robot_sessions.*, robots.display_name,
                   robots.scenario_id, scenarios.name AS scenario_name,
                   robots.status AS robot_status,
                   robots.enabled AS robot_enabled,
                   robots.approved AS robot_approved,
                   robots.channel_id,
                   claimed_agents.display_name
                       AS claimed_agent_display_name
            FROM robot_sessions
            JOIN robots ON robots.id = robot_sessions.robot_id
            JOIN scenarios ON scenarios.id = robots.scenario_id
            LEFT JOIN agents AS claimed_agents
              ON claimed_agents.id = robot_sessions.claimed_by_agent_id
            LEFT JOIN agent_scenarios
              ON agent_scenarios.scenario_id = scenarios.id
            WHERE scenarios.enabled = 1
              AND robots.deleted_at IS NULL
              {access_clause}
              {status_clause}
            GROUP BY robot_sessions.id
            ORDER BY robot_sessions.started_at DESC
            """,
            params,
        ).fetchall()
        leases = connection.execute(
            """
            SELECT takeover_leases.*, agents.display_name AS agent_display_name
            FROM takeover_leases
            JOIN agents ON agents.id = takeover_leases.agent_id
            WHERE takeover_leases.status = 'active'
            """
        ).fetchall()
    lease_by_session = {row["session_id"]: row for row in leases}
    return [
        session_to_dict(row, lease=lease_by_session.get(row["id"]))
        for row in rows
    ]


def ensure_session_access(
    connection: sqlite3.Connection,
    *,
    actor: Principal,
    session_id: str,
) -> sqlite3.Row:
    row = get_session_row(connection, session_id)
    if actor.role != "admin":
        access = connection.execute(
            """
            SELECT 1 FROM agent_scenarios
            WHERE agent_id = ? AND scenario_id = ?
            """,
            (actor.id, row["scenario_id"]),
        ).fetchone()
        if access is None:
            raise ForbiddenError("无权访问该会话")
    return row


def ensure_session_claim(
    connection: sqlite3.Connection,
    *,
    actor: Principal,
    session: sqlite3.Row,
) -> None:
    if actor.role == "admin":
        return
    claimed_by = session["claimed_by_agent_id"]
    if claimed_by == actor.id:
        return
    if claimed_by is None:
        raise ConflictError("请先接入该会话")
    display_name = (
        session["claimed_agent_display_name"]
        if "claimed_agent_display_name" in session.keys()
        else ""
    ) or "其他客服"
    raise ConflictError(f"会话已被客服 {display_name} 占用")


def end_session(
    db: Database,
    *,
    session_id: str,
    robot_id: str | None,
    reason: str,
    actor: Principal | None = None,
) -> dict[str, Any]:
    now = utc_now_text()
    with db.transaction(immediate=True) as connection:
        row = get_session_row(connection, session_id)
        if robot_id is not None and row["robot_id"] != robot_id:
            raise ForbiddenError("机器人无权结束该会话")
        if row["status"] == "ended":
            raise ConflictError("业务会话已经结束")
        lease = connection.execute(
            """
            SELECT * FROM takeover_leases
            WHERE session_id = ? AND status = 'active'
            """,
            (session_id,),
        ).fetchone()
        connection.execute(
            """
            UPDATE robot_sessions
            SET status = 'ended', claimed_by_agent_id = NULL,
                claimed_at = NULL, ended_at = ?, end_reason = ?
            WHERE id = ?
            """,
            (now, reason, session_id),
        )
        if lease is not None:
            connection.execute(
                """
                UPDATE takeover_leases
                SET status = 'released', microphone_published = 0,
                    released_at = ?, end_reason = ?
                WHERE id = ?
                """,
                (now, "session_ended", lease["id"]),
            )
        connection.execute(
            "DELETE FROM session_listeners WHERE session_id = ?",
            (session_id,),
        )
        connection.execute(
            """
            UPDATE robot_media_state
            SET session_id = NULL, microphone_published = 0,
                camera_published = 0, automatic_speech_paused = 0,
                updated_at = ?
            WHERE robot_id = ?
            """,
            (now, row["robot_id"]),
        )
        insert_audit(
            connection,
            actor_type=(
                "agent"
                if actor is not None or lease is not None
                else "robot"
            ),
            actor_id=(
                actor.id
                if actor is not None
                else lease["agent_id"] if lease is not None else row["robot_id"]
            ),
            action="session_ended",
            target_type="robot_session",
            target_id=session_id,
            metadata={"reason": reason},
        )
    result = dict(row)
    result.update(
        {
            "status": "ended",
            "claimed_by_agent_id": None,
            "claimed_at": None,
            "claimed_agent_display_name": None,
            "ended_at": now,
            "end_reason": reason,
        }
    )
    return {
        "session": session_to_dict(result),
        "releasedLeaseId": lease["id"] if lease is not None else None,
        "releasedAgentId": lease["agent_id"] if lease is not None else None,
    }


def end_sessions_for_offline_robot(
    db: Database,
    *,
    robot_id: str,
    reason: str,
) -> list[dict[str, Any]]:
    with db.connection() as connection:
        rows = connection.execute(
            """
            SELECT id FROM robot_sessions
            WHERE robot_id = ? AND status IN ('active', 'agent_takeover')
            """,
            (robot_id,),
        ).fetchall()
    ended = []
    for row in rows:
        try:
            ended.append(
                end_session(
                    db,
                    session_id=row["id"],
                    robot_id=robot_id,
                    reason=reason,
                )
            )
        except ConflictError:
            continue
    return ended


def set_listen_request(
    db: Database,
    *,
    actor: Principal,
    session_id: str,
    enabled: bool,
) -> dict[str, Any]:
    now = utc_now_text()
    with db.transaction(immediate=True) as connection:
        session = ensure_session_access(
            connection,
            actor=actor,
            session_id=session_id,
        )
        if session["status"] == "ended":
            raise ConflictError("业务会话已经结束")
        if session["robot_status"] == "offline":
            raise ConflictError("机器人不在线")
        if enabled:
            ensure_session_claim(
                connection,
                actor=actor,
                session=session,
            )
            connection.execute(
                """
                INSERT INTO session_listeners (
                    session_id, agent_id, enabled, updated_at
                ) VALUES (?, ?, 1, ?)
                ON CONFLICT(session_id, agent_id) DO UPDATE SET
                    enabled = 1,
                    updated_at = excluded.updated_at
                """,
                (session_id, actor.id, now),
            )
        else:
            connection.execute(
                """
                DELETE FROM session_listeners
                WHERE session_id = ? AND agent_id = ?
                """,
                (session_id, actor.id),
            )
        any_listener = connection.execute(
            """
            SELECT 1 FROM session_listeners
            WHERE session_id = ? AND enabled = 1
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        insert_audit(
            connection,
            actor_type="agent",
            actor_id=actor.id,
            action="listen_enabled" if enabled else "listen_disabled",
            target_type="robot_session",
            target_id=session_id,
        )
    return {
        "sessionId": session_id,
        "robotId": session["robot_id"],
        "agentId": actor.id,
        "enabled": enabled,
        "robotMicrophoneRequested": any_listener is not None,
        "occurredAt": now,
    }
