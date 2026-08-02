"""Versioned, updateable semantic personal-editing-style profiles."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


FORMAT = "opencull-personal-style-profile-v1"
STYLE_FIELDS = (
    "profile_name", "visual_signature", "tonal_preferences", "contrast_preferences",
    "color_preferences", "white_balance_preferences", "saturation_preferences",
    "highlight_shadow_preferences", "texture_detail_preferences",
    "composition_preferences", "subject_and_skin_preferences",
    "scene_adaptation", "preferred_adjustments", "avoid_or_guardrails",
    "professional_refinement", "confidence",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _images(root: Path, limit: int = 64) -> list[Path]:
    suffixes = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
    return sorted((p for p in root.rglob("*") if p.is_file() and p.suffix.casefold() in suffixes),
                  key=lambda p: str(p).casefold())[:limit]


def _selected_images(source: Path, limit: int = 64) -> list[Path]:
    """Resolve either a traditional example folder or a file-selection manifest."""
    if source.is_dir():
        return _images(source, limit)
    if source.is_file() and source.suffix.casefold() == ".json":
        try:
            value = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"style example manifest is unreadable: {source}") from exc
        paths = value.get("files", []) if isinstance(value, dict) else value
        if not isinstance(paths, list):
            raise ValueError("style example manifest files must be a list")
        suffixes = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
        return sorted(
            (Path(str(path)).expanduser().resolve() for path in paths
             if Path(str(path)).expanduser().is_file()
             and Path(str(path)).suffix.casefold() in suffixes),
            key=lambda path: str(path).casefold())[:limit]
    raise ValueError(f"style example source is unavailable: {source}")


def build_style_request(photos: str, existing: str = "", limit: int | float = 64) -> str:
    root = Path(photos).expanduser().resolve()
    try:
        normalized_limit = int(limit)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("style example limit must be a positive integer") from exc
    if normalized_limit < 1 or float(limit) != normalized_limit:
        raise ValueError("style example limit must be a positive integer")
    files = _selected_images(root, normalized_limit)
    if not files:
        raise ValueError("style example source contains no supported images")
    prior = None
    if str(existing).strip():
        path = Path(existing).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"existing style profile is unavailable: {path}")
        prior = json.loads(path.read_text(encoding="utf-8"))
        if prior.get("format") != FORMAT:
            raise ValueError("unsupported personal style profile")
    value = {"format": "opencull-style-profile-request-v1", "photos_root": str(root),
             "examples": [{"path": str(p), "sha256": _sha256(p)} for p in files],
             "existing_profile": prior}
    value["signature"] = hashlib.sha256(
        json.dumps(value, sort_keys=True).encode()).hexdigest()
    return json.dumps(value, indent=2, sort_keys=True)


def style_profile_prompt(request: str, mode: str = "update") -> str:
    prior = json.loads(request).get("existing_profile")
    return f"""You are a photographic colorist extracting an updateable personal
editing-style profile from the supplied finished photographs. Describe repeated
choices, not incidental scene content. Be exhaustive and concrete: tonal curve,
black point, highlight behavior, contrast and microcontrast, saturation by hue,
warm/cool balance, color separation, skin treatment, texture/sharpening,
composition/crop tendencies, and how the style adapts across daylight, people,
architecture, landscape, and low light. Separate intentional signature from
technical errors. Recommend professional guardrails that preserve detail and
natural subjects without erasing the author's character.

Update mode: {mode}. Existing profile (preserve stable preferences unless the
new examples provide strong evidence of change): {json.dumps(prior or {})}

Return JSON only with these keys: {', '.join(STYLE_FIELDS)}. Every field except
confidence is a concise but semantically rich string; confidence is 0 to 1.
    """


def style_example_paths(request: str) -> list[str]:
    value = json.loads(request)
    return [str(item["path"]) for item in value.get("examples", [])]


def normalize_style_profile(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("style profile must be an object")
    result = {field: str(value.get(field, "")).strip() for field in STYLE_FIELDS
              if field != "confidence"}
    try:
        result["confidence"] = round(float(value.get("confidence", 0)), 4)
    except (TypeError, ValueError) as exc:
        raise ValueError("style profile confidence is invalid") from exc
    if not all(result[field] for field in STYLE_FIELDS if field != "confidence"):
        raise ValueError("style profile contains empty semantic fields")
    if not 0 <= result["confidence"] <= 1:
        raise ValueError("style profile confidence is out of range")
    return result


def style_profile_json(request: str, value: Any, mode: str = "update") -> str:
    request_value = json.loads(request)
    profile = normalize_style_profile(value)
    now = datetime.now(timezone.utc).isoformat()
    prior = request_value.get("existing_profile") or {}
    history = list(prior.get("history", [])) if isinstance(prior, dict) else []
    history.append({"updated_at": now, "mode": mode,
                    "request_signature": request_value["signature"],
                    "example_count": len(request_value["examples"]),
                    "profile": profile})
    return json.dumps({"format": FORMAT, "revision": int(prior.get("revision", 0)) + 1,
                       "updated_at": now, "mode": mode,
                       "request_signature": request_value["signature"],
                       "examples": request_value["examples"], "history": history,
                       "profile": profile}, indent=2, sort_keys=True)


def valid_style_profile(value: Any) -> bool:
    try:
        normalize_style_profile(value.get("profile", value) if isinstance(value, dict) else value)
        return True
    except ValueError:
        return False
