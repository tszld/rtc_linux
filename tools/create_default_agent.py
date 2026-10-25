from __future__ import annotations

import argparse
import json
import secrets
from collections.abc import Sequence
from typing import Any

from server.admin import create_agent
from server.auth import Principal
from server.config import ServerConfig
from server.db import Database
from server.scenarios import create_scenario


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="在控制端创建默认客服账号并生成一次性随机密码"
    )
    parser.add_argument("--username", default="agent01")
    parser.add_argument("--display-name", default="默认客服")
    return parser.parse_args(argv)


def _admin_principal(db: Database) -> Principal:
    with db.connection() as connection:
        row = connection.execute(
            """
            SELECT id, role, display_name
            FROM agents
            WHERE role = 'admin' AND enabled = 1
            ORDER BY created_at
            LIMIT 1
            """
        ).fetchone()
    if row is None:
        raise ValueError("控制端没有可用的管理员，请先执行 --create-admin")
    return Principal(
        principal_type="agent",
        id=row["id"],
        role=row["role"],
        display_name=row["display_name"],
    )


def create_default_agent(
    db: Database,
    *,
    username: str = "agent01",
    display_name: str = "默认客服",
) -> dict[str, Any]:
    admin = _admin_principal(db)
    with db.connection() as connection:
        scenarios = connection.execute(
            """
            SELECT id, name
            FROM scenarios
            WHERE enabled = 1
            ORDER BY name
            """
        ).fetchall()
    if scenarios:
        scenario_ids = [row["id"] for row in scenarios]
    else:
        scenario = create_scenario(
            db,
            actor=admin,
            name="默认场景",
        )
        scenario_ids = [scenario["id"]]

    password = secrets.token_urlsafe(18)
    agent = create_agent(
        db,
        actor=admin,
        username=username,
        password=password,
        display_name=display_name,
        scenario_ids=scenario_ids,
    )
    return {
        "username": username,
        "password": password,
        "agentId": agent["id"],
        "displayName": agent["displayName"],
        "scenarioIds": scenario_ids,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    config = ServerConfig.load()
    if not config.db_path.is_file():
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": f"控制端数据库不存在：{config.db_path}",
                },
                ensure_ascii=False,
            )
        )
        return 2
    try:
        result = create_default_agent(
            Database(config.db_path),
            username=args.username,
            display_name=args.display_name,
        )
    except (ValueError, RuntimeError) as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc)},
                ensure_ascii=False,
            )
        )
        return 1
    print(
        json.dumps(
            {"ok": True, **result},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
