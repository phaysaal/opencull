"""Render when necessary and atomically deliver one developed photograph."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from renderer_export_pipeline import run_renderer_export_pipeline
from development_pipeline import run_pipeline
from opencull_gui.project import export_project_render


def _write_receipt(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def run_delivery_export(
    *, source: Path, reference: Path, directions: Path, photo: str,
    style: str, engine: str, demosaic: str, render: Path | None,
    render_output_dir: Path, project: Path, destination: Path,
    export_key: str, receipt: Path,
) -> dict:
    print("EXPORT_PROGRESS 5 Validating project and recipe", flush=True)
    render_path = render.expanduser().resolve() if render else None
    if render_path is None:
        if engine == "darktable":
            print("EXPORT_PROGRESS 12 Rendering full-size Darktable treatment", flush=True)
            rendered = run_renderer_export_pipeline(
                source, reference, directions, photo, style,
                render_output_dir, project, demosaic)
            render_path = Path(
                rendered["renders"]["darktable_guided"]["output"]["path"])
        else:
            print("EXPORT_PROGRESS 12 Rendering full-size Default treatment", flush=True)
            rendered = run_pipeline(
                source, reference, directions, photo, style,
                render_output_dir, project)
            render_path = Path(rendered["output"]["path"])
    print("EXPORT_PROGRESS 88 Copying and verifying the delivery image", flush=True)
    record = export_project_render(
        project, render_path, destination, export_key=export_key,
        photo=photo, style=style, engine=engine)
    _write_receipt(receipt, record)
    print("EXPORT_PROGRESS 100 Export complete", flush=True)
    print("EXPORT_RESULT " + json.dumps(record, separators=(",", ":")), flush=True)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--directions", type=Path, required=True)
    parser.add_argument("--photo", required=True)
    parser.add_argument("--style", required=True)
    parser.add_argument("--engine", choices=["default", "darktable"], required=True)
    parser.add_argument("--demosaic", required=True)
    parser.add_argument("--render", type=Path)
    parser.add_argument("--render-output-dir", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--export-key", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    run_delivery_export(
        source=args.source, reference=args.reference,
        directions=args.directions, photo=args.photo, style=args.style,
        engine=args.engine, demosaic=args.demosaic, render=args.render,
        render_output_dir=args.render_output_dir, project=args.project,
        destination=args.destination, export_key=args.export_key,
        receipt=args.receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
