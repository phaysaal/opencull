"""Local HTTP server for immutable evidence and separate human review state."""

from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import os
import secrets
import tempfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageOps

from colour_profile import srgb_profile

from . import dialogs
from .actions import ActionController, ActionError, export_bytes
from .development import DevelopmentWorkspace
from .directions import DirectionsIndex
from .faces import FaceError, FaceStore
from .jobs import JobError, JobManager
from .photos import PhotoError, PhotoStore, PreviewManager
from .project import (
    ensure_project_layout,
    import_legacy_development_artifacts,
    legacy_migration_preview,
    load_or_create_folder_project,
    migrate_legacy_project,
    project_sha256,
    register_file_artifact,
    update_project,
)
from .providers import ProviderError, ProviderStore
from .raw_sources import RawSourceError, RawSourceStore
from .report import ReportIndex
from .reviews import ReviewError, ReviewStore
from .shortlist import ShortlistError, ShortlistIndex, load_shortlist
from .shortlist_reviews import (
    ShortlistReviewError,
    ShortlistReviewStore,
    default_shortlist_review_path,
)
from .xmp import xmp_zip

STATIC_ROOT = Path(__file__).with_name("static")


def _atomic_json_file(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=f".{path.name}.", suffix=".tmp", delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


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
        preview_workers: int | None = None,
        jobs: JobManager | None = None,
        providers: ProviderStore | None = None,
        faces: FaceStore | None = None,
        shortlist_path: Path | None = None,
        shortlist_default_path: Path | None = None,
        shutdown_jobs: bool = True,
    ):
        super().__init__(address, ReviewHandler)
        self.report = report
        self.photos = photos
        self.reviews = reviews
        self.csrf_token = secrets.token_urlsafe(32)
        self.previews = PreviewManager(photos, workers=preview_workers)
        self.jobs = jobs
        self.shutdown_jobs = shutdown_jobs
        self.providers = providers
        self.faces = faces
        legacy_project_path = report.path.with_suffix(".opencull-project.json")
        self.project_path, self.project = load_or_create_folder_project(
            photos.root, report.path.stem, legacy_project_path)
        self.project = import_legacy_development_artifacts(
            self.project_path, legacy_project_path)
        self.project_layout = ensure_project_layout(photos.root)
        self.raw_sources = RawSourceStore(
            self.project_layout["Reports"] /
            f"{report.path.stem}.raw-source.json", report)
        self.actions = ActionController(
            report, reviews, photos, self.project_path, self.raw_sources)
        # The develop stage is not about HTTP, so it does not live in here.
        # The native window uses the same object directly.
        self.development = DevelopmentWorkspace(
            self.project_path, self.project_layout, self.raw_sources,
            directions=self.edit_directions_payload)
        self.project = register_file_artifact(
            self.project_path, "culling_report", report.path,
            stage="cull", singleton=True)
        self.shortlist: ShortlistIndex | None = None
        self.shortlist_reviews: ShortlistReviewStore | None = None
        self.default_shortlist_path = (
            shortlist_default_path.expanduser().resolve()
            if shortlist_default_path is not None
            else self.project_layout["Reports"] /
                 f"{report.path.stem}.professional-shortlist.json")
        if shortlist_path is not None and shortlist_path.is_file():
            self.load_shortlist(shortlist_path)
        self.payload = {
            **report.public_payload(photos.root),
            "measurements": measurements or {},
            "manifest_path": manifest_path,
            # Folder and file choosers depend on what the desktop provides.
            # Reporting this lets the interface disable a button with a reason
            # instead of offering one that fails when pressed.
            "chooser": dialogs.status(),
            "raw_source": self.raw_sources.public(),
            "project": {**self.project, "path": str(self.project_path),
                         "sha256": project_sha256(self.project_path)},
        }

    def project_payload(self) -> dict:
        self.project_path, self.project = load_or_create_folder_project(
            self.photos.root, self.report.path.stem,
            self.report.path.with_suffix(".opencull-project.json"))
        if self.reviews.path.is_file():
            self.project = register_file_artifact(
                self.project_path, "culling_review", self.reviews.path,
                stage="cull")
        return {**self.project, "path": str(self.project_path),
                "sha256": project_sha256(self.project_path)}

    def migration_payload(self) -> dict:
        return legacy_migration_preview(self.project_path, self.photos.root)

    def migrate_project(self, confirmation: str) -> dict:
        result = migrate_legacy_project(
            self.project_path, self.photos.root, confirmation)
        self.project_path = Path(result["path"])
        self.project_layout = ensure_project_layout(self.photos.root)
        self.project = result["project"]
        self.actions.bind_project(self.project_path)
        self.development.bind(self.project_path, self.project_layout)
        self.project = register_file_artifact(
            self.project_path, "culling_report", self.report.path,
            stage="cull", singleton=True)
        self.payload["project"] = {
            **self.project, "path": str(self.project_path),
            "sha256": project_sha256(self.project_path),
        }
        result["project"] = self.payload["project"]
        return result

    def style_profile_payload(self) -> dict:
        requested = self.project.get("active_style_profile")
        if not requested:
            return {"available": False, "reason": "no personal style profile is selected"}
        path = Path(str(requested)).expanduser().resolve()
        if not path.is_file():
            return {"available": False, "reason": "selected style profile is unavailable",
                    "path": str(path)}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ShortlistError(f"cannot read style profile: {exc}") from exc
        if value.get("format") != "opencull-personal-style-profile-v1":
            raise ShortlistError("unsupported personal style profile")
        return {"available": True, "path": str(path), "profile": value}

    def development_payload(self) -> dict:
        payload = self.development.payload()
        self.project = self.development.project
        return payload

    def development_recipe_preview(
        self, photo: str, style: str, engine: str, demosaic: str,
        maximum: int,
    ) -> Path:
        return self.development.recipe_preview(
            photo, style, engine, demosaic, maximum)

    def import_development_recipe(self) -> dict:
        result = self.development.import_recipe()
        self.project = self.development.project
        return result

    def render_portable_development_recipe(
        self, photo: str, style: str, engine: str, demosaic: str,
    ) -> dict:
        result = self.development.render_portable(photo, style, engine, demosaic)
        self.project = self.development.project
        return result

    def verification_payload(self) -> dict:
        return {"format": "opencull-verification-workspace-v1",
                "certificates": self.project.get("artifacts", {}).get("verifications", []) or [],
                "project_sha256": project_sha256(self.project_path)}

    def export_payload(self) -> dict:
        payload = self.development.export_payload()
        self.project = self.development.project
        return payload

    def export_render(self, source: str, destination: str) -> dict:
        record = self.development.export_render(source, destination)
        self.project = self.development.project
        return record

    def load_shortlist(self, path: Path) -> dict:
        shortlist = load_shortlist(path, self.report, self.photos.root)
        reviews = ShortlistReviewStore(
            self.project_layout["Reviews"] /
            f"{shortlist.path.stem}.review.json", shortlist)
        migration = reviews.merge_legacy(
            default_shortlist_review_path(shortlist.path))
        self.shortlist = shortlist
        self.shortlist_reviews = reviews
        self.project = register_file_artifact(
            self.project_path, "shortlist", shortlist.path,
            stage="shortlist")
        if migration["merged"]:
            self.project = register_file_artifact(
                self.project_path, "shortlist_review", reviews.path,
                stage="shortlist")
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

    def edit_directions_payload(self) -> dict:
        if self.shortlist is None or self.shortlist_reviews is None:
            return {"available": False, "reason": "shortlist is not loaded"}
        return DirectionsIndex(
            self.shortlist, self.shortlist_reviews,
            self.project_layout["Recipes"],
            style_profile=lambda: str(
                self.project.get("active_style_profile") or ""),
        ).payload()

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

    def _immutable_preview_file(self, path: Path, revision: str) -> None:
        """Serve only a completed JPEG artifact through the preview file route."""
        try:
            body = path.read_bytes()
        except OSError:
            self._json({"error": "preview file not found"}, HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=31536000, immutable")
        self.send_header("ETag", f'"{revision}"')
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _image_preview(self, path: Path, maximum: int) -> None:
        """Serve a screen-bounded local preview without altering its source."""
        try:
            stat = path.stat()
            identity = hashlib.sha256(
                f"{path.resolve()}|{stat.st_mtime_ns}|{stat.st_size}|{maximum}".encode()
            ).hexdigest()[:20]
            cache_dir = self.server.project_layout["Previews"] / "Develop"
            cache_dir.mkdir(parents=True, exist_ok=True)
            cached = cache_dir / f"{path.stem}-{maximum}-{identity}.jpg"
            if cached.is_file():
                self._file(cached, "image/jpeg")
                return
        except (OSError, KeyError):
            cached = None
        try:
            with Image.open(path) as opened:
                image = ImageOps.exif_transpose(opened).convert("RGB")
                image.thumbnail((maximum, maximum), Image.Resampling.LANCZOS)
                output = io.BytesIO()
                image.save(output, "JPEG", quality=91, optimize=True,
                           icc_profile=srgb_profile())
                body = output.getvalue()
        except (OSError, ValueError):
            self._json({"error": "development preview could not be decoded"},
                       HTTPStatus.NOT_FOUND)
            return
        if cached is not None:
            try:
                temporary = cached.with_name(f".{cached.name}.{secrets.token_hex(4)}.tmp")
                temporary.write_bytes(body)
                os.replace(temporary, cached)
            except OSError:
                temporary.unlink(missing_ok=True)
        self._headers(HTTPStatus.OK, "image/jpeg", len(body))
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
        if parsed.path == "/api/project":
            self._json(self.server.project_payload())
            return
        if parsed.path == "/api/project/migration":
            self._json(self.server.migration_payload())
            return
        if parsed.path == "/api/style-profile":
            self._json(self.server.style_profile_payload())
            return
        if parsed.path == "/api/style-photo":
            requested = parse_qs(parsed.query).get("path", [""])[0]
            path = Path(requested).expanduser().resolve()
            image_types = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
            if path.suffix.casefold() not in image_types or not path.is_file():
                self._json({"error": "style photograph is unavailable"}, HTTPStatus.NOT_FOUND)
            else:
                self._file(path)
            return
        if parsed.path == "/api/development":
            self._json(self.server.development_payload())
            return
        if parsed.path == "/api/development/preview":
            query = parse_qs(parsed.query)
            try:
                maximum = max(240, min(
                    int(query.get("max", ["960"])[0]), 1440))
                preview = self.server.development_recipe_preview(
                    query.get("photo", [""])[0],
                    query.get("style", [""])[0],
                    query.get("engine", ["darktable"])[0],
                    query.get("demosaic", ["markesteijn-3-pass"])[0],
                    maximum,
                )
            except (ValueError, OSError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._file(preview, "image/jpeg")
            return
        if parsed.path == "/api/development/image":
            query = parse_qs(parsed.query)
            requested = query.get("path", [""])[0]
            allowed = {str(Path(str(item.get("path", ""))).expanduser().resolve())
                       for item in self.server.project.get("artifacts", {}).get("renders", [])
                       if isinstance(item, dict)}
            allowed.update(
                str(Path(str(item.get("contact_sheet", ""))).expanduser().resolve())
                for item in self.server.project.get("artifacts", {}).get(
                    "renderer_comparisons", [])
                if isinstance(item, dict) and item.get("contact_sheet"))
            path = str(Path(requested).expanduser().resolve())
            if path not in allowed or not Path(path).is_file():
                self._json({"error": "development image is not linked to this project"}, HTTPStatus.NOT_FOUND)
            else:
                try:
                    maximum = int(query.get("max", ["0"])[0])
                except ValueError:
                    maximum = 0
                if maximum:
                    self._image_preview(Path(path), max(160, min(maximum, 2560)))
                else:
                    self._file(Path(path), "image/jpeg")
            return
        if parsed.path == "/api/verification":
            self._json(self.server.verification_payload())
            return
        if parsed.path == "/api/export-project":
            self._json(self.server.export_payload())
            return
        if parsed.path == "/api/review":
            self._json(self.server.reviews.public_state())
            return
        if parsed.path == "/api/shortlist":
            self._json(self.server.shortlist_payload())
            return
        if parsed.path == "/api/raw-source":
            self._json(self.server.raw_sources.public())
            return
        if parsed.path == "/api/edit-directions":
            self._json(self.server.edit_directions_payload())
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
        if parsed.path == "/api/action/cleanup-candidates":
            try:
                self._json(self.server.actions.cleanup_candidates())
            except ActionError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/jobs":
            if self.server.jobs is None:
                self._json({"error": "job manager is disabled"}, HTTPStatus.NOT_FOUND)
            else:
                self._json(self.server.jobs.public())
            return
        if parsed.path == "/api/jobs/style-profile-result":
            if self.server.jobs is None:
                self._json({"error": "job manager is disabled"}, HTTPStatus.NOT_FOUND)
            else:
                try:
                    self._json(self.server.jobs.style_profile_result(
                        parse_qs(parsed.query).get("job_id", [""])[0]))
                except JobError as exc:
                    self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
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
            crop.save(output, "JPEG", quality=90, icc_profile=srgb_profile())
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
        if parsed.path == "/api/previews/file":
            query = parse_qs(parsed.query)
            name = query.get("name", [""])[0]
            size = query.get("size", ["thumb"])[0]
            revision = query.get("revision", [""])[0]
            try:
                expected = self.server.photos.preview_revision(name, size)
                preview = self.server.photos.cached_preview(name, size)
            except PhotoError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
                return
            if not revision or revision != expected or preview is None:
                self._json(
                    {"error": "preview artifact is not ready"},
                    HTTPStatus.NOT_FOUND,
                )
                return
            self._immutable_preview_file(preview, expected)
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
            if parsed.path == "/api/project":
                changes = {key: body[key] for key in
                           ("name", "source_folder", "active_style_profile", "stage", "artifacts", "rendering")
                           if key in body}
                self._json(update_project(self.server.project_path, **changes))
                self.server.project = self.server.project_payload()
                return
            if parsed.path == "/api/project/migrate":
                self._json(self.server.migrate_project(
                    str(body.get("confirmation", ""))))
                return
            if parsed.path == "/api/export-project":
                self._json(self.server.export_render(str(body.get("source", "")),
                                                      str(body.get("destination", ""))))
                return
            if parsed.path == "/api/export/reveal":
                requested = Path(str(body.get("path", ""))).expanduser().resolve()
                exports = {
                    str(Path(str(item.get("destination", ""))).expanduser().resolve())
                    for item in self.server.project.get("artifacts", {}).get("exports", [])
                    if isinstance(item, dict)
                }
                if str(requested) not in exports or not requested.is_file():
                    raise ValueError("exported file is not linked to this project")
                dialogs.reveal(requested)
                self._json({"path": str(requested)})
                return
            if parsed.path == "/api/export/pick-folder":
                try:
                    chosen = dialogs.choose_folder("Choose export folder")
                except dialogs.DialogError as exc:
                    raise JobError(str(exc)) from exc
                self._json({"path": chosen})
                return
            if parsed.path == "/api/development/import-recipe":
                self._json(self.server.import_development_recipe())
                return
            if parsed.path == "/api/development/render-portable":
                self._json(self.server.render_portable_development_recipe(
                    str(body.get("photo", "")), str(body.get("style", "")),
                    str(body.get("engine", "darktable")),
                    str(body.get("demosaic", "markesteijn-3-pass")),
                ))
                return
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
                    interesting=body.get("interesting", False),
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
                    body.get("judge_panel"),
                    body.get("judge_votes", 5),
                    body.get("judge_required", 4),
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
            elif parsed.path == "/api/jobs/add-edit-suggestions":
                if self.server.jobs is None:
                    raise JobError("job manager is disabled")
                result = self.server.jobs.add_edit_suggestions(
                    shortlist=str(body.get("shortlist", "")),
                    review=str(body.get("review", "")),
                    photos=str(body.get("photos", "")),
                    output=str(body.get("output", "")),
                    profile=str(body.get("profile", "family")),
                    provider_profile_id=str(
                        body.get("provider_profile_id", "")),
                    model=str(body.get("model", "")),
                    consensus=bool(body.get("consensus", False)),
                    style_profile=str(body.get("style_profile", "")),
                    only_photo=str(body.get("only_photo", "")),
                    only_photos=(
                        body.get("only_photos")
                        if isinstance(body.get("only_photos"), list)
                        else []
                    ),
                )
                self._json(result)
                return
            elif parsed.path == "/api/jobs/add-treatment":
                if self.server.jobs is None:
                    raise JobError("job manager is disabled")
                result = self.server.jobs.add_treatment(
                    photos=str(body.get("photos", "")),
                    photo=str(body.get("photo", "")),
                    rounds=body.get("rounds", 3),
                    output=str(body.get("output", "")),
                    provider_profile_id=str(
                        body.get("provider_profile_id", "")),
                )
                self._json(result)
                return
            elif parsed.path == "/api/jobs/add-semantic-verification":
                if self.server.jobs is None:
                    raise JobError("job manager is disabled")
                result = self.server.jobs.add_semantic_verification(
                    str(body.get("original", "")),
                    str(body.get("developed", "")),
                    str(body.get("suggestion", "")),
                    str(body.get("thumbnail", "")),
                    str(body.get("output", "")),
                    str(body.get("provider_profile_id", "")),
                    str(body.get("model", "")),
                    bool(body.get("consensus", False)),
                    str(self.server.project_path),
                )
                self._json(result)
                return
            elif parsed.path == "/api/jobs/add-style-profile":
                if self.server.jobs is None:
                    raise JobError("job manager is disabled")
                result = self.server.jobs.add_style_profile(
                    body.get("photos", ""), str(body.get("output", "")),
                    str(body.get("existing", "")), str(body.get("mode", "update")),
                    str(body.get("provider_profile_id", "")),
                    str(body.get("model", "")), int(body.get("limit", 64)),
                )
                self._json(result)
                return
            elif parsed.path == "/api/jobs/add-development-render":
                if self.server.jobs is None:
                    raise JobError("job manager is disabled")
                result = self.server.jobs.add_development_render(
                    str(body.get("baseline", "")), str(body.get("recipe", "")),
                    str(body.get("output_dir", "")), str(body.get("project", "")),
                    str(body.get("reference_jpeg", "")), str(body.get("adjustments", "")),
                    bool(body.get("allow_incomplete", False)))
                self._json(result)
                return
            elif parsed.path == "/api/jobs/add-development-pipeline":
                if self.server.jobs is None:
                    raise JobError("job manager is disabled")
                result = self.server.jobs.add_development_pipeline(
                    str(body.get("source", "")),
                    str(body.get("reference", "")),
                    str(body.get("directions", "")),
                    str(body.get("photo", "")),
                    str(body.get("style", "")),
                    str(body.get("project", "")),
                )
                self._json(result)
                return
            elif parsed.path == "/api/jobs/add-renderer-comparison":
                if self.server.jobs is None:
                    raise JobError("job manager is disabled")
                result = self.server.jobs.add_renderer_comparison(
                    str(body.get("source", "")), str(body.get("reference", "")),
                    str(body.get("directions", "")), str(body.get("photo", "")),
                    str(body.get("style", "")), str(body.get("opencull_render", "")),
                    str(body.get("project", "")),
                    str(body.get("demosaic", "markesteijn-1-pass")))
                self._json(result)
                return
            elif parsed.path == "/api/jobs/add-renderer-export":
                if self.server.jobs is None:
                    raise JobError("job manager is disabled")
                result = self.server.jobs.add_renderer_export(
                    str(body.get("source", "")), str(body.get("reference", "")),
                    str(body.get("directions", "")), str(body.get("photo", "")),
                    str(body.get("style", "")), str(body.get("project", "")),
                    str(body.get("demosaic", "markesteijn-1-pass")))
                self._json(result)
                return
            elif parsed.path == "/api/jobs/add-delivery-export":
                if self.server.jobs is None:
                    raise JobError("job manager is disabled")
                result = self.server.jobs.add_delivery_export(
                    str(body.get("source", "")), str(body.get("reference", "")),
                    str(body.get("directions", "")), str(body.get("photo", "")),
                    str(body.get("style", "")), str(body.get("engine", "")),
                    str(body.get("project", "")), str(body.get("destination", "")),
                    str(body.get("demosaic", "markesteijn-1-pass")),
                    str(body.get("render", "")))
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
                try:
                    chosen = dialogs.choose_folder(
                        "Choose a folder of photographs to cull")
                except dialogs.DialogError as exc:
                    raise JobError(str(exc)) from exc
                self._json({"photos": chosen})
                return
            elif parsed.path == "/api/jobs/pick-style-photos":
                try:
                    photos = dialogs.choose_files(
                        "Add finished photographs",
                        image_only=True, multiple=True)
                except dialogs.DialogError as exc:
                    raise JobError(str(exc)) from exc
                self._json({"photos": photos})
                return
            elif parsed.path == "/api/jobs/pick-style-profile":
                try:
                    chosen = dialogs.choose_files(
                        "Choose an existing personal style profile")[0]
                except dialogs.DialogError as exc:
                    raise JobError(str(exc)) from exc
                self._json({"existing": chosen})
                return
            elif parsed.path == "/api/jobs/pick-style-output":
                try:
                    output = dialogs.choose_save_path(
                        "Save the personal style profile",
                        "personal-style-profile-v2.json")
                except dialogs.DialogError as exc:
                    raise JobError(str(exc)) from exc
                if Path(output).suffix.casefold() != ".json":
                    output += ".json"
                self._json({"output": output})
                return
            elif parsed.path == "/api/raw-source/pick":
                try:
                    chosen = dialogs.choose_folder(
                        "Choose the folder containing RAW originals")
                except dialogs.DialogError as exc:
                    raise RawSourceError(str(exc)) from exc
                state = self.server.raw_sources.configure(Path(chosen))
                self.server.payload["raw_source"] = state
                self.server.project = register_file_artifact(
                    self.server.project_path, "raw_source_map",
                    self.server.raw_sources.path, stage="shortlist")
                self._json(state)
                return
            elif parsed.path == "/api/raw-source/configure":
                state = self.server.raw_sources.configure(
                    Path(str(body.get("path", ""))))
                self.server.payload["raw_source"] = state
                self.server.project = register_file_artifact(
                    self.server.project_path, "raw_source_map",
                    self.server.raw_sources.path, stage="shortlist")
                self._json(state)
                return
            elif parsed.path == "/api/action/pick-destination":
                try:
                    chosen = dialogs.choose_folder(
                        "Choose a destination for selected photographs")
                except dialogs.DialogError as exc:
                    raise ActionError(str(exc)) from exc
                self._json({"destination": chosen})
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
            if parsed.path.startswith("/api/review/") and self.server.reviews.path.is_file():
                self.server.project = register_file_artifact(
                    self.server.project_path, "culling_review",
                    self.server.reviews.path, stage="cull")
            elif (
                parsed.path.startswith("/api/shortlist/")
                and self.server.shortlist_reviews is not None
                and self.server.shortlist_reviews.path.is_file()
            ):
                self.server.project = register_file_artifact(
                    self.server.project_path, "shortlist_review",
                    self.server.shortlist_reviews.path, stage="shortlist")
            elif (
                parsed.path.startswith("/api/raw-source/")
                and self.server.raw_sources.path.is_file()
            ):
                self.server.project = register_file_artifact(
                    self.server.project_path, "raw_source_map",
                    self.server.raw_sources.path, stage="shortlist")
            self._json(state)
        except (
            ReviewError, PhotoError, ActionError, JobError,
            ProviderError,
            FaceError,
            ShortlistError,
            ShortlistReviewError,
            RawSourceError,
            TypeError, ValueError,
        ) as exc:
            status = (
                HTTPStatus.CONFLICT
                if "reload" in str(exc).lower()
                or "different version" in str(exc).lower()
                else HTTPStatus.BAD_REQUEST
            )
            self._json({"error": str(exc)}, status)
