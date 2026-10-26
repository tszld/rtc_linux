from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .admin import (
    create_agent,
    create_robot,
    delete_robot,
    list_admin_robots,
    list_agents,
    rename_robot,
    rotate_robot_credential,
    set_agent_enabled,
    set_robot_approval,
    set_robot_enabled,
    set_robot_scenario,
)
from .audit import insert_audit, list_audit_events
from .auth import (
    AuthError,
    Principal,
    authenticate_agent,
    authenticate_robot,
    consume_ws_ticket,
    issue_ws_ticket,
    resolve_access_token,
    revoke_access_token,
)
from .config import ServerConfig
from .db import Database
from .errors import ServiceError
from .models import (
    CreateAgentRequest,
    CreateRobotRequest,
    CreateScenarioRequest,
    DisconnectRequest,
    LeaseRequest,
    ListenRequest,
    LoginRequest,
    MediaStateRequest,
    PlaybackVolumeRequest,
    RobotLoginRequest,
    RobotRegisterRequest,
    SetAgentScenariosRequest,
    SetRobotScenarioRequest,
    TakeoverMicrophoneRequest,
    UpdateRobotRequest,
)
from .presence import (
    heartbeat,
    mark_robot_offline,
    set_media_state,
    sweep_presence,
)
from .robots import (
    ensure_robot_access,
    get_robot,
    list_visible_robots,
    register_robot,
    robot_self_payload,
    robot_to_dict,
)
from .rtc import RtcConfigurationError, make_rtc_auth
from .scenarios import (
    create_scenario,
    ensure_scenario_access,
    list_scenarios_for_principal,
    set_agent_scenarios,
    set_scenario_enabled,
)
from .security import utc_now_text
from .sessions import (
    claim_session,
    end_session,
    end_sessions_for_offline_robot,
    ensure_session_claim,
    ensure_session_access,
    get_session,
    list_sessions_for_principal,
    release_session_claim,
    set_listen_request,
    start_session,
)
from .takeover import (
    acquire_lease,
    expire_leases,
    get_active_lease,
    lease_to_dict,
    release_all_for_agent,
    release_lease,
    renew_lease,
    set_lease_microphone,
)
from .ws_manager import WebSocketManager


bearer_scheme = HTTPBearer(auto_error=False)
logger = logging.getLogger(__name__)


class LoginRateLimiter:
    def __init__(self, *, attempts: int = 10, window_seconds: int = 60) -> None:
        self.attempts = attempts
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str) -> None:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            events = self._events[key]
            while events and events[0] < cutoff:
                events.popleft()
            if len(events) >= self.attempts:
                raise HTTPException(status_code=429, detail="登录尝试过多，请稍后重试")
            events.append(now)


def _camel_session(raw: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "id": "id",
        "robot_id": "robotId",
        "status": "status",
        "started_at": "startedAt",
        "ended_at": "endedAt",
        "end_reason": "endReason",
    }
    return {
        output: raw[field]
        for field, output in mapping.items()
        if field in raw
    }


