"""Persistent sequential culling queue with supervised Kimiya subprocesses."""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .project import (
    ensure_project_layout,
    load_or_create_folder_project,
    load_project,
    register_file_artifact,
    register_job_output,
)
from .providers import (
    DEFAULT_JUDGMENT_POLICY,
    ProviderError,
    ProviderStore,
    normalize_judgment_policy,
)
from .shortlist import bar_checkpoint_path, readable_checkpoint

QUEUE_FORMAT = "opencull-job-queue-v1"
ACTIVE = {"running", "stopping", "detached"}
TERMINAL = {"completed", "failed", "cancelled"}
PROFILES = {"family", "professional", "balanced"}


def kimiya_arguments(job: dict[str, Any]) -> tuple[str, list[str]]:
    """Which program a job runs, and what it is told.

    Two callers build this command: the manager here, which runs the
    interpreter directly, and the packaged desktop application, which
    re-enters its own executable as a worker. They differ only in how
    they start Python, so only that stays separate -- the arguments are
    settled once. They had drifted, and the drift was silent: an album
    marked infrared with a photographer's note about a solar eclipse
    passed both to the job record and neither to the program, because
    the desktop's copy of this list had never heard of them.
    """
    kind = job.get("kind")
    if kind == "style_profile":
        return "style_profile.kim", [
            f"photos={job['photos']}", f"output={job['output']}",
            f"existing={job.get('existing', '')}",
            f"mode={job.get('mode', 'update')}",
            f"limit={job.get('limit', 64)}",
        ]
    if kind == "semantic_verification":
        return "semantic_verification.kim", [
            f"original={job['original']}",
            f"developed={job['developed']}",
            f"thumbnail={job.get('thumbnail', '')}",
            f"suggestion={job['suggestion']}",
            f"consensus={'true' if job.get('consensus') else 'false'}",
            f"output={job['output']}",
        ]
    if kind == "edit_suggestions":
        return "edit_suggestions.kim", [
            f"shortlist={job['shortlist']}",
            f"review={job['review']}",
            f"photos={job['photos']}",
            f"output={job['output']}",
            f"profile={job['profile']}",
            f"style_profile={job.get('style_profile', '')}",
            f"style_profiles={json.dumps(job.get('style_profiles', []))}",
            f"only_photo={job.get('only_photo', '')}",
            f"only_photos={json.dumps(job.get('only_photos', []))}",
            f"spectrum={job.get('spectrum', 'visible')}",
            f"cutoff_nm={job.get('cutoff_nm', 0)}",
            f"about={job.get('about', '')}",
            "resume=true",
        ]
    if kind == "professional_shortlist":
        return "professional_shortlist.kim", [
            f"report={job['report']}",
            f"photos={job['photos']}",
            f"review={job.get('review', '')}",
            f"output={job['output']}",
            f"policy={job['policy']}",
            f"profile={job['profile']}",
            f"spectrum={job.get('spectrum', 'visible')}",
            f"cutoff_nm={job.get('cutoff_nm', 0)}",
            f"about={job.get('about', '')}",
            "resume=true",
        ]
    if kind == "kimiya_program":
        return job["program"], [
            f"{key}={value}"
            for key, value in (job.get("parameters") or {}).items()]
    if kind == "control_zones":
        return "control_zones.kim", [
            f"photos={job['photos']}",
            f"photo={job['photo']}",
            f"output={job['output']}",
            f"spectrum={job.get('spectrum', 'visible')}",
            f"cutoff_nm={job.get('cutoff_nm', 0)}",
            f"about={job.get('about', '')}",
        ]
    if kind == "treatment":
        return "protect_then_reveal.kim", [
            f"photos={job['photos']}",
            f"photo={job['photo']}",
            f"output={job['output']}",
            f"spectrum={job.get('spectrum', 'visible')}",
            f"cutoff_nm={job.get('cutoff_nm', 0)}",
            f"about={job.get('about', '')}",
            f"rounds={job.get('rounds', 3)}",
        ]
    if kind not in {None, "culling"}:
        raise ValueError(f"unsupported kimiya job kind: {kind}")
    return "opencull.kim", [
        f"photos={job['photos']}", f"output={job['output']}",
        f"keep_per_group={job['keep_per_group']}",
        f"recursive={'true' if job['recursive'] else 'false'}",
        f"profile={job['profile']}",
        f"only_photos={json.dumps(job.get('only_photos') or [])}",
        "resume=true",
    ]


class JobError(ValueError):
    """A requested queue transition or path is invalid."""


def _photo_inside(source: Path, photo: str) -> str:
    """The photograph's path within its folder, subfolders honoured.

    A photograph may live below the folder's root -- the Superimpose
    stack does, under .darkimiya -- so a relative path is kept whole
    rather than cut to its last name, and only escaping the folder is
    refused.
    """
    asked = str(photo).strip()
    name = asked if "/" in asked or "\\" in asked else Path(asked).name
    inside = source / name
    try:
        inside.resolve().relative_to(source)
    except ValueError:
        raise JobError(
            f"photograph escapes the folder: {photo!r}") from None
    if not name or not inside.is_file():
        raise JobError(f"no such photograph in {source}: {name or photo!r}")
    return name


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=f".{path.name}.", suffix=".tmp", delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=f".{path.name}.", suffix=".tmp", delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _pid_alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _pid_runs_job(pid: Any, job_id: str) -> bool:
    """Whether a pid provably belongs to one job's own worker.

    A recorded pid can be recycled by an innocent process after a
    restart, so liveness alone never justifies a signal. The worker's
    command line names the job's generated program -- which carries the
    job id -- and that is the proof. Where /proc is unavailable the
    answer is no, and the caller declines to signal.
    """
    try:
        cmdline = (
            Path(f"/proc/{int(pid)}/cmdline")
            .read_bytes().decode("utf-8", errors="replace"))
    except (OSError, TypeError, ValueError):
        return False
    return bool(job_id) and job_id in cmdline


