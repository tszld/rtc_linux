from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from server.auth import create_or_update_admin
from server.db import Database
from server.security import verify_password
from tools.create_default_agent import create_default_agent


class DefaultAgentTests(unittest.TestCase):
    def test_creates_agent_and_default_scenario(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db = Database(Path(temporary) / "default-agent.db")
            db.initialize()
            create_or_update_admin(
                db,
                username="admin",
                password="admin-password",
                display_name="管理员",
            )

            result = create_default_agent(db)

            self.assertEqual(result["username"], "agent01")
            self.assertGreaterEqual(len(result["password"]), 8)
            self.assertEqual(len(result["scenarioIds"]), 1)
            with db.connection() as connection:
                agent = connection.execute(
                    "SELECT * FROM agents WHERE username = 'agent01'"
                ).fetchone()
                scenario = connection.execute(
                    "SELECT * FROM scenarios WHERE name = '默认场景'"
                ).fetchone()
                mapping = connection.execute(
                    "SELECT * FROM agent_scenarios WHERE agent_id = ?",
                    (result["agentId"],),
                ).fetchone()
            self.assertIsNotNone(agent)
            self.assertTrue(
                verify_password(result["password"], agent["password_hash"])
            )
            self.assertIsNotNone(scenario)
            self.assertEqual(mapping["scenario_id"], scenario["id"])


if __name__ == "__main__":
    unittest.main()
