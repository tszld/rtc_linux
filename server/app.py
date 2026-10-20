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
from .admin import (
    AdminError,
    AdminIdentity,
    authenticate_admin,
    create_agent,
    list_admin_audit,
    list_admin_clients,
    list_agents,
    login_admin,
    logout_admin,
    reset_agent_password,
    reset_client_credential,
    set_agent_enabled,
    set_client_status,
    update_agent_display_name,
    update_client_display_name,
)
from .calls import (
    CallError,
    agent_hangup as agent_hangup_action,
    assign_waiting_calls,
    claim_call,
    create_call,
    create_call_for_client,
    end_call_by_caller,
    expire_calls,
    get_call,
    get_assigned_call,
    list_visible_calls,
    mark_agent_connected,
    mark_agent_disconnected,
    mark_caller_disconnected,
    mark_caller_ready,
    resume_agent_call,
    verify_caller_token,
    end_call,
)
from .clients import (
    ClientError,
    ClientIdentity,
    authenticate_client,
    client_self_payload,
    list_approved_client_directory,
    register_client,
    request_client_reregistration,
    require_approved_client,
)
from .config import ServerConfig, load_config
from .db import initialize_database
from .models import (
    ClientRegisterRequest,
    ClientStatusRequest,
    CreateAgentRequest,
    CreateCallRequest,
    DisplayNameRequest,
    LoginRequest,
    MessageResponse,
    ResetPasswordRequest,
)
from .rtc import RtcConfigurationError
from .ws_manager import ConnectionManager


