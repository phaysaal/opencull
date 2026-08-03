"""Kimiya-facing helpers for semantic edit verification."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from semantic_verifier import build_prompt, normalize_judgment


FORMAT = "opencull-semantic-verification-request-v1"


def _file(path: str) -> Path:
    value = Path(str(path)).expanduser().resolve()
    if not value.is_file():
        raise ValueError(f"evidence image is unavailable: {value}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_verification_request(
    original: str, developed: str, suggestion: str, thumbnail: str = ""
) -> str:
    if not str(suggestion).strip():
        raise ValueError("edit suggestion is empty")
    paths = {"original": _file(original), "developed": _file(developed)}
    if str(thumbnail).strip():
        paths["thumbnail"] = _file(thumbnail)
    evidence = {
        key: {"path": str(path), "sha256": _sha256(path)}
        for key, path in paths.items()
    }
    value = {"format": FORMAT, "suggestion": str(suggestion).strip(),
             "evidence": evidence}
    value["signature"] = hashlib.sha256(
        json.dumps(value, sort_keys=True).encode()).hexdigest()
    return json.dumps(value, indent=2, sort_keys=True)


def verification_prompt(request: str) -> str:
    value = json.loads(request)
    return build_prompt(value["suggestion"])


def normalize_verification(value: Any) -> dict[str, Any]:
    return normalize_judgment(value)


def valid_verification(value: Any) -> bool:
    try:
        normalized = normalize_judgment(value)
    except (TypeError, ValueError):
        return False
    return bool(normalized["reasoning"])


def verification_json(request: str, judgment: Any, agent: str = "A") -> str:
    value = json.loads(request)
    normalized = normalize_judgment(judgment)
    return json.dumps({"format": "opencull-semantic-verification-v1",
                       "request_signature": value["signature"],
                       "evidence": value["evidence"], "suggestion": value["suggestion"],
                       "agent": agent, "judgment": normalized},
                      indent=2, sort_keys=True)


def consensus_judgment(values: Any) -> dict[str, Any]:
    """Aggregate normalized judgments using a strict satisfactory majority."""
    judgments = [normalize_judgment(value) for value in values]
    if not judgments:
        raise ValueError("at least one judgment is required")
    votes = sum(1 for item in judgments if item["satisfactory"])
    numeric = ("confidence", "follows_suggestion", "preserves_subject",
               "preserves_composition", "photographic_quality")
    result = {"satisfactory": votes * 2 > len(judgments),
              "votes_satisfactory": votes, "votes_total": len(judgments),
              **{field: round(sum(item[field] for item in judgments) / len(judgments), 4)
                 for field in numeric}}
    result["concerns"] = list(dict.fromkeys(
        concern for item in judgments for concern in item["concerns"]))
    result["reasoning"] = (f"Consensus from {len(judgments)} model(s): "
                           f"{votes} satisfactory vote(s).")
    return result


def consensus_verification_json(request: str, judgment: Any, judgments: Any,
                                models: Any) -> str:
    value = json.loads(request)
    normalized = normalize_judgment(judgment)
    individual = [normalize_judgment(item) for item in judgments]
    return json.dumps({"format": "opencull-semantic-verification-v1",
                       "request_signature": value["signature"],
                       "evidence": value["evidence"], "suggestion": value["suggestion"],
                       "agent": "consensus", "models": list(models),
                       "judgments": individual, "judgment": normalized},
                      indent=2, sort_keys=True)
