from __future__ import annotations

import argparse
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent.parent
CALLER_DIR = Path(__file__).resolve().parent
WEB_DIR = CALLER_DIR / "web"
SHARED_WEB_DIR = ROOT / "web"


def load_config() -> dict:
    path = CALLER_DIR / "config.json"
    if not path.is_file():
        return {
            "backend_url": "http://127.0.0.1:8000",
            "client_id": "caller-pc-a",
            "default_name": "呼叫客户端 A",
        }
    return json.loads(path.read_text(encoding="utf-8-sig"))


def make_handler(config: dict, public_config: dict):
    static_routes = {
        "/": (WEB_DIR / "index.html", "text/html; charset=utf-8"),
        "/app.js": (WEB_DIR / "app.js", "application/javascript; charset=utf-8"),
        "/styles.css": (WEB_DIR / "styles.css", "text/css; charset=utf-8"),
        "/sdk.js": (SHARED_WEB_DIR / "sdk.js", "application/javascript; charset=utf-8"),
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def send_bytes(self, body: bytes, mime: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/config.json":
                self.send_bytes(
                    json.dumps(public_config, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            route = static_routes.get(path)
            if not route:
                self.send_bytes(b"not found", "text/plain; charset=utf-8", 404)
                return
            file_path, mime = route
            self.send_bytes(file_path.read_bytes(), mime)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="启动本机呼叫页面")
    parser.add_argument("--backend", help="集中后端地址")
    parser.add_argument("--client-id", help="呼叫客户端编号")
    parser.add_argument("--name", help="呼叫客户端显示名称")
    parser.add_argument("--no-browser", action="store_true", help="只启动页面服务")
    args = parser.parse_args()

    config = load_config()
    backend_url = (args.backend or config.get("backend_url", "")).rstrip("/")
    if not backend_url.startswith(("http://", "https://")):
        raise ValueError("backend_url 必须是 http:// 或 https:// 地址")
    client_id = args.client_id or config.get("client_id", "caller-pc-a")
    public_config = {
        "backendUrl": backend_url,
        "clientId": client_id,
        "defaultName": args.name or config.get("default_name", "呼叫客户端"),
    }

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler(config, public_config)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"呼叫页面: {url}", flush=True)
    print(f"集中后端: {backend_url}", flush=True)
    print(f"客户端编号: {client_id}", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()

