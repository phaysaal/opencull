"""Deterministic, read-only grouping of visual files and RAW companions."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from scan import BITMAP_EXTENSIONS, RAW_EXTENSIONS, SUPPORTED_EXTENSIONS


class AssetError(ValueError):
    """A photo library cannot be indexed safely."""


def _safe_relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError as exc:
        raise AssetError(f"photo escapes the source folder: {path}") from exc


@dataclass(frozen=True)
class AssetFamily:
    """Files in one directory sharing a case-insensitive filename stem."""

    asset_id: str
    directory: str
    stem: str
    files: tuple[str, ...]
    bitmap_files: tuple[str, ...]
    raw_files: tuple[str, ...]
    ambiguous: bool
    ambiguity: str

    def public(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "directory": self.directory,
            "stem": self.stem,
            "files": list(self.files),
            "bitmap_files": list(self.bitmap_files),
            "raw_files": list(self.raw_files),
            "ambiguous": self.ambiguous,
            "ambiguity": self.ambiguity,
        }


@dataclass(frozen=True)
class AssetIndex:
    root: Path
    families: tuple[AssetFamily, ...]
    family_by_file: dict[str, AssetFamily]

    def family_for(self, name: str) -> AssetFamily | None:
        return self.family_by_file.get(Path(name).as_posix())

    def raw_companions(self, name: str) -> tuple[str, ...]:
        family = self.family_for(name)
        return family.raw_files if family else ()


def index_asset_families(root: Path, recursive: bool = True) -> AssetIndex:
    """Index supported files without decoding or modifying any photograph."""
    resolved = root.expanduser().resolve()
    if not resolved.is_dir():
        raise AssetError(f"not a photo directory: {resolved}")
    iterator = resolved.rglob("*") if recursive else resolved.iterdir()
    grouped: dict[tuple[str, str], list[tuple[Path, str]]] = {}
    for path in iterator:
        if (
            not path.is_file()
            or path.suffix.lower() not in SUPPORTED_EXTENSIONS
        ):
            continue
        relative = _safe_relative(resolved, path)
        relative_path = Path(relative)
        key = (
            relative_path.parent.as_posix().casefold(),
            relative_path.stem.casefold(),
        )
        grouped.setdefault(key, []).append((path, relative))

    families: list[AssetFamily] = []
    family_by_file: dict[str, AssetFamily] = {}
    for key in sorted(grouped):
        records = sorted(grouped[key], key=lambda item: item[1].casefold())
        names = tuple(relative for _, relative in records)
        bitmaps = tuple(
            relative for path, relative in records
            if path.suffix.lower() in BITMAP_EXTENSIONS)
        raws = tuple(
            relative for path, relative in records
            if path.suffix.lower() in RAW_EXTENSIONS)
        duplicate_kinds = sorted({
            path.suffix.lower()
            for path, _ in records
            if sum(
                other.suffix.lower() == path.suffix.lower()
                for other, _ in records) > 1
        })
        reasons = []
        if len(raws) > 1:
            reasons.append("multiple RAW companions")
        if duplicate_kinds:
            reasons.append(
                "duplicate file types: " + ", ".join(duplicate_kinds))
        directory = Path(names[0]).parent.as_posix()
        if directory == ".":
            directory = ""
        identity = f"{key[0]}\0{key[1]}"
        family = AssetFamily(
            asset_id="asset-" + hashlib.sha256(
                identity.encode()).hexdigest()[:16],
            directory=directory,
            stem=Path(names[0]).stem,
            files=names,
            bitmap_files=bitmaps,
            raw_files=raws,
            ambiguous=bool(reasons),
            ambiguity="; ".join(reasons),
        )
        families.append(family)
        for name in names:
            family_by_file[name] = family
    return AssetIndex(resolved, tuple(families), family_by_file)
