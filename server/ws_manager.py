from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass
from typing import Literal

from fastapi import WebSocket

from .calls import get_call, list_visible_calls
from .config import ServerConfig


@dataclass
class WsTicket:
    kind: Literal["agent", "caller"]
    identity: str
    expires_at: float


class ConnectionManager:
    def __init__(self, config: ServerConfig):
        self.config = config
        self.tickets: dict[str, WsTicket] = {}
        self.agent_connections: dict[str, WebSocket] = {}
        self.caller_connections: dict[str, WebSocket] = {}
        self.lock = asyncio.Lock()

    def create_ticket(self, kind: Literal["agent", "caller"], identity: str) -> str:
        ticket = secrets.token_urlsafe(32)
        self.tickets[ticket] = WsTicket(
            kind=kind,
            identity=identity,
            expires_at=time.monotonic() + self.config.ws_ticket_ttl_seconds,
        )
        return ticket

    def consume_ticket(
        self, ticket: str, expected_kind: Literal["agent", "caller"]
    ) -> WsTicket | None:
        value = self.tickets.get(ticket)
        if not value:
            return None
        if value.kind != expected_kind or value.expires_at <= time.monotonic():
            return None
        self.tickets.pop(ticket, None)
        return value

    async def register_agent(self, agent_id: str, websocket: WebSocket) -> None:
        async with self.lock:
            previous = self.agent_connections.get(agent_id)
            self.agent_connections[agent_id] = websocket
        if previous and previous is not websocket:
            try:
                await previous.close(code=4001, reason="replaced")
            except Exception:
                pass

    async def unregister_agent(self, agent_id: str, websocket: WebSocket) -> bool:
        async with self.lock:
            if self.agent_connections.get(agent_id) is websocket:
                self.agent_connections.pop(agent_id, None)
                return True
        return False

    async def register_caller(self, call_id: str, websocket: WebSocket) -> None:
        async with self.lock:
            previous = self.caller_connections.get(call_id)
            self.caller_connections[call_id] = websocket
        if previous and previous is not websocket:
            try:
                await previous.close(code=4001, reason="replaced")
            except Exception:
                pass
        await self.send_caller_call(call_id)

    async def unregister_caller(self, call_id: str, websocket: WebSocket) -> bool:
        async with self.lock:
            if self.caller_connections.get(call_id) is websocket:
                self.caller_connections.pop(call_id, None)
                return True
        return False

    async def send_agent_snapshot(self, agent_id: str) -> None:
        websocket = self.agent_connections.get(agent_id)
        if not websocket:
            return
        payload = {
            "type": "snapshot",
            "calls": list_visible_calls(self.config),
        }
        await self._safe_send(websocket, payload)

    async def send_caller_call(self, call_id: str) -> None:
        websocket = self.caller_connections.get(call_id)
        if not websocket:
            return
        try:
            call = get_call(self.config, call_id)
        except Exception:
            return
        await self._safe_send(websocket, {"type": "call", "call": call})

    async def broadcast_calls(self) -> None:
        calls = list_visible_calls(self.config)
        for agent_id in list(self.agent_connections):
            websocket = self.agent_connections.get(agent_id)
            if websocket:
                await self._safe_send(
                    websocket, {"type": "snapshot", "calls": calls}
                )
        for call_id in list(self.caller_connections):
            await self.send_caller_call(call_id)

    async def _safe_send(self, websocket: WebSocket, payload: dict) -> None:
        try:
            await websocket.send_json(payload)
        except Exception:
            return
