"""One-shot supervised RAW/JPEG development from an OpenCull edit direction."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import tifffile
from PIL import Image, ImageOps

from development_engine import _srgb_to_linear_rec2020, render_recipe
from opencull_gui.project import register_render
from raw_developer import render_baseline
from recipe_compiler import compile_recipe

STYLES = {"calibrated", "standard", "signature", "creative", "personal"}


def calibrated_recipe(photo: str, source_kind: str) -> dict:
    """A technical camera-JPEG-matched baseline with no creative operations."""
    return {
        "format": "opencull-development-recipe-v1",
        "source_photo": photo,
        "source_kind": source_kind,
        "style": "calibrated",
        "title": "Calibrated RAW baseline",
        "intent": "Match the camera JPEG's broad tone and colour response.",
        "working_space": "scene-linear-rec2020-d65",
        "operations": [],
        "guardrails": ["Do not apply a creative treatment."],
        "diagnostics": [],
        "coverage": {"instructions": 0, "executable": 0,
                     "guardrails": 1, "unsupported": 0},
    }


def run_pipeline(
    source: Path, reference: Path, directions: Path, photo: str, style: str,
    output_dir: Path, project: Path,
) -> dict:
    if style not in STYLES:
        raise ValueError("unsupported development treatment")
    source = source.expanduser().resolve()
    reference = reference.expanduser().resolve()
    directions = directions.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    project = project.expanduser().resolve()
    if not source.is_file() or not reference.is_file():
        raise ValueError("development source and reference JPEG must exist")
    value = json.loads(directions.read_text(encoding="utf-8"))
    entry = next(
        (item for item in value.get("entries", [])
         if isinstance(item, dict) and item.get("photo") == photo), None)
    if entry is None:
        raise ValueError("photograph has no matching edit direction")
    source_kind = (
        "raw" if source.suffix.casefold() not in {".jpg", ".jpeg"} else "jpeg")
    print("DEVELOP_PROGRESS 10 Compiling guided recipe", flush=True)
    recipe = (
        calibrated_recipe(photo, source_kind)
        if style == "calibrated" else
        compile_recipe(
            photo, style, str(entry.get(f"{style}_title", style.title())),
            str(entry.get(f"{style}_intent", "")),
            entry.get(f"{style}_recipe", {}), str(entry.get("guardrails", "")),
            source_kind)
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    recipe_path = output_dir / f"{Path(photo).stem}.{style}.recipe.json"
    recipe_path.write_text(json.dumps(recipe, indent=2) + "\n", encoding="utf-8")
    if recipe["source_kind"] == "raw":
        print("DEVELOP_PROGRESS 25 Decoding neutral RAW baseline", flush=True)
        baseline_record = render_baseline(source, output_dir / "baseline")
        baseline = Path(baseline_record["outputs"]["linear_tiff"]["path"])
    else:
        print("DEVELOP_PROGRESS 25 Preparing color-managed JPEG baseline", flush=True)
        with Image.open(source) as image:
            oriented = ImageOps.exif_transpose(image)
            srgb = np.asarray(oriented.convert("RGB"), dtype=np.float32) / 255.0
        linear = np.clip(_srgb_to_linear_rec2020(srgb), 0.0, 1.0)
        baseline = output_dir / f"{Path(photo).stem}.jpeg-baseline-rec2020.tiff"
        tifffile.imwrite(baseline, np.uint16(linear * 65535.0 + 0.5))
    if style != "calibrated":
        print("DEVELOP_PROGRESS 45 Saving calibrated comparison", flush=True)
        calibrated = render_recipe(
            baseline, calibrated_recipe(photo, source_kind), output_dir,
            reference_jpeg=reference)
        register_render(project, calibrated)
    print("DEVELOP_PROGRESS 60 Applying guided treatment", flush=True)
    result = render_recipe(
        baseline, recipe, output_dir, allow_incomplete=True,
        reference_jpeg=reference)
    print("DEVELOP_PROGRESS 90 Registering provenance", flush=True)
    register_render(project, result)
    print("DEVELOP_PROGRESS 100 Treatment ready", flush=True)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--directions", type=Path, required=True)
    parser.add_argument("--photo", required=True)
    parser.add_argument("--style", choices=sorted(STYLES), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(run_pipeline(
        args.source, args.reference, args.directions, args.photo, args.style,
        args.output_dir, args.project), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
