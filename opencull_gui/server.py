"""Local HTTP server for immutable evidence and separate human review state."""

from __future__ import annotations

import json
import mimetypes
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .actions import ActionController, ActionError, export_bytes
from .photos import PhotoError, PhotoStore, PreviewManager
from .report import ReportIndex
from .reviews import ReviewError, ReviewStore


STATIC_ROOT = Path(__file__).with_name("static")


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        report: ReportIndex,
        photos: PhotoStore,
        reviews: ReviewStore,
        measurements: dict | None = None,
        manifest_path: str | None = None,
        preview_workers: int = 2,
    ):
        super().__init__(address, ReviewHandler)
        self.report = report
        self.photos = photos
        self.reviews = reviews
        self.csrf_token = secrets.token_urlsafe(32)
        self.previews = PreviewManager(photos, workers=preview_workers)
        self.actions = ActionController(report, reviews, photos)
        self.payload = {
            **report.public_payload(photos.root),
            "measurements": measurements or {},
            "manifest_path": manifest_path,
        }

    def server_close(self) -> None:
        previews = getattr(self, "previews", None)
        if previews is not None:
            previews.shutdown()
        super().server_close()


class ReviewHandler(BaseHTTPRequestHandler):
    server: ReviewServer

    def log_message(self, format: str, *args: object) -> None:
        print(f"[OpenCull GUI] {self.address_string()} - {format % args}")

    def _headers(self, status: int, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self'; script-src 'self'; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()

    def _json(self, value: object, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(value, ensure_ascii=False).encode()
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _file(self, path: Path, content_type: str | None = None) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._json({"error": "file not found"}, HTTPStatus.NOT_FOUND)
            return
        mime = content_type or mimetypes.guess_type(path.name)[0]
        self._headers(HTTPStatus.OK, mime or "application/octet-stream", len(body))
        self.wfile.write(body)

    def _download(
        self, body: bytes, content_type: str, filename: str
    ) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _valid_host(self) -> bool:
        host = self.headers.get("Host", "").rsplit(":", 1)[0].lower()
        return host in {"127.0.0.1", "localhost"}

    def _require_host(self) -> bool:
        if self._valid_host():
            return True
        self._json({"error": "invalid Host header"}, HTTPStatus.FORBIDDEN)
        return False

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ReviewError("Invalid Content-Length.") from exc
        if length <= 0 or length > 65536:
            raise ReviewError("Request body must be between 1 byte and 64 KiB.")
        try:
            value = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ReviewError("Request body is not valid JSON.") from exc
        if not isinstance(value, dict):
            raise ReviewError("Request body must be a JSON object.")
        return value

    def _require_csrf(self) -> bool:
        supplied = self.headers.get("X-OpenCull-CSRF", "")
        if secrets.compare_digest(supplied, self.server.csrf_token):
            return True
        self._json({"error": "invalid review token"}, HTTPStatus.FORBIDDEN)
        return False

    def do_GET(self) -> None:
        if not self._require_host():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self._json({
                "ok": True,
                "mode": "immutable evidence with separate human review",
            })
            return
        if parsed.path == "/api/report":
            self._json({
                **self.server.payload,
                "review": self.server.reviews.public_state(),
                "csrf_token": self.server.csrf_token,
            })
            return
        if parsed.path == "/api/review":
            self._json(self.server.reviews.public_state())
            return
        if parsed.path == "/api/export":
            self._json(self.server.reviews.export())
            return
        if parsed.path == "/api/export/file":
            query = parse_qs(parsed.query)
            try:
                body, content_type, filename = export_bytes(
                    self.server.reviews,
                    query.get("policy", ["human_only"])[0],
                    query.get("format", ["json"])[0],
                )
            except ActionError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._download(body, content_type, filename)
            return
        if parsed.path == "/api/action/status":
            query = parse_qs(parsed.query)
            try:
                self._json(self.server.actions.status(
                    query.get("id", [""])[0]))
            except ActionError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        if parsed.path == "/api/previews/status":
            query = parse_qs(parsed.query)
            names = query.get("name", [])
            size = query.get("size", ["thumb"])[0]
            try:
                self._json(self.server.previews.status(names, size))
            except PhotoError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/cache":
            self._json(self.server.previews.progress())
            return
        if parsed.path == "/api/image":
            query = parse_qs(parsed.query)
            name = query.get("name", [""])[0]
            size = query.get("size", ["thumb"])[0]
            try:
                preview = self.server.photos.cached_preview(name, size)
            except PhotoError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
                return
            if preview is None:
                state = self.server.previews.request(name, size)
                self._json(state, HTTPStatus.ACCEPTED)
                return
            self._file(preview, "image/jpeg")
            return
        static_name = "index.html" if parsed.path == "/" else parsed.path.lstrip("/")
        if static_name not in {"index.html", "app.js", "styles.css"}:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        self._file(STATIC_ROOT / static_name)

    def do_POST(self) -> None:
        if not self._require_host() or not self._require_csrf():
            return
        parsed = urlparse(self.path)
        try:
            body = self._read_json()
            if parsed.path == "/api/review/cluster":
                state = self.server.reviews.update_cluster(
                    str(body.get("cluster_id", "")),
                    body.get("keepers"),
                    body.get("note"),
                    body.get("reviewed"),
                    body.get("revision"),
                )
            elif parsed.path == "/api/review/position":
                state = self.server.reviews.update_position(
                    str(body.get("cluster_id", "")),
                    body.get("revision"),
                )
            elif parsed.path == "/api/previews/focus":
                visible = body.get("visible", [])
                prefetch = body.get("prefetch", [])
                if (
                    not isinstance(visible, list)
                    or not isinstance(prefetch, list)
                    or len(visible) > 128
                    or len(prefetch) > 256
                    or any(not isinstance(name, str)
                           for name in visible + prefetch)
                ):
                    raise ReviewError("Invalid preview focus request.")
                result = self.server.previews.focus(
                    visible, prefetch, str(body.get("size", "thumb")))
                self._json(result)
                return
            elif parsed.path == "/api/previews/retry":
                result = self.server.previews.retry(
                    str(body.get("name", "")),
                    str(body.get("size", "thumb")),
                )
                self._json(result)
                return
            elif parsed.path == "/api/cache/clear":
                result = self.server.previews.clear_cache()
                self._json(result)
                return
            elif parsed.path == "/api/action/preflight":
                result = self.server.actions.preflight(**body)
                self._json(result)
                return
            elif parsed.path == "/api/action/execute":
                result = self.server.actions.execute(
                    str(body.get("plan_id", "")),
                    str(body.get("confirmation", "")),
                )
                self._json(result)
                return
            elif parsed.path == "/api/action/cancel":
                result = self.server.actions.cancel(
                    str(body.get("operation_id", "")))
                self._json(result)
                return
            elif parsed.path == "/api/action/rollback":
                result = self.server.actions.rollback(
                    str(body.get("operation_id", "")),
                    str(body.get("confirmation", "")),
                )
                self._json(result)
                return
            elif parsed.path == "/api/action/resume":
                result = self.server.actions.resume_journal(
                    Path(str(body.get("journal_path", ""))),
                    str(body.get("confirmation", "")),
                )
                self._json(result)
                return
            elif parsed.path == "/api/action/contact-sheet":
                result = self.server.actions.contact_sheet(
                    Path(str(body.get("destination", ""))),
                    str(body.get("policy", "human_only")),
                    str(body.get("confirmation", "")),
                    int(body.get("columns", 4)),
                    int(body.get("rows", 5)),
                )
                self._json(result)
                return
            else:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            self._json(state)
        except (ReviewError, PhotoError, ActionError, TypeError, ValueError) as exc:
            status = (
                HTTPStatus.CONFLICT
                if "reload" in str(exc).lower()
                or "different version" in str(exc).lower()
                else HTTPStatus.BAD_REQUEST
            )
            self._json({"error": str(exc)}, status)
