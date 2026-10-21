from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, QUrlQuery, Signal
from PySide6.QtWebSockets import QWebSocket
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.api import ApiClient
from agent.rtc_panel import RtcPanel


AGENT_DIR = Path(__file__).resolve().parent
ROOT_DIR = AGENT_DIR.parent


def resolve_agent_config_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT_DIR / path
    return path.resolve()


CONFIG_PATH = resolve_agent_config_path(
    os.getenv("RTC_AGENT_CONFIG", AGENT_DIR / "config.json")
)


def load_agent_config() -> dict:
    if CONFIG_PATH.is_file():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    return {"backend_url": "http://127.0.0.1:8000"}


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def format_duration(value: str | None) -> str:
    started = parse_time(value)
    if not started:
        return "-"
    seconds = max(0, int((datetime.now(timezone.utc) - started).total_seconds()))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


STATUS_LABELS = {
    "waiting": "等待接听",
    "connecting": "连接中",
    "active": "通话中",
    "interrupted": "连接异常",
    "ended": "已结束",
}


def find_next_waiting_call(calls: list[dict]) -> dict | None:
    waiting_calls = (call for call in calls if call.get("status") == "waiting")
    return min(
        waiting_calls,
        key=lambda call: (
            call.get("readyAt") or call.get("createdAt") or "",
            call.get("id") or "",
        ),
        default=None,
    )


ADMIN_CLIENT_STATUS_LABELS = {
    "pending": "待审批",
    "approved": "已批准",
    "rejected": "已拒绝",
    "disabled": "已停用",
}


class AgentWebSocket(QObject):
    message_received = Signal(dict)
    state_changed = Signal(bool, str)
    ticket_failed = Signal(str)

    def __init__(self, api: ApiClient, token: str, parent: QObject | None = None):
        super().__init__(parent)
        self.api = api
        self.token = token
        self.socket = QWebSocket()
        self.socket.textMessageReceived.connect(self._on_message)
        self.socket.connected.connect(lambda: self.state_changed.emit(True, "已连接"))
        self.socket.disconnected.connect(
            lambda: self.state_changed.emit(False, "状态连接已断开")
        )
        self.socket.errorOccurred.connect(
            lambda _error: self.state_changed.emit(False, "状态连接异常")
        )

    def open(self) -> None:
        self.api.request(
            "POST",
            "/api/auth/ws-ticket",
            token=self.token,
            callback=self._open_with_ticket,
        )

    def _open_with_ticket(self, ok: bool, status: int, payload: dict | None) -> None:
        if not ok or not payload or not payload.get("ticket"):
            message = (payload or {}).get("detail", f"HTTP {status}")
            self.ticket_failed.emit(str(message))
            return
        base = self.api.base_url.replace("https://", "wss://").replace(
            "http://", "ws://"
        )
        url = QUrl(base + "/ws")
        query = QUrlQuery()
        query.addQueryItem("ticket", payload["ticket"])
        url.setQuery(query)
        self.socket.open(url)

    def close(self) -> None:
        self.socket.close()

    def ping(self) -> None:
        if self.socket.isValid():
            self.socket.sendTextMessage("ping")

    def _on_message(self, text: str) -> None:
        if text == "pong":
            return
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return
        self.message_received.emit(payload)


