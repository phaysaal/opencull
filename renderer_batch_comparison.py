"""Render an evidence batch through the built-in and darktable engine chains.

The comparison deliberately compiles one treatment per photograph and applies
that same recipe to both starting points.  It prefers a matching RAW source,
but can fall back to the reference JPEG so incomplete archives remain usable.
"""

from __future__ import annotations

import argparse
import html
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import tifffile
from PIL import Image, ImageDraw, ImageFont, ImageOps

from development_engine import _srgb_to_linear_rec2020, render_recipe
from raw_developer import render_baseline
from recipe_compiler import compile_recipe
from renderer_comparison import (
    image_metrics,
    pair_metrics,
    render_full_resolution_pair,
)


FORMAT = "darkimiya-renderer-comparison-batch-v1"
JPEG_SUFFIXES = {".jpg", ".jpeg"}


def _jpeg_baseline(source: Path, destination: Path) -> Path:
    with Image.open(source) as image:
        oriented = ImageOps.exif_transpose(image)
        srgb = np.asarray(oriented.convert("RGB"), dtype=np.float32) / 255.0
    linear = np.clip(_srgb_to_linear_rec2020(srgb), 0.0, 1.0)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tifffile.imwrite(destination, np.uint16(linear * 65535.0 + 0.5))
    return destination


