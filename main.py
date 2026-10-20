from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence
from urllib.error import HTTPError
from urllib.request import Request, urlopen


@contextmanager
def temporary_argv(arguments: Sequence[str]) -> Iterator[None]:
    original = sys.argv
    sys.argv = [original[0], *arguments]
    try:
        yield
    finally:
        sys.argv = original


def run_server(args: argparse.Namespace) -> int:
    from server.config import load_config

    config = load_config()
    port = args.port or config.port
    if args.create_admin or args.reset_admin_password:
        from server.admin import (
            create_initial_admin,
            reset_initial_admin_password,
        )
        from server.db import initialize_database

        initialize_database(config, end_stale_calls=False)
        username = args.admin_username or "admin"
        password = prompt_admin_password(
            "重置密码" if args.reset_admin_password else "创建管理员"
        )
        if password is None:
            return 1
        if args.reset_admin_password:
            reset_initial_admin_password(
                config,
                username=username,
                password=password,
            )
            print(f"超级管理员 {username} 的密码已重置。")
        else:
            created = create_initial_admin(
                config,
                username=username,
                password=password,
            )
            print(f"超级管理员 {created.username} 已创建。")
        return 0
    if args.stop:
        return stop_server(config, port)

    host = args.host or config.host
    shutdown_token = secrets.token_urlsafe(32)
    control_path = server_control_path(config, port)
    control_path.parent.mkdir(parents=True, exist_ok=True)
    control_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "port": port,
                "token": shutdown_token,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    app, server = build_uvicorn_server(
        config,
        host=host,
        port=port,
        shutdown_token=shutdown_token,
    )
    print(
        "停止后端：python -X utf8 main.py server --stop",
        flush=True,
    )
    try:
        server.run()
    finally:
        try:
            control_path.unlink()
        except FileNotFoundError:
            pass
    return 0


def prompt_admin_password(action: str) -> str | None:
    password = getpass.getpass(f"请输入{action}的新密码：")
    if not password:
        print("密码不能为空。", file=sys.stderr)
        return None
    confirmation = getpass.getpass("请再次输入密码：")
    if password != confirmation:
        print("两次输入的密码不一致。", file=sys.stderr)
        return None
    return password


def server_control_path(config, port: int) -> Path:
    return config.db_path.parent / f"server-control-{port}.json"


def build_uvicorn_server(
    config,
    *,
    host: str,
    port: int,
    shutdown_token: str,
):
    import uvicorn

    from server.app import create_app

    app = create_app(config)
    uvicorn_config = uvicorn.Config(
        app,
        host=host,
        port=port,
        timeout_graceful_shutdown=3,
    )
    server = uvicorn.Server(uvicorn_config)
    app.state.shutdown_token = shutdown_token
    app.state.request_shutdown = lambda: setattr(server, "should_exit", True)
    return app, server


def stop_server(config, port: int) -> int:
    control_path = server_control_path(config, port)
    if not control_path.is_file():
        print(f"未找到本机后端控制文件：{control_path}", file=sys.stderr)
        return 1
    try:
        control = json.loads(control_path.read_text(encoding="utf-8"))
        port = int(control["port"])
        token = str(control["token"])
        request = Request(
            f"http://127.0.0.1:{port}/api/server/shutdown",
            method="POST",
            headers={"X-Server-Control-Token": token},
        )
        with urlopen(request, timeout=5) as response:
            response.read()
        print(f"后端停止请求已发送，PID {control['pid']}。")
        return 0
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        print(f"后端拒绝停止请求：HTTP {error.code} {detail}", file=sys.stderr)
        return 1
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"无法停止后端：{error}", file=sys.stderr)
        return 1


def run_agent(_: argparse.Namespace) -> int:
    with temporary_argv([]):
        from agent.main import main as agent_main

        return agent_main()


def run_caller(args: argparse.Namespace) -> int:
    arguments = []
    if args.backend:
        arguments.extend(["--backend", args.backend])
    if args.name:
        arguments.extend(["--name", args.name])
    if args.port:
        arguments.extend(["--port", str(args.port)])
    if args.no_browser:
        arguments.append("--no-browser")

    with temporary_argv(arguments):
        from caller.launcher import main as caller_main

        caller_main()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RTC 原型统一启动入口")
    subparsers = parser.add_subparsers(dest="mode", required=True)

    server_parser = subparsers.add_parser("server", help="启动集中后端")
    server_parser.add_argument("--host", help="覆盖配置中的监听地址")
    server_parser.add_argument("--port", type=int, help="覆盖配置中的监听端口")
    server_parser.add_argument(
        "--stop",
        action="store_true",
        help="停止当前电脑上正在运行的后端",
    )
    server_parser.add_argument(
        "--create-admin",
        action="store_true",
        help="创建唯一超级管理员",
    )
    server_parser.add_argument(
        "--reset-admin-password",
        action="store_true",
        help="重置超级管理员密码",
    )
    server_parser.add_argument(
        "--admin-username",
        default="admin",
        help="管理员用户名，默认 admin",
    )
    server_parser.set_defaults(handler=run_server)

    agent_parser = subparsers.add_parser("agent", help="启动 PySide6 客服端")
    agent_parser.set_defaults(handler=run_agent)

    caller_parser = subparsers.add_parser("caller", help="启动本机呼叫页面")
    caller_parser.add_argument("--backend", help="覆盖集中后端地址")
    caller_parser.add_argument("--name", help="覆盖呼叫客户端显示名称")
    caller_parser.add_argument("--port", type=int, help="覆盖本机页面端口")
    caller_parser.add_argument(
        "--no-browser", action="store_true", help="只启动页面服务"
    )
    caller_parser.set_defaults(handler=run_caller)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
