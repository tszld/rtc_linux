from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Sequence

import uvicorn

from project_config import resolve_bind_host

from .app import create_app
from .auth import create_or_update_admin
from .config import ServerConfig
from .db import Database


def _prompt_password(reset: bool) -> str:
    label = "新管理员密码" if reset else "管理员密码"
    password = getpass.getpass(f"{label}: ")
    confirmation = getpass.getpass(f"再次输入{label}: ")
    if password != confirmation:
        raise ValueError("两次输入的密码不一致")
    return password


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ARTC 业务后端")
    parser.add_argument(
        "--create-admin",
        metavar="USERNAME",
        help="创建超级管理员后退出",
    )
    parser.add_argument(
        "--reset-admin-password",
        metavar="USERNAME",
        help="重置管理员密码后退出",
    )
    parser.add_argument(
        "--init-only",
        action="store_true",
        help="仅初始化数据库后退出",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    if not sys.platform.startswith("linux"):
        print("ARTC 控制端只支持 Linux。", file=sys.stderr)
        return 1
    args = parse_args(argv)
    try:
        config = ServerConfig.load()
        config.validate()
        bind_host = resolve_bind_host(config.host)
        db = Database(config.db_path)
        db.initialize()

        if args.create_admin:
            create_or_update_admin(
                db,
                username=args.create_admin,
                password=_prompt_password(reset=False),
            )
            print(f"管理员 {args.create_admin} 已创建。")
            return 0
        if args.reset_admin_password:
            create_or_update_admin(
                db,
                username=args.reset_admin_password,
                password=_prompt_password(reset=True),
                reset_existing=True,
            )
            print(f"管理员 {args.reset_admin_password} 密码已重置。")
            return 0
        if args.init_only:
            print(f"数据库已初始化：{config.db_path}")
            return 0
        with db.connection() as connection:
            has_account = connection.execute(
                "SELECT 1 FROM agents LIMIT 1"
            ).fetchone() is not None
        if not has_account:
            print(
                "警告：数据库为空，尚未创建任何客服或管理员。"
                "请先停止服务并执行："
                "python main.py server --create-admin admin",
                file=sys.stderr,
            )
        if not config.rtc_configured:
            print(
                "警告：ARTC AppID/AppKey 未配置，媒体 Token 接口将返回 503。",
                file=sys.stderr,
            )
        print(
            f"控制端监听：{bind_host}:{config.port}",
            flush=True,
        )
        app = create_app(config, db)
        uvicorn.run(
            app,
            host=bind_host,
            port=config.port,
            log_level="info",
            access_log=False,
        )
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"后端启动失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
