"""为 ARTC PoC 生成短期单参数 Token。

该工具只在开发机或业务后端运行。AppKey 从未跟踪的根目录 config.json 读取，
不得嵌入机器人端、客服端或提交到版本库。
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from project_config import CONFIG_PATH, get_section
from server.rtc import build_artc_token

DEFAULT_EXPIRES_IN_SECONDS = 3600


def load_credentials(config_path: Path | None) -> tuple[str, str]:
    config = get_section("server", config_path or CONFIG_PATH)
    app_id = str(config.get("app_id", "")).strip()
    app_key = str(config.get("app_key", "")).strip()
    if not app_id or not app_key:
        raise ValueError("项目配置 server 节缺少 app_id 或 app_key")
    return app_id, app_key


def write_token_file(path: Path, token: str) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(token)
            handle.write("\n")
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="生成 ARTC PoC 短期单参数 Token（仅开发/服务端使用）"
    )
    parser.add_argument("--channel", required=True, help="ARTC 频道 ID")
    parser.add_argument("--user-id", required=True, help="ARTC userId")
    parser.add_argument(
        "--expires-in",
        type=int,
        default=DEFAULT_EXPIRES_IN_SECONDS,
        help=f"有效期秒数，默认 {DEFAULT_EXPIRES_IN_SECONDS}",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="可选输出文件；写入时自动使用 0600 权限",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.expires_in <= 0:
        raise SystemExit("--expires-in 必须大于 0")
    app_id, app_key = load_credentials(CONFIG_PATH)
    token = build_artc_token(
        app_id,
        app_key,
        args.channel,
        args.user_id,
        expires_at=int(time.time()) + args.expires_in,
    )
    if args.output:
        write_token_file(args.output, token)
        print(f"Token 已写入 {args.output}（权限 0600）")
    else:
        print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