class JobManager:
    """Runs at most one culling job and persists all queue transitions."""

    def __init__(
        self,
        state_path: Path,
        project_root: Path,
        python: str | None = None,
        command_builder: Callable[[dict[str, Any]], list[str]] | None = None,
        autostart: bool = True,
        providers: ProviderStore | None = None,
        program_checker: Callable[[Path], None] | None = None,
        kimiya_workspace_root: Path | None = None,
        output_root: Path | None = None,
    ):
        self.state_path = state_path.expanduser().resolve()
        self.project_root = project_root.expanduser().resolve()
        self.python = python or sys.executable
        self.command_builder = command_builder or self._command
        self.providers = providers
        self.program_checker = program_checker or self._check_program
        self.kimiya_workspace_root = (
            kimiya_workspace_root.expanduser().resolve()
            if kimiya_workspace_root else None)
        self.output_root = (
            output_root.expanduser().resolve()
            if output_root else self.project_root)
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._process: subprocess.Popen[str] | None = None
        self._active_job_id: str | None = None
        self._state = self._load()
        self._recover()
        self._thread = threading.Thread(
            target=self._worker, name="opencull-culling-queue", daemon=True)
        if autostart:
            self._thread.start()

    def _empty(self) -> dict[str, Any]:
        return {
            "format": QUEUE_FORMAT,
            "revision": 0,
            "created_at": _now(),
            "updated_at": _now(),
            "jobs": [],
        }

    def _load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self._empty()
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise JobError(f"cannot read job queue: {exc}") from exc
        if not isinstance(data, dict) or data.get("format") != QUEUE_FORMAT:
            raise JobError("job queue has an unsupported format")
        jobs = data.get("jobs")
        if not isinstance(jobs, list):
            raise JobError("job queue jobs must be a list")
        seen: set[str] = set()
        for job in jobs:
            if not isinstance(job, dict) or not isinstance(job.get("id"), str):
                raise JobError("job queue contains an invalid job")
            if job["id"] in seen:
                raise JobError("job queue contains duplicate IDs")
            seen.add(job["id"])
        return data

    def _save(self) -> None:
        self._state["revision"] = int(self._state.get("revision", 0)) + 1
        self._state["updated_at"] = _now()
        _atomic_json(self.state_path, self._state)

    def _register_completed_output(self, job: dict[str, Any]) -> None:
        """Best-effort project registration for new and recovered queue jobs."""
        requested = str(job.get("project") or "").strip()
        if not requested or not Path(str(job.get("output", ""))).is_file():
            return
        try:
            if job.get("kind") == "delivery_export":
                receipt = json.loads(
                    Path(str(job["output"])).read_text(encoding="utf-8"))
                job["export_destination"] = str(receipt.get("destination", ""))
                job["export_sha256"] = str(receipt.get("sha256", ""))
            project = register_job_output(Path(requested), job)
            # Every style the run could speak in is recorded as an artifact
            # of the shoot. None of them becomes "the" style: which one a
            # photograph used is written on that photograph's own entry, and
            # a folder-wide pin would silently steer the next run.
            offered = [
                str(job.get("style_profile") or "").strip(),
                *(str(item) for item in (job.get("style_profiles") or [])),
            ]
            for style_profile in dict.fromkeys(filter(None, offered)):
                if Path(style_profile).is_file():
                    project = register_file_artifact(
                        Path(requested), "style_profile", Path(style_profile),
                        stage="style")
            job["project_id"] = project.get("id")
            job.pop("project_registration_error", None)
        except (OSError, ValueError, KeyError) as exc:
            job["project_registration_error"] = str(exc)

    @staticmethod
    def _inside_app_bundle(path: Path) -> bool:
        return any(part.lower().endswith(".app") for part in path.parts)

    def _migrate_legacy_bundle_report(self, job: dict[str, Any]) -> bool:
        """Recover reports an older release wrote inside its replaceable bundle."""
        if job.get("kind") == "professional_shortlist":
            return False
        old_output = Path(str(job.get("output", ""))).expanduser().resolve()
        if not self._inside_app_bundle(old_output):
            return False
        report: Any = None
        committed_text: str | None = None
        if old_output.is_file():
            try:
                report = json.loads(old_output.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                report = None
        if report is None and self.kimiya_workspace_root is not None:
            certificate = (
                self.kimiya_workspace_root / job["id"] / "certificate.json")
            try:
                certified = json.loads(certificate.read_text(encoding="utf-8"))
                if certified.get("status") == "COMMITTED":
                    committed_text = certified.get("value")
                    report = json.loads(committed_text or "")
            except (OSError, json.JSONDecodeError, TypeError):
                report = None
        if not isinstance(report, dict) or report.get("format") != "opencull-report-v2":
            return False
        target = (self.output_root / old_output.name).resolve()
        if not target.is_file():
            if committed_text is not None:
                _atomic_text(target, committed_text)
            else:
                _atomic_json(target, report)
        old_text = str(old_output)
        job.update({
            "legacy_output": old_text,
            "output": str(target),
            "checkpoint": f"{target}.checkpoint.json",
            "log": f"{target}.log",
            "message": (
                "Culling report recovered from the replaceable app bundle "
                "into Darkimiya project results."
            ),
        })
        for dependent in self._state["jobs"]:
            dependent_report = dependent.get("report")
            if (
                isinstance(dependent_report, str)
                and Path(dependent_report).expanduser().resolve() == old_output
            ):
                dependent["report"] = str(target)
        return True

    def _repair_recovered_report_identity(self, job: dict[str, Any]) -> bool:
        """Restore exact certified bytes after an older recovery reformatted JSON."""
        if (
            job.get("kind") == "professional_shortlist"
            or not job.get("legacy_output")
            or self.kimiya_workspace_root is None
        ):
            return False
        target = Path(str(job.get("output", ""))).expanduser().resolve()
        certificate = (
            self.kimiya_workspace_root / job["id"] / "certificate.json")
        try:
            certified = json.loads(certificate.read_text(encoding="utf-8"))
            committed_text = certified.get("value")
            committed_report = json.loads(committed_text)
            current_text = target.read_text(encoding="utf-8")
            current_report = json.loads(current_text)
        except (OSError, json.JSONDecodeError, TypeError):
            return False
        if (
            certified.get("status") != "COMMITTED"
            or not isinstance(committed_text, str)
            or committed_report.get("format") != "opencull-report-v2"
            or current_report != committed_report
            or current_text == committed_text
        ):
            return False
        _atomic_text(target, committed_text)
        job["message"] = (
            "Recovered report identity verified against its committed "
            "Kimiya certificate."
        )
        return True

    def _recover(self) -> None:
        changed = False
        for job in self._state["jobs"]:
            output = Path(str(job.get("output") or "")).expanduser()
            if (
                job.get("kind") == "style_profile"
                and job.get("status") == "queued"
                and output.is_absolute()
                and output.parent == Path("/")
                and not output.exists()
            ):
                migrated = self.output_root / output.name
                job.update(
                    output=str(migrated), checkpoint=str(migrated),
                    log=f"{migrated}.log",
                    message="Waiting for personal-style extraction worker.")
                changed = True
            changed = self._migrate_legacy_bundle_report(job) or changed
            changed = self._repair_recovered_report_identity(job) or changed
            if job.get("status") == "completed":
                before = (
                    job.get("project_id"),
                    job.get("project_registration_error"),
                )
                self._register_completed_output(job)
                changed = changed or before != (
                    job.get("project_id"),
                    job.get("project_registration_error"),
                )
        for job in self._state["jobs"]:
            if job.get("status") in ACTIVE:
                # A live process outranks a file on disk. Some jobs write
                # to the same output path every run -- the timelapse's
                # report -- so a leftover from the LAST run is on disk the
                # whole time this one renders; read as completion, it
                # marked a working run finished at startup and freed the
                # queue to start a second one beside it. Only a dead pid
                # makes the file the evidence.
                if _pid_alive(job.get("pid")):
                    job.update(
                        status="detached",
                        message=(
                            "The run survived the GUI restart; "
                            "monitoring without launching another job."
                        ),
                    )
                elif Path(job["output"]).is_file():
                    # The message is what the photographer reads, and
                    # leaving the old one there says "is running" beside a
                    # job that finished. Registering the output here too:
                    # the loop above has already passed, so a run that
                    # completed unwatched would otherwise wait for the
                    # next launch to reach the project catalogue.
                    job.update(
                        status="completed", finished_at=_now(), pid=None,
                        message="Completed while the application was "
                                "not running.")
                    self._register_completed_output(job)
                else:
                    job.update(
                        status="paused", pid=None,
                        message="GUI restarted; resume from the validated checkpoint.",
                    )
                changed = True
        if changed:
            self._save()

    def resolve_report_path(self, path: Path) -> Path:
        """Resolve a saved pre-migration report path to its durable location."""
        requested = path.expanduser().resolve()
        if requested.is_file():
            return requested
        with self._lock:
            for job in self._state["jobs"]:
                if job.get("legacy_output") == str(requested):
                    recovered = Path(job["output"])
                    if recovered.is_file():
                        return recovered
        return requested

    def _command(self, job: dict[str, Any]) -> list[str]:
        if job.get("kind") == "delivery_export":
            command = [
                self.python, str(self.project_root / "delivery_export_pipeline.py"),
                "--source", job["source"], "--reference", job["reference"],
                "--directions", job["directions"], "--photo", job["photo"],
                "--style", job["style"], "--engine", job["engine"],
                "--demosaic", job["demosaic"],
                "--render-output-dir", job["render_output_dir"],
                "--project", job["project"],
                "--destination", job["destination"],
                "--export-key", job["export_key"],
                "--receipt", job["output"],
            ]
            if job.get("render"):
                command.extend(["--render", job["render"]])
            return command
        if job.get("kind") == "renderer_export":
            return [
                self.python, str(self.project_root / "renderer_export_pipeline.py"),
                "--source", job["source"], "--reference", job["reference"],
                "--directions", job["directions"], "--photo", job["photo"],
                "--style", job["style"], "--output-dir", job["output_dir"],
                "--project", job["project"], "--demosaic", job["demosaic"],
            ]
        if job.get("kind") == "renderer_comparison":
            return [
                self.python, str(self.project_root / "comparison_pipeline.py"),
                "--source", job["source"], "--reference", job["reference"],
                "--directions", job["directions"], "--photo", job["photo"],
                "--style", job["style"], "--opencull-render", job["opencull_render"],
                "--output-dir", job["output_dir"], "--project", job["project"],
                "--demosaic", job["demosaic"],
            ]
        if job.get("kind") == "development_pipeline":
            return [
                self.python, str(self.project_root / "development_pipeline.py"),
                "--source", job["source"], "--reference", job["reference"],
                "--directions", job["directions"], "--photo", job["photo"],
                "--style", job["style"], "--output-dir", job["output_dir"],
                "--project", job["project"],
            ]
        if job.get("kind") == "development_render":
            command = [self.python, str(self.project_root / "development_engine.py"),
                       job["baseline"], job["recipe"], "--output-dir", job["output_dir"],
                       "--project", job["project"]]
            if job.get("reference_jpeg"):
                command.extend(["--reference-jpeg", job["reference_jpeg"]])
            if job.get("adjustments"):
                command.extend(["--adjustments", job["adjustments"]])
            if job.get("allow_incomplete"):
                command.append("--allow-incomplete")
            return command
        program, arguments = kimiya_arguments(job)
        return [
            self.python, "-m", "kimiya", "run",
            job.get("program_path") or str(self.project_root / program),
            *arguments,
        ]

    def _job(self, job_id: str) -> dict[str, Any]:
        for job in self._state["jobs"]:
            if job["id"] == job_id:
                return job
        raise JobError("unknown culling job")

    @staticmethod
    def _validate_output(path: Path) -> Path:
        path = path.expanduser().resolve()
        if path.suffix.lower() != ".json":
            raise JobError("output must be a .json file")
        if path.exists():
            raise JobError("output already exists; choose it only through retry/resume")
        if not path.parent.is_dir():
            raise JobError("output parent directory does not exist")
        return path

    def add(
        self,
        photos: str,
        output: str = "",
        keep_per_group: Any = 2,
        recursive: Any = False,
        profile: str = "family",
        provider_profile_id: str = "",
        judge_panel: Any = None,
        judge_votes: Any = 5,
        judge_required: Any = 4,
        only_photos: list[str] | None = None,
    ) -> dict[str, Any]:
        source = Path(str(photos)).expanduser().resolve()
        if not source.is_dir():
            raise JobError(f"photo folder is not a directory: {source}")
        try:
            keep = int(keep_per_group)
        except (TypeError, ValueError) as exc:
            raise JobError("maximum keepers must be an integer") from exc
        if keep < 1 or keep > 20:
            raise JobError("maximum keepers must be between 1 and 20")
        if profile not in PROFILES:
            raise JobError(f"unsupported culling profile: {profile}")
        try:
            judgment_policy = normalize_judgment_policy(
                judge_panel, judge_votes, judge_required)
        except ProviderError as exc:
            raise JobError(str(exc)) from exc
        if (
            judgment_policy != DEFAULT_JUDGMENT_POLICY
            and not provider_profile_id
        ):
            raise JobError(
                "choose a provider profile before using a custom judgment policy")
        try:
            project_path, project = load_or_create_folder_project(
                source, source.name)
            layout = ensure_project_layout(source)
        except (OSError, ValueError) as exc:
            raise JobError(
                f"cannot create the Darkimiya project in {source}: {exc}") from exc
        chosen_output = (
            self._validate_output(Path(output))
            if str(output).strip()
            else self._default_output(source, layout["Reports"])
        )
        with self._lock:
            blocking = next(
                (job for job in self._state["jobs"]
                 if str(job.get("photos") or "").strip()
                 and Path(str(job["photos"])) == source
                 and job.get("status") not in TERMINAL), None)
            if blocking is not None:
                if blocking.get("status") == "paused":
                    raise JobError(
                        "This folder's cull is paused mid-run. Resume it "
                        "instead of starting another; the checkpoint keeps "
                        "everything already decided.")
                raise JobError(
                    "This folder is already being culled. Progress shows "
                    "on the folder card and in the queue.")
            job_id = uuid.uuid4().hex[:12]
            provider_bundle = None
            if provider_profile_id:
                if self.providers is None:
                    raise JobError("provider profiles are disabled")
                try:
                    provider_bundle = self.providers.materialize(
                        job_id, provider_profile_id,
                        judgment_policy=judgment_policy)
                    self.program_checker(Path(provider_bundle["program_path"]))
                except (ProviderError, JobError) as exc:
                    raise JobError(str(exc)) from exc
            log = chosen_output.with_suffix(chosen_output.suffix + ".log")
            checkpoint = Path(str(chosen_output) + ".checkpoint.json")
            job = {
                "id": job_id,
                "kind": "culling",
                "project": str(project_path),
                "project_id": project["id"],
                "photos": str(source),
                "output": str(chosen_output),
                "checkpoint": str(checkpoint),
                "log": str(log),
                "keep_per_group": keep,
                "recursive": bool(recursive),
                "profile": profile,
                "judgment_policy": judgment_policy,
                # The photographer's prefilter. Empty means the whole folder,
                # and the command line then stays what it always was.
                "only_photos": [
                    str(item).strip() for item in (only_photos or [])
                    if str(item).strip()
                ],
                "status": "queued",
                "message": "Waiting for the culling worker.",
                "pid": None,
                "created_at": _now(),
                "started_at": None,
                "finished_at": None,
                "exit_code": None,
                "provider_profile_id": provider_profile_id or None,
                "provider_profile_name": (
                    provider_bundle["profile"]["name"]
                    if provider_bundle else "Legacy agents.kim"
                ),
                "provider_kind": (
                    provider_bundle["profile"]["kind"]
                    if provider_bundle else "legacy"
                ),
                "provider_privacy": (
                    "local" if provider_bundle
                    and provider_bundle["profile"]["kind"] == "ollama"
                    else "remote-zdr" if provider_bundle
                    and provider_bundle["profile"]["kind"] == "openrouter"
                    and provider_bundle["profile"]["zdr"]
                    else "declared-in-agents.kim" if not provider_bundle
                    else "remote-provider-policy"
                ),
                "provider_config_sha256": (
                    provider_bundle["agents_sha256"]
                    if provider_bundle else None
                ),
                "program_sha256": (
                    provider_bundle["program_sha256"]
                    if provider_bundle else None
                ),
                "program_path": (
                    provider_bundle["program_path"]
                    if provider_bundle else None
                ),
                "credential_env": (
                    provider_bundle["credential_env"]
                    if provider_bundle else ""
                ),
            }
            self._state["jobs"].append(job)
            self._save()
        self._wake.set()
        return self.public()

    def add_professional(
        self,
        report: str,
        photos: str,
        output: str = "",
        review: str = "",
        policy: str = "effective",
        profile: str = "family",
        provider_profile_id: str = "",
        reassess: bool = False,
    ) -> dict[str, Any]:
        """Queue a professional shortlist derived from an existing report.

        ``reassess`` retires an existing shortlist so the models can judge
        the selection again from scratch. The old shortlist and its
        checkpoint are moved aside with a timestamp rather than deleted, so
        an assessment is never silently lost.
        """
        report_path = Path(str(report)).expanduser().resolve()
        source = Path(str(photos)).expanduser().resolve()
        review_path = (
            Path(str(review)).expanduser().resolve()
            if str(review).strip() else None)
        if not report_path.is_file():
            raise JobError(f"culling report is missing: {report_path}")
        if not source.is_dir():
            raise JobError(f"photo folder is not a directory: {source}")
        if review_path is not None and not review_path.is_file():
            raise JobError(f"review file is missing: {review_path}")
        if policy not in {"human_only", "effective", "ai_only", "all"}:
            raise JobError(f"unsupported candidate policy: {policy}")
        # The bar is a composed description, not one of the culling
        # profiles: strictness plus any lenses plus the photographer's
        # own words, in prose the prompt reads directly.
        profile = str(profile or "").strip()
        if not profile or len(profile) > 700:
            raise JobError(
                "the assessment bar must be a short description of what "
                "to judge by")
        try:
            project_path, project = load_or_create_folder_project(
                source, report_path.stem,
                report_path.with_suffix(".opencull-project.json"))
            layout = ensure_project_layout(source)
        except (OSError, ValueError) as exc:
            raise JobError(f"cannot open the Darkimiya project: {exc}") from exc
        chosen_output = (
            self._validate_output(Path(output))
            if str(output).strip()
            else (layout["Reports"] /
                  f"{report_path.stem}.professional-shortlist.json").resolve()
        )
        if chosen_output.exists():
            if not reassess:
                raise JobError(
                    "professional shortlist output already exists")
            # Keep the superseded assessment beside the new one rather than
            # destroying it: a reassessment is a second opinion, not an
            # erasure.
            stamp = _now().replace(":", "").replace("-", "")[:15]
            for path in (chosen_output,
                         Path(str(chosen_output) + ".checkpoint.json")):
                if path.is_file():
                    path.rename(path.with_name(
                        f"{path.name}.superseded-{stamp}"))
        with self._lock:
            if any(
                job.get("kind") == "professional_shortlist"
                and Path(job.get("report", "")) == report_path
                and job.get("status") not in TERMINAL
                for job in self._state["jobs"]
            ):
                raise JobError(
                    "this report already has a queued professional shortlist")
            job_id = uuid.uuid4().hex[:12]
            provider_bundle = None
            if provider_profile_id:
                if self.providers is None:
                    raise JobError("provider profiles are disabled")
                try:
                    provider_bundle = self.providers.materialize(
                        job_id, provider_profile_id,
                        "professional_shortlist.kim")
                    self.program_checker(Path(provider_bundle["program_path"]))
                except (ProviderError, JobError) as exc:
                    raise JobError(str(exc)) from exc
            profile_data = (
                provider_bundle["profile"] if provider_bundle else {})
            log = chosen_output.with_suffix(chosen_output.suffix + ".log")
            # Named by the bar, so a change of mind leaves the previous
            # bar's ratings intact and resumable.
            checkpoint = Path(bar_checkpoint_path(chosen_output, profile))
            job = {
                "id": job_id,
                "kind": "professional_shortlist",
                "project": str(project_path),
                "project_id": project["id"],
                "report": str(report_path),
                "photos": str(source),
                "review": str(review_path) if review_path else "",
                "output": str(chosen_output),
                "checkpoint": str(checkpoint),
                "log": str(log),
                "policy": policy,
                "profile": profile,
                # Read from the project rather than asked for again: the
                # filter was on the lens for the whole album, and the
                # photographer already said so on the develop page.
                "spectrum": str(
                    (project.get("rendering") or {}).get("spectrum")
                    or "visible"),
                "cutoff_nm": float(
                    (project.get("rendering") or {}).get("cutoff_nm") or 0),
                "about": str(project.get("about") or ""),
                "status": "queued",
                "message": "Waiting for the professional-shortlist worker.",
                "pid": None,
                "created_at": _now(),
                "started_at": None,
                "finished_at": None,
                "exit_code": None,
                "provider_profile_id": provider_profile_id or None,
                "provider_profile_name": (
                    profile_data.get("name") if provider_bundle
                    else "Legacy agents.kim"),
                "provider_kind": (
                    profile_data.get("kind") if provider_bundle else "legacy"),
                "provider_privacy": (
                    "local" if profile_data.get("kind") == "ollama"
                    else "remote-zdr"
                    if profile_data.get("kind") == "openrouter"
                    and profile_data.get("zdr")
                    else "declared-in-agents.kim"
                    if not provider_bundle else "remote-provider-policy"),
                "provider_config_sha256": (
                    provider_bundle["agents_sha256"]
                    if provider_bundle else None),
                "program_sha256": (
                    provider_bundle["program_sha256"]
                    if provider_bundle else None),
                "program_path": (
                    provider_bundle["program_path"]
                    if provider_bundle else None),
                "credential_env": (
                    provider_bundle["credential_env"]
                    if provider_bundle else ""),
            }
            self._state["jobs"].append(job)
            self._save()
        self._wake.set()
        return self.public()

    def _default_output(self, source: Path, directory: Path | None = None) -> Path:
        base = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in source.name
        ).strip("_") or "photos"
        root = directory or self.output_root
        candidate = root / f"{base}-results.json"
        number = 2
        # Only a live job's output is spoken for. A failed or cancelled
        # run left a checkpoint at its output and no report; giving the
        # retry the same path is what lets it resume the decisions
        # instead of paying for them again.
        while candidate.exists() or any(
            str(job.get("output") or "") == str(candidate)
            and job.get("status") not in TERMINAL
            for job in self._state["jobs"]
        ):
            candidate = root / f"{base}-results-{number}.json"
            number += 1
        return candidate.resolve()

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        # Keep Kimiya stage markers visible to the live queue log while a
        # subprocess is running; redirected stdout is otherwise buffered.
        environment["PYTHONUNBUFFERED"] = "1"
        kimiya_checkout = self.project_root.parent / "kimiya-lang"
        if (kimiya_checkout / "kimiya").is_dir():
            existing_pythonpath = environment.get("PYTHONPATH", "")
            environment["PYTHONPATH"] = os.pathsep.join(
                part for part in [
                    str(kimiya_checkout), existing_pythonpath
                ] if part
            )
        return environment

    def _environment_for_job(self, job: dict[str, Any]) -> dict[str, str]:
        """Give delivery rendering a bounded, lower-priority execution lane.

        Preview darktable processes are launched by the threaded review server
        and retain normal interactive priority.  A full-resolution export is a
        separate supervised process; limiting its native worker pools prevents
        it from consuming every core while the photographer continues to
        inspect previews.
        """
        environment = self._environment()
        if job.get("kind") in {"renderer_export", "delivery_export"}:
            environment.update({
                "DARKIMIYA_BACKGROUND_EXPORT": "1",
                "DARKIMIYA_EXPORT_NICE": "10",
                "OMP_NUM_THREADS": "2",
                "OPENBLAS_NUM_THREADS": "2",
                "VECLIB_MAXIMUM_THREADS": "2",
                "NUMEXPR_NUM_THREADS": "2",
            })
        return environment

    def _check_program(self, program: Path) -> None:
        result = subprocess.run(
            [self.python, "-m", "kimiya", "check", str(program)],
            cwd=self.project_root,
            env=self._environment(),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if result.returncode != 0:
            detail = (result.stdout + "\n" + result.stderr).strip()[-4000:]
            raise JobError(
                f"generated provider program does not pass Kimiya check: {detail}")

    def action(self, job_id: str, action: str) -> dict[str, Any]:
        with self._lock:
            job = self._job(job_id)
            status = job["status"]
            if action == "pause":
                if status == "running" and job_id == self._active_job_id:
                    job.update(
                        status="stopping",
                        message="Pausing after process shutdown.")
                    self._save()
                    assert self._process is not None
                    self._process.terminate()
                elif status == "detached" and _pid_runs_job(
                        job.get("pid"), job_id):
                    # Detached but provably ours: signal it, and let the
                    # detached monitor mark it paused when it ends.
                    job.update(message=(
                        "Pausing after the current step; the checkpoint "
                        "keeps every decision."))
                    self._save()
                    os.kill(int(job["pid"]), signal.SIGTERM)
                else:
                    raise JobError(
                        "only a running job can be paused")
            elif action == "cancel":
                if status in TERMINAL:
                    raise JobError("job is already finished")
                if status == "detached":
                    raise JobError(
                        "a detached process cannot be signalled safely; "
                        "wait for it to finish")
                job["_stop_as"] = "cancelled"
                if status == "running" and job_id == self._active_job_id:
                    job.update(status="stopping", message="Cancelling culling process.")
                    assert self._process is not None
                    self._process.terminate()
                else:
                    job.update(
                        status="cancelled", finished_at=_now(),
                        message="Cancelled; checkpoint retained.")
                self._save()
            elif action in {"resume", "retry"}:
                allowed = {"paused", "failed", "cancelled"}
                if status not in allowed:
                    raise JobError(f"cannot {action} a {status} job")
                if Path(job["output"]).exists():
                    raise JobError("job output already exists")
                job.update(
                    status="queued", message="Queued to resume from checkpoint.",
                    pid=None, exit_code=None, finished_at=None)
                job.pop("_stop_as", None)
                self._save()
                self._wake.set()
            else:
                raise JobError(f"unsupported job action: {action}")
            return self.public()

    def add_treatment(
        self,
        photos: str,
        photo: str,
        rounds: Any = 3,
        output: str = "",
        provider_profile_id: str = "",
    ) -> dict[str, Any]:
        """Queue a Kimiya Treatment: one photograph, developed in rounds.

        The most expensive thing a single frame can ask for -- each round
        is a plan, a question per mask, a render and a critique -- so it
        is queued per photograph and never for a folder.
        """
        source = Path(photos).expanduser().resolve()
        if not source.is_dir():
            raise JobError(f"photo folder is not a directory: {source}")
        name = _photo_inside(source, photo)
        try:
            budget = int(rounds)
        except (TypeError, ValueError):
            raise JobError("rounds must be a small whole number") from None
        if not 1 <= budget <= 6:
            raise JobError("rounds must be between 1 and 6")
        try:
            project_path, project = load_or_create_folder_project(
                source, source.name)
            layout = ensure_project_layout(source)
        except (OSError, ValueError) as exc:
            raise JobError(f"cannot open the Darkimiya project: {exc}") from exc
        if str(output).strip():
            chosen_output = self._validate_output(Path(output))
        else:
            # A frame may be treated again; the report of the last time is
            # not something a new run silently overwrites.
            stem = Path(name).stem
            chosen_output = (layout["Recipes"] / f"{stem}.treatment.json").resolve()
            number = 2
            while chosen_output.exists():
                chosen_output = (
                    layout["Recipes"] / f"{stem}.treatment-{number}.json").resolve()
                number += 1
        with self._lock:
            job_id = uuid.uuid4().hex[:12]
            provider_bundle = None
            if provider_profile_id:
                if self.providers is None:
                    raise JobError("provider profiles are disabled")
                try:
                    provider_bundle = self.providers.materialize(
                        job_id, provider_profile_id, "protect_then_reveal.kim")
                    self.program_checker(Path(provider_bundle["program_path"]))
                except (ProviderError, JobError) as exc:
                    raise JobError(str(exc)) from exc
            profile_data = provider_bundle["profile"] if provider_bundle else {}
            job = {
                "id": job_id,
                "kind": "treatment",
                "project": str(project_path),
                "project_id": project["id"],
                "photos": str(source),
                "photo": name,
                "rounds": budget,
                "output": str(chosen_output),
                "checkpoint": f"{chosen_output}.checkpoint.json",
                "log": f"{chosen_output}.log",
                "spectrum": str(
                    (project.get("rendering") or {}).get("spectrum")
                    or "visible"),
                "cutoff_nm": float(
                    (project.get("rendering") or {}).get("cutoff_nm") or 0),
                "about": str(project.get("about") or ""),
                "status": "queued",
                "message": f"Waiting to treat {name}.",
                "pid": None, "created_at": _now(), "started_at": None,
                "finished_at": None, "exit_code": None,
                "provider_profile_id": provider_profile_id or None,
                "requested_model": None,
                "provider_profile_name": profile_data.get(
                    "name", "Legacy agents.kim"),
                "provider_kind": profile_data.get("kind", "legacy"),
                "provider_privacy": (
                    "local" if profile_data.get("kind") == "ollama"
                    else "remote-zdr" if profile_data.get("kind") == "openrouter"
                    and profile_data.get("zdr")
                    else "declared-in-agents.kim" if not provider_bundle
                    else "remote-provider-policy"),
                "provider_config_sha256": (
                    provider_bundle["agents_sha256"] if provider_bundle else None),
                "program_sha256": (
                    provider_bundle["program_sha256"] if provider_bundle else None),
                "program_path": (
                    provider_bundle["program_path"] if provider_bundle else None),
                # The worker reads this to put the key into the process
                # environment. Every job kind records it; the one that
                # forgot queued fine and then starved its model calls.
                "credential_env": (
                    provider_bundle["credential_env"]
                    if provider_bundle else ""),
            }
            self._state["jobs"].append(job)
            self._save()
        self._wake.set()
        return self.public()

    def add_program(
        self,
        program: str,
        parameters: dict[str, Any],
        provider_profile_id: str = "",
        photos: str = "",
        store: Any = None,
    ) -> dict[str, Any]:
        """Queue one of the photographer's own Kimiya programs.

        The same rails as every built-in: a per-job bundle carrying
        agents.kim with key_env and never the key, the program's hash
        pinned on the job, the compiler's check before anything runs,
        and the log beside the output. What is different is only whose
        program it is.
        """
        if store is None:
            raise JobError("the program store is not available")
        name = Path(str(program)).name
        try:
            resolved = store.resolve_uses(
                store.read(name), agents=Path("agents.kim"))
        except Exception as exc:
            raise JobError(str(exc)) from exc
        settled: dict[str, Any] = {}
        for key, value in (parameters or {}).items():
            said = str(value)
            if len(said) > 2000:
                raise JobError(f"parameter {key} is unreasonably long")
            settled[str(key)] = said
        output = str(settled.get("output") or "").strip()
        if not output:
            raise JobError(
                "the run needs an output parameter, so the queue can say "
                "where the result landed")
        output_path = Path(output).expanduser().resolve()
        if not output_path.parent.is_dir():
            raise JobError(
                f"output directory does not exist: {output_path.parent}")
        settled["output"] = str(output_path)
        with self._lock:
            job_id = uuid.uuid4().hex[:12]
            provider_bundle = None
            if provider_profile_id:
                if self.providers is None:
                    raise JobError("provider profiles are disabled")
                try:
                    provider_bundle = self.providers.materialize_custom(
                        job_id, provider_profile_id,
                        Path(str(store.directory)) / name, resolved)
                    self.program_checker(Path(provider_bundle["program_path"]))
                except (ProviderError, JobError) as exc:
                    raise JobError(str(exc)) from exc
            profile_data = provider_bundle["profile"] if provider_bundle else {}
            job = {
                "id": job_id,
                "kind": "kimiya_program",
                "program": name,
                "parameters": settled,
                "photos": str(photos or settled.get("photos") or ""),
                "output": str(output_path),
                "checkpoint": f"{output_path}.checkpoint.json",
                "log": f"{output_path}.log",
                "status": "queued",
                "message": f"Waiting to run {name}.",
                "pid": None, "created_at": _now(), "started_at": None,
                "finished_at": None, "exit_code": None,
                "provider_profile_id": provider_profile_id or None,
                "requested_model": None,
                "provider_profile_name": profile_data.get(
                    "name", "Legacy agents.kim"),
                "provider_kind": profile_data.get("kind", "legacy"),
                "provider_privacy": (
                    "local" if profile_data.get("kind") == "ollama"
                    else "remote-zdr" if profile_data.get("kind") == "openrouter"
                    and profile_data.get("zdr")
                    else "declared-in-agents.kim" if not provider_bundle
                    else "remote-provider-policy"),
                "provider_config_sha256": (
                    provider_bundle["agents_sha256"] if provider_bundle else None),
                "program_sha256": (
                    provider_bundle["program_sha256"] if provider_bundle else None),
                "program_path": (
                    provider_bundle["program_path"] if provider_bundle else None),
                "credential_env": (
                    provider_bundle["credential_env"]
                    if provider_bundle else ""),
            }
            self._state["jobs"].append(job)
            self._save()
        self._wake.set()
        return self.public()

    def add_control_zones(
        self,
        photos: str,
        photo: str,
        provider_profile_id: str = "",
    ) -> dict[str, Any]:
        """Queue the advisory-band placement for one frame: one model
        call and a light panel, writing the zones file the fine-tune
        page reads."""
        source = Path(photos).expanduser().resolve()
        if not source.is_dir():
            raise JobError(f"photo folder is not a directory: {source}")
        name = _photo_inside(source, photo)
        try:
            project_path, project = load_or_create_folder_project(
                source, source.name)
            layout = ensure_project_layout(source)
        except (OSError, ValueError) as exc:
            raise JobError(f"cannot open the Darkimiya project: {exc}") from exc
        chosen_output = (layout["Recipes"]
                         / f"{Path(name).stem}.control-zones.json").resolve()
        if chosen_output.exists():
            # Advice is replaceable: a re-ask overwrites rather than
            # numbering, because two zone files for one frame would
            # leave the page guessing which advice is current.
            chosen_output.unlink()
        with self._lock:
            job_id = uuid.uuid4().hex[:12]
            provider_bundle = None
            if provider_profile_id:
                if self.providers is None:
                    raise JobError("provider profiles are disabled")
                try:
                    provider_bundle = self.providers.materialize(
                        job_id, provider_profile_id, "control_zones.kim")
                    self.program_checker(Path(provider_bundle["program_path"]))
                except (ProviderError, JobError) as exc:
                    raise JobError(str(exc)) from exc
            profile_data = provider_bundle["profile"] if provider_bundle else {}
            job = {
                "id": job_id,
                "kind": "control_zones",
                "project": str(project_path),
                "project_id": project["id"],
                "photos": str(source),
                "photo": name,
                "output": str(chosen_output),
                "checkpoint": f"{chosen_output}.checkpoint.json",
                "log": f"{chosen_output}.log",
                "spectrum": str(
                    (project.get("rendering") or {}).get("spectrum")
                    or "visible"),
                "cutoff_nm": float(
                    (project.get("rendering") or {}).get("cutoff_nm") or 0),
                "about": str(project.get("about") or ""),
                "status": "queued",
                "message": f"Waiting to place the bands for {name}.",
                "pid": None, "created_at": _now(), "started_at": None,
                "finished_at": None, "exit_code": None,
                "provider_profile_id": provider_profile_id or None,
                "requested_model": None,
                "provider_profile_name": profile_data.get(
                    "name", "Legacy agents.kim"),
                "provider_kind": profile_data.get("kind", "legacy"),
                "provider_privacy": (
                    "local" if profile_data.get("kind") == "ollama"
                    else "remote-zdr" if profile_data.get("kind") == "openrouter"
                    and profile_data.get("zdr")
                    else "declared-in-agents.kim" if not provider_bundle
                    else "remote-provider-policy"),
                "provider_config_sha256": (
                    provider_bundle["agents_sha256"] if provider_bundle else None),
                "program_sha256": (
                    provider_bundle["program_sha256"] if provider_bundle else None),
                "program_path": (
                    provider_bundle["program_path"] if provider_bundle else None),
                "credential_env": (
                    provider_bundle["credential_env"]
                    if provider_bundle else ""),
            }
            self._state["jobs"].append(job)
            self._save()
        self._wake.set()
        return self.public()

    def add_edit_suggestions(
        self,
        shortlist: str,
        review: str,
        photos: str,
        output: str = "",
        profile: str = "family",
        provider_profile_id: str = "",
        model: str = "",
        consensus: bool = False,
        style_profile: str = "",
        style_profiles: list[str] | None = None,
        only_photo: str = "",
        only_photos: list[str] | None = None,
    ) -> dict[str, Any]:
        shortlist_path = Path(shortlist).expanduser().resolve()
        review_path = Path(review).expanduser().resolve()
        source = Path(photos).expanduser().resolve()
        if not shortlist_path.is_file() or not review_path.is_file():
            raise JobError("shortlist and its human review must exist")
        if not source.is_dir():
            raise JobError(f"photo folder is not a directory: {source}")
        style_path = Path(style_profile).expanduser().resolve() if str(style_profile).strip() else None
        if style_path is not None and not style_path.is_file():
            raise JobError("personal style profile does not exist")
        # Every style the run may speak in. The run chooses among them per
        # photograph, so a missing one is a menu that lies about itself.
        style_bank = []
        for item in style_profiles or []:
            resolved = Path(str(item)).expanduser().resolve()
            if not resolved.is_file():
                raise JobError("personal style profile does not exist")
            if str(resolved) not in style_bank:
                style_bank.append(str(resolved))
        profile = str(profile or "").strip()
        if not profile or len(profile) > 700:
            raise JobError(
                "the editing profile must be a short description of what "
                "to judge by")
        try:
            project_path, project = load_or_create_folder_project(
                source, shortlist_path.stem)
            layout = ensure_project_layout(source)
        except (OSError, ValueError) as exc:
            raise JobError(f"cannot open the Darkimiya project: {exc}") from exc
        chosen_output = (
            self._validate_output(Path(output))
            if str(output).strip()
            else (layout["Recipes"] /
                  f"{shortlist_path.stem}.edit-directions.json").resolve()
        )
        if chosen_output.exists():
            raise JobError("edit-directions output already exists")
        with self._lock:
            job_id = uuid.uuid4().hex[:12]
            provider_bundle = None
            if provider_profile_id:
                if self.providers is None:
                    raise JobError("provider profiles are disabled")
                try:
                    provider_profile = next(
                        (
                            item for item in self.providers.public()["profiles"]
                            if item["id"] == provider_profile_id
                        ),
                        None,
                    )
                    if model and (
                        provider_profile is None
                        or provider_profile.get("kind") != "openrouter"
                    ):
                        raise JobError(
                            "a per-job model name requires an OpenRouter profile")
                    provider_bundle = self.providers.materialize(
                        job_id, provider_profile_id, "edit_suggestions.kim",
                        {"A": model} if model else None)
                    self.program_checker(Path(provider_bundle["program_path"]))
                except (ProviderError, JobError) as exc:
                    raise JobError(str(exc)) from exc
            profile_data = provider_bundle["profile"] if provider_bundle else {}
            job = {
                "id": job_id,
                "kind": "edit_suggestions",
                "project": str(project_path),
                "project_id": project["id"],
                "shortlist": str(shortlist_path),
                "review": str(review_path),
                "photos": str(source),
                "output": str(chosen_output),
                "checkpoint": f"{chosen_output}.checkpoint.json",
                "log": f"{chosen_output}.log",
                "profile": profile,
                "style_profile": str(style_path) if style_path else "",
                "style_profiles": style_bank,
                "only_photo": str(only_photo).strip(),
                "only_photos": [
                    str(item).strip() for item in (only_photos or [])
                    if str(item).strip()
                ],
                "spectrum": str(
                    (project.get("rendering") or {}).get("spectrum")
                    or "visible"),
                "cutoff_nm": float(
                    (project.get("rendering") or {}).get("cutoff_nm") or 0),
                "about": str(project.get("about") or ""),
                "status": "queued",
                "message": "Waiting for the edit-direction worker.",
                "pid": None, "created_at": _now(), "started_at": None,
                "finished_at": None, "exit_code": None,
                "provider_profile_id": provider_profile_id or None,
                "requested_model": (
                    profile_data.get("models", {}).get("A")
                    if provider_bundle else None),
                "provider_profile_name": profile_data.get(
                    "name", "Legacy agents.kim"),
                "provider_kind": profile_data.get("kind", "legacy"),
                "provider_privacy": (
                    "local" if profile_data.get("kind") == "ollama"
                    else "remote-zdr" if profile_data.get("kind") == "openrouter"
                    and profile_data.get("zdr")
                    else "declared-in-agents.kim" if not provider_bundle
                    else "remote-provider-policy"),
                "provider_config_sha256": (
                    provider_bundle["agents_sha256"] if provider_bundle else None),
                "program_sha256": (
                    provider_bundle["program_sha256"] if provider_bundle else None),
                "program_path": (
                    provider_bundle["program_path"] if provider_bundle else None),
                "credential_env": (
                    provider_bundle["credential_env"] if provider_bundle else ""),
            }
            self._state["jobs"].append(job)
            self._save()
        self._wake.set()
        return self.public()

    def add_semantic_verification(
        self, original: str, developed: str, suggestion: str,
        thumbnail: str = "", output: str = "", provider_profile_id: str = "",
        model: str = "", consensus: bool = False, project: str = "",
    ) -> dict[str, Any]:
        original_path = Path(original).expanduser().resolve()
        developed_path = Path(developed).expanduser().resolve()
        thumbnail_path = Path(thumbnail).expanduser().resolve() if str(thumbnail).strip() else None
        if not original_path.is_file() or not developed_path.is_file():
            raise JobError("original and developed images must exist")
        if thumbnail_path is not None and not thumbnail_path.is_file():
            raise JobError("thumbnail image must exist")
        if not str(suggestion).strip():
            raise JobError("edit suggestion must not be empty")
        project_path = (
            Path(project).expanduser().resolve() if str(project).strip() else None)
        if project_path is not None and not project_path.is_file():
            raise JobError("Darkimiya project manifest does not exist")
        if str(output).strip():
            chosen_output = self._validate_output(Path(output))
        elif project_path is not None:
            source_root = Path(load_project(project_path)["source_folder"])
            layout = ensure_project_layout(source_root)
            chosen_output = self._validate_output(
                layout["Verification"] /
                f"{developed_path.stem}.semantic-verification.json")
        else:
            chosen_output = self._validate_output(
                developed_path.with_suffix(".semantic-verification.json"))
        if chosen_output.exists():
            raise JobError("semantic verification output already exists")
        with self._lock:
            job_id = uuid.uuid4().hex[:12]
            if not provider_profile_id or self.providers is None:
                raise JobError("select a configured model provider")
            try:
                profile = next((p for p in self.providers.public()["profiles"]
                                if p["id"] == provider_profile_id), None)
                if model and (profile is None or profile.get("kind") != "openrouter"):
                    raise JobError("a per-job model requires an OpenRouter profile")
                bundle = self.providers.materialize(
                    job_id, provider_profile_id, "semantic_verification.kim",
                    {"A": model} if model else None)
                self.program_checker(Path(bundle["program_path"]))
            except (ProviderError, JobError) as exc:
                raise JobError(str(exc)) from exc
            pdata = bundle["profile"]
            job = {
                "id": job_id, "kind": "semantic_verification",
                "project": str(project_path) if project_path else "",
                "original": str(original_path), "developed": str(developed_path),
                "thumbnail": str(thumbnail_path) if thumbnail_path else "",
                "suggestion": str(suggestion).strip(), "output": str(chosen_output),
                "consensus": bool(consensus),
                "checkpoint": str(chosen_output), "log": f"{chosen_output}.log",
                "status": "queued", "message": "Waiting for semantic verifier.",
                "pid": None, "created_at": _now(), "started_at": None,
                "finished_at": None, "exit_code": None,
                "provider_profile_id": provider_profile_id,
                "provider_profile_name": pdata.get("name", "Provider"),
                "provider_kind": pdata.get("kind", "openrouter"),
                "provider_privacy": "remote-zdr" if pdata.get("zdr") else "remote-provider-policy",
                "requested_model": pdata.get("models", {}).get("A"),
                "provider_config_sha256": bundle["agents_sha256"],
                "program_sha256": bundle["program_sha256"],
                "program_path": bundle["program_path"],
                "credential_env": bundle["credential_env"],
            }
            self._state["jobs"].append(job)
            self._save()
        self._wake.set()
        return self.public()

    def add_style_profile(self, photos: str | list[str], output: str = "", existing: str = "",
                          mode: str = "update", provider_profile_id: str = "",
                          model: str = "", limit: int = 64) -> dict[str, Any]:
        selected_files: list[Path] | None = None
        if isinstance(photos, list):
            selected_files = []
            for value in photos:
                path = Path(str(value)).expanduser().resolve()
                if not path.is_file():
                    raise JobError(f"style example photograph does not exist: {path}")
                selected_files.append(path)
            selected_files = list(dict.fromkeys(selected_files))
            if not selected_files:
                raise JobError("choose at least one style example photograph")
            source = selected_files[0].parent
        else:
            source = Path(photos).expanduser().resolve()
            if not source.is_dir():
                raise JobError("style example folder is not a directory")
        existing_path = Path(existing).expanduser().resolve() if str(existing).strip() else None
        if existing_path is not None and not existing_path.is_file():
            raise JobError("existing style profile does not exist")
        if str(output).strip():
            requested_output = Path(output).expanduser()
            if requested_output.is_absolute() and requested_output.parent == Path("/"):
                raise JobError(
                    "save the style profile in a folder, not at the filesystem root")
            if not requested_output.is_absolute():
                requested_output = self.output_root / requested_output
            chosen_output = self._validate_output(requested_output)
        else:
            chosen_output = self._validate_output(
                self.output_root / "personal-style-profile.json")
        if chosen_output.exists():
            raise JobError("style profile output already exists")
        if mode not in {"update", "replace"}:
            raise JobError("style profile mode must be update or replace")
        with self._lock:
            job_id = uuid.uuid4().hex[:12]
            photos_argument = str(source)
            if selected_files is not None:
                manifest = self.state_path.parent / "style-profile-examples" / f"{job_id}.json"
                _atomic_json(manifest, {
                    "format": "opencull-style-examples-v1",
                    "files": [str(path) for path in selected_files],
                })
                photos_argument = str(manifest)
            if not provider_profile_id or self.providers is None:
                raise JobError("select a configured model provider")
            profile = next((p for p in self.providers.public()["profiles"] if p["id"] == provider_profile_id), None)
            if model and (profile is None or profile.get("kind") != "openrouter"):
                raise JobError("a per-job model requires an OpenRouter profile")
            bundle = self.providers.materialize(job_id, provider_profile_id, "style_profile.kim",
                                                {"A": model} if model else None)
            self.program_checker(Path(bundle["program_path"]))
            pdata = bundle["profile"]
            self._state["jobs"].append({
                "id": job_id, "kind": "style_profile", "photos": photos_argument,
                "photo_examples": [str(path) for path in selected_files] if selected_files else [],
                "output": str(chosen_output), "existing": str(existing_path or ""),
                "mode": mode, "limit": int(limit), "checkpoint": str(chosen_output),
                "log": f"{chosen_output}.log", "status": "queued",
                "message": "Waiting for style-profile worker.", "pid": None,
                "created_at": _now(), "started_at": None, "finished_at": None,
                "exit_code": None, "provider_profile_id": provider_profile_id,
                "provider_profile_name": pdata.get("name", "Provider"),
                "provider_kind": pdata.get("kind", "openrouter"),
                "provider_privacy": "remote-zdr" if pdata.get("zdr") else "remote-provider-policy",
                "requested_model": pdata.get("models", {}).get("A"),
                "provider_config_sha256": bundle["agents_sha256"],
                "program_sha256": bundle["program_sha256"], "program_path": bundle["program_path"],
                "credential_env": bundle["credential_env"],
            })
            self._save()
        self._wake.set()
        return self.public()

    def add_development_render(self, baseline: str, recipe: str, output_dir: str,
                               project: str, reference_jpeg: str = "",
                               adjustments: str = "", allow_incomplete: bool = False) -> dict[str, Any]:
        baseline_path = Path(baseline).expanduser().resolve()
        recipe_path = Path(recipe).expanduser().resolve()
        project_path = Path(project).expanduser().resolve()
        destination = Path(output_dir).expanduser().resolve()
        if not baseline_path.is_file() or not recipe_path.is_file() or not project_path.is_file():
            raise JobError("baseline, recipe, and project manifest must exist")
        try:
            recipe_value = json.loads(recipe_path.read_text(encoding="utf-8"))
            stem = Path(str(recipe_value.get("source_photo", baseline_path.stem))).stem
            style = str(recipe_value.get("style", "render"))
        except (OSError, json.JSONDecodeError) as exc:
            raise JobError(f"cannot read recipe: {exc}") from exc
        expected = destination / f"{stem}.{style}.jpg"
        if expected.exists():
            raise JobError("development output already exists")
        with self._lock:
            job_id = uuid.uuid4().hex[:12]
            job = {"id": job_id, "kind": "development_render", "baseline": str(baseline_path),
                   "recipe": str(recipe_path), "output_dir": str(destination),
                   "project": str(project_path), "reference_jpeg": str(Path(reference_jpeg).expanduser().resolve()) if reference_jpeg else "",
                   "adjustments": str(Path(adjustments).expanduser().resolve()) if adjustments else "",
                   "allow_incomplete": bool(allow_incomplete), "output": str(expected),
                   "checkpoint": str(expected), "log": f"{expected}.log", "status": "queued",
                   "message": "Waiting for development renderer.", "pid": None,
                   "created_at": _now(), "started_at": None, "finished_at": None, "exit_code": None,
                   "provider_profile_name": "Local deterministic renderer", "provider_kind": "local",
                   "provider_privacy": "local"}
            self._state["jobs"].append(job)
            self._save()
        self._wake.set()
        return self.public()

    def add_development_pipeline(
        self, source: str, reference: str, directions: str, photo: str,
        style: str, project: str,
    ) -> dict[str, Any]:
        source_path = Path(source).expanduser().resolve()
        reference_path = Path(reference).expanduser().resolve()
        directions_path = Path(directions).expanduser().resolve()
        project_path = Path(project).expanduser().resolve()
        if not all(path.is_file() for path in (
            source_path, reference_path, directions_path, project_path
        )):
            raise JobError(
                "source, reference JPEG, edit directions, and project must exist")
        if style not in {
            "calibrated", "standard", "signature", "creative", "personal"
        }:
            raise JobError("unsupported development treatment")
        try:
            direction_value = json.loads(directions_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise JobError(f"cannot read edit directions: {exc}") from exc
        if not any(
            isinstance(entry, dict) and entry.get("photo") == photo
            for entry in direction_value.get("entries", [])
        ):
            raise JobError("photograph has no matching edit direction")
        project_source = Path(load_project(project_path)["source_folder"])
        destination = ensure_project_layout(project_source)["Developments"] / Path(photo).stem
        expected = destination / f"{Path(photo).stem}.{style}.jpg"
        with self._lock:
            job_id = uuid.uuid4().hex[:12]
            self._state["jobs"].append({
                "id": job_id, "kind": "development_pipeline",
                "source": str(source_path), "reference": str(reference_path),
                "directions": str(directions_path), "photo": photo,
                "style": style, "output_dir": str(destination),
                "project": str(project_path), "photos": str(reference_path.parent),
                "output": str(expected), "checkpoint": str(expected),
                "log": f"{expected}.log", "status": "queued",
                "message": "Waiting for the photo-development worker.",
                "pid": None, "created_at": _now(), "started_at": None,
                "finished_at": None, "exit_code": None,
                "provider_profile_name": "Local deterministic renderer",
                "provider_kind": "local", "provider_privacy": "local",
            })
            self._save()
        self._wake.set()
        return self.public()

    def add_renderer_comparison(
        self, source: str, reference: str, directions: str, photo: str,
        style: str, opencull_render: str, project: str,
        demosaic: str = "markesteijn-1-pass",
    ) -> dict[str, Any]:
        paths = [Path(item).expanduser().resolve() for item in (
            source, reference, directions, opencull_render, project)]
        if not all(path.is_file() for path in paths):
            raise JobError(
                "source, reference, directions, built-in render, and project must exist")
        source_path, reference_path, directions_path, render_path, project_path = paths
        if style not in {
            "calibrated", "standard", "signature", "creative", "personal"
        }:
            raise JobError("unsupported renderer-comparison treatment")
        if demosaic not in {
            "markesteijn-1-pass", "markesteijn-3-pass",
            "markesteijn-3-pass-vng",
        }:
            raise JobError("unsupported darktable demosaic method")
        project_source = Path(load_project(project_path)["source_folder"])
        destination = (
            ensure_project_layout(project_source)["Developments"] /
            Path(photo).stem / "Renderer Comparisons")
        expected = destination / f"{source_path.stem}.{demosaic}.renderer-comparison.json"
        with self._lock:
            job_id = uuid.uuid4().hex[:12]
            self._state["jobs"].append({
                "id": job_id, "kind": "renderer_comparison",
                "source": str(source_path), "reference": str(reference_path),
                "directions": str(directions_path), "photo": photo,
                "style": style, "opencull_render": str(render_path),
                "demosaic": demosaic,
                "output_dir": str(destination), "project": str(project_path),
                "photos": str(reference_path.parent), "output": str(expected),
                "checkpoint": str(expected), "log": f"{expected}.log",
                "status": "queued", "message": "Waiting for paired renderer research.",
                "pid": None, "created_at": _now(), "started_at": None,
                "finished_at": None, "exit_code": None,
                "provider_profile_name": "Darkimiya + darktable 5.6",
                "provider_kind": "local", "provider_privacy": "local",
            })
            self._save()
        self._wake.set()
        return self.public()

    def add_renderer_export(
        self, source: str, reference: str, directions: str, photo: str,
        style: str, project: str, demosaic: str = "markesteijn-1-pass",
    ) -> dict[str, Any]:
        paths = [Path(item).expanduser().resolve() for item in (
            source, reference, directions, project)]
        if not all(path.is_file() for path in paths):
            raise JobError(
                "source, reference, directions, and project must exist")
        source_path, reference_path, directions_path, project_path = paths
        if style not in {
            "calibrated", "standard", "signature", "creative", "personal"
        }:
            raise JobError("unsupported full-resolution treatment")
        if demosaic not in {
            "markesteijn-1-pass", "markesteijn-3-pass",
            "markesteijn-3-pass-vng",
        }:
            raise JobError("unsupported darktable demosaic method")
        project_source = Path(load_project(project_path)["source_folder"])
        destination = (
            ensure_project_layout(project_source)["Developments"] /
            Path(photo).stem / "Full Resolution")
        expected = destination / (
            f"{source_path.stem}.{style}.{demosaic}.full-resolution.json")
        with self._lock:
            job_id = uuid.uuid4().hex[:12]
            self._state["jobs"].append({
                "id": job_id, "kind": "renderer_export",
                "source": str(source_path), "reference": str(reference_path),
                "directions": str(directions_path), "photo": photo,
                "style": style, "demosaic": demosaic,
                "output_dir": str(destination), "project": str(project_path),
                "photos": str(reference_path.parent), "output": str(expected),
                "checkpoint": str(expected), "log": f"{expected}.log",
                "status": "queued",
                "message": "Waiting for the full-resolution darktable renderer.",
                "pid": None, "created_at": _now(), "started_at": None,
                "finished_at": None, "exit_code": None,
                "provider_profile_name": "Darkimiya + darktable full resolution",
                "provider_kind": "local", "provider_privacy": "local",
                "execution_lane": "background_export",
                "resource_policy": {
                    "process_priority": "low", "maximum_native_threads": 2,
                    "isolated_darktable_state": True,
                },
            })
            self._save()
        self._wake.set()
        return self.public()

    def add_delivery_export(
        self, source: str, reference: str, directions: str, photo: str,
        style: str, engine: str, project: str, destination: str,
        demosaic: str = "markesteijn-1-pass", render: str = "",
    ) -> dict[str, Any]:
        """Queue one durable render-and-deliver operation with active dedupe."""
        paths = [Path(item).expanduser().resolve() for item in (
            source, reference, directions, project)]
        if not all(path.is_file() for path in paths):
            raise JobError(
                "source, reference, edit directions, and project must exist")
        source_path, reference_path, directions_path, project_path = paths
        render_path = Path(render).expanduser().resolve() if render else None
        if style not in {
            "calibrated", "standard", "signature", "creative", "personal"
        } and render_path is None:
            raise JobError("unsupported export treatment")
        if engine not in {"default", "darktable"}:
            raise JobError("unsupported export renderer")
        if demosaic not in {
            "markesteijn-1-pass", "markesteijn-3-pass",
            "markesteijn-3-pass-vng",
        }:
            raise JobError("unsupported darktable demosaic method")
        project_value = load_project(project_path)
        if render_path is not None:
            allowed = {
                str(Path(str(item.get("path", ""))).expanduser().resolve())
                for item in project_value.get("artifacts", {}).get("renders", []) or []
                if isinstance(item, dict) and item.get("path")
            }
            if not render_path.is_file() or str(render_path) not in allowed:
                raise JobError("existing render is not linked to this project")
        source_stat = source_path.stat()
        identity = {
            "project": str(project_path), "photo": photo, "style": style,
            "engine": engine, "demosaic": demosaic,
            "directions_sha256": hashlib.sha256(
                directions_path.read_bytes()).hexdigest(),
            "source": str(source_path), "source_size": source_stat.st_size,
            "source_mtime_ns": source_stat.st_mtime_ns,
        }
        export_key = hashlib.sha256(json.dumps(
            identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        requested = Path(destination).expanduser().resolve()
        if requested.suffix.casefold() not in {".jpg", ".jpeg"}:
            raise JobError("the current export format must be JPEG")
        project_source = Path(str(project_value["source_folder"]))
        layout = ensure_project_layout(project_source)
        render_output = (
            layout["Developments"] / Path(photo).stem /
            ("Full Resolution" if engine == "darktable" else "Default Export"))
        with self._lock:
            duplicate = next((
                job for job in self._state["jobs"]
                if job.get("kind") == "delivery_export"
                and job.get("export_key") == export_key
                and job.get("status") in {"queued", *ACTIVE}
            ), None)
            if duplicate is not None:
                raise JobError(
                    "This photograph and recipe are already queued for export.")
            job_id = uuid.uuid4().hex[:12]
            receipt = layout["Operations"] / f"export-{job_id}.json"
            self._state["jobs"].append({
                "id": job_id, "kind": "delivery_export",
                "source": str(source_path), "reference": str(reference_path),
                "directions": str(directions_path), "photo": photo,
                "style": style, "engine": engine, "demosaic": demosaic,
                "render": str(render_path or ""),
                "render_output_dir": str(render_output),
                "destination": str(requested), "export_key": export_key,
                "project": str(project_path), "photos": str(reference_path.parent),
                "output": str(receipt), "checkpoint": str(receipt),
                "log": f"{receipt}.log", "status": "queued",
                "message": "Waiting in the image-export queue.", "pid": None,
                "created_at": _now(), "started_at": None, "finished_at": None,
                "exit_code": None, "provider_profile_name": (
                    "Darktable background export" if engine == "darktable"
                    else "Darkimiya Default background export"),
                "provider_kind": "local", "provider_privacy": "local",
                "execution_lane": "background_export",
                "resource_policy": {
                    "process_priority": "low", "maximum_native_threads": 2,
                    "isolated_darktable_state": engine == "darktable",
                },
            })
            self._save()
        self._wake.set()
        return self.public()

    def relink_source(self, job_id: str, photos: str) -> dict[str, Any]:
        """Explicitly replace an unavailable source folder for a stopped job."""
        source = Path(str(photos)).expanduser().resolve()
        if not source.is_dir():
            raise JobError(f"replacement photo folder is not a directory: {source}")
        with self._lock:
            job = self._job(job_id)
            if job["status"] in ACTIVE or (
                job["status"] == "queued" and job_id == self._active_job_id
            ):
                raise JobError("an active culling job cannot be relinked")
            previous = Path(job["photos"])
            job["photos"] = str(source)
            job["message"] = (
                f"Source relinked from {previous.name} to {source.name}. "
                "Review the folder before resuming."
            )
            self._save()
            return self.public()

    def remove(self, job_id: str, remove_artifacts: bool = False) -> dict[str, Any]:
        """Remove a stopped queue record, optionally deleting resumable state."""
        with self._lock:
            job = self._job(job_id)
            if job["status"] in ACTIVE or job["status"] in {"queued", "stopping"}:
                raise JobError("an active or waiting culling job cannot be removed")
            checkpoint = Path(job["checkpoint"])
            log = Path(job["log"])
            self._state["jobs"].remove(job)
            self._save()
        if remove_artifacts:
            for artifact in (checkpoint, log):
                try:
                    if artifact.is_file() or artifact.is_symlink():
                        artifact.unlink()
                except OSError as exc:
                    raise JobError(
                        f"queue entry was removed, but could not delete "
                        f"{artifact.name}: {exc}") from exc
        return self.public()

    def open_review(self, job_id: str) -> dict[str, Any]:
        """Launch a separate read/review server for a completed report."""
        with self._lock:
            job = deepcopy(self._job(job_id))
        if job.get("status") != "completed":
            raise JobError("only a completed job can be opened for review")
        if not Path(job["output"]).is_file():
            raise JobError("completed report is missing")
        review_log = Path(job["output"] + ".gui.log")
        handle = review_log.open("a", encoding="utf-8")
        try:
            command = [
                    self.python,
                    str(self.project_root / "gui.py"),
                    (
                        job["report"]
                        if job.get("kind") == "professional_shortlist"
                        else job["output"]
                    ),
                    job["photos"],
                    "--port", "0",
                    "--no-job-manager",
                ]
            if job.get("kind") == "professional_shortlist":
                command.extend(["--shortlist", job["output"]])
            process = subprocess.Popen(
                command,
                cwd=self.project_root,
                stdout=handle,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        except Exception as exc:
            handle.close()
            raise JobError(f"could not start review server: {exc}") from exc
        handle.close()
        return {
            "job_id": job_id,
            "pid": process.pid,
            "message": "A separate review server is starting in a new browser tab.",
            "log": str(review_log),
        }

    def public(self) -> dict[str, Any]:
        with self._lock:
            jobs = []
            for stored in self._state["jobs"]:
                job = {
                    key: deepcopy(value)
                    for key, value in stored.items()
                    if not key.startswith("_")
                }
                job["progress"] = self._progress(stored)
                job["log_tail"] = self._log_tail(Path(stored["log"]))
                jobs.append(job)
            return {
                "format": QUEUE_FORMAT,
                "revision": self._state["revision"],
                "state_path": str(self.state_path),
                "sequential": True,
                "active_job_id": next(
                    (job["id"] for job in jobs if job["status"] in ACTIVE),
                    None,
                ),
                "jobs": jobs,
            }

    def style_profile_result(self, job_id: str) -> dict[str, Any]:
        """Return the example photographs recorded by a completed style job."""
        with self._lock:
            job = deepcopy(self._job(job_id))
        if job.get("kind") != "style_profile":
            raise JobError("job is not a personal style extraction")
        output = Path(str(job.get("output", ""))).expanduser().resolve()
        if not output.is_file():
            raise JobError("style profile output is not available yet")
        try:
            value = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise JobError(f"style profile output is unreadable: {exc}") from exc
        examples = []
        for item in value.get("examples", []):
            if isinstance(item, dict):
                path = str(item.get("path", "")).strip()
            else:
                path = str(item).strip()
            if path:
                examples.append(path)
        return {
            "job_id": job_id,
            "output": str(output),
            "examples": list(dict.fromkeys(examples)),
            # The current extractor uses every supplied example (up to its limit).
            # Keep this separate so a future selector can return a strict subset.
            "selected_examples": list(dict.fromkeys(examples)),
        }

    @staticmethod
    def _progress(job: dict[str, Any]) -> dict[str, Any]:
        checkpoint = readable_checkpoint(job["checkpoint"])
        completed = total = 0
        checkpoint_complete = False
        kind = job.get("kind")

        if kind == "treatment":
            # The run writes its work down as it goes -- a plan, then the
            # masks, then the round's render -- so progress is read off
            # the disk rather than guessed. The newest directory for this
            # frame that appeared after the job started is this run's.
            rounds = max(int(job.get("rounds") or 3), 1)
            stem = Path(str(job.get("photo") or "")).stem
            started = str(job.get("started_at") or job.get("created_at") or "")
            newest, rendered, planning = None, 0, 0
            root = (Path(str(job.get("photos") or "")) / ".darkimiya"
                    / "Treatments")
            if stem and root.is_dir():
                stamp = started.replace("-", "").replace(":", "")[:15]
                candidates = sorted(
                    item.name for item in root.glob(f"{stem}.*")
                    if item.is_dir()
                    and (not stamp or item.name.rsplit(".", 1)[-1] >= stamp))
                newest = root / candidates[-1] if candidates else None
            if newest is not None:
                rendered = len(list(newest.glob("round-*.jpg")))
                planning = len(list(newest.glob("plan-*.json")))
            stage = (
                "waiting for the first plan" if planning == 0
                else f"round {rendered}: judging the render"
                if rendered >= planning
                else f"round {planning}: planning and rendering")
            return {
                "completed_clusters": rendered,
                "total_clusters": rounds,
                "fraction": min(rendered / rounds, 1.0),
                "checkpoint_complete": False,
                "completed_items": rendered,
                "total_items": rounds,
                "stage": stage,
            }

        # Render jobs deliberately use their image output as the completion
        # artifact.  It is not a Kimiya JSON checkpoint and must never be
        # decoded as text while the queue API is assembling status.
        if kind in {"development_render", "development_pipeline", "renderer_comparison",
                    "renderer_export", "delivery_export"}:
            completed = 1 if Path(job.get("output", "")).is_file() else 0
            total = 1
            fraction = completed / total
            if kind == "delivery_export" and not completed:
                matches = re.findall(
                    r"EXPORT_PROGRESS\s+(\d{1,3})\s+[^\r\n]+",
                    JobManager._log_tail(Path(str(job.get("log", "")))))
                fraction = min(1.0, max(0.0, int(matches[-1]) / 100)) if matches else 0
            return {
                "completed_clusters": completed,
                "total_clusters": total,
                "fraction": fraction,
                "checkpoint_complete": bool(completed),
                "completed_items": completed,
                "total_items": total,
            }
        if kind == "style_profile":
            # A style profile has no per-item checkpoint: the program
            # narrates itself instead, and that narration is the only
            # honest progress there is.
            said = re.findall(
                r"STYLE_PROGRESS\s+(\d{1,3})\s+([^\r\n]+)",
                JobManager._log_tail(Path(str(job.get("log", "")))))
            done = Path(job.get("output", "")).is_file()
            percent = 100 if done else (int(said[-1][0]) if said else 0)
            return {
                "completed_clusters": 0, "total_clusters": 0,
                "fraction": percent / 100,
                "checkpoint_complete": done,
                "completed_items": 0, "total_items": 0,
                "stage": said[-1][1].strip() if said else "",
            }
        if kind == "kimiya_program":
            # A program with no per-item checkpoint narrates itself. The
            # timelapse prints a marker per phase step; when it does, that
            # is the honest progress. A program that prints none falls
            # through to the checkpoint reading below unchanged.
            #
            # The log and the output both survive from run to run -- the
            # timelapse writes to the same folder every time -- so a rerun
            # opened at 100% instantly: last run's markers still in the
            # tail, last run's report still on disk. Only what THIS run
            # printed counts, which is everything after the supervisor's
            # own opening line; and being finished is the job's status to
            # say, never the presence of a file an earlier run wrote.
            if job.get("status") == "queued":
                # Not started: whatever the log holds is another run's.
                return {
                    "completed_clusters": 0, "total_clusters": 0,
                    "fraction": 0.0, "checkpoint_complete": False,
                    "completed_items": 0, "total_items": 0, "stage": "",
                }
            tail = JobManager._log_tail(Path(str(job.get("log", ""))))
            begun = tail.rfind("starting supervised command")
            if begun >= 0:
                tail = tail[begun:]
            said = re.findall(
                r"TIMELAPSE_PROGRESS\s+(\d{1,3})\s+([^\r\n]+)", tail)
            if said:
                percent = int(said[-1][0])
                return {
                    "completed_clusters": 0, "total_clusters": 0,
                    "fraction": max(0.0, min(1.0, percent / 100)),
                    "checkpoint_complete": False,
                    "completed_items": 0, "total_items": 0,
                    "stage": said[-1][1].strip(),
                }
        if checkpoint.is_file():
            try:
                data = json.loads(checkpoint.read_text(encoding="utf-8"))
                if kind == "professional_shortlist":
                    completed = len(data.get("assessments", []))
                    total = int(
                        data.get("signature", {}).get("candidate_count", 0))
                elif kind == "edit_suggestions":
                    completed = len(data.get("entries", []))
                    total = len(data.get("candidate_photos", [])) or completed
                elif kind == "semantic_verification":
                    completed = 1 if data.get("judgment") else 0
                    total = 1
                elif kind == "style_profile":
                    completed = 1 if data.get("profile") else 0
                    total = 1
                else:
                    completed = len(data.get("decisions", []))
                    total = len(
                        data.get("signature", {}).get("cluster_ids", []))
                checkpoint_complete = bool(
                    data.get("completed") or data.get("complete"))
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                pass
        return {
            "completed_clusters": completed,
            "total_clusters": total,
            "fraction": completed / total if total else 0,
            "checkpoint_complete": checkpoint_complete,
            "completed_items": completed,
            "total_items": total,
        }

    @staticmethod
    def _log_tail(path: Path, limit: int = 12000) -> str:
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - limit))
                return handle.read().decode("utf-8", errors="replace")
        except OSError:
            return ""

    def _detached_alive(self) -> bool:
        detached = [
            job for job in self._state["jobs"] if job["status"] == "detached"]
        for job in detached:
            if _pid_alive(job.get("pid")):
                return True
            if Path(job["output"]).is_file():
                job.update(
                    status="completed", pid=None, finished_at=_now(),
                    message="Completed while GUI was restarted.")
                self._register_completed_output(job)
            else:
                job.update(
                    status="paused", pid=None,
                    message="Detached process ended; resume from checkpoint.")
            self._save()
        return False

    def _worker(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                if self._detached_alive():
                    next_job = None
                else:
                    next_job = next(
                        (job for job in self._state["jobs"]
                         if job["status"] == "queued"),
                        None,
                    )
            if next_job is None:
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            self._run(next_job["id"])

    def _run(self, job_id: str) -> None:
        with self._lock:
            job = self._job(job_id)
            if job["status"] != "queued":
                return
            Path(job["log"]).parent.mkdir(parents=True, exist_ok=True)
            log_handle = Path(job["log"]).open("a", encoding="utf-8")
            log_handle.write(f"\n[{_now()}] starting supervised command\n")
            log_handle.flush()
            try:
                command = self.command_builder(deepcopy(job))
                environment = self._environment_for_job(job)
                if self.kimiya_workspace_root is not None:
                    workspace = self.kimiya_workspace_root / job["id"]
                    workspace.mkdir(parents=True, exist_ok=True)
                    os.chmod(workspace, 0o700)
                    environment["KIMIYA_WORKSPACE"] = str(workspace)
                credential_env = str(job.get("credential_env") or "")
                if credential_env:
                    if self.providers is None:
                        raise JobError(
                            "provider credential store is unavailable")
                    profile_id = str(job.get("provider_profile_id") or "")
                    environment[credential_env] = (
                        self.providers.keychain.get(profile_id))
                process = subprocess.Popen(
                    command,
                    cwd=self.project_root,
                    env=environment,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                )
            except Exception as exc:
                log_handle.close()
                job.update(
                    status="failed", finished_at=_now(),
                    message=f"Could not start Kimiya: {exc}", exit_code=None)
                self._save()
                return
            self._process = process
            self._active_job_id = job_id
            kind_label = (
                "Professional shortlist"
                if job.get("kind") == "professional_shortlist"
                else "Edit-direction assessment"
                if job.get("kind") == "edit_suggestions"
                else "Semantic edit verification"
                if job.get("kind") == "semantic_verification"
                else "RAW development render"
                if job.get("kind") == "development_render"
                else "Guided photo development"
                if job.get("kind") == "development_pipeline"
                else "Paired renderer comparison"
                if job.get("kind") == "renderer_comparison"
                else "Full-resolution darktable render"
                if job.get("kind") == "renderer_export"
                else "Image export"
                if job.get("kind") == "delivery_export"
                else "Personal style extraction"
                if job.get("kind") == "style_profile"
                else "Kimiya treatment"
                if job.get("kind") == "treatment"
                else "Slider advice placement"
                if job.get("kind") == "control_zones"
                else f"Your program {job.get('program', '')}"
                if job.get("kind") == "kimiya_program"
                else "Kimiya culling")
            job.update(
                status="running", pid=process.pid, started_at=_now(),
                finished_at=None, message=f"{kind_label} is running.")
            self._save()
        exit_code = process.wait()
        log_handle.close()
        with self._lock:
            job = self._job(job_id)
            requested = job.pop("_stop_as", None)
            if Path(job["output"]).is_file() and exit_code == 0:
                status, message = (
                    "completed",
                    "Professional shortlist is ready for review."
                    if job.get("kind") == "professional_shortlist"
                    else "Professional edit directions are ready."
                    if job.get("kind") == "edit_suggestions"
                    else "Semantic verification certificate is ready."
                    if job.get("kind") == "semantic_verification"
                    else "Development render is ready."
                    if job.get("kind") == "development_render"
                    else "Guided photo treatment is ready."
                    if job.get("kind") == "development_pipeline"
                    else "Built-in and darktable comparison is ready."
                    if job.get("kind") == "renderer_comparison"
                    else "Full-resolution darktable files are ready to export."
                    if job.get("kind") == "renderer_export"
                    else "Image export is complete."
                    if job.get("kind") == "delivery_export"
                    else "Personal style profile is ready."
                    if job.get("kind") == "style_profile"
                    else "The treatment is warranted; it is on the develop page."
                    if job.get("kind") == "treatment"
                    else "Slider advice placed; it paints when the frame is next opened."
                    if job.get("kind") == "control_zones"
                    else f"{job.get('program', 'Your program')} finished; its output is beside its log."
                    if job.get("kind") == "kimiya_program"
                    else "Culling report is ready for review.",
                )
            elif requested == "cancelled":
                status, message = "cancelled", "Cancelled; checkpoint retained."
            elif job["status"] == "stopping":
                status, message = "paused", "Paused; resume will validate the checkpoint."
            elif (job.get("kind") == "treatment"
                    and "ABSTAINED" in self._log_tail(Path(job["log"]))):
                status, message = "failed", (
                    "The panel declined to vouch for the treatment. Every "
                    "round, render and measurement is kept under "
                    ".darkimiya/Treatments beside the photographs.")
            elif "PROVIDER REFUSED" in self._log_tail(Path(job["log"])):
                # Exhausted credit or failed authentication: no retry can
                # help, and every decision so far is checkpointed. Pausing
                # keeps the run resumable the moment the account is fixed.
                status, message = "paused", (
                    "Paused: the provider refused the account -- exhausted "
                    "credit or failed authentication. Fix the account, "
                    "then resume; everything done so far is kept.")
            else:
                status, message = "failed", f"Kimiya exited with status {exit_code}."
            job.update(
                status=status, message=message, exit_code=exit_code,
                pid=None, finished_at=_now())
            if status == "completed":
                self._register_completed_output(job)
                if job.get("project_registration_error"):
                    job["message"] += (
                        " The output is safe, but project registration needs "
                        "attention: " + job["project_registration_error"])
            self._process = None
            self._active_job_id = None
            self._save()
        if status == "failed":
            self._maybe_retry_certification(job)
        self._wake.set()

    def _maybe_retry_certification(self, job: dict[str, Any]) -> None:
        """One automatic fresh-panel retry when only the gate refused.

        A cull that finished its decisions and then abstained at
        certification carries a complete checkpoint; a judge is a
        stochastic instrument, and a fresh panel routinely approves the
        identical evidence. One retry, marked so it never loops: a real
        defect still fails, now with the refused evidence on disk.
        """
        if (
            job.get("kind", "culling") != "culling"
            or job.get("exit_code") != 2
            or job.get("auto_retry")
            or not Path(str(job.get("checkpoint") or "")).is_file()
            or Path(str(job.get("output") or "")).exists()
        ):
            return
        policy = job.get("judgment_policy") or {}
        try:
            self.add(
                job["photos"], job["output"], job.get("keep_per_group", 2),
                job.get("recursive", True), job.get("profile", "family"),
                job.get("provider_profile_id") or "",
                policy.get("panel"), policy.get("votes", 5),
                policy.get("required", 4),
                only_photos=list(job.get("only_photos") or []) or None,
            )
        except (JobError, ProviderError):
            # The refusal stands as recorded; retrying was best-effort.
            return
        with self._lock:
            job["auto_retried"] = True
            retried = self._state["jobs"][-1]
            retried["auto_retry"] = True
            retried["message"] = (
                "Certification was refused; asking a fresh panel once. "
                "Every decision is kept in the checkpoint.")
            self._save()

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)
