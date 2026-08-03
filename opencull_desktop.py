#!/usr/bin/env python3
"""Native macOS launcher and internal frozen-process entry points for Darkimiya."""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import webbrowser
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from opencull_gui import dialogs
from opencull_gui.faces import FaceStore, default_face_db
from opencull_gui.jobs import JobManager
from opencull_gui.macos import InstanceLock, MacOSPaths, resource_root
from opencull_gui.measurements import load_measurements
from opencull_gui.photos import PhotoStore
from opencull_gui.project import (
    ensure_project_layout,
    legacy_migration_preview,
    load_or_create_folder_project,
    migrate_legacy_project,
)
from opencull_gui.project_catalog import ProjectCatalog
from opencull_gui.providers import ProviderStore
from opencull_gui.report import load_report
from opencull_gui.reviews import ReviewStore, default_review_path
from opencull_gui.server import ReviewServer


def executable_command(*arguments: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, *arguments]
    return [sys.executable, str(Path(__file__).resolve()), *arguments]


def run_kimiya(arguments: Sequence[str]) -> int:
    checkout = Path(__file__).resolve().parent.parent / "kimiya-lang"
    if not getattr(sys, "frozen", False) and (checkout / "kimiya").is_dir():
        sys.path.insert(0, str(checkout))
    from kimiya.cli import main as kimiya_main

    previous = sys.argv
    try:
        sys.argv = ["kimiya", *arguments]
        result = kimiya_main()
        return int(result or 0)
    finally:
        sys.argv = previous


def run_development_pipeline(arguments: Sequence[str]) -> int:
    from development_pipeline import main as pipeline_main
    return int(pipeline_main(list(arguments)) or 0)


def run_comparison_pipeline(arguments: Sequence[str]) -> int:
    from comparison_pipeline import main as pipeline_main
    return int(pipeline_main(list(arguments)) or 0)


def run_renderer_export_pipeline(arguments: Sequence[str]) -> int:
    from renderer_export_pipeline import main as pipeline_main
    return int(pipeline_main(list(arguments)) or 0)


def run_delivery_export_pipeline(arguments: Sequence[str]) -> int:
    from delivery_export_pipeline import main as pipeline_main
    return int(pipeline_main(list(arguments)) or 0)


def run_review(
    report_path: Path, photos_path: Path, paths: MacOSPaths,
    no_browser: bool = False,
) -> int:
    server = make_review_server(report_path, photos_path, paths)
    url = f"http://127.0.0.1:{server.server_port}/"
    if not no_browser:
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def make_review_server(
    report_path: Path, photos_path: Path, paths: MacOSPaths,
    shortlist_path: Path | None = None,
    jobs: JobManager | None = None,
    providers: ProviderStore | None = None,
) -> ReviewServer:
    """Construct an isolated review server for a browser or native window."""
    report = load_report(report_path)
    cache = paths.cache / "previews"
    photos = PhotoStore(photos_path, cache)
    project_path, _project = load_or_create_folder_project(
        photos.root, report.path.stem,
        report.path.with_suffix(".opencull-project.json"))
    layout = ensure_project_layout(photos.root)
    review_path = (
        default_review_path(report.path)
        if project_path != layout["manifest"]
        else layout["Reviews"] / f"{report.path.stem}.review.json")
    reviews = ReviewStore(review_path, report, photos.root)
    measurements, manifest_path = load_measurements(None, report)
    providers = providers or ProviderStore(
        paths.providers, resource_root(),
        generated_root=paths.generated / "providers")
    faces = FaceStore(
        default_face_db(report.path), report, photos, reviews, paths.face_models)
    adjacent_shortlist = report.path.with_name(
        f"{report.path.stem}.professional-shortlist.json")
    persistent_shortlist = layout["Reports"] / (
        f"{report.path.stem}.professional-shortlist.json")
    detected_shortlist = shortlist_path or (
        persistent_shortlist if persistent_shortlist.is_file()
        else adjacent_shortlist)
    server = ReviewServer(
        ("127.0.0.1", 0), report, photos, reviews,
        measurements=measurements, manifest_path=manifest_path,
        jobs=jobs, providers=providers, faces=faces,
        shortlist_path=(
            detected_shortlist if detected_shortlist.is_file() else None),
        shortlist_default_path=persistent_shortlist,
        shutdown_jobs=jobs is None)
    return server


