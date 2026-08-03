#!/usr/bin/env python3
"""Launch Darkimiya's local GUI with immutable evidence and human review."""

from __future__ import annotations

import argparse
import threading
import webbrowser
from pathlib import Path

from opencull_gui.faces import FaceError, FaceStore, default_face_db
from opencull_gui.jobs import JobError, JobManager
from opencull_gui.measurements import ManifestError, load_measurements
from opencull_gui.photos import PhotoError, PhotoStore
from opencull_gui.project import ensure_project_layout, load_or_create_folder_project
from opencull_gui.providers import ProviderError, ProviderStore
from opencull_gui.report import ReportError, load_report
from opencull_gui.reviews import ReviewStore, default_review_path
from opencull_gui.server import ReviewServer
from opencull_gui.shortlist import ShortlistError


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="OpenCull result JSON")
    parser.add_argument("photos", type=Path, help="source photograph directory")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        choices=["127.0.0.1", "localhost"],
        help="local interface (remote binding is intentionally unsupported)",
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--cache",
        type=Path,
        default=Path(".opencull-cache/thumbnails"),
        help="generated preview cache",
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="do not open the browser"
    )
    parser.add_argument(
        "--review",
        type=Path,
        help="human-review sidecar (default: REPORT with .review.json suffix)",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="optional scanner manifest providing technical measurements",
    )
    parser.add_argument(
        "--shortlist",
        type=Path,
        help="optional professional-shortlist JSON bound to REPORT",
    )
    parser.add_argument(
        "--preview-workers",
        type=int,
        default=None,
        help=(
            "bounded background preview workers "
            "(default: scaled to this machine, maximum: 8)"
        ),
    )
    parser.add_argument(
        "--jobs",
        type=Path,
        default=Path(".opencull-jobs.json"),
        help="persistent culling queue state (default: .opencull-jobs.json)",
    )
    parser.add_argument(
        "--no-job-manager",
        action="store_true",
        help="disable queue worker (used by additional review-only tabs)",
    )
    parser.add_argument(
        "--providers",
        type=Path,
        default=Path(".opencull-providers.json"),
        help="non-secret provider profiles (default: .opencull-providers.json)",
    )
    parser.add_argument(
        "--faces",
        type=Path,
        help="private face database (default: REPORT.faces.sqlite3)",
    )
    parser.add_argument(
        "--face-models",
        type=Path,
        default=Path(".opencull-models"),
        help="pinned local YuNet/SFace directory",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = load_report(args.report)
        photos = PhotoStore(args.photos, args.cache)
        project_path, _project = load_or_create_folder_project(
            photos.root, report.path.stem,
            report.path.with_suffix(".opencull-project.json"))
        layout = ensure_project_layout(photos.root)
        managed_review = layout["Reviews"] / f"{report.path.stem}.review.json"
        reviews = ReviewStore(
            args.review or (
                managed_review
                if project_path == layout["manifest"]
                else default_review_path(report.path)),
            report,
            photos.root,
        )
        measurements, manifest_path = load_measurements(args.manifest, report)
        project_root = Path(__file__).resolve().parent
        providers = (
            None if args.no_job_manager
            else ProviderStore(args.providers, project_root)
        )
        jobs = (
            None if args.no_job_manager
            else JobManager(args.jobs, project_root, providers=providers)
        )
        faces = FaceStore(
            args.faces or default_face_db(report.path),
            report, photos, reviews, args.face_models)
    except (
        ReportError, PhotoError, ManifestError, JobError, ProviderError,
        FaceError,
    ) as exc:
        raise SystemExit(f"error: {exc}") from exc
    try:
        server = ReviewServer(
            (args.host, args.port),
            report,
            photos,
            reviews,
            measurements=measurements,
            manifest_path=manifest_path,
            preview_workers=args.preview_workers,
            jobs=jobs,
            providers=providers,
            faces=faces,
            shortlist_path=args.shortlist,
        )
    except ShortlistError as exc:
        raise SystemExit(f"error: {exc}") from exc
    url = f"http://{args.host}:{server.server_port}/"
    print("OpenCull review GUI")
    print(f"Report: {report.path}")
    print(f"Photos: {photos.root}")
    print(f"Review: {reviews.path}")
    print(f"Metrics: {manifest_path or 'not supplied'}")
    print(f"Jobs:   {jobs.state_path if jobs else 'disabled in review-only tab'}")
    print(
        f"Providers: "
        f"{providers.path if providers else 'disabled in review-only tab'}")
    print(f"Faces:  {faces.path} (local-only private database)")
    print(f"URL:    {url}")
    print("Mode:   viewing is read-only; file actions require explicit confirmation")
    print("Press Ctrl-C to stop.")
    if not args.no_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nStopping OpenCull GUI.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
