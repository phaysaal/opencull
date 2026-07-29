"""Private loopback API used by the native macOS OpenCull shell."""

from __future__ import annotations

import json
import platform
import secrets
import subprocess
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .jobs import JobError, JobManager
from .providers import ProviderError, ProviderStore


class DesktopBridgeServer(ThreadingHTTPServer):
    """Own the queue engine while exposing only authenticated local requests."""

    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        address: tuple[str, int],
        jobs: JobManager,
        providers: ProviderStore,
        open_review: Callable[[Path, Path], dict[str, object]],
        diagnostics: dict[str, object] | None = None,
    ):
        super().__init__(address, DesktopBridgeHandler)
        self.jobs = jobs
        self.providers = providers
        self.open_review = open_review
        self.diagnostics = diagnostics or {}
        self.token = secrets.token_urlsafe(32)


class DesktopBridgeHandler(BaseHTTPRequestHandler):
    server: DesktopBridgeServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def _json(
        self, value: dict[str, Any], status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        expected = f"Bearer {self.server.token}"
        if secrets.compare_digest(self.headers.get("Authorization", ""), expected):
            return True
        self._json({"error": "invalid desktop session"}, HTTPStatus.FORBIDDEN)
        return False

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid content length") from exc
        if length < 0 or length > 128 * 1024:
            raise ValueError("request body is too large")
        try:
            value = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise ValueError("request body must be JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("request body must be an object")
        return value

    def do_GET(self) -> None:
        if not self._authorized():
            return
        path = urlparse(self.path).path
        if path == "/health":
            self._json({"ok": True, "service": "opencull-native-bridge-v1"})
        elif path == "/state":
            self._json({
                "queue": self.server.jobs.public(),
                "providers": self.server.providers.public(),
            })
        elif path == "/diagnostics":
            self._json({
                "format": "opencull-native-diagnostics-v1",
                "python": platform.python_version(),
                "architecture": platform.machine(),
                "queue_revision": self.server.jobs.public()["revision"],
                "provider_revision": self.server.providers.public()["revision"],
                **self.server.diagnostics,
            })
        else:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if not self._authorized():
            return
        try:
            body = self._body()
            path = urlparse(self.path).path
            if path == "/jobs":
                result = self.server.jobs.add(
                    str(body.get("photos", "")),
                    str(body.get("output", "")),
                    body.get("keep_per_group", 2),
                    body.get("recursive", False),
                    str(body.get("profile", "family")),
                    str(body.get("provider_profile_id", "")),
                )
            elif path == "/jobs/action":
                result = self.server.jobs.action(
                    str(body.get("job_id", "")),
                    str(body.get("action", "")),
                )
            elif path == "/jobs/relink":
                result = self.server.jobs.relink_source(
                    str(body.get("job_id", "")),
                    str(body.get("photos", "")),
                )
            elif path == "/jobs/remove":
                result = self.server.jobs.remove(
                    str(body.get("job_id", "")),
                    bool(body.get("remove_artifacts", False)),
                )
            elif path == "/jobs/review":
                job_id = str(body.get("job_id", ""))
                job = next(
                    (item for item in self.server.jobs.public()["jobs"]
                     if item["id"] == job_id),
                    None,
                )
                if job is None:
                    raise JobError("unknown culling job")
                if job["status"] != "completed" or not Path(job["output"]).is_file():
                    raise JobError("only a completed job can be opened for review")
                result = self.server.open_review(
                    Path(job["output"]), Path(job["photos"]))
            elif path == "/reviews/open":
                result = self.server.open_review(
                    Path(str(body.get("report", ""))).expanduser().resolve(),
                    Path(str(body.get("photos", ""))).expanduser().resolve(),
                )
            elif path == "/providers/save":
                result = self.server.providers.save(
                    body.get("profile", {}),
                    body.get("revision"),
                    str(body.get("secret", "")),
                )
            elif path == "/providers/delete":
                result = self.server.providers.delete(
                    str(body.get("profile_id", "")),
                    body.get("revision"),
                    bool(body.get("remove_credential", False)),
                )
            elif path == "/providers/test":
                result = self.server.providers.test_connection(
                    str(body.get("profile_id", "")))
            elif path == "/reveal":
                target = Path(str(body.get("path", ""))).expanduser().resolve()
                if not target.exists():
                    raise JobError("the item to reveal no longer exists")
                subprocess.Popen(["open", "-R", str(target)])
                result = {"ok": True}
            else:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            self._json(result)
        except (JobError, ProviderError, TypeError, ValueError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)


def serve_desktop_bridge(
    jobs: JobManager,
    providers: ProviderStore,
    open_review: Callable[[Path, Path], dict[str, object]],
    diagnostics: dict[str, object] | None = None,
) -> int:
    server = DesktopBridgeServer(
        ("127.0.0.1", 0), jobs, providers, open_review, diagnostics)
    print(json.dumps({
        "format": "opencull-native-bootstrap-v1",
        "url": f"http://127.0.0.1:{server.server_port}",
        "token": server.token,
    }), flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        jobs.shutdown()
    return 0