def run_release_smoke_test(output_path: Path) -> int:
    """Exercise release-only dependencies without launching a GUI or network."""
    root = resource_root()
    required = [
        root / "opencull.kim",
        root / "professional_shortlist.kim",
        root / "edit_suggestions.kim",
        root / "agents.kim",
        root / "scan.py",
        root / "opencull_kernel.py",
        root / "shortlist_kernel.py",
        root / "edit_suggestion_kernel.py",
        root / "opencull_gui" / "static" / "index.html",
        root / "opencull_gui" / "static" / "styles.css",
        root / "opencull_gui" / "static" / "app.js",
        root / ".opencull-models" / "face_detection_yunet_2023mar.onnx",
        root / ".opencull-models" / "face_recognition_sface_2021dec.onnx",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    checks: dict[str, object] = {
        "format": "opencull-macos-smoke-v1",
        "frozen": bool(getattr(sys, "frozen", False)),
        "architecture": os.uname().machine,
        "resource_root": str(root),
        "required_files": len(required),
        "missing_files": missing,
    }
    try:
        import cv2
        import numpy
        import PIL
        from PIL import Image

        checks["dependencies"] = {
            "opencv": cv2.__version__,
            "numpy": numpy.__version__,
            "pillow": PIL.__version__,
        }
        with tempfile.TemporaryDirectory(prefix="opencull-release-smoke-") as temporary:
            paths = MacOSPaths.create(Path(temporary))
            lock = InstanceLock(paths.support / "desktop.lock")
            first_lock = lock.acquire()
            second_lock = InstanceLock(paths.support / "desktop.lock")
            duplicate_rejected = not second_lock.acquire()
            second_lock.release()
            lock.release()
            providers = ProviderStore(
                paths.providers, root,
                generated_root=paths.generated / "providers")
            jobs = JobManager(
                paths.jobs, root, providers=providers,
                command_builder=lambda _job: ["/usr/bin/true"],
                program_checker=lambda _program: None,
                kimiya_workspace_root=paths.kimiya)
            queue_empty = jobs.public()["jobs"] == []
            jobs.shutdown()
            migration_source = Path(temporary) / "legacy-photos"
            migration_source.mkdir()
            migration_evidence = Path(temporary) / "legacy-report.json"
            migration_evidence.write_text('{"report":true}\n', encoding="utf-8")
            legacy_manifest = Path(temporary) / "legacy.opencull-project.json"
            legacy_manifest.write_text(json.dumps({
                "format": "opencull-project-v1", "id": "smoke-legacy",
                "source_folder": str(migration_source),
                "artifacts": {"culling_report": {"path": str(migration_evidence)}},
            }) + "\n", encoding="utf-8")
            legacy_bytes = legacy_manifest.read_bytes()
            migration_preview = legacy_migration_preview(
                legacy_manifest, migration_source)
            migration_result = migrate_legacy_project(
                legacy_manifest, migration_source,
                migration_preview["confirmation_code"])
            checks["project_migration"] = bool(
                migration_result["project"].get("format") == "darkimiya-project-v1"
                and migration_result["journal"].get("status") == "completed"
                and legacy_manifest.read_bytes() == legacy_bytes
                and Path(migration_result["path"]).is_file())
            from opencull_gui.project_catalog import ProjectCatalog
            catalog_source = Path(temporary) / "catalog-photos"
            catalog_source.mkdir()
            Image.new("RGB", (16, 12), "blue").save(
                catalog_source / "sample.jpg")
            catalog = ProjectCatalog(paths.support / "projects.json")
            catalog_project = catalog.add(str(catalog_source))["projects"][0]
            manual_report = catalog.manual_selection_report(catalog_project["id"])
            checks["project_catalog"] = bool(
                len(catalog.public()["projects"]) == 1
                and manual_report.is_file()
                and catalog.public()["projects"][0]["report_available"])
            checks["private_paths"] = all(
                directory.is_dir() and directory.stat().st_mode & 0o077 == 0
                for directory in (
                    paths.support, paths.cache, paths.logs, paths.results))
            checks["single_instance"] = first_lock and duplicate_rejected
            checks["provider_store"] = providers.public()["profiles"] == []
            checks["queue_store"] = queue_empty
        checks["kimiya_check_status"] = run_kimiya(
            ["check", str(root / "opencull.kim")])
        checks["professional_kimiya_check_status"] = run_kimiya(
            ["check", str(root / "professional_shortlist.kim")])
        checks["edit_suggestions_kimiya_check_status"] = run_kimiya(
            ["check", str(root / "edit_suggestions.kim")])
    except Exception as exc:
        checks["error"] = f"{type(exc).__name__}: {exc}"
    checks["passed"] = (
        not checks.get("missing_files")
        and checks.get("private_paths") is True
        and checks.get("single_instance") is True
        and checks.get("provider_store") is True
        and checks.get("queue_store") is True
        and checks.get("project_migration") is True
        and checks.get("project_catalog") is True
        and checks.get("kimiya_check_status") == 0
        and checks.get("professional_kimiya_check_status") == 0
        and checks.get("edit_suggestions_kimiya_check_status") == 0
        and "error" not in checks
    )
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(checks, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return 0 if checks["passed"] else 1


@dataclass
class DesktopServices:
    """The application's long-lived services, independent of any interface."""

    jobs: JobManager
    providers: ProviderStore
    projects: ProjectCatalog
    open_review: Callable[..., dict[str, object]]
    review_servers: list[ReviewServer]
    paths: MacOSPaths

    def diagnostics(self) -> dict[str, object]:
        return {
            "resource_root": str(resource_root()),
            "queue_path": str(self.paths.jobs),
            "provider_path": str(self.paths.providers),
            "log_path": str(self.paths.launcher_log),
            "results_path": str(self.paths.results),
            "frozen": bool(getattr(sys, "frozen", False)),
        }

    def close(self) -> None:
        for server in self.review_servers:
            server.shutdown()
            server.server_close()
        self.review_servers.clear()
        self.jobs.shutdown()


def build_desktop_services(paths: MacOSPaths) -> DesktopServices:
    """Construct the queue, providers, project library and review opener.

    Shared so the Qt launcher and the headless bridge wire the application
    the same way rather than each assembling their own.
    """
    providers = ProviderStore(
        paths.providers, resource_root(),
        generated_root=paths.generated / "providers")
    def build_job_command(job: dict) -> list[str]:
        if job.get("kind") == "delivery_export":
            command = executable_command(
                "--delivery-export-pipeline-worker",
                "--source", job["source"], "--reference", job["reference"],
                "--directions", job["directions"], "--photo", job["photo"],
                "--style", job["style"], "--engine", job["engine"],
                "--demosaic", job["demosaic"],
                "--render-output-dir", job["render_output_dir"],
                "--project", job["project"], "--destination", job["destination"],
                "--export-key", job["export_key"], "--receipt", job["output"])
            if job.get("render"):
                command.extend(["--render", job["render"]])
            return command
        if job.get("kind") == "renderer_export":
            return executable_command(
                "--renderer-export-pipeline-worker",
                "--source", job["source"], "--reference", job["reference"],
                "--directions", job["directions"], "--photo", job["photo"],
                "--style", job["style"], "--output-dir", job["output_dir"],
                "--project", job["project"], "--demosaic", job["demosaic"])
        if job.get("kind") == "renderer_comparison":
            return executable_command(
                "--comparison-pipeline-worker",
                "--source", job["source"], "--reference", job["reference"],
                "--directions", job["directions"], "--photo", job["photo"],
                "--style", job["style"], "--opencull-render", job["opencull_render"],
                "--output-dir", job["output_dir"], "--project", job["project"],
                "--demosaic", job["demosaic"])
        if job.get("kind") == "development_pipeline":
            return executable_command(
                "--development-pipeline-worker",
                "--source", job["source"], "--reference", job["reference"],
                "--directions", job["directions"], "--photo", job["photo"],
                "--style", job["style"], "--output-dir", job["output_dir"],
                "--project", job["project"])
        if job.get("kind") == "style_profile":
            return executable_command(
                "--kimiya-worker", "run",
                job.get("program_path")
                or str(resource_root() / "style_profile.kim"),
                f"photos={job['photos']}", f"output={job['output']}",
                f"existing={job.get('existing', '')}",
                f"mode={job.get('mode', 'update')}",
                f"limit={job.get('limit', 64)}")
        if job.get("kind") == "semantic_verification":
            return executable_command(
                "--kimiya-worker", "run",
                job.get("program_path")
                or str(resource_root() / "semantic_verification.kim"),
                f"original={job['original']}",
                f"developed={job['developed']}",
                f"thumbnail={job.get('thumbnail', '')}",
                f"suggestion={job['suggestion']}",
                f"consensus={'true' if job.get('consensus') else 'false'}",
                f"output={job['output']}")
        if job.get("kind") == "edit_suggestions":
            return executable_command(
                "--kimiya-worker", "run",
                job.get("program_path")
                or str(resource_root() / "edit_suggestions.kim"),
                f"shortlist={job['shortlist']}", f"review={job['review']}",
                f"photos={job['photos']}", f"output={job['output']}",
                f"profile={job['profile']}",
                f"style_profile={job.get('style_profile', '')}",
                f"only_photo={job.get('only_photo', '')}",
                f"only_photos={json.dumps(job.get('only_photos', []))}",
                "resume=true")
        if job.get("kind") == "professional_shortlist":
            return executable_command(
                "--kimiya-worker", "run",
                job.get("program_path")
                or str(resource_root() / "professional_shortlist.kim"),
                f"report={job['report']}", f"photos={job['photos']}",
                f"review={job.get('review', '')}", f"output={job['output']}",
                f"policy={job['policy']}", f"profile={job['profile']}",
                "resume=true")
        if job.get("kind") not in {None, "culling"}:
            raise ValueError(f"unsupported native job kind: {job.get('kind')}")
        return executable_command(
            "--kimiya-worker", "run",
            job.get("program_path") or str(resource_root() / "opencull.kim"),
            f"photos={job['photos']}", f"output={job['output']}",
            f"keep_per_group={job['keep_per_group']}",
            f"recursive={'true' if job['recursive'] else 'false'}",
            f"profile={job['profile']}", "resume=true")

    jobs = JobManager(
        paths.jobs, resource_root(), providers=providers,
        command_builder=build_job_command,
        program_checker=lambda program: _check_native_program(program),
        kimiya_workspace_root=paths.kimiya,
        output_root=paths.results)
    projects = ProjectCatalog(paths.support / "projects.json")
    projects.import_jobs(jobs.public())

    review_servers: list[ReviewServer] = []

    def open_review(
        report: Path, photos: Path, shortlist: Path | None = None
    ) -> dict[str, object]:
        server = make_review_server(
            report, photos, paths, shortlist, jobs, providers)
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.25},
            name=f"opencull-review-{report.stem}",
            daemon=True)
        thread.start()
        review_servers.append(server)
        return {
            "url": f"http://127.0.0.1:{server.server_port}/",
            "title": (
                f"{report.stem.removesuffix('-results')} · Professional"
                if shortlist else
                report.stem.removesuffix("-results") or photos.name),
        }

    return DesktopServices(
        jobs=jobs, providers=providers, projects=projects,
        open_review=open_review, review_servers=review_servers, paths=paths)


