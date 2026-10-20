from __future__ import annotations

import uuid

from pydantic import BaseModel, Field, field_validator


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class ClientRegisterRequest(BaseModel):
    installation_uuid: str = Field(min_length=36, max_length=36)
    display_name: str = Field(min_length=1, max_length=64)

    @field_validator("installation_uuid")
    @classmethod
    def validate_installation_uuid(cls, value: str) -> str:
        try:
            return str(uuid.UUID(value))
        except ValueError as error:
            raise ValueError("安装标识必须是有效的 UUID") from error

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("显示名称不能为空")
        return value


class CreateAgentRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        if not all(character.isalnum() or character in "-_" for character in value):
            raise ValueError("用户名只能包含字母、数字、短横线和下划线")
        return value

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("显示名称不能为空")
        return value


class DisplayNameRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=64)

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("显示名称不能为空")
        return value


class ResetPasswordRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class ClientStatusRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=256)


class CreateCallRequest(BaseModel):
    client_id: str = Field(min_length=1, max_length=64)
    client_name: str = Field(min_length=1, max_length=64)

    @field_validator("client_id")
    @classmethod
    def validate_client_id(cls, value: str) -> str:
        if not all(character.isalnum() or character in "-_" for character in value):
            raise ValueError("客户端编号只能包含字母、数字、短横线和下划线")
        return value


class MessageResponse(BaseModel):
    ok: bool = True
    message: str = ""
