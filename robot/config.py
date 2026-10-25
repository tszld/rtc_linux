from __future__ import annotations

import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from project_config import (
    ROOT,
    get_control_url,
    get_section,
    reject_unknown_keys,
)


@dataclass(frozen=True)
class RobotConfig:
    backend_url: str
    robot_id: str
    credential: str
    sdk_root: Path
    log_dir: Path
    display_name: str = ""
    heartbeat_seconds: float = 5
    reconnect_seconds: float = 2
    auto_start_session: bool = True
    exit_when_session_ends: bool = True
    audio_output: bool = True
    test_tone_frequency: float | None = None
    camera_enabled: bool = False

    @classmethod
    def load(
        cls,
        config_path: Path | str | None = None,
    ) -> RobotConfig:
        values: dict[str, Any] = get_section("robot", config_path)
        reject_unknown_keys(
            "robot",
            values,
            {
                "robot_id",
                "credential",
                "sdk_root",
                "log_dir",
                "heartbeat_seconds",
                "reconnect_seconds",
                "auto_start_session",
                "exit_when_session_ends",
                "audio_output",
                "test_tone_frequency",
                "camera_enabled",
            },
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

        robot_id = str(values.get("robot_id", "")).strip()
        credential = str(values.get("credential", "")).strip()
        if not robot_id or not credential:
            raise ValueError(
                "robot.robot_id 和 robot.credential 不能为空"
            )

        def resolve_path(name: str, default: str) -> Path:
            raw = Path(str(values.get(name, default))).expanduser()
            return raw if raw.is_absolute() else (ROOT / raw).resolve()

        def number(name: str, default: float) -> float:
            try:
                return float(values.get(name, default))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"robot.{name} 必须是数字") from exc

        def boolean(name: str, default: bool) -> bool:
            raw = values.get(name, default)
            if not isinstance(raw, bool):
                raise ValueError(f"robot.{name} 必须是布尔值")
            return raw

        tone_value = values.get("test_tone_frequency")
        if tone_value in (None, ""):
            tone_frequency = None
        else:
            try:
                tone_frequency = float(tone_value)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "robot.test_tone_frequency 必须是数字或 null"
                ) from exc

        config = cls(
            backend_url=backend_url,
            robot_id=robot_id,
            credential=credential,
            sdk_root=resolve_path(
                "sdk_root",
                "robot/vendor/AliRTCSDK_Linux-7.11.0",
            ),
            log_dir=resolve_path("log_dir", "/tmp/artc-robot-logs"),
            display_name=str(
                values.get("display_name") or socket.gethostname()
            ).strip()[:128],
            heartbeat_seconds=number("heartbeat_seconds", 5),
            reconnect_seconds=number("reconnect_seconds", 2),
            auto_start_session=boolean("auto_start_session", True),
            exit_when_session_ends=boolean(
                "exit_when_session_ends",
                True,
            ),
            audio_output=boolean("audio_output", True),
            test_tone_frequency=tone_frequency,
            camera_enabled=boolean("camera_enabled", False),
        )
        if config.heartbeat_seconds <= 0 or config.reconnect_seconds <= 0:
            raise ValueError("机器人心跳和重连间隔必须大于 0")
        if config.camera_enabled:
            raise ValueError("当前 Linux PoC 尚未启用摄像头采集")
        return config
