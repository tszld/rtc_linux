from __future__ import annotations

import json
from typing import Callable

from PySide6.QtCore import QByteArray, QObject, QUrl
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest


ApiCallback = Callable[[bool, int, dict | None], None]


class ApiClient(QObject):
    def __init__(self, base_url: str, parent: QObject | None = None):
        super().__init__(parent)
        self.base_url = base_url.rstrip("/")
        self.manager = QNetworkAccessManager(self)
        self.pending: set[QNetworkReply] = set()

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        token: str | None = None,
        headers: dict[str, str] | None = None,
        callback: ApiCallback | None = None,
    ) -> None:
        request = QNetworkRequest(QUrl(self.base_url + path))
        request.setRawHeader(b"Accept", b"application/json")
        if token:
            request.setRawHeader(
                b"Authorization", f"Bearer {token}".encode("utf-8")
            )
        for key, value in (headers or {}).items():
            request.setRawHeader(key.encode("utf-8"), value.encode("utf-8"))

        payload = QByteArray()
        if body is not None:
            payload = QByteArray(
                json.dumps(body, ensure_ascii=False).encode("utf-8")
            )
            request.setHeader(
                QNetworkRequest.KnownHeaders.ContentTypeHeader,
                "application/json; charset=utf-8",
            )

        method_upper = method.upper()
        if method_upper == "GET":
            reply = self.manager.get(request)
        else:
            reply = self.manager.sendCustomRequest(
                request, method_upper.encode("ascii"), payload
            )
        self.pending.add(reply)
        reply.finished.connect(
            lambda: self._finish_reply(reply, callback)
        )

    def _finish_reply(
        self, reply: QNetworkReply, callback: ApiCallback | None
    ) -> None:
        self.pending.discard(reply)
        status = reply.attribute(
            QNetworkRequest.Attribute.HttpStatusCodeAttribute
        )
        status_code = int(status) if status is not None else 0
        raw = bytes(reply.readAll())
        payload = None
        if raw:
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = {"detail": raw.decode("utf-8", errors="replace")}
        is_ok = (
            reply.error() == QNetworkReply.NetworkError.NoError
            and 200 <= status_code < 300
        )
        if not is_ok and payload is None:
            payload = {"detail": reply.errorString()}
        reply.deleteLater()
        if callback:
            callback(is_ok, status_code, payload)

