from __future__ import annotations

import asyncio
import secrets
import tempfile
import unittest
import uuid
from pathlib import Path

from server.app import create_app
from server.auth import create_or_update_admin
from server.config import ServerConfig
from server.db import Database
from tests.asgi_client import request_json, websocket_messages


class ArtcApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "api.db"
        self.db = Database(self.db_path)
        self.db.initialize()
        create_or_update_admin(
            self.db,
            username="admin",
            password="admin-password",
            display_name="管理员",
        )
        self.config = ServerConfig(
            app_id="api-app",
            app_key="api-secret-key",
            db_path=self.db_path,
            presence_degraded_seconds=5,
            presence_timeout_seconds=10,
            takeover_lease_seconds=30,
            max_takeover_lease_seconds=120,
        )
        self.app = create_app(self.config, self.db)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(self, *args, **kwargs):
        return asyncio.run(request_json(self.app, *args, **kwargs))

    def login(self, username: str, password: str, admin: bool = False):
        path = "/api/admin/login" if admin else "/api/auth/login"
        status, _, payload = self.request(
            "POST",
            path,
            body={"username": username, "password": password},
        )
        self.assertEqual(status, 200, payload)
        return payload["accessToken"]

    @staticmethod
    def auth_headers(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def test_robot_self_registration_waits_for_approval(self) -> None:
        robot_id = str(uuid.uuid4())
        credential = secrets.token_urlsafe(24)

        status, _, registration = self.request(
            "POST",
            "/api/robot/register",
            body={
                "robotId": robot_id,
                "credential": credential,
                "displayName": "自注册机器人",
            },
        )
        self.assertEqual(status, 200, registration)
        self.assertEqual(registration["registrationState"], "pending")
        self.assertFalse(registration["approved"])
        self.assertNotIn(credential, str(registration))

        status, _, pending = self.request(
            "POST",
            "/api/robot/login",
            body={"robotId": robot_id, "credential": credential},
        )
        self.assertEqual(status, 403)
        self.assertEqual(pending["detail"], "机器人尚未审批")

        admin_token = self.login("admin", "admin-password", admin=True)
        status, _, _ = self.request(
            "POST",
            f"/api/admin/robots/{robot_id}/approve",
            headers=self.auth_headers(admin_token),
        )
        self.assertEqual(status, 200)

        status, _, duplicate = self.request(
            "POST",
            "/api/robot/register",
            body={
                "robotId": robot_id,
                "credential": credential,
                "displayName": "自注册机器人",
            },
        )
        self.assertEqual(status, 200, duplicate)
        self.assertTrue(duplicate["approved"])
        self.assertFalse(duplicate["created"])

        status, _, login = self.request(
            "POST",
            "/api/robot/login",
            body={"robotId": robot_id, "credential": credential},
        )
        self.assertEqual(status, 200, login)
        self.assertTrue(login["accessToken"])

        status, _, conflict = self.request(
            "POST",
            "/api/robot/register",
            body={
                "robotId": robot_id,
                "credential": secrets.token_urlsafe(24),
                "displayName": "冒名机器人",
            },
        )
        self.assertEqual(status, 409)
        self.assertIn("占用", conflict["detail"])

    def test_full_admin_robot_agent_api_flow(self) -> None:
        admin_token = self.login("admin", "admin-password", admin=True)
        admin_headers = self.auth_headers(admin_token)

        status, _, scenario = self.request(
            "POST",
            "/api/admin/scenarios",
            body={"name": "API 场景"},
            headers=admin_headers,
        )
        self.assertEqual(status, 200, scenario)
        scenario_id = scenario["id"]

        status, _, agent = self.request(
            "POST",
            "/api/admin/agents",
            body={
                "username": "api-agent",
                "password": "agent-password",
                "displayName": "API 客服",
                "scenarioIds": [scenario_id],
            },
            headers=admin_headers,
        )
        self.assertEqual(status, 200, agent)

        status, _, robot = self.request(
            "POST",
            "/api/admin/robots",
            body={"displayName": "API 机器人", "scenarioId": scenario_id},
            headers=admin_headers,
        )
        self.assertEqual(status, 200, robot)
        self.assertTrue(robot["credential"])

        status, _, _ = self.request(
            "POST",
            f"/api/admin/robots/{robot['id']}/approve",
            headers=admin_headers,
        )
        self.assertEqual(status, 200)

        status, _, robot_login = self.request(
            "POST",
            "/api/robot/login",
            body={
                "robotId": robot["id"],
                "credential": robot["credential"],
            },
        )
        self.assertEqual(status, 200, robot_login)
        robot_headers = self.auth_headers(robot_login["accessToken"])

        status, _, _ = self.request(
            "POST",
            "/api/robot/heartbeat",
            headers=robot_headers,
        )
        self.assertEqual(status, 200)

        status, _, session = self.request(
            "POST",
            "/api/robot/sessions/start",
            headers=robot_headers,
        )
        self.assertEqual(status, 200, session)

        agent_token = self.login("api-agent", "agent-password")
        agent_headers = self.auth_headers(agent_token)
        status, _, sessions = self.request(
            "GET",
            "/api/robot-sessions",
            headers=agent_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(sessions[0]["id"], session["id"])

        status, _, claimed = self.request(
            "POST",
            f"/api/robot-sessions/{session['id']}/claim",
            headers=agent_headers,
        )
        self.assertEqual(status, 200, claimed)
        self.assertEqual(claimed["claim"]["agentId"], agent["id"])

        status, _, viewer = self.request(
            "POST",
            f"/api/robot-sessions/{session['id']}/viewer-token",
            headers=agent_headers,
        )
        self.assertEqual(status, 200, viewer)
        self.assertEqual(viewer["channel"], robot["channelId"])

        status, _, lease = self.request(
            "POST",
            f"/api/robot-sessions/{session['id']}/takeover-lease",
            body={"requestedSeconds": 30},
            headers=agent_headers,
        )
        self.assertEqual(status, 200, lease)
        self.assertEqual(lease["agentId"], agent["id"])

        status, _, lease_with_mic = self.request(
            "POST",
            f"/api/robot-sessions/{session['id']}/takeover-lease/microphone",
            body={"enabled": True},
            headers=agent_headers,
        )
        self.assertEqual(status, 200, lease_with_mic)
        self.assertTrue(lease_with_mic["microphoneEnabled"])

        status, _, listen = self.request(
            "POST",
            f"/api/robot-sessions/{session['id']}/listen",
            body={"enabled": True},
            headers=agent_headers,
        )
        self.assertEqual(status, 200, listen)
        self.assertTrue(listen["robotMicrophoneRequested"])

        status, _, released = self.request(
            "POST",
            f"/api/robot-sessions/{session['id']}/takeover-lease/release",
            headers=agent_headers,
        )
        self.assertEqual(status, 200, released)
        self.assertEqual(released["status"], "released")
        self.assertFalse(released["microphoneEnabled"])

        status, _, ended_by_agent = self.request(
            "POST",
            f"/api/robot-sessions/{session['id']}/end",
            headers=agent_headers,
        )
        self.assertEqual(status, 200, ended_by_agent)
        self.assertEqual(ended_by_agent["status"], "ended")
        self.assertEqual(ended_by_agent["endReason"], "agent_ended")

        status, _, renamed = self.request(
            "PATCH",
            f"/api/admin/robots/{robot['id']}",
            body={"displayName": "API 机器人新名称"},
            headers=admin_headers,
        )
        self.assertEqual(status, 200, renamed)
        self.assertEqual(renamed["displayName"], "API 机器人新名称")

        status, _, deleted = self.request(
            "DELETE",
            f"/api/admin/robots/{robot['id']}",
            headers=admin_headers,
        )
        self.assertEqual(status, 200, deleted)
        self.assertTrue(deleted["ok"])
        status, _, sessions_after_delete = self.request(
            "GET",
            "/api/robot-sessions?includeEnded=true",
            headers=agent_headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(sessions_after_delete, [])
        status, _, _ = self.request(
            "POST",
            "/api/robot/heartbeat",
            headers=robot_headers,
        )
        self.assertEqual(status, 401)

        status, _, _ = self.request(
            "POST",
            f"/api/admin/agents/{agent['id']}/disable",
            headers=admin_headers,
        )
        self.assertEqual(status, 200)

        status, _, _ = self.request(
            "GET",
            "/api/scenarios",
            headers=agent_headers,
        )
        self.assertEqual(status, 401)

        status, _, audit = self.request(
            "GET",
            "/api/admin/audit?limit=100",
            headers=admin_headers,
        )
        self.assertEqual(status, 200)
        actions = {event["action"] for event in audit}
        self.assertIn("session_started", actions)
        self.assertIn("takeover_acquired", actions)
        self.assertIn("takeover_released", actions)
        self.assertIn("robot_renamed", actions)
        self.assertIn("robot_deleted", actions)
        self.assertNotIn(self.config.app_key, str(audit))

    def test_unauthorized_and_invalid_ticket_are_rejected(self) -> None:
        status, headers, _ = self.request("GET", "/api/robots")
        self.assertEqual(status, 401)
        self.assertEqual(headers.get("cache-control"), "no-store")
        self.assertEqual(headers.get("x-frame-options"), "DENY")

        messages = asyncio.run(
            websocket_messages(self.app, ticket="invalid-ticket")
        )
        self.assertEqual(messages[0]["type"], "websocket.close")
        self.assertEqual(messages[0]["code"], 4401)

    def test_websocket_snapshot_ticket_and_ping(self) -> None:
        token = self.login("admin", "admin-password", admin=True)
        status, _, ticket = self.request(
            "GET",
            "/api/auth/ws-ticket",
            headers=self.auth_headers(token),
        )
        self.assertEqual(status, 200, ticket)

        messages = asyncio.run(
            websocket_messages(self.app, ticket=ticket["ticket"])
        )
        self.assertEqual(messages[0]["type"], "websocket.accept")
        snapshot = messages[1]
        self.assertEqual(snapshot["type"], "snapshot")
        self.assertEqual(snapshot["principal"]["role"], "admin")
        self.assertEqual(messages[2]["type"], "pong")

        second = asyncio.run(
            websocket_messages(self.app, ticket=ticket["ticket"])
        )
        self.assertEqual(second[0]["type"], "websocket.close")

    def test_agent_websocket_disconnect_releases_takeover_lease(self) -> None:
        admin_token = self.login("admin", "admin-password", admin=True)
        admin_headers = self.auth_headers(admin_token)
        status, _, scenario = self.request(
            "POST",
            "/api/admin/scenarios",
            body={"name": "断线释放场景"},
            headers=admin_headers,
        )
        self.assertEqual(status, 200, scenario)
        status, _, agent = self.request(
            "POST",
            "/api/admin/agents",
            body={
                "username": "disconnect-agent",
                "password": "agent-password",
                "displayName": "断线客服",
                "scenarioIds": [scenario["id"]],
            },
            headers=admin_headers,
        )
        self.assertEqual(status, 200, agent)
        status, _, robot = self.request(
            "POST",
            "/api/admin/robots",
            body={"displayName": "断线机器人", "scenarioId": scenario["id"]},
            headers=admin_headers,
        )
        self.assertEqual(status, 200, robot)
        self.request(
            "POST",
            f"/api/admin/robots/{robot['id']}/approve",
            headers=admin_headers,
        )
        status, _, robot_login = self.request(
            "POST",
            "/api/robot/login",
            body={
                "robotId": robot["id"],
                "credential": robot["credential"],
            },
        )
        self.assertEqual(status, 200, robot_login)
        robot_headers = self.auth_headers(robot_login["accessToken"])
        self.request("POST", "/api/robot/heartbeat", headers=robot_headers)
        status, _, session = self.request(
            "POST",
            "/api/robot/sessions/start",
            headers=robot_headers,
        )
        self.assertEqual(status, 200, session)

        agent_token = self.login("disconnect-agent", "agent-password")
        agent_headers = self.auth_headers(agent_token)
        status, _, claimed = self.request(
            "POST",
            f"/api/robot-sessions/{session['id']}/claim",
            headers=agent_headers,
        )
        self.assertEqual(status, 200, claimed)
        status, _, lease = self.request(
            "POST",
            f"/api/robot-sessions/{session['id']}/takeover-lease",
            body={},
            headers=agent_headers,
        )
        self.assertEqual(status, 200, lease)
        status, _, listen = self.request(
            "POST",
            f"/api/robot-sessions/{session['id']}/listen",
            body={"enabled": True},
            headers=agent_headers,
        )
        self.assertEqual(status, 200, listen)
        self.assertTrue(listen["robotMicrophoneRequested"])
        status, _, ticket = self.request(
            "GET",
            "/api/auth/ws-ticket",
            headers=agent_headers,
        )
        self.assertEqual(status, 200, ticket)
        messages = asyncio.run(
            websocket_messages(self.app, ticket=ticket["ticket"])
        )
        self.assertEqual(messages[1]["type"], "snapshot")

        with self.db.connection() as connection:
            session_row = connection.execute(
                """
                SELECT status, claimed_by_agent_id
                FROM robot_sessions WHERE id = ?
                """,
                (session["id"],),
            ).fetchone()
            lease_row = connection.execute(
                "SELECT status,end_reason FROM takeover_leases WHERE id = ?",
                (lease["id"],),
            ).fetchone()
            listener_row = connection.execute(
                """
                SELECT enabled FROM session_listeners
                WHERE session_id = ? AND agent_id = ?
                """,
                (session["id"], agent["id"]),
            ).fetchone()
        self.assertEqual(session_row["status"], "active")
        self.assertIsNone(session_row["claimed_by_agent_id"])
        self.assertEqual(lease_row["status"], "released")
        self.assertEqual(lease_row["end_reason"], "agent_disconnected")
        self.assertEqual(listener_row["enabled"], 0)


if __name__ == "__main__":
    unittest.main()
