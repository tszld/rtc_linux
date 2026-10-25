from __future__ import annotations

import base64
import hashlib
import json
import re
import time

from .config import ServerConfig


IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class RtcConfigurationError(RuntimeError):
    pass


def validate_identifier(value: str, label: str) -> str:
    if not IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(
            f"{label} 只能包含英文字母、数字、短划线或下划线，长度为 1-64"
        )
    return value


def build_artc_token(
    app_id: str,
    app_key: str,
    channel_id: str,
    user_id: str,
    *,
    expires_at: int,
    nonce: str = "",
) -> str:
    if not app_id:
        raise ValueError("ARTC AppID 不能为空")
    if not app_key:
        raise ValueError("ARTC AppKey 不能为空")
    validate_identifier(channel_id, "ChannelID")
    validate_identifier(user_id, "UserId")
    signature_source = (
        f"{app_id}{app_key}{channel_id}{user_id}{nonce}{expires_at}"
    )
    signature = hashlib.sha256(signature_source.encode("utf-8")).hexdigest()
    payload = {
        "appid": app_id,
        "channelid": channel_id,
        "userid": user_id,
        "nonce": nonce,
        "timestamp": expires_at,
        "token": signature,
    }
    return base64.b64encode(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).decode("ascii")


def make_rtc_auth(
    config: ServerConfig,
    *,
    channel_id: str,
    user_id: str,
    user_name: str,
) -> dict[str, str | int]:
    if not config.rtc_configured:
        raise RtcConfigurationError("ARTC AppID/AppKey 未配置")
    expires_at = int(time.time()) + config.rtc_token_ttl_seconds
    token = build_artc_token(
        config.app_id,
        config.app_key,
        channel_id,
        user_id,
        expires_at=expires_at,
    )
    return {
        "appId": config.app_id,
        "channel": channel_id,
        "userId": user_id,
        "userName": user_name,
        "token": token,
        "expiresAt": expires_at,
    }
