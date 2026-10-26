from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit


async def request_json(
    app: Any,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], Any]:
    status, response_headers, payload = await request_bytes(
        app,
        method,
        path,
        body=body,
        headers=headers,
        query=query,
    )
    parsed = json.loads(payload) if payload else None
    return status, response_headers, parsed


async def request_bytes(
    app: Any,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    split = urlsplit(path)
    path = split.path
    combined_query = parse_qsl(split.query, keep_blank_values=True)
    combined_query.extend((query or {}).items())
    raw_body = (
        json.dumps(body, ensure_ascii=False).encode("utf-8")
        if body is not None
        else b""
    )
    request_headers = {
        "host": "testserver",
        "content-type": "application/json",
    }
    request_headers.update(
        {name.lower(): value for name, value in (headers or {}).items()}
    )
    messages: list[dict[str, Any]] = []
    body_sent = False
    disconnected = asyncio.Event()

    async def receive() -> dict[str, Any]:
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": raw_body, "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": urlencode(combined_query).encode("ascii"),
        "root_path": "",
        "headers": [
            (name.encode("latin-1"), value.encode("latin-1"))
            for name, value in request_headers.items()
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    try:
        await app(scope, receive, send)
    finally:
        disconnected.set()
    start = next(message for message in messages if message["type"] == "http.response.start")
    payload = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    response_headers = {
        name.decode("latin-1").lower(): value.decode("latin-1")
        for name, value in start.get("headers", [])
    }
    return int(start["status"]), response_headers, payload


async def websocket_messages(
    app: Any,
    *,
    ticket: str,
    ping: bool = True,
) -> list[dict[str, Any]]:
    incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    outgoing: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    query = urlencode({"ticket": ticket}).encode("ascii")
    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": "/ws",
        "raw_path": b"/ws",
        "query_string": query,
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "subprotocols": [],
    }

    async def receive() -> dict[str, Any]:
        return await incoming.get()

    async def send(message: dict[str, Any]) -> None:
        if message.get("type") == "websocket.send":
            if message.get("text") is not None:
                await outgoing.put(json.loads(message["text"]))
                return
            if message.get("bytes") is not None:
                await outgoing.put(json.loads(message["bytes"]))
                return
        await outgoing.put(message)

    task = asyncio.create_task(app(scope, receive, send))
    await incoming.put({"type": "websocket.connect"})
    messages = []
    accept_or_close = await asyncio.wait_for(outgoing.get(), timeout=2)
    messages.append(accept_or_close)
    if accept_or_close["type"] == "websocket.close":
        await asyncio.wait_for(task, timeout=2)
        return messages

    snapshot = await asyncio.wait_for(outgoing.get(), timeout=2)
    messages.append(snapshot)
    if ping:
        await incoming.put(
            {
                "type": "websocket.receive",
                "text": json.dumps({"type": "ping"}),
                "bytes": None,
            }
        )
        messages.append(await asyncio.wait_for(outgoing.get(), timeout=2))
    await incoming.put({"type": "websocket.disconnect", "code": 1000})
    await asyncio.wait_for(task, timeout=2)
    return messages


class WebSocketTestSession:
    def __init__(self, task, incoming, outgoing) -> None:
        self.task = task
        self.incoming = incoming
        self.outgoing = outgoing

    async def next_message(self, timeout: float = 2) -> dict[str, Any]:
        return await asyncio.wait_for(self.outgoing.get(), timeout=timeout)

    async def close(self) -> None:
        await self.incoming.put(
            {"type": "websocket.disconnect", "code": 1000}
        )
        await asyncio.wait_for(self.task, timeout=2)


async def open_websocket(
    app: Any,
    *,
    ticket: str,
) -> WebSocketTestSession:
    incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    outgoing: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    query = urlencode({"ticket": ticket}).encode("ascii")
    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": "/ws",
        "raw_path": b"/ws",
        "query_string": query,
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "subprotocols": [],
    }

    async def receive() -> dict[str, Any]:
        return await incoming.get()

    async def send(message: dict[str, Any]) -> None:
        if message.get("type") == "websocket.send":
            payload = message.get("text")
            if payload is None and message.get("bytes") is not None:
                payload = message["bytes"].decode("utf-8")
            if payload is not None:
                await outgoing.put(json.loads(payload))
                return
        await outgoing.put(message)

    task = asyncio.create_task(app(scope, receive, send))
    await incoming.put({"type": "websocket.connect"})
    accepted = await asyncio.wait_for(outgoing.get(), timeout=2)
    if accepted.get("type") != "websocket.accept":
        await asyncio.wait_for(task, timeout=2)
        raise AssertionError(f"WebSocket 连接失败：{accepted}")
    snapshot = await asyncio.wait_for(outgoing.get(), timeout=2)
    if snapshot.get("type") != "snapshot":
        await asyncio.wait_for(task, timeout=2)
        raise AssertionError(f"WebSocket 未返回快照：{snapshot}")
    return WebSocketTestSession(task, incoming, outgoing)
