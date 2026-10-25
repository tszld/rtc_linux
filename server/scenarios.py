from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from .audit import insert_audit
from .auth import Principal
from .db import Database
from .errors import ConflictError, ForbiddenError, NotFoundError
from .security import utc_now_text


DEFAULT_SCENARIO_NAME = "默认场景"


def scenario_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "enabled": bool(row["enabled"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def create_scenario(
    db: Database,
    *,
    actor: Principal,
    name: str,
) -> dict[str, Any]:
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("场景名称不能为空")
    scenario_id = str(uuid.uuid4())
    now = utc_now_text()
    try:
        with db.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO scenarios (id, name, enabled, created_at, updated_at)
                VALUES (?, ?, 1, ?, ?)
                """,
                (scenario_id, clean_name, now, now),
            )
            insert_audit(
                connection,
                actor_type="agent",
                actor_id=actor.id,
                action="scenario_created",
                target_type="scenario",
                target_id=scenario_id,
                metadata={"name": clean_name},
            )
            row = connection.execute(
                "SELECT * FROM scenarios WHERE id = ?",
                (scenario_id,),
            ).fetchone()
    except sqlite3.IntegrityError as exc:
        raise ConflictError("场景名称已存在") from exc
    return scenario_to_dict(row)


def get_or_create_default_scenario(
    connection: sqlite3.Connection,
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM scenarios WHERE name = ?",
        (DEFAULT_SCENARIO_NAME,),
    ).fetchone()
    if row is not None:
        return row
    row = connection.execute(
        "SELECT * FROM scenarios WHERE enabled = 1 ORDER BY name LIMIT 1"
    ).fetchone()
    if row is not None:
        return row

    scenario_id = str(uuid.uuid4())
    now = utc_now_text()
    connection.execute(
        """
        INSERT INTO scenarios (id, name, enabled, created_at, updated_at)
        VALUES (?, ?, 1, ?, ?)
        """,
        (scenario_id, DEFAULT_SCENARIO_NAME, now, now),
    )
    insert_audit(
        connection,
        actor_type="system",
        actor_id=None,
        action="scenario_created",
        target_type="scenario",
        target_id=scenario_id,
        metadata={"name": DEFAULT_SCENARIO_NAME, "reason": "robot_registration"},
    )
    return connection.execute(
        "SELECT * FROM scenarios WHERE id = ?",
        (scenario_id,),
    ).fetchone()


def set_scenario_enabled(
    db: Database,
    *,
    actor: Principal,
    scenario_id: str,
    enabled: bool,
) -> dict[str, Any]:
    now = utc_now_text()
    with db.transaction(immediate=True) as connection:
        row = connection.execute(
            "SELECT * FROM scenarios WHERE id = ?",
            (scenario_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("场景不存在")
        connection.execute(
            "UPDATE scenarios SET enabled = ?, updated_at = ? WHERE id = ?",
            (int(enabled), now, scenario_id),
        )
        insert_audit(
            connection,
            actor_type="agent",
            actor_id=actor.id,
            action="scenario_enabled" if enabled else "scenario_disabled",
            target_type="scenario",
            target_id=scenario_id,
        )
        updated = connection.execute(
            "SELECT * FROM scenarios WHERE id = ?",
            (scenario_id,),
        ).fetchone()
    return scenario_to_dict(updated)


def list_scenarios_for_principal(
    db: Database,
    *,
    actor: Principal,
) -> list[dict[str, Any]]:
    with db.connection() as connection:
        if actor.role == "admin":
            rows = connection.execute(
                "SELECT * FROM scenarios ORDER BY name"
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT scenarios.*
                FROM scenarios
                JOIN agent_scenarios
                  ON agent_scenarios.scenario_id = scenarios.id
                WHERE agent_scenarios.agent_id = ? AND scenarios.enabled = 1
                ORDER BY scenarios.name
                """,
                (actor.id,),
            ).fetchall()
    return [scenario_to_dict(row) for row in rows]


def ensure_scenario_access(
    connection: sqlite3.Connection,
    *,
    actor: Principal,
    scenario_id: str,
) -> None:
    if actor.role == "admin":
        return
    row = connection.execute(
        """
        SELECT 1
        FROM agent_scenarios
        JOIN scenarios ON scenarios.id = agent_scenarios.scenario_id
        WHERE agent_scenarios.agent_id = ?
          AND agent_scenarios.scenario_id = ?
          AND scenarios.enabled = 1
        """,
        (actor.id, scenario_id),
    ).fetchone()
    if row is None:
        raise ForbiddenError("无权访问该场景")


def set_agent_scenarios(
    db: Database,
    *,
    actor: Principal,
    agent_id: str,
    scenario_ids: list[str],
) -> list[dict[str, Any]]:
    unique_ids = list(dict.fromkeys(scenario_ids))
    with db.transaction(immediate=True) as connection:
        agent = connection.execute(
            "SELECT * FROM agents WHERE id = ?",
            (agent_id,),
        ).fetchone()
        if agent is None:
            raise NotFoundError("客服账号不存在")
        if unique_ids:
            placeholders = ",".join("?" for _ in unique_ids)
            found = connection.execute(
                f"SELECT id FROM scenarios WHERE id IN ({placeholders})",
                unique_ids,
            ).fetchall()
            if len(found) != len(unique_ids):
                raise NotFoundError("包含不存在的场景")
        connection.execute(
            "DELETE FROM agent_scenarios WHERE agent_id = ?",
            (agent_id,),
        )
        connection.executemany(
            "INSERT INTO agent_scenarios (agent_id, scenario_id) VALUES (?, ?)",
            [(agent_id, scenario_id) for scenario_id in unique_ids],
        )
        insert_audit(
            connection,
            actor_type="agent",
            actor_id=actor.id,
            action="agent_scenarios_updated",
            target_type="agent",
            target_id=agent_id,
            metadata={"scenarioIds": unique_ids},
        )
        if unique_ids:
            placeholders = ",".join("?" for _ in unique_ids)
            rows = connection.execute(
                f"SELECT * FROM scenarios WHERE id IN ({placeholders}) ORDER BY name",
                unique_ids,
            ).fetchall()
        else:
            rows = []
    return [scenario_to_dict(row) for row in rows]