def _raw_index(search_roots: list[Path], stems: set[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for root in search_roots:
        resolved = root.expanduser().resolve()
        if not resolved.is_dir():
            continue
        for path in resolved.rglob("*"):
            if (path.is_file() and path.suffix.casefold() == ".raf"
                    and path.stem.casefold() in stems):
                result.setdefault(path.stem.casefold(), path.resolve())
    return result


def _style_for(entry: dict[str, Any], preferred: str) -> str | None:
    if entry.get(f"{preferred}_recipe"):
        return preferred
    if preferred == "personal" and entry.get("standard_recipe"):
        return "standard"
    return None


def _contact_sheet_1440p(
    items: list[tuple[str, Path]], output: Path,
) -> None:
    """Build an uncropped two-up proof sized for a 2560x1440 display."""
    canvas_width, canvas_height = 2560, 1440
    label_height, gutter, outer_margin = 72, 20, 20
    panel_width = (canvas_width - gutter) // 2
    available_width = panel_width - outer_margin * 2
    available_height = canvas_height - label_height - outer_margin
    canvas = Image.new("RGB", (canvas_width, canvas_height), "#15191f")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=26)
    for index, (label, path) in enumerate(items[:2]):
        with Image.open(path) as opened:
            image = opened.convert("RGB")
            image.thumbnail(
                (available_width, available_height), Image.Resampling.LANCZOS)
        panel_x = index * (panel_width + gutter)
        x = panel_x + (panel_width - image.width) // 2
        y = label_height + (available_height - image.height) // 2
        canvas.paste(image, (x, y))
        draw.text(
            (panel_x + outer_margin, 22), label,
            fill="#f4f6f8", font=font)
    canvas.save(output, format="JPEG", quality=95, optimize=True)


def _write_gallery(output_dir: Path, manifest: dict[str, Any]) -> Path:
    cards = []
    for item in manifest["items"]:
        photo = html.escape(str(item["photo"]))
        stem = Path(str(item["photo"])).stem
        source_kind = html.escape(str(item["source_kind"]).upper())
        style = html.escape(str(item["style"]))
        cards.append(f"""
        <article>
          <h2>{photo}</h2><p>{source_kind} source · {style} recipe</p>
          <div class="compare" style="--position:50%">
            <img class="base" src="pairs/{stem}.darktable.jpg"
                 alt="Darktable-powered renderer for {photo}">
            <img class="reveal" src="pairs/{stem}.default.jpg"
                 alt="Default renderer for {photo}">
            <span class="label left">Default</span><span class="label right">Darktable</span>
            <input aria-label="Drag to compare Default and Darktable" type="range"
                   min="0" max="100" value="50">
          </div>
          <nav><a href="pairs/{stem}.default.jpg">Default full size</a>
          <a href="pairs/{stem}.darktable.jpg">Darktable full size</a>
          <a href="pairs/{stem}.side-by-side.jpg">2560×1440 side by side</a></nav>
        </article>""")
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Darkimiya renderer comparison</title>
<style>
*{{box-sizing:border-box}} html{{scroll-snap-type:y proximity}}
body{{margin:0;background:#11151b;color:#f4f6f8;font:15px -apple-system,BlinkMacSystemFont,sans-serif}}
header{{position:sticky;top:0;padding:14px 24px;background:#11151bf2;backdrop-filter:blur(18px);z-index:2}}
h1{{margin:0 0 3px;font-size:21px}} header p,article p{{margin:0;color:#aab4c1}}
main{{width:100%;max-width:2560px;margin:auto;padding:10px 14px 32px}}
article{{min-height:calc(100vh - 82px);scroll-snap-align:start;background:#1b222c;border:1px solid #303b49;border-radius:12px;padding:12px 14px;margin-bottom:12px}}
h2{{display:inline;font-size:17px;margin:0 10px 0 0}} article p{{display:inline}}
.compare{{position:relative;width:100%;height:calc(100vh - 150px);margin:10px auto;background:#15191f;border-radius:7px;overflow:hidden}}
.compare img{{position:absolute;inset:0;width:100%;height:100%;object-fit:contain}}
.compare .reveal{{clip-path:inset(0 calc(100% - var(--position)) 0 0)}}
.compare input{{position:absolute;left:2%;bottom:16px;width:96%;z-index:3;accent-color:#f4f6f8}}
.label{{position:absolute;top:14px;z-index:4;padding:6px 10px;border-radius:7px;background:#11151bcc;font-weight:650}}
.label.left{{left:14px}} .label.right{{right:14px}}
nav{{display:flex;gap:22px}} a{{color:#73b7ff;text-decoration:none}} a:hover{{text-decoration:underline}}
</style></head><body><header><h1>Default vs darktable-powered</h1>
<p>{len(manifest['items'])} personal-style pairs · Markesteijn 3-pass for RAF sources · click a proof or full-size file</p>
</header><main>{''.join(cards)}</main>
<script>document.querySelectorAll('.compare').forEach(viewer=>{{
const slider=viewer.querySelector('input');
slider.addEventListener('input',()=>viewer.style.setProperty('--position',slider.value+'%'));
}});</script></body></html>"""
    destination = output_dir / "index.html"
    destination.write_text(page, encoding="utf-8")
    return destination


def render_batch(
    directions: Path,
    output_dir: Path,
    raw_search_roots: list[Path],
    *,
    style: str = "personal",
    demosaic: str = "markesteijn-3-pass",
    darktable_cli: str | Path | None = None,
    force_photos: set[str] | None = None,
) -> dict[str, Any]:
    directions = directions.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    payload = json.loads(directions.read_text(encoding="utf-8"))
    entries = [item for item in payload.get("entries", [])
               if isinstance(item, dict) and item.get("photo")]
    photos_root = Path(str(payload.get("photos_root", directions.parent))).resolve()
    stems = {Path(str(item["photo"])).stem.casefold() for item in entries}
    raw_files = _raw_index(raw_search_roots, stems)
    output_dir.mkdir(parents=True, exist_ok=True)

    prior_items: dict[str, dict[str, Any]] = {}
    prior_manifest_path = output_dir / "manifest.json"
    if prior_manifest_path.is_file():
        try:
            prior = json.loads(prior_manifest_path.read_text(encoding="utf-8"))
            if prior.get("format") == FORMAT:
                prior_items = {
                    str(item.get("photo")): item for item in prior.get("items", [])
                    if isinstance(item, dict) and item.get("photo")
                }
        except (OSError, json.JSONDecodeError):
            prior_items = {}

    manifest: dict[str, Any] = {
        "format": FORMAT,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "directions": str(directions),
        "preferred_style": style,
        "darktable_demosaic": demosaic,
        "comparison_notice": (
            "Default means the current Darkimiya built-in RAW/JPEG baseline, "
            "camera-JPEG calibration, and recipe executor. Darktable-powered "
            "means darktable full-resolution development followed by the same "
            "compiled recipe executor. Only RAW pairs assess demosaicing."),
        "items": [],
        "failures": [],
    }
    usable = [(entry, _style_for(entry, style)) for entry in entries]
    usable = [(entry, chosen) for entry, chosen in usable if chosen]
    total = len(usable)
    force_photos = force_photos or set()
    for number, (entry, chosen_style) in enumerate(usable, 1):
        photo = str(entry["photo"])
        prior_item = prior_items.get(photo)
        if photo not in force_photos and prior_item and all(
            Path(str(prior_item.get(key, ""))).is_file()
            for key in ("default", "darktable_powered", "side_by_side")
        ):
            manifest["items"].append(prior_item)
            print(f"BATCH_PROGRESS {number}/{total} {photo} resumed", flush=True)
            continue
        reference = (photos_root / photo).resolve()
        source = raw_files.get(reference.stem.casefold(), reference)
        source_kind = "jpeg" if source.suffix.casefold() in JPEG_SUFFIXES else "raw"
        item_dir = output_dir / reference.stem
        print(
            f"BATCH_PROGRESS {number - 1}/{total} {photo} "
            f"({source_kind}, {chosen_style})",
            flush=True,
        )
        try:
            if not reference.is_file() or not source.is_file():
                raise ValueError("source or reference photograph is unavailable")
            recipe = compile_recipe(
                photo,
                chosen_style,
                str(entry.get(f"{chosen_style}_title", chosen_style.title())),
                str(entry.get(f"{chosen_style}_intent", "")),
                entry.get(f"{chosen_style}_recipe", {}),
                str(entry.get("guardrails", "")),
                source_kind,
            )
            recipe_path = item_dir / f"{reference.stem}.{chosen_style}.recipe.json"
            recipe_path.parent.mkdir(parents=True, exist_ok=True)
            recipe_path.write_text(json.dumps(recipe, indent=2) + "\n", encoding="utf-8")

            if source_kind == "raw":
                baseline_record = render_baseline(source, item_dir / "default-baseline")
                baseline = Path(baseline_record["outputs"]["linear_tiff"]["path"])
            else:
                baseline_record = None
                baseline = _jpeg_baseline(
                    source, item_dir / "default-baseline" / f"{source.stem}.tiff")
            default_record = render_recipe(
                baseline,
                recipe,
                item_dir / "default",
                allow_incomplete=True,
                reference_jpeg=reference,
            )
            darktable_record = render_full_resolution_pair(
                source,
                reference,
                recipe,
                item_dir / "darktable-powered",
                darktable_cli=darktable_cli,
                demosaic_mode=demosaic,
            )
            default_output = Path(default_record["output"]["path"])
            darktable_output = Path(
                darktable_record["renders"]["darktable_guided"]["output"]["path"])
            pair_dir = output_dir / "pairs"
            pair_dir.mkdir(exist_ok=True)
            default_copy = pair_dir / f"{reference.stem}.default.jpg"
            darktable_copy = pair_dir / f"{reference.stem}.darktable.jpg"
            shutil.copy2(default_output, default_copy)
            shutil.copy2(darktable_output, darktable_copy)
            sheet = pair_dir / f"{reference.stem}.side-by-side.jpg"
            _contact_sheet_1440p([
                (f"Default · {source_kind.upper()}", default_copy),
                (f"Darktable-powered · {demosaic}", darktable_copy),
            ], sheet)
            record = {
                "photo": photo,
                "source": str(source),
                "source_kind": source_kind,
                "style": chosen_style,
                "recipe": str(recipe_path),
                "default": str(default_copy),
                "darktable_powered": str(darktable_copy),
                "side_by_side": str(sheet),
                "metrics": {
                    "default": image_metrics(default_copy),
                    "darktable_powered": image_metrics(darktable_copy),
                    "pair": pair_metrics(default_copy, darktable_copy),
                },
                "default_baseline": baseline_record,
                "darktable_report": darktable_record["report_path"],
            }
            manifest["items"].append(record)
            print(f"BATCH_PROGRESS {number}/{total} {photo} ready", flush=True)
        except Exception as exc:  # batch evidence should preserve later items
            failure = {"photo": photo, "source": str(source), "error": str(exc)}
            manifest["failures"].append(failure)
            print(f"BATCH_FAILURE {photo}: {exc}", flush=True)
        (output_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
    manifest["summary"] = {
        "requested": total,
        "completed": len(manifest["items"]),
        "failed": len(manifest["failures"]),
        "raw_pairs": sum(item["source_kind"] == "raw" for item in manifest["items"]),
        "jpeg_pairs": sum(item["source_kind"] == "jpeg" for item in manifest["items"]),
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    # Refresh proof dimensions even when every expensive render was resumed.
    for item in manifest["items"]:
        _contact_sheet_1440p([
            (f"Default · {str(item['source_kind']).upper()}", Path(item["default"])),
            (f"Darktable-powered · {demosaic}", Path(item["darktable_powered"])),
        ], Path(item["side_by_side"]))
    manifest["gallery"] = str(_write_gallery(output_dir, manifest))
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directions", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--raw-search-root", type=Path, action="append", default=[])
    parser.add_argument("--style", choices=("standard", "signature", "creative", "personal"),
                        default="personal")
    parser.add_argument("--demosaic", choices=(
        "markesteijn-1-pass", "markesteijn-3-pass", "markesteijn-3-pass-vng"),
        default="markesteijn-3-pass")
    parser.add_argument("--darktable-cli")
    parser.add_argument(
        "--force-photo", action="append", default=[],
        help="rerender a named photograph while resuming all other valid pairs")
    args = parser.parse_args(argv)
    result = render_batch(
        args.directions,
        args.output_dir,
        args.raw_search_root,
        style=args.style,
        demosaic=args.demosaic,
        darktable_cli=args.darktable_cli,
        force_photos=set(args.force_photo),
    )
    print(json.dumps(result["summary"], indent=2))
    return 1 if result["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