def create_app(config: ServerConfig | None = None) -> FastAPI:
    settings = config or load_config()
    manager = ConnectionManager(settings)

    async def dispatch_and_broadcast_calls() -> None:
        assigned = assign_waiting_calls(
            settings,
            manager.connected_agent_ids(),
        )
        manager.note_assignments(assigned)
        await manager.broadcast_calls()

    async def expiry_loop() -> None:
        while True:
            await asyncio.sleep(1)
            try:
                changed = expire_calls(settings)
                if changed:
                    # 先让客服端看到超时后的 waiting/ended 状态，再开始下一轮派单。
                    await manager.broadcast_calls()
                    await dispatch_and_broadcast_calls()
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

    @app.exception_handler(AdminError)
    async def admin_error_handler(_: Request, exc: AdminError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    @app.exception_handler(ClientError)
    async def client_error_handler(_: Request, exc: ClientError) -> JSONResponse:
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

    def require_admin(
        authorization: Annotated[str | None, Header()] = None,
    ) -> AdminIdentity:
        return authenticate_admin(settings, bearer_token(authorization))

    Admin = Annotated[AdminIdentity, Depends(require_admin)]

    def require_client(
        x_client_credential: Annotated[str | None, Header()] = None,
        x_installation_uuid: Annotated[str | None, Header()] = None,
    ) -> ClientIdentity:
        return authenticate_client(
            settings,
            installation_uuid=x_installation_uuid or "",
            credential=x_client_credential or "",
        )

    Client = Annotated[ClientIdentity, Depends(require_client)]

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

    @app.post("/api/admin/login")
    def admin_login(body: LoginRequest) -> dict:
        token, admin = login_admin(
            settings,
            username=body.username.strip(),
            password=body.password,
        )
        return {
            "accessToken": token,
            "admin": {"id": admin.id, "username": admin.username},
        }

    @app.post("/api/admin/logout", response_model=MessageResponse)
    def admin_logout(admin: Admin) -> MessageResponse:
        logout_admin(settings, admin.token_hash)
        return MessageResponse(message="logged_out")

    @app.post("/api/clients/register")
    def client_register(
        body: ClientRegisterRequest,
        x_client_credential: Annotated[str | None, Header()] = None,
    ) -> dict:
        client, issued_credential = register_client(
            settings,
            installation_uuid=body.installation_uuid,
            display_name=body.display_name,
            credential=x_client_credential,
        )
        return {
            "client": client,
            "credential": issued_credential,
        }

    @app.get("/api/clients/me")
    def client_me(client: Client) -> dict:
        return {"client": client_self_payload(settings, client)}

    @app.post("/api/clients/reregister")
    def client_reregister(client: Client) -> dict:
        return {"client": request_client_reregistration(settings, client)}

    @app.get("/api/clients")
    def client_directory(_: Agent) -> dict:
        return {"clients": list_approved_client_directory(settings)}

    @app.get("/api/calls")
    def calls(_: Agent) -> dict:
        return {"calls": list_visible_calls(settings)}

    @app.post("/api/calls")
    def create_new_call(body: CreateCallRequest, client: Client) -> dict:
        del body
        approved = require_approved_client(client)
        call, caller_token, rtc_auth = create_call_for_client(
            settings,
            client=approved,
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
        mark_caller_ready(settings, call_id, x_caller_token or "")
        await dispatch_and_broadcast_calls()
        return {"call": get_call(settings, call_id)}

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
        await dispatch_and_broadcast_calls()
        return {"call": call}

    @app.post("/api/calls/{call_id}/claim")
    async def agent_claim(call_id: str, agent: Agent) -> dict:
        call, rtc_auth = claim_call(settings, call_id, agent)
        await dispatch_and_broadcast_calls()
        return {"call": call, "rtc": rtc_auth}

    @app.post("/api/calls/{call_id}/assignment")
    def agent_assignment(call_id: str, agent: Agent) -> dict:
        call, rtc_auth = get_assigned_call(settings, call_id, agent)
        return {"call": call, "rtc": rtc_auth}

    @app.post("/api/calls/{call_id}/connected")
    async def agent_connected(call_id: str, agent: Agent) -> dict:
        call = mark_agent_connected(settings, call_id, agent)
        await dispatch_and_broadcast_calls()
        return {"call": call}

    @app.post("/api/calls/{call_id}/hangup")
    async def agent_hangup_endpoint(call_id: str, agent: Agent) -> dict:
        call = agent_hangup_action(settings, call_id, agent)
        await dispatch_and_broadcast_calls()
        return {"call": call}

    @app.post("/api/calls/{call_id}/resume")
    async def agent_resume(call_id: str, agent: Agent) -> dict:
        call, rtc_auth = resume_agent_call(settings, call_id, agent)
        await dispatch_and_broadcast_calls()
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
        await dispatch_and_broadcast_calls()
        return {"call": ended}

    @app.get("/api/admin/clients")
    def admin_clients(
        _: Admin,
        status: str | None = Query(default=None),
    ) -> dict:
        return {"clients": list_admin_clients(settings, status=status)}

    @app.patch("/api/admin/clients/{client_id}")
    def admin_update_client(
        client_id: str,
        body: DisplayNameRequest,
        admin: Admin,
    ) -> MessageResponse:
        update_client_display_name(
            settings,
            admin,
            client_id=client_id,
            display_name=body.display_name,
        )
        return MessageResponse(message="updated")

    @app.post("/api/admin/clients/{client_id}/approve")
    def admin_approve_client(client_id: str, admin: Admin) -> MessageResponse:
        set_client_status(settings, admin, client_id=client_id, status="approved")
        return MessageResponse(message="approved")

    @app.post("/api/admin/clients/{client_id}/reject")
    def admin_reject_client(
        client_id: str,
        admin: Admin,
        body: ClientStatusRequest | None = None,
    ) -> MessageResponse:
        set_client_status(
            settings,
            admin,
            client_id=client_id,
            status="rejected",
            reason=body.reason if body else None,
        )
        return MessageResponse(message="rejected")

    @app.post("/api/admin/clients/{client_id}/disable")
    def admin_disable_client(client_id: str, admin: Admin) -> MessageResponse:
        set_client_status(settings, admin, client_id=client_id, status="disabled")
        return MessageResponse(message="disabled")

    @app.post("/api/admin/clients/{client_id}/enable")
    def admin_enable_client(client_id: str, admin: Admin) -> MessageResponse:
        set_client_status(settings, admin, client_id=client_id, status="approved")
        return MessageResponse(message="enabled")

    @app.post("/api/admin/clients/{client_id}/reset-credential")
    def admin_reset_client_credential(
        client_id: str,
        admin: Admin,
    ) -> MessageResponse:
        reset_client_credential(settings, admin, client_id=client_id)
        return MessageResponse(message="credential_reset")

    @app.get("/api/admin/agents")
    def admin_agents(_: Admin) -> dict:
        return {"agents": list_agents(settings)}

    @app.post("/api/admin/agents")
    def admin_create_agent(
        body: CreateAgentRequest,
        admin: Admin,
    ) -> dict:
        agent = create_agent(
            settings,
            admin,
            username=body.username,
            display_name=body.display_name,
            password=body.password,
        )
        return {"agent": agent}

    @app.patch("/api/admin/agents/{agent_id}")
    def admin_update_agent(
        agent_id: str,
        body: DisplayNameRequest,
        admin: Admin,
    ) -> MessageResponse:
        update_agent_display_name(
            settings,
            admin,
            agent_id=agent_id,
            display_name=body.display_name,
        )
        return MessageResponse(message="updated")

    @app.post("/api/admin/agents/{agent_id}/reset-password")
    def admin_reset_agent_password(
        agent_id: str,
        body: ResetPasswordRequest,
        admin: Admin,
    ) -> MessageResponse:
        reset_agent_password(
            settings,
            admin,
            agent_id=agent_id,
            password=body.password,
        )
        return MessageResponse(message="password_reset")

    @app.post("/api/admin/agents/{agent_id}/disable")
    def admin_disable_agent(agent_id: str, admin: Admin) -> MessageResponse:
        set_agent_enabled(settings, admin, agent_id=agent_id, enabled=False)
        return MessageResponse(message="disabled")

    @app.post("/api/admin/agents/{agent_id}/enable")
    def admin_enable_agent(agent_id: str, admin: Admin) -> MessageResponse:
        set_agent_enabled(settings, admin, agent_id=agent_id, enabled=True)
        return MessageResponse(message="enabled")

    @app.get("/api/admin/audit")
    def admin_audit(_: Admin, limit: int = Query(default=200, ge=1, le=1000)) -> dict:
        return {"events": list_admin_audit(settings, limit=limit)}

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
            await dispatch_and_broadcast_calls()
            try:
                while True:
                    message = await websocket.receive_text()
                    if message == "ping":
                        await websocket.send_text("pong")
            except WebSocketDisconnect:
                removed = await manager.unregister_agent(agent_id, websocket)
                if removed and mark_agent_disconnected(settings, agent_id):
                    await dispatch_and_broadcast_calls()
            except Exception:
                removed = await manager.unregister_agent(agent_id, websocket)
                if removed and mark_agent_disconnected(settings, agent_id):
                    await dispatch_and_broadcast_calls()
            return

        assert caller_ticket is not None
        call_id = caller_ticket.identity
        await manager.register_caller(call_id, websocket)
        try:
            while True:
                message = await websocket.receive_text()
                if message == "ping":
                    await websocket.send_text("pong")
        except WebSocketDisconnect:
            removed = await manager.unregister_caller(call_id, websocket)
            if removed and mark_caller_disconnected(settings, call_id):
                await dispatch_and_broadcast_calls()
        except Exception:
            removed = await manager.unregister_caller(call_id, websocket)
            if removed and mark_caller_disconnected(settings, call_id):
                await dispatch_and_broadcast_calls()

    return app


app = create_app()
