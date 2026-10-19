from __future__ import annotations

import time
from pathlib import Path
import sys

from .config import ServerConfig

TOKEN_DIR = Path(__file__).resolve().parent / "token"
if str(TOKEN_DIR) not in sys.path:
    sys.path.insert(0, str(TOKEN_DIR))

from .token.app_token import AppToken
from .token.service import Service


class RtcConfigurationError(Exception):
    pass


def make_rtc_auth(
    config: ServerConfig,
    *,
    room_id: str,
    user_id: str,
    user_name: str,
) -> dict[str, str]:
    if not config.app_id or not config.app_key:
        raise RtcConfigurationError(
            "DingRTC AppID/AppKey 未配置，请设置 RTC_APP_ID 和 RTC_APP_KEY"
        )
    builder = AppToken(
        config.app_id,
        config.app_key,
        int(time.time()) + config.rtc_token_ttl_seconds,
    )
    service = Service(room_id, user_id)
    service.add_audio_publish_privilege()
    service.add_screen_publish_privilege()
    builder.set_service(service)
    return {
        "appId": config.app_id,
        "channel": room_id,
        "uid": user_id,
        "token": builder.build(),
        "userName": user_name,
    }
