from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config.json"
SKIPPED_PARTS = {
    ".git",
    "vendor",
    "__pycache__",
    ".tmp-agent-web",
    ".tmp-render",
    ".tmp-tests",
    "dist",
    "build",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="扫描仓库中的配置密钥泄漏")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    if not isinstance(config, dict):
        print(json.dumps({"ok": False, "error": "配置必须是 JSON 对象"}))
        return 2

    server = config.get("server", {})
    robot = config.get("robot", {})
    app_key = str(server.get("app_key", "")) if isinstance(server, dict) else ""
    robot_credential = (
        str(robot.get("credential", "")) if isinstance(robot, dict) else ""
    )
    if not app_key and not robot_credential:
        print(
            json.dumps(
                {"ok": False, "error": "server.app_key 和 robot.credential 均为空"},
                ensure_ascii=False,
            )
        )
        return 2

    hits = []
    credential_hits = []
    scanned = 0
    for path in ROOT.rglob("*"):
        if (
            not path.is_file()
            or path.resolve() == config_path
            or any(part in SKIPPED_PARTS for part in path.parts)
        ):
            continue
        try:
            if path.stat().st_size > 2_000_000:
                continue
            body = path.read_bytes()
        except OSError:
            continue
        scanned += 1
        if app_key and app_key.encode("utf-8") in body:
            hits.append(str(path.relative_to(ROOT)))
        if robot_credential and robot_credential.encode("utf-8") in body:
            credential_hits.append(str(path.relative_to(ROOT)))
    result = {
        "ok": not hits and not credential_hits,
        "scannedFiles": scanned,
        "leakPaths": hits,
        "credentialLeakPaths": credential_hits,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if not hits and not credential_hits else 1


if __name__ == "__main__":
    raise SystemExit(main())