def run_native_server(
    paths: MacOSPaths, present: Callable[[str], None] | None = None,
) -> int:
    """Run the queue service, either headless or behind a window we present."""
    from opencull_gui.desktop_server import serve_desktop_bridge

    services = build_desktop_services(paths)
    try:
        return serve_desktop_bridge(
            services.jobs, services.providers, services.open_review,
            diagnostics=services.diagnostics(), projects=services.projects,
            present=present)
    finally:
        services.close()


def run_launcher(
    paths: MacOSPaths, diagnose: bool = False, screen: str = "",
) -> int:
    """Open the native launcher and run until its window closes."""
    import opencull_qt

    return opencull_qt.run(paths, diagnose=diagnose, screen=screen)


def _check_native_program(program: Path) -> None:
    result = subprocess.run(
        executable_command("--kimiya-worker", "check", str(program)),
        capture_output=True, text=True, timeout=30)
    if result.returncode:
        from opencull_gui.jobs import JobError
        raise JobError(
            (result.stderr or result.stdout or "Kimiya check failed").strip())


class DesktopApp:
    def __init__(
        self, paths: MacOSPaths, finder_items: Sequence[str] = (),
    ):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.paths = paths
        self.root = tk.Tk()
        self.root.title("Darkimiya")
        self.root.geometry("920x620")
        self.root.minsize(760, 480)
        self.status = tk.StringVar(value="Ready")
        self.recovery_status = tk.StringVar(value="No interrupted jobs")
        self.folder = tk.StringVar()
        self.keep = tk.IntVar(value=2)
        self.profile = tk.StringVar(value="family")
        self.recursive = tk.BooleanVar(value=False)
        self.provider = tk.StringVar()
        self.providers = ProviderStore(
            paths.providers, resource_root(),
            generated_root=paths.generated / "providers")
        self.jobs = JobManager(
            paths.jobs, resource_root(), providers=self.providers,
            command_builder=self._worker_command,
            program_checker=self._check_program,
            kimiya_workspace_root=paths.kimiya)
        self._build()
        self._refresh()
        if finder_items:
            self.root.after(100, lambda: self.open_finder_items(finder_items))
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _worker_command(self, job: dict) -> list[str]:
        return executable_command(
            "--kimiya-worker", "run",
            job.get("program_path") or str(resource_root() / "opencull.kim"),
            f"photos={job['photos']}", f"output={job['output']}",
            f"keep_per_group={job['keep_per_group']}",
            f"recursive={'true' if job['recursive'] else 'false'}",
            f"profile={job['profile']}", "resume=true")

    def _check_program(self, program: Path) -> None:
        result = subprocess.run(
            executable_command("--kimiya-worker", "check", str(program)),
            capture_output=True, text=True, timeout=30)
        if result.returncode:
            from opencull_gui.jobs import JobError
            raise JobError(
                (result.stderr or result.stdout or "Kimiya check failed").strip())

    def _build(self) -> None:
        from tkinter import ttk

        outer = ttk.Frame(self.root, padding=20)
        outer.pack(fill="both", expand=True)
        ttk.Label(
            outer, text="Darkimiya", font=("Helvetica Neue", 26, "bold")
        ).pack(anchor="w")
        ttk.Label(
            outer,
            text="Private, inspectable photo culling · source photographs stay untouched",
        ).pack(anchor="w", pady=(0, 18))

        if not self.paths.onboarding_marker.exists():
            self.welcome = ttk.LabelFrame(
                outer, text="Welcome · your first trustworthy result", padding=14)
            self.welcome.pack(fill="x", pady=(0, 14))
            ttk.Label(
                self.welcome,
                text=(
                    "1  Choose a photo folder     "
                    "2  Configure a model provider     "
                    "3  Review keepers before export"
                ),
                font=("Helvetica Neue", 12, "bold"),
            ).pack(anchor="w")
            ttk.Label(
                self.welcome,
                text=(
                    "The OpenCull engine never changes source photographs during culling. "
                    "Provider credentials stay in the system credential store."
                ),
            ).pack(anchor="w", pady=(6, 8))
            welcome_actions = ttk.Frame(self.welcome)
            welcome_actions.pack(fill="x")
            ttk.Button(
                welcome_actions, text="Choose photo folder",
                command=self.choose_folder).pack(side="left")
            ttk.Button(
                welcome_actions, text="Configure provider",
                command=self.configure_provider).pack(side="left", padx=8)
            ttk.Button(
                welcome_actions, text="Dismiss guide",
                command=self.dismiss_onboarding).pack(side="right")

        form = ttk.LabelFrame(outer, text="New culling job", padding=14)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text="Photo folder").grid(row=0, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.folder).grid(
            row=0, column=1, sticky="ew", padx=8)
        ttk.Button(form, text="Choose…", command=self.choose_folder).grid(
            row=0, column=2)
        ttk.Label(form, text="At most").grid(row=1, column=0, sticky="w", pady=10)
        ttk.Spinbox(form, from_=1, to=20, textvariable=self.keep, width=5).grid(
            row=1, column=1, sticky="w", padx=8)
        ttk.Label(form, text="Profile").grid(row=2, column=0, sticky="w")
        ttk.Combobox(
            form, textvariable=self.profile,
            values=("family", "professional", "balanced"),
            state="readonly", width=18).grid(row=2, column=1, sticky="w", padx=8)
        ttk.Checkbutton(
            form, text="Include nested folders",
            variable=self.recursive).grid(row=3, column=1, sticky="w", padx=8)
        ttk.Label(form, text="Provider").grid(row=4, column=0, sticky="w", pady=10)
        self.provider_box = ttk.Combobox(
            form, textvariable=self.provider, state="readonly")
        self.provider_box.grid(row=4, column=1, sticky="ew", padx=8)
        ttk.Button(
            form, text="Configure…", command=self.configure_provider).grid(
                row=4, column=2)
        ttk.Button(form, text="Add to queue", command=self.add_job).grid(
            row=5, column=1, sticky="w", padx=8)

        heading = ttk.Frame(outer)
        heading.pack(fill="x", pady=(18, 6))
        ttk.Label(
            heading, text="Culling queue", font=("Helvetica Neue", 16, "bold")
        ).pack(side="left")
        ttk.Label(heading, textvariable=self.recovery_status).pack(
            side="left", padx=14)
        ttk.Button(
            heading, text="Open existing result…",
            command=self.open_existing).pack(side="right")
        columns = ("status", "folder", "profile", "message")
        self.queue = ttk.Treeview(
            outer, columns=columns, show="headings", selectmode="browse")
        for name, width in zip(columns, (90, 270, 100, 330)):
            self.queue.heading(name, text=name.title())
            self.queue.column(name, width=width, minwidth=60)
        self.queue.pack(fill="both", expand=True)
        self.queue.bind("<Double-1>", lambda _event: self.open_selected())
        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(
            buttons, text="Open selected result",
            command=self.open_selected).pack(side="left")
        ttk.Button(
            buttons, text="Pause / resume",
            command=self.toggle_selected).pack(side="left", padx=8)
        ttk.Button(
            buttons, text="Reveal in Finder",
            command=self.reveal_selected).pack(side="left")
        ttk.Label(buttons, textvariable=self.status).pack(side="right")

    def dismiss_onboarding(self) -> None:
        try:
            self.paths.onboarding_marker.write_text(
                "Darkimiya onboarding completed.\n", encoding="utf-8")
            os.chmod(self.paths.onboarding_marker, 0o600)
        except OSError as exc:
            self.status.set(f"Could not save onboarding preference: {exc}")
            return
        if hasattr(self, "welcome"):
            self.welcome.destroy()

    def choose_folder(self) -> None:
        from tkinter import filedialog

        chosen = filedialog.askdirectory(mustexist=True)
        if chosen:
            self.folder.set(chosen)

    def _provider_choices(self) -> tuple[list[str], dict[str, str]]:
        profiles = self.providers.public()["profiles"]
        labels = ([] if getattr(sys, "frozen", False) else ["Legacy agents.kim"]) + [
            f"{item['name']} · {item['kind']}" for item in profiles]
        mapping = (
            {} if getattr(sys, "frozen", False)
            else {"Legacy agents.kim": ""})
        mapping.update({
            f"{item['name']} · {item['kind']}": item["id"]
            for item in profiles})
        return labels, mapping

    def configure_provider(self) -> None:
        tk, ttk = self.tk, self.ttk
        dialog = tk.Toplevel(self.root)
        dialog.title("New model provider")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)
        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        # Edit the existing profile rather than always composing a new one.
        # Without this the dialog had no way to reach a saved profile, so each
        # save appended another copy and a stored credential was invisible.
        state = self.providers.public()
        existing = state["profiles"][0] if state["profiles"] else None
        stored = bool(existing and existing.get("credential") == "stored")
        kind = tk.StringVar(value=existing["kind"] if existing else "openrouter")
        name = tk.StringVar(value=existing["name"] if existing else "OpenRouter")
        endpoint = tk.StringVar(
            value=existing["endpoint"] if existing and existing["kind"] != "openrouter"
            else "")
        secret = tk.StringVar(value="")
        defaults = {
            "A": "openai/gpt-5.6-luna-pro",
            "B": "openai/gpt-5.6-luna-pro",
            "C": "openai/gpt-4.1-mini",
            "D": "openai/gpt-5.6-luna-pro",
        }
        if existing:
            defaults = {**defaults, **existing.get("models", {})}
        models = {agent: tk.StringVar(value=model) for agent, model in defaults.items()}
        ttk.Label(
            frame, text="Provider configuration",
            font=("Helvetica Neue", 16, "bold")).grid(
                row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        fields = [
            ("Name", name),
            ("Endpoint", endpoint),
            ("Credential", secret),
        ]
        ttk.Label(frame, text="Kind").grid(row=1, column=0, sticky="w")
        kind_box = ttk.Combobox(
            frame, textvariable=kind,
            values=("openrouter", "ollama", "openai"), state="readonly")
        kind_box.grid(row=1, column=1, sticky="ew", padx=(10, 0))
        for row, (label, variable) in enumerate(fields, start=2):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w")
            entry = ttk.Entry(
                frame, textvariable=variable,
                show="•" if label == "Credential" else "")
            entry.grid(row=row, column=1, sticky="ew", padx=(10, 0), pady=3)
            if label == "Credential" and stored:
                # Tk has no placeholder, so the stored state is shown beside
                # the field. It is a label rather than entry text, which could
                # be submitted and would replace a working key with bullets.
                ttk.Label(
                    frame, text="•" * 20 + "  stored — leave blank to keep",
                    foreground="#2f8f5b").grid(
                        row=row + 1, column=1, sticky="w", padx=(10, 0))
        row = 6 if stored else 5
        for agent in ("A", "B", "C", "D"):
            ttk.Label(frame, text=f"Agent {agent} model").grid(
                row=row, column=0, sticky="w")
            ttk.Entry(frame, textvariable=models[agent], width=46).grid(
                row=row, column=1, sticky="ew", padx=(10, 0), pady=3)
            row += 1
        storage = state.get("credential_storage") or {}
        where = storage.get("label", "the system credential store")
        notice = ttk.Label(
            frame,
            text=(
                f"Credentials are written to {where} and never saved in settings."
                + ("" if storage.get("protected", True)
                   else " No system keyring was found.")
            ),
            wraplength=480)
        notice.grid(row=row, column=0, columnspan=2, sticky="w", pady=(10, 8))

        def save() -> None:
            try:
                provider_kind = kind.get()
                profile_value = {
                    "name": name.get().strip(),
                    "kind": provider_kind,
                    "endpoint": endpoint.get().strip(),
                    "models": {
                        agent: value.get().strip()
                        for agent, value in models.items()},
                    "credential_required": (
                        provider_kind == "openrouter"
                        or bool(secret.get()) or stored),
                    "zdr": provider_kind == "openrouter",
                    "cost_note": "",
                }
                # Updating in place keeps the stored credential when the field
                # is left blank; omitting the id appended a new profile whose
                # credential was missing.
                if existing:
                    profile_value["id"] = existing["id"]
                result = self.providers.save(
                    profile_value, self.providers.public()["revision"],
                    secret.get())
                profile = next(
                    (item for item in result["profiles"]
                     if item["id"] == profile_value.get("id")),
                    result["profiles"][-1])
                self.provider.set(f"{profile['name']} · {profile['kind']}")
                self.status.set(f"Provider saved; credential is in {where}.")
                dialog.destroy()
                self._refresh()
            except Exception as exc:
                notice.configure(text=str(exc))

        controls = ttk.Frame(frame)
        controls.grid(row=row + 1, column=0, columnspan=2, sticky="e")
        ttk.Button(controls, text="Cancel", command=dialog.destroy).pack(
            side="left", padx=6)
        ttk.Button(controls, text="Save", command=save).pack(side="left")

    def add_job(self) -> None:
        try:
            _, mapping = self._provider_choices()
            if self.provider.get() not in mapping:
                raise ValueError(
                    "Configure and select a model provider before adding a job.")
            output = self._next_output(Path(self.folder.get()))
            self.jobs.add(
                self.folder.get(), output=str(output),
                keep_per_group=self.keep.get(),
                recursive=self.recursive.get(), profile=self.profile.get(),
                provider_profile_id=mapping.get(self.provider.get(), ""))
            self.status.set("Job added. Culling starts sequentially.")
        except Exception as exc:
            self._error(str(exc))
        self._refresh()

    def _next_output(self, photos: Path) -> Path:
        clean = "".join(
            character if character.isalnum() or character in "-_"
            else "_" for character in photos.name).strip("_") or "photos"
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        candidate = self.paths.results / f"{clean}-{stamp}-results.json"
        number = 2
        while candidate.exists():
            candidate = self.paths.results / (
                f"{clean}-{stamp}-results-{number}.json")
            number += 1
        return candidate

    def _selected_job(self) -> dict | None:
        selected = self.queue.selection()
        if not selected:
            return None
        job_id = selected[0]
        return next(
            (job for job in self.jobs.public()["jobs"] if job["id"] == job_id),
            None)

    def open_selected(self) -> None:
        job = self._selected_job()
        if not job or not Path(job["output"]).is_file():
            self.status.set("Select a completed job first.")
            return
        self._spawn_review(Path(job["output"]), Path(job["photos"]))

    def open_existing(self, initial_report: str = "") -> None:
        from tkinter import filedialog

        report = initial_report or filedialog.askopenfilename(
            title="Choose a Darkimiya culling result",
            filetypes=(("Darkimiya culling JSON", "*.json"),))
        if not report:
            return
        photos = filedialog.askdirectory(
            title="Choose the corresponding photo folder", mustexist=True)
        if photos:
            self._spawn_review(Path(report), Path(photos))

    def open_finder_items(self, items: Sequence[str]) -> None:
        paths = [Path(item).expanduser().resolve() for item in items]
        folder = next((item for item in paths if item.is_dir()), None)
        report = next(
            (item for item in paths if item.is_file()
             and item.suffix.lower() == ".json"), None)
        if folder and report:
            self._spawn_review(report, folder)
        elif folder:
            self.folder.set(str(folder))
            self.status.set("Folder received from Finder. Review settings, then add it.")
        elif report:
            self.open_existing(str(report))

    def _spawn_review(self, report: Path, photos: Path) -> None:
        subprocess.Popen(executable_command(
            "--review-window", str(report), str(photos)),
            start_new_session=True)
        self.status.set(f"Opening {report.name}")

    def toggle_selected(self) -> None:
        job = self._selected_job()
        if not job:
            return
        try:
            action = (
                "pause" if job["status"] in {"running", "queued"}
                else "resume" if job["status"] == "paused" else "")
            if action:
                self.jobs.action(job["id"], action)
        except Exception as exc:
            self._error(str(exc))
        self._refresh()

    def reveal_selected(self) -> None:
        job = self._selected_job()
        if not job:
            return
        from tkinter import messagebox

        target = Path(job["output"]) if Path(job["output"]).exists() else Path(job["photos"])
        try:
            dialogs.reveal(target)
        except dialogs.DialogError as exc:
            messagebox.showerror("Darkimiya", str(exc))

    def _refresh(self) -> None:
        labels, _mapping = self._provider_choices()
        self.provider_box["values"] = labels
        if self.provider.get() not in labels:
            self.provider.set(labels[0] if labels else "")
        selected = self.queue.selection()
        self.queue.delete(*self.queue.get_children())
        jobs = self.jobs.public()["jobs"]
        attention = [
            job for job in jobs
            if job["status"] in {"failed", "paused", "detached"}
            or not Path(job["photos"]).is_dir()
        ]
        self.recovery_status.set(
            f"{len(attention)} job{'s' if len(attention) != 1 else ''} need recovery"
            if attention else "No interrupted jobs")
        for job in jobs:
            self.queue.insert(
                "", "end", iid=job["id"],
                values=(
                    job["status"], Path(job["photos"]).name, job["profile"],
                    job.get("message", "")))
        if selected and self.queue.exists(selected[0]):
            self.queue.selection_set(selected[0])
        self.root.after(1000, self._refresh)

    def _error(self, message: str) -> None:
        from tkinter import messagebox

        self.status.set(message)
        messagebox.showerror("Darkimiya", message)

    def close(self) -> None:
        self.jobs.shutdown()
        self.root.destroy()

    def run(self) -> int:
        self.root.mainloop()
        return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kimiya-worker", nargs=argparse.REMAINDER)
    parser.add_argument("--development-pipeline-worker", nargs=argparse.REMAINDER)
    parser.add_argument("--comparison-pipeline-worker", nargs=argparse.REMAINDER)
    parser.add_argument("--renderer-export-pipeline-worker", nargs=argparse.REMAINDER)
    parser.add_argument("--delivery-export-pipeline-worker", nargs=argparse.REMAINDER)
    parser.add_argument("--review-window", nargs=2, metavar=("REPORT", "PHOTOS"))
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--release-smoke-test", metavar="REPORT_PATH")
    parser.add_argument("--native-server", action="store_true")
    parser.add_argument(
        "--screen", default="",
        help="open on a named monitor, for example eDP-1 (default: primary)")
    parser.add_argument(
        "--diagnose", action="store_true",
        help="report the display, screens and window placement at startup")
    parser.add_argument(
        "--tk-launcher", action="store_true",
        help="use the previous Tk launcher instead of the application window")
    parser.add_argument("finder_items", nargs="*")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.kimiya_worker is not None:
        return run_kimiya(args.kimiya_worker)
    if args.development_pipeline_worker is not None:
        return run_development_pipeline(args.development_pipeline_worker)
    if args.comparison_pipeline_worker is not None:
        return run_comparison_pipeline(args.comparison_pipeline_worker)
    if args.renderer_export_pipeline_worker is not None:
        return run_renderer_export_pipeline(args.renderer_export_pipeline_worker)
    if args.delivery_export_pipeline_worker is not None:
        return run_delivery_export_pipeline(args.delivery_export_pipeline_worker)
    if args.release_smoke_test:
        return run_release_smoke_test(Path(args.release_smoke_test))
    paths = MacOSPaths.create()
    logging.basicConfig(
        filename=paths.launcher_log, level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s")
    if args.review_window:
        return run_review(
            Path(args.review_window[0]), Path(args.review_window[1]), paths,
            no_browser=args.no_browser)
    if args.native_server:
        lock = InstanceLock(paths.support / "desktop.lock")
        if not lock.acquire():
            logging.info("Darkimiya desktop is already running")
            print(json.dumps({
                "format": "opencull-native-bootstrap-v1",
                "error": (
                    "Darkimiya is already running. Quit the existing Darkimiya "
                    "window with Command-Q, then launch it once."
                ),
            }), flush=True)
            return 2
        try:
            return run_native_server(paths)
        finally:
            lock.release()
    lock = InstanceLock(paths.support / "desktop.lock")
    if not lock.acquire():
        logging.info("Darkimiya desktop is already running")
        print(
            "Darkimiya is already running. Its window may be on another "
            "screen or workspace; close it before starting a second copy.",
            flush=True)
        return 0
    try:
        if args.tk_launcher:
            return DesktopApp(paths, args.finder_items).run()
        return run_launcher(
            paths, diagnose=args.diagnose, screen=args.screen or "")
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
