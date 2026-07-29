"""Secure provider profiles and immutable per-job Kimiya configuration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


PROVIDER_FORMAT = "opencull-provider-profiles-v1"
KINDS = {"openrouter", "openai", "ollama"}
AGENTS = ("A", "B", "C", "D")
VISION_AGENTS = {"A", "B", "D"}
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,199}$")


class ProviderError(ValueError):
    """Provider metadata, credentials, or connectivity are invalid."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_text(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=f".{path.name}.", suffix=".tmp", delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


class MacOSKeychain:
    """Minimal generic-password adapter; secret values are never returned publicly."""

    account = "opencull"
    prefix = "org.opencull.provider."

    def _service(self, profile_id: str) -> str:
        return self.prefix + profile_id

    def has(self, profile_id: str) -> bool:
        result = subprocess.run(
            [
                "security", "find-generic-password",
                "-a", self.account, "-s", self._service(profile_id),
            ],
            capture_output=True, text=True, check=False)
        return result.returncode == 0

    def get(self, profile_id: str) -> str:
        result = subprocess.run(
            [
                "security", "find-generic-password", "-w",
                "-a", self.account, "-s", self._service(profile_id),
            ],
            capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise ProviderError("provider credential is missing from macOS Keychain")
        secret = result.stdout.strip()
        if not secret:
            raise ProviderError("provider credential in macOS Keychain is empty")
        return secret

    def set(self, profile_id: str, secret: str) -> None:
        if not secret or len(secret) > 8192:
            raise ProviderError("credential must contain between 1 and 8192 characters")
        # `security -w value` exposes value in the process argument list.
        # Interactive mode accepts the command on stdin; -X avoids shell-like
        # quoting entirely while storing the exact UTF-8 password bytes.
        command = (
            "add-generic-password -U "
            f"-a {self.account} -s {self._service(profile_id)} "
            f"-X {secret.encode('utf-8').hex()}\n"
        )
        result = subprocess.run(
            ["security", "-i"],
            input=command,
            capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise ProviderError(
                f"could not save credential in macOS Keychain: "
                f"{result.stderr.strip()}")

    def delete(self, profile_id: str) -> None:
        result = subprocess.run(
            [
                "security", "delete-generic-password",
                "-a", self.account, "-s", self._service(profile_id),
            ],
            capture_output=True, text=True, check=False)
        if result.returncode not in {0, 44}:
            raise ProviderError(
                f"could not remove Keychain credential: {result.stderr.strip()}")


class ProviderStore:
    def __init__(
        self,
        path: Path,
        project_root: Path,
        keychain: Any | None = None,
        generated_root: Path | None = None,
    ):
        self.path = path.expanduser().resolve()
        self.project_root = project_root.expanduser().resolve()
        self.generated_root = (
            generated_root.expanduser().resolve() if generated_root
            else self.project_root / ".opencull-generated" / "providers")
        self.keychain = keychain or MacOSKeychain()
        self._lock = threading.RLock()
        self._state = self._load()

    def _empty(self) -> dict[str, Any]:
        return {
            "format": PROVIDER_FORMAT,
            "revision": 0,
            "created_at": _now(),
            "updated_at": _now(),
            "profiles": [],
        }

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderError(f"cannot read provider profiles: {exc}") from exc
        if not isinstance(data, dict) or data.get("format") != PROVIDER_FORMAT:
            raise ProviderError("provider profile file has an unsupported format")
        profiles = data.get("profiles")
        if not isinstance(profiles, list):
            raise ProviderError("provider profiles must be a list")
        data["profiles"] = [
            self._validate(profile, existing_id=profile.get("id"))
            for profile in profiles
        ]
        return data

    def _save(self) -> None:
        self._state["revision"] = int(self._state.get("revision", 0)) + 1
        self._state["updated_at"] = _now()
        _atomic_text(
            self.path,
            json.dumps(self._state, indent=2, ensure_ascii=False) + "\n")

    @staticmethod
    def _endpoint(kind: str, value: Any) -> str:
        if kind == "openrouter":
            return "https://openrouter.ai/api/v1"
        default = (
            "http://127.0.0.1:11434"
            if kind == "ollama" else ""
        )
        endpoint = str(value or default).strip().rstrip("/")
        parsed = urlparse(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username or parsed.password
            or parsed.query or parsed.fragment
        ):
            raise ProviderError(
                "endpoint must be an http(s) URL without credentials, query, or fragment")
        if kind == "openai" and not parsed.path.rstrip("/").endswith("/v1"):
            raise ProviderError("OpenAI-compatible endpoint must end in /v1")
        return endpoint

    def _validate(
        self, value: Any, existing_id: str | None = None
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ProviderError("provider profile must be an object")
        kind = str(value.get("kind", "")).strip().lower()
        if kind not in KINDS:
            raise ProviderError(f"unsupported provider kind: {kind!r}")
        name = str(value.get("name", "")).strip()
        if not name or len(name) > 100:
            raise ProviderError("profile name must contain 1 to 100 characters")
        models = value.get("models")
        if not isinstance(models, dict) or set(models) != set(AGENTS):
            raise ProviderError("models must define exactly agents A, B, C, and D")
        clean_models = {}
        for agent in AGENTS:
            model = str(models[agent]).strip()
            if not MODEL_RE.fullmatch(model):
                raise ProviderError(f"invalid model identifier for agent {agent}")
            clean_models[agent] = model
        profile_id = existing_id or uuid.uuid4().hex[:12]
        if not re.fullmatch(r"[a-f0-9]{12}", profile_id):
            raise ProviderError("provider profile ID is invalid")
        credential_required = kind == "openrouter" or bool(
            value.get("credential_required", False))
        return {
            "id": profile_id,
            "name": name,
            "kind": kind,
            "endpoint": self._endpoint(kind, value.get("endpoint")),
            "models": clean_models,
            "credential_required": credential_required,
            "zdr": bool(value.get("zdr", True)) if kind == "openrouter" else False,
            "cost_note": str(value.get("cost_note", ""))[:300],
            "created_at": str(value.get("created_at") or _now()),
            "updated_at": _now(),
        }

    def _profile(self, profile_id: str) -> dict[str, Any]:
        for profile in self._state["profiles"]:
            if profile["id"] == profile_id:
                return profile
        raise ProviderError("unknown provider profile")

    def public(self) -> dict[str, Any]:
        with self._lock:
            profiles = []
            for profile in self._state["profiles"]:
                public = deepcopy(profile)
                public["credential"] = (
                    "stored" if self.keychain.has(profile["id"]) else "missing")
                public["privacy"] = (
                    "local" if profile["kind"] == "ollama"
                    and urlparse(profile["endpoint"]).hostname
                    in {"127.0.0.1", "localhost"}
                    else "remote-zdr" if profile["kind"] == "openrouter"
                    and profile["zdr"]
                    else "remote-provider-policy"
                )
                profiles.append(public)
            return {
                "format": PROVIDER_FORMAT,
                "revision": self._state["revision"],
                "path": str(self.path),
                "profiles": profiles,
                "secrets_returned": False,
            }

    def save(
        self,
        value: dict[str, Any],
        expected_revision: Any,
        secret: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            try:
                expected = int(expected_revision)
            except (TypeError, ValueError) as exc:
                raise ProviderError("provider revision is invalid") from exc
            if expected != self._state["revision"]:
                raise ProviderError(
                    "provider profiles changed in another tab; reload before saving")
            requested_id = str(value.get("id", "")).strip() or None
            existing = None
            if requested_id:
                existing = self._profile(requested_id)
            validated = self._validate(
                {**(existing or {}), **value}, existing_id=requested_id)
            if any(
                profile["id"] != validated["id"]
                and profile["name"].casefold() == validated["name"].casefold()
                for profile in self._state["profiles"]
            ):
                raise ProviderError("provider profile names must be unique")
            if validated["credential_required"] and not (
                secret or self.keychain.has(validated["id"])
            ):
                raise ProviderError("this provider profile requires a credential")
            if secret:
                self.keychain.set(validated["id"], secret)
            if existing:
                index = self._state["profiles"].index(existing)
                validated["created_at"] = existing["created_at"]
                self._state["profiles"][index] = validated
            else:
                self._state["profiles"].append(validated)
            self._save()
            return self.public()

    def delete(
        self, profile_id: str, expected_revision: Any, remove_credential: bool
    ) -> dict[str, Any]:
        with self._lock:
            if int(expected_revision) != self._state["revision"]:
                raise ProviderError(
                    "provider profiles changed in another tab; reload before deleting")
            profile = self._profile(profile_id)
            self._state["profiles"].remove(profile)
            if remove_credential and self.keychain.has(profile_id):
                self.keychain.delete(profile_id)
            self._save()
            return self.public()

    def credential(self, profile_id: str) -> str:
        profile = self._profile(profile_id)
        if profile["credential_required"] or self.keychain.has(profile_id):
            return self.keychain.get(profile_id)
        return ""

    def test_connection(self, profile_id: str, timeout: float = 10) -> dict[str, Any]:
        profile = deepcopy(self._profile(profile_id))
        endpoint = profile["endpoint"]
        url = (
            endpoint + "/api/tags"
            if profile["kind"] == "ollama"
            else endpoint + "/models"
        )
        headers = {"Accept": "application/json"}
        credential = self.credential(profile_id)
        if credential:
            headers["Authorization"] = f"Bearer {credential}"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read(2_000_000))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise ProviderError(f"provider connection test failed: {exc}") from exc
        values = data.get("models" if profile["kind"] == "ollama" else "data", [])
        model_records = {
            str(item.get("name") or item.get("id")): item
            for item in values if isinstance(item, dict)
        }
        available = set(model_records)
        missing = [
            f"{agent}: {model}"
            for agent, model in profile["models"].items()
            if model not in available
        ]
        vision_status = {}
        for agent, model in profile["models"].items():
            if agent not in VISION_AGENTS:
                vision_status[agent] = "not-required"
                continue
            record = model_records.get(model, {})
            modalities = (
                record.get("architecture", {}).get("input_modalities", [])
                if isinstance(record.get("architecture"), dict) else []
            )
            if "image" in modalities:
                vision_status[agent] = "metadata-confirmed"
            elif profile["kind"] == "openrouter" and record:
                vision_status[agent] = "metadata-does-not-confirm-image"
            else:
                vision_status[agent] = "requires-live-multimodal-validation"
        return {
            "profile_id": profile_id,
            "reachable": True,
            "available_model_count": len(available),
            "configured_models_present": not missing,
            "missing_models": missing,
            "vision_declarations": {
                agent: agent in VISION_AGENTS for agent in AGENTS},
            "vision_capability": vision_status,
            "notice": (
                "Endpoint reachability and model IDs were checked. OpenRouter "
                "vision is checked from model metadata when available; generic "
                "OpenAI/Ollama capability requires a live multimodal request."
            ),
        }

    def materialize(self, job_id: str, profile_id: str) -> dict[str, Any]:
        with self._lock:
            profile = deepcopy(self._profile(profile_id))
        env_name = f"OPENCULL_PROVIDER_TOKEN_{profile_id.upper()}"
        lines = [
            "-- Generated by OpenCull Phase 7. Contains no credential value.",
            f"-- profile: {profile['name']} ({profile_id})",
            "",
        ]
        for agent in AGENTS:
            lines.extend([
                f"agent {agent}:",
                f'    backend = "{profile["kind"]}"',
                f'    model   = "{profile["models"][agent]}"',
            ])
            if profile["kind"] != "openrouter":
                lines.append(f'    url     = "{profile["endpoint"]}"')
            if profile["credential_required"]:
                lines.append(f'    key_env = "{env_name}"')
            if profile["kind"] == "openrouter":
                lines.append(
                    f"    zdr      = {'true' if profile['zdr'] else 'false'}")
            if agent in VISION_AGENTS:
                lines.append("    vision   = true")
            lines.append("")
        agents_text = "\n".join(lines)
        source = (self.project_root / "opencull.kim").read_text(encoding="utf-8")
        bundle = (self.generated_root / job_id).resolve()
        agents_path = bundle / "agents.kim"
        program_path = bundle / "opencull.kim"
        replacements = {
            'use "agents.kim"': f'use "{agents_path}"',
            'use python "scan.py"': (
                f'use python "{self.project_root / "scan.py"}"'),
            'use python "opencull_kernel.py"': (
                f'use python "{self.project_root / "opencull_kernel.py"}"'),
        }
        for original, generated in replacements.items():
            if original not in source:
                raise ProviderError(
                    f"cannot generate job program; missing declaration: {original}")
            source = source.replace(original, generated, 1)
        _atomic_text(agents_path, agents_text)
        _atomic_text(program_path, source)
        agents_sha = hashlib.sha256(agents_text.encode()).hexdigest()
        program_sha = hashlib.sha256(source.encode()).hexdigest()
        manifest = {
            "format": "opencull-generated-provider-bundle-v1",
            "job_id": job_id,
            "profile": profile,
            "agents_sha256": agents_sha,
            "program_sha256": program_sha,
            "agents_path": str(agents_path),
            "program_path": str(program_path),
            "credential_env": env_name if profile["credential_required"] else "",
            "contains_secret": False,
            "generated_at": _now(),
        }
        _atomic_text(
            bundle / "manifest.json",
            json.dumps(manifest, indent=2) + "\n")
        return manifest
