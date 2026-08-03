"""Render one photograph through OpenCull and darktable research paths."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import tifffile
from PIL import Image, ImageDraw, ImageFont, ImageOps

from darktable_engine import render_darktable_default
from development_engine import _srgb_to_linear_rec2020, render_recipe
from raw_developer import render_baseline

COMPARISON_FORMAT = "opencull-renderer-comparison-v1"
FULL_RESOLUTION_FORMAT = "opencull-renderer-export-v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _jpeg_baseline(source: Path, destination: Path) -> Path:
    with Image.open(source) as image:
        oriented = ImageOps.exif_transpose(image)
        srgb = np.asarray(oriented.convert("RGB"), dtype=np.float32) / 255.0
    linear = np.clip(_srgb_to_linear_rec2020(srgb), 0.0, 1.0)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tifffile.imwrite(destination, np.uint16(linear * 65535.0 + 0.5))
    return destination


def _image_array(path: Path, size: tuple[int, int] | None = None) -> np.ndarray:
    with Image.open(path) as image:
        image = image.convert("RGB")
        if size and image.size != size:
            image = image.resize(size, Image.Resampling.LANCZOS)
        return np.asarray(image, dtype=np.float32) / 255.0


def image_metrics(path: Path, max_dimension: int = 2048) -> dict[str, Any]:
    with Image.open(path) as image:
        size = image.size
        measured = image.copy()
        measured.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
        measurement_size = measured.size
        rgb = np.asarray(measured.convert("RGB"), dtype=np.float32) / 255.0
    luma = np.tensordot(rgb, np.array([0.2126, 0.7152, 0.0722]), axes=1)
    maximum = rgb.max(axis=2)
    minimum = rgb.min(axis=2)
    saturation = np.divide(maximum - minimum, np.maximum(maximum, 1e-6))
    padded = np.pad(luma, 1, mode="edge")
    laplacian = (
        padded[:-2, 1:-1] + padded[2:, 1:-1]
        + padded[1:-1, :-2] + padded[1:-1, 2:]
        - 4 * padded[1:-1, 1:-1])
    return {
        "path": str(path), "sha256": _sha256(path),
        "width": size[0], "height": size[1],
        "measurement_width": measurement_size[0],
        "measurement_height": measurement_size[1],
        "mean_luminance": round(float(luma.mean()), 6),
        "median_luminance": round(float(np.median(luma)), 6),
        "luminance_p01": round(float(np.quantile(luma, 0.01)), 6),
        "luminance_p99": round(float(np.quantile(luma, 0.99)), 6),
        "shadow_clip_fraction": round(float((maximum <= 1 / 255).mean()), 6),
        "highlight_clip_fraction": round(float((minimum >= 254 / 255).mean()), 6),
        "mean_saturation": round(float(saturation.mean()), 6),
        "sharpness_laplacian_variance": round(float(laplacian.var()), 8),
    }


def pair_metrics(first: Path, second: Path, max_dimension: int = 2048) -> dict[str, Any]:
    with Image.open(first) as image:
        measured = image.copy()
        measured.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
        common = measured.size
        a = np.asarray(measured.convert("RGB"), dtype=np.float32) / 255.0
    b = _image_array(second, common)
    delta = a - b
    mse = float(np.mean(delta * delta))
    luma_a = np.tensordot(a, np.array([0.2126, 0.7152, 0.0722]), axes=1)
    luma_b = np.tensordot(b, np.array([0.2126, 0.7152, 0.0722]), axes=1)
    correlation = float(np.corrcoef(luma_a.ravel(), luma_b.ravel())[0, 1])
    return {
        "comparison_size": list(common),
        "mean_absolute_rgb_difference": round(float(np.abs(delta).mean()), 6),
        "root_mean_square_rgb_difference": round(math.sqrt(mse), 6),
        "psnr_db": None if mse == 0 else round(10 * math.log10(1 / mse), 4),
        "luminance_correlation": None if not math.isfinite(correlation)
        else round(correlation, 6),
        "note": "Second image was resized to the first image dimensions for pair metrics.",
    }


def _contact_sheet(items: list[tuple[str, Path]], output: Path) -> None:
    panel_width, panel_height, label_height = 720, 540, 54
    canvas = Image.new("RGB", (panel_width * len(items), panel_height + label_height),
                       "#15191f")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=18)
    for index, (label, path) in enumerate(items):
        with Image.open(path) as opened:
            image = opened.convert("RGB")
            image.thumbnail((panel_width, panel_height), Image.Resampling.LANCZOS)
        x = index * panel_width + (panel_width - image.width) // 2
        y = label_height + (panel_height - image.height) // 2
        canvas.paste(image, (x, y))
        draw.text((index * panel_width + 18, 17), label, fill="#f4f6f8", font=font)
    canvas.save(output, format="JPEG", quality=94, optimize=True)


def compare_renderers(
    source: Path, reference: Path, recipe: dict[str, Any], output_dir: Path,
    *, darktable_cli: str | Path | None = None,
    existing_opencull_render: Path | None = None,
    preview_max_dimension: int = 2048,
    demosaic_mode: str = "markesteijn-1-pass",
) -> dict[str, Any]:
    source = source.expanduser().resolve()
    reference = reference.expanduser().resolve()
    destination = output_dir.expanduser().resolve()
    if not source.is_file() or not reference.is_file():
        raise ValueError("source and reference photographs must exist")
    if recipe.get("format") != "opencull-development-recipe-v1":
        raise ValueError("comparison requires a compiled OpenCull recipe")
    destination.mkdir(parents=True, exist_ok=True)
    recipe_path = destination / f"{source.stem}.comparison.recipe.json"
    recipe_path.write_text(json.dumps(recipe, indent=2) + "\n", encoding="utf-8")

    source_kind = "jpeg" if source.suffix.casefold() in {".jpg", ".jpeg"} else "raw"
    existing = existing_opencull_render.expanduser().resolve() if existing_opencull_render else None
    if existing:
        if not existing.is_file():
            raise ValueError(f"existing OpenCull render is unavailable: {existing}")
        opencull = {
            "format": "opencull-existing-render-reference-v1",
            "created_at": datetime.now(UTC).isoformat(),
            "recipe": recipe,
            "output": {"path": str(existing), "sha256": _sha256(existing)},
            "notice": "Reused completed OpenCull render for memory-safe comparison.",
        }
        baseline_record = None
    else:
        if source_kind == "raw":
            baseline_record = render_baseline(source, destination / "opencull-baseline")
            baseline = Path(baseline_record["outputs"]["linear_tiff"]["path"])
        else:
            baseline_record = None
            baseline = _jpeg_baseline(
                source, destination / "opencull-baseline" / f"{source.stem}.tiff")
        opencull = render_recipe(
            baseline, recipe, destination / "opencull", allow_incomplete=True,
            reference_jpeg=reference)

    darktable = render_darktable_default(
        source, destination / "darktable", executable=darktable_cli,
        max_dimension=preview_max_dimension, demosaic_mode=demosaic_mode)
    darktable_output = Path(darktable["output"]["path"])
    darktable_baseline = _jpeg_baseline(
        darktable_output,
        destination / "darktable-guided-baseline" / f"{source.stem}.tiff")
    hybrid_recipe = json.loads(json.dumps(recipe))
    hybrid_recipe["style"] = (
        f"{recipe.get('style', 'render')}-darktable-{demosaic_mode}-guided")
    hybrid_recipe["engine_chain"] = ["darktable-default", "opencull-recipe-executor"]
    guided = render_recipe(
        darktable_baseline, hybrid_recipe, destination / "darktable-guided",
        allow_incomplete=True)

    opencull_output = Path(opencull["output"]["path"])
    guided_output = Path(guided["output"]["path"])
    sheet = destination / f"{source.stem}.{demosaic_mode}.renderer-comparison.jpg"
    demosaic_label = darktable.get("engine", {}).get(
        "demosaic", {}).get("label", demosaic_mode)
    _contact_sheet([
        ("OpenCull renderer", opencull_output),
        (f"darktable native · {demosaic_label}", darktable_output),
        (f"darktable {demosaic_label} + same OpenCull recipe", guided_output),
    ], sheet)
    metrics = {
        "opencull": image_metrics(opencull_output),
        "darktable_native": image_metrics(darktable_output),
        "darktable_guided": image_metrics(guided_output),
        "opencull_vs_darktable_native": pair_metrics(opencull_output, darktable_output),
        "opencull_vs_darktable_guided": pair_metrics(opencull_output, guided_output),
    }
    record = {
        "format": COMPARISON_FORMAT,
        "created_at": datetime.now(UTC).isoformat(),
        "research_notice": (
            "The native darktable image is a no-recipe control. The guided darktable "
            "image uses darktable for initial development and the current OpenCull "
            "executor for identical compiled creative operations. It does not yet "
            "claim native darktable execution of those operations."),
        "measurement_notice": (
            f"Visual and numeric comparisons use previews bounded to "
            f"{preview_max_dimension}px to avoid full-resolution memory bias; "
            "source/output dimensions and hashes remain recorded."),
        "demosaic": darktable.get("engine", {}).get("demosaic", {}),
        "source": {"path": str(source), "sha256": _sha256(source),
                   "kind": source_kind},
        "reference": {"path": str(reference), "sha256": _sha256(reference)},
        "recipe": {"path": str(recipe_path), "sha256": _sha256(recipe_path),
                   "style": recipe.get("style"),
                   "source_photo": recipe.get("source_photo"),
                   "operation_count": len(recipe.get("operations", []))},
        "renders": {"opencull": opencull, "darktable_native": darktable,
                    "darktable_guided": guided},
        "visual_comparison": {"path": str(sheet), "sha256": _sha256(sheet)},
        "technical_comparison": metrics,
        "baseline": baseline_record,
    }
    report = destination / f"{source.stem}.{demosaic_mode}.renderer-comparison.json"
    report.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    record["report_path"] = str(report)
    return record


def render_full_resolution_pair(
    source: Path, reference: Path, recipe: dict[str, Any], output_dir: Path,
    *, darktable_cli: str | Path | None = None,
    demosaic_mode: str = "markesteijn-1-pass",
    progress: Callable[[int, str], None] | None = None,
) -> dict[str, Any]:
    """Render exportable darktable-native and darktable-guided files.

    Unlike :func:`compare_renderers`, this path never constrains the darktable
    output dimensions and does not build a contact sheet or full-size pairwise
    metrics.  It is therefore suitable for delivery while the comparison path
    remains a deliberately bounded proofing workflow.
    """
    source = source.expanduser().resolve()
    reference = reference.expanduser().resolve()
    destination = output_dir.expanduser().resolve()
    if not source.is_file() or not reference.is_file():
        raise ValueError("source and reference photographs must exist")
    if recipe.get("format") != "opencull-development-recipe-v1":
        raise ValueError("full-resolution rendering requires a compiled OpenCull recipe")
    destination.mkdir(parents=True, exist_ok=True)
    recipe_path = destination / f"{source.stem}.full-resolution.recipe.json"
    recipe_path.write_text(json.dumps(recipe, indent=2) + "\n", encoding="utf-8")

    if progress:
        progress(20, "Developing the complete RAW with darktable")
    darktable = render_darktable_default(
        source, destination / "darktable-native", executable=darktable_cli,
        max_dimension=None, demosaic_mode=demosaic_mode)
    darktable_output = Path(darktable["output"]["path"])
    with Image.open(darktable_output) as image:
        native_dimensions = list(image.size)

    if progress:
        progress(50, "Preparing the full-resolution guided baseline")
    baseline = _jpeg_baseline(
        darktable_output,
        destination / "darktable-guided-baseline" / f"{source.stem}.tiff")
    hybrid_recipe = json.loads(json.dumps(recipe))
    original_style = str(recipe.get("style", "render"))
    hybrid_recipe["style"] = (
        f"{original_style}-darktable-{demosaic_mode}-guided-full")
    hybrid_recipe["engine_chain"] = [
        "darktable-full-resolution", "opencull-recipe-executor"]
    if progress:
        progress(65, "Applying the compiled OpenCull treatment at full size")
    guided = render_recipe(
        baseline, hybrid_recipe, destination / "darktable-guided",
        allow_incomplete=True)
    guided_output = Path(guided["output"]["path"])
    with Image.open(guided_output) as image:
        guided_dimensions = list(image.size)
    if progress:
        progress(88, "Hashing and recording the completed files")

    native_project_render = {
        "format": "opencull-external-render-reference-v1",
        "created_at": darktable.get("created_at", datetime.now(UTC).isoformat()),
        "recipe": {
            "style": f"{original_style}-darktable-{demosaic_mode}-native-full",
            "source_photo": recipe.get("source_photo"),
            "revision": recipe.get("revision", 0),
            "engine_chain": ["darktable-full-resolution"],
        },
        "output": darktable["output"],
        "engine": darktable.get("engine", {}),
    }
    record = {
        "format": FULL_RESOLUTION_FORMAT,
        "created_at": datetime.now(UTC).isoformat(),
        "source": {"path": str(source), "sha256": _sha256(source)},
        "reference": {"path": str(reference), "sha256": _sha256(reference)},
        "recipe": {"path": str(recipe_path), "sha256": _sha256(recipe_path),
                   "style": original_style,
                   "source_photo": recipe.get("source_photo")},
        "demosaic": darktable.get("engine", {}).get("demosaic", {}),
        "renders": {
            "darktable_native": native_project_render,
            "darktable_guided": guided,
        },
        "dimensions": {
            "darktable_native": native_dimensions,
            "darktable_guided": guided_dimensions,
        },
        "notice": (
            "The native file is an unstyled darktable control. The guided file "
            "uses full-resolution darktable development followed by the compiled "
            "OpenCull creative recipe; it does not claim native darktable execution "
            "of those creative operations."),
    }
    report = destination / (
        f"{source.stem}.{original_style}.{demosaic_mode}.full-resolution.json")
    report.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    record["report_path"] = str(report)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("reference", type=Path)
    parser.add_argument("recipe", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--darktable-cli")
    parser.add_argument("--opencull-render", type=Path,
                        help="reuse a completed OpenCull render instead of rerendering")
    parser.add_argument("--preview-max-dimension", type=int, default=2048)
    parser.add_argument("--demosaic", choices=[
        "markesteijn-1-pass", "markesteijn-3-pass",
        "markesteijn-3-pass-vng"], default="markesteijn-1-pass")
    args = parser.parse_args(argv)
    recipe = json.loads(args.recipe.read_text(encoding="utf-8"))
    result = compare_renderers(
        args.source, args.reference, recipe, args.output_dir,
        darktable_cli=args.darktable_cli,
        existing_opencull_render=args.opencull_render,
        preview_max_dimension=args.preview_max_dimension,
        demosaic_mode=args.demosaic)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
