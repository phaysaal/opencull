"""Multimodal semantic verification for a photographic edit triplet.

This module deliberately keeps the transport separate from the judgment
schema.  Credentials are supplied by the caller (or ``OPENROUTER_API_KEY``);
no provider/API file is read here.  The returned certificate records hashes of
the evidence, never the image bytes.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FORMAT = "opencull-semantic-verification-v1"
DEFAULT_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
JUDGMENT_FIELDS = (
    "satisfactory", "confidence", "follows_suggestion", "preserves_subject",
    "preserves_composition", "photographic_quality", "concerns", "reasoning",
)


class SemanticVerificationError(ValueError):
    """Evidence, provider response, or judgment schema is invalid."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _image_data_url(path: Path) -> str:
    suffix = path.suffix.casefold()
    mime = "image/jpeg" if suffix in {".jpg", ".jpeg"} else "image/png"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def build_prompt(suggestion: str) -> str:
    return f"""You are a careful senior photo editor reviewing an edit semantically.
You are given the original camera JPEG, an optional original thumbnail, and the
newly developed image. The edit suggestion is authoritative artistic intent,
not a demand for pixel identity.

EDIT SUGGESTION:
{suggestion}

Judge whether the developed image is satisfactory for that suggestion. Check
whether the intended mood and tonal/color direction were followed, while the
subject, expression, important surroundings, composition, and believable
photographic quality were preserved. Treat an intentional creative change as
acceptable when it is consistent with the suggestion. Do not fail an image
merely because it differs from the camera JPEG's processing.

Return JSON only with exactly these keys:
{{
  "satisfactory": true or false,
  "confidence": 0.0 to 1.0,
  "follows_suggestion": 0.0 to 1.0,
  "preserves_subject": 0.0 to 1.0,
  "preserves_composition": 0.0 to 1.0,
  "photographic_quality": 0.0 to 1.0,
  "concerns": [short strings],
  "reasoning": "concise explanation"
}}
Use "satisfactory": false when a material concern remains. If uncertain,
lower confidence and explain the uncertainty rather than inventing detail."""


def _extract_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    text = str(value).strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SemanticVerificationError("provider did not return JSON judgment") from exc
    if not isinstance(parsed, dict):
        raise SemanticVerificationError("judgment JSON must be an object")
    return parsed


def normalize_judgment(value: Any) -> dict[str, Any]:
    raw = _extract_json(value)
    missing = [field for field in JUDGMENT_FIELDS if field not in raw]
    if missing:
        raise SemanticVerificationError(
            "judgment omitted required fields: " + ", ".join(missing))
    concerns = raw["concerns"]
    if isinstance(concerns, str):
        concerns = [concerns]
    result = {
        "satisfactory": raw["satisfactory"] if isinstance(raw["satisfactory"], bool)
        else None,
        "concerns": concerns if isinstance(concerns, list) else None,
        "reasoning": str(raw["reasoning"]).strip(),
    }
    if result["satisfactory"] is None or result["concerns"] is None or not result["reasoning"]:
        raise SemanticVerificationError("judgment contains invalid boolean/list/text fields")
    for field in JUDGMENT_FIELDS[1:6]:
        try:
            number = float(raw[field])
        except (TypeError, ValueError) as exc:
            raise SemanticVerificationError(f"invalid judgment score: {field}") from exc
        if not 0 <= number <= 1:
            raise SemanticVerificationError(f"judgment score out of range: {field}")
        result[field] = round(number, 4)
    result["concerns"] = [str(item).strip() for item in result["concerns"] if str(item).strip()]
    return result


def _response_text(payload: dict[str, Any]) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise SemanticVerificationError("provider response has no message content") from exc
    if isinstance(content, list):
        content = "".join(str(part.get("text", "")) for part in content
                           if isinstance(part, dict))
    return str(content)


def _http_chat(endpoint: str, api_key: str, payload: dict[str, Any]) -> str:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        endpoint, data=body, method="POST",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json",
                 "HTTP-Referer": "https://opencull.local",
                 "X-Title": "OpenCull semantic verification"})
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            value = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # network and provider errors are one safe boundary
        raise SemanticVerificationError(f"semantic provider request failed: {exc}") from exc
    return _response_text(value)