class LoginPage(QWidget):
    login_requested = Signal(str, str, str)

    def __init__(self, backend_url: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("loginPage")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(0)
        layout.addStretch(1)

        card = QFrame()
        card.setObjectName("loginCard")
        card.setMinimumWidth(370)
        card.setMaximumWidth(430)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(32, 30, 32, 28)
        card_layout.setSpacing(10)

        title = QLabel("客服工作台")
        title.setObjectName("loginTitle")
        subtitle = QLabel("登录后接收新呼叫提示并点击接听")
        subtitle.setObjectName("mutedText")
        subtitle.setWordWrap(True)
        card_layout.addWidget(title)
        card_layout.addWidget(subtitle)
        card_layout.addSpacing(14)

        role_label = QLabel("登录身份")
        role_label.setObjectName("fieldLabel")
        self.role = QComboBox()
        self.role.addItem("客服登录", "agent")
        self.role.addItem("管理员登录", "admin")
        username_label = QLabel("登录账号")
        username_label.setObjectName("fieldLabel")
        self.username = QLineEdit()
        self.username.setPlaceholderText("登录账号")
        self.username.setMaxLength(64)
        password_label = QLabel("密码")
        password_label.setObjectName("fieldLabel")
        self.password = QLineEdit()
        self.password.setPlaceholderText("密码")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setMaxLength(256)
        self.login_button = QPushButton("登录")
        self.login_button.setObjectName("primaryButton")
        self.error = QLabel("")
        self.error.setObjectName("errorText")
        self.error.setWordWrap(True)
        self.error.setMinimumHeight(18)
        server = QLabel(f"控制服务：{backend_url}")
        server.setObjectName("serverText")
        server.setWordWrap(True)
        server.setAlignment(Qt.AlignmentFlag.AlignCenter)

        card_layout.addWidget(role_label)
        card_layout.addWidget(self.role)
        card_layout.addWidget(username_label)
        card_layout.addWidget(self.username)
        card_layout.addWidget(password_label)
        card_layout.addWidget(self.password)
        card_layout.addSpacing(6)
        card_layout.addWidget(self.login_button)
        card_layout.addWidget(self.error)
        card_layout.addWidget(server)

        layout.addWidget(card, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(2)

        self.login_button.clicked.connect(self._submit)
        self.password.returnPressed.connect(self._submit)
        self.username.setFocus()

    def _submit(self) -> None:
        username = self.username.text().strip()
        password = self.password.text()
        if not username or not password:
            self.show_error("请输入客服账号和密码")
            return
        self.login_requested.emit(self.role.currentData(), username, password)

    def set_busy(self, busy: bool) -> None:
        self.login_button.setEnabled(not busy)
        self.role.setEnabled(not busy)
        self.username.setEnabled(not busy)
        self.password.setEnabled(not busy)
        self.login_button.setText("正在登录..." if busy else "登录")

    def show_error(self, message: str) -> None:
        self.error.setText(message)


class AgentWindow(QMainWindow):
    def __init__(self, backend_url: str):
        super().__init__()
        self.backend_url = backend_url
        self.setWindowTitle("RTC 客服工作台")
        self.resize(1280, 800)
        self.setMinimumSize(1024, 640)

        self.api = ApiClient(backend_url, self)
        self.token: str | None = None
        self.admin_token: str | None = None
        self.agent: dict | None = None
        self.admin: dict | None = None
        self.calls: dict[str, dict] = {}
        self.clients: dict[str, dict] = {}
        self.current_call: dict | None = None
        self.websocket: AgentWebSocket | None = None
        self.reconnect_timer: QTimer | None = None
        self.reconnect_enabled = False
        self.claiming = False
        self.resuming_call_id: str | None = None
        self.muted = False
        self.incoming_call_id: str | None = None
        self.last_notified_waiting_call_id: str | None = None
        self.screen_focus_active = False

        self.stack = QStackedWidget(self)
        self.setCentralWidget(self.stack)
        self.login_page = LoginPage(backend_url)
        self.dashboard = self._build_dashboard()
        self.admin_dashboard = self._build_admin_dashboard()
        self.stack.addWidget(self.login_page)
        self.stack.addWidget(self.dashboard)
        self.stack.addWidget(self.admin_dashboard)
        self.login_page.login_requested.connect(self.login)

        self.heartbeat_timer = QTimer(self)
        self.heartbeat_timer.setInterval(5000)
        self.heartbeat_timer.timeout.connect(self.send_heartbeat)
        self.ws_ping_timer = QTimer(self)
        self.ws_ping_timer.setInterval(10000)
        self.ws_ping_timer.timeout.connect(
            lambda: self.websocket.ping() if self.websocket else None
        )
        self.table_timer = QTimer(self)
        self.table_timer.setInterval(1000)
        self.table_timer.timeout.connect(self.render_calls)
        self.client_timer = QTimer(self)
        self.client_timer.setInterval(5000)
        self.client_timer.timeout.connect(self.refresh_clients)
        self.admin_timer = QTimer(self)
        self.admin_timer.setInterval(5000)
        self.admin_timer.timeout.connect(self.refresh_admin_data)

        self._apply_style()

    def _build_dashboard(self) -> QWidget:
        page = QWidget()
        page.setObjectName("dashboardPage")
        root = QVBoxLayout(page)
        root.setContentsMargins(24, 20, 24, 22)
        root.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(10)
        title_block = QVBoxLayout()
        title_block.setSpacing(2)
        heading = QLabel("客服工作台")
        heading.setObjectName("pageTitle")
        title_block.addWidget(heading)
        self.agent_label = QLabel("未登录")
        self.agent_label.setObjectName("mutedText")
        title_block.addWidget(self.agent_label)
        self.connection_label = QLabel("状态连接中")
        self.connection_label.setObjectName("connectionBadge")
        self.connection_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.logout_button = QPushButton("退出登录")
        self.logout_button.setObjectName("secondaryButton")
        self.logout_button.clicked.connect(self.logout)
        header.addLayout(title_block)
        header.addStretch(1)
        header.addWidget(self.connection_label)
        header.addWidget(self.logout_button)
        root.addLayout(header)

        self.dashboard_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.dashboard_splitter.setChildrenCollapsible(False)
        self.dashboard_splitter.setHandleWidth(10)

        left = QFrame()
        left.setObjectName("sectionFrame")
        left.setMinimumWidth(420)
        self.dashboard_left = left
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(16, 15, 16, 16)
        left_layout.setSpacing(8)

        self.dashboard_tabs = QTabWidget()
        clients_page = QWidget()
        clients_layout = QVBoxLayout(clients_page)
        clients_layout.setContentsMargins(0, 8, 0, 0)
        self.client_hint = QLabel("已批准客户端")
        self.client_hint.setObjectName("mutedText")
        self.client_table = QTableWidget(0, 4)
        self.client_table.setHorizontalHeaderLabels(
            ["客户端名称", "客户端 ID", "状态", "当前会话"]
        )
        self.client_table.verticalHeader().setVisible(False)
        self.client_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.client_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.client_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        client_header = self.client_table.horizontalHeader()
        client_header.setStretchLastSection(False)
        client_header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        client_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.client_table.setColumnWidth(1, 120)
        self.client_table.setColumnWidth(2, 60)
        self.client_table.setColumnWidth(3, 110)
        clients_layout.addWidget(self.client_hint)
        clients_layout.addWidget(self.client_table, 1)

        calls_page = QWidget()
        calls_layout = QVBoxLayout(calls_page)
        calls_layout.setContentsMargins(0, 8, 0, 0)
        self.list_hint = QLabel("等待呼叫按呼入顺序排在前方，由客服点击接听")
        self.list_hint.setObjectName("mutedText")
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["呼叫方", "状态", "等待时长", "房间", "操作"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setDefaultSectionSize(48)
        table_header = self.table.horizontalHeader()
        table_header.setStretchLastSection(False)
        table_header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        table_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(1, 70)
        self.table.setColumnWidth(2, 70)
        self.table.setColumnWidth(3, 96)
        self.table.setColumnWidth(4, 68)
        calls_layout.addWidget(self.list_hint)
        calls_layout.addWidget(self.table, 1)
        self.dashboard_tabs.addTab(clients_page, "客户端")
        self.dashboard_tabs.addTab(calls_page, "通话房间")
        left_layout.addWidget(self.dashboard_tabs, 1)

        right = QFrame()
        right.setObjectName("sectionFrame")
        right.setMinimumWidth(500)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(12, 12, 12, 12)
        right_layout.setSpacing(6)
        right_title_row = QHBoxLayout()
        right_title_row.setSpacing(8)
        self.right_title = QLabel("当前通话")
        self.right_title.setObjectName("sectionTitle")
        self.screen_focus_button = QPushButton("放大共享画面")
        self.screen_focus_button.setObjectName("compactButton")
        self.screen_focus_button.setEnabled(False)
        self.screen_focus_button.clicked.connect(self.toggle_screen_focus)
        right_title_row.addWidget(self.right_title)
        right_title_row.addStretch(1)
        right_title_row.addWidget(self.screen_focus_button)
        self.current_status = QLabel("尚未接听")
        self.current_status.setObjectName("currentStatus")
        self.current_session = QLabel("当前空闲，等待新呼叫")
        self.current_session.setObjectName("mutedText")
        self.current_session.setWordWrap(True)
        self.remote_state = QLabel("")
        self.remote_state.setObjectName("remoteState")
        self.remote_state.setWordWrap(True)
        self.incoming_panel = QFrame()
        self.incoming_panel.setObjectName("incomingPanel")
        incoming_layout = QHBoxLayout(self.incoming_panel)
        incoming_layout.setContentsMargins(12, 10, 10, 10)
        incoming_layout.setSpacing(10)
        self.incoming_label = QLabel("")
        self.incoming_label.setObjectName("incomingLabel")
        self.incoming_label.setWordWrap(True)
        self.accept_incoming_button = QPushButton("接听")
        self.accept_incoming_button.setObjectName("incomingAcceptButton")
        self.accept_incoming_button.clicked.connect(self.accept_incoming_call)
        incoming_layout.addWidget(self.incoming_label, 1)
        incoming_layout.addWidget(self.accept_incoming_button)
        self.incoming_panel.hide()
        self.rtc_panel = RtcPanel()
        self.rtc_panel.setMinimumHeight(360)
        self.rtc_panel.hide()
        self.rtc_panel.event_received.connect(self.on_rtc_event)
        controls = QHBoxLayout()
        self.mute_button = QPushButton("静音")
        self.mute_button.setEnabled(False)
        self.mute_button.clicked.connect(self.toggle_mute)
        self.play_button = QPushButton("播放远端声音")
        self.play_button.setEnabled(False)
        self.play_button.clicked.connect(self.rtc_panel.play_remote_audio)
        self.hangup_button = QPushButton("挂断")
        self.hangup_button.setObjectName("dangerButton")
        self.hangup_button.setEnabled(False)
        self.hangup_button.clicked.connect(self.hangup_current)
        controls.setSpacing(8)
        controls.addWidget(self.mute_button, 1)
        controls.addWidget(self.play_button, 2)
        controls.addWidget(self.hangup_button, 1)
        right_layout.addLayout(right_title_row)
        right_layout.addWidget(self.current_status)
        right_layout.addWidget(self.current_session)
        right_layout.addWidget(self.remote_state)
        right_layout.addWidget(self.incoming_panel)
        right_layout.addWidget(self.rtc_panel, 1)
        right_layout.addLayout(controls)

        self.dashboard_splitter.addWidget(left)
        self.dashboard_splitter.addWidget(right)
        self.dashboard_splitter.setStretchFactor(0, 2)
        self.dashboard_splitter.setStretchFactor(1, 3)
        self.dashboard_splitter.setSizes([500, 700])
        root.addWidget(self.dashboard_splitter, 1)
        return page

    def toggle_screen_focus(self) -> None:
        self.set_screen_focus(not self.screen_focus_active)

    def set_screen_focus(self, focused: bool) -> None:
        self.screen_focus_active = focused
        self.dashboard_left.setVisible(not focused)
        self.current_status.setVisible(not focused)
        self.current_session.setVisible(not focused)
        self.remote_state.setVisible(not focused)
        self.incoming_panel.setVisible(False)
        self.right_title.setText("共享画面" if focused else "当前通话")
        self.screen_focus_button.setText(
            "恢复布局" if focused else "放大共享画面"
        )
        if not focused:
            self.dashboard_splitter.setSizes([500, 700])

    def _build_admin_dashboard(self) -> QWidget:
        page = QWidget()
        page.setObjectName("dashboardPage")
        root = QVBoxLayout(page)
        root.setContentsMargins(24, 20, 24, 22)
        root.setSpacing(14)

        header = QHBoxLayout()
        title_block = QVBoxLayout()
        title_block.setSpacing(2)
        title = QLabel("RTC 管理平台")
        title.setObjectName("pageTitle")
        self.admin_label = QLabel("未登录")
        self.admin_label.setObjectName("mutedText")
        title_block.addWidget(title)
        title_block.addWidget(self.admin_label)
        self.admin_logout_button = QPushButton("退出登录")
        self.admin_logout_button.setObjectName("secondaryButton")
        self.admin_logout_button.clicked.connect(self.admin_logout)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh_admin_data)
        header.addLayout(title_block)
        header.addStretch(1)
        header.addWidget(refresh)
        header.addWidget(self.admin_logout_button)
        root.addLayout(header)

        tabs = QTabWidget()

        clients_page = QWidget()
        clients_layout = QVBoxLayout(clients_page)
        client_actions = QHBoxLayout()
        for text, handler in (
            ("批准", self.approve_selected_client),
            ("拒绝", self.reject_selected_client),
            ("停用", self.disable_selected_client),
            ("恢复", self.enable_selected_client),
            ("改名", self.rename_selected_client),
            ("重置凭证", self.reset_selected_client_credential),
        ):
            button = QPushButton(text)
            button.clicked.connect(handler)
            client_actions.addWidget(button)
        client_actions.addStretch(1)
        self.admin_clients_table = QTableWidget(0, 5)
        self.admin_clients_table.setHorizontalHeaderLabels(
            ["显示名称", "客户端 ID", "审批状态", "占用", "安装 UUID"]
        )
        self.admin_clients_table.verticalHeader().setVisible(False)
        self.admin_clients_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self.admin_clients_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.admin_clients_table.setSelectionMode(
            QTableWidget.SelectionMode.SingleSelection
        )
        header_view = self.admin_clients_table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        clients_layout.addLayout(client_actions)
        clients_layout.addWidget(self.admin_clients_table, 1)

        agents_page = QWidget()
        agents_layout = QVBoxLayout(agents_page)
        agent_actions = QHBoxLayout()
        for text, handler in (
            ("新建客服", self.create_agent_account),
            ("修改显示名", self.rename_selected_agent),
            ("重置密码", self.reset_selected_agent_password),
            ("停用", self.disable_selected_agent),
            ("启用", self.enable_selected_agent),
        ):
            button = QPushButton(text)
            button.clicked.connect(handler)
            agent_actions.addWidget(button)
        agent_actions.addStretch(1)
        self.admin_agents_table = QTableWidget(0, 4)
        self.admin_agents_table.setHorizontalHeaderLabels(
            ["客服账号", "显示名称", "状态", "创建时间"]
        )
        self.admin_agents_table.verticalHeader().setVisible(False)
        self.admin_agents_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self.admin_agents_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.admin_agents_table.setSelectionMode(
            QTableWidget.SelectionMode.SingleSelection
        )
        self.admin_agents_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        agents_layout.addLayout(agent_actions)
        agents_layout.addWidget(self.admin_agents_table, 1)

        audit_page = QWidget()
        audit_layout = QVBoxLayout(audit_page)
        self.admin_audit_table = QTableWidget(0, 5)
        self.admin_audit_table.setHorizontalHeaderLabels(
            ["时间", "操作", "目标类型", "目标 ID", "详情"]
        )
        self.admin_audit_table.verticalHeader().setVisible(False)
        self.admin_audit_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self.admin_audit_table.setSelectionMode(
            QTableWidget.SelectionMode.NoSelection
        )
        audit_header = self.admin_audit_table.horizontalHeader()
        audit_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        audit_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        audit_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        audit_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        audit_header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        audit_layout.addWidget(self.admin_audit_table, 1)

        tabs.addTab(clients_page, "客户端审批")
        tabs.addTab(agents_page, "客服账号")
        tabs.addTab(audit_page, "管理审计")
        root.addWidget(tabs, 1)
        return page

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QStackedWidget {
                background: #f3f5f7;
            }
            QWidget {
                color: #1f2937;
                font: 14px "Segoe UI", "Microsoft YaHei UI";
            }
            QLabel {
                background: transparent;
            }
            #loginPage, #dashboardPage {
                background: #f3f5f7;
            }
            #loginCard {
                background: white;
                border: 1px solid #d9e1e8;
                border-radius: 8px;
            }
            #loginTitle {
                font-size: 26px;
                font-weight: 700;
                color: #17212b;
            }
            #pageTitle {
                font-size: 24px;
                font-weight: 700;
                color: #17212b;
            }
            #mutedText, #serverText {
                color: #667085;
            }
            #serverText {
                font-size: 12px;
            }
            #fieldLabel {
                margin-top: 2px;
                font-size: 13px;
                font-weight: 600;
                color: #344054;
            }
            #errorText {
                color: #b42318;
            }
            QLineEdit {
                min-height: 40px;
                padding: 0 11px;
                border: 1px solid #cfd8e3;
                border-radius: 6px;
                background: white;
                selection-background-color: #2563eb;
            }
            QLineEdit:focus {
                border: 1px solid #2563eb;
            }
            QComboBox {
                min-height: 38px;
                padding: 0 10px;
                border: 1px solid #cfd8e3;
                border-radius: 6px;
                background: white;
            }
            QTabWidget::pane {
                border: 0;
                background: white;
            }
            QTabBar::tab {
                min-width: 96px;
                padding: 8px 14px;
                border-bottom: 2px solid transparent;
                background: transparent;
                color: #667085;
                font-weight: 600;
            }
            QTabBar::tab:selected {
                border-bottom-color: #2563eb;
                color: #1d4ed8;
            }
            QPushButton {
                min-height: 36px;
                padding: 0 14px;
                border: 1px solid #c8d2dc;
                border-radius: 6px;
                background: #f8fafc;
                color: #344054;
                font-weight: 600;
            }
            QPushButton:hover:not(:disabled) {
                background: #f1f5f9;
                border-color: #98a7b7;
            }
            QPushButton:disabled {
                color: #98a2b3;
                background: #f8fafc;
            }
            #primaryButton {
                min-height: 42px;
                border-color: #2563eb;
                background: #2563eb;
                color: white;
            }
            #primaryButton:hover:not(:disabled) {
                border-color: #1d4ed8;
                background: #1d4ed8;
            }
            #dangerButton {
                border-color: #f0b4ae;
                background: #fff5f4;
                color: #b42318;
            }
            #dangerButton:hover:not(:disabled) {
                border-color: #d92d20;
                background: #feeceb;
            }
            #sectionFrame {
                background: white;
                border: 1px solid #d9e1e8;
                border-radius: 8px;
            }
            #sectionTitle {
                font-size: 16px;
                font-weight: 700;
                color: #17212b;
            }
            #currentStatus {
                font-size: 20px;
                font-weight: 700;
                color: #2563eb;
            }
            #remoteState {
                color: #087f5b;
            }
            #incomingPanel {
                border: 1px solid #f0c36d;
                border-radius: 6px;
                background: #fff7e5;
            }
            #incomingLabel {
                color: #8a4b00;
                font-weight: 700;
            }
            #incomingAcceptButton {
                border-color: #d97706;
                background: #d97706;
                color: white;
            }
            #compactButton {
                min-height: 30px;
                padding: 0 10px;
                font-size: 12px;
            }
            #connectionBadge {
                min-width: 86px;
                padding: 4px 10px;
                border-radius: 999px;
                background: #e8edf3;
                color: #52606d;
                font-size: 12px;
                font-weight: 600;
            }
            QTableWidget {
                border: 0;
                background: white;
                alternate-background-color: #f9fbfc;
                selection-background-color: #eaf1ff;
                selection-color: #17212b;
            }
            QHeaderView::section {
                min-height: 38px;
                padding: 0 8px;
                border: 0;
                border-bottom: 1px solid #e4e9ef;
                background: #f7f9fb;
                color: #667085;
                font-weight: 600;
            }
            QSplitter::handle {
                background: transparent;
            }
            """
        )

    def login(self, role: str, username: str, password: str) -> None:
        self.login_page.set_busy(True)
        self.login_page.show_error("")
        if role == "admin":
            self.api.request(
                "POST",
                "/api/admin/login",
                body={"username": username, "password": password},
                callback=self._on_admin_login,
            )
            return
        self.api.request(
            "POST",
            "/api/auth/login",
            body={"username": username, "password": password},
            callback=self._on_login,
        )

    def _on_admin_login(
        self,
        ok: bool,
        status: int,
        payload: dict | None,
    ) -> None:
        self.login_page.set_busy(False)
        if not ok:
            message = (payload or {}).get("detail", f"登录失败，HTTP {status}")
            self.login_page.show_error(str(message))
            return
        self.admin_token = (payload or {}).get("accessToken")
        self.admin = (payload or {}).get("admin")
        if not self.admin_token or not self.admin:
            self.login_page.show_error("管理员登录响应不完整")
            return
        self.admin_label.setText(f"超级管理员 · {self.admin['username']}")
        self.stack.setCurrentWidget(self.admin_dashboard)
        self.admin_timer.start()
        self.refresh_admin_data()

    def _on_login(self, ok: bool, status: int, payload: dict | None) -> None:
        self.login_page.set_busy(False)
        if not ok:
            message = (payload or {}).get("detail", f"登录失败，HTTP {status}")
            self.login_page.show_error(str(message))
            return
        self.token = (payload or {}).get("accessToken")
        self.agent = (payload or {}).get("agent")
        if not self.token or not self.agent:
            self.login_page.show_error("后端登录响应不完整")
            return
        self.agent_label.setText(
            f"{self.agent['displayName']}  ·  {self.agent['username']}"
        )
        self.stack.setCurrentWidget(self.dashboard)
        self.reconnect_enabled = True
        self.open_websocket()
        self.heartbeat_timer.start()
        self.ws_ping_timer.start()
        self.table_timer.start()
        self.client_timer.start()
        self.dashboard_tabs.setCurrentIndex(0)
        self.refresh_clients()

    def open_websocket(self) -> None:
        if not self.token or not self.reconnect_enabled:
            return
        if self.websocket:
            self.websocket.close()
            self.websocket.deleteLater()
        self.websocket = AgentWebSocket(self.api, self.token, self)
        self.websocket.message_received.connect(self.on_ws_message)
        self.websocket.state_changed.connect(self.on_connection_state)
        self.websocket.ticket_failed.connect(self.on_ws_ticket_failed)
        self.websocket.open()

    def schedule_reconnect(self) -> None:
        if not self.reconnect_enabled:
            return
        if self.reconnect_timer:
            self.reconnect_timer.stop()
        self.reconnect_timer = QTimer(self)
        self.reconnect_timer.setSingleShot(True)
        self.reconnect_timer.timeout.connect(self.open_websocket)
        self.reconnect_timer.start(1800)

    def on_connection_state(self, connected: bool, message: str) -> None:
        self.connection_label.setText("状态已连接" if connected else message)
        if connected:
            self.connection_label.setStyleSheet(
                "background:#d9f3e7;color:#157f55;border-radius:999px;padding:4px 9px;"
            )
        else:
            self.connection_label.setStyleSheet(
                "background:#fee4e2;color:#b42318;border-radius:999px;padding:4px 9px;"
            )
            self.schedule_reconnect()

    def on_ws_ticket_failed(self, message: str) -> None:
        self.connection_label.setText("无法连接状态服务")
        self.schedule_reconnect()

    def send_heartbeat(self) -> None:
        if not self.token:
            return
        self.api.request(
            "POST",
            "/api/auth/heartbeat",
            token=self.token,
            callback=self._on_heartbeat,
        )

    def _on_heartbeat(
        self, ok: bool, status: int, _payload: dict | None
    ) -> None:
        if not ok and status in (401, 403):
            self.on_auth_lost()

    def on_auth_lost(self) -> None:
        self.reconnect_enabled = False
        QMessageBox.warning(self, "登录失效", "客服登录已失效，请重新登录。")
        self.return_to_login()

    def on_ws_message(self, payload: dict) -> None:
        if payload.get("type") != "snapshot":
            return
        calls = payload.get("calls") or []
        self.calls = {call["id"]: call for call in calls}
        if self.current_call:
            updated = self.calls.get(self.current_call["id"])
            if not updated:
                self.finish_current_local("会话已结束")
            else:
                self.current_call = updated
                self.update_current_call()
            if updated and updated["status"] == "ended":
                self.finish_current_local("会话已结束")
            elif (
                self.current_call
                and updated["status"] == "waiting"
                and updated.get("claimedAgentId") is None
            ):
                self.finish_current_local("接听超时，请重新接听")
            elif (
                self.current_call
                and updated["status"] == "interrupted"
                and updated.get("claimedAgentId") == self.agent["id"]
                and updated.get("interruptedSide") == "agent"
            ):
                self.try_resume_current()
        if not self.current_call and self.agent:
            interrupted = next(
                (
                    call
                    for call in calls
                    if call.get("status") == "interrupted"
                    and call.get("claimedAgentId") == self.agent["id"]
                    and call.get("interruptedSide") == "agent"
                ),
                None,
            )
            if interrupted:
                self.current_call = interrupted
                self.rtc_panel.show()
                self.mute_button.setEnabled(True)
                self.play_button.setEnabled(True)
                self.hangup_button.setEnabled(True)
                self.screen_focus_button.setEnabled(True)
                self.remote_state.setText("检测到中断会话，正在恢复 RTC")
                self.update_current_call()
                self.try_resume_current()
        self.render_calls()
        self.refresh_clients()

    def refresh_clients(self) -> None:
        if not self.token:
            return
        self.api.request(
            "GET",
            "/api/clients",
            token=self.token,
            callback=self._on_clients_loaded,
        )

    def _on_clients_loaded(
        self,
        ok: bool,
        status: int,
        payload: dict | None,
    ) -> None:
        if not ok:
            return
        clients = (payload or {}).get("clients") or []
        self.clients = {client["id"]: client for client in clients}
        self.render_clients()

    def render_clients(self) -> None:
        clients = list(self.clients.values())
        self.client_table.setRowCount(len(clients))
        for row_index, client in enumerate(clients):
            name = QTableWidgetItem(client.get("displayName") or "-")
            client_id = QTableWidgetItem(client.get("id") or "-")
            availability = QTableWidgetItem(
                "空闲" if client.get("availability") == "idle" else "占用"
            )
            current_call = client.get("currentCall") or {}
            call_text = QTableWidgetItem(current_call.get("id") or "-")
            self.client_table.setItem(row_index, 0, name)
            self.client_table.setItem(row_index, 1, client_id)
            self.client_table.setItem(row_index, 2, availability)
            self.client_table.setItem(row_index, 3, call_text)
        idle_count = sum(
            1 for client in clients if client.get("availability") == "idle"
        )
        self.client_hint.setText(
            f"共 {len(clients)} 个客户端，{idle_count} 个空闲"
        )

    def render_calls(self) -> None:
        calls = list(self.calls.values())
        self.table.setRowCount(len(calls))
        busy_with_call = bool(self.current_call)
        for row_index, call in enumerate(calls):
            caller = QTableWidgetItem(call.get("callerName") or "-")
            status = QTableWidgetItem(
                STATUS_LABELS.get(call.get("status"), call.get("status", "-"))
            )
            duration = QTableWidgetItem(format_duration(call.get("createdAt")))
            room = QTableWidgetItem(str(call.get("roomId") or "-"))
            self.table.setItem(row_index, 0, caller)
            self.table.setItem(row_index, 1, status)
            self.table.setItem(row_index, 2, duration)
            self.table.setItem(row_index, 3, room)

            action = QPushButton("接听")
            can_accept = call.get("status") == "waiting" and not busy_with_call
            action.setEnabled(can_accept and not self.claiming)
            if call.get("status") != "waiting":
                claimed = call.get("claimedAgentId")
                action.setText("本人占用" if claimed == self.agent["id"] else "已占用")
            action.clicked.connect(
                lambda _checked=False, call_id=call["id"]: self.claim_call(call_id)
            )
            self.table.setCellWidget(row_index, 4, action)
        self.list_hint.setText(
            f"共 {len(calls)} 个未结束房间"
            if calls
            else "当前没有等待或进行中的房间"
        )
        self.update_incoming_call(calls)

    def update_incoming_call(self, calls: list[dict]) -> None:
        call = find_next_waiting_call(calls)
        if not call:
            self.incoming_call_id = None
            self.last_notified_waiting_call_id = None
            self.incoming_panel.hide()
            return
        if self.current_call or self.claiming:
            self.incoming_call_id = None
            self.incoming_panel.hide()
            return

        call_id = call["id"]
        self.incoming_call_id = call_id
        position = call.get("queuePosition")
        queue_text = f" · 排队第 {position} 位" if position else ""
        self.incoming_label.setText(
            f"新呼叫：{call.get('callerName') or '未知客户'}{queue_text}"
        )
        self.accept_incoming_button.setEnabled(not self.claiming)
        self.incoming_panel.show()
        if self.last_notified_waiting_call_id != call_id:
            self.last_notified_waiting_call_id = call_id
            QApplication.alert(self, 0)
            QApplication.beep()

    def accept_incoming_call(self) -> None:
        if self.incoming_call_id:
            self.claim_call(self.incoming_call_id)

    def claim_call(self, call_id: str) -> None:
        if self.claiming or self.current_call or not self.token:
            return
        self.claiming = True
        self.render_calls()
        self.api.request(
            "POST",
            f"/api/calls/{call_id}/claim",
            token=self.token,
            callback=self._on_claimed,
        )

    def _on_claimed(self, ok: bool, status: int, payload: dict | None) -> None:
        self.claiming = False
        if not ok:
            message = (payload or {}).get("detail", f"接听失败，HTTP {status}")
            QMessageBox.warning(self, "无法接听", str(message))
            self.refresh_calls()
            return
        call = (payload or {}).get("call")
        auth = (payload or {}).get("rtc")
        if not call or not auth:
            QMessageBox.warning(self, "接听失败", "后端响应缺少通话或 RTC 凭证。")
            return
        self._activate_agent_call(call, auth)

    def _activate_agent_call(self, call: dict, auth: dict) -> None:
        self.current_call = call
        self.current_status.setText("正在连接 RTC")
        self.current_session.setText(
            f"{call['callerName']} · {call['id']}"
        )
        self.remote_state.setText("正在申请客服麦克风")
        self.rtc_panel.show()
        self.mute_button.setEnabled(True)
        self.play_button.setEnabled(True)
        self.hangup_button.setEnabled(True)
        self.screen_focus_button.setEnabled(True)
        self.rtc_panel.join(auth)
        self.render_calls()

    def on_rtc_event(self, event: dict) -> None:
        event_type = event.get("type")
        if event_type == "joined":
            call = self.current_call
            if not call or not self.token:
                return
            self.api.request(
                "POST",
                f"/api/calls/{call['id']}/connected",
                token=self.token,
                callback=self._on_rtc_connected,
            )
            self.current_status.setText("通话中")
            self.remote_state.setText("RTC 已连接")
        elif event_type == "connecting":
            self.current_status.setText("正在连接 RTC")
        elif event_type == "media_ready":
            self.current_status.setText("正在连接 RTC")
            self.remote_state.setText("麦克风已获取，正在加入房间")
        elif event_type == "joined_room":
            self.current_status.setText("正在连接 RTC")
            self.remote_state.setText("已加入房间，正在发布麦克风")
        elif event_type == "permission":
            if event.get("granted"):
                self.remote_state.setText("麦克风权限已允许，正在获取设备")
            else:
                self.remote_state.setText("麦克风权限被拒绝")
        elif event_type == "remote_audio":
            if event.get("playing"):
                self.remote_state.setText("正在播放呼叫端声音")
            else:
                self.remote_state.setText("远端声音等待播放授权")
                self.play_button.setEnabled(True)
        elif event_type == "remote_screen":
            if event.get("playing"):
                self.remote_state.setText("正在查看呼叫端屏幕")
            else:
                self.remote_state.setText("呼叫端未共享屏幕")
        elif event_type == "muted":
            self.muted = bool(event.get("muted"))
            self.mute_button.setText("取消静音" if self.muted else "静音")
        elif event_type == "error":
            code = str(event.get("code") or "").strip()
            message = str(event.get("message") or "RTC 异常")
            detail = (
                f"{message}（错误码 {code}）"
                if code and code not in message
                else message
            )
            self.current_status.setText("RTC 连接失败")
            self.remote_state.setText(detail)
        elif event_type == "stopped":
            self.mute_button.setEnabled(False)
            self.play_button.setEnabled(False)

    def _on_rtc_connected(
        self, ok: bool, status: int, payload: dict | None
    ) -> None:
        if not ok:
            message = (payload or {}).get("detail", f"状态上报失败，HTTP {status}")
            self.finish_current_local("RTC 状态已失效，请重新接听")
            self.remote_state.setText(str(message))
            return
        call = (payload or {}).get("call")
        if call:
            self.current_call = call
            self.current_status.setText("通话中")
            self.remote_state.setText("双向音频已建立")
            self.refresh_calls()

    def update_current_call(self) -> None:
        if not self.current_call:
            return
        call = self.current_call
        self.current_status.setText(
            STATUS_LABELS.get(call["status"], call["status"])
        )
        self.current_session.setText(f"{call['callerName']} · {call['id']}")

    def try_resume_current(self) -> None:
        if (
            not self.current_call
            or not self.token
            or self.resuming_call_id == self.current_call["id"]
        ):
            return
        self.resuming_call_id = self.current_call["id"]
        call_id = self.current_call["id"]
        self.api.request(
            "POST",
            f"/api/calls/{call_id}/resume",
            token=self.token,
            callback=self._on_resumed,
        )

    def _on_resumed(self, ok: bool, status: int, payload: dict | None) -> None:
        call_id = self.resuming_call_id
        self.resuming_call_id = None
        if not ok:
            return
        call = (payload or {}).get("call")
        auth = (payload or {}).get("rtc")
        if call and auth and call_id == call["id"]:
            self.current_call = call
            self.rtc_panel.join(auth)
            self.current_status.setText("正在恢复 RTC")
            self.remote_state.setText("正在重新加入房间")

    def toggle_mute(self) -> None:
        if not self.current_call:
            return
        target = not self.muted
        self.rtc_panel.set_muted(target)

    def hangup_current(self) -> None:
        if not self.current_call or not self.token:
            return
        call_id = self.current_call["id"]
        self.rtc_panel.leave()
        self.api.request(
            "POST",
            f"/api/calls/{call_id}/hangup",
            token=self.token,
            callback=self._on_hangup,
        )

    def _on_hangup(self, ok: bool, status: int, payload: dict | None) -> None:
        if not ok:
            message = (payload or {}).get("detail", f"挂断失败，HTTP {status}")
            QMessageBox.warning(self, "挂断失败", str(message))
            return
        self.finish_current_local()
        self.refresh_calls()

    def finish_current_local(self, message: str | None = None) -> None:
        self.set_screen_focus(False)
        self.rtc_panel.leave()
        self.rtc_panel.hide()
        self.current_call = None
        self.muted = False
        self.current_status.setText("尚未接听")
        self.current_session.setText("从左侧选择一个等待中的房间")
        self.remote_state.setText("")
        self.mute_button.setText("静音")
        self.mute_button.setEnabled(False)
        self.play_button.setEnabled(False)
        self.hangup_button.setEnabled(False)
        self.screen_focus_button.setEnabled(False)
        if message:
            self.current_status.setText(message)
        self.render_calls()

    def refresh_calls(self) -> None:
        if not self.token:
            return
        self.api.request(
            "GET",
            "/api/calls",
            token=self.token,
            callback=self._on_refresh_calls,
        )

    def _on_refresh_calls(
        self, ok: bool, status: int, payload: dict | None
    ) -> None:
        if not ok:
            return
        calls = (payload or {}).get("calls") or []
        self.calls = {call["id"]: call for call in calls}
        self.render_calls()

    def refresh_admin_data(self) -> None:
        if not self.admin_token:
            return
        self.api.request(
            "GET",
            "/api/admin/clients",
            token=self.admin_token,
            callback=self._on_admin_clients,
        )
        self.api.request(
            "GET",
            "/api/admin/agents",
            token=self.admin_token,
            callback=self._on_admin_agents,
        )
        self.api.request(
            "GET",
            "/api/admin/audit",
            token=self.admin_token,
            callback=self._on_admin_audit,
        )

    def _on_admin_clients(
        self,
        ok: bool,
        status: int,
        payload: dict | None,
    ) -> None:
        if not ok:
            self._show_admin_error("加载客户端失败", status, payload)
            return
        clients = (payload or {}).get("clients") or []
        self.admin_clients_table.setRowCount(len(clients))
        for row_index, client in enumerate(clients):
            name = QTableWidgetItem(client.get("displayName") or "-")
            name.setData(Qt.ItemDataRole.UserRole, client.get("id"))
            client_id = QTableWidgetItem(client.get("id") or "-")
            approval = QTableWidgetItem(
                ADMIN_CLIENT_STATUS_LABELS.get(
                    client.get("status"),
                    client.get("status") or "-",
                )
            )
            availability = QTableWidgetItem(
                "空闲" if client.get("availability") == "idle" else "占用"
            )
            installation = QTableWidgetItem(client.get("installationUuid") or "-")
            self.admin_clients_table.setItem(row_index, 0, name)
            self.admin_clients_table.setItem(row_index, 1, client_id)
            self.admin_clients_table.setItem(row_index, 2, approval)
            self.admin_clients_table.setItem(row_index, 3, availability)
            self.admin_clients_table.setItem(row_index, 4, installation)

    def _on_admin_agents(
        self,
        ok: bool,
        status: int,
        payload: dict | None,
    ) -> None:
        if not ok:
            self._show_admin_error("加载客服账号失败", status, payload)
            return
        agents = (payload or {}).get("agents") or []
        self.admin_agents_table.setRowCount(len(agents))
        for row_index, agent in enumerate(agents):
            username = QTableWidgetItem(agent.get("username") or "-")
            username.setData(Qt.ItemDataRole.UserRole, agent.get("id"))
            display_name = QTableWidgetItem(agent.get("displayName") or "-")
            enabled = QTableWidgetItem("启用" if agent.get("enabled") else "停用")
            created = QTableWidgetItem(agent.get("createdAt") or "-")
            self.admin_agents_table.setItem(row_index, 0, username)
            self.admin_agents_table.setItem(row_index, 1, display_name)
            self.admin_agents_table.setItem(row_index, 2, enabled)
            self.admin_agents_table.setItem(row_index, 3, created)

    def _on_admin_audit(
        self,
        ok: bool,
        status: int,
        payload: dict | None,
    ) -> None:
        if not ok:
            self._show_admin_error("加载审计记录失败", status, payload)
            return
        events = (payload or {}).get("events") or []
        self.admin_audit_table.setRowCount(len(events))
        for row_index, event in enumerate(events):
            values = (
                event.get("createdAt") or "-",
                event.get("action") or "-",
                event.get("targetType") or "-",
                event.get("targetId") or "-",
                json.dumps(event.get("details") or {}, ensure_ascii=False),
            )
            for column, value in enumerate(values):
                self.admin_audit_table.setItem(
                    row_index,
                    column,
                    QTableWidgetItem(str(value)),
                )

    def _show_admin_error(
        self,
        title: str,
        status: int,
        payload: dict | None,
    ) -> None:
        message = (payload or {}).get("detail", f"HTTP {status}")
        QMessageBox.warning(self, title, str(message))

    def _admin_action(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
    ) -> None:
        if not self.admin_token:
            return
        self.api.request(
            method,
            path,
            body=body,
            token=self.admin_token,
            callback=self._on_admin_action,
        )

    def _on_admin_action(
        self,
        ok: bool,
        status: int,
        payload: dict | None,
    ) -> None:
        if not ok:
            self._show_admin_error("管理操作失败", status, payload)
            return
        self.refresh_admin_data()

    def _selected_admin_id(self, table: QTableWidget) -> str | None:
        row = table.currentRow()
        if row < 0:
            QMessageBox.information(self, "请选择记录", "请先选择一行。")
            return None
        item = table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def approve_selected_client(self) -> None:
        client_id = self._selected_admin_id(self.admin_clients_table)
        if client_id:
            self._admin_action("POST", f"/api/admin/clients/{client_id}/approve")

    def reject_selected_client(self) -> None:
        client_id = self._selected_admin_id(self.admin_clients_table)
        if not client_id:
            return
        reason, accepted = QInputDialog.getText(
            self,
            "拒绝客户端",
            "拒绝原因（可留空）：",
        )
        if accepted:
            self._admin_action(
                "POST",
                f"/api/admin/clients/{client_id}/reject",
                body={"reason": reason.strip() or None},
            )

    def disable_selected_client(self) -> None:
        client_id = self._selected_admin_id(self.admin_clients_table)
        if client_id:
            self._admin_action("POST", f"/api/admin/clients/{client_id}/disable")

    def enable_selected_client(self) -> None:
        client_id = self._selected_admin_id(self.admin_clients_table)
        if client_id:
            self._admin_action("POST", f"/api/admin/clients/{client_id}/enable")

    def rename_selected_client(self) -> None:
        client_id = self._selected_admin_id(self.admin_clients_table)
        if not client_id:
            return
        row = self.admin_clients_table.currentRow()
        current = self.admin_clients_table.item(row, 0).text()
        name, accepted = QInputDialog.getText(
            self,
            "修改客户端名称",
            "显示名称：",
            text=current,
        )
        if accepted and name.strip():
            self._admin_action(
                "PATCH",
                f"/api/admin/clients/{client_id}",
                body={"display_name": name.strip()},
            )

    def reset_selected_client_credential(self) -> None:
        client_id = self._selected_admin_id(self.admin_clients_table)
        if not client_id:
            return
        answer = QMessageBox.question(
            self,
            "重置客户端凭证",
            "重置后客户端需要重新提交注册并再次审批，继续吗？",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._admin_action(
                "POST",
                f"/api/admin/clients/{client_id}/reset-credential",
            )

    def create_agent_account(self) -> None:
        username, accepted = QInputDialog.getText(
            self,
            "新建客服",
            "登录用户名：",
        )
        if not accepted or not username.strip():
            return
        display_name, accepted = QInputDialog.getText(
            self,
            "新建客服",
            "显示名称：",
        )
        if not accepted or not display_name.strip():
            return
        password, accepted = QInputDialog.getText(
            self,
            "新建客服",
            "初始密码：",
            QLineEdit.EchoMode.Password,
        )
        if not accepted or not password:
            return
        self._admin_action(
            "POST",
            "/api/admin/agents",
            body={
                "username": username.strip(),
                "display_name": display_name.strip(),
                "password": password,
            },
        )

    def rename_selected_agent(self) -> None:
        agent_id = self._selected_admin_id(self.admin_agents_table)
        if not agent_id:
            return
        row = self.admin_agents_table.currentRow()
        current = self.admin_agents_table.item(row, 1).text()
        name, accepted = QInputDialog.getText(
            self,
            "修改客服显示名",
            "显示名称：",
            text=current,
        )
        if accepted and name.strip():
            self._admin_action(
                "PATCH",
                f"/api/admin/agents/{agent_id}",
                body={"display_name": name.strip()},
            )

    def reset_selected_agent_password(self) -> None:
        agent_id = self._selected_admin_id(self.admin_agents_table)
        if not agent_id:
            return
        password, accepted = QInputDialog.getText(
            self,
            "重置客服密码",
            "新密码：",
            QLineEdit.EchoMode.Password,
        )
        if accepted and password:
            self._admin_action(
                "POST",
                f"/api/admin/agents/{agent_id}/reset-password",
                body={"password": password},
            )

    def disable_selected_agent(self) -> None:
        agent_id = self._selected_admin_id(self.admin_agents_table)
        if agent_id:
            self._admin_action("POST", f"/api/admin/agents/{agent_id}/disable")

    def enable_selected_agent(self) -> None:
        agent_id = self._selected_admin_id(self.admin_agents_table)
        if agent_id:
            self._admin_action("POST", f"/api/admin/agents/{agent_id}/enable")

    def admin_logout(self) -> None:
        if self.admin_token:
            token = self.admin_token
            self.admin_token = None
            self.api.request(
                "POST",
                "/api/admin/logout",
                token=token,
            )
        self.return_to_login()

    def logout(self) -> None:
        if self.current_call:
            self.hangup_current()
        self.reconnect_enabled = False
        self.heartbeat_timer.stop()
        self.ws_ping_timer.stop()
        self.table_timer.stop()
        self.client_timer.stop()
        self.admin_timer.stop()
        if self.websocket:
            self.websocket.close()
            self.websocket = None
        if self.token:
            self.api.request(
                "POST", "/api/auth/logout", token=self.token
            )
        self.return_to_login()

    def return_to_login(self) -> None:
        self.reconnect_enabled = False
        self.heartbeat_timer.stop()
        self.ws_ping_timer.stop()
        self.table_timer.stop()
        self.client_timer.stop()
        self.admin_timer.stop()
        if self.websocket:
            self.websocket.close()
            self.websocket = None
        self.rtc_panel.leave()
        self.rtc_panel.hide()
        self.token = None
        self.agent = None
        self.admin_token = None
        self.admin = None
        self.calls.clear()
        self.clients.clear()
        self.current_call = None
        self.incoming_call_id = None
        self.last_notified_waiting_call_id = None
        self.set_screen_focus(False)
        self.incoming_panel.hide()
        self.screen_focus_button.setEnabled(False)
        self.screen_focus_button.setText("放大共享画面")
        self.table.setRowCount(0)
        self.client_table.setRowCount(0)
        self.admin_clients_table.setRowCount(0)
        self.admin_agents_table.setRowCount(0)
        self.admin_audit_table.setRowCount(0)
        self.login_page.password.clear()
        self.login_page.set_busy(False)
        self.stack.setCurrentWidget(self.login_page)

    def closeEvent(self, event) -> None:
        self.reconnect_enabled = False
        if self.current_call:
            self.hangup_current()
        if self.websocket:
            self.websocket.close()
        if self.admin_token:
            self.api.request(
                "POST",
                "/api/admin/logout",
                token=self.admin_token,
            )
        self.rtc_panel.close()
        super().closeEvent(event)


def main() -> int:
    config = load_agent_config()
    backend_url = config.get("backend_url", "http://127.0.0.1:8000")
    app = QApplication(sys.argv)
    app.setApplicationName("RTC 客服工作台")
    app.setStyle("Fusion")
    window = AgentWindow(backend_url)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
