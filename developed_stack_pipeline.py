"""Develop each chosen frame with its kept look, then stack the results.

The ordinary stack goes physics-first: raw sensor light, calibrated and
averaged, taste applied afterwards to the one picture. This pipeline is
the other road, asked for by name: the photographer's look applied to
EVERY frame first, each rendered to a 16-bit TIFF, and those stacked.
It is the G4 route from the research albums, made a button.

The trade is stated rather than hidden: frames that went through the
same nonlinear recipe stack fine relative to each other, but what the
recipe discarded -- a crushed black, a clipped star -- is discarded in
every frame, and no stack can give it back.
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any


def _say(percent: float, stage: str) -> None:
    print(f"TIMELAPSE_PROGRESS {int(round(percent))} {stage}", flush=True)


def _workspace(photos: Path, names: list[str]):
    """A develop stage for this folder, the way the treatments build one."""
    from opencull_gui.development import DevelopmentWorkspace
    from opencull_gui.project import (
        ensure_project_layout,
        load_or_create_folder_project,
    )
    from opencull_gui.raw_sources import RawSourceStore
    from opencull_gui.report import ReportIndex, load_report

    layout = ensure_project_layout(photos)
    found = sorted(layout["Reports"].glob("*-results.json"))
    if found:
        report = load_report(found[-1])
    else:
        report = ReportIndex(
            path=layout["Reports"] / f"{photos.name}-results.json",
            sha256="", data={}, cluster_by_id={}, decision_by_id={},
            photo_names=tuple(names))
    project_path, _ = load_or_create_folder_project(
        photos, report.path.stem,
        report.path.with_suffix(".opencull-project.json"))
    return DevelopmentWorkspace(
        project_path, layout,
        RawSourceStore(
            layout["Reports"] / f"{report.path.stem}.raw-source.json",
            report))


def run_developed_stack(
    photos: Path, plan_path: Path, output: Path, mode: str,
    focal_mm: float, sensor_mm: float, engine: str, demosaic: str,
) -> dict[str, Any]:
    import superimpose_kernel as sky

    photos = photos.expanduser().resolve()
    output = output.expanduser().resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    frames: dict[str, dict] = plan.get("frames") or {}
    if len(frames) < 2:
        raise ValueError("a stack needs at least two planned frames")
    workspace = _workspace(photos, list(frames))
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
            prefix=".developed-frames-", dir=output) as temporary:
        staged = Path(temporary)
        total = len(frames)
        for index, (name, recipe) in enumerate(sorted(frames.items())):
            _say(3 + index * 37.0 / total,
                 f"developing {index + 1} of {total}")
            # The style rides inside the recipe; the preview is asked
            # under the baseline name, the way treatment rounds are.
            proof = workspace.recipe_preview(
                name, "calibrated", engine, demosaic, 100000,
                recipe=recipe, tiff_sidecar=True)
            deep = Path(proof).with_suffix(".tiff")
            if not deep.is_file():
                raise ValueError(f"no 16-bit render for {name}")
            shutil.copy2(deep, staged / f"{Path(name).stem}.tiff")
        _say(42, "registering the developed frames")
        placed = sky.register(str(staged), "*.tiff", "",
                              float(focal_mm), float(sensor_mm), True)
        told = sky.superimpose(placed, mode, str(output))
        result = json.loads(told)
        if result.get("error"):
            raise ValueError(str(result["error"]))
    receipt = output / "developed-stack.json"
    receipt.write_text(json.dumps({
        "format": "darkimiya-developed-stack-v1",
        "photos": str(photos),
        "frames": sorted(frames),
        "engine": engine,
        "stack": result.get("stack"), "proof": result.get("proof"),
        "superimpose": result,
    }, indent=2) + "\n", encoding="utf-8")
    _say(100, "done")
    return result


def main() -> None:
    told = argparse.ArgumentParser(description=__doc__)
    told.add_argument("--photos", required=True)
    told.add_argument("--plan", required=True)
    told.add_argument("--output", required=True)
    told.add_argument("--mode", default="clipped")
    told.add_argument("--focal-mm", type=float, default=0.0)
    told.add_argument("--sensor-mm", type=float, default=23.5)
    told.add_argument("--engine", default="default")
    told.add_argument("--demosaic", default="markesteijn-1-pass")
    asked = told.parse_args()
    result = run_developed_stack(
        Path(asked.photos), Path(asked.plan), Path(asked.output),
        asked.mode, asked.focal_mm, asked.sensor_mm,
        asked.engine, asked.demosaic)
    print(f"{result.get('frames_used')} developed frames stacked into "
          f"{result.get('stack')}")


if __name__ == "__main__":
    main()
