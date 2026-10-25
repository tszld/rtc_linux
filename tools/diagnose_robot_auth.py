from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from project_config import ROOT, get_section
from server.config import ServerConfig
from server.db import Database


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="只读诊断机器人身份是否存在于控制端数据库（仅限控制端 Linux）"
    )
    parser.add_argument(
        "--robot-id",
        help="不提供时读取 config.json 的 robot.robot_id",
    )
    parser.add_argument(
        "--db",
        type=Path,
        help="仅诊断时读取指定 SQLite 文件，不改变项目配置",
    )
    return parser.parse_args(argv)


def inspect_robot(db: Database, robot_id: str) -> dict[str, object]:
    with db.connection() as connection:
        row = connection.execute(
            """
            SELECT id, approved, enabled, status, scenario_id
            FROM robots
            WHERE id = ?
            """,
            (robot_id,),
        ).fetchone()
    if row is None:
        return {"robotExists": False}
    return {
        "robotExists": True,
        "approved": bool(row["approved"]),
        "enabled": bool(row["enabled"]),
        "status": row["status"],
        "scenarioId": row["scenario_id"],
    }


def inspect_robot_file(path: Path, robot_id: str) -> dict[str, object]:
    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT id, approved, enabled, status, scenario_id
            FROM robots
            WHERE id = ?
            """,
            (robot_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return {"robotExists": False}
    return {
        "robotExists": True,
        "approved": bool(row["approved"]),
        "enabled": bool(row["enabled"]),
        "status": row["status"],
        "scenarioId": row["scenario_id"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    config = ServerConfig.load()
    robot_id = str(
        args.robot_id
        or get_section("robot").get("robot_id", "")
    ).strip()
    if not robot_id:
        print(
            json.dumps(
                {"ok": False, "error": "缺少 robot_id"},
                ensure_ascii=False,
            )
        )
        return 2
    db_path = args.db.expanduser() if args.db else config.db_path
    if not db_path.is_absolute():
        db_path = (ROOT / db_path).resolve()
    if not db_path.is_file():
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": (
                        f"未找到控制端数据库：{db_path}。"
                        "此诊断工具只能在控制端 Linux 运行；"
                        "机器人端不读取数据库。"
                    ),
                },
                ensure_ascii=False,
            )
        )
        return 2

    try:
        result = inspect_robot_file(db_path, robot_id)
    except sqlite3.Error as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": f"无法只读查询数据库：{exc}",
                },
                ensure_ascii=False,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "ok": bool(result["robotExists"]),
                "dbPath": str(db_path),
                **result,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if result["robotExists"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
