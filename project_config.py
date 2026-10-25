from __future__ import annotations

import ipaddress
import json
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
CONFIG_TEMPLATE_PATH = ROOT / "config.example.json"
TAILSCALE_NETWORK = ipaddress.ip_network("100.64.0.0/10")
TOP_LEVEL_SECTIONS = {"cluster", "server", "agent", "robot"}


def load_project_config(path: Path | str | None = None) -> dict[str, Any]:
    config_path = Path(path) if path else CONFIG_PATH
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(
            f"缺少项目配置 {config_path}；请从 {CONFIG_TEMPLATE_PATH.name} 创建"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取项目配置 {config_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"项目配置 {config_path} 必须是 JSON 对象")

    unknown_sections = sorted(set(payload) - TOP_LEVEL_SECTIONS)
    if unknown_sections:
        raise ValueError(
            f"项目配置包含未知顶层字段：{', '.join(unknown_sections)}"
        )
    for name in TOP_LEVEL_SECTIONS:
        value = payload.get(name, {})
        if not isinstance(value, dict):
            raise ValueError(f"项目配置 {name} 必须是 JSON 对象")
    return payload


def reject_unknown_keys(
    section: str,
    values: dict[str, Any],
    allowed: set[str],
) -> None:
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ValueError(
            f"项目配置 {section} 包含未知字段：{', '.join(unknown)}"
        )


def get_section(
    name: str,
    path: Path | str | None = None,
) -> dict[str, Any]:
    return dict(load_project_config(path).get(name, {}))


def get_control_url(path: Path | str | None = None) -> str:
    cluster = get_section("cluster", path)
    reject_unknown_keys("cluster", cluster, {"control_url"})
    control_url = str(cluster.get("control_url", "")).strip().rstrip("/")
    if not control_url:
        raise ValueError("项目配置 cluster.control_url 不能为空")
    return control_url


def resolve_bind_host(host: str) -> str:
    if host.strip().lower() != "tailscale":
        return host
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except FileNotFoundError as exc:
        raise ValueError("host=tailscale，但未找到 tailscale 命令") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"无法读取 Tailscale IPv4 地址：{exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ValueError(
            f"tailscale ip -4 执行失败：{detail or f'退出码 {result.returncode}'}"
        )
    for line in result.stdout.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if address.version == 4 and address in TAILSCALE_NETWORK:
            return str(address)
    raise ValueError("Tailscale 未返回有效的 100.64.0.0/10 IPv4 地址")
