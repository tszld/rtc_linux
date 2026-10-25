from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from project_config import ROOT, get_section, reject_unknown_keys


@dataclass(frozen=True)
class ServerConfig:
    app_id: str
    app_key: str
    db_path: Path
    host: str = "0.0.0.0"
    port: int = 8000
    access_token_ttl_seconds: int = 12 * 60 * 60
    rtc_token_ttl_seconds: int = 10 * 60
    ws_ticket_ttl_seconds: int = 30
    presence_degraded_seconds: int = 15
    presence_timeout_seconds: int = 30
    takeover_lease_seconds: int = 60
    max_takeover_lease_seconds: int = 300
    cors_origins: tuple[str, ...] = ()

    @property
    def rtc_configured(self) -> bool:
        return bool(self.app_id and self.app_key)

    @classmethod
    def load(
        cls,
        config_path: Path | str | None = None,
    ) -> ServerConfig:
        values: dict[str, Any] = get_section("server", config_path)
        reject_unknown_keys(
            "server",
            values,
            {
                "app_id",
                "app_key",
                "db_path",
                "host",
                "port",
                "access_token_ttl_seconds",
                "token_ttl_seconds",
                "ws_ticket_ttl_seconds",
                "presence_degraded_seconds",
                "presence_timeout_seconds",
                "takeover_lease_seconds",
                "max_takeover_lease_seconds",
                "cors_origins",
            },
        )

        def text_value(name: str, default: str = "") -> str:
            return str(values.get(name, default)).strip()

        def int_value(name: str, default: int) -> int:
            raw = values.get(name, default)
            try:
                return int(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"server.{name} 必须是整数") from exc

        db_value = text_value("db_path", "server/data/artc.db")
        db_path = Path(db_value).expanduser()
        if not db_path.is_absolute():
            db_path = (ROOT / db_path).resolve()

        raw_origins = values.get("cors_origins", [])
        if not isinstance(raw_origins, list):
            raise ValueError("server.cors_origins 必须是字符串数组")
        cors_origins = tuple(str(item).strip() for item in raw_origins)

        config = cls(
            app_id=text_value("app_id"),
            app_key=text_value("app_key"),
            db_path=db_path,
            host=text_value("host", "0.0.0.0"),
            port=int_value("port", 8000),
            access_token_ttl_seconds=int_value(
                "access_token_ttl_seconds",
                12 * 60 * 60,
            ),
            rtc_token_ttl_seconds=int_value("token_ttl_seconds", 10 * 60),
            ws_ticket_ttl_seconds=int_value("ws_ticket_ttl_seconds", 30),
            presence_degraded_seconds=int_value(
                "presence_degraded_seconds",
                15,
            ),
            presence_timeout_seconds=int_value(
                "presence_timeout_seconds",
                30,
            ),
            takeover_lease_seconds=int_value("takeover_lease_seconds", 60),
            max_takeover_lease_seconds=int_value(
                "max_takeover_lease_seconds",
                300,
            ),
            cors_origins=cors_origins,
        )
        config.validate()
        return config

    def validate(self) -> None:
        integer_fields = {
            "port": self.port,
            "access_token_ttl_seconds": self.access_token_ttl_seconds,
            "rtc_token_ttl_seconds": self.rtc_token_ttl_seconds,
            "ws_ticket_ttl_seconds": self.ws_ticket_ttl_seconds,
            "presence_degraded_seconds": self.presence_degraded_seconds,
            "presence_timeout_seconds": self.presence_timeout_seconds,
            "takeover_lease_seconds": self.takeover_lease_seconds,
            "max_takeover_lease_seconds": self.max_takeover_lease_seconds,
        }
        for name, value in integer_fields.items():
            if value <= 0:
                raise ValueError(f"{name} 必须大于 0")
        if self.port > 65535:
            raise ValueError("port 不能大于 65535")
        if not self.host:
            raise ValueError("server.host 不能为空")
        if self.presence_degraded_seconds >= self.presence_timeout_seconds:
            raise ValueError(
                "presence_degraded_seconds 必须小于 presence_timeout_seconds"
            )
        if self.takeover_lease_seconds > self.max_takeover_lease_seconds:
            raise ValueError("默认接管租约不能大于最大租约")
        for origin in self.cors_origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(f"cors_origins 包含无效来源：{origin}")
