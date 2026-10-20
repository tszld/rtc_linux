from __future__ import annotations

import os
import unittest
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from main import build_uvicorn_server, run_server
from server.admin import login_admin
from server.config import ROOT_DIR, ServerConfig, load_config


class ServerLaunchTests(unittest.TestCase):
    def test_relative_database_path_is_resolved_from_project_root(self) -> None:
        config = ServerConfig(
            app_id="test-app",
            app_key="test-key",
            db_path=Path("server/data/relative.db"),
        )

        self.assertEqual(
            config.db_path,
            (ROOT_DIR / "server/data/relative.db").resolve(),
        )

    def test_relative_database_environment_path_uses_project_root(self) -> None:
        with (
            patch.dict(
                os.environ,
                {"RTC_DB_PATH": "server/data/environment.db"},
                clear=False,
            ),
            patch("server.config._load_json", return_value={}),
        ):
            config = load_config()

        self.assertEqual(
            config.db_path,
            (ROOT_DIR / "server/data/environment.db").resolve(),
        )

    def test_server_has_graceful_and_control_plane_shutdown(self) -> None:
        config = ServerConfig(
            app_id="test-app",
            app_key="test-key",
            host="127.0.0.1",
            port=8123,
        )
        app, server = build_uvicorn_server(
            config,
            host="127.0.0.1",
            port=8123,
            shutdown_token="test-shutdown-token",
        )

        self.assertEqual(
            server.config.timeout_graceful_shutdown,
            3,
        )
        self.assertEqual(app.state.shutdown_token, "test-shutdown-token")
        self.assertTrue(callable(app.state.request_shutdown))

        app.state.request_shutdown()
        self.assertTrue(server.should_exit)

    def test_create_admin_command_prompts_for_password(self) -> None:
        with TemporaryDirectory() as temp:
            config = ServerConfig(
                app_id="test-app",
                app_key="test-key",
                db_path=Path(temp) / "admin.db",
            )
            args = Namespace(
                host=None,
                port=None,
                stop=False,
                create_admin=True,
                reset_admin_password=False,
                admin_username="admin",
            )
            with (
                patch("server.config.load_config", return_value=config),
                patch(
                    "main.getpass.getpass",
                    side_effect=["admin-password", "admin-password"],
                ),
            ):
                result = run_server(args)

            self.assertEqual(result, 0)
            token, admin = login_admin(
                config,
                username="admin",
                password="admin-password",
            )
            self.assertTrue(token)
            self.assertEqual(admin.username, "admin")

    def test_normal_server_start_does_not_prompt_for_admin_password(self) -> None:
        class FakeServer:
            def run(self) -> None:
                return

        with TemporaryDirectory() as temp:
            config = ServerConfig(
                app_id="test-app",
                app_key="test-key",
                db_path=Path(temp) / "server.db",
            )
            args = Namespace(
                host=None,
                port=None,
                stop=False,
                create_admin=False,
                reset_admin_password=False,
                admin_username="admin",
            )
            with (
                patch("server.config.load_config", return_value=config),
                patch(
                    "main.build_uvicorn_server",
                    return_value=(object(), FakeServer()),
                ),
                patch("main.getpass.getpass") as password_prompt,
            ):
                result = run_server(args)

        self.assertEqual(result, 0)
        password_prompt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
