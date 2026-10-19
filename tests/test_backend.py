from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import uvicorn
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect as websocket_connect

from server.app import create_app
from server.auth import (
    AuthError,
    NO_EXPIRY_AT,
    authenticate_access_token,
    login_agent,
    logout_agent,
)
from server.calls import (
    CallError,
    agent_hangup,
    claim_call,
    create_call,
    expire_calls,
    get_call,
    mark_agent_connected,
    mark_agent_disconnected,
    mark_caller_ready,
)
from server.config import ServerConfig
from server.db import db_session, initialize_database, iso_time, utc_now
from server.rtc import make_rtc_auth
from server.token.app_token import AppToken
from server.token.service import Service
from server.ws_manager import ConnectionManager


def make_seed_agents(count: int) -> list[dict[str, str]]:
    return [
        {
            "username": f"agent{index:02d}",
            "password": "agent123",
            "display_name": f"客服 {index:02d}",
        }
        for index in range(1, count + 1)
    ]


def create_ready_call(
    config: ServerConfig, client_id: str
) -> tuple[dict, str]:
    call, caller_token, _ = create_call(
        config, client_id=client_id, client_name=f"呼叫 {client_id}"
    )
    mark_caller_ready(config, call["id"], caller_token)
    return call, caller_token


def age_call_timestamp(
    config: ServerConfig, call_id: str, column: str, seconds: int
) -> None:
    allowed_columns = {"created_at", "claimed_at", "interrupted_at"}
    if column not in allowed_columns:
        raise ValueError(f"unsupported call timestamp: {column}")
    old_value = iso_time(utc_now() - timedelta(seconds=seconds))
    with db_session(config) as connection:
        connection.execute(
            f"UPDATE calls SET {column} = ? WHERE id = ?",
            (old_value, call_id),
        )


def request_json(
    base_url: str,
    method: str,
    path: str,
    *,
    body: dict | None = None,
    token: str | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict]:
    payload = None
    request_headers = {"Accept": "application/json"}
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    request_headers.update(headers or {})
    request = Request(
        base_url + path,
        data=payload,
        method=method,
        headers=request_headers,
    )
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def receive_call_status(
    websocket, expected_status: str, timeout: float = 3
) -> dict:
    deadline = time.monotonic() + timeout
    last_call = None
    while time.monotonic() < deadline:
        message = json.loads(
            websocket.recv(timeout=max(0.05, deadline - time.monotonic()))
        )
        if message.get("type") != "call":
            continue
        last_call = message.get("call") or {}
        if last_call.get("status") == expected_status:
            return last_call
    raise AssertionError(
        f"did not receive call status {expected_status}, last call: {last_call}"
    )


class BackendServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.config = ServerConfig(
            app_id="test-app",
            app_key="test-app-key",
            db_path=Path(self.temp.name) / "test.db",
            seed_agents=make_seed_agents(12),
        )
        initialize_database(self.config, end_stale_calls=False)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_atomic_claim_and_hangup(self) -> None:
        token_one, agent_one, _ = login_agent(
            self.config, "agent01", "agent123"
        )
        token_two, agent_two, _ = login_agent(
            self.config, "agent02", "agent123"
        )
        self.assertTrue(token_one)
        self.assertTrue(token_two)

        call, caller_token, rtc_auth = create_call(
            self.config, client_id="caller-a", client_name="呼叫 A"
        )
        self.assertEqual(rtc_auth["channel"], call["roomId"])
        mark_caller_ready(self.config, call["id"], caller_token)

        claimed, agent_rtc = claim_call(self.config, call["id"], agent_one)
        self.assertEqual(claimed["status"], "connecting")
        self.assertEqual(claimed["claimedAgentId"], agent_one.id)
        self.assertEqual(agent_rtc["channel"], call["roomId"])
        with self.assertRaises(CallError):
            claim_call(self.config, call["id"], agent_two)

    def test_rtc_token_allows_audio_and_screen_publish(self) -> None:
        rtc_auth = make_rtc_auth(
            self.config,
            room_id="room-screen",
            user_id="caller-screen",
            user_name="屏幕共享测试",
        )
        parsed = AppToken.parse(rtc_auth["token"])
        privilege = parsed.service.privilege
        self.assertTrue(privilege & Service.ENABLE_AUDIO_PRIVILEGE)
        self.assertTrue(privilege & Service.ENABLE_SCREEN_PRIVILEGE)

    def test_duplicate_login_is_rejected_until_logout(self) -> None:
        token, _, _ = login_agent(self.config, "agent01", "agent123")
        self.assertTrue(token)
        with self.assertRaises(AuthError) as context:
            login_agent(self.config, "agent01", "agent123")
        self.assertEqual(context.exception.status_code, 409)

    def test_concurrent_login_only_one_succeeds(self) -> None:
        barrier = threading.Barrier(2)

        def attempt_login() -> int:
            barrier.wait()
            try:
                login_agent(self.config, "agent01", "agent123")
                return 200
            except AuthError as error:
                return error.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(lambda _: attempt_login(), range(2)))

        self.assertEqual(statuses.count(200), 1)
        self.assertEqual(statuses.count(409), 1)

    def test_concurrent_claim_only_one_of_ten_succeeds(self) -> None:
        agents = [
            login_agent(self.config, f"agent{index:02d}", "agent123")[1]
            for index in range(1, 11)
        ]
        call, _ = create_ready_call(self.config, "caller-concurrent")
        barrier = threading.Barrier(len(agents))

        def attempt_claim(agent) -> int:
            barrier.wait()
            try:
                claim_call(self.config, call["id"], agent)
                return 200
            except CallError as error:
                return error.status_code

        with ThreadPoolExecutor(max_workers=len(agents)) as pool:
            statuses = list(pool.map(attempt_claim, agents))

        self.assertEqual(statuses.count(200), 1)
        self.assertEqual(statuses.count(409), len(agents) - 1)
        self.assertEqual(get_call(self.config, call["id"])["status"], "connecting")

    def test_agent_hangup_ends_call_and_timeout_transitions(self) -> None:
        _, agent, _ = login_agent(self.config, "agent01", "agent123")
        call, _ = create_ready_call(self.config, "caller-timeout")

        claim_call(self.config, call["id"], agent)
        ended = agent_hangup(self.config, call["id"], agent)
        self.assertEqual(ended["status"], "ended")
        self.assertEqual(ended["endReason"], "agent_hangup")
        self.assertIsNotNone(ended["endedAt"])

        timeout_call, _ = create_ready_call(self.config, "caller-timeout-2")
        claim_call(self.config, timeout_call["id"], agent)
        age_call_timestamp(
            self.config,
            timeout_call["id"],
            "claimed_at",
            self.config.connecting_timeout_seconds + 1,
        )
        changed = expire_calls(self.config)
        self.assertIn(timeout_call["id"], changed)
        self.assertEqual(
            get_call(self.config, timeout_call["id"])["status"],
            "waiting",
        )

        claim_call(self.config, timeout_call["id"], agent)
        active = mark_agent_connected(self.config, timeout_call["id"], agent)
        self.assertEqual(active["status"], "active")
        self.assertEqual(
            mark_agent_disconnected(self.config, agent.id),
            [timeout_call["id"]],
        )
        interrupted = get_call(self.config, timeout_call["id"])
        self.assertEqual(interrupted["status"], "interrupted")
        self.assertEqual(interrupted["interruptedSide"], "agent")

        age_call_timestamp(
            self.config,
            timeout_call["id"],
            "interrupted_at",
            16,
        )
        changed = expire_calls(self.config)
        self.assertIn(timeout_call["id"], changed)
        interrupted_ended = get_call(self.config, timeout_call["id"])
        self.assertEqual(interrupted_ended["status"], "ended")
        self.assertEqual(
            interrupted_ended["endReason"],
            "interrupted_timeout",
        )

    def test_agent_cannot_claim_two_calls(self) -> None:
        _, agent, _ = login_agent(self.config, "agent01", "agent123")
        first, _ = create_ready_call(self.config, "caller-first")
        second, _ = create_ready_call(self.config, "caller-second")

        claim_call(self.config, first["id"], agent)
        with self.assertRaises(CallError) as context:
            claim_call(self.config, second["id"], agent)
        self.assertEqual(context.exception.status_code, 409)

    def test_logout_allows_relogin(self) -> None:
        first_token, first_agent, _ = login_agent(
            self.config, "agent01", "agent123"
        )
        logout_agent(self.config, first_agent.token_hash)
        second_token, _, _ = login_agent(self.config, "agent01", "agent123")
        self.assertNotEqual(first_token, second_token)

    def test_agent_login_has_no_fixed_expiry(self) -> None:
        token, agent, expires_at = login_agent(
            self.config, "agent01", "agent123"
        )
        self.assertEqual(expires_at, NO_EXPIRY_AT)
        old_time = iso_time(utc_now() - timedelta(hours=24))
        with db_session(self.config) as connection:
            connection.execute(
                """
                UPDATE agent_sessions
                SET expires_at = ?, last_seen_at = ?
                WHERE token_hash = ?
                """,
                (old_time, old_time, agent.token_hash),
            )

        authenticated = authenticate_access_token(self.config, token)
        self.assertEqual(authenticated.id, agent.id)

    def test_disabled_agent_token_is_rejected(self) -> None:
        token, agent, _ = login_agent(self.config, "agent01", "agent123")
        with db_session(self.config) as connection:
            connection.execute(
                "UPDATE agents SET enabled = 0 WHERE id = ?",
                (agent.id,),
            )

        with self.assertRaises(AuthError):
            authenticate_access_token(self.config, token)

    def test_restart_ends_all_unfinished_calls(self) -> None:
        _, agent_one, _ = login_agent(self.config, "agent01", "agent123")
        _, agent_two, _ = login_agent(self.config, "agent02", "agent123")
        waiting, _ = create_ready_call(self.config, "caller-waiting")
        connecting, _ = create_ready_call(self.config, "caller-connecting")
        active, _ = create_ready_call(self.config, "caller-active")

        claim_call(self.config, connecting["id"], agent_one)
        claim_call(self.config, active["id"], agent_two)
        mark_agent_connected(self.config, active["id"], agent_two)

        initialize_database(self.config)

        for call_id in (waiting["id"], connecting["id"], active["id"]):
            call = get_call(self.config, call_id)
            self.assertEqual(call["status"], "ended")
            self.assertEqual(call["endReason"], "backend_restart")

    def test_websocket_ticket_is_single_use(self) -> None:
        manager = ConnectionManager(self.config)
        ticket = manager.create_ticket("agent", "agent-test")
        consumed = manager.consume_ticket(ticket, "agent")
        self.assertIsNotNone(consumed)
        self.assertEqual(consumed.identity, "agent-test")
        self.assertIsNone(manager.consume_ticket(ticket, "agent"))

    def test_database_does_not_store_app_key_or_plaintext_password(self) -> None:
        with db_session(self.config) as connection:
            table_names = [
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
                if not row["name"].startswith("sqlite_")
            ]
            stored_values = []
            for table_name in table_names:
                rows = connection.execute(f'SELECT * FROM "{table_name}"').fetchall()
                for row in rows:
                    stored_values.extend(str(value) for value in tuple(row))

        serialized = "\n".join(stored_values)
        self.assertNotIn("test-app-key", serialized)
        self.assertNotIn("agent123", serialized)


class HttpContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.config = ServerConfig(
            app_id="test-app",
            app_key="test-app-key",
            db_path=Path(cls.temp.name) / "http.db",
            seed_agents=make_seed_agents(20),
        )
        cls.app = create_app(cls.config)
        cls.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        cls.socket.bind(("127.0.0.1", 0))
        cls.port = cls.socket.getsockname()[1]
        cls.socket.close()
        config = uvicorn.Config(
            cls.app,
            host="127.0.0.1",
            port=cls.port,
            log_level="error",
            lifespan="on",
        )
        cls.server = uvicorn.Server(config)
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        deadline = time.monotonic() + 10
        while not cls.server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        if not cls.server.started:
            raise RuntimeError("test server did not start")
        cls.base_url = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.should_exit = True
        cls.thread.join(timeout=5)
        cls.temp.cleanup()

    def login(self, username: str) -> str:
        status, payload = request_json(
            self.base_url,
            "POST",
            "/api/auth/login",
            body={"username": username, "password": "agent123"},
        )
        self.assertEqual(status, 200, payload)
        return payload["accessToken"]

    def test_http_call_flow_and_conflict(self) -> None:
        status, health = request_json(self.base_url, "GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["rtcConfigured"])
        self.assertNotIn("test-app-key", json.dumps(health))

        agent_one = self.login("agent01")
        agent_two = self.login("agent02")
        status, created = request_json(
            self.base_url,
            "POST",
            "/api/calls",
            body={"client_id": "caller-http", "client_name": "HTTP 呼叫"},
        )
        self.assertEqual(status, 200, created)
        call_id = created["call"]["id"]
        caller_token = created["callerToken"]

        status, ready = request_json(
            self.base_url,
            "POST",
            f"/api/calls/{call_id}/ready",
            headers={"X-Caller-Token": caller_token},
        )
        self.assertEqual(status, 200, ready)
        self.assertEqual(ready["call"]["status"], "waiting")

        status, first_claim = request_json(
            self.base_url,
            "POST",
            f"/api/calls/{call_id}/claim",
            token=agent_one,
        )
        self.assertEqual(status, 200, first_claim)
        self.assertEqual(first_claim["call"]["status"], "connecting")
        self.assertNotIn("test-app-key", json.dumps(first_claim))

        status, conflict = request_json(
            self.base_url,
            "POST",
            f"/api/calls/{call_id}/claim",
            token=agent_two,
        )
        self.assertEqual(status, 409, conflict)

        status, connected = request_json(
            self.base_url,
            "POST",
            f"/api/calls/{call_id}/connected",
            token=agent_one,
        )
        self.assertEqual(status, 200, connected)
        self.assertEqual(connected["call"]["status"], "active")

        status, ended = request_json(
            self.base_url,
            "POST",
            f"/api/calls/{call_id}/hangup",
            token=agent_one,
        )
        self.assertEqual(status, 200, ended)
        self.assertEqual(ended["call"]["status"], "ended")
        self.assertEqual(ended["call"]["endReason"], "agent_hangup")

        status, caller_view = request_json(
            self.base_url,
            "GET",
            f"/api/calls/{call_id}",
            headers={"X-Caller-Token": caller_token},
        )
        self.assertEqual(status, 200, caller_view)
        self.assertEqual(caller_view["call"]["status"], "ended")

    def test_private_network_cors_preflight_is_allowed(self) -> None:
        origin = "http://127.0.0.1:51947"
        request = Request(
            self.base_url + "/api/calls",
            method="OPTIONS",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
                "Access-Control-Request-Private-Network": "true",
            },
        )
        with urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(
                response.headers["Access-Control-Allow-Origin"],
                origin,
            )
            self.assertEqual(
                response.headers["Access-Control-Allow-Private-Network"],
                "true",
            )

    def test_unauthenticated_calls_and_invalid_caller_token(self) -> None:
        status, payload = request_json(self.base_url, "GET", "/api/calls")
        self.assertEqual(status, 401, payload)

        status, created = request_json(
            self.base_url,
            "POST",
            "/api/calls",
            body={"client_id": "caller-invalid-token", "client_name": "无效凭证"},
        )
        self.assertEqual(status, 200, created)
        status, payload = request_json(
            self.base_url,
            "POST",
            f"/api/calls/{created['call']['id']}/ready",
            headers={"X-Caller-Token": "wrong-token"},
        )
        self.assertEqual(status, 403, payload)

    def test_ten_concurrent_http_claims_only_one_succeeds(self) -> None:
        tokens = [self.login(f"agent{index:02d}") for index in range(3, 13)]
        status, created = request_json(
            self.base_url,
            "POST",
            "/api/calls",
            body={"client_id": "caller-http-concurrent", "client_name": "并发呼叫"},
        )
        self.assertEqual(status, 200, created)
        call_id = created["call"]["id"]
        caller_token = created["callerToken"]
        status, ready = request_json(
            self.base_url,
            "POST",
            f"/api/calls/{call_id}/ready",
            headers={"X-Caller-Token": caller_token},
        )
        self.assertEqual(status, 200, ready)

        barrier = threading.Barrier(len(tokens))

        def claim(token: str) -> int:
            barrier.wait()
            status_code, _ = request_json(
                self.base_url,
                "POST",
                f"/api/calls/{call_id}/claim",
                token=token,
            )
            return status_code

        with ThreadPoolExecutor(max_workers=len(tokens)) as pool:
            statuses = list(pool.map(claim, tokens))

        self.assertEqual(statuses.count(200), 1)
        self.assertEqual(statuses.count(409), len(tokens) - 1)

    def test_logout_allows_relogin_over_http(self) -> None:
        token = self.login("agent13")
        status, payload = request_json(
            self.base_url,
            "POST",
            "/api/auth/logout",
            token=token,
        )
        self.assertEqual(status, 200, payload)
        relogin_token = self.login("agent13")
        self.assertNotEqual(token, relogin_token)

    def test_local_shutdown_endpoint_requires_control_token(self) -> None:
        self.app.state.shutdown_token = "shutdown-test-token"
        shutdown_requests: list[bool] = []
        self.app.state.request_shutdown = lambda: shutdown_requests.append(True)

        status, payload = request_json(
            self.base_url,
            "POST",
            "/api/server/shutdown",
            headers={"X-Server-Control-Token": "wrong-token"},
        )
        self.assertEqual(status, 403, payload)
        self.assertEqual(shutdown_requests, [])

        status, payload = request_json(
            self.base_url,
            "POST",
            "/api/server/shutdown",
            headers={"X-Server-Control-Token": "shutdown-test-token"},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["message"], "shutting_down")
        self.assertEqual(shutdown_requests, [True])

    def test_websocket_snapshot_and_single_use_ticket(self) -> None:
        token = self.login("agent14")
        status, payload = request_json(
            self.base_url,
            "POST",
            "/api/auth/ws-ticket",
            token=token,
        )
        self.assertEqual(status, 200, payload)
        ws_url = (
            self.base_url.replace("http://", "ws://")
            + "/ws?ticket="
            + quote(payload["ticket"], safe="")
        )

        with websocket_connect(ws_url, open_timeout=3) as websocket:
            snapshot = json.loads(websocket.recv(timeout=3))
            self.assertEqual(snapshot["type"], "snapshot")
            self.assertIn("calls", snapshot)

        with self.assertRaises(InvalidStatus):
            with websocket_connect(ws_url, open_timeout=3):
                pass

    def test_agent_hangup_notifies_caller_and_ends_call(self) -> None:
        token = self.login("agent16")
        status, created = request_json(
            self.base_url,
            "POST",
            "/api/calls",
            body={"client_id": "caller-hangup-ws", "client_name": "同步挂断"},
        )
        self.assertEqual(status, 200, created)
        call_id = created["call"]["id"]
        caller_token = created["callerToken"]

        status, ready = request_json(
            self.base_url,
            "POST",
            f"/api/calls/{call_id}/ready",
            headers={"X-Caller-Token": caller_token},
        )
        self.assertEqual(status, 200, ready)

        status, ticket_payload = request_json(
            self.base_url,
            "POST",
            f"/api/calls/{call_id}/ws-ticket",
            headers={"X-Caller-Token": caller_token},
        )
        self.assertEqual(status, 200, ticket_payload)
        ws_url = (
            self.base_url.replace("http://", "ws://")
            + "/ws?ticket="
            + quote(ticket_payload["ticket"], safe="")
        )

        with websocket_connect(ws_url, open_timeout=3) as websocket:
            receive_call_status(websocket, "waiting")

            status, claimed = request_json(
                self.base_url,
                "POST",
                f"/api/calls/{call_id}/claim",
                token=token,
            )
            self.assertEqual(status, 200, claimed)
            receive_call_status(websocket, "connecting")

            status, connected = request_json(
                self.base_url,
                "POST",
                f"/api/calls/{call_id}/connected",
                token=token,
            )
            self.assertEqual(status, 200, connected)
            receive_call_status(websocket, "active")

            status, ended = request_json(
                self.base_url,
                "POST",
                f"/api/calls/{call_id}/hangup",
                token=token,
            )
            self.assertEqual(status, 200, ended)
            self.assertEqual(ended["call"]["status"], "ended")
            caller_call = receive_call_status(websocket, "ended")
            self.assertEqual(caller_call["endReason"], "agent_hangup")

    def test_agent_websocket_disconnect_marks_call_interrupted(self) -> None:
        token = self.login("agent15")
        status, created = request_json(
            self.base_url,
            "POST",
            "/api/calls",
            body={"client_id": "caller-ws-disconnect", "client_name": "断线呼叫"},
        )
        self.assertEqual(status, 200, created)
        call_id = created["call"]["id"]
        status, ready = request_json(
            self.base_url,
            "POST",
            f"/api/calls/{call_id}/ready",
            headers={"X-Caller-Token": created["callerToken"]},
        )
        self.assertEqual(status, 200, ready)
        status, claimed = request_json(
            self.base_url,
            "POST",
            f"/api/calls/{call_id}/claim",
            token=token,
        )
        self.assertEqual(status, 200, claimed)

        status, ticket_payload = request_json(
            self.base_url,
            "POST",
            "/api/auth/ws-ticket",
            token=token,
        )
        self.assertEqual(status, 200, ticket_payload)
        ws_url = (
            self.base_url.replace("http://", "ws://")
            + "/ws?ticket="
            + quote(ticket_payload["ticket"], safe="")
        )
        with websocket_connect(ws_url, open_timeout=3) as websocket:
            snapshot = json.loads(websocket.recv(timeout=3))
            self.assertEqual(snapshot["type"], "snapshot")

        deadline = time.monotonic() + 3
        call = get_call(self.config, call_id)
        while call["status"] != "interrupted" and time.monotonic() < deadline:
            time.sleep(0.05)
            call = get_call(self.config, call_id)
        self.assertEqual(call["status"], "interrupted")
        self.assertEqual(call["interruptedSide"], "agent")

if __name__ == "__main__":
    unittest.main()
