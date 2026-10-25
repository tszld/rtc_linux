from __future__ import annotations

import sys
from collections.abc import Callable, Sequence


COMPONENTS = {
    "server": "控制端",
    "robot": "机器人端",
    "agent": "Web 客服工作台",
}


def print_usage() -> None:
    print(
        "ARTC 统一入口\n"
        "\n"
        "用法：\n"
        "  python main.py              交互选择组件\n"
        "  python main.py server       启动控制端\n"
        "  python main.py robot        启动机器人端\n"
        "  python main.py agent        启动 Web 客服工作台\n"
        "\n"
        "统一配置：\n"
        "  config.json（模板：config.example.json）\n"
        "\n"
        "Web 客服工作台默认地址：http://127.0.0.1:8100/"
    )


def select_component(
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> str | None:
    output_fn("ARTC 统一入口")
    output_fn("")
    output_fn("  1. 启动控制端")
    output_fn("  2. 启动机器人端")
    output_fn("  3. 启动 Web 客服工作台")
    output_fn("  0. 退出")
    output_fn("")
    choices = {"1": "server", "2": "robot", "3": "agent"}
    while True:
        choice = input_fn("请选择 [0-3]: ").strip()
        if choice == "0":
            return None
        component = choices.get(choice)
        if component is not None:
            return component
        output_fn("请输入 0、1、2 或 3。")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] in {"-h", "--help", "help"}:
        print_usage()
        return 0

    if arguments:
        component = arguments.pop(0).lower()
    else:
        if not sys.stdin.isatty():
            print_usage()
            return 2
        selected = select_component()
        if selected is None:
            return 0
        component = selected

    if component not in COMPONENTS:
        print(
            f"未知组件：{component}；可用组件为 server、robot、agent。",
            file=sys.stderr,
        )
        return 2

    if component == "server":
        from server.cli import main as run_component
    elif component == "robot":
        from robot.business import main as run_component
    else:
        from agent.web_host import main as run_component
    return run_component(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
