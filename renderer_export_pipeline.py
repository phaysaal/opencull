"""Compile one edit direction and render full-resolution darktable outputs."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def _enter_background_export_lane() -> None:
    """Lower this worker before importing native image-processing modules.

    ``nice`` is inherited by darktable-cli children.  The queue supplies the
    native thread limits through the environment, leaving interactive preview
    processes at normal priority with their own disposable Darktable state.
    """
    requested = os.environ.get("DARKIMIYA_EXPORT_NICE", "").strip()
    if not requested:
        return
    try:
        os.nice(max(0, min(19, int(requested))))
    except (AttributeError, OSError, ValueError):
        # Isolation and thread limits still apply if the OS declines a nice
        # adjustment (for example, under a constrained packaging sandbox).
        pass


_enter_background_export_lane()

from development_pipeline import calibrated_recipe
from opencull_gui.project import register_render
from recipe_compiler import compile_recipe
from renderer_comparison import render_full_resolution_pair


def run_renderer_export_pipeline(
    source: Path, reference: Path, directions: Path, photo: str, style: str,
    output_dir: Path, project: Path, demosaic: str,
) -> dict:
    print("RENDER_EXPORT_PROGRESS 5 Reading the edit direction", flush=True)
    value = json.loads(directions.expanduser().resolve().read_text(encoding="utf-8"))
    entry = next((item for item in value.get("entries", [])
                  if isinstance(item, dict) and item.get("photo") == photo), None)
    if entry is None:
        raise ValueError("photograph has no matching edit direction")
    source_kind = "jpeg" if source.suffix.casefold() in {".jpg", ".jpeg"} else "raw"
    recipe = calibrated_recipe(photo, source_kind) if style == "calibrated" else compile_recipe(
        photo, style, str(entry.get(f"{style}_title", style.title())),
        str(entry.get(f"{style}_intent", "")), entry.get(f"{style}_recipe", {}),
        str(entry.get("guardrails", "")), source_kind)
    print("RENDER_EXPORT_PROGRESS 15 Rendering full-resolution darktable image", flush=True)
    result = render_full_resolution_pair(
        source, reference, recipe, output_dir, demosaic_mode=demosaic,
        progress=lambda percent, message: print(
            f"RENDER_EXPORT_PROGRESS {percent} {message}", flush=True))
    print("RENDER_EXPORT_PROGRESS 90 Registering exportable project renders", flush=True)
    register_render(project, result["renders"]["darktable_native"])
    register_render(project, result["renders"]["darktable_guided"])
    print("RENDER_EXPORT_PROGRESS 100 Full-resolution darktable files are ready", flush=True)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--directions", type=Path, required=True)
    parser.add_argument("--photo", required=True)
    parser.add_argument("--style", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--demosaic", choices=[
        "markesteijn-1-pass", "markesteijn-3-pass",
        "markesteijn-3-pass-vng"], default="markesteijn-1-pass")
    args = parser.parse_args(argv)
    result = run_renderer_export_pipeline(
        args.source, args.reference, args.directions, args.photo, args.style,
        args.output_dir, args.project, args.demosaic)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
