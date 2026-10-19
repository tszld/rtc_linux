import asyncio
import secrets
from contextlib import asynccontextmanager, suppress
from typing import Annotated

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .auth import (
    AuthError,
    AuthenticatedAgent,
    authenticate_access_token,
    login_agent,
    logout_agent,
)
from .calls import (
    CallError,
    agent_hangup as agent_hangup_action,
    claim_call,
    create_call,
    end_call_by_caller,
    expire_calls,
    get_call,
    list_visible_calls,
    mark_agent_connected,
    mark_agent_disconnected,
    mark_caller_disconnected,
    mark_caller_ready,
    resume_agent_call,
    resume_caller,
    verify_caller_token,
    end_call,
)
from .config import ServerConfig, load_config
from .db import initialize_database
from .models import CreateCallRequest, LoginRequest, MessageResponse
from .rtc import RtcConfigurationError
from .ws_manager import ConnectionManager


def create_app(config: ServerConfig | None = None) -> FastAPI:
    settings = config or load_config()
    manager = ConnectionManager(settings)

    async def expiry_loop() -> None:
        while True:
            await asyncio.sleep(1)
            try:
                changed = expire_calls(settings)
                if changed:
                    await manager.broadcast_calls()
            except asyncio.CancelledError:
                raise
            except Exception:
                continue

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        initialize_database(settings)
        task = asyncio.create_task(expiry_loop())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="RTC Control Plane", version="0.1.0", lifespan=lifespan)
    app.state.config = settings
    app.state.manager = manager
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=settings.cors_origin_regex,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        allow_private_network=True,
    )

    @app.exception_handler(AuthError)
    async def auth_error_handler(_: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    @app.exception_handler(CallError)
    async def call_error_handler(_: Request, exc: CallError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    @app.exception_handler(RtcConfigurationError)
    async def rtc_error_handler(
        _: Request, exc: RtcConfigurationError
    ) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    def bearer_token(authorization: str | None) -> str:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise AuthError("缺少访问凭证")
        return authorization[7:].strip()

    def require_agent(
        authorization: Annotated[str | None, Header()] = None,
    ) -> AuthenticatedAgent:
        return authenticate_access_token(settings, bearer_token(authorization))

    Agent = Annotated[AuthenticatedAgent, Depends(require_agent)]

    @app.get("/api/health")
    def health() -> dict:
        return {
            "ok": True,
            "service": "rtc-control-plane",
            "rtcConfigured": bool(settings.app_id and settings.app_key),
        }

    @app.post("/api/auth/login")
    def login(body: LoginRequest) -> dict:
        token, agent, expires_at = login_agent(
            settings, body.username.strip(), body.password
        )
        return {
            "accessToken": token,
            "expiresAt": expires_at,
            "agent": {
                "id": agent.id,
                "username": agent.username,
                "displayName": agent.display_name,
            },
        }

    @app.post("/api/auth/heartbeat", response_model=MessageResponse)
    def heartbeat(_: Agent) -> MessageResponse:
        return MessageResponse(message="ok")

    @app.post("/api/auth/logout", response_model=MessageResponse)
    def logout(agent: Agent) -> MessageResponse:
        logout_agent(settings, agent.token_hash)
        return MessageResponse(message="logged_out")

    @app.post("/api/server/shutdown", response_model=MessageResponse)
    def shutdown_local_server(
        request: Request,
        x_server_control_token: Annotated[str | None, Header()] = None,
    ) -> MessageResponse:
        client_host = request.client.host if request.client else ""
        expected_token = getattr(app.state, "shutdown_token", None)
        if client_host not in {"127.0.0.1", "::1"}:
            raise HTTPException(status_code=403, detail="仅允许本机关闭后端")
        if (
            not expected_token
            or not x_server_control_token
            or not secrets.compare_digest(expected_token, x_server_control_token)
        ):
            raise HTTPException(status_code=403, detail="关闭凭证无效")
        request_shutdown = getattr(app.state, "request_shutdown", None)
        if not callable(request_shutdown):
            raise HTTPException(status_code=503, detail="当前启动方式不支持远程停止")
        request_shutdown()
        return MessageResponse(message="shutting_down")

    @app.post("/api/auth/ws-ticket")
    def agent_ws_ticket(agent: Agent) -> dict:
        return {"ticket": manager.create_ticket("agent", agent.id)}

    @app.get("/api/calls")
    def calls(_: Agent) -> dict:
        return {"calls": list_visible_calls(settings)}

    @app.post("/api/calls")
    def create_new_call(body: CreateCallRequest) -> dict:
        call, caller_token, rtc_auth = create_call(
            settings,
            client_id=body.client_id.strip(),
            client_name=body.client_name.strip(),
        )
        return {
            "call": call,
            "callerToken": caller_token,
            "rtc": rtc_auth,
            "wsTicket": manager.create_ticket("caller", call["id"]),
        }

    @app.post("/api/calls/{call_id}/ready")
    async def caller_ready(
        call_id: str,
        x_caller_token: Annotated[str | None, Header()] = None,
    ) -> dict:
        call = mark_caller_ready(settings, call_id, x_caller_token or "")
        await manager.broadcast_calls()
        return {"call": call}

    @app.post("/api/calls/{call_id}/ws-ticket")
    def caller_ws_ticket(
        call_id: str,
        x_caller_token: Annotated[str | None, Header()] = None,
    ) -> dict:
        verify_caller_token(settings, call_id, x_caller_token or "")
        return {"ticket": manager.create_ticket("caller", call_id)}

    @app.get("/api/calls/{call_id}")
    def caller_get_call(
        call_id: str,
        x_caller_token: Annotated[str | None, Header()] = None,
    ) -> dict:
        verify_caller_token(settings, call_id, x_caller_token or "")
        return {"call": get_call(settings, call_id)}

    @app.post("/api/calls/{call_id}/end")
    async def caller_end_call(
        call_id: str,
        x_caller_token: Annotated[str | None, Header()] = None,
    ) -> dict:
        call = end_call_by_caller(settings, call_id, x_caller_token or "")
        await manager.broadcast_calls()
        return {"call": call}

    @app.post("/api/calls/{call_id}/claim")
    async def agent_claim(call_id: str, agent: Agent) -> dict:
        call, rtc_auth = claim_call(settings, call_id, agent)
        await manager.broadcast_calls()
        return {"call": call, "rtc": rtc_auth}

    @app.post("/api/calls/{call_id}/connected")
    async def agent_connected(call_id: str, agent: Agent) -> dict:
        call = mark_agent_connected(settings, call_id, agent)
        await manager.broadcast_calls()
        return {"call": call}

    @app.post("/api/calls/{call_id}/hangup")
    async def agent_hangup_endpoint(call_id: str, agent: Agent) -> dict:
        call = agent_hangup_action(settings, call_id, agent)
        await manager.broadcast_calls()
        return {"call": call}

    @app.post("/api/calls/{call_id}/resume")
    async def agent_resume(call_id: str, agent: Agent) -> dict:
        call, rtc_auth = resume_agent_call(settings, call_id, agent)
        await manager.broadcast_calls()
        return {"call": call, "rtc": rtc_auth}

    @app.post("/api/calls/{call_id}/force-end")
    async def agent_force_end(call_id: str, agent: Agent) -> dict:
        call = get_call(settings, call_id)
        if call["status"] == "ended":
            return {"call": call}
        if (
            call["claimedAgentId"] not in (None, agent.id)
            and call["status"] in ("connecting", "active", "interrupted")
        ):
            raise CallError("当前客服无权结束该会话", 403)
        ended = end_call(settings, call_id, "agent_force_end", "agent", agent.id)
        await manager.broadcast_calls()
        return {"call": ended}

    @app.websocket("/ws")
    async def websocket_endpoint(
        websocket: WebSocket, ticket: str = Query(default="")
    ) -> None:
        consumed = manager.consume_ticket(ticket, "agent")
        caller_ticket = None
        if not consumed:
            caller_ticket = manager.consume_ticket(ticket, "caller")
        if not consumed and not caller_ticket:
            await websocket.close(code=4401, reason="invalid ticket")
            return

        await websocket.accept()
        if consumed:
            agent_id = consumed.identity
            await manager.register_agent(agent_id, websocket)
            try:
                while True:
                    message = await websocket.receive_text()
                    if message == "ping":
                        await websocket.send_text("pong")
            except WebSocketDisconnect:
                removed = await manager.unregister_agent(agent_id, websocket)
                if removed and mark_agent_disconnected(settings, agent_id):
                    await manager.broadcast_calls()
            except Exception:
                removed = await manager.unregister_agent(agent_id, websocket)
                if removed and mark_agent_disconnected(settings, agent_id):
                    await manager.broadcast_calls()
            return

        assert caller_ticket is not None
        call_id = caller_ticket.identity
        await manager.register_caller(call_id, websocket)
        resumed = resume_caller(settings, call_id)
        if resumed:
            await manager.broadcast_calls()
        try:
            while True:
                message = await websocket.receive_text()
                if message == "ping":
                    await websocket.send_text("pong")
        except WebSocketDisconnect:
            removed = await manager.unregister_caller(call_id, websocket)
            if removed and mark_caller_disconnected(settings, call_id):
                await manager.broadcast_calls()
        except Exception:
            removed = await manager.unregister_caller(call_id, websocket)
            if removed and mark_caller_disconnected(settings, call_id):
                await manager.broadcast_calls()

    return app


app = create_app()
