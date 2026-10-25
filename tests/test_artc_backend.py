from __future__ import annotations

import base64
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path

from server.admin import (
    create_agent,
    create_robot,
    delete_robot,
    list_admin_robots,
    rename_robot,
    set_agent_enabled,
    set_robot_approval,
)
from server.auth import (
    AuthError,
    authenticate_agent,
    authenticate_robot,
    consume_ws_ticket,
    create_or_update_admin,
    issue_ws_ticket,
    resolve_access_token,
)
from server.config import ServerConfig
from server.db import Database
from server.errors import ConflictError
from server.presence import heartbeat, set_media_state, sweep_presence
from server.robots import list_visible_robots
from server.rtc import build_artc_token, make_rtc_auth
from server.scenarios import create_scenario
from server.security import hash_password, isoformat, utc_now, verify_password
from server.sessions import (
    claim_session,
    end_session,
    get_session,
    list_sessions_for_principal,
    release_session_claim,
    set_listen_request,
    start_session,
)
from server.takeover import (
    acquire_lease,
    expire_leases,
    get_active_lease,
    release_lease,
    renew_lease,
    set_lease_microphone,
)
from tools.diagnose_robot_auth import inspect_robot, inspect_robot_file


class DatabaseMigrationTests(unittest.TestCase):
    def test_v1_database_is_migrated_to_v3(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "v1.db"
            connection = sqlite3.connect(db_path)
            connection.executescript(
                """
                CREATE TABLE robots (id TEXT PRIMARY KEY);
                CREATE TABLE robot_sessions (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL
                );
                CREATE TABLE takeover_leases (id TEXT PRIMARY KEY);
                PRAGMA user_version = 1;
                """
            )
            connection.commit()
            connection.close()

            Database(db_path).initialize()

            connection = sqlite3.connect(db_path)
            robot_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(robots)"
                ).fetchall()
            }
            session_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(robot_sessions)"
                ).fetchall()
            }
            lease_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(takeover_leases)"
                ).fetchall()
            }
            version = connection.execute(
                "PRAGMA user_version"
            ).fetchone()[0]
            connection.close()

        self.assertEqual(version, 3)
        self.assertIn("deleted_at", robot_columns)
        self.assertIn("claimed_by_agent_id", session_columns)
        self.assertIn("claimed_at", session_columns)
        self.assertIn("microphone_published", lease_columns)

    def test_v2_database_is_migrated_to_v3(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "v2.db"
            connection = sqlite3.connect(db_path)
            connection.executescript(
                """
                CREATE TABLE takeover_leases (id TEXT PRIMARY KEY);
                PRAGMA user_version = 2;
                """
            )
            connection.commit()
            connection.close()

            Database(db_path).initialize()

            connection = sqlite3.connect(db_path)
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(takeover_leases)"
                ).fetchall()
            }
            version = connection.execute(
                "PRAGMA user_version"
            ).fetchone()[0]
            connection.close()

        self.assertEqual(version, 3)
        self.assertIn("microphone_published", columns)


class BackendFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "rtc.db"
        self.db = Database(self.db_path)
        self.config = ServerConfig(
            app_id="test-app",
            app_key="test-key",
            db_path=self.db_path,
            access_token_ttl_seconds=3600,
            rtc_token_ttl_seconds=600,
            ws_ticket_ttl_seconds=30,
            presence_degraded_seconds=5,
            presence_timeout_seconds=10,
            takeover_lease_seconds=30,
            max_takeover_lease_seconds=120,
        )
        self.db.initialize()
        self.admin = create_or_update_admin(
            self.db,
            username="admin",
            password="admin-password",
            display_name="管理员",
        )
        self.scenario = create_scenario(
            self.db,
            actor=self.admin,
            name="场景 A",
        )
        self.agent_a = create_agent(
            self.db,
            actor=self.admin,
            username="agent-a",
            password="agent-password-a",
            display_name="客服 A",
            scenario_ids=[self.scenario["id"]],
        )
        self.agent_b = create_agent(
            self.db,
            actor=self.admin,
            username="agent-b",
            password="agent-password-b",
            display_name="客服 B",
            scenario_ids=[self.scenario["id"]],
        )
        robot = create_robot(
            self.db,
            actor=self.admin,
            display_name="机器人 A",
            scenario_id=self.scenario["id"],
        )
        self.robot_id = robot["id"]
        self.robot_credential = robot["credential"]
        self.robot = set_robot_approval(
            self.db,
            actor=self.admin,
            robot_id=self.robot_id,
            approved=True,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def agent_principal(self, username: str, password: str):
        token = authenticate_agent(
            self.db,
            username=username,
            password=password,
            ttl_seconds=3600,
        )
        return resolve_access_token(self.db, token["accessToken"])

    def robot_principal(self):
        token = authenticate_robot(
            self.db,
            robot_id=self.robot_id,
            credential=self.robot_credential,
            ttl_seconds=3600,
        )
        return resolve_access_token(self.db, token["accessToken"])

    def start_robot(self):
        principal = self.robot_principal()
        heartbeat(self.db, robot_id=principal.id)
        session = start_session(self.db, robot_id=principal.id)
        return principal, session


class SecurityTests(BackendFixture):
    def test_password_hash_is_salted_and_verifiable(self) -> None:
        first = hash_password("correct horse battery staple")
        second = hash_password("correct horse battery staple")

        self.assertNotEqual(first, second)
        self.assertTrue(verify_password("correct horse battery staple", first))
        self.assertFalse(verify_password("wrong", first))

    def test_admin_and_agent_login_roles(self) -> None:
        admin = authenticate_agent(
            self.db,
            username="admin",
            password="admin-password",
            ttl_seconds=60,
            require_admin=True,
        )
        self.assertEqual(admin["principal"]["role"], "admin")

        with self.assertRaises(AuthError):
            authenticate_agent(
                self.db,
                username="agent-a",
                password="agent-password-a",
                ttl_seconds=60,
                require_admin=True,
            )


class ScenarioAndRobotTests(BackendFixture):
    def test_admin_can_rename_and_delete_robot(self) -> None:
        _, session = self.start_robot()
        renamed = rename_robot(
            self.db,
            actor=self.admin,
            robot_id=self.robot_id,
            display_name="机器人 A 新名称",
        )
        self.assertEqual(renamed["displayName"], "机器人 A 新名称")
        self.assertEqual(
            list_admin_robots(self.db)[0]["displayName"],
            "机器人 A 新名称",
        )

        deleted = delete_robot(
            self.db,
            actor=self.admin,
            robot_id=self.robot_id,
        )

        self.assertEqual(deleted["robotId"], self.robot_id)
        self.assertEqual(
            get_session(self.db, session["id"])["status"],
            "ended",
        )
        self.assertEqual(list_admin_robots(self.db), [])
        with self.assertRaises(AuthError):
            authenticate_robot(
                self.db,
                robot_id=self.robot_id,
                credential=self.robot_credential,
                ttl_seconds=60,
            )

    def test_robot_auth_diagnostic_reports_existing_record(self) -> None:
        result = inspect_robot(self.db, self.robot_id)

        self.assertTrue(result["robotExists"])
        self.assertTrue(result["approved"])
        self.assertTrue(result["enabled"])

    def test_robot_auth_diagnostic_reports_missing_record(self) -> None:
        result = inspect_robot(self.db, "missing-robot")

        self.assertEqual(result, {"robotExists": False})

    def test_robot_auth_diagnostic_reads_specific_database_file(self) -> None:
        result = inspect_robot_file(self.db_path, self.robot_id)

        self.assertTrue(result["robotExists"])
        self.assertTrue(result["approved"])
        self.assertTrue(result["enabled"])

    def test_agent_only_sees_authorized_scenarios(self) -> None:
        other_scenario = create_scenario(
            self.db,
            actor=self.admin,
            name="场景 B",
        )
        create_agent(
            self.db,
            actor=self.admin,
            username="agent-c",
            password="agent-password-c",
            display_name="客服 C",
            scenario_ids=[other_scenario["id"]],
        )
        agent_a = self.agent_principal("agent-a", "agent-password-a")
        agent_c = self.agent_principal("agent-c", "agent-password-c")
        heartbeat(self.db, robot_id=self.robot_id)

        robots_a = list_visible_robots(self.db, actor=agent_a)
        robots_c = list_visible_robots(self.db, actor=agent_c)

        self.assertEqual([robot["id"] for robot in robots_a], [self.robot_id])
        self.assertEqual(robots_c, [])

    def test_rtc_token_matches_user_and_channel_without_app_key(self) -> None:
        auth = make_rtc_auth(
            self.config,
            channel_id=self.robot["channelId"],
            user_id=f"robot-{self.robot_id}",
            user_name=self.robot["displayName"],
        )
        payload = json.loads(base64.b64decode(auth["token"]))

        self.assertEqual(payload["channelid"], self.robot["channelId"])
        self.assertEqual(payload["userid"], f"robot-{self.robot_id}")
        self.assertNotIn(self.config.app_key, base64.b64decode(auth["token"]).decode())

    def test_disabled_agent_token_is_rejected(self) -> None:
        login = authenticate_agent(
            self.db,
            username="agent-a",
            password="agent-password-a",
            ttl_seconds=60,
        )
        set_agent_enabled(
            self.db,
            actor=self.admin,
            agent_id=self.agent_a["id"],
            enabled=False,
        )

        with self.assertRaises(AuthError):
            resolve_access_token(self.db, login["accessToken"])


class PresenceAndSessionTests(BackendFixture):
    def test_heartbeat_and_expiry_transitions(self) -> None:
        robot_principal = self.robot_principal()
        result = heartbeat(self.db, robot_id=robot_principal.id)
        self.assertEqual(result["status"], "online")

        with self.db.transaction() as connection:
            connection.execute(
                """
                UPDATE robots SET last_seen_at = ?
                WHERE id = ?
                """,
                (
                    isoformat(utc_now() - timedelta(seconds=7)),
                    robot_principal.id,
                ),
            )
        degraded = sweep_presence(self.db, config=self.config)
        self.assertEqual(degraded[-1]["to"], "degraded")

        with self.db.transaction() as connection:
            connection.execute(
                """
                UPDATE robots SET last_seen_at = ?
                WHERE id = ?
                """,
                (
                    isoformat(utc_now() - timedelta(seconds=20)),
                    robot_principal.id,
                ),
            )
        offline = sweep_presence(self.db, config=self.config)
        self.assertEqual(offline[-1]["to"], "offline")

    def test_session_media_and_listener_state(self) -> None:
        _, session = self.start_robot()
        agent = self.agent_principal("agent-a", "agent-password-a")
        claim_session(
            self.db,
            actor=agent,
            session_id=session["id"],
        )

        media = set_media_state(
            self.db,
            robot_id=self.robot_id,
            session_id=session["id"],
            microphone_published=True,
            camera_published=False,
            automatic_speech_paused=True,
        )
        listen = set_listen_request(
            self.db,
            actor=agent,
            session_id=session["id"],
            enabled=True,
        )

        self.assertTrue(media["microphonePublished"])
        self.assertTrue(listen["robotMicrophoneRequested"])
        self.assertEqual(
            list_sessions_for_principal(
                self.db,
                actor=agent,
            )[0]["status"],
            "active",
        )

    def test_duplicate_session_is_rejected_and_end_is_final(self) -> None:
        _, session = self.start_robot()
        with self.assertRaises(ConflictError):
            start_session(self.db, robot_id=self.robot_id)

        end_session(
            self.db,
            session_id=session["id"],
            robot_id=self.robot_id,
            reason="test_end",
        )
        ended = get_session(self.db, session["id"])
        self.assertEqual(ended["status"], "ended")
        with self.assertRaises(ConflictError):
            end_session(
                self.db,
                session_id=session["id"],
                robot_id=self.robot_id,
                reason="double_end",
            )


class SessionClaimTests(BackendFixture):
    def test_only_one_agent_can_claim_a_session(self) -> None:
        _, session = self.start_robot()
        agent_a = self.agent_principal("agent-a", "agent-password-a")
        agent_b = self.agent_principal("agent-b", "agent-password-b")

        claimed = claim_session(
            self.db,
            actor=agent_a,
            session_id=session["id"],
        )
        self.assertEqual(claimed["claim"]["agentId"], agent_a.id)

        visible_to_b = list_sessions_for_principal(
            self.db,
            actor=agent_b,
        )
        self.assertEqual(
            visible_to_b[0]["claim"]["agentDisplayName"],
            agent_a.display_name,
        )
        with self.assertRaises(ConflictError):
            claim_session(
                self.db,
                actor=agent_b,
                session_id=session["id"],
            )
        with self.assertRaises(ConflictError):
            set_listen_request(
                self.db,
                actor=agent_b,
                session_id=session["id"],
                enabled=True,
            )

        released = release_session_claim(
            self.db,
            actor=agent_a,
            session_id=session["id"],
        )
        self.assertIsNone(released["session"]["claim"])
        claimed_by_b = claim_session(
            self.db,
            actor=agent_b,
            session_id=session["id"],
        )
        self.assertEqual(claimed_by_b["claim"]["agentId"], agent_b.id)

    def test_agent_cannot_claim_two_sessions(self) -> None:
        _, first_session = self.start_robot()
        second_robot = create_robot(
            self.db,
            actor=self.admin,
            display_name="机器人 B",
            scenario_id=self.scenario["id"],
        )
        set_robot_approval(
            self.db,
            actor=self.admin,
            robot_id=second_robot["id"],
            approved=True,
        )
        heartbeat(self.db, robot_id=second_robot["id"])
        second_session = start_session(
            self.db,
            robot_id=second_robot["id"],
        )
        agent_a = self.agent_principal("agent-a", "agent-password-a")
        claim_session(
            self.db,
            actor=agent_a,
            session_id=first_session["id"],
        )

        with self.assertRaises(ConflictError):
            claim_session(
                self.db,
                actor=agent_a,
                session_id=second_session["id"],
            )


class TakeoverTests(BackendFixture):
    def test_concurrent_claim_only_one_succeeds(self) -> None:
        _, session = self.start_robot()
        principals = []
        for index in range(10):
            username = f"agent-{index}"
            password = f"password-{index}"
            create_agent(
                self.db,
                actor=self.admin,
                username=username,
                password=password,
                display_name=f"客服 {index}",
                scenario_ids=[self.scenario["id"]],
            )
            principals.append(self.agent_principal(username, password))

        barrier = threading.Barrier(10)
        results: list[object] = []
        lock = threading.Lock()

        def claim(principal) -> None:
            barrier.wait()
            try:
                result = claim_session(
                    self.db,
                    actor=principal,
                    session_id=session["id"],
                )
            except Exception as exc:
                result = exc
            with lock:
                results.append(result)

        threads = [
            threading.Thread(target=claim, args=(principal,))
            for principal in principals
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        successes = [
            result for result in results if isinstance(result, dict)
        ]
        conflicts = [
            result for result in results if isinstance(result, ConflictError)
        ]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(conflicts), 9)
        self.assertEqual(get_session(self.db, session["id"])["status"], "active")

    def test_takeover_microphone_state_is_persisted_and_cleared(self) -> None:
        _, session = self.start_robot()
        agent_a = self.agent_principal("agent-a", "agent-password-a")
        agent_b = self.agent_principal("agent-b", "agent-password-b")
        claim_session(
            self.db,
            actor=agent_a,
            session_id=session["id"],
        )
        acquire_lease(
            self.db,
            actor=agent_a,
            session_id=session["id"],
            requested_seconds=30,
            max_seconds=120,
        )["lease"]

        updated = set_lease_microphone(
            self.db,
            actor=agent_a,
            session_id=session["id"],
            enabled=True,
        )["lease"]
        self.assertTrue(updated["microphoneEnabled"])
        self.assertTrue(
            get_session(self.db, session["id"])["takeover"][
                "microphoneEnabled"
            ]
        )
        with self.assertRaises(ConflictError):
            set_lease_microphone(
                self.db,
                actor=agent_b,
                session_id=session["id"],
                enabled=True,
            )

        released = release_lease(
            self.db,
            actor=agent_a,
            session_id=session["id"],
        )
        self.assertFalse(released["lease"]["microphoneEnabled"])

    def test_renew_release_and_expire(self) -> None:
        _, session = self.start_robot()
        agent = self.agent_principal("agent-a", "agent-password-a")
        claim_session(
            self.db,
            actor=agent,
            session_id=session["id"],
        )
        acquired = acquire_lease(
            self.db,
            actor=agent,
            session_id=session["id"],
            requested_seconds=30,
            max_seconds=120,
        )
        renewed = renew_lease(
            self.db,
            actor=agent,
            session_id=session["id"],
            requested_seconds=60,
            max_seconds=120,
        )
        self.assertNotEqual(
            acquired["lease"]["expiresAt"],
            renewed["lease"]["expiresAt"],
        )
        released = release_lease(
            self.db,
            actor=agent,
            session_id=session["id"],
        )
        self.assertEqual(released["lease"]["status"], "released")

        acquired_again = acquire_lease(
            self.db,
            actor=agent,
            session_id=session["id"],
            requested_seconds=30,
            max_seconds=120,
        )
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE takeover_leases SET expires_at = ? WHERE id = ?",
                (
                    isoformat(utc_now() - timedelta(seconds=1)),
                    acquired_again["lease"]["id"],
                ),
            )
        expired = expire_leases(self.db)
        self.assertEqual(expired[0]["status"], "expired")
        self.assertIsNone(get_active_lease(self.db, robot_id=self.robot_id))
        self.assertEqual(get_session(self.db, session["id"])["status"], "active")

    def test_expired_lease_is_cleared_after_database_reopen(self) -> None:
        _, session = self.start_robot()
        agent = self.agent_principal("agent-a", "agent-password-a")
        claim_session(
            self.db,
            actor=agent,
            session_id=session["id"],
        )
        lease = acquire_lease(
            self.db,
            actor=agent,
            session_id=session["id"],
            requested_seconds=30,
            max_seconds=120,
        )
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE takeover_leases SET expires_at = ? WHERE id = ?",
                (
                    isoformat(utc_now() - timedelta(seconds=1)),
                    lease["lease"]["id"],
                ),
            )

        reopened = Database(self.db_path)
        expired = expire_leases(reopened)

        self.assertEqual(expired[0]["status"], "expired")
        self.assertEqual(get_session(reopened, session["id"])["status"], "active")


class WebSocketTicketTests(BackendFixture):
    def test_ticket_is_single_use(self) -> None:
        principal = self.agent_principal("agent-a", "agent-password-a")
        ticket = issue_ws_ticket(
            self.db,
            principal=principal,
            ttl_seconds=30,
        )

        consumed = consume_ws_ticket(self.db, ticket["ticket"])
        self.assertEqual(consumed.id, principal.id)
        with self.assertRaises(AuthError):
            consume_ws_ticket(self.db, ticket["ticket"])


if __name__ == "__main__":
    unittest.main()
