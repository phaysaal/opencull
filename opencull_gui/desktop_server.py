"""Private loopback API used by the native macOS Darkimiya shell."""

from __future__ import annotations

import json
import platform
import secrets
import threading
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import dialogs
from .jobs import JobError, JobManager
from .project_catalog import ProjectCatalog, ProjectCatalogError
from .providers import ProviderError, ProviderStore

LAUNCHER_ROOT = Path(__file__).with_name("launcher")


class DesktopBridgeServer(ThreadingHTTPServer):
    """Own the queue engine while exposing only authenticated local requests."""

    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        address: tuple[str, int],
        jobs: JobManager,
        providers: ProviderStore,
        open_review: Callable[..., dict[str, object]],
        diagnostics: dict[str, object] | None = None,
        projects: ProjectCatalog | None = None,
    ):
        super().__init__(address, DesktopBridgeHandler)
        self.jobs = jobs
        self.providers = providers
        self.open_review = open_review
        self.diagnostics = diagnostics or {}
        self.projects = projects
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

    def _serve_launcher_asset(self, path: str) -> bool:
        """Serve the launcher's own files, which carry no secrets.

        The page is what obtains the session token, so it cannot present one
        yet. Only these three names are reachable, and the session is bound to
        loopback, so this widens nothing an attacker could not already read
        from the installed package.
        """
        names = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/launcher.css": ("launcher.css", "text/css; charset=utf-8"),
            "/launcher.js": ("launcher.js", "text/javascript; charset=utf-8"),
        }
        if path not in names:
            return False
        name, content_type = names[path]
        body = (LAUNCHER_ROOT / name).read_bytes()
        if name == "index.html":
            # The token goes into the document rather than the URL, where it
            # would survive in history and in any logged request line.
            token = json.dumps(self.server.token)
            body = body.replace(
                b"<script src=\"/launcher.js\">",
                f"<script>window.__DARKIMIYA_TOKEN__={token};</script>"
                "<script src=\"/launcher.js\">".encode())
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self'; connect-src 'self'; img-src 'self' data:")
        self.end_headers()
        self.wfile.write(body)
        return True

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if self._serve_launcher_asset(path):
            return
        if not self._authorized():
            return
        if path == "/health":
            self._json({"ok": True, "service": "opencull-native-bridge-v1"})
        elif path == "/state":
            self._json({
                # Lets the launcher show ~/Pictures/Shoot rather than the
                # absolute path, which is orientation rather than evidence.
                "home": str(Path.home()),
                "queue": self.server.jobs.public(),
                "providers": self.server.providers.public(),
                "projects": (
                    self.server.projects.public(self.server.jobs.public())
                    if self.server.projects else {
                        "format": "darkimiya-project-catalog-v1",
                        "revision": 0, "projects": [],
                    }),
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
                    body.get("judge_panel"),
                    body.get("judge_votes", 5),
                    body.get("judge_required", 4),
                )
            elif path == "/projects":
                if self.server.projects is None:
                    raise ProjectCatalogError("project library is unavailable")
                result = self.server.projects.add(str(body.get("photos", "")))
            elif path == "/projects/import-report":
                if self.server.projects is None:
                    raise ProjectCatalogError("project library is unavailable")
                result = self.server.projects.add_existing_report(
                    str(body.get("photos", "")), str(body.get("report", "")))
            elif path == "/projects/cull":
                if self.server.projects is None:
                    raise ProjectCatalogError("project library is unavailable")
                record = self.server.projects.record_for(
                    str(body.get("project_id", "")))
                result = self.server.jobs.add(
                    str(record["photos"]), "",
                    body.get("keep_per_group", 2),
                    body.get("recursive", True),
                    str(body.get("profile", "family")),
                    str(body.get("provider_profile_id", "")),
                    body.get("judge_panel"),
                    body.get("judge_votes", 5),
                    body.get("judge_required", 4),
                )
            elif path == "/projects/manual":
                if self.server.projects is None:
                    raise ProjectCatalogError("project library is unavailable")
                project_id = str(body.get("project_id", ""))
                record = self.server.projects.record_for(project_id)
                report = self.server.projects.manual_selection_report(project_id)
                result = self.server.open_review(
                    report, Path(str(record["photos"])))
            elif path == "/projects/open":
                if self.server.projects is None:
                    raise ProjectCatalogError("project library is unavailable")
                record = self.server.projects.record_for(
                    str(body.get("project_id", "")))
                public = self.server.projects.public(self.server.jobs.public())
                project = next(
                    item for item in public["projects"]
                    if item["id"] == record["id"])
                if not project.get("report_available"):
                    raise ProjectCatalogError(
                        "project has no selection report; start culling or continue without culling")
                result = self.server.open_review(
                    Path(str(project["report"])), Path(str(record["photos"])))
            elif path == "/jobs/professional":
                result = self.server.jobs.add_professional(
                    str(body.get("report", "")),
                    str(body.get("photos", "")),
                    str(body.get("output", "")),
                    str(body.get("review", "")),
                    str(body.get("policy", "effective")),
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
                if job.get("kind") == "professional_shortlist":
                    result = self.server.open_review(
                        Path(job["report"]), Path(job["photos"]),
                        Path(job["output"]))
                else:
                    result = self.server.open_review(
                        Path(job["output"]), Path(job["photos"]))
            elif path == "/reviews/open":
                report = Path(str(body.get("report", ""))).expanduser().resolve()
                resolver = getattr(
                    self.server.jobs, "resolve_report_path", None)
                if resolver is not None:
                    report = resolver(report)
                result = self.server.open_review(
                    report,
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
            elif path == "/choose-folder":
                try:
                    result = {"path": dialogs.choose_folder(
                        str(body.get("prompt")
                            or "Choose a folder of photographs to cull"))}
                except dialogs.DialogCancelled:
                    # Dismissing a chooser is a decision, not a failure.
                    result = {"path": ""}
                except dialogs.DialogError as exc:
                    raise JobError(str(exc)) from exc
            elif path == "/choose-report":
                try:
                    result = {"path": dialogs.choose_files(
                        "Choose a finished culling report")[0]}
                except dialogs.DialogCancelled:
                    result = {"path": ""}
                except dialogs.DialogError as exc:
                    raise JobError(str(exc)) from exc
            elif path == "/reveal":
                target = Path(str(body.get("path", ""))).expanduser().resolve()
                if not target.exists():
                    raise JobError("the item to reveal no longer exists")
                try:
                    dialogs.reveal(target)
                except dialogs.DialogError as exc:
                    raise JobError(str(exc)) from exc
                result = {"ok": True}
            else:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            self._json(result)
        except (
            JobError, ProviderError, ProjectCatalogError, TypeError, ValueError,
        ) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)


def serve_desktop_bridge(
    jobs: JobManager,
    providers: ProviderStore,
    open_review: Callable[..., dict[str, object]],
    diagnostics: dict[str, object] | None = None,
    projects: ProjectCatalog | None = None,
    present: Callable[[str], None] | None = None,
) -> int:
    server = DesktopBridgeServer(
        ("127.0.0.1", 0), jobs, providers, open_review, diagnostics, projects)
    url = f"http://127.0.0.1:{server.server_port}"
    if present is None:
        # Bootstrap mode: an external shell owns the window and reads this
        # line to find the service.
        print(json.dumps({
            "format": "opencull-native-bootstrap-v1",
            "url": url,
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

    # Presented mode: this process owns the window, so the service runs
    # behind it and stops when it closes.
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.25},
        name="darkimiya-bridge", daemon=True)
    thread.start()
    try:
        present(f"{url}/")
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        jobs.shutdown()
    return 0
