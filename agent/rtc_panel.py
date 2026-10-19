from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from PySide6.QtCore import QObject, QTimer, QUrl, Signal, Slot
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QVBoxLayout, QWidget


ROOT = Path(__file__).resolve().parent.parent
RTC_DIR = Path(__file__).resolve().parent / "rtc"
SHARED_WEB_DIR = ROOT / "web"


class LocalRtcServer:
    def __init__(self):
        routes = {
            "/": (RTC_DIR / "index.html", "text/html; charset=utf-8"),
            "/rtc.js": (RTC_DIR / "rtc.js", "application/javascript; charset=utf-8"),
            "/rtc.css": (RTC_DIR / "rtc.css", "text/css; charset=utf-8"),
            "/qwebchannel.js": (
                RTC_DIR / "qwebchannel.js",
                "application/javascript; charset=utf-8",
            ),
            "/sdk.js": (
                SHARED_WEB_DIR / "sdk.js",
                "application/javascript; charset=utf-8",
            ),
        }

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_GET(self):
                path = urlparse(self.path).path
                route = routes.get(path)
                if not route:
                    self.send_error(404)
                    return
                file_path, mime = route
                body = file_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class RtcBridge(QObject):
    event_received = Signal(str)

    @Slot(str)
    def emitEvent(self, payload: str) -> None:
        self.event_received.emit(payload)


class AudioWebEnginePage(QWebEnginePage):
    permission_decided = Signal(str, bool)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.featurePermissionRequested.connect(
            self._handle_feature_permission_requested
        )

    def _handle_feature_permission_requested(
        self, security_origin: QUrl, feature
    ) -> None:
        audio_feature = QWebEnginePage.Feature.MediaAudioCapture
        policy = QWebEnginePage.PermissionPolicy
        granted = feature == audio_feature
        if granted:
            self.setFeaturePermission(
                security_origin,
                feature,
                policy.PermissionGrantedByUser,
            )
        else:
            self.setFeaturePermission(
                security_origin,
                feature,
                policy.PermissionDeniedByUser,
            )
        self.permission_decided.emit(feature.name, granted)


class RtcPanel(QWidget):
    event_received = Signal(dict)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.local_server = LocalRtcServer()
        self.local_server.start()
        self.pending_auth: dict | None = None
        self.page_ready = False
        self.bridge_ready = False
        self.session_active = False
        self.bridge_timeout = QTimer(self)
        self.bridge_timeout.setSingleShot(True)
        self.bridge_timeout.setInterval(8000)
        self.bridge_timeout.timeout.connect(self._on_bridge_timeout)

        self.view = QWebEngineView(self)
        self.page = AudioWebEnginePage(self)
        self.view.setPage(self.page)
        try:
            self.view.settings().setAttribute(
                QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture,
                False,
            )
        except AttributeError:
            pass

        self.bridge = RtcBridge(self)
        self.bridge.event_received.connect(self._on_js_event)
        self.channel = QWebChannel(self)
        self.channel.registerObject("qtBridge", self.bridge)
        self.page.setWebChannel(self.channel)
        self.page.permission_decided.connect(self._on_permission_decided)
        self.page.loadStarted.connect(self._on_load_started)
        self.page.loadFinished.connect(self._on_load_finished)
        self.page.load(QUrl(self.local_server.url))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)

    def _on_load_started(self) -> None:
        self.page_ready = False
        self.bridge_ready = False

    def _on_permission_decided(self, feature: str, granted: bool) -> None:
        if feature != QWebEnginePage.Feature.MediaAudioCapture.name:
            return
        self.event_received.emit(
            {
                "type": "permission",
                "feature": feature,
                "granted": granted,
            }
        )

    def _on_load_finished(self, ok: bool) -> None:
        self.page_ready = ok
        if not ok:
            self.pending_auth = None
            self.bridge_timeout.stop()
            self.event_received.emit(
                {"type": "error", "message": "本地 RTC 页面加载失败"}
            )
            return
        self._run_pending_start()

    def _run_pending_start(self) -> None:
        if not self.page_ready or not self.bridge_ready or not self.pending_auth:
            return
        auth = self.pending_auth
        self.pending_auth = None
        self.bridge_timeout.stop()
        self._run_start(auth)

    def _on_bridge_timeout(self) -> None:
        if not self.pending_auth or self.bridge_ready:
            return
        self.pending_auth = None
        self.event_received.emit(
            {
                "type": "error",
                "code": "BRIDGE_TIMEOUT",
                "message": "RTC 页面通信初始化超时，请重试。",
            }
        )

    def _run_start(self, auth: dict) -> None:
        payload = json.dumps(auth, ensure_ascii=False)
        self.session_active = True
        self.page.runJavaScript(f"window.rtcSession.start({payload});")

    def join(self, auth: dict) -> None:
        self.pending_auth = auth
        self.bridge_timeout.start()
        self._run_pending_start()

    def leave(self) -> None:
        self.pending_auth = None
        self.bridge_timeout.stop()
        self.session_active = False
        if self.page_ready:
            self.page.runJavaScript("window.rtcSession.stop();")

    def set_muted(self, muted: bool) -> None:
        if self.page_ready:
            value = "true" if muted else "false"
            self.page.runJavaScript(f"window.rtcSession.setMuted({value});")

    def play_remote_audio(self) -> None:
        if self.page_ready:
            self.page.runJavaScript("window.rtcSession.playRemoteAudio();")

    def _on_js_event(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"type": "error", "message": "RTC 页面返回了无效消息"}
        if not isinstance(payload, dict):
            payload = {"type": "error", "message": "RTC 页面返回了无效消息"}
        if payload.get("type") == "ready":
            self.bridge_ready = True
            self._run_pending_start()
        self.event_received.emit(payload)

    def closeEvent(self, event) -> None:
        try:
            self.leave()
        except Exception:
            pass
        self.local_server.stop()
        super().closeEvent(event)
