from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from project_config import get_control_url, get_section, reject_unknown_keys


@dataclass(frozen=True)
class AgentWebConfig:
    backend_url: str
    host: str = "127.0.0.1"
    port: int = 8100
    auto_open_browser: bool = True

    @classmethod
    def load(
        cls,
        config_path: Path | str | None = None,
    ) -> AgentWebConfig:
        values: dict[str, Any] = get_section("agent", config_path)
        reject_unknown_keys(
            "agent",
            values,
            {"host", "port", "auto_open_browser"},
        )
        backend_url = get_control_url(config_path)
        parsed_backend = urlsplit(backend_url)
        if (
            parsed_backend.scheme not in {"http", "https"}
            or not parsed_backend.netloc
        ):
            raise ValueError(
                "cluster.control_url 必须是有效的 http:// 或 https:// 地址"
            )

        host = str(values.get("host", "127.0.0.1")).strip()
        if not host:
            raise ValueError("agent.host 不能为空")

        try:
            port = int(values.get("port", 8100))
        except (TypeError, ValueError) as exc:
            raise ValueError("agent.port 必须是整数") from exc
        if port <= 0 or port > 65535:
            raise ValueError("agent.port 必须在 1-65535 之间")

        raw_open_browser = values.get("auto_open_browser", True)
        if not isinstance(raw_open_browser, bool):
            raise ValueError("agent.auto_open_browser 必须是布尔值")

        return cls(
            backend_url=backend_url,
            host=host,
            port=port,
            auto_open_browser=raw_open_browser,
        )
