from __future__ import annotations

import unittest

from main import build_uvicorn_server
from server.config import ServerConfig


class ServerLaunchTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
