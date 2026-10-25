from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import webbrowser
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .config import AgentWebConfig


WEB_DIR = Path(__file__).resolve().parent / "web"
STATIC_ROUTES = {
    "/": WEB_DIR / "index.html",
    "/index.html": WEB_DIR / "index.html",
    "/admin": WEB_DIR / "admin.html",
    "/admin.html": WEB_DIR / "admin.html",
    "/admin.js": WEB_DIR / "admin.js",
    "/app.js": WEB_DIR / "app.js",
    "/rtc.js": WEB_DIR / "rtc.js",
    "/styles.css": WEB_DIR / "styles.css",
    "/vendor/aliyun-rtc-sdk.js": WEB_DIR / "vendor" / "aliyun-rtc-sdk.js",
}


class AgentWebServer:
    def __init__(self, config: AgentWebConfig) -> None:
        self.config = config
        self.server = ThreadingHTTPServer(
            (config.host, config.port),
            self._build_handler(config),
        )
        self.server.daemon_threads = True

    @staticmethod
    def _build_handler(config: AgentWebConfig) -> type[BaseHTTPRequestHandler]:
        runtime_config = json.dumps(
            {"backendUrl": config.backend_url},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

        class Handler(BaseHTTPRequestHandler):
            def do_HEAD(self) -> None:
                self._serve(send_body=False)

            def do_GET(self) -> None:
                self._serve(send_body=True)

            def _serve(self, *, send_body: bool) -> None:
                path = urlsplit(self.path).path
                if path == "/agent-config.json":
                    body = runtime_config
                    content_type = "application/json; charset=utf-8"
                    cache_control = "no-store"
                else:
                    file_path = STATIC_ROUTES.get(path)
                    if file_path is None:
                        self.send_error(404)
                        return
                    try:
                        body = file_path.read_bytes()
                    except OSError:
                        self.send_error(404)
                        return
                    content_type = (
                        mimetypes.guess_type(file_path.name)[0]
                        or "application/octet-stream"
                    )
                    if file_path.suffix in {".html", ".js", ".css"}:
                        content_type = f"{content_type}; charset=utf-8"
                    cache_control = (
                        "no-store"
                        if file_path.suffix in {".html", ".js", ".css"}
                        else "public, max-age=86400"
                    )

                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", cache_control)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                if send_body:
                    self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                return

        return Handler

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    @property
    def open_host(self) -> str:
        if self.config.host in {"0.0.0.0", "::"}:
            return "127.0.0.1"
        return self.config.host

    @property
    def url(self) -> str:
        return f"http://{self.open_host}:{self.port}/"

    def serve_forever(self) -> None:
        self.server.serve_forever()

    def close(self) -> None:
        self.server.server_close()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="启动 ARTC Web 客服工作台")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="启动后不自动打开浏览器",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = AgentWebConfig.load()
        server = AgentWebServer(config)
    except (OSError, ValueError) as exc:
        print(f"Web 客服工作台启动失败：{exc}", file=sys.stderr)
        return 1

    print(f"Web 客服工作台：{server.url}")
    print(f"业务控制端：{config.backend_url}", flush=True)
    try:
        if config.auto_open_browser and not args.no_browser:
            opened = webbrowser.open(server.url, new=2)
            if not opened:
                print(
                    "未找到可用的图形浏览器，请在浏览器中打开上述地址。",
                    file=sys.stderr,
                    flush=True,
                )
        server.serve_forever()
        return 0
    except KeyboardInterrupt:
        print("正在停止 Web 客服工作台。", flush=True)
        return 0
    finally:
        server.close()


if __name__ == "__main__":
    raise SystemExit(main())
