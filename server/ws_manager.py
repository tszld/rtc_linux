from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

from fastapi import WebSocket

from .auth import Principal


class WebSocketManager:
    def __init__(self) -> None:
        self._agents: dict[str, set[WebSocket]] = {}
        self._robots: dict[str, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(
        self,
        websocket: WebSocket,
        principal: Principal,
    ) -> None:
        await websocket.accept()
        async with self._lock:
            target = (
                self._agents
                if principal.principal_type == "agent"
                else self._robots
            )
            target.setdefault(principal.id, set()).add(websocket)

    async def disconnect(
        self,
        websocket: WebSocket,
        principal: Principal,
    ) -> bool:
        async with self._lock:
            target = (
                self._agents
                if principal.principal_type == "agent"
                else self._robots
            )
            connections = target.get(principal.id)
            if connections is not None:
                connections.discard(websocket)
                if not connections:
                    target.pop(principal.id, None)
                    return False
            return True

    async def connected_agent_ids(self) -> list[str]:
        async with self._lock:
            return list(self._agents)

    async def send_to_agent(
        self,
        agent_id: str,
        message: dict[str, Any],
    ) -> None:
        async with self._lock:
            connections = list(self._agents.get(agent_id, ()))
        await self._send_many(self._agents, agent_id, connections, message)

    async def send_to_robot(
        self,
        robot_id: str,
        message: dict[str, Any],
    ) -> None:
        async with self._lock:
            connections = list(self._robots.get(robot_id, ()))
        await self._send_many(self._robots, robot_id, connections, message)

    async def is_robot_connected(self, robot_id: str) -> bool:
        async with self._lock:
            return bool(self._robots.get(robot_id))

    async def broadcast_to_agents(
        self,
        agent_ids: Iterable[str],
        message: dict[str, Any],
    ) -> None:
        for agent_id in dict.fromkeys(agent_ids):
            await self.send_to_agent(agent_id, message)

    async def close_principal(
        self,
        *,
        principal_type: str,
        principal_id: str,
        code: int = 4403,
        reason: str = "disabled",
    ) -> None:
        async with self._lock:
            registry = (
                self._agents
                if principal_type == "agent"
                else self._robots
            )
            connections = list(registry.get(principal_id, ()))
        for websocket in connections:
            try:
                await websocket.close(code=code, reason=reason)
            except Exception:
                pass

    async def _send_many(
        self,
        registry: dict[str, set[WebSocket]],
        principal_id: str,
        connections: list[WebSocket],
        message: dict[str, Any],
    ) -> None:
        dead = []
        for websocket in connections:
            try:
                await websocket.send_json(message)
            except Exception:
                dead.append(websocket)
        if dead:
            async with self._lock:
                current = registry.get(principal_id)
                if current is not None:
                    for websocket in dead:
                        current.discard(websocket)
                    if not current:
                        registry.pop(principal_id, None)