def verify_triplet(
    original: Path,
    developed: Path,
    suggestion: str,
    thumbnail: Path | None = None,
    *, model: str = "openai/gpt-5.6-luna-pro",
    endpoint: str = DEFAULT_ENDPOINT,
    api_key: str | None = None,
    transport: Callable[[str, str, dict[str, Any]], str] | None = None,
) -> dict[str, Any]:
    original = original.expanduser().resolve()
    developed = developed.expanduser().resolve()
    thumbnail = thumbnail.expanduser().resolve() if thumbnail else None
    for path in (original, developed):
        if not path.is_file():
            raise SemanticVerificationError(f"evidence image is unavailable: {path}")
    if thumbnail and not thumbnail.is_file():
        raise SemanticVerificationError(f"thumbnail is unavailable: {thumbnail}")
    if not str(suggestion).strip():
        raise SemanticVerificationError("edit suggestion is empty")
    images = [{"type": "text", "text": build_prompt(suggestion)}]
    for label, path in (("ORIGINAL JPEG", original), ("ORIGINAL THUMBNAIL", thumbnail),
                        ("DEVELOPED IMAGE", developed)):
        if path is None:
            continue
        images.append({"type": "text", "text": label})
        images.append({"type": "image_url", "image_url": {"url": _image_data_url(path)}})
    payload = {"model": model, "temperature": 0, "response_format": {"type": "json_object"},
               "messages": [{"role": "user", "content": images}]}
    key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
    if transport is None:
        if not key:
            raise SemanticVerificationError("no provider credential supplied")
        transport = _http_chat
    response = transport(endpoint, key, payload)
    judgment = normalize_judgment(response)
    evidence = {"original": {"path": str(original), "sha256": _sha256(original)},
                "developed": {"path": str(developed), "sha256": _sha256(developed)}}
    if thumbnail:
        evidence["thumbnail"] = {"path": str(thumbnail), "sha256": _sha256(thumbnail)}
    return {"format": FORMAT, "created_at": datetime.now(UTC).isoformat(),
            "model": model, "endpoint": endpoint, "evidence": evidence,
            "suggestion": suggestion, "judgment": judgment}


def verify_consensus(
    original: Path,
    developed: Path,
    suggestion: str,
    thumbnail: Path | None = None,
    *, models: list[str] | tuple[str, ...],
    endpoint: str = DEFAULT_ENDPOINT,
    api_key: str | None = None,
    transport: Callable[[str, str, dict[str, Any]], str] | None = None,
) -> dict[str, Any]:
    """Verify one edit with several models and return an auditable consensus.

    Every model receives the same triplet and prompt.  A strict majority of
    ``satisfactory`` votes is required; numeric scores are averaged and
    concerns are de-duplicated in first-seen order.  Individual certificates
    are retained so disagreement remains inspectable rather than hidden.
    """
    selected = [str(model).strip() for model in models if str(model).strip()]
    if not selected:
        raise SemanticVerificationError("at least one verification model is required")
    certificates = [verify_triplet(
        original, developed, suggestion, thumbnail, model=model,
        endpoint=endpoint, api_key=api_key, transport=transport)
        for model in selected
    ]
    judgments = [certificate["judgment"] for certificate in certificates]
    votes = sum(1 for judgment in judgments if judgment["satisfactory"])
    numeric = ("confidence", "follows_suggestion", "preserves_subject",
               "preserves_composition", "photographic_quality")
    aggregate = {
        "satisfactory": votes * 2 > len(judgments),
        "votes_satisfactory": votes,
        "votes_total": len(judgments),
        **{field: round(sum(float(judgment[field]) for judgment in judgments) /
                        len(judgments), 4) for field in numeric},
    }
    concerns: list[str] = []
    for judgment in judgments:
        for concern in judgment["concerns"]:
            if concern not in concerns:
                concerns.append(concern)
    aggregate["concerns"] = concerns
    aggregate["reasoning"] = (
        f"Consensus from {len(judgments)} model(s): {votes} satisfactory vote(s). "
        "Individual judgments are retained for review."
    )
    first = certificates[0]
    return {"format": FORMAT, "created_at": datetime.now(UTC).isoformat(),
            "model": "consensus", "models": selected, "endpoint": endpoint,
            "evidence": first["evidence"], "suggestion": suggestion,
            "judgment": aggregate, "judgments": judgments}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("original", type=Path)
    parser.add_argument("developed", type=Path)
    parser.add_argument("suggestion")
    parser.add_argument("--thumbnail", type=Path)
    parser.add_argument("--model", default="openai/gpt-5.6-luna-pro")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = verify_triplet(args.original, args.developed, args.suggestion,
                            args.thumbnail, model=args.model, endpoint=args.endpoint)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["judgment"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
