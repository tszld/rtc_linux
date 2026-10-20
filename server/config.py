from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parent.parent
SERVER_DIR = ROOT_DIR / "server"


def resolve_project_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT_DIR / path
    return path.resolve()


@dataclass
class ServerConfig:
    app_id: str = "cn4zs9jm"
    app_key: str = ""
    db_path: Path = Path("server/data/rtc.db")
    host: str = "0.0.0.0"
    port: int = 8000
    rtc_token_ttl_seconds: int = 3600
    agent_presence_ttl_seconds: int = 20
    ws_ticket_ttl_seconds: int = 60
    connecting_timeout_seconds: int = 45
    interrupted_timeout_seconds: int = 15
    created_timeout_seconds: int = 120
    cors_origin_regex: str = r"http://(127\.0\.0\.1|localhost)(:\d+)?"
    seed_agents: list[dict[str, str]] = field(
        default_factory=lambda: [
            {
                "username": "agent01",
                "password": "agent123",
                "display_name": "客服 01",
            },
            {
                "username": "agent02",
                "password": "agent123",
                "display_name": "客服 02",
            },
        ]
    )

    def __post_init__(self) -> None:
        self.db_path = resolve_project_path(self.db_path)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_config() -> ServerConfig:
    config = ServerConfig()
    for candidate in (SERVER_DIR / "config.json", SERVER_DIR / "config.local.json"):
        values = _load_json(candidate)
        for key, value in values.items():
            if hasattr(config, key):
                setattr(config, key, value)

    config.app_id = os.getenv("RTC_APP_ID", config.app_id)
    config.app_key = os.getenv("RTC_APP_KEY", config.app_key)
    config.host = os.getenv("RTC_HOST", config.host)
    config.port = int(os.getenv("RTC_PORT", str(config.port)))
    config.db_path = resolve_project_path(
        os.getenv("RTC_DB_PATH", str(config.db_path))
    )
    config.rtc_token_ttl_seconds = int(
        os.getenv("RTC_TOKEN_TTL_SECONDS", str(config.rtc_token_ttl_seconds))
    )
    config.agent_presence_ttl_seconds = int(
        os.getenv("RTC_AGENT_PRESENCE_TTL_SECONDS", str(config.agent_presence_ttl_seconds))
    )
    config.ws_ticket_ttl_seconds = int(
        os.getenv("RTC_WS_TICKET_TTL_SECONDS", str(config.ws_ticket_ttl_seconds))
    )
    config.connecting_timeout_seconds = int(
        os.getenv(
            "RTC_CONNECTING_TIMEOUT_SECONDS", str(config.connecting_timeout_seconds)
        )
    )
    config.interrupted_timeout_seconds = int(
        os.getenv(
            "RTC_INTERRUPTED_TIMEOUT_SECONDS", str(config.interrupted_timeout_seconds)
        )
    )
    config.created_timeout_seconds = int(
        os.getenv("RTC_CREATED_TIMEOUT_SECONDS", str(config.created_timeout_seconds))
    )
    config.cors_origin_regex = os.getenv(
        "RTC_CORS_ORIGIN_REGEX", config.cors_origin_regex
    )

    seed_json = os.getenv("RTC_AGENT_SEED_JSON")
    if seed_json:
        config.seed_agents = json.loads(seed_json)
    return config
