"""Persistent sequential culling queue with supervised Kimiya subprocesses."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .providers import ProviderError, ProviderStore


QUEUE_FORMAT = "opencull-job-queue-v1"
ACTIVE = {"running", "stopping", "detached"}
TERMINAL = {"completed", "failed", "cancelled"}
PROFILES = {"family", "professional", "balanced"}


class JobError(ValueError):
    """A requested queue transition or path is invalid."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _pid_alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


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

    def _recover(self) -> None:
        changed = False
        for job in self._state["jobs"]:
            if job.get("status") in ACTIVE:
                if Path(job["output"]).is_file():
                    job.update(status="completed", finished_at=_now(), pid=None)
                elif _pid_alive(job.get("pid")):
                    job.update(
                        status="detached",
                        message=(
                            "Culling process survived the GUI restart; "
                            "monitoring without launching another job."
                        ),
                    )
                else:
                    job.update(
                        status="paused", pid=None,
                        message="GUI restarted; resume from the validated checkpoint.",
                    )
                changed = True
        if changed:
            self._save()

    def _command(self, job: dict[str, Any]) -> list[str]:
        return [
            self.python, "-m", "kimiya", "run",
            job.get("program_path") or str(self.project_root / "opencull.kim"),
            f"photos={job['photos']}",
            f"output={job['output']}",
            f"keep_per_group={job['keep_per_group']}",
            f"recursive={'true' if job['recursive'] else 'false'}",
            f"profile={job['profile']}",
            "resume=true",
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
        chosen_output = (
            self._validate_output(Path(output))
            if str(output).strip()
            else self._default_output(source)
        )
        with self._lock:
            if any(
                Path(job["photos"]) == source
                and job.get("status") not in TERMINAL
                for job in self._state["jobs"]
            ):
                raise JobError("this folder is already queued")
            job_id = uuid.uuid4().hex[:12]
            provider_bundle = None
            if provider_profile_id:
                if self.providers is None:
                    raise JobError("provider profiles are disabled")
                try:
                    provider_bundle = self.providers.materialize(
                        job_id, provider_profile_id)
                    self.program_checker(Path(provider_bundle["program_path"]))
                except (ProviderError, JobError) as exc:
                    raise JobError(str(exc)) from exc
            log = chosen_output.with_suffix(chosen_output.suffix + ".log")
            checkpoint = Path(str(chosen_output) + ".checkpoint.json")
            job = {
                "id": job_id,
                "photos": str(source),
                "output": str(chosen_output),
                "checkpoint": str(checkpoint),
                "log": str(log),
                "keep_per_group": keep,
                "recursive": bool(recursive),
                "profile": profile,
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

    def _default_output(self, source: Path) -> Path:
        base = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in source.name
        ).strip("_") or "photos"
        candidate = self.output_root / f"{base}-results.json"
        number = 2
        while candidate.exists() or any(
            Path(job["output"]) == candidate for job in self._state["jobs"]
        ):
            candidate = self.output_root / f"{base}-results-{number}.json"
            number += 1
        return candidate.resolve()

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        kimiya_checkout = self.project_root.parent / "kimiya-lang"
        if (kimiya_checkout / "kimiya").is_dir():
            existing_pythonpath = environment.get("PYTHONPATH", "")
            environment["PYTHONPATH"] = os.pathsep.join(
                part for part in [
                    str(kimiya_checkout), existing_pythonpath
                ] if part
            )
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
                if status != "running" or job_id != self._active_job_id:
                    raise JobError("only the currently running job can be paused")
                job.update(status="stopping", message="Pausing after process shutdown.")
                self._save()
                assert self._process is not None
                self._process.terminate()
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
            process = subprocess.Popen(
                [
                    self.python,
                    str(self.project_root / "gui.py"),
                    job["output"],
                    job["photos"],
                    "--port", "0",
                    "--no-job-manager",
                ],
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

    @staticmethod
    def _progress(job: dict[str, Any]) -> dict[str, Any]:
        checkpoint = Path(job["checkpoint"])
        completed = total = 0
        checkpoint_complete = False
        if checkpoint.is_file():
            try:
                data = json.loads(checkpoint.read_text(encoding="utf-8"))
                completed = len(data.get("decisions", []))
                total = len(data.get("signature", {}).get("cluster_ids", []))
                checkpoint_complete = bool(data.get("completed"))
            except (OSError, json.JSONDecodeError, TypeError):
                pass
        return {
            "completed_clusters": completed,
            "total_clusters": total,
            "fraction": completed / total if total else 0,
            "checkpoint_complete": checkpoint_complete,
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
            command = self.command_builder(deepcopy(job))
            Path(job["log"]).parent.mkdir(parents=True, exist_ok=True)
            log_handle = Path(job["log"]).open("a", encoding="utf-8")
            log_handle.write(f"\n[{_now()}] starting supervised command\n")
            log_handle.flush()
            try:
                environment = self._environment()
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
            job.update(
                status="running", pid=process.pid, started_at=_now(),
                finished_at=None, message="Kimiya culling is running.")
            self._save()
        exit_code = process.wait()
        log_handle.close()
        with self._lock:
            job = self._job(job_id)
            requested = job.pop("_stop_as", None)
            if Path(job["output"]).is_file() and exit_code == 0:
                status, message = "completed", "Culling report is ready for review."
            elif requested == "cancelled":
                status, message = "cancelled", "Cancelled; checkpoint retained."
            elif job["status"] == "stopping":
                status, message = "paused", "Paused; resume will validate the checkpoint."
            else:
                status, message = "failed", f"Kimiya exited with status {exit_code}."
            job.update(
                status=status, message=message, exit_code=exit_code,
                pid=None, finished_at=_now())
            self._process = None
            self._active_job_id = None
            self._save()
        self._wake.set()

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)
