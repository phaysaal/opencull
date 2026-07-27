#!/usr/bin/env python3
"""Launch OpenCull's local GUI with immutable evidence and human review."""

from __future__ import annotations

import argparse
import threading
import webbrowser
from pathlib import Path

from opencull_gui.photos import PhotoError, PhotoStore
from opencull_gui.measurements import ManifestError, load_measurements
from opencull_gui.report import ReportError, load_report
from opencull_gui.reviews import ReviewStore, default_review_path
from opencull_gui.server import ReviewServer


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
        "--preview-workers",
        type=int,
        default=2,
        help="bounded background preview workers (default: 2, maximum: 8)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = load_report(args.report)
        photos = PhotoStore(args.photos, args.cache)
        reviews = ReviewStore(
            args.review or default_review_path(report.path),
            report,
            photos.root,
        )
        measurements, manifest_path = load_measurements(args.manifest, report)
    except (ReportError, PhotoError, ManifestError) as exc:
        raise SystemExit(f"error: {exc}") from exc
    server = ReviewServer(
        (args.host, args.port),
        report,
        photos,
        reviews,
        measurements=measurements,
        manifest_path=manifest_path,
        preview_workers=args.preview_workers,
    )
    url = f"http://{args.host}:{server.server_port}/"
    print("OpenCull review GUI")
    print(f"Report: {report.path}")
    print(f"Photos: {photos.root}")
    print(f"Review: {reviews.path}")
    print(f"Metrics: {manifest_path or 'not supplied'}")
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
