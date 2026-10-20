from __future__ import annotations

import argparse
import json
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent.parent
CALLER_DIR = Path(__file__).resolve().parent
WEB_DIR = CALLER_DIR / "web"
SHARED_WEB_DIR = ROOT / "web"
CONFIG_PATH = CALLER_DIR / "config.json"
LOCAL_CONFIG_PATH = CALLER_DIR / "config.local.json"


def load_config() -> dict:
    config = {
        "backend_url": "http://127.0.0.1:8000",
        "default_name": "呼叫客户端 A",
        "port": 8765,
    }
    for path in (CONFIG_PATH, LOCAL_CONFIG_PATH):
        if path.is_file():
            config.update(json.loads(path.read_text(encoding="utf-8-sig")))
    return config


def ensure_installation_uuid(config: dict) -> str:
    installation_uuid = str(config.get("installation_uuid") or "")
    if installation_uuid:
        try:
            return str(uuid.UUID(installation_uuid))
        except ValueError:
            pass
    installation_uuid = str(uuid.uuid4())
    save_local_config(
        {
            "installation_uuid": installation_uuid,
            "client_credential": config.get("client_credential"),
        }
    )
    config["installation_uuid"] = installation_uuid
    return installation_uuid


def ensure_client_display_name(config: dict) -> str:
    configured_name = str(config.get("default_name") or "").strip()
    if configured_name:
        return configured_name[:64]

    generated_name = str(config.get("generated_display_name") or "").strip()
    if generated_name:
        return generated_name[:64]

    generated_name = "CALL-" + uuid.uuid4().hex[:8].upper()
    save_local_config({"generated_display_name": generated_name})
    config["generated_display_name"] = generated_name
    return generated_name


def save_local_config(values: dict) -> None:
    current = {}
    if LOCAL_CONFIG_PATH.is_file():
        try:
            current = json.loads(LOCAL_CONFIG_PATH.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            current = {}
    current.update(values)
    LOCAL_CONFIG_PATH.write_text(
        json.dumps(current, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def reset_installation_identity(
    public_config: dict,
    local_lock: threading.Lock,
) -> str:
    installation_uuid = str(uuid.uuid4())
    with local_lock:
        save_local_config(
            {
                "installation_uuid": installation_uuid,
                "client_credential": None,
            }
        )
        public_config["installationUuid"] = installation_uuid
        public_config["clientCredential"] = None
    return installation_uuid


def make_handler(
    public_config: dict,
    local_lock: threading.Lock,
):
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

        def do_POST(self):
            path = urlparse(self.path).path
            if path not in {"/client-credential", "/client-reset-identity"}:
                self.send_bytes(b"not found", "text/plain; charset=utf-8", 404)
                return
            origin = self.headers.get("Origin")
            expected_origin = f"http://{self.headers.get('Host', '')}"
            if origin and origin != expected_origin:
                self.send_bytes(
                    b'{"detail":"invalid origin"}',
                    "application/json; charset=utf-8",
                    403,
                )
                return
            content_type = self.headers.get("Content-Type", "")
            if not content_type.lower().startswith("application/json"):
                self.send_bytes(
                    b'{"detail":"application/json required"}',
                    "application/json; charset=utf-8",
                    415,
                )
                return
            if path == "/client-reset-identity":
                installation_uuid = reset_installation_identity(
                    public_config,
                    local_lock,
                )
                self.send_bytes(
                    json.dumps(
                        {
                            "installationUuid": installation_uuid,
                            "clientCredential": None,
                        }
                    ).encode("utf-8"),
                    "application/json; charset=utf-8",
                )
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 4096:
                    raise ValueError("invalid length")
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                credential = str(body["client_credential"])
                if not credential or len(credential) > 256:
                    raise ValueError("invalid credential")
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                self.send_bytes(
                    b'{"detail":"invalid request"}',
                    "application/json; charset=utf-8",
                    400,
                )
                return
            with local_lock:
                save_local_config(
                    {
                        "installation_uuid": public_config["installationUuid"],
                        "client_credential": credential,
                    }
                )
                public_config["clientCredential"] = credential
            self.send_bytes(
                b'{"ok":true}',
                "application/json; charset=utf-8",
            )

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="启动本机呼叫页面")
    parser.add_argument("--backend", help="集中后端地址")
    parser.add_argument("--name", help="呼叫客户端显示名称")
    parser.add_argument("--port", type=int, help="本机页面固定端口")
    parser.add_argument("--no-browser", action="store_true", help="只启动页面服务")
    args = parser.parse_args()

    config = load_config()
    backend_url = (args.backend or config.get("backend_url", "")).rstrip("/")
    if not backend_url.startswith(("http://", "https://")):
        raise ValueError("backend_url 必须是 http:// 或 https:// 地址")
    installation_uuid = ensure_installation_uuid(config)
    display_name = args.name or ensure_client_display_name(config)
    public_config = {
        "backendUrl": backend_url,
        "defaultName": display_name,
        "installationUuid": installation_uuid,
        "clientCredential": config.get("client_credential"),
    }
    port = args.port or int(config.get("port", 8765))
    local_lock = threading.Lock()

    try:
        server = ThreadingHTTPServer(
            ("127.0.0.1", port),
            make_handler(public_config, local_lock),
        )
    except OSError as error:
        raise OSError(
            f"无法监听本机端口 {port}，请关闭占用该端口的程序或使用 --port 指定其他端口"
        ) from error
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"呼叫页面: {url}", flush=True)
    print(f"集中后端: {backend_url}", flush=True)
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
