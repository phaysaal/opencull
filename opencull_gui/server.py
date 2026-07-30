"""Local HTTP server for immutable evidence and separate human review state."""

from __future__ import annotations

import json
import mimetypes
import subprocess
import secrets
import sys
import io
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .actions import ActionController, ActionError, export_bytes
from .photos import PhotoError, PhotoStore, PreviewManager
from .report import ReportIndex
from .reviews import ReviewError, ReviewStore
from .xmp import xmp_zip
from .jobs import JobError, JobManager
from .providers import ProviderError, ProviderStore
from .faces import FaceError, FaceStore
from .shortlist import ShortlistError, ShortlistIndex, load_shortlist
from .shortlist_reviews import (
    ShortlistReviewError,
    ShortlistReviewStore,
    default_shortlist_review_path,
)


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
        jobs: JobManager | None = None,
        providers: ProviderStore | None = None,
        faces: FaceStore | None = None,
        shortlist_path: Path | None = None,
        shutdown_jobs: bool = True,
    ):
        super().__init__(address, ReviewHandler)
        self.report = report
        self.photos = photos
        self.reviews = reviews
        self.csrf_token = secrets.token_urlsafe(32)
        self.previews = PreviewManager(photos, workers=preview_workers)
        self.actions = ActionController(report, reviews, photos)
        self.jobs = jobs
        self.shutdown_jobs = shutdown_jobs
        self.providers = providers
        self.faces = faces
        self.shortlist: ShortlistIndex | None = None
        self.shortlist_reviews: ShortlistReviewStore | None = None
        self.default_shortlist_path = report.path.with_name(
            f"{report.path.stem}.professional-shortlist.json")
        if shortlist_path is not None and shortlist_path.is_file():
            self.load_shortlist(shortlist_path)
        self.payload = {
            **report.public_payload(photos.root),
            "measurements": measurements or {},
            "manifest_path": manifest_path,
        }

    def load_shortlist(self, path: Path) -> dict:
        shortlist = load_shortlist(path, self.report, self.photos.root)
        reviews = ShortlistReviewStore(
            default_shortlist_review_path(shortlist.path), shortlist)
        self.shortlist = shortlist
        self.shortlist_reviews = reviews
        return self.shortlist_payload()

    def shortlist_payload(self) -> dict:
        if self.shortlist is None or self.shortlist_reviews is None:
            return {
                "available": False,
                "default_path": str(self.default_shortlist_path),
            }
        return {
            "available": True,
            "shortlist_path": str(self.shortlist.path),
            "shortlist": self.shortlist.data,
            "review": self.shortlist_reviews.public_state(),
        }

    def server_close(self) -> None:
        previews = getattr(self, "previews", None)
        if previews is not None:
            previews.shutdown()
        jobs = getattr(self, "jobs", None)
        if jobs is not None and self.shutdown_jobs:
            jobs.shutdown()
        faces = getattr(self, "faces", None)
        if faces is not None:
            faces.shutdown()
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
        if parsed.path == "/api/shortlist":
            self._json(self.server.shortlist_payload())
            return
        if parsed.path == "/api/shortlist/export":
            if self.server.shortlist_reviews is None:
                self._json(
                    {"error": "professional shortlist is not loaded"},
                    HTTPStatus.NOT_FOUND)
            else:
                self._json(self.server.shortlist_reviews.export())
            return
        if parsed.path == "/api/action/recovery":
            self._json({
                "journals": self.server.actions.recoverable_journals()})
            return
        if parsed.path == "/api/jobs":
            if self.server.jobs is None:
                self._json({"error": "job manager is disabled"}, HTTPStatus.NOT_FOUND)
            else:
                self._json(self.server.jobs.public())
            return
        if parsed.path == "/api/providers":
            if self.server.providers is None:
                self._json(
                    {"error": "provider profiles are disabled"},
                    HTTPStatus.NOT_FOUND)
            else:
                self._json(self.server.providers.public())
            return
        if parsed.path == "/api/people":
            if self.server.faces is None:
                self._json(
                    {"error": "private face indexing is disabled"},
                    HTTPStatus.NOT_FOUND)
            else:
                self._json(self.server.faces.public())
            return
        if parsed.path == "/api/person-faces":
            if self.server.faces is None:
                self._json(
                    {"error": "private face indexing is disabled"},
                    HTTPStatus.NOT_FOUND)
                return
            query = parse_qs(parsed.query)
            try:
                payload = self.server.faces.person_faces(
                    query.get("id", [""])[0],
                    offset=int(query.get("offset", ["0"])[0]),
                    limit=int(query.get("limit", ["100"])[0]),
                )
            except (FaceError, ValueError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._json(payload)
            return
        if parsed.path == "/api/face":
            if self.server.faces is None:
                self._json(
                    {"error": "private face indexing is disabled"},
                    HTTPStatus.NOT_FOUND)
                return
            query = parse_qs(parsed.query)
            try:
                crop = self.server.faces.face_crop(
                    query.get("id", [""])[0])
            except FaceError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
                return
            output = io.BytesIO()
            crop.save(output, "JPEG", quality=90)
            body = output.getvalue()
            self._headers(HTTPStatus.OK, "image/jpeg", len(body))
            self.wfile.write(body)
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
        if parsed.path == "/api/export/xmp":
            query = parse_qs(parsed.query)
            try:
                body, filename = xmp_zip(
                    self.server.reviews,
                    query.get("policy", ["effective"])[0],
                )
            except ActionError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._download(body, "application/zip", filename)
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
                    body.get("photo_annotations"),
                    str(body.get("action", "review")),
                )
            elif parsed.path == "/api/review/undo":
                state = self.server.reviews.undo(body.get("revision"))
            elif parsed.path == "/api/review/position":
                state = self.server.reviews.update_position(
                    str(body.get("cluster_id", "")),
                    body.get("revision"),
                )
            elif parsed.path == "/api/shortlist/load":
                result = self.server.load_shortlist(
                    Path(str(body.get("path", ""))))
                self._json(result)
                return
            elif parsed.path == "/api/shortlist/review":
                if self.server.shortlist_reviews is None:
                    raise ShortlistReviewError(
                        "professional shortlist is not loaded")
                state = self.server.shortlist_reviews.update(
                    str(body.get("photo", "")),
                    body.get("tier"),
                    body.get("edit_raw"),
                    body.get("note"),
                    body.get("reviewed"),
                    body.get("revision"),
                )
            elif parsed.path == "/api/shortlist/undo":
                if self.server.shortlist_reviews is None:
                    raise ShortlistReviewError(
                        "professional shortlist is not loaded")
                state = self.server.shortlist_reviews.undo(
                    body.get("revision"))
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
            elif parsed.path == "/api/jobs/add":
                if self.server.jobs is None:
                    raise JobError("job manager is disabled")
                result = self.server.jobs.add(
                    str(body.get("photos", "")),
                    str(body.get("output", "")),
                    body.get("keep_per_group", 2),
                    body.get("recursive", False),
                    str(body.get("profile", "family")),
                    str(body.get("provider_profile_id", "")),
                )
                self._json(result)
                return
            elif parsed.path == "/api/jobs/action":
                if self.server.jobs is None:
                    raise JobError("job manager is disabled")
                result = self.server.jobs.action(
                    str(body.get("job_id", "")),
                    str(body.get("action", "")),
                )
                self._json(result)
                return
            elif parsed.path == "/api/jobs/add-professional":
                if self.server.jobs is None:
                    raise JobError("job manager is disabled")
                result = self.server.jobs.add_professional(
                    str(body.get("report", "")),
                    str(body.get("photos", "")),
                    str(body.get("output", "")),
                    str(body.get("review", "")),
                    str(body.get("policy", "effective")),
                    str(body.get("profile", "family")),
                    str(body.get("provider_profile_id", "")),
                )
                self._json(result)
                return
            elif parsed.path == "/api/jobs/open-review":
                if self.server.jobs is None:
                    raise JobError("job manager is disabled")
                result = self.server.jobs.open_review(
                    str(body.get("job_id", "")))
                self._json(result)
                return
            elif parsed.path == "/api/jobs/pick-folder":
                if sys.platform != "darwin":
                    raise JobError("native folder selection is available only on macOS")
                result = subprocess.run(
                    [
                        "osascript", "-e",
                        'POSIX path of (choose folder with prompt '
                        '"Choose a folder of photographs to cull")',
                    ],
                    capture_output=True, text=True, timeout=300, check=False,
                )
                if result.returncode != 0:
                    if "-128" in result.stderr:
                        raise JobError("folder selection was cancelled")
                    raise JobError(
                        f"folder chooser failed: {result.stderr.strip()}")
                self._json({"photos": result.stdout.strip().rstrip("/")})
                return
            elif parsed.path == "/api/action/pick-destination":
                if sys.platform != "darwin":
                    raise ActionError(
                        "native folder selection is available only on macOS")
                result = subprocess.run(
                    [
                        "osascript", "-e",
                        'POSIX path of (choose folder with prompt '
                        '"Choose a destination for selected photographs")',
                    ],
                    capture_output=True, text=True, timeout=300, check=False,
                )
                if result.returncode != 0:
                    if "-128" in result.stderr:
                        raise ActionError("folder selection was cancelled")
                    raise ActionError(
                        f"folder chooser failed: {result.stderr.strip()}")
                self._json({
                    "destination": result.stdout.strip().rstrip("/")})
                return
            elif parsed.path == "/api/providers/save":
                if self.server.providers is None:
                    raise ProviderError("provider profiles are disabled")
                result = self.server.providers.save(
                    body.get("profile", {}),
                    body.get("revision"),
                    str(body.get("secret", "")),
                )
                self._json(result)
                return
            elif parsed.path == "/api/providers/delete":
                if self.server.providers is None:
                    raise ProviderError("provider profiles are disabled")
                result = self.server.providers.delete(
                    str(body.get("profile_id", "")),
                    body.get("revision"),
                    bool(body.get("remove_credential", False)),
                )
                self._json(result)
                return
            elif parsed.path == "/api/providers/test":
                if self.server.providers is None:
                    raise ProviderError("provider profiles are disabled")
                result = self.server.providers.test_connection(
                    str(body.get("profile_id", "")))
                self._json(result)
                return
            elif parsed.path == "/api/people/start":
                if self.server.faces is None:
                    raise FaceError("private face indexing is disabled")
                self._json(self.server.faces.start())
                return
            elif parsed.path == "/api/people/cancel":
                if self.server.faces is None:
                    raise FaceError("private face indexing is disabled")
                self._json(self.server.faces.cancel())
                return
            elif parsed.path == "/api/people/rename":
                if self.server.faces is None:
                    raise FaceError("private face indexing is disabled")
                self._json(self.server.faces.rename(
                    str(body.get("person_id", "")),
                    str(body.get("name", "")),
                    bool(body.get("confirmed", False)),
                ))
                return
            elif parsed.path == "/api/people/merge":
                if self.server.faces is None:
                    raise FaceError("private face indexing is disabled")
                self._json(self.server.faces.merge(body.get("person_ids")))
                return
            elif parsed.path == "/api/people/split":
                if self.server.faces is None:
                    raise FaceError("private face indexing is disabled")
                self._json(self.server.faces.split(
                    str(body.get("person_id", "")),
                    body.get("face_ids"),
                ))
                return
            elif parsed.path == "/api/people/forget":
                if self.server.faces is None:
                    raise FaceError("private face indexing is disabled")
                person_id = str(body.get("person_id", ""))
                if body.get("confirmation") != f"FORGET {person_id}":
                    raise FaceError(
                        f"confirmation must exactly equal: FORGET {person_id}")
                self._json(self.server.faces.forget(person_id))
                return
            elif parsed.path == "/api/people/delete-all":
                if self.server.faces is None:
                    raise FaceError("private face indexing is disabled")
                if body.get("confirmation") != "DELETE ALL PRIVATE FACE DATA":
                    raise FaceError(
                        "confirmation must exactly equal: "
                        "DELETE ALL PRIVATE FACE DATA")
                self._json(self.server.faces.delete_all())
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
        except (
            ReviewError, PhotoError, ActionError, JobError,
            ProviderError,
            FaceError,
            ShortlistError,
            ShortlistReviewError,
            TypeError, ValueError,
        ) as exc:
            status = (
                HTTPStatus.CONFLICT
                if "reload" in str(exc).lower()
                or "different version" in str(exc).lower()
                else HTTPStatus.BAD_REQUEST
            )
            self._json({"error": str(exc)}, status)
