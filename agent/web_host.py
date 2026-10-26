from __future__ import annotations

import argparse
import hmac
import json
import mimetypes
import secrets
import sys
import traceback
import webbrowser
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .config import AgentWebConfig
from .native_audio import (
    BEAUTIFY_MODES,
    COMBO_VOICE_DEFAULTS,
    REVERB_MODES,
    VOICE_MODE_SDK_NAMES,
    VOICE_MODES,
    NativeAudioError,
    NativeAudioManager,
)


WEB_DIR = Path(__file__).resolve().parent / "web"
STATIC_ROUTES = {
    "/": WEB_DIR / "index.html",
    "/index.html": WEB_DIR / "index.html",
    "/admin": WEB_DIR / "admin.html",
    "/admin.html": WEB_DIR / "admin.html",
    "/admin.js": WEB_DIR / "admin.js",
    "/app.js": WEB_DIR / "app.js",
    "/native-audio.js": WEB_DIR / "native-audio.js",
    "/rtc.js": WEB_DIR / "rtc.js",
    "/styles.css": WEB_DIR / "styles.css",
    "/vendor/aliyun-rtc-sdk.js": WEB_DIR / "vendor" / "aliyun-rtc-sdk.js",
}


class AgentWebServer:
    def __init__(
        self,
        config: AgentWebConfig,
        native_audio: NativeAudioManager | None = None,
    ) -> None:
        self.config = config
        self.native_audio = native_audio or NativeAudioManager()
        self.control_token = secrets.token_urlsafe(32)
        self.server = ThreadingHTTPServer(
            (config.host, config.port),
            self._build_handler(
                config,
                self.native_audio,
                self.control_token,
            ),
        )
        self.server.daemon_threads = True

    @staticmethod
    def _build_handler(
        config: AgentWebConfig,
        native_audio: NativeAudioManager,
        control_token: str,
    ) -> type[BaseHTTPRequestHandler]:
        class Handler(BaseHTTPRequestHandler):
            def _runtime_config(self) -> bytes:
                return json.dumps(
                    {
                        "backendUrl": config.backend_url,
                        "nativeAudioAvailable": native_audio.available,
                        "agentControlToken": control_token,
                        "voiceModes": [
                            {
                                "value": mode,
                                "label": label,
                                "sdkName": VOICE_MODE_SDK_NAMES[mode],
                            }
                            for mode, label in VOICE_MODES.items()
                        ],
                        "comboVoiceDefaults": COMBO_VOICE_DEFAULTS,
                        "reverbModes": [
                            {"value": mode, "label": label}
                            for mode, label in REVERB_MODES.items()
                        ],
                        "beautifyModes": [
                            {"value": mode, "label": label}
                            for mode, label in BEAUTIFY_MODES.items()
                        ],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")

            def _host_is_loopback(self) -> bool:
                host_header = self.headers.get("Host", "")
                parsed = urlsplit(f"http://{host_header}")
                actual_port = int(self.server.server_address[1])
                return (
                    parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                    and parsed.port == actual_port
                )

            def _has_control_key(self) -> bool:
                supplied = self.headers.get("X-Agent-Key", "")
                return bool(supplied) and hmac.compare_digest(
                    supplied,
                    control_token,
                )

            def _post_is_trusted(self) -> bool:
                if not self._host_is_loopback() or not self._has_control_key():
                    return False
                expected_origin = f"http://{self.headers.get('Host', '')}"
                return self.headers.get("Origin", "") == expected_origin

            def _write_json(
                self,
                payload: dict[str, object],
                *,
                status: int = 200,
                send_body: bool = True,
            ) -> None:
                body = json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                if send_body:
                    self.wfile.write(body)

            def _read_json_body(self) -> dict[str, object]:
                try:
                    content_length = int(self.headers.get("Content-Length", "0"))
                except ValueError as exc:
                    raise ValueError("Content-Length 无效") from exc
                if content_length <= 0 or content_length > 65536:
                    raise ValueError("请求正文为空或过大")
                payload = json.loads(self.rfile.read(content_length))
                if not isinstance(payload, dict):
                    raise ValueError("请求正文必须是 JSON 对象")
                return payload

            def do_HEAD(self) -> None:
                self._serve_get(send_body=False)

            def do_GET(self) -> None:
                self._serve_get(send_body=True)

            def _serve_get(self, *, send_body: bool) -> None:
                path = urlsplit(self.path).path
                if path == "/api/native-audio/status":
                    if not self._host_is_loopback() or not self._has_control_key():
                        self._write_json(
                            {"detail": "本地音频控制未授权"},
                            status=403,
                            send_body=send_body,
                        )
                        return
                    self._write_json(
                        native_audio.status(),
                        send_body=send_body,
                    )
                    return
                if path == "/agent-config.json":
                    body = self._runtime_config()
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

            def do_POST(self) -> None:
                if not self._post_is_trusted():
                    self._write_json(
                        {"detail": "本地音频控制未授权"},
                        status=403,
                    )
                    return
                try:
                    body = self._read_json_body()
                    path = urlsplit(self.path).path
                    if path == "/api/native-audio/start":
                        token = str(body.get("token", "")).strip()
                        session_id = str(body.get("sessionId", "")).strip()
                        voice_mode = body.get("voiceMode", 0)
                        if not token or not session_id:
                            raise ValueError("token 和 sessionId 不能为空")
                        if type(voice_mode) is not int:
                            raise ValueError("voiceMode 必须是整数")
                        result = native_audio.start(
                            {"token": token},
                        session_id=session_id,
                        voice_mode=voice_mode,
                        combo_voice=body.get("comboVoice"),
                    )
                    elif path == "/api/native-audio/microphone":
                        enabled = body.get("enabled")
                        if not isinstance(enabled, bool):
                            raise ValueError("enabled 必须是布尔值")
                        result = native_audio.set_microphone(enabled)
                    elif path == "/api/native-audio/voice":
                        voice_mode = body.get("mode")
                        if type(voice_mode) is not int:
                            raise ValueError("mode 必须是整数")
                        result = native_audio.set_voice_mode(
                            voice_mode,
                            body.get("comboVoice"),
                        )
                    elif path == "/api/native-audio/token":
                        token = str(body.get("token", "")).strip()
                        if not token:
                            raise ValueError("token 不能为空")
                        result = native_audio.refresh_token(token)
                    elif path == "/api/native-audio/heartbeat":
                        result = native_audio.heartbeat()
                    elif path == "/api/native-audio/stop":
                        result = native_audio.stop()
                    else:
                        self._write_json({"detail": "接口不存在"}, status=404)
                        return
                    self._write_json(result)
                except ValueError as exc:
                    self._write_json({"detail": str(exc)}, status=400)
                except NativeAudioError as exc:
                    self._write_json({"detail": str(exc)}, status=409)
                except Exception:
                    traceback.print_exc()
                    self._write_json(
                        {"detail": "本地音频助手处理失败"},
                        status=500,
                    )

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
        self.native_audio.stop()
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
