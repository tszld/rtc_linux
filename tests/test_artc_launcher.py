from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import main
from agent.web_host import parse_args as parse_agent_args
from robot.business import parse_args as parse_robot_args
from server.cli import parse_args as parse_server_args


class ArtcLauncherTests(unittest.TestCase):
    def test_help_lists_runtime_components_and_web_agent(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main.main(["--help"]), 0)

        text = output.getvalue()
        self.assertIn("python main.py server", text)
        self.assertIn("python main.py robot", text)
        self.assertIn("python main.py agent", text)
        self.assertIn("http://127.0.0.1:8100/", text)
        self.assertIn("config.json", text)

    def test_component_selection_maps_to_robot(self) -> None:
        answers = iter(["9", "2"])
        messages: list[str] = []

        selected = main.select_component(
            input_fn=lambda _: next(answers),
            output_fn=messages.append,
        )

        self.assertEqual(selected, "robot")
        self.assertIn("请输入 0、1、2 或 3。", messages)

    def test_component_selection_maps_to_web_agent(self) -> None:
        selected = main.select_component(
            input_fn=lambda _: "3",
            output_fn=lambda _: None,
        )

        self.assertEqual(selected, "agent")

    def test_unknown_component_is_rejected(self) -> None:
        error = io.StringIO()
        with redirect_stderr(error):
            result = main.main(["caller"])

        self.assertEqual(result, 2)
        self.assertIn("未知组件", error.getvalue())

    def test_component_arguments_are_forwarded_without_json_path(self) -> None:
        with patch("server.cli.main", return_value=7) as server_main:
            result = main.main(["server", "--init-only"])

        self.assertEqual(result, 7)
        server_main.assert_called_once_with(["--init-only"])

    def test_web_agent_arguments_are_forwarded(self) -> None:
        with patch("agent.web_host.main", return_value=6) as agent_main:
            with patch("sys.platform", "win32"):
                result = main.main(["agent", "--no-browser"])

        self.assertEqual(result, 6)
        agent_main.assert_called_once_with(["--no-browser"])

    def test_non_tty_without_component_prints_usage(self) -> None:
        with patch("sys.stdin.isatty", return_value=False):
            output = io.StringIO()
            with redirect_stdout(output):
                result = main.main([])

        self.assertEqual(result, 2)
        self.assertIn("http://127.0.0.1:8100/", output.getvalue())

    def test_non_linux_platform_is_rejected(self) -> None:
        error = io.StringIO()
        with patch("sys.platform", "win32"):
            with redirect_stderr(error):
                result = main.main(["server"])

        self.assertEqual(result, 1)
        self.assertIn("只支持 Linux", error.getvalue())

    def test_component_entries_reject_manual_json_paths(self) -> None:
        for parser in (
            parse_server_args,
            parse_robot_args,
            parse_agent_args,
        ):
            with self.subTest(parser=parser.__module__):
                with redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        parser(["--config", "config.json"])


if __name__ == "__main__":
    unittest.main()
