from __future__ import annotations

import json
import sqlite3
from typing import Any

from .db import Database
from .security import utc_now_text


def insert_audit(
    connection: sqlite3.Connection,
    *,
    actor_type: str,
    actor_id: str | None,
    action: str,
    target_type: str,
    target_id: str | None,
    metadata: dict[str, Any] | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO audit_events (
            actor_type, actor_id, action, target_type, target_id,
            metadata_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            actor_type,
            actor_id,
            action,
            target_type,
            target_id,
            json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":")),
            utc_now_text(),
        ),
    )


def list_audit_events(db: Database, *, limit: int = 100) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 500))
    with db.connection() as connection:
        rows = connection.execute(
            """
            SELECT * FROM audit_events
            ORDER BY id DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        result.append(item)
    return result