def create_app(
    config: ServerConfig | None = None,
    db: Database | None = None,
) -> FastAPI:
    resolved_config = config or ServerConfig.load()
    resolved_db = db or Database(resolved_config.db_path)
    resolved_db.initialize()
    manager = WebSocketManager()
    login_limiter = LoginRateLimiter()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        stop_event = asyncio.Event()

        async def sweep_loop() -> None:
            while not stop_event.is_set():
                try:
                    expired = await asyncio.to_thread(expire_leases, resolved_db)
                    for lease in expired:
                        await _broadcast_takeover_event(
                            resolved_db,
                            manager,
                            lease,
                            "takeover_expired",
                        )
                    status_events = await asyncio.to_thread(
                        sweep_presence,
                        resolved_db,
                        config=resolved_config,
                    )
                    for status_event in status_events:
                        await _broadcast_robot_status(
                            resolved_db,
                            manager,
                            status_event,
                        )
                        if status_event["to"] == "offline":
                            ended = await asyncio.to_thread(
                                end_sessions_for_offline_robot,
                                resolved_db,
                                robot_id=status_event["robotId"],
                                reason="presence_timeout",
                            )
                            for item in ended:
                                await _broadcast_session_end(
                                    resolved_db,
                                    manager,
                                    item,
                                )
                except Exception:
                    logger.exception("后台状态清理任务失败")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=2)
                except asyncio.TimeoutError:
                    pass

        task = asyncio.create_task(sweep_loop())
        app.state.sweep_task = task
        try:
            yield
        finally:
            stop_event.set()
            await task

    app = FastAPI(
        title="ARTC 业务协作平台",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.config = resolved_config
    app.state.db = resolved_db
    app.state.ws_manager = manager
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost",
            "http://127.0.0.1",
            *resolved_config.cors_origins,
        ],
        allow_origin_regex=r"^http://(127\.0\.0\.1|localhost)(:\d+)?$",
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(AuthError)
    async def auth_error_handler(
        _request: Request,
        exc: AuthError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": str(exc)},
        )

    @app.exception_handler(ServiceError)
    async def service_error_handler(
        _request: Request,
        exc: ServiceError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": str(exc)},
        )

    @app.exception_handler(RtcConfigurationError)
    async def rtc_error_handler(
        _request: Request,
        exc: RtcConfigurationError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": str(exc)},
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(
        _request: Request,
        exc: ValueError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"detail": str(exc)},
        )

    def credentials_value(
        credentials: HTTPAuthorizationCredentials | None,
    ) -> str:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise AuthError("缺少 Bearer Token")
        return credentials.credentials

    async def current_principal(
        credentials: HTTPAuthorizationCredentials | None = Depends(
            bearer_scheme
        ),
    ) -> Principal:
        return resolve_access_token(
            resolved_db,
            credentials_value(credentials),
        )

    async def require_agent(
        principal: Principal = Depends(current_principal),
    ) -> Principal:
        if principal.principal_type != "agent":
            raise AuthError("需要客服身份", 403)
        return principal

    async def require_admin(
        principal: Principal = Depends(require_agent),
    ) -> Principal:
        if principal.role != "admin":
            raise AuthError("需要管理员权限", 403)
        return principal

    async def require_robot(
        principal: Principal = Depends(current_principal),
    ) -> Principal:
        if principal.principal_type != "robot":
            raise AuthError("需要机器人身份", 403)
        return principal

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "rtcConfigured": resolved_config.rtc_configured,
            "serverTime": utc_now_text(),
        }

    @app.post("/api/auth/login")
    async def auth_login(
        body: LoginRequest,
        request: Request,
    ) -> dict[str, Any]:
        login_limiter.hit(request.client.host if request.client else "unknown")
        return authenticate_agent(
            resolved_db,
            username=body.username,
            password=body.password,
            ttl_seconds=resolved_config.access_token_ttl_seconds,
        )

    @app.post("/api/admin/login")
    async def admin_login(
        body: LoginRequest,
        request: Request,
    ) -> dict[str, Any]:
        login_limiter.hit(request.client.host if request.client else "unknown")
        return authenticate_agent(
            resolved_db,
            username=body.username,
            password=body.password,
            ttl_seconds=resolved_config.access_token_ttl_seconds,
            require_admin=True,
        )

    @app.get("/api/auth/me")
    async def auth_me(
        principal: Principal = Depends(current_principal),
    ) -> dict[str, Any]:
        return principal.to_public_dict()

    @app.post("/api/auth/logout")
    async def auth_logout(
        credentials: HTTPAuthorizationCredentials | None = Depends(
            bearer_scheme
        ),
    ) -> dict[str, bool]:
        revoke_access_token(resolved_db, credentials_value(credentials))
        return {"ok": True}

    @app.get("/api/auth/ws-ticket")
    async def agent_ws_ticket(
        principal: Principal = Depends(require_agent),
    ) -> dict[str, str]:
        return issue_ws_ticket(
            resolved_db,
            principal=principal,
            ttl_seconds=resolved_config.ws_ticket_ttl_seconds,
        )

    @app.post("/api/robot/register")
    async def robot_register(
        body: RobotRegisterRequest,
    ) -> dict[str, Any]:
        return register_robot(
            resolved_db,
            robot_id=body.robot_id,
            credential=body.credential,
            display_name=body.display_name,
        )

    @app.post("/api/robot/login")
    async def robot_login(body: RobotLoginRequest) -> dict[str, Any]:
        return authenticate_robot(
            resolved_db,
            robot_id=body.robot_id,
            credential=body.credential,
            ttl_seconds=resolved_config.access_token_ttl_seconds,
        )

    @app.get("/api/robot/me")
    async def robot_me(
        principal: Principal = Depends(require_robot),
    ) -> dict[str, Any]:
        with resolved_db.connection() as connection:
            return robot_self_payload(connection, principal.id)

    @app.get("/api/robot/ws-ticket")
    async def robot_ws_ticket(
        principal: Principal = Depends(require_robot),
    ) -> dict[str, str]:
        return issue_ws_ticket(
            resolved_db,
            principal=principal,
            ttl_seconds=resolved_config.ws_ticket_ttl_seconds,
        )

    @app.post("/api/robot/heartbeat")
    async def robot_heartbeat(
        principal: Principal = Depends(require_robot),
    ) -> dict[str, Any]:
        result = heartbeat(resolved_db, robot_id=principal.id)
        result["presenceTimeoutSeconds"] = (
            resolved_config.presence_timeout_seconds
        )
        if result["previousStatus"] != "online":
            await _broadcast_robot_status(
                resolved_db,
                manager,
                {
                    "robotId": principal.id,
                    "from": result["previousStatus"],
                    "to": "online",
                    "lastSeenAt": result["lastSeenAt"],
                    "occurredAt": utc_now_text(),
                },
            )
        return result

    @app.post("/api/robot/rtc-token")
    async def robot_rtc_token(
        principal: Principal = Depends(require_robot),
    ) -> dict[str, Any]:
        robot = get_robot(resolved_db, principal.id)
        if robot["status"] == "offline":
            raise ServiceError("机器人离线，不能获取 ARTC Token", 409)
        auth = make_rtc_auth(
            resolved_config,
            channel_id=robot["channel_id"],
            user_id=f"robot-{robot['id']}",
            user_name=robot["display_name"],
        )
        with resolved_db.transaction() as connection:
            insert_audit(
                connection,
                actor_type="robot",
                actor_id=principal.id,
                action="rtc_token_issued",
                target_type="robot",
                target_id=principal.id,
            )
        return auth

    @app.post("/api/robot/media-state")
    async def robot_media_state(
        body: MediaStateRequest,
        principal: Principal = Depends(require_robot),
    ) -> dict[str, Any]:
        media = set_media_state(
            resolved_db,
            robot_id=principal.id,
            session_id=body.session_id,
            microphone_published=body.microphone_published,
            camera_published=body.camera_published,
            automatic_speech_paused=body.automatic_speech_paused,
        )
        scenario_id, agent_ids = _scenario_and_agent_ids_for_robot(
            resolved_db,
            principal.id,
        )
        await manager.broadcast_to_agents(
            agent_ids,
            {"type": "media_state", **media},
        )
        return media

    @app.post("/api/robot/sessions/start")
    async def robot_session_start(
        principal: Principal = Depends(require_robot),
    ) -> dict[str, Any]:
        session = start_session(resolved_db, robot_id=principal.id)
        scenario_id, agent_ids = _scenario_and_agent_ids_for_robot(
            resolved_db,
            principal.id,
        )
        message = {
            "type": "session_started",
            "session": session,
            "occurredAt": utc_now_text(),
        }
        await manager.broadcast_to_agents(agent_ids, message)
        await manager.send_to_robot(principal.id, message)
        return session

    @app.post("/api/robot/sessions/{session_id}/end")
    async def robot_session_end(
        session_id: str,
        principal: Principal = Depends(require_robot),
    ) -> dict[str, Any]:
        result = end_session(
            resolved_db,
            session_id=session_id,
            robot_id=principal.id,
            reason="robot_ended",
        )
        await _broadcast_session_end(resolved_db, manager, result)
        return result["session"]

    @app.post("/api/robot/disconnect")
    async def robot_disconnect(
        body: DisconnectRequest,
        principal: Principal = Depends(require_robot),
    ) -> dict[str, Any]:
        status_event = mark_robot_offline(
            resolved_db,
            robot_id=principal.id,
            reason=body.reason,
        )
        if status_event is not None:
            await _broadcast_robot_status(
                resolved_db,
                manager,
                status_event,
            )
        ended = end_sessions_for_offline_robot(
            resolved_db,
            robot_id=principal.id,
            reason=body.reason,
        )
        for item in ended:
            await _broadcast_session_end(resolved_db, manager, item)
        return {"ok": True}

    @app.get("/api/scenarios")
    async def scenarios(
        principal: Principal = Depends(require_agent),
    ) -> list[dict[str, Any]]:
        return list_scenarios_for_principal(
            resolved_db,
            actor=principal,
        )

    @app.get("/api/robots")
    async def visible_robots(
        principal: Principal = Depends(require_agent),
    ) -> list[dict[str, Any]]:
        return list_visible_robots(resolved_db, actor=principal)

    @app.get("/api/robot-sessions")
    async def robot_sessions(
        include_ended: bool = Query(default=False, alias="includeEnded"),
        principal: Principal = Depends(require_agent),
    ) -> list[dict[str, Any]]:
        return list_sessions_for_principal(
            resolved_db,
            actor=principal,
            include_ended=include_ended,
        )

    @app.post("/api/robot-sessions/{session_id}/claim")
    async def claim_robot_session(
        session_id: str,
        principal: Principal = Depends(require_agent),
    ) -> dict[str, Any]:
        session = claim_session(
            resolved_db,
            actor=principal,
            session_id=session_id,
        )
        await _broadcast_session_claim(
            resolved_db,
            manager,
            session,
            "session_claimed",
        )
        return session

    @app.post("/api/robot-sessions/{session_id}/claim/release")
    async def release_robot_session_claim(
        session_id: str,
        principal: Principal = Depends(require_agent),
    ) -> dict[str, Any]:
        result = release_session_claim(
            resolved_db,
            actor=principal,
            session_id=session_id,
        )
        if result["releasedLease"] is not None:
            await _broadcast_takeover_event(
                resolved_db,
                manager,
                result["releasedLease"],
                "takeover_released",
            )
        if result["listenerUpdate"] is not None:
            await _broadcast_listen_state(
                resolved_db,
                manager,
                result["listenerUpdate"],
            )
        await _broadcast_session_claim(
            resolved_db,
            manager,
            result["session"],
            "session_released",
        )
        return result["session"]

    @app.post("/api/robot-sessions/{session_id}/end")
    async def agent_end_robot_session(
        session_id: str,
        principal: Principal = Depends(require_agent),
    ) -> dict[str, Any]:
        with resolved_db.connection() as connection:
            session = ensure_session_access(
                connection,
                actor=principal,
                session_id=session_id,
            )
            ensure_session_claim(
                connection,
                actor=principal,
                session=session,
            )
        result = end_session(
            resolved_db,
            session_id=session_id,
            robot_id=None,
            reason="agent_ended",
            actor=principal,
        )
        await _broadcast_session_end(resolved_db, manager, result)
        return result["session"]

    @app.post("/api/robot-sessions/{session_id}/viewer-token")
    async def viewer_token(
        session_id: str,
        principal: Principal = Depends(require_agent),
    ) -> dict[str, Any]:
        with resolved_db.connection() as connection:
            session = ensure_session_access(
                connection,
                actor=principal,
                session_id=session_id,
            )
            if session["status"] == "ended":
                raise ServiceError("业务会话已经结束", 409)
            if session["robot_status"] == "offline":
                raise ServiceError("机器人离线", 409)
            ensure_session_claim(
                connection,
                actor=principal,
                session=session,
            )
        auth = make_rtc_auth(
            resolved_config,
            channel_id=session["channel_id"],
            user_id=f"agent-{principal.id}",
            user_name=principal.display_name,
        )
        with resolved_db.transaction() as connection:
            insert_audit(
                connection,
                actor_type="agent",
                actor_id=principal.id,
                action="viewer_token_issued",
                target_type="robot_session",
                target_id=session_id,
            )
        return {**auth, "sessionId": session_id}

    @app.post("/api/robot-sessions/{session_id}/talk-token")
    async def talk_token(
        session_id: str,
        principal: Principal = Depends(require_agent),
    ) -> dict[str, Any]:
        with resolved_db.connection() as connection:
            session = ensure_session_access(
                connection,
                actor=principal,
                session_id=session_id,
            )
            if session["status"] == "ended":
                raise ServiceError("业务会话已经结束", 409)
            ensure_session_claim(
                connection,
                actor=principal,
                session=session,
            )

        active_lease = get_active_lease(
            resolved_db,
            robot_id=session["robot_id"],
        )
        if (
            active_lease is None
            or active_lease["sessionId"] != session_id
            or active_lease["agentId"] != principal.id
        ):
            raise ServiceError("没有有效的接管租约，不能获取麦克风 Token", 409)

        auth = make_rtc_auth(
            resolved_config,
            channel_id=session["channel_id"],
            user_id=f"agent-{principal.id}-talk",
            user_name=f"{principal.display_name} 语音",
        )
        with resolved_db.transaction() as connection:
            insert_audit(
                connection,
                actor_type="agent",
                actor_id=principal.id,
                action="talk_token_issued",
                target_type="robot_session",
                target_id=session_id,
                metadata={"leaseId": active_lease["id"]},
            )
        return {
            **auth,
            "sessionId": session_id,
            "leaseId": active_lease["id"],
            "role": "talk",
        }

    @app.post("/api/robot-sessions/{session_id}/takeover-lease")
    async def takeover_acquire(
        session_id: str,
        body: LeaseRequest,
        principal: Principal = Depends(require_agent),
    ) -> dict[str, Any]:
        result = acquire_lease(
            resolved_db,
            actor=principal,
            session_id=session_id,
            requested_seconds=(
                body.requested_seconds
                if body.requested_seconds is not None
                else resolved_config.takeover_lease_seconds
            ),
            max_seconds=resolved_config.max_takeover_lease_seconds,
        )
        for lease in result["expiredLeases"]:
            await _broadcast_takeover_event(
                resolved_db,
                manager,
                lease,
                "takeover_expired",
            )
        await _broadcast_takeover_event(
            resolved_db,
            manager,
            result["lease"],
            "agent_takeover",
        )
        return result["lease"]

    @app.post("/api/robot-sessions/{session_id}/takeover-lease/renew")
    async def takeover_renew(
        session_id: str,
        body: LeaseRequest,
        principal: Principal = Depends(require_agent),
    ) -> dict[str, Any]:
        result = renew_lease(
            resolved_db,
            actor=principal,
            session_id=session_id,
            requested_seconds=(
                body.requested_seconds
                if body.requested_seconds is not None
                else resolved_config.takeover_lease_seconds
            ),
            max_seconds=resolved_config.max_takeover_lease_seconds,
        )
        for lease in result["expiredLeases"]:
            await _broadcast_takeover_event(
                resolved_db,
                manager,
                lease,
                "takeover_expired",
            )
        await _broadcast_takeover_event(
            resolved_db,
            manager,
            result["lease"],
            "takeover_renewed",
        )
        return result["lease"]

    @app.post(
        "/api/robot-sessions/{session_id}/takeover-lease/microphone"
    )
    async def takeover_microphone(
        session_id: str,
        body: TakeoverMicrophoneRequest,
        principal: Principal = Depends(require_agent),
    ) -> dict[str, Any]:
        result = set_lease_microphone(
            resolved_db,
            actor=principal,
            session_id=session_id,
            enabled=body.enabled,
        )
        for lease in result["expiredLeases"]:
            await _broadcast_takeover_event(
                resolved_db,
                manager,
                lease,
                "takeover_expired",
            )
        await _broadcast_takeover_event(
            resolved_db,
            manager,
            result["lease"],
            "takeover_microphone_changed",
        )
        return result["lease"]

    @app.post("/api/robot-sessions/{session_id}/takeover-lease/release")
    async def takeover_release(
        session_id: str,
        principal: Principal = Depends(require_agent),
    ) -> dict[str, Any]:
        result = release_lease(
            resolved_db,
            actor=principal,
            session_id=session_id,
        )
        for lease in result["expiredLeases"]:
            await _broadcast_takeover_event(
                resolved_db,
                manager,
                lease,
                "takeover_expired",
            )
        await _broadcast_takeover_event(
            resolved_db,
            manager,
            result["lease"],
            "takeover_released",
        )
        return result["lease"]

    @app.post("/api/robot-sessions/{session_id}/listen")
    async def listen(
        session_id: str,
        body: ListenRequest,
        principal: Principal = Depends(require_agent),
    ) -> dict[str, Any]:
        result = set_listen_request(
            resolved_db,
            actor=principal,
            session_id=session_id,
            enabled=body.enabled,
        )
        await _broadcast_listen_state(resolved_db, manager, result)
        return result

    @app.post(
        "/api/robot-sessions/{session_id}/agent-playback-volume"
    )
    async def set_agent_playback_volume(
        session_id: str,
        body: PlaybackVolumeRequest,
        principal: Principal = Depends(require_agent),
    ) -> dict[str, Any]:
        with resolved_db.connection() as connection:
            session = ensure_session_access(
                connection,
                actor=principal,
                session_id=session_id,
            )
            ensure_session_claim(
                connection,
                actor=principal,
                session=session,
            )
            if session["status"] == "ended":
                raise ServiceError("业务会话已经结束", 409)

        delivered = await manager.is_robot_connected(session["robot_id"])
        await manager.send_to_robot(
            session["robot_id"],
            {
                "type": "agent_playback_volume_changed",
                "robotId": session["robot_id"],
                "sessionId": session_id,
                "volume": body.volume,
                "occurredAt": utc_now_text(),
            },
        )
        return {
            "sessionId": session_id,
            "volume": body.volume,
            "delivered": delivered,
        }

    @app.get("/api/admin/scenarios")
    async def admin_scenarios(
        principal: Principal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return list_scenarios_for_principal(resolved_db, actor=principal)

    @app.post("/api/admin/scenarios")
    async def admin_create_scenario(
        body: CreateScenarioRequest,
        principal: Principal = Depends(require_admin),
    ) -> dict[str, Any]:
        return create_scenario(
            resolved_db,
            actor=principal,
            name=body.name,
        )

    @app.post("/api/admin/scenarios/{scenario_id}/disable")
    async def admin_disable_scenario(
        scenario_id: str,
        principal: Principal = Depends(require_admin),
    ) -> dict[str, Any]:
        return set_scenario_enabled(
            resolved_db,
            actor=principal,
            scenario_id=scenario_id,
            enabled=False,
        )

    @app.post("/api/admin/scenarios/{scenario_id}/enable")
    async def admin_enable_scenario(
        scenario_id: str,
        principal: Principal = Depends(require_admin),
    ) -> dict[str, Any]:
        return set_scenario_enabled(
            resolved_db,
            actor=principal,
            scenario_id=scenario_id,
            enabled=True,
        )

    @app.get("/api/admin/agents")
    async def admin_agents(
        principal: Principal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return list_agents(resolved_db)

    @app.post("/api/admin/agents")
    async def admin_create_agent(
        body: CreateAgentRequest,
        principal: Principal = Depends(require_admin),
    ) -> dict[str, Any]:
        return create_agent(
            resolved_db,
            actor=principal,
            username=body.username,
            password=body.password,
            display_name=body.display_name,
            scenario_ids=body.scenario_ids,
        )

    @app.post("/api/admin/agents/{agent_id}/disable")
    async def admin_disable_agent(
        agent_id: str,
        principal: Principal = Depends(require_admin),
    ) -> dict[str, Any]:
        result = set_agent_enabled(
            resolved_db,
            actor=principal,
            agent_id=agent_id,
            enabled=False,
        )
        for lease in result.pop("releasedLeases", []):
            await _broadcast_takeover_event(
                resolved_db,
                manager,
                lease,
                "takeover_released",
            )
        for listener_update in result.pop("listenerUpdates", []):
            await _broadcast_listen_state(
                resolved_db,
                manager,
                listener_update,
            )
        for session in result.pop("claimUpdates", []):
            await _broadcast_session_claim(
                resolved_db,
                manager,
                session,
                "session_released",
            )
        await manager.close_principal(
            principal_type="agent",
            principal_id=agent_id,
        )
        return result

    @app.post("/api/admin/agents/{agent_id}/enable")
    async def admin_enable_agent(
        agent_id: str,
        principal: Principal = Depends(require_admin),
    ) -> dict[str, Any]:
        result = set_agent_enabled(
            resolved_db,
            actor=principal,
            agent_id=agent_id,
            enabled=True,
        )
        result.pop("releasedLeases", None)
        result.pop("listenerUpdates", None)
        result.pop("claimUpdates", None)
        return result

    @app.put("/api/admin/agents/{agent_id}/scenarios")
    async def admin_set_agent_scenarios(
        agent_id: str,
        body: SetAgentScenariosRequest,
        principal: Principal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return set_agent_scenarios(
            resolved_db,
            actor=principal,
            agent_id=agent_id,
            scenario_ids=body.scenario_ids,
        )

    @app.get("/api/admin/robots")
    async def admin_robots(
        principal: Principal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return list_admin_robots(resolved_db)

    @app.post("/api/admin/robots")
    async def admin_create_robot(
        body: CreateRobotRequest,
        principal: Principal = Depends(require_admin),
    ) -> dict[str, Any]:
        return create_robot(
            resolved_db,
            actor=principal,
            display_name=body.display_name,
            scenario_id=body.scenario_id,
        )

    @app.patch("/api/admin/robots/{robot_id}")
    async def admin_rename_robot(
        robot_id: str,
        body: UpdateRobotRequest,
        principal: Principal = Depends(require_admin),
    ) -> dict[str, Any]:
        robot = rename_robot(
            resolved_db,
            actor=principal,
            robot_id=robot_id,
            display_name=body.display_name,
        )
        await _broadcast_robot_updated(resolved_db, manager, robot)
        return robot

    @app.delete("/api/admin/robots/{robot_id}")
    async def admin_delete_robot(
        robot_id: str,
        principal: Principal = Depends(require_admin),
    ) -> dict[str, Any]:
        result = delete_robot(
            resolved_db,
            actor=principal,
            robot_id=robot_id,
        )
        for item in result.pop("endedSessions"):
            await _broadcast_session_end(resolved_db, manager, item)
        await _broadcast_robot_deleted(
            resolved_db,
            manager,
            robot_id,
        )
        await manager.close_principal(
            principal_type="robot",
            principal_id=robot_id,
        )
        return {"ok": True, **result}

    @app.post("/api/admin/robots/{robot_id}/approve")
    async def admin_approve_robot(
        robot_id: str,
        principal: Principal = Depends(require_admin),
    ) -> dict[str, Any]:
        return set_robot_approval(
            resolved_db,
            actor=principal,
            robot_id=robot_id,
            approved=True,
        )

    @app.post("/api/admin/robots/{robot_id}/reject")
    async def admin_reject_robot(
        robot_id: str,
        principal: Principal = Depends(require_admin),
    ) -> dict[str, Any]:
        result = set_robot_approval(
            resolved_db,
            actor=principal,
            robot_id=robot_id,
            approved=False,
        )
        ended = end_sessions_for_offline_robot(
            resolved_db,
            robot_id=robot_id,
            reason="robot_rejected",
        )
        for item in ended:
            await _broadcast_session_end(resolved_db, manager, item)
        await _broadcast_robot_state(
            resolved_db,
            manager,
            robot_id,
            "offline",
        )
        await manager.close_principal(
            principal_type="robot",
            principal_id=robot_id,
        )
        result["endedSessions"] = ended
        return result

    @app.post("/api/admin/robots/{robot_id}/disable")
    async def admin_disable_robot(
        robot_id: str,
        principal: Principal = Depends(require_admin),
    ) -> dict[str, Any]:
        result = set_robot_enabled(
            resolved_db,
            actor=principal,
            robot_id=robot_id,
            enabled=False,
        )
        for item in result.pop("endedSessions", []):
            await _broadcast_session_end(resolved_db, manager, item)
        await _broadcast_robot_state(
            resolved_db,
            manager,
            robot_id,
            "offline",
        )
        await manager.close_principal(
            principal_type="robot",
            principal_id=robot_id,
        )
        return result

    @app.post("/api/admin/robots/{robot_id}/enable")
    async def admin_enable_robot(
        robot_id: str,
        principal: Principal = Depends(require_admin),
    ) -> dict[str, Any]:
        result = set_robot_enabled(
            resolved_db,
            actor=principal,
            robot_id=robot_id,
            enabled=True,
        )
        result.pop("endedSessions", None)
        return result

    @app.put("/api/admin/robots/{robot_id}/scenario")
    async def admin_set_robot_scenario(
        robot_id: str,
        body: SetRobotScenarioRequest,
        principal: Principal = Depends(require_admin),
    ) -> dict[str, Any]:
        return set_robot_scenario(
            resolved_db,
            actor=principal,
            robot_id=robot_id,
            scenario_id=body.scenario_id,
        )

    @app.post("/api/admin/robots/{robot_id}/rotate-credential")
    async def admin_rotate_robot_credential(
        robot_id: str,
        principal: Principal = Depends(require_admin),
    ) -> dict[str, str]:
        return rotate_robot_credential(
            resolved_db,
            actor=principal,
            robot_id=robot_id,
        )

    @app.get("/api/admin/audit")
    async def admin_audit(
        limit: int = Query(default=100, ge=1, le=500),
        principal: Principal = Depends(require_admin),
    ) -> list[dict[str, Any]]:
        return list_audit_events(resolved_db, limit=limit)

    @app.websocket("/ws")
    async def websocket_endpoint(
        websocket: WebSocket,
        ticket: str | None = Query(default=None),
    ) -> None:
        if not ticket:
            await websocket.close(code=4401, reason="missing ticket")
            return
        try:
            principal = consume_ws_ticket(resolved_db, ticket)
        except AuthError as exc:
            await websocket.close(code=4401, reason=str(exc))
            return

        await manager.connect(websocket, principal)
        try:
            await websocket.send_json(
                _snapshot_for_principal(resolved_db, principal)
            )
            while True:
                message = await websocket.receive_json()
                if isinstance(message, dict) and message.get("type") == "ping":
                    await websocket.send_json(
                        {"type": "pong", "serverTime": utc_now_text()}
                    )
                else:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "code": "UNSUPPORTED_MESSAGE",
                            "message": "只支持 ping 控制消息",
                        }
                    )
        except WebSocketDisconnect:
            pass
        finally:
            still_connected = await manager.disconnect(websocket, principal)
            if not still_connected:
                await _cleanup_disconnected_principal(
                    resolved_db,
                    manager,
                    principal,
                )

    return app


async def _cleanup_disconnected_principal(
    db: Database,
    manager: WebSocketManager,
    principal: Principal,
) -> None:
    if principal.principal_type == "agent":
        released = release_all_for_agent(
            db,
            agent_id=principal.id,
            reason="agent_disconnected",
        )
        for lease in released["leases"]:
            await _broadcast_takeover_event(
                db,
                manager,
                lease,
                "takeover_released",
            )
        for listener_update in released["listenerUpdates"]:
            await _broadcast_listen_state(
                db,
                manager,
                listener_update,
            )
        for session in released["claimUpdates"]:
            await _broadcast_session_claim(
                db,
                manager,
                session,
                "session_released",
            )
        return

    status_event = mark_robot_offline(
        db,
        robot_id=principal.id,
        reason="websocket_disconnected",
    )
    if status_event is not None:
        await _broadcast_robot_status(db, manager, status_event)
    ended = end_sessions_for_offline_robot(
        db,
        robot_id=principal.id,
        reason="robot_websocket_disconnected",
    )
    for item in ended:
        await _broadcast_session_end(db, manager, item)


def _scenario_and_agent_ids_for_robot(
    db: Database,
    robot_id: str,
) -> tuple[str, list[str]]:
    with db.connection() as connection:
        robot = connection.execute(
            "SELECT scenario_id FROM robots WHERE id = ?",
            (robot_id,),
        ).fetchone()
        if robot is None:
            raise ServiceError("机器人不存在", 404)
        rows = connection.execute(
            """
            SELECT DISTINCT agents.id
            FROM agents
            LEFT JOIN agent_scenarios
              ON agent_scenarios.agent_id = agents.id
            WHERE agents.enabled = 1
              AND (
                  agents.role = 'admin'
                  OR agent_scenarios.scenario_id = ?
              )
            """,
            (robot["scenario_id"],),
        ).fetchall()
    return robot["scenario_id"], [row["id"] for row in rows]


async def _broadcast_listen_state(
    db: Database,
    manager: WebSocketManager,
    event: dict[str, Any],
) -> None:
    _, agent_ids = _scenario_and_agent_ids_for_robot(
        db,
        event["robotId"],
    )
    message = {"type": "listen_changed", **event}
    await manager.send_to_robot(event["robotId"], message)
    await manager.broadcast_to_agents(agent_ids, message)


async def _broadcast_session_claim(
    db: Database,
    manager: WebSocketManager,
    session: dict[str, Any],
    message_type: str,
) -> None:
    _, agent_ids = _scenario_and_agent_ids_for_robot(
        db,
        session["robotId"],
    )
    await manager.broadcast_to_agents(
        agent_ids,
        {
            "type": message_type,
            "session": session,
            "occurredAt": utc_now_text(),
        },
    )


async def _broadcast_robot_status(
    db: Database,
    manager: WebSocketManager,
    event: dict[str, Any],
) -> None:
    _, agent_ids = _scenario_and_agent_ids_for_robot(
        db,
        event["robotId"],
    )
    await manager.broadcast_to_agents(
        agent_ids,
        {"type": "robot_status", **event},
    )


async def _broadcast_robot_state(
    db: Database,
    manager: WebSocketManager,
    robot_id: str,
    status: str,
) -> None:
    await _broadcast_robot_status(
        db,
        manager,
        {
            "robotId": robot_id,
            "from": "",
            "to": status,
            "lastSeenAt": "",
            "occurredAt": utc_now_text(),
        },
    )


async def _broadcast_robot_updated(
    db: Database,
    manager: WebSocketManager,
    robot: dict[str, Any],
) -> None:
    _, agent_ids = _scenario_and_agent_ids_for_robot(
        db,
        robot["id"],
    )
    await manager.broadcast_to_agents(
        agent_ids,
        {
            "type": "robot_updated",
            "robot": robot,
            "occurredAt": utc_now_text(),
        },
    )


async def _broadcast_robot_deleted(
    db: Database,
    manager: WebSocketManager,
    robot_id: str,
) -> None:
    _, agent_ids = _scenario_and_agent_ids_for_robot(
        db,
        robot_id,
    )
    await manager.broadcast_to_agents(
        agent_ids,
        {
            "type": "robot_deleted",
            "robotId": robot_id,
            "occurredAt": utc_now_text(),
        },
    )


async def _broadcast_takeover_event(
    db: Database,
    manager: WebSocketManager,
    lease: dict[str, Any],
    message_type: str,
) -> None:
    robot_id = lease["robotId"]
    scenario_id, agent_ids = _scenario_and_agent_ids_for_robot(db, robot_id)
    message = {
        "type": message_type,
        "robotId": robot_id,
        "sessionId": lease["sessionId"],
        "lease": lease,
        "occurredAt": utc_now_text(),
    }
    await manager.broadcast_to_agents(agent_ids, message)
    await manager.send_to_robot(robot_id, message)


async def _broadcast_session_end(
    db: Database,
    manager: WebSocketManager,
    result: dict[str, Any],
) -> None:
    session = result["session"]
    scenario_id = session["scenarioId"]
    with db.connection() as connection:
        rows = connection.execute(
            """
            SELECT DISTINCT agents.id
            FROM agents
            LEFT JOIN agent_scenarios
              ON agent_scenarios.agent_id = agents.id
            WHERE agents.enabled = 1
              AND (
                  agents.role = 'admin'
                  OR agent_scenarios.scenario_id = ?
              )
            """,
            (scenario_id,),
        ).fetchall()
    agent_ids = [row["id"] for row in rows]
    message = {
        "type": "session_ended",
        "session": session,
        "occurredAt": utc_now_text(),
    }
    await manager.broadcast_to_agents(agent_ids, message)
    await manager.send_to_robot(session["robotId"], message)
    if result.get("releasedAgentId"):
        released_lease = {
            "id": result.get("releasedLeaseId"),
            "robotId": session["robotId"],
            "sessionId": session["id"],
            "agentId": result["releasedAgentId"],
            "status": "released",
            "microphoneEnabled": False,
            "acquiredAt": "",
            "expiresAt": "",
            "releasedAt": session["endedAt"],
            "endReason": "session_ended",
        }
        await _broadcast_takeover_event(
            db,
            manager,
            released_lease,
            "takeover_released",
        )


def _snapshot_for_principal(
    db: Database,
    principal: Principal,
) -> dict[str, Any]:
    base = {
        "type": "snapshot",
        "principal": principal.to_public_dict(),
        "serverTime": utc_now_text(),
    }
    if principal.principal_type == "agent":
        base.update(
            {
                "scenarios": list_scenarios_for_principal(
                    db,
                    actor=principal,
                ),
                "robots": list_visible_robots(db, actor=principal),
                "sessions": list_sessions_for_principal(
                    db,
                    actor=principal,
                ),
            }
        )
        return base

    with db.connection() as connection:
        payload = robot_self_payload(connection, principal.id)
        media = payload["robot"].get("media")
    current = payload["currentSession"]
    if current is not None and current["status"] != "ended":
        current = get_session(db, current["id"])
    lease = get_active_lease(db, robot_id=principal.id)
    base.update(
        {
            "robot": payload["robot"],
            "scenarioName": payload["scenarioName"],
            "session": current,
            "activeLease": lease,
            "media": media,
            "robotMicrophoneRequested": payload["robotMicrophoneRequested"],
        }
    )
    return base


def create_default_app() -> FastAPI:
    return create_app()
