"""Compile one edit direction and compare OpenCull with darktable."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from development_pipeline import calibrated_recipe
from opencull_gui.project import register_renderer_comparison
from recipe_compiler import compile_recipe
from renderer_comparison import compare_renderers


def run_comparison_pipeline(
    source: Path, reference: Path, directions: Path, photo: str, style: str,
    opencull_render: Path, output_dir: Path, project: Path,
    demosaic: str,
) -> dict:
    print("COMPARE_PROGRESS 10 Reading the common edit direction", flush=True)
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
    print("COMPARE_PROGRESS 25 Starting isolated darktable", flush=True)
    result = compare_renderers(
        source, reference, recipe, output_dir,
        existing_opencull_render=opencull_render, preview_max_dimension=2048,
        demosaic_mode=demosaic)
    print("COMPARE_PROGRESS 85 Saving visual and technical evidence", flush=True)
    register_renderer_comparison(project, result)
    print("COMPARE_PROGRESS 100 Renderer comparison ready", flush=True)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--directions", type=Path, required=True)
    parser.add_argument("--photo", required=True)
    parser.add_argument("--style", required=True)
    parser.add_argument("--opencull-render", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--demosaic", choices=[
        "markesteijn-1-pass", "markesteijn-3-pass",
        "markesteijn-3-pass-vng"], default="markesteijn-1-pass")
    args = parser.parse_args(argv)
    result = run_comparison_pipeline(
        args.source, args.reference, args.directions, args.photo, args.style,
        args.opencull_render, args.output_dir, args.project, args.demosaic)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
