from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from .audit import insert_audit
from .auth import Principal, revoke_principal_tokens
from .db import Database
from .errors import ConflictError, ForbiddenError, NotFoundError
from .robots import robot_to_dict
from .security import hash_password, hash_secret, new_secret, utc_now_text
from .sessions import end_sessions_for_offline_robot


def _agent_to_dict(
    row: sqlite3.Row,
    scenario_ids: list[str] | None = None,
) -> dict[str, Any]:
    result = {
        "id": row["id"],
        "username": row["username"],
        "displayName": row["display_name"],
        "role": row["role"],
        "enabled": bool(row["enabled"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
    if scenario_ids is not None:
        result["scenarioIds"] = scenario_ids
    return result


def list_agents(db: Database) -> list[dict[str, Any]]:
    with db.connection() as connection:
        rows = connection.execute(
            "SELECT * FROM agents ORDER BY username"
        ).fetchall()
        mappings = connection.execute(
            "SELECT agent_id, scenario_id FROM agent_scenarios"
        ).fetchall()
    scenario_map: dict[str, list[str]] = {}
    for mapping in mappings:
        scenario_map.setdefault(mapping["agent_id"], []).append(
            mapping["scenario_id"]
        )
    return [
        _agent_to_dict(row, scenario_map.get(row["id"], []))
        for row in rows
    ]


def create_agent(
    db: Database,
    *,
    actor: Principal,
    username: str,
    password: str,
    display_name: str,
    scenario_ids: list[str],
) -> dict[str, Any]:
    clean_username = username.strip()
    clean_display_name = display_name.strip()
    if not clean_username or not clean_display_name:
        raise ValueError("用户名和显示名称不能为空")
    if len(password) < 8:
        raise ValueError("客服密码至少 8 个字符")
    scenario_ids = list(dict.fromkeys(scenario_ids))
    agent_id = str(uuid.uuid4())
    now = utc_now_text()
    try:
        with db.transaction(immediate=True) as connection:
            if scenario_ids:
                placeholders = ",".join("?" for _ in scenario_ids)
                found = connection.execute(
                    f"SELECT id FROM scenarios WHERE id IN ({placeholders})",
                    scenario_ids,
                ).fetchall()
                if len(found) != len(scenario_ids):
                    raise NotFoundError("包含不存在的场景")
            connection.execute(
                """
                INSERT INTO agents (
                    id, username, password_hash, display_name,
                    role, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'agent', 1, ?, ?)
                """,
                (
                    agent_id,
                    clean_username,
                    hash_password(password),
                    clean_display_name,
                    now,
                    now,
                ),
            )
            connection.executemany(
                "INSERT INTO agent_scenarios (agent_id, scenario_id) VALUES (?, ?)",
                [(agent_id, scenario_id) for scenario_id in scenario_ids],
            )
            insert_audit(
                connection,
                actor_type="agent",
                actor_id=actor.id,
                action="agent_created",
                target_type="agent",
                target_id=agent_id,
                metadata={"username": clean_username},
            )
            row = connection.execute(
                "SELECT * FROM agents WHERE id = ?",
                (agent_id,),
            ).fetchone()
    except sqlite3.IntegrityError as exc:
        raise ConflictError("用户名已存在") from exc
    return _agent_to_dict(row, scenario_ids)


def set_agent_enabled(
    db: Database,
    *,
    actor: Principal,
    agent_id: str,
    enabled: bool,
) -> dict[str, Any]:
    now = utc_now_text()
    with db.transaction(immediate=True) as connection:
        row = connection.execute(
            "SELECT * FROM agents WHERE id = ?",
            (agent_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("客服账号不存在")
        if row["id"] == actor.id and not enabled:
            raise ForbiddenError("管理员不能停用当前登录账号")
        connection.execute(
            "UPDATE agents SET enabled = ?, updated_at = ? WHERE id = ?",
            (int(enabled), now, agent_id),
        )
        revoke_principal_tokens(
            connection,
            principal_type="agent",
            principal_id=agent_id,
        )
        insert_audit(
            connection,
            actor_type="agent",
            actor_id=actor.id,
            action="agent_enabled" if enabled else "agent_disabled",
            target_type="agent",
            target_id=agent_id,
        )
        updated = connection.execute(
            "SELECT * FROM agents WHERE id = ?",
            (agent_id,),
        ).fetchone()
    payload = _agent_to_dict(updated)
    if not enabled:
        from .takeover import release_all_for_agent

        released = release_all_for_agent(
            db,
            agent_id=agent_id,
            reason="agent_disabled",
        )
        payload["releasedLeases"] = released["leases"]
        payload["listenerUpdates"] = released["listenerUpdates"]
        payload["claimUpdates"] = released["claimUpdates"]
    return payload


def list_admin_robots(db: Database) -> list[dict[str, Any]]:
    with db.connection() as connection:
        rows = connection.execute(
            """
            SELECT robots.*, scenarios.name AS scenario_name
            FROM robots
            JOIN scenarios ON scenarios.id = robots.scenario_id
            WHERE robots.deleted_at IS NULL
            ORDER BY scenarios.name, robots.display_name
            """
        ).fetchall()
    return [robot_to_dict(row) for row in rows]


def create_robot(
    db: Database,
    *,
    actor: Principal,
    display_name: str,
    scenario_id: str,
) -> dict[str, Any]:
    clean_name = display_name.strip()
    if not clean_name:
        raise ValueError("机器人显示名称不能为空")
    robot_id = str(uuid.uuid4())
    channel_id = "rtc-" + uuid.uuid4().hex
    credential = new_secret(32)
    now = utc_now_text()
    with db.transaction(immediate=True) as connection:
        scenario = connection.execute(
            "SELECT * FROM scenarios WHERE id = ?",
            (scenario_id,),
        ).fetchone()
        if scenario is None:
            raise NotFoundError("场景不存在")
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
                clean_name,
                scenario_id,
                channel_id,
                hash_secret(credential),
                now,
                now,
            ),
        )
        insert_audit(
            connection,
            actor_type="agent",
            actor_id=actor.id,
            action="robot_created",
            target_type="robot",
            target_id=robot_id,
            metadata={"scenarioId": scenario_id},
        )
        row = connection.execute(
            """
            SELECT robots.*, scenarios.name AS scenario_name
            FROM robots
            JOIN scenarios ON scenarios.id = robots.scenario_id
            WHERE robots.id = ?
            """,
            (robot_id,),
        ).fetchone()
    payload = robot_to_dict(row)
    payload["robotId"] = robot_id
    payload["credential"] = credential
    return payload


def rename_robot(
    db: Database,
    *,
    actor: Principal,
    robot_id: str,
    display_name: str,
) -> dict[str, Any]:
    clean_name = display_name.strip()
    if not clean_name:
        raise ValueError("机器人显示名称不能为空")
    if len(clean_name) > 128:
        raise ValueError("机器人显示名称不能超过 128 个字符")
    now = utc_now_text()
    with db.transaction(immediate=True) as connection:
        robot = connection.execute(
            """
            SELECT * FROM robots
            WHERE id = ? AND deleted_at IS NULL
            """,
            (robot_id,),
        ).fetchone()
        if robot is None:
            raise NotFoundError("机器人不存在")
        old_name = robot["display_name"]
        connection.execute(
            "UPDATE robots SET display_name = ?, updated_at = ? WHERE id = ?",
            (clean_name, now, robot_id),
        )
        insert_audit(
            connection,
            actor_type="agent",
            actor_id=actor.id,
            action="robot_renamed",
            target_type="robot",
            target_id=robot_id,
            metadata={"oldDisplayName": old_name, "newDisplayName": clean_name},
        )
        row = connection.execute(
            """
            SELECT robots.*, scenarios.name AS scenario_name
            FROM robots
            JOIN scenarios ON scenarios.id = robots.scenario_id
            WHERE robots.id = ?
            """,
            (robot_id,),
        ).fetchone()
    return robot_to_dict(row)


def delete_robot(
    db: Database,
    *,
    actor: Principal,
    robot_id: str,
) -> dict[str, Any]:
    now = utc_now_text()
    with db.transaction(immediate=True) as connection:
        robot = connection.execute(
            """
            SELECT * FROM robots
            WHERE id = ? AND deleted_at IS NULL
            """,
            (robot_id,),
        ).fetchone()
        if robot is None:
            raise NotFoundError("机器人不存在")
        connection.execute(
            """
            UPDATE robots
            SET enabled = 0, approved = 0, status = 'offline',
                deleted_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, now, robot_id),
        )
        revoke_principal_tokens(
            connection,
            principal_type="robot",
            principal_id=robot_id,
        )
        insert_audit(
            connection,
            actor_type="agent",
            actor_id=actor.id,
            action="robot_deleted",
            target_type="robot",
            target_id=robot_id,
            metadata={"displayName": robot["display_name"]},
        )
    ended_sessions = end_sessions_for_offline_robot(
        db,
        robot_id=robot_id,
        reason="robot_deleted",
    )
    return {
        "robotId": robot_id,
        "displayName": robot["display_name"],
        "endedSessions": ended_sessions,
    }


def set_robot_approval(
    db: Database,
    *,
    actor: Principal,
    robot_id: str,
    approved: bool,
) -> dict[str, Any]:
    now = utc_now_text()
    with db.transaction(immediate=True) as connection:
        robot = connection.execute(
            "SELECT * FROM robots WHERE id = ? AND deleted_at IS NULL",
            (robot_id,),
        ).fetchone()
        if robot is None:
            raise NotFoundError("机器人不存在")
        connection.execute(
            """
            UPDATE robots
            SET approved = ?,
                status = CASE WHEN ? = 0 THEN 'offline' ELSE status END,
                updated_at = ?
            WHERE id = ?
            """,
            (int(approved), int(approved), now, robot_id),
        )
        if not approved:
            revoke_principal_tokens(
                connection,
                principal_type="robot",
                principal_id=robot_id,
            )
        insert_audit(
            connection,
            actor_type="agent",
            actor_id=actor.id,
            action="robot_approved" if approved else "robot_rejected",
            target_type="robot",
            target_id=robot_id,
        )
        row = connection.execute(
            """
            SELECT robots.*, scenarios.name AS scenario_name
            FROM robots
            JOIN scenarios ON scenarios.id = robots.scenario_id
            WHERE robots.id = ?
            """,
            (robot_id,),
        ).fetchone()
    return robot_to_dict(row)


def set_robot_enabled(
    db: Database,
    *,
    actor: Principal,
    robot_id: str,
    enabled: bool,
) -> dict[str, Any]:
    now = utc_now_text()
    ended_sessions: list[dict[str, Any]] = []
    with db.transaction(immediate=True) as connection:
        robot = connection.execute(
            "SELECT * FROM robots WHERE id = ? AND deleted_at IS NULL",
            (robot_id,),
        ).fetchone()
        if robot is None:
            raise NotFoundError("机器人不存在")
        connection.execute(
            """
            UPDATE robots
            SET enabled = ?, status = CASE WHEN ? = 0 THEN 'offline' ELSE status END,
                updated_at = ?
            WHERE id = ?
            """,
            (int(enabled), int(enabled), now, robot_id),
        )
        if not enabled:
            revoke_principal_tokens(
                connection,
                principal_type="robot",
                principal_id=robot_id,
            )
        insert_audit(
            connection,
            actor_type="agent",
            actor_id=actor.id,
            action="robot_enabled" if enabled else "robot_disabled",
            target_type="robot",
            target_id=robot_id,
        )
    if not enabled:
        ended_sessions = end_sessions_for_offline_robot(
            db,
            robot_id=robot_id,
            reason="robot_disabled",
        )
    row = _robot_admin_row(db, robot_id)
    payload = robot_to_dict(row)
    payload["endedSessions"] = ended_sessions
    return payload


def set_robot_scenario(
    db: Database,
    *,
    actor: Principal,
    robot_id: str,
    scenario_id: str,
) -> dict[str, Any]:
    now = utc_now_text()
    with db.transaction(immediate=True) as connection:
        robot = connection.execute(
            "SELECT * FROM robots WHERE id = ? AND deleted_at IS NULL",
            (robot_id,),
        ).fetchone()
        if robot is None:
            raise NotFoundError("机器人不存在")
        active = connection.execute(
            """
            SELECT 1 FROM robot_sessions
            WHERE robot_id = ? AND status IN ('active', 'agent_takeover')
            """,
            (robot_id,),
        ).fetchone()
        if active is not None:
            raise ConflictError("业务会话进行中不能切换场景")
        scenario = connection.execute(
            "SELECT 1 FROM scenarios WHERE id = ?",
            (scenario_id,),
        ).fetchone()
        if scenario is None:
            raise NotFoundError("场景不存在")
        connection.execute(
            """
            UPDATE robots SET scenario_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (scenario_id, now, robot_id),
        )
        insert_audit(
            connection,
            actor_type="agent",
            actor_id=actor.id,
            action="robot_scenario_changed",
            target_type="robot",
            target_id=robot_id,
            metadata={"scenarioId": scenario_id},
        )
    return robot_to_dict(_robot_admin_row(db, robot_id))


def rotate_robot_credential(
    db: Database,
    *,
    actor: Principal,
    robot_id: str,
) -> dict[str, str]:
    credential = new_secret(32)
    now = utc_now_text()
    with db.transaction(immediate=True) as connection:
        robot = connection.execute(
            "SELECT 1 FROM robots WHERE id = ? AND deleted_at IS NULL",
            (robot_id,),
        ).fetchone()
        if robot is None:
            raise NotFoundError("机器人不存在")
        connection.execute(
            """
            UPDATE robots SET credential_hash = ?, updated_at = ?
            WHERE id = ?
            """,
            (hash_secret(credential), now, robot_id),
        )
        revoke_principal_tokens(
            connection,
            principal_type="robot",
            principal_id=robot_id,
        )
        insert_audit(
            connection,
            actor_type="agent",
            actor_id=actor.id,
            action="robot_credential_rotated",
            target_type="robot",
            target_id=robot_id,
        )
    return {"robotId": robot_id, "credential": credential}


def _robot_admin_row(db: Database, robot_id: str) -> sqlite3.Row:
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
