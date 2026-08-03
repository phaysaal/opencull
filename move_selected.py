#!/usr/bin/env python3
"""Move photographs selected by an OpenCull report into a subfolder."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_SUBFOLDER = "opensull"


class MoveSelectedError(ValueError):
    """The move cannot be performed safely."""


@dataclass(frozen=True)
class Move:
    source: Path
    destination: Path


def selected_names(report_path: Path) -> list[str]:
    try:
        report: Any = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MoveSelectedError(f"cannot read report {report_path}: {exc}") from exc

    if not isinstance(report, dict) or not isinstance(report.get("keep"), list):
        raise MoveSelectedError("report must contain a 'keep' list")

    names: list[str] = []
    seen: set[str] = set()
    for decision in report["keep"]:
        if not isinstance(decision, dict) or not isinstance(
                decision.get("photos"), list):
            raise MoveSelectedError("every keep entry must contain a 'photos' list")
        for name in decision["photos"]:
            if not isinstance(name, str) or not name:
                raise MoveSelectedError("selected photo names must be non-empty text")
            if Path(name).name != name or name in {".", ".."}:
                raise MoveSelectedError(f"unsafe selected filename: {name!r}")
            if name not in seen:
                seen.add(name)
                names.append(name)
    return names


def build_plan(
    report_path: Path,
    photos_dir: Path,
    subfolder: str,
    recursive: bool = False,
) -> list[Move]:
    photos_dir = photos_dir.resolve()
    if not photos_dir.is_dir():
        raise MoveSelectedError(f"photo directory does not exist: {photos_dir}")

    relative_destination = Path(subfolder)
    if (
        not subfolder
        or relative_destination.is_absolute()
        or ".." in relative_destination.parts
    ):
        raise MoveSelectedError("subfolder must be a safe relative path")

    destination_dir = (photos_dir / relative_destination).resolve()
    if destination_dir == photos_dir or photos_dir not in destination_dir.parents:
        raise MoveSelectedError("destination must be a subfolder of the photo directory")

    plan: list[Move] = []
    for name in selected_names(report_path):
        direct = photos_dir / name
        if direct.is_file():
            matches = [direct]
        elif recursive:
            matches = [
                path for path in photos_dir.rglob(name)
                if path.is_file() and destination_dir not in path.resolve().parents
            ]
        else:
            matches = []

        if not matches:
            raise MoveSelectedError(f"selected photo is missing: {name}")
        if len(matches) > 1:
            locations = ", ".join(str(path) for path in matches)
            raise MoveSelectedError(
                f"selected filename is ambiguous ({name}): {locations}")

        destination = destination_dir / name
        if destination.exists():
            raise MoveSelectedError(f"destination already exists: {destination}")
        plan.append(Move(matches[0].resolve(), destination))
    return plan


def execute(plan: list[Move]) -> None:
    if not plan:
        return
    plan[0].destination.parent.mkdir(parents=True, exist_ok=True)
    for item in plan:
        shutil.move(str(item.source), str(item.destination))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Move photos selected by an OpenCull result into a subfolder. "
            "The default is a dry run; pass --apply to perform the moves."
        )
    )
    parser.add_argument("report", type=Path, help="OpenCull result JSON")
    parser.add_argument("photos", type=Path, help="folder containing original photos")
    parser.add_argument(
        "--subfolder",
        default=DEFAULT_SUBFOLDER,
        help=f"destination below PHOTOS (default: {DEFAULT_SUBFOLDER})",
    )
    parser.add_argument(
        "--recursive", action="store_true", help="find selected files below nested folders"
    )
    parser.add_argument(
        "--apply", action="store_true", help="perform moves after successful preflight"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        plan = build_plan(args.report, args.photos, args.subfolder, args.recursive)
        action = "MOVE" if args.apply else "WOULD MOVE"
        for item in plan:
            print(f"{action}: {item.source} -> {item.destination}")
        if args.apply:
            execute(plan)
            print(f"Moved {len(plan)} selected photo(s).")
        else:
            print(f"Dry run: {len(plan)} selected photo(s); nothing changed.")
            if plan:
                print("Run again with --apply to perform these moves.")
        return 0
    except MoveSelectedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
