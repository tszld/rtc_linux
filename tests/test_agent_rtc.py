from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu")

from PySide6.QtCore import QUrl
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWidgets import QApplication

from agent.main import ROOT_DIR, find_next_waiting_call, resolve_agent_config_path
from agent.rtc_panel import AudioWebEnginePage


class AudioWebEnginePagePermissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_audio_capture_signal_grants_permission(self) -> None:
        page = AudioWebEnginePage()
        origin = QUrl("http://127.0.0.1:12345/")
        feature = QWebEnginePage.Feature.MediaAudioCapture
        decisions: list[tuple[str, bool]] = []
        page.permission_decided.connect(
            lambda decided_feature, granted: decisions.append(
                (decided_feature, granted)
            )
        )

        with patch.object(page, "setFeaturePermission") as set_permission:
            page.featurePermissionRequested.emit(origin, feature)

        set_permission.assert_called_once_with(
            origin,
            feature,
            QWebEnginePage.PermissionPolicy.PermissionGrantedByUser,
        )
        self.assertEqual(decisions, [("MediaAudioCapture", True)])
        page.deleteLater()


class AgentAssignmentTests(unittest.TestCase):
    def test_relative_agent_config_path_is_resolved_from_project_root(self) -> None:
        self.assertEqual(
            resolve_agent_config_path("agent/config.json"),
            (ROOT_DIR / "agent" / "config.json").resolve(),
        )

    def test_next_waiting_call_uses_ready_time_fifo(self) -> None:
        calls = [
            {
                "id": "call-newer",
                "status": "waiting",
                "readyAt": "2026-10-20T10:00:02+00:00",
            },
            {
                "id": "call-active",
                "status": "active",
            },
            {
                "id": "call-older",
                "status": "waiting",
                "readyAt": "2026-10-20T10:00:01+00:00",
            },
        ]

        waiting = find_next_waiting_call(calls)

        self.assertEqual(waiting["id"], "call-older")

if __name__ == "__main__":
    unittest.main()
