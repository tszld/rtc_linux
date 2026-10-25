from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class LoginRequest(ApiModel):
    username: str
    password: str


class RobotLoginRequest(ApiModel):
    robot_id: str = Field(alias="robotId")
    credential: str


class RobotRegisterRequest(ApiModel):
    robot_id: str = Field(alias="robotId")
    credential: str
    display_name: str = Field(alias="displayName")


class MediaStateRequest(ApiModel):
    session_id: str | None = Field(default=None, alias="sessionId")
    microphone_published: bool = Field(
        default=False,
        alias="microphonePublished",
    )
    camera_published: bool = Field(default=False, alias="cameraPublished")
    automatic_speech_paused: bool = Field(
        default=False,
        alias="automaticSpeechPaused",
    )


class LeaseRequest(ApiModel):
    requested_seconds: int | None = Field(
        default=None,
        alias="requestedSeconds",
    )


class TakeoverMicrophoneRequest(ApiModel):
    enabled: bool


class ListenRequest(ApiModel):
    enabled: bool


class CreateScenarioRequest(ApiModel):
    name: str


class CreateAgentRequest(ApiModel):
    username: str
    password: str
    display_name: str = Field(alias="displayName")
    scenario_ids: list[str] = Field(default_factory=list, alias="scenarioIds")


class SetAgentScenariosRequest(ApiModel):
    scenario_ids: list[str] = Field(alias="scenarioIds")


class CreateRobotRequest(ApiModel):
    display_name: str = Field(alias="displayName")
    scenario_id: str = Field(alias="scenarioId")


class UpdateRobotRequest(ApiModel):
    display_name: str = Field(alias="displayName")


class SetRobotScenarioRequest(ApiModel):
    scenario_id: str = Field(alias="scenarioId")


class DisconnectRequest(ApiModel):
    reason: str = "robot_disconnect"
