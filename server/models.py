from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


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

