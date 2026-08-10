import http.client
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import numpy as np
import tifffile
from PIL import Image

from delivery_export_pipeline import run_delivery_export
from opencull_desktop import run_release_smoke_test
from opencull_gui.actions import (
    ActionController,
    ActionError,
    Operation,
    build_plan,
    create_contact_sheets,
    default_journal_path,
    export_bytes,
    policy_clusters,
)
from opencull_gui.development import DevelopmentWorkspace
from opencull_gui.faces import FaceError, FaceStore
from opencull_gui.jobs import JobError, JobManager
from opencull_gui.macos import APP_NAME, InstanceLock, MacOSPaths
from opencull_gui.measurements import ManifestError, load_measurements
from opencull_gui.photos import PhotoError, PhotoStore, PreviewManager
from opencull_gui.project import (
    ensure_project_layout,
    load_or_create_folder_project,
    load_project,
    register_render,
    update_project,
)
from opencull_gui.providers import ProviderError, ProviderStore
from opencull_gui.raw_sources import RawSourceStore
from opencull_gui.report import ReportError, load_report
from opencull_gui.reviews import ReviewError, ReviewStore
from opencull_gui.server import ReviewServer
from opencull_gui.xmp import xmp_zip


def development_workspace(photos, entries=(), raw_root="", raw_matches=None,
                          decoders=frozenset()):
    """A develop stage over a real project, with no server around it.

    Edit directions arrive from the shortlist, which the develop stage does
    not own, so they are supplied here rather than produced.
    """
    project_path, _ = load_or_create_folder_project(photos, "Trip")
    public = {
        "configured": bool(raw_root), "root": str(raw_root),
        "matches": dict(raw_matches or {}),
    }
    return DevelopmentWorkspace(
        project_path, ensure_project_layout(photos),
        type("RawSources", (), {"public": lambda _self: public})(),
        directions=lambda: {
            "available": bool(entries), "path": "",
            "directions": {"entries": list(entries)}},
        decoders=set(decoders))


def report_data(names=("A.JPG", "B.JPG")):
    return {
        "format": "opencull-report-v2",
        "manifest_sha256": "manifest",
        "clusters": [{"cluster_id": "group-0001", "photos": list(names)}],
        "keep": [{
            "cluster_id": "group-0001",
            "photos": [names[0]],
            "rationale": "best expression",
            "confidence": 0.9,
            "warning": "",
            "fallback": False,
            "photographic_assessment": [],
        }],
        "warnings": [],
        "adaptive_clustering": {"enabled": False},
        "notice": "read only",
    }


class GuiReportTests(unittest.TestCase):
    def write_report(self, root: Path, data=None):
        path = root / "report.json"
        path.write_text(json.dumps(data or report_data()), encoding="utf-8")
        return path

    def test_validates_and_indexes_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = load_report(self.write_report(root))
            self.assertEqual(report.photo_names, ("A.JPG", "B.JPG"))
            self.assertEqual(
                report.decision_by_id["group-0001"]["photos"], ["A.JPG"])

    def test_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ReportError, "unsafe"):
                load_report(self.write_report(
                    root, report_data(("../outside.JPG", "B.JPG"))))

    def test_rejects_unknown_keeper(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = report_data()
            data["keep"][0]["photos"] = ["UNKNOWN.JPG"]
            with self.assertRaisesRegex(ReportError, "unknown selected"):
                load_report(self.write_report(root, data))

    def test_indexes_1500_photo_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            names = [f"P{number:04d}.JPG" for number in range(1500)]
            data = {
                "format": "opencull-report-v2",
                "manifest_sha256": "large",
                "clusters": [
                    {"cluster_id": f"group-{number:04d}", "photos": [name]}
                    for number, name in enumerate(names, start=1)
                ],
                "keep": [
                    {
                        "cluster_id": f"group-{number:04d}",
                        "photos": [name],
                        "rationale": "singleton",
                    }
                    for number, name in enumerate(names, start=1)
                ],
                "warnings": [],
            }
            report = load_report(self.write_report(root, data))
            self.assertEqual(len(report.photo_names), 1500)
            self.assertEqual(len(report.cluster_by_id), 1500)


class GuiPhotoTests(unittest.TestCase):
    def test_creates_and_reuses_thumbnail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            Image.new("RGB", (1200, 800), "orange").save(photos / "A.JPG")
            store = PhotoStore(photos, root / "cache")
            first = store.preview("A.JPG", "thumb")
            first_mtime = first.stat().st_mtime_ns
            second = store.preview("A.JPG", "thumb")
            self.assertEqual(first, second)
            self.assertEqual(first_mtime, second.stat().st_mtime_ns)
            with Image.open(first) as image:
                self.assertLessEqual(max(image.size), 520)

    def test_refuses_escape_and_missing_photo(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            store = PhotoStore(photos, root / "cache")
            with self.assertRaises(PhotoError):
                store.resolve("../secret.JPG")
            with self.assertRaisesRegex(PhotoError, "missing"):
                store.resolve("missing.JPG")

    def test_corrupt_cache_is_regenerated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            Image.new("RGB", (200, 100), "purple").save(photos / "A.JPG")
            store = PhotoStore(photos, root / "cache")
            cached = store.preview("A.JPG", "thumb")
            cached.write_bytes(b"not a jpeg")
            self.assertIsNone(store.cached_preview("A.JPG", "thumb"))
            regenerated = store.generate_preview("A.JPG", "thumb")
            with Image.open(regenerated) as image:
                self.assertEqual(image.format, "JPEG")

    def test_external_drive_disconnect_and_reconnect(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            Image.new("RGB", (80, 60), "red").save(photos / "A.JPG")
            store = PhotoStore(photos, root / "cache")
            disconnected = root / "disconnected"
            photos.rename(disconnected)
            with self.assertRaisesRegex(PhotoError, "reconnect"):
                store.resolve("A.JPG")
            disconnected.rename(photos)
            self.assertTrue(store.preview("A.JPG", "thumb").is_file())


class RawSourceStoreTests(unittest.TestCase):
    def test_matches_external_raws_by_case_insensitive_filename_stem(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_path = root / "shoot-results.json"
            report_path.write_text(
                json.dumps(report_data(("nested/DSCF0001.JPG", "DSCF0002.jpg"))),
                encoding="utf-8",
            )
            raw_root = root / "raw-originals"
            (raw_root / "card-a").mkdir(parents=True)
            (raw_root / "card-a" / "dscf0001.RAF").write_bytes(b"raw")
            (raw_root / "DSCF9999.RAF").write_bytes(b"unrelated")
            store = RawSourceStore(
                root / "shoot-results.raw-source.json",
                load_report(report_path),
            )

            state = store.configure(raw_root)

            self.assertEqual(
                state["matches"]["nested/DSCF0001.JPG"],
                ["card-a/dscf0001.RAF"],
            )
            self.assertEqual(state["matches"]["DSCF0002.jpg"], [])
            self.assertEqual(state["summary"]["matched"], 1)
            self.assertEqual(state["summary"]["missing"], 1)

    def test_persists_folder_and_reports_ambiguous_stem_matches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_path = root / "shoot-results.json"
            report_path.write_text(
                json.dumps(report_data(("A.JPG", "B.JPG"))),
                encoding="utf-8",
            )
            report = load_report(report_path)
            raw_root = root / "raw-originals"
            (raw_root / "one").mkdir(parents=True)
            (raw_root / "two").mkdir()
            (raw_root / "one" / "A.RAF").write_bytes(b"one")
            (raw_root / "two" / "A.dng").write_bytes(b"two")
            settings = root / "shoot-results.raw-source.json"

            RawSourceStore(settings, report).configure(raw_root)
            restored = RawSourceStore(settings, report).public()

            self.assertEqual(restored["summary"]["ambiguous"], 1)
            self.assertEqual(len(restored["matches"]["A.JPG"]), 2)
            self.assertEqual(restored["root"], str(raw_root.resolve()))


class FakePhotoStore:
    SIZES = {"thumb": 520, "detail": 2400}

    def __init__(self):
        self.ready = set()
        self.started = []
        self.release = threading.Event()
        self.active = 0
        self.peak = 0
        self.lock = threading.Lock()

    def cached_preview(self, name, size):
        return Path("/cached") if (name, size) in self.ready else None

    def generate_preview(self, name, size):
        with self.lock:
            self.started.append(name)
            self.active += 1
            self.peak = max(self.peak, self.active)
        self.release.wait(timeout=2)
        self.ready.add((name, size))
        with self.lock:
            self.active -= 1
        return Path("/cached")

    def cache_stats(self):
        return {"files": len(self.ready), "bytes": len(self.ready) * 100}

    def clear_cache(self):
        result = self.cache_stats()
        self.ready.clear()
        return result


class PreviewManagerTests(unittest.TestCase):
    def wait_for(self, condition, timeout=3):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if condition():
                return
            time.sleep(0.01)
        self.fail("preview manager condition timed out")

    def test_worker_concurrency_is_bounded(self):
        store = FakePhotoStore()
        manager = PreviewManager(store, workers=2, max_pending=16)
        try:
            manager.focus(["A.JPG", "B.JPG", "C.JPG"], [], "thumb")
            self.wait_for(lambda: len(store.started) >= 2)
            self.assertEqual(store.peak, 2)
            store.release.set()
            self.wait_for(lambda: manager.progress()["ready"] == 3)
        finally:
            store.release.set()
            manager.shutdown()

    def test_stale_prefetch_is_cancelled(self):
        store = FakePhotoStore()
        manager = PreviewManager(store, workers=1, max_pending=16)
        try:
            manager.focus(["VISIBLE.JPG"], ["STALE.JPG"], "thumb")
            self.wait_for(lambda: store.started == ["VISIBLE.JPG"])
            manager.focus(["NEW.JPG"], [], "thumb")
            store.release.set()
            self.wait_for(
                lambda: manager.progress()["cancelled"] >= 1)
            self.assertNotIn("STALE.JPG", store.started)
        finally:
            store.release.set()
            manager.shutdown()

    def test_unexpected_decoder_error_does_not_kill_worker(self):
        class ErrorStore(FakePhotoStore):
            def generate_preview(self, name, size):
                if name == "BROKEN.RAF":
                    raise RuntimeError("decoder crashed")
                self.ready.add((name, size))
                return Path("/cached")

        store = ErrorStore()
        manager = PreviewManager(store, workers=1, max_pending=16)
        try:
            manager.focus(["BROKEN.RAF", "GOOD.JPG"], [], "thumb")
            self.wait_for(lambda: manager.progress()["failed"] == 1)
            self.wait_for(lambda: manager.progress()["ready"] == 1)
        finally:
            manager.shutdown()

    def test_stalled_decoder_is_replaced_and_queue_continues(self):
        class StallingStore(FakePhotoStore):
            def __init__(self):
                super().__init__()
                self.stalled = threading.Event()

            def generate_preview(self, name, size):
                if name == "STUCK.RAF":
                    self.stalled.set()
                    self.release.wait(timeout=2)
                    return Path("/ignored")
                self.ready.add((name, size))
                return Path("/cached")

        store = StallingStore()
        manager = PreviewManager(
            store, workers=1, max_pending=16, stall_timeout=0.05)
        try:
            manager.focus(["STUCK.RAF", "NEXT.JPG"], [], "thumb")
            self.assertTrue(store.stalled.wait(timeout=1))
            self.wait_for(lambda: manager.progress()["failed"] == 1)
            self.wait_for(lambda: manager.progress()["ready"] == 1)
            status = manager.status(["STUCK.RAF"], "thumb")
            self.assertIn(
                "decoder was restarted",
                status["previews"]["STUCK.RAF"]["error"],
            )
        finally:
            store.release.set()
            manager.shutdown()


class GuiManifestTests(unittest.TestCase):
    def test_loads_matching_measurements_and_rejects_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_path = root / "report.json"
            report_path.write_text(json.dumps(report_data()), encoding="utf-8")
            report = load_report(report_path)
            manifest = {
                "format": "opencull-manifest-v1",
                "groups": [{"id": "g", "candidates": [
                    {"name": "A.JPG", "sharpness": 90},
                    {"name": "B.JPG", "sharpness": 80},
                ]}],
            }
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            values, _ = load_measurements(manifest_path, report)
            self.assertEqual(values["A.JPG"]["sharpness"], 90)
            manifest["groups"][0]["candidates"].pop()
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ManifestError, "identities differ"):
                load_measurements(manifest_path, report)


class GuiReviewTests(unittest.TestCase):
    def make_store(self, root: Path):
        report_path = root / "report.json"
        report_path.write_text(json.dumps(report_data()), encoding="utf-8")
        report = load_report(report_path)
        return ReviewStore(root / "report.review.json", report, root), report

    def test_atomically_saves_and_reloads_human_decision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, report = self.make_store(root)
            updated = store.update_cluster(
                "group-0001", ["B.JPG"], "better smile", True, 0)
            self.assertEqual(updated["revision"], 1)
            self.assertTrue(store.path.exists())
            reloaded = ReviewStore(store.path, report, root)
            decision = reloaded.public_state()["clusters"]["group-0001"]
            self.assertEqual(decision["keepers"], ["B.JPG"])
            self.assertEqual(decision["note"], "better smile")
            self.assertTrue(decision["reviewed"])

    def test_rejects_unknown_keeper_and_revision_conflict(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, _ = self.make_store(Path(temporary))
            with self.assertRaisesRegex(ReviewError, "Unknown human keeper"):
                store.update_cluster(
                    "group-0001", ["UNKNOWN.JPG"], "", True, 0)
            store.update_position("group-0001", 0)
            with self.assertRaisesRegex(ReviewError, "another browser"):
                store.update_cluster(
                    "group-0001", ["A.JPG"], "", True, 0)

    def test_stale_report_sidecar_is_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, report = self.make_store(root)
            store.update_cluster("group-0001", ["A.JPG"], "", True, 0)
            changed_data = report_data()
            changed_data["notice"] = "changed"
            changed_path = root / "changed.json"
            changed_path.write_text(json.dumps(changed_data), encoding="utf-8")
            stale = ReviewStore(store.path, load_report(changed_path), root)
            self.assertFalse(stale.public_state()["status"]["compatible"])
            with self.assertRaisesRegex(ReviewError, "different version"):
                stale.update_position("group-0001", 0)

    def test_export_labels_human_and_unreviewed_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, _ = self.make_store(Path(temporary))
            store.update_cluster(
                "group-0001", ["B.JPG"], "human choice", True, 0)
            exported = store.export()
            self.assertEqual(exported["clusters"][0]["decision_source"], "human")
            self.assertEqual(exported["selected_photos"], ["B.JPG"])

    def test_photo_judgments_history_statistics_and_undo(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, _ = self.make_store(root)
            state = store.update_cluster(
                "group-0001", ["B.JPG"], "better expression", True, 0,
                {
                    "A.JPG": {
                        "rating": 1, "flag": "reject", "label": "red"},
                    "B.JPG": {
                        "rating": 5, "flag": "keep", "label": "green"},
                },
                "rated-expression",
            )
            self.assertEqual(state["revision"], 1)
            self.assertEqual(state["status"]["history_events"], 1)
            self.assertEqual(
                state["clusters"]["group-0001"]["photo_annotations"]["B.JPG"],
                {"rating": 5, "flag": "keep", "label": "green"},
            )
            exported = store.export()
            self.assertEqual(
                exported["clusters"][0]["photo_annotations"]["A.JPG"]["flag"],
                "reject",
            )
            undone = store.undo(1)
            self.assertEqual(undone["revision"], 2)
            self.assertNotIn("group-0001", undone["clusters"])

    def test_old_review_sidecar_migrates_optional_phase5_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, report = self.make_store(root)
            store.update_cluster(
                "group-0001", ["A.JPG"], "legacy", True, 0)
            data = json.loads(store.path.read_text())
            data.pop("history", None)
            data["clusters"]["group-0001"].pop("photo_annotations", None)
            store.path.write_text(json.dumps(data), encoding="utf-8")
            migrated = ReviewStore(store.path, report, root).public_state()
            self.assertTrue(migrated["status"]["compatible"])
            self.assertEqual(migrated["history"], [])
            self.assertEqual(
                migrated["clusters"]["group-0001"]["photo_annotations"], {})

    def test_rejects_invalid_photo_judgment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, _ = self.make_store(root)
            with self.assertRaisesRegex(ReviewError, "between 0 and 5"):
                store.update_cluster(
                    "group-0001", [], "", True, 0,
                    {"A.JPG": {"rating": 9, "flag": "keep", "label": ""}})

    def test_lightroom_xmp_zip_is_deterministic_and_nonmutating(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, _ = self.make_store(root)
            store.update_cluster(
                "group-0001", ["B.JPG"], "", True, 0,
                {"B.JPG": {
                    "rating": 5, "flag": "keep", "label": "green"}})
            body, filename = xmp_zip(store, "effective")
            self.assertEqual(filename, "opencull-lightroom-xmp.zip")
            with zipfile.ZipFile(BytesIO(body)) as archive:
                self.assertIn("A.xmp", archive.namelist())
                self.assertIn("B.xmp", archive.namelist())
                selected = archive.read("B.xmp").decode()
                self.assertIn('xmp:Rating="5"', selected)
                self.assertIn('xmp:Label="Green"', selected)
                self.assertIn('opencull:Pick="1"', selected)

    def test_1500_cluster_review_history_reloads_after_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            names = [f"P{number:04d}.JPG" for number in range(1500)]
            data = {
                "format": "opencull-report-v2",
                "manifest_sha256": "phase5-scale",
                "clusters": [
                    {"cluster_id": f"group-{number:04d}", "photos": [name]}
                    for number, name in enumerate(names, start=1)
                ],
                "keep": [
                    {"cluster_id": f"group-{number:04d}", "photos": [name]}
                    for number, name in enumerate(names, start=1)
                ],
                "warnings": [],
            }
            report_path = root / "large.json"
            report_path.write_text(json.dumps(data), encoding="utf-8")
            report = load_report(report_path)
            path = root / "large.review.json"
            store = ReviewStore(path, report, root)
            revision = 0
            for number in range(25):
                cluster_id = f"group-{number + 1:04d}"
                name = names[number]
                updated = store.update_cluster(
                    cluster_id, [name], "", True, revision,
                    {name: {
                        "rating": 4, "flag": "keep", "label": "green"}},
                )
                revision = updated["revision"]
            restarted = ReviewStore(path, report, root).public_state()
            self.assertTrue(restarted["status"]["compatible"])
            self.assertEqual(restarted["status"]["reviewed_clusters"], 25)
            self.assertEqual(restarted["status"]["history_events"], 25)
            self.assertEqual(restarted["revision"], 25)


class GuiActionTests(unittest.TestCase):
    def make_context(self, root: Path):
        photos = root / "photos"
        photos.mkdir()
        Image.new("RGB", (120, 80), "blue").save(photos / "A.JPG")
        Image.new("RGB", (120, 80), "green").save(photos / "B.JPG")
        report_path = root / "report.json"
        report_path.write_text(json.dumps(report_data()), encoding="utf-8")
        report = load_report(report_path)
        photo_store = PhotoStore(photos, root / "cache")
        reviews = ReviewStore(root / "review.json", report, photos)
        return report, photo_store, reviews

    def wait_operation(self, operation, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = operation.public()
            if state["status"] not in {"planned", "running"}:
                return state
            time.sleep(0.01)
        self.fail("operation did not finish")

    def test_selection_policies_and_nonmutating_exports(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, _, reviews = self.make_context(root)
            self.assertEqual(
                policy_clusters(reviews, "human_only")[0]["keepers"], [])
            with self.assertRaisesRegex(ActionError, "remain unreviewed"):
                policy_clusters(reviews, "require_all")
            reviews.update_cluster(
                "group-0001", ["B.JPG"], "human", True, 0)
            selected = policy_clusters(reviews, "human_only")
            self.assertEqual(selected[0]["keepers"], ["B.JPG"])
            csv_body, content_type, _ = export_bytes(
                reviews, "human_only", "csv")
            self.assertIn(b"B.JPG", csv_body)
            self.assertIn("text/csv", content_type)

    def test_preflight_detects_collision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report, photos, reviews = self.make_context(root)
            reviews.update_cluster(
                "group-0001", ["A.JPG"], "", True, 0)
            destination = root / "export"
            destination.mkdir()
            (destination / "A.JPG").write_bytes(b"collision")
            plan = build_plan(
                report, reviews, photos, destination,
                "human_only", "copy", "opensull")
            self.assertTrue(plan["summary"]["errors"])
            self.assertEqual(len(plan["summary"]["collisions"]), 1)
            self.assertRegex(plan["confirmation_code"], r"^\d{4}$")

    def test_verified_copy_and_move_rollback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report, photos, reviews = self.make_context(root)
            reviews.update_cluster(
                "group-0001", ["A.JPG"], "", True, 0)
            copy_plan = build_plan(
                report, reviews, photos, root / "copies",
                "human_only", "copy", "opensull")
            copy_op = Operation(copy_plan, root / "copy.operation.json")
            copy_op.start()
            copied = self.wait_operation(copy_op)
            self.assertEqual(copied["status"], "completed")
            self.assertEqual(
                copied["journal_path"],
                str((root / "copy.operation.json").resolve()))
            self.assertEqual(
                (root / "photos" / "A.JPG").read_bytes(),
                (root / "copies" / "A.JPG").read_bytes(),
            )
            self.assertTrue(copied["items"][0]["sha256"])

            move_plan = build_plan(
                report, reviews, photos, root / "moved",
                "human_only", "move", "opensull")
            move_op = Operation(move_plan, root / "move.operation.json")
            move_op.start()
            moved = self.wait_operation(move_op)
            self.assertEqual(moved["status"], "completed")
            self.assertFalse((root / "photos" / "A.JPG").exists())
            move_op.rollback_move()
            self.assertTrue((root / "photos" / "A.JPG").exists())
            self.assertFalse((root / "moved" / "A.JPG").exists())
            self.assertEqual(
                move_op.public()["rollback"]["status"], "completed")

    def test_move_unselected_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report, photos, reviews = self.make_context(root)
            reviews.update_cluster(
                "group-0001", ["A.JPG"], "", True, 0)
            plan = build_plan(
                report, reviews, photos, root / "unselected",
                "human_only", "move", "opensull",
                selection_scope="unselected")
            self.assertEqual(
                [item["source_name"] for item in plan["items"]], ["B.JPG"])
            self.assertEqual(plan["summary"]["selected_files"], 0)
            self.assertEqual(plan["summary"]["unselected_files"], 1)
            operation = Operation(plan, root / "unselected.operation.json")
            operation.start()
            self.assertEqual(
                self.wait_operation(operation)["status"], "completed")
            self.assertTrue((photos.root / "A.JPG").exists())
            self.assertFalse((photos.root / "B.JPG").exists())
            self.assertTrue((root / "unselected" / "B.JPG").exists())

    def test_project_filter_quarantines_rejections_and_reserves_raws(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report, photos, reviews = self.make_context(root)
            reviews.update_cluster(
                "group-0001", ["A.JPG"], "final review", True, 0)
            internal_raw = photos.root / "A.RAF"
            internal_raw.write_bytes(b"internal-raw")
            external = root / "external-raws"; external.mkdir()
            external_raw = external / "A.RAF"
            external_raw.write_bytes(b"external-raw")
            project_path, _ = load_or_create_folder_project(
                photos.root, "Project filter")
            raw_sources = RawSourceStore(
                photos.root / ".darkimiya" / "Reports" / "raw-source.json",
                report)
            raw_sources.configure(external)
            controller = ActionController(
                report, reviews, photos, project_path, raw_sources)

            plan = controller.preflight(action="project_filter")
            self.assertEqual(plan["summary"]["rejected_files"], 1)
            self.assertEqual(plan["summary"]["raw_reserve_files"], 2)
            self.assertEqual(plan["summary"]["external_raw_copies"], 1)
            self.assertFalse(plan["summary"]["errors"])
            by_category = {
                (item["category"], item["operation"])
                for item in plan["items"]}
            self.assertIn(("rejected", "move"), by_category)
            self.assertIn(("raw_reserve", "move"), by_category)
            self.assertIn(("raw_reserve", "copy"), by_category)

            operation = controller.execute(
                plan["plan_id"], plan["confirmation_code"])
            deadline = time.time() + 5
            while time.time() < deadline:
                operation = controller.status(plan["plan_id"])
                if operation["status"] not in {"planned", "running"}:
                    break
                time.sleep(0.01)
            self.assertEqual(operation["status"], "completed")
            self.assertTrue((photos.root / "A.JPG").is_file())
            self.assertFalse((photos.root / "B.JPG").exists())
            self.assertFalse(internal_raw.exists())
            self.assertTrue(external_raw.is_file())
            self.assertTrue((photos.root / ".darkimiya" / "Rejected" / "B.JPG").is_file())
            self.assertEqual(
                photos.resolve("B.JPG"),
                (photos.root / ".darkimiya" / "Rejected" / "B.JPG").resolve())
            self.assertTrue((photos.root / ".darkimiya" / "RAW Reserve" / "A.RAF").is_file())
            self.assertTrue((photos.root / ".darkimiya" / "RAW Reserve" / "External" / "A.RAF").is_file())
            project = load_project(project_path)
            self.assertEqual(len(project["artifacts"]["rejected"]), 1)
            self.assertEqual(len(project["artifacts"]["raw_reserve"]), 2)
            self.assertEqual(len(project["artifacts"]["operations"]), 1)
            self.assertEqual(raw_sources.public()["summary"]["raw_files"], 2)

            rolled_back = controller.rollback(
                plan["plan_id"], f"ROLLBACK {plan['plan_id']}")
            self.assertEqual(rolled_back["status"], "rolled-back")
            self.assertTrue((photos.root / "B.JPG").is_file())
            self.assertTrue(internal_raw.is_file())
            self.assertTrue(external_raw.is_file())
            self.assertFalse((photos.root / ".darkimiya" / "RAW Reserve" / "A.RAF").exists())
            self.assertEqual(raw_sources.public()["summary"]["raw_files"], 0)
            project = load_project(project_path)
            self.assertTrue(all(
                item["status"] == "restored"
                for key in ("rejected", "raw_reserve")
                for item in project["artifacts"][key]))

    def test_final_cleanup_preserves_useful_raws_and_can_keep_every_raw(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report, photos, reviews = self.make_context(root)
            reviews.update_cluster(
                "group-0001", ["A.JPG"], "final review", True, 0)
            project_path, _ = load_or_create_folder_project(
                photos.root, "Final cleanup")
            rejected = photos.root / ".darkimiya" / "Rejected"
            rejected.mkdir(parents=True, exist_ok=True)
            shutil_source = photos.root / "B.JPG"
            shutil_source.replace(rejected / "B.JPG")
            (rejected / "Unused.RAF").write_bytes(b"unused-raw")
            reserve = photos.root / ".darkimiya" / "RAW Reserve"
            (reserve / "Useful.RAF").write_bytes(b"useful-raw")
            raw_sources = RawSourceStore(
                photos.root / ".darkimiya" / "Reports" / "raw-source.json",
                report)
            raw_sources.configure(reserve)
            controller = ActionController(
                report, reviews, photos, project_path, raw_sources)

            inventory = controller.cleanup_candidates()
            self.assertEqual(inventory["summary"]["files"], 2)
            fake_trash = root / "System Trash"
            with patch(
                "opencull_gui.actions._trash_root_for",
                side_effect=lambda _source, batch: fake_trash / batch,
            ):
                default_plan = controller.preflight(
                    action="system_cleanup",
                    cleanup_policy="preserve_useful_raws")
            self.assertEqual(default_plan["summary"]["trashed_files"], 2)
            self.assertEqual(default_plan["summary"]["retained_raw_files"], 0)
            self.assertTrue((reserve / "Useful.RAF").is_file())

            inspect = controller.preflight(
                action="system_cleanup", cleanup_policy="inspect",
                selected_cleanup_paths=["B.JPG"])
            self.assertEqual([item["source_name"] for item in inspect["items"]], ["B.JPG"])

            with patch(
                "opencull_gui.actions._trash_root_for",
                side_effect=lambda _source, batch: fake_trash / batch,
            ):
                plan = controller.preflight(
                    action="system_cleanup", cleanup_policy="keep_every_raw")
            self.assertEqual(plan["summary"]["trashed_files"], 1)
            self.assertEqual(plan["summary"]["retained_raw_files"], 1)
            controller.execute(plan["plan_id"], plan["confirmation_code"])
            deadline = time.time() + 5
            while time.time() < deadline:
                operation = controller.status(plan["plan_id"])
                if operation["status"] not in {"planned", "running"}:
                    break
                time.sleep(0.01)
            self.assertEqual(operation["status"], "completed")
            self.assertFalse((rejected / "B.JPG").exists())
            self.assertFalse((rejected / "Unused.RAF").exists())
            self.assertTrue((reserve / "Useful.RAF").is_file())
            retained = reserve / "Retained from Rejected" / "Unused.RAF"
            self.assertTrue(retained.is_file())
            trashed = next(
                Path(item["destination"]) for item in plan["items"]
                if item["category"] == "system_trash")
            self.assertTrue(trashed.is_file())
            project = load_project(project_path)
            self.assertTrue(any(
                item.get("status") == "system_trash"
                for item in project["artifacts"]["rejected"]))

            rolled_back = controller.rollback(
                plan["plan_id"], f"ROLLBACK {plan['plan_id']}")
            self.assertEqual(rolled_back["status"], "rolled-back")
            self.assertTrue((rejected / "B.JPG").is_file())
            self.assertTrue((rejected / "Unused.RAF").is_file())
            self.assertFalse(trashed.exists())
            self.assertFalse(retained.exists())
            self.assertTrue((reserve / "Useful.RAF").is_file())

    def test_trash_unselected_is_verified_and_rollbackable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report, photos, reviews = self.make_context(root)
            reviews.update_cluster(
                "group-0001", ["A.JPG"], "", True, 0)
            fake_trash = root / "Trash"
            with patch(
                "opencull_gui.actions._trash_root_for",
                side_effect=lambda _source, batch: fake_trash / batch,
            ):
                plan = build_plan(
                    report, reviews, photos, None,
                    "human_only", "trash", "opensull",
                    selection_scope="unselected")
            self.assertEqual(plan["destination"], "macOS Trash")
            self.assertEqual(
                [item["source_name"] for item in plan["items"]], ["B.JPG"])
            operation = Operation(plan, root / "trash.operation.json")
            operation.start()
            self.assertEqual(
                self.wait_operation(operation)["status"], "completed")
            trashed = Path(plan["items"][0]["destination"])
            self.assertFalse((photos.root / "B.JPG").exists())
            self.assertTrue(trashed.exists())
            operation.rollback_move()
            self.assertTrue((photos.root / "B.JPG").exists())
            self.assertFalse(trashed.exists())

    def test_trash_refuses_selected_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report, photos, reviews = self.make_context(root)
            with self.assertRaisesRegex(
                    ActionError, "limited to unselected"):
                build_plan(
                    report, reviews, photos, None,
                    "effective", "trash", "opensull",
                    selection_scope="selected")

    def test_resume_finishes_pending_journal_items(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report, photos, reviews = self.make_context(root)
            reviews.update_cluster(
                "group-0001", ["A.JPG", "B.JPG"], "", True, 0)
            plan = build_plan(
                report, reviews, photos, root / "resume",
                "human_only", "copy", "opensull")
            journal_path = root / "resume.operation.json"
            first = Operation(plan, journal_path)
            first._execute_item(first.journal["items"][0])
            first.journal["status"] = "paused"
            first._save()
            resumed = Operation(plan, journal_path)
            resumed.start()
            state = self.wait_operation(resumed)
            self.assertEqual(state["status"], "completed")
            self.assertEqual(state["completed_files"], 2)

    def test_controller_discovers_only_valid_incomplete_report_journals(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report, photos, reviews = self.make_context(root)
            reviews.update_cluster(
                "group-0001", ["A.JPG", "B.JPG"], "", True, 0)
            plan = build_plan(
                report, reviews, photos, root / "resume",
                "human_only", "move", "opensull")
            journal = default_journal_path(report, plan)
            operation = Operation(plan, journal)
            operation._execute_item(operation.journal["items"][0])
            operation.journal["status"] = "running"
            operation._save()
            controller = ActionController(report, reviews, photos)
            found = controller.recoverable_journals()
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["plan_id"], plan["plan_id"])
            self.assertEqual(found[0]["status"], "interrupted")
            self.assertEqual(found[0]["completed_files"], 1)
            self.assertEqual(found[0]["remaining_files"], 1)

    def test_companion_and_cluster_inspection_layout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report, photos, reviews = self.make_context(root)
            (photos.root / "A.RAF").write_bytes(b"raw companion")
            reviews.update_cluster(
                "group-0001", ["A.JPG"], "", True, 0)
            plan = build_plan(
                report, reviews, photos, root / "clusters",
                "human_only", "copy", "cluster_inspection",
                include_companions=True)
            destinations = [item["destination"] for item in plan["items"]]
            self.assertTrue(any(
                "cluster-0001/selected/A.JPG" in path
                for path in destinations))
            self.assertTrue(any(
                "cluster-0001/selected/A.RAF" in path
                for path in destinations))
            self.assertTrue(any(
                "cluster-0001/not-selected/B.JPG" in path
                for path in destinations))

    def test_contact_sheet_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, photos, reviews = self.make_context(root)
            reviews.update_cluster(
                "group-0001", ["A.JPG", "B.JPG"], "", True, 0)
            outputs = create_contact_sheets(
                reviews, photos, root / "sheets", "human_only", 2, 1)
            self.assertEqual(len(outputs), 1)
            with Image.open(outputs[0]) as sheet:
                self.assertEqual(sheet.format, "JPEG")


class FakeKeychain:
    def __init__(self):
        self.values = {}

    def has(self, profile_id):
        return profile_id in self.values

    def get(self, profile_id):
        if profile_id not in self.values:
            raise ProviderError("missing fake credential")
        return self.values[profile_id]

    def set(self, profile_id, secret):
        self.values[profile_id] = secret

    def delete(self, profile_id):
        self.values.pop(profile_id, None)


class FakeFaceEngine:
    def __init__(self):
        self.calls = []

    def analyze(self, path):
        self.calls.append(path.name)
        vectors = {
            "A.JPG": [1, 0, 0, 0],
            "B.JPG": [0.99, 0.01, 0, 0],
            "C.JPG": [0, 1, 0, 0],
        }
        vector = np.asarray(vectors[path.name], dtype=np.float32)
        vector /= np.linalg.norm(vector)
        return (100, 80), [{
            "box": [20, 10, 40, 50],
            "landmarks": [30, 25, 50, 25, 40, 35, 32, 48, 48, 48],
            "confidence": 0.98,
            "embedding": vector,
        }]


class GuiFaceTests(unittest.TestCase):
    def make_store(self, root, engine=None):
        names = ("A.JPG", "B.JPG", "C.JPG")
        photos = root / "photos"
        photos.mkdir()
        for name, color in zip(names, ("red", "orange", "blue")):
            Image.new("RGB", (100, 80), color).save(photos / name)
        report_path = root / "report.json"
        report_path.write_text(
            json.dumps(report_data(names)), encoding="utf-8")
        report = load_report(report_path)
        photo_store = PhotoStore(photos, root / "cache")
        reviews = ReviewStore(root / "review.json", report, photos)
        return (
            FaceStore(
                root / "faces.sqlite3", report, photo_store, reviews,
                root / "models", engine=engine or FakeFaceEngine()),
            report,
            reviews,
        )

    def wait_faces(self, store, timeout=3):
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = store.public()
            if state["status"]["state"] in {"completed", "failed", "paused"}:
                self.assertEqual(state["status"]["state"], "completed", state)
                return state
            time.sleep(0.02)
        self.fail("face indexing did not finish")

    def test_local_index_clusters_and_never_exposes_embeddings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            engine = FakeFaceEngine()
            store, _, _ = self.make_store(root, engine)
            try:
                store.start()
                state = self.wait_faces(store)
                self.assertEqual(state["status"]["faces"], 3)
                self.assertEqual(state["status"]["people"], 2)
                self.assertTrue(state["embeddings_exposed"] is False)
                self.assertTrue(all(
                    "embedding" not in face
                    for person in state["people"] for face in person["faces"]))
                grouped = sorted(
                    person["face_count"] for person in state["people"])
                self.assertEqual(grouped, [1, 2])
                self.assertEqual(sorted(engine.calls), [
                    "A.JPG", "B.JPG", "C.JPG"])
                self.assertEqual(os.stat(store.path).st_mode & 0o777, 0o600)
            finally:
                store.shutdown()

    def test_empty_database_can_rebind_report_hash_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, report, reviews = self.make_store(root)
            database = store.path
            photos = store.photos
            models = store.model_root
            store.shutdown()
            connection = __import__("sqlite3").connect(database)
            connection.execute(
                "UPDATE metadata SET value = ? WHERE key = 'report_sha256'",
                ("temporary-reformatted-hash",),
            )
            connection.commit()
            connection.close()

            rebound = FaceStore(
                database, report, photos, reviews, models,
                engine=FakeFaceEngine(),
            )
            try:
                metadata = dict(rebound._db.execute(
                    "SELECT key, value FROM metadata"))
                self.assertEqual(metadata["report_sha256"], report.sha256)
            finally:
                rebound.shutdown()

    def test_populated_database_never_rebinds_report_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, report, reviews = self.make_store(root)
            database = store.path
            photos = store.photos
            models = store.model_root
            store.start()
            self.wait_faces(store)
            store.shutdown()
            connection = __import__("sqlite3").connect(database)
            connection.execute(
                "UPDATE metadata SET value = ? WHERE key = 'report_sha256'",
                ("different-report",),
            )
            connection.commit()
            connection.close()

            with self.assertRaisesRegex(FaceError, "different evidence"):
                FaceStore(
                    database, report, photos, reviews, models,
                    engine=FakeFaceEngine(),
                )

    def test_rename_split_merge_forget_and_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, _, reviews = self.make_store(Path(temporary))
            try:
                reviews.update_cluster(
                    "group-0001", ["A.JPG"], "", True, 0)
                store.start()
                state = self.wait_faces(store)
                pair = next(
                    person for person in state["people"]
                    if person["face_count"] == 2)
                self.assertEqual(pair["coverage"]["selected_photos"], 1)
                state = store.rename(pair["id"], "Private Child", True)
                named = next(
                    person for person in state["people"]
                    if person["id"] == pair["id"])
                self.assertEqual(named["private_name"], "Private Child")
                state = store.split(pair["id"], [named["faces"][0]["id"]])
                self.assertEqual(len(state["people"]), 3)
                anonymous = [
                    person["id"] for person in state["people"]
                    if not person["private_name"]
                ]
                state = store.merge(anonymous[:2])
                self.assertEqual(len(state["people"]), 2)
                target = next(
                    person for person in state["people"]
                    if not person["private_name"])
                state = store.forget(target["id"])
                self.assertEqual(state["status"]["people"], 1)
                self.assertEqual(state["status"]["faces"], 1)
            finally:
                store.shutdown()

    def test_restart_skips_unchanged_photos_and_delete_all(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_engine = FakeFaceEngine()
            store, report, reviews = self.make_store(root, first_engine)
            store.start()
            self.wait_faces(store)
            store.shutdown()
            second_engine = FakeFaceEngine()
            photo_store = PhotoStore(root / "photos", root / "cache2")
            restarted = FaceStore(
                root / "faces.sqlite3", report, photo_store, reviews,
                root / "models", engine=second_engine)
            try:
                restarted.start()
                self.wait_faces(restarted)
                self.assertEqual(second_engine.calls, [])
                deleted = restarted.delete_all()
                self.assertEqual(deleted["status"]["faces"], 0)
                self.assertEqual(deleted["status"]["people"], 0)
            finally:
                restarted.shutdown()

    def test_face_crop_and_report_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, _, _ = self.make_store(root)
            try:
                store.start()
                state = self.wait_faces(store)
                face_id = state["people"][0]["faces"][0]["id"]
                crop = store.face_crop(face_id)
                self.assertGreater(crop.width, 0)
                self.assertGreater(crop.height, 0)
            finally:
                store.shutdown()
            changed = report_data(("A.JPG", "B.JPG", "C.JPG"))
            changed["notice"] = "different report evidence"
            changed_path = root / "changed.json"
            changed_path.write_text(json.dumps(changed), encoding="utf-8")
            changed_report = load_report(changed_path)
            photo_store = PhotoStore(root / "photos", root / "cache3")
            reviews = ReviewStore(
                root / "changed.review.json", changed_report, root / "photos")
            with self.assertRaisesRegex(FaceError, "different evidence"):
                FaceStore(
                    root / "faces.sqlite3", changed_report, photo_store, reviews,
                    root / "models", engine=FakeFaceEngine())

    def test_1500_photo_index_is_resumable_and_bounded(self):
        class ScaleEngine:
            def analyze(self, path):
                return (10, 10), [{
                    "box": [1, 1, 5, 6],
                    "landmarks": [2, 3, 5, 3, 4, 4, 2, 6, 5, 6],
                    "confidence": 0.95,
                    "embedding": np.asarray([1, 0, 0, 0], dtype=np.float32),
                }]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            names = [f"P{number:04d}.JPG" for number in range(1500)]
            for name in names:
                (photos / name).write_bytes(b"local-test")
            report_path = root / "large.json"
            report_path.write_text(json.dumps({
                "format": "opencull-report-v2",
                "manifest_sha256": "phase8-scale",
                "clusters": [
                    {"cluster_id": f"group-{index:04d}", "photos": [name]}
                    for index, name in enumerate(names, start=1)
                ],
                "keep": [
                    {"cluster_id": f"group-{index:04d}", "photos": [name]}
                    for index, name in enumerate(names, start=1)
                ],
                "warnings": [],
            }), encoding="utf-8")
            report = load_report(report_path)
            photo_store = PhotoStore(photos, root / "cache")
            reviews = ReviewStore(root / "review.json", report, photos)
            store = FaceStore(
                root / "faces.sqlite3", report, photo_store, reviews,
                root / "models", engine=ScaleEngine())
            try:
                store.start()
                # 1,500 photographs is a throughput test, not a latency one;
                # the default deadline is sized for the small fixtures.
                state = self.wait_faces(store, timeout=60)
                self.assertEqual(state["status"]["processed"], 1500)
                self.assertEqual(state["status"]["faces"], 1500)
                self.assertEqual(state["status"]["people"], 1)
                self.assertLess(len(json.dumps(state)), 800_000)
            finally:
                store.shutdown()

    def test_face_engine_module_has_no_network_or_process_egress(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "opencull_gui" / "faces.py"
        ).read_text(encoding="utf-8")
        for forbidden in (
            "import urllib", "import requests", "import socket",
            "import subprocess", "urlopen(", "Popen(",
        ):
            self.assertNotIn(forbidden, source)


def provider_data(kind="openrouter", endpoint=""):
    return {
        "name": f"Test {kind}",
        "kind": kind,
        "endpoint": endpoint,
        "models": {
            "A": "google/gemini-2.5-flash",
            "B": "openai/gpt-4.1-mini",
            "C": "mistralai/mistral-small-3.2-24b-instruct",
            "D": "qwen/qwen3-vl-30b-a3b-instruct",
        },
        "credential_required": kind == "openrouter",
        "zdr": True,
        "cost_note": "test only",
    }


class GuiProviderTests(unittest.TestCase):
    def make_store(self, root):
        project = root / "opencull"
        project.mkdir(parents=True)
        source = Path(__file__).resolve().parents[1]
        for name in (
            "opencull.kim", "scan.py", "opencull_kernel.py",
            "professional_shortlist.kim", "shortlist_kernel.py",
        ):
            (project / name).write_bytes((source / name).read_bytes())
        keychain = FakeKeychain()
        return ProviderStore(root / "providers.json", project, keychain), keychain

    def test_secret_is_write_only_and_never_materialized(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, keychain = self.make_store(root)
            secret = "super-secret-provider-token"
            public = store.save(provider_data(), 0, secret)
            self.assertNotIn(secret, json.dumps(public))
            self.assertTrue(public["secrets_returned"] is False)
            profile = public["profiles"][0]
            self.assertEqual(profile["credential"], "stored")
            self.assertEqual(keychain.get(profile["id"]), secret)
            manifest = store.materialize("job123456789", profile["id"])
            self.assertTrue(manifest["contains_secret"] is False)
            for path in Path(manifest["program_path"]).parent.iterdir():
                if path.is_file():
                    self.assertNotIn(secret, path.read_text(encoding="utf-8"))
            agents = Path(manifest["agents_path"]).read_text()
            self.assertIn("key_env", agents)
            self.assertIn("zdr      = true", agents)

    def test_migrates_only_opencull_default_models_to_affordable_panel(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, keychain = self.make_store(root)
            saved = store.save(provider_data(), 0, "private-token")
            profile_id = saved["profiles"][0]["id"]

            migrated = ProviderStore(
                root / "providers.json", root / "opencull", keychain)
            self.assertEqual(
                migrated.public()["profiles"][0]["models"],
                {
                    "A": "openai/gpt-5.6-luna-pro",
                    "B": "openai/gpt-5.6-luna-pro",
                    "C": "openai/gpt-4.1-mini",
                    "D": "openai/gpt-5.6-luna-pro",
                },
            )

            custom = provider_data()
            custom["id"] = profile_id
            custom["models"] = dict(custom["models"])
            custom["models"]["A"] = "user/custom-vision-model"
            migrated.save(custom, migrated.public()["revision"])
            reopened = ProviderStore(
                root / "providers.json", root / "opencull", keychain)
            self.assertEqual(
                reopened.public()["profiles"][0]["models"]["A"],
                "user/custom-vision-model",
            )

    def test_materializes_professional_program_without_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, _ = self.make_store(root)
            saved = store.save(provider_data(), 0, "private-token")
            profile = saved["profiles"][0]
            manifest = store.materialize(
                "professional1", profile["id"],
                "professional_shortlist.kim")
            self.assertEqual(
                manifest["program_name"], "professional_shortlist.kim")
            program = Path(manifest["program_path"]).read_text(encoding="utf-8")
            self.assertIn(
                str((root / "opencull" / "shortlist_kernel.py").resolve()),
                program)
            self.assertNotIn("private-token", program)

    def test_materializes_per_job_openrouter_model_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, _ = self.make_store(root)
            saved = store.save(provider_data(), 0, "private-token")
            profile = saved["profiles"][0]
            manifest = store.materialize(
                "overridejob1", profile["id"], "opencull.kim",
                {"A": "openai/gpt-5.6-luna-pro"},
            )
            agents = Path(manifest["agents_path"]).read_text(encoding="utf-8")
            self.assertIn(
                'model   = "openai/gpt-5.6-luna-pro"', agents)
            self.assertEqual(
                manifest["profile"]["models"]["A"],
                "openai/gpt-5.6-luna-pro",
            )
            self.assertEqual(
                store.public()["profiles"][0]["models"]["A"],
                "google/gemini-2.5-flash",
            )

    def test_materializes_immutable_custom_judgment_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, _ = self.make_store(root)
            saved = store.save(provider_data(), 0, "private-token")
            profile = saved["profiles"][0]
            policy = {"panel": ["B", "C", "D"], "votes": 7, "required": 5}
            manifest = store.materialize(
                "policyjob123", profile["id"],
                judgment_policy=policy)
            program = Path(manifest["program_path"]).read_text(encoding="utf-8")
            self.assertIn(
                "judge<7,5/7> (evidence |= report_policy(keep_per_group)) "
                "under k_cull panel [B, C, D]",
                program,
            )
            self.assertEqual(manifest["judgment_policy"], policy)
            stored = json.loads(
                (Path(manifest["program_path"]).parent / "manifest.json")
                .read_text(encoding="utf-8"))
            self.assertEqual(stored["judgment_policy"], policy)
            checker = JobManager(
                root / "check-jobs.json", Path(__file__).resolve().parents[1],
                autostart=False)
            try:
                checker._check_program(Path(manifest["program_path"]))
            finally:
                checker.shutdown()

    def test_rejects_invalid_judgment_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, _ = self.make_store(Path(temporary))
            saved = store.save(provider_data(), 0, "private-token")
            profile_id = saved["profiles"][0]["id"]
            with self.assertRaisesRegex(ProviderError, "duplicate"):
                store.materialize(
                    "duplicate123", profile_id,
                    judgment_policy={
                        "panel": ["C", "C"], "votes": 5, "required": 4})
            with self.assertRaisesRegex(ProviderError, "at least the number"):
                store.materialize(
                    "fewvotes123", profile_id,
                    judgment_policy={
                        "panel": ["B", "C", "D"], "votes": 2,
                        "required": 2})

    def test_validates_endpoint_and_revision_conflicts(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, _ = self.make_store(Path(temporary))
            with self.assertRaisesRegex(ProviderError, "without credentials"):
                store.save(provider_data(
                    "openai", "https://user:pass@example.com/v1"), 0)
            with self.assertRaisesRegex(ProviderError, "end in /v1"):
                store.save(provider_data(
                    "openai", "https://example.com/api"), 0)
            saved = store.save(
                provider_data("ollama", "http://127.0.0.1:11434"), 0)
            with self.assertRaisesRegex(ProviderError, "another tab"):
                store.save(provider_data(), 0, "token")
            self.assertEqual(saved["profiles"][0]["privacy"], "local")

    def test_queue_binds_immutable_provider_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, _ = self.make_store(root)
            saved = store.save(
                provider_data("ollama", "http://127.0.0.1:11434"), 0)
            profile_id = saved["profiles"][0]["id"]
            photos = root / "photos"
            photos.mkdir()
            manager = JobManager(
                root / "jobs.json", store.project_root,
                providers=store, autostart=False,
                command_builder=lambda job: [],
                program_checker=lambda path: None,
            )
            try:
                state = manager.add(
                    str(photos), str(root / "result.json"),
                    provider_profile_id=profile_id)
                job = state["jobs"][0]
                self.assertEqual(job["provider_profile_id"], profile_id)
                self.assertEqual(job["provider_kind"], "ollama")
                self.assertEqual(len(job["provider_config_sha256"]), 64)
                original_sha = job["provider_config_sha256"]
                changed = provider_data(
                    "ollama", "http://127.0.0.1:11434")
                changed.update({
                    "id": profile_id,
                    "name": "Changed after queueing",
                    "models": {
                        **changed["models"], "C": "qwen2.5:14b"},
                })
                store.save(changed, saved["revision"])
                persisted = manager.public()["jobs"][0]
                self.assertEqual(
                    persisted["provider_config_sha256"], original_sha)
                self.assertEqual(
                    persisted["provider_profile_name"], "Test ollama")
            finally:
                manager.shutdown()

    def test_queue_binds_custom_judgment_policy_to_generated_program(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, _ = self.make_store(root)
            saved = store.save(
                provider_data("ollama", "http://127.0.0.1:11434"), 0)
            profile_id = saved["profiles"][0]["id"]
            photos = root / "photos"
            photos.mkdir()
            checked = []
            manager = JobManager(
                root / "jobs.json", store.project_root,
                providers=store, autostart=False,
                command_builder=lambda job: [],
                program_checker=lambda path: checked.append(path),
            )
            try:
                state = manager.add(
                    str(photos), str(root / "result.json"),
                    provider_profile_id=profile_id,
                    judge_panel=["B", "C", "D"],
                    judge_votes=7, judge_required=5)
                job = state["jobs"][0]
                self.assertEqual(job["judgment_policy"], {
                    "panel": ["B", "C", "D"],
                    "votes": 7,
                    "required": 5,
                })
                self.assertEqual(checked, [Path(job["program_path"])])
                self.assertIn(
                    "judge<7,5/7>",
                    Path(job["program_path"]).read_text(encoding="utf-8"),
                )
            finally:
                manager.shutdown()

    def test_openai_compatible_connection_and_model_discovery(self):
        class ModelsHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                body = json.dumps({"data": [
                    {"id": model}
                    for model in provider_data()["models"].values()
                ]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), ModelsHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                store, _ = self.make_store(Path(temporary))
                profile = provider_data(
                    "openai", f"http://127.0.0.1:{server.server_port}/v1")
                saved = store.save(profile, 0)
                result = store.test_connection(saved["profiles"][0]["id"])
                self.assertTrue(result["reachable"])
                self.assertTrue(result["configured_models_present"])
                self.assertEqual(result["available_model_count"], 4)
                self.assertTrue(result["vision_declarations"]["A"])
                self.assertFalse(result["vision_declarations"]["C"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class GuiMacOSAppTests(unittest.TestCase):
    def test_desktop_uses_darkimiya_application_namespace(self):
        self.assertEqual(APP_NAME, "Darkimiya")
        self.assertEqual(MacOSPaths().support.name, "Darkimiya")
        self.assertEqual(MacOSPaths().cache.name, "Darkimiya")
        self.assertEqual(MacOSPaths().logs.name, "Darkimiya")
        self.assertEqual(MacOSPaths().launcher_log.name, "Darkimiya.log")

    def test_application_paths_are_private_and_separated(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = MacOSPaths.create(Path(temporary))
            self.assertTrue(paths.support.is_dir())
            self.assertTrue(paths.cache.is_dir())
            self.assertTrue(paths.logs.is_dir())
            self.assertTrue(paths.results.is_dir())
            self.assertNotEqual(paths.jobs.parent, paths.cache)
            self.assertEqual(paths.jobs.parent, paths.support)
            self.assertEqual(paths.providers.parent, paths.support)
            self.assertEqual(paths.kimiya.parent, paths.support)
            self.assertEqual(paths.onboarding_marker.parent, paths.support)
            for directory in (
                paths.support, paths.cache, paths.logs, paths.results
            ):
                self.assertEqual(directory.stat().st_mode & 0o077, 0)

    def test_single_instance_lock_is_exclusive_and_releasable(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "desktop.lock"
            first = InstanceLock(path)
            second = InstanceLock(path)
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            self.assertEqual(path.stat().st_mode & 0o077, 0)
            first.release()
            self.assertTrue(second.acquire())
            second.release()

    @unittest.skipUnless(
        sys.platform == "darwin",
        "the release smoke test validates a macOS bundle layout",
    )
    def test_release_smoke_validates_runtime_and_writes_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            receipt = Path(temporary) / "release-smoke.json"
            self.assertEqual(run_release_smoke_test(receipt), 0)
            result = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(result["format"], "opencull-macos-smoke-v1")
            self.assertTrue(result["passed"])
            self.assertEqual(result["missing_files"], [])
            self.assertEqual(result["kimiya_check_status"], 0)
            self.assertTrue(result["private_paths"])
            self.assertTrue(result["single_instance"])
            self.assertTrue(result["project_migration"])
            self.assertTrue(result["project_catalog"])


class GuiDesignFoundationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        static = Path(__file__).parents[1] / "opencull_gui" / "static"
        cls.html = (static / "index.html").read_text(encoding="utf-8")
        cls.css = (static / "styles.css").read_text(encoding="utf-8")
        cls.javascript = (static / "app.js").read_text(encoding="utf-8")

    def test_application_shell_has_stable_navigation_and_landmarks(self):
        self.assertIn('class="skip-link"', self.html)
        self.assertIn('class="app-sidebar"', self.html)
        self.assertIn('class="app-sidebar-navigation"', self.html)
        self.assertIn('class="toolbar-context"', self.html)
        self.assertIn('class="toolbar-menu"', self.html)
        self.assertIn('aria-current="page"', self.html)
        self.assertIn('id="review-workspace"', self.html)
        self.assertIn('aria-label="Darkimiya project navigation"', self.html)
        self.assertNotIn('class="app-navigation"', self.html)
        self.assertNotIn('class="project-stage-bar"', self.html)

    def test_recovery_center_exposes_explicit_non_destructive_legacy_migration(self):
        self.assertIn('id="migration-panel"', self.html)
        self.assertIn('id="migration-code"', self.html)
        self.assertIn('id="migrate-project"', self.html)
        self.assertIn("/api/project/migration", self.javascript)
        self.assertIn("/api/project/migrate", self.javascript)
        self.assertIn("The old manifest stays untouched", self.html)
        self.assertIn("No photograph is moved", self.html)

    def test_native_project_review_reuses_the_primary_window(self):
        root = Path(__file__).parents[1] / "native-macos" / "Sources" / "OpenCullNative"
        app = (root / "OpenCullNativeApp.swift").read_text(encoding="utf-8")
        content = (root / "ContentView.swift").read_text(encoding="utf-8")
        models = (root / "Models.swift").read_text(encoding="utf-8")
        self.assertIn("struct DarkimiyaApp: App", app)
        self.assertNotIn("Choose an OpenCull", content)
        self.assertNotIn("OpenCull native diagnostics", models)
        self.assertIn('case projects = "Projects"', content)
        self.assertIn('Label("Add Project"', content)
        self.assertIn('Text("Projects").font', content)
        self.assertIn("project.cullingBlocksWorkflow", content)
        self.assertIn(".defaultSize(width: 1100, height: 760)", app)
        self.assertIn('Button("Cull the Folder")', content)
        self.assertIn('Button("Re-Cull")', content)
        self.assertIn('backend.act(jobID: culling.id, action: "cancel")', content)
        self.assertIn('ProgressView(value: culling.progress.fraction)', content)
        self.assertNotIn("ProjectDashboardView", content)
        self.assertNotIn("activeProjectID", models)
        self.assertIn("target = await backend.openProject(project.id)", content)
        self.assertIn("target = await backend.openManualSelection(project.id)", content)
        content = (root / "ContentView.swift").read_text(encoding="utf-8")
        models = (root / "Models.swift").read_text(encoding="utf-8")
        self.assertNotIn("WindowGroup(for: ReviewTarget.self)", app)
        self.assertNotIn("@Environment(\\.openWindow)", content)
        self.assertIn("ProjectWorkspaceView(target: target)", content)
        self.assertIn("final class ProjectSession: ObservableObject", models)
        self.assertIn("projectSession.open(target)", content)
        self.assertIn('<span>Activity</span>', self.html)

    def test_unified_sidebar_exposes_each_project_stage_once(self):
        for stage in ("cull", "shortlist", "style", "develop", "verify"):
            self.assertEqual(self.html.count(f'data-stage="{stage}"'), 1)
        for label in ("Cull", "Shortlist", "Personal style", "Develop images", "Verify"):
            self.assertIn(f"<span>{label}</span>", self.html)
        self.assertNotIn('data-stage="export"', self.html)
        self.assertNotIn('id="export-workspace"', self.html)
        self.assertIn('id="jobs-button" class="app-sidebar-item"', self.html)
        self.assertIn('id="people-button" class="app-sidebar-item"', self.html)
        self.assertIn("--app-sidebar-width:", self.css)

    def test_professional_shortlist_workspace_contract(self):
        for identifier in (
            "shortlist-nav-button", "professional-workspace",
            "shortlist-list", "shortlist-tier-filter",
            "shortlist-raw-filter", "shortlist-review-filter",
            "shortlist-human-tier", "shortlist-edit-raw",
            "save-shortlist-review", "save-shortlist-next",
            "shortlist-assessment", "open-origin-cluster",
            "start-shortlist-assessment", "shortlist-generate-dialog",
            "shortlist-generate-policy", "shortlist-generate-provider",
            "submit-shortlist-generate", "shortlist-job-progress",
        ):
            self.assertIn(f'id="{identifier}"', self.html)
        for contract in (
            'fetch("/api/shortlist")',
            '"/api/shortlist/review"',
            '"/api/shortlist/undo"',
            "function renderShortlistWorkspace()",
            "function navigateShortlist(delta)",
            'state.workspaceMode === "professional"',
            'event.key.toLowerCase() === "e"',
            '"/api/jobs/add-professional"',
            "async function monitorShortlistJob()",
            'operationalPost("/api/shortlist/load"',
        ):
            self.assertIn(contract, self.javascript)
        self.assertIn(".professional-workspace", self.css)
        self.assertIn("#professional-workspace[hidden]", self.css)
        self.assertIn("display: none !important", self.css)
        self.assertIn(".professional-assessment-grid", self.css)
        self.assertIn("Keep RAW for professional editing", self.html)
        self.assertNotIn('id="shortlist-visible-count"', self.html)

    def test_shortlist_sidebar_prioritizes_edit_directions_without_completed_job_chrome(self):
        self.assertIn(
            '</div>\n      <section class="edit-direction-launch"',
            self.html,
        )
        self.assertLess(
            self.html.index('class="edit-direction-launch"'),
            self.html.index('id="shortlist-list"'),
        )
        self.assertIn('id="personal-style-suggestion"', self.html)
        self.assertNotIn('id="continue-to-style"', self.html)
        self.assertNotIn('id="open-shortlist-queue"', self.html)
        self.assertIn("function renderPersonalStyleAvailability(", self.javascript)
        self.assertIn('job.status === "completed"', self.javascript)
        self.assertIn("#edit-regeneration-dialog {", self.css)
        self.assertIn(
            "grid-template-rows: minmax(250px, .82fr) auto minmax(230px, 1.18fr)",
            self.css,
        )

    def test_culling_form_exposes_a_bounded_judgment_policy(self):
        for identity in ("job-judge-votes", "job-judge-required"):
            self.assertIn(f'id="{identity}"', self.html)
        self.assertEqual(self.html.count('class="job-judge-agent"'), 4)
        self.assertIn("function jobJudgmentPolicy()", self.javascript)
        self.assertIn("judge_panel: judgment.panel", self.javascript)
        self.assertIn("judge_votes: judgment.votes", self.javascript)
        self.assertIn("judge_required: judgment.required", self.javascript)
        self.assertIn("Kimiya validates the generated program", self.html)

    def test_dom_ids_are_unique_and_javascript_contract_is_present(self):
        html_ids = re.findall(r'\bid="([^"]+)"', self.html)
        self.assertEqual(len(html_ids), len(set(html_ids)))
        referenced = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', self.javascript))
        self.assertFalse(referenced.difference(html_ids))

    def test_development_exports_are_queued_and_scoped_to_the_active_recipe(self):
        self.assertIn('"/api/jobs/add-delivery-export"', self.javascript)
        self.assertIn("state.deliveryExportJobId = job?.id || null;", self.javascript)
        self.assertIn("job.photo === state.activeDevelopPhoto", self.javascript)
        self.assertIn("job.style === state.activeDevelopVariant", self.javascript)
        self.assertIn(
            'job.engine === (state.activeDevelopEngine || $("#develop-engine").value)',
            self.javascript,
        )
        self.assertIn("function renderDeliveryExportJobStatus()", self.javascript)
        self.assertNotIn(
            "Full-resolution darktable files are ready in Export",
            self.javascript,
        )
        active_recovery = (
            '["queued", "running", "stopping", "detached"].includes(job.status)'
        )
        self.assertIn(active_recovery, self.javascript)

    def test_design_tokens_focus_and_reduced_motion_are_defined(self):
        for token in (
            "--bg:", "--panel:", "--text:", "--muted:", "--accent:",
            "--blue:", "--warning:", "--danger:", "--space-4:",
        ):
            self.assertIn(token, self.css)
        self.assertIn(":focus-visible", self.css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", self.css)
        self.assertIn("dialog::backdrop", self.css)

    def test_preview_images_wait_for_an_immutable_decoded_artifact(self):
        for contract in (
            "function bindPreviewImage(",
            "function commitDecodedPreview(",
            'candidate.decoding = "async"',
            "await candidate.decode()",
            'new IntersectionObserver(',
            "/api/previews/file?name=",
        ):
            self.assertIn(contract, self.javascript)
        self.assertNotRegex(
            self.javascript,
            r"\.src\s*=\s*[^;\n]*?/api/image",
        )
        self.assertNotIn("image.src = imageUrl", self.javascript)

    def test_custom_close_glyph_is_centered_inside_its_circle(self):
        close_rule = re.search(
            r"\.close-button \{(?P<body>.*?)\}",
            self.css,
            re.DOTALL,
        )
        self.assertIsNotNone(close_rule)
        rules = self.css[self.css.rfind(".close-button {"):]
        for declaration in (
            "display: inline-flex", "align-items: center",
            "justify-content: center", "line-height: 1", "padding: 0",
        ):
            self.assertIn(declaration, rules)

    def test_phase2_review_workspace_exposes_progress_density_and_comparison(self):
        for identifier in (
            "cluster-progress-bar", "workspace-selection",
            "workspace-review-state", "compare-tray", "compare-items",
            "clear-compare", "open-comparison",
        ):
            self.assertIn(f'id="{identifier}"', self.html)
        self.assertIn('role="group" aria-label="Photo card density"', self.html)
        self.assertIn('class="photo-number"', self.html)
        self.assertIn("function renderReviewWorkspace(", self.javascript)
        self.assertIn('button.setAttribute("aria-pressed"', self.javascript)
        self.assertIn(".review.density-compact .photo-grid", self.css)

    def test_phase2_comparison_is_deliberate_and_keyboard_clearable(self):
        toggle = re.search(
            r"function toggleCompare\(name\) \{(?P<body>.*?)\n\}",
            self.javascript,
            re.DOTALL,
        )
        self.assertIsNotNone(toggle)
        self.assertNotIn("openViewer", toggle.group("body"))
        self.assertIn(
            '$("#open-comparison").addEventListener("click", compareMarked)',
            self.javascript,
        )
        self.assertIn('event.key.toLowerCase() === "x"', self.javascript)
        for shortcut in ("ArrowDown", "A", "N", "U", "C", "X"):
            self.assertIn(shortcut, self.html)
        self.assertIn('aria-keyshortcuts="A"', self.html)
        self.assertIn("event.preventDefault()", self.javascript)

    def test_review_keyboard_supports_rapid_decisions_and_group_navigation(self):
        self.assertIn('event.key === "ArrowRight"', self.javascript)
        self.assertIn(
            'event.key === "ArrowLeft" || event.key === "ArrowUp"',
            self.javascript,
        )
        self.assertIn('event.key === "ArrowDown"', self.javascript)
        self.assertIn(
            'event.key.toLowerCase() === "a" && !event.repeat',
            self.javascript,
        )
        self.assertIn("void acceptSelectionAndNext();", self.javascript)
        self.assertIn(
            "cluster.photos[number - 1] && !event.repeat", self.javascript)
        self.assertIn(
            "Accept the current selection and move to the next group", self.html)

    def test_delivery_destination_has_native_folder_chooser(self):
        self.assertIn('id="pick-operation-destination"', self.html)
        self.assertIn("async function pickOperationDestination()", self.javascript)
        self.assertIn('"/api/action/pick-destination"', self.javascript)
        self.assertIn(
            '"click", pickOperationDestination', self.javascript)

    def test_delivery_auto_detects_interrupted_operation_journals(self):
        for identifier in (
            "recovery-operation-status", "recoverable-operations",
        ):
            self.assertIn(f'id="{identifier}"', self.html)
        self.assertIn(
            "async function refreshRecoverableOperations(", self.javascript)
        self.assertIn('fetch("/api/action/recovery")', self.javascript)
        self.assertIn(
            "async function resumeDetectedOperation(", self.javascript)
        self.assertIn("Resume remaining files", self.javascript)

    def test_file_operation_progress_recovers_from_polling_interruptions(self):
        self.assertIn(
            "progress refresh will retry", self.javascript)
        self.assertIn(
            "() => monitorOperation(operationId), 1500", self.javascript)
        self.assertIn(
            'document.addEventListener("visibilitychange"', self.javascript)
        self.assertIn(
            "if (!document.hidden && state.operationId)", self.javascript)

    def test_preview_polling_discards_stale_groups_and_recovers(self):
        self.assertIn("previewGeneration: {}", self.javascript)
        self.assertIn(
            "state.previewGeneration[size] !== generation", self.javascript)
        self.assertIn(
            "() => pollPreviews(names, size, generation), 1000",
            self.javascript,
        )
        self.assertIn(
            "() => focusPreviews(visible, prefetch, size), 1000",
            self.javascript,
        )

    def test_group_markers_use_optimistic_drafts_and_revert_failed_saves(self):
        self.assertIn(
            "const human = state.drafts.get(cluster.cluster_id)",
            self.javascript,
        )
        self.assertIn("saved.then((success) => {", self.javascript)
        self.assertIn("if (success) return;", self.javascript)

    def test_active_group_stays_visible_during_keyboard_navigation(self):
        self.assertIn(
            'target.querySelector(".cluster-item.active")', self.javascript)
        self.assertIn("activeItem.scrollIntoView({", self.javascript)
        self.assertIn('block: "nearest"', self.javascript)

    def test_phase2_group_browser_is_a_compact_source_list(self):
        for identifier in (
            "cluster-filter-menu", "active-filter-label",
            "cluster-window-controls", "person-filter",
        ):
            self.assertIn(f'id="{identifier}"', self.html)
        self.assertIn('class="cluster-filter-popover"', self.html)
        self.assertIn('class="cluster-browser-summary"', self.html)
        self.assertIn('visual.className = "cluster-visual"', self.javascript)
        self.assertIn('status.className = "cluster-row-status"', self.javascript)
        self.assertIn('$("#cluster-filter-menu").open = false', self.javascript)
        self.assertNotIn('class="filename-preview"', self.html)
        for selector in (
            ".cluster-filter-popover", ".cluster-visual",
            ".cluster-row-status", ".cluster-item-copy",
        ):
            self.assertIn(selector, self.css)

    def test_phase3_canvas_keeps_photographs_primary(self):
        for marker in (
            'class="photo-image-surface"', 'class="human-toggle selection-toggle"',
            'class="selection-mark"', 'class="photo-format-badge"',
            'class="photo-details-button"', 'class="photo-info" hidden',
        ):
            self.assertIn(marker, self.html)
        self.assertIn('card.classList.add("effective-selected")', self.javascript)
        self.assertIn('imageButton.addEventListener("dblclick"', self.javascript)
        self.assertIn('setTimeout(() => toggleHumanKeeper(cluster, name), 220)', self.javascript)
        self.assertIn('formatBadge.textContent = photoFormatLabel(name, rawFiles)', self.javascript)
        self.assertIn('details.hidden = !expanded', self.javascript)
        for selector in (
            ".photo-image-surface", ".selection-toggle",
            ".photo-format-badge", ".photo-details-copy",
        ):
            self.assertIn(selector, self.css)

    def test_phase4_contextual_inspector_centralizes_review_evidence(self):
        for identifier in (
            "inspector-toggle", "review-inspector", "inspector-group-panel",
            "inspector-photo-panel", "inspector-recovery-panel",
            "inspector-rationale", "inspector-photo-metadata",
            "inspector-photo-flag", "inspector-toggle-keeper",
            "inspector-open-recovery", "inspector-link-raw",
        ):
            self.assertIn(f'id="{identifier}"', self.html)
        self.assertIn('role="tablist" aria-label="Inspector sections"', self.html)
        for contract in (
            "function setInspectorTab(tab)", "function setInspectorOpen(open)",
            "function focusReviewPhoto(name", "function renderReviewInspector(cluster, decision)",
            "function appendInspectorMetadata(target, metrics)",
            'event.key.toLowerCase() === "i"',
        ):
            self.assertIn(contract, self.javascript)
        for selector in (
            ".review-inspector", ".inspector-tabs", ".inspector-photo-preview",
            ".inspector-metadata", ".inspector-annotation-grid",
        ):
            self.assertIn(selector, self.css)
        self.assertIn("#review-workspace .evidence { display: none; }", self.css)

    def test_phase3_queue_has_guided_intake_and_operational_summary(self):
        self.assertIn('class="job-intake-grid"', self.html)
        self.assertIn('class="step-number">1</span>', self.html)
        self.assertIn('class="step-number">2</span>', self.html)
        for identifier in (
            "queue-running-count", "queue-waiting-count",
            "queue-completed-count", "queue-attention-count",
        ):
            self.assertIn(f'id="{identifier}"', self.html)
        self.assertIn('id="job-form-status" class="job-form-status"', self.html)
        self.assertIn("function renderQueueOverview(", self.javascript)
        self.assertIn("const jobStatusPresentation =", self.javascript)

    def test_phase3_queue_intake_blocks_incomplete_jobs_and_explains_states(self):
        self.assertRegex(
            self.html,
            r'id="add-job" class="primary-button" type="button" disabled',
        )
        self.assertIn("function updateJobReadiness()", self.javascript)
        self.assertIn(
            '$("#job-photos").addEventListener("input", updateJobReadiness)',
            self.javascript,
        )
        for state_name in (
            "queued:", "running:", "paused:", "failed:",
            "cancelled:", "completed:",
        ):
            self.assertIn(state_name, self.javascript)
        self.assertIn(".queue-empty", self.css)
        self.assertIn(".job-progress-block", self.css)

    def test_phase4_provider_center_explains_privacy_and_agent_data_flow(self):
        for identifier in (
            "provider-local-count", "provider-zdr-count",
            "provider-attention-count", "provider-privacy-badge",
            "provider-credential-badge", "provider-routing-title",
            "provider-routing-description", "provider-secret-help",
        ):
            self.assertIn(f'id="{identifier}"', self.html)
        for agent in ("A", "B", "C", "D"):
            self.assertIn(f'data-agent="{agent}"', self.html)
        self.assertIn("Image pixels", self.html)
        self.assertIn("Text evidence", self.html)
        self.assertIn("Immutable queue binding", self.html)
        self.assertIn("function providerTrustPresentation(", self.javascript)
        self.assertIn("function renderProviderTrust(", self.javascript)

    def test_phase4_provider_results_are_structured_without_secret_return(self):
        self.assertIn("function renderProviderTestResult(", self.javascript)
        self.assertIn("provider-test-checks", self.javascript)
        self.assertIn("provider-vision-results", self.javascript)
        self.assertNotIn(
            '$("#provider-test-result").textContent = JSON.stringify',
            self.javascript,
        )
        self.assertIn("macOS Keychain protection.", self.html)
        self.assertIn("never return a", self.html)
        self.assertIn(".provider-trust-badge.local", self.css)
        self.assertIn(".provider-trust-badge.zdr", self.css)
        self.assertIn(".provider-test-failure", self.css)

    def test_phase5_people_workspace_exposes_private_index_and_review_state(self):
        for identifier in (
            "people-privacy-badge", "people-progress-label",
            "people-progress-percent", "people-group-count",
            "people-face-count", "people-confirmed-count",
            "people-review-count", "people-visible-count",
            "person-confirmation-badge", "person-face-selection",
        ):
            self.assertIn(f'id="{identifier}"', self.html)
        self.assertIn("Embeddings stay in the private local database", self.html)
        self.assertIn("Original photographs remain untouched", self.html)
        self.assertIn("function updateMergeSelection()", self.javascript)
        self.assertIn("function updateFaceSelection()", self.javascript)

    def test_phase5_people_supports_search_filters_and_deliberate_mutations(self):
        self.assertIn('id="people-search" type="search"', self.html)
        for value in ("all", "unconfirmed", "confirmed"):
            self.assertIn(f'data-people-filter="{value}"', self.html)
        self.assertRegex(
            self.html,
            r'id="merge-people" class="quiet-button" type="button" disabled',
        )
        self.assertRegex(
            self.html,
            r'id="split-person" class="quiet-button" type="button" disabled',
        )
        self.assertIn("state.mergeSelection", self.javascript)
        self.assertIn("state.faceSelection", self.javascript)
        self.assertIn(".face-confidence.low", self.css)
        self.assertIn(".person-danger-zone", self.css)

    def test_phase6_delivery_separates_exports_from_file_operations(self):
        self.assertIn("Your originals are protected by default", self.html)
        self.assertIn("No photographs changed", self.html)
        self.assertIn("Preflight never changes files", self.html)
        self.assertIn("Copies are hash-verified", self.html)
        for identifier in (
            "policy-explanation", "policy-title", "policy-description",
            "policy-risk", "operation-risk-badge", "operation-risk-message",
            "plan-review-warning", "operation-confirmation-block",
        ):
            self.assertIn(f'id="{identifier}"', self.html)
        self.assertIn("const selectionPolicyPresentation =", self.javascript)
        self.assertIn("function renderSelectionPolicy()", self.javascript)
        self.assertIn("function renderOperationRisk()", self.javascript)
        self.assertIn(".nonmutating-badge", self.css)
        self.assertIn(".operation-risk-badge.move", self.css)

    def test_phase6_preflight_and_receipt_make_mutation_deliberate(self):
        self.assertRegex(
            self.html,
            r'id="execute-operation" class="danger-button" type="button" disabled',
        )
        self.assertIn('inputmode="numeric"', self.html)
        self.assertIn('maxlength="4"', self.html)
        self.assertIn("plan.confirmation_code", self.javascript)
        self.assertIn("Type the four-digit code", self.javascript)
        for identifier in (
            "operation-state-title", "operation-state-badge",
            "operation-progress-percent", "operation-receipt",
            "receipt-operation", "receipt-files", "receipt-bytes",
            "receipt-destination", "receipt-journal",
        ):
            self.assertIn(f'id="{identifier}"', self.html)
        self.assertIn("Verified completion receipt", self.html)
        self.assertIn("Review changes invalidate old plans", self.html)
        self.assertIn("operation.journal_path", self.javascript)
        self.assertIn(".operation-receipt", self.css)

    def test_darkimiya_cleanup_is_separate_inspectable_and_raw_aware(self):
        self.assertIn(
            '<option value="system_cleanup">Final cleanup to macOS Trash…</option>',
            self.html)
        for identifier in (
            "cleanup-policy", "cleanup-inspector", "cleanup-candidate-list",
            "cleanup-select-all", "cleanup-select-none",
        ):
            self.assertIn(f'id="{identifier}"', self.html)
        for policy in (
            "preserve_useful_raws", "keep_every_raw", "jpeg_only", "inspect",
        ):
            self.assertIn(f'value="{policy}"', self.html)
        self.assertIn("async function loadCleanupCandidates", self.javascript)
        self.assertIn('action === "system_cleanup"', self.javascript)
        self.assertIn(".cleanup-candidate-list", self.css)

    def test_phase7_recovery_center_explains_predictable_interruptions(self):
        for identifier in (
            "recovery-button", "recovery-banner", "recovery-dialog",
            "recovery-health-title", "recovery-checks",
            "recovery-open-queue", "recovery-open-providers",
            "retry-current-review",
        ):
            self.assertIn(f'id="{identifier}"', self.html)
        for scenario in (
            "External drive unavailable", "Interrupted culling job",
            "Stale human review", "Preview cannot decode",
        ):
            self.assertIn(scenario, self.html)
        self.assertIn("function renderRecoveryCenter(", self.javascript)
        self.assertIn("function setRecoveryBanner(", self.javascript)
        self.assertIn(".recovery-check.failed", self.css)

    def test_phase7_first_result_path_is_optional_and_preserves_expert_flow(self):
        self.assertIn("Three steps to trustworthy culling", self.html)
        self.assertIn("Stored credentials are never shown here", self.html)
        self.assertIn("Expert workflows remain available directly", self.html)
        self.assertIn('id="open-first-cluster"', self.html)
        desktop = (
            Path(__file__).parents[1] / "opencull_desktop.py"
        ).read_text(encoding="utf-8")
        self.assertIn("Welcome · your first trustworthy result", desktop)
        self.assertIn("if not self.paths.onboarding_marker.exists()", desktop)
        self.assertIn("def dismiss_onboarding(self)", desktop)

    def test_phase8_cluster_navigation_is_bounded_for_large_libraries(self):
        for identifier in (
            "previous-cluster-window", "cluster-window-status",
            "next-cluster-window", "review-remaining",
        ):
            self.assertIn(f'id="{identifier}"', self.html)
        self.assertIn("clusterWindowSize: 200", self.javascript)
        self.assertIn("state.visible.slice(", self.javascript)
        self.assertIn("function moveClusterWindow(", self.javascript)
        self.assertIn('target.setAttribute("aria-busy", "true")', self.javascript)
        self.assertIn(".cluster-window-controls", self.css)

    def test_review_can_accept_and_advance_after_successful_save(self):
        self.assertIn('id="accept-ai-next"', self.html)
        self.assertIn("Accept selection &amp; Next", self.html)
        self.assertIn("async function acceptSelectionAndNext()", self.javascript)
        self.assertIn(
            "if (saved && state.activeClusterId === clusterId) navigate(1);",
            self.javascript)
        self.assertIn(
            '$("#accept-ai-next").addEventListener("click", acceptSelectionAndNext);',
            self.javascript)
        self.assertIn('event.key.toLowerCase() === "a" && !event.repeat',
                      self.javascript)

    def test_phase5_uses_a_persistent_compact_decision_bar(self):
        for identifier in (
            "skip-group", "review-more-actions", "actionbar-undo",
            "accept-ai-next", "review-note",
        ):
            self.assertIn(f'id="{identifier}"', self.html)
        self.assertIn('class="review-action-bar"', self.html)
        self.assertIn('class="inspector-review-note"', self.html)
        self.assertNotIn('class="human-review"', self.html)
        self.assertIn("function skipActiveGroup()", self.javascript)
        self.assertIn(
            '(draft) => ({...draft, reviewed: true}), "Selection accepted"',
            self.javascript)
        self.assertIn('.review-action-bar {', self.css)
        self.assertIn('position: fixed;', self.css[self.css.rfind(".review-action-bar {"):])

    def test_phase6_uses_quiet_progress_and_coordinated_recovery(self):
        self.assertIn('id="summary" class="summary"', self.html)
        self.assertIn('id="preview-status" class="status" role="status"', self.html)
        self.assertIn('aria-live="polite" hidden>Preview queue idle', self.html)
        for identifier in ("queue-preview-status", "queue-preview-details"):
            self.assertIn(f'id="{identifier}"', self.html)
        self.assertIn("recoveryIssues: new Map()", self.javascript)
        self.assertIn('copy.className = "project-progress-copy"', self.javascript)
        self.assertIn('progress.className = "project-review-progress"', self.javascript)
        self.assertIn("state.recoveryIssues.set(title", self.javascript)
        self.assertIn("additional issue", self.javascript)
        self.assertNotIn('target.append(alert(`Missing source files:', self.javascript)
        for selector in (
            ".project-progress-copy", ".project-review-progress",
            ".queue-preview-diagnostics", "#cluster-alerts:empty",
        ):
            self.assertIn(selector, self.css)

    def test_redesign_phase7_has_adaptive_semantic_visual_system(self):
        self.assertIn('content="dark light"', self.html)
        for token in (
            "--oc-window:", "--oc-toolbar:", "--oc-sidebar:",
            "--oc-surface:", "--oc-control:", "--oc-separator:",
            "--oc-text:", "--oc-text-secondary:", "--oc-accent:",
            "--oc-success:", "--oc-warning:", "--oc-danger:",
            "--oc-focus:", "--oc-control-height:",
        ):
            self.assertIn(token, self.css)
        self.assertIn("color-scheme: dark light", self.css)
        self.assertIn("@media (prefers-color-scheme: light)", self.css)
        self.assertIn("@media (prefers-contrast: more)", self.css)
        self.assertIn("@media (forced-colors: active)", self.css)
        self.assertIn('[role="button"]', self.css)

    def test_redesign_phase7_inspector_tabs_follow_keyboard_tab_pattern(self):
        for name in ("group", "photo", "recovery"):
            self.assertIn(f'id="inspector-tab-{name}"', self.html)
            self.assertIn(f'aria-controls="inspector-{name}-panel"', self.html)
            self.assertIn(f'aria-labelledby="inspector-tab-{name}"', self.html)
        self.assertIn("button.tabIndex = active ? 0 : -1", self.javascript)
        self.assertIn('event.key === "ArrowRight"', self.javascript)
        self.assertIn('event.key === "ArrowLeft"', self.javascript)
        self.assertIn('event.key === "Home"', self.javascript)
        self.assertIn('event.key === "End"', self.javascript)
        self.assertIn("inspectorTabs[target].focus()", self.javascript)

    def test_redesign_phase7_light_controls_and_recipes_are_legible(self):
        self.assertIn('summary.textContent = "Editing recipe"', self.javascript)
        self.assertNotIn("Capture One parameter and layer recipe", self.javascript)
        self.assertIn(".edit-direction-card > details > summary", self.css)
        self.assertIn("font-family: inherit", self.css)
        refinement = self.css[self.css.rfind(
            "/* Light appearance contrast corrections") :]
        self.assertIn('.search input,', refinement)
        self.assertIn('input[type="text"]', refinement)
        self.assertIn("background: #ffffff", refinement)
        self.assertIn(".professional-verdict .rationale", refinement)
        self.assertIn(".professional-evidence > .alert.fallback", refinement)
        self.assertIn('class="raw-evidence-disclosure"', self.html)
        self.assertIn(
            ".professional-evidence > .raw-evidence-disclosure", refinement)
        guardrail_style = refinement[
            refinement.index(".professional-evidence > .alert.fallback {") :]
        self.assertIn("margin-top: 18px", guardrail_style)
        self.assertIn("font-size: 11px", guardrail_style)
        self.assertIn(
            ".professional-evidence > .alert.fallback span", refinement)
        recipe_typography = refinement[
            refinement.index(".edit-direction-card .direction-recipe h5,") :]
        self.assertIn("font-family: inherit", recipe_typography)
        self.assertIn("font-size: 12px", recipe_typography)

    def test_personal_style_dialog_is_a_compact_aligned_form(self):
        for selector in (
            "#style-profile-dialog", "#style-profile-dialog .dialog-header",
            "#style-profile-dialog .shortlist-generate-body",
            "#style-profile-dialog .dialog-actions",
        ):
            self.assertIn(selector, self.css)
        self.assertIn("height: auto", self.css[self.css.rfind("#style-profile-dialog {") :])
        self.assertIn("grid-template-columns: 190px minmax(0, 1fr)", self.css)
        self.assertIn('id="style-profile-dialog"', self.html)

    def test_personal_style_examples_use_additive_native_file_pickers(self):
        for identifier in (
            "pick-style-photos", "pick-style-existing", "style-photos-summary",
            "style-photo-list",
        ):
            self.assertIn(f'id="{identifier}"', self.html)
        self.assertIn('id="style-photos" type="text" readonly', self.html)
        self.assertIn('id="style-existing" type="text" readonly', self.html)
        for endpoint in (
            '"/api/jobs/pick-style-photos"',
            '"/api/jobs/pick-style-profile"',
        ):
            self.assertIn(endpoint, self.javascript)
        self.assertIn("state.stylePhotoPaths = [...new Set", self.javascript)
        self.assertIn("function renderStylePhotoSelection()", self.javascript)
        self.assertIn("style-photo-remove", self.javascript)
        server = (Path(__file__).parents[1] / "opencull_gui" / "server.py").read_text(
            encoding="utf-8")
        jobs = (Path(__file__).parents[1] / "opencull_gui" / "jobs.py").read_text(
            encoding="utf-8")
        self.assertIn('body.get("photos", "")', server)
        self.assertIn("style-profile-examples", jobs)

    def test_provider_and_model_routes_are_customizable_for_style_workflows(self):
        self.assertIn('class="edit-direction-provider-field"', self.html)
        self.assertIn('id="edit-direction-provider"', self.html)
        self.assertIn("current.regeneration_required", self.javascript)
        self.assertIn("saved as a revision", self.html)
        self.assertIn("function updateShortlistPreviewAspect()", self.javascript)
        self.assertIn('stage.classList.toggle("preview-portrait"', self.javascript)
        self.assertIn("grid-template-rows: minmax(0, 1fr) minmax(0, 1fr)", self.css)
        self.assertIn("aspect-ratio: var(--preview-aspect-ratio, 3 / 2)", self.css)
        self.assertIn(
            "height: calc(100vh - var(--app-toolbar-height) - 42px)",
            self.css,
        )
        self.assertIn('id="regenerate-edit-direction"', self.html)
        self.assertIn("kimiya_validation?.status", self.javascript)
        self.assertIn("only_photo: onlyPhoto", self.javascript)
        self.assertIn("Regenerate this photograph", self.html)
        self.assertIn('id="generate-missing-edit-directions"', self.html)
        self.assertIn('id="regenerate-all-edit-directions"', self.html)
        self.assertIn("function chooseEditDirectionGeneration(current)", self.javascript)
        self.assertIn("only_photos: onlyPhotos", self.javascript)
        self.assertIn("openai/gpt-5.6-luna-pro", self.html)
        self.assertIn("openai/gpt-5.6-luna-pro", self.javascript)
        self.assertIn("for (const profile of state.providers?.profiles || [])", self.javascript)
        self.assertIn("AI model", self.html)
        self.assertIn('value="openai/gpt-5.6-luna-pro"', self.html)
        self.assertIn(".edit-direction-provider-field", self.css)
        self.assertIn('id="style-provider-help"', self.html)
        self.assertIn("function providerOptionLabel(profile)", self.javascript)
        self.assertIn("function providerKindLabel(kind)", self.javascript)
        self.assertIn('item.kind === "openrouter"', self.javascript)
        self.assertIn('options.model || "openai/gpt-5.6-luna-pro"', self.javascript)

    def test_style_profile_queue_has_live_stage_progress_semantics(self):
        self.assertIn('job.kind === "style_profile"', self.javascript)
        self.assertIn("function styleProfileProgress(job)", self.javascript)
        self.assertIn("STYLE_PROGRESS", self.javascript)
        self.assertIn("Starting the profile extractor", self.javascript)
        self.assertIn("latest validated extraction stage", self.javascript)
        self.assertIn("job.photo_examples?.length", self.javascript)
        self.assertIn("function jobDisplayName(job)", self.javascript)
        self.assertIn("Added ${jobTime(job.created_at)}", self.javascript)
        self.assertIn('id="style-extraction-progress"', self.html)
        self.assertIn("function renderStyleExtractionProgress()", self.javascript)
        self.assertIn("Personal style extraction started", self.javascript)
        self.assertIn('job.kind !== "style_profile"', self.javascript)
        self.assertNotIn(
            'setSaveStatus("Personal style extraction added to Queue")',
            self.javascript)
        self.assertIn("refreshJobs(true);", self.javascript)

    def test_phase8_exposes_concurrent_activity_and_keyboard_navigation(self):
        for identifier in (
            "review-activity", "culling-activity",
            "preview-activity", "people-activity",
        ):
            self.assertIn(f'id="{identifier}"', self.html)
        self.assertIn('aria-label="Concurrent activity"', self.html)
        self.assertIn('["ArrowDown", "ArrowUp", "Home", "End"]', self.javascript)
        self.assertIn('event.preventDefault();\n      openViewer([name]);', self.javascript)
        self.assertIn("@media (pointer: coarse)", self.css)
        self.assertIn(".background-activity strong.attention", self.css)

    def test_phase9_uses_shared_status_language_and_consistent_icons(self):
        self.assertIn("const productStatusLanguage =", self.javascript)
        for value in (
            'ready: "Ready"', 'working: "In progress"',
            'paused: "Paused safely"', 'attention: "Needs attention"',
            'verified: "Completed and verified"',
        ):
            self.assertIn(value, self.javascript)
        for icon in ("review-icon", "queue-icon", "people-icon"):
            self.assertIn(f'class="nav-icon {icon}"', self.html)
            self.assertIn(f".{icon}", self.css)
        self.assertNotIn('class="nav-icon" aria-hidden="true">', self.html)

    def test_phase9_shortcuts_and_feedback_are_accessible_and_non_destructive(self):
        for identifier in (
            "shortcuts-button", "shortcuts-dialog", "close-shortcuts",
            "dismiss-shortcuts", "toast-region",
        ):
            self.assertIn(f'id="{identifier}"', self.html)
        self.assertIn("Shortcuts never run copy, move, delete", self.html)
        self.assertIn('aria-keyshortcuts="?"', self.html)
        self.assertIn("function toggleShortcuts()", self.javascript)
        self.assertIn("function showToast(", self.javascript)
        self.assertIn('role="status"', self.html)
        self.assertIn(".toast.error", self.css)
        self.assertIn("@keyframes dialog-enter", self.css)


class GuiJobTests(unittest.TestCase):
    def test_development_recipe_preview_is_bounded_local_and_cached(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"; photos.mkdir()
            Image.new("RGB", (320, 200), (70, 110, 150)).save(photos / "A.JPG")
            workspace = development_workspace(photos, entries=[{"photo": "A.JPG"}])
            first = workspace.recipe_preview(
                "A.JPG", "calibrated", "default",
                "markesteijn-3-pass", 96)
            second = workspace.recipe_preview(
                "A.JPG", "calibrated", "default",
                "markesteijn-3-pass", 96)
            self.assertEqual(first, second)
            self.assertTrue(first.is_file())
            with Image.open(first) as preview:
                self.assertLessEqual(max(preview.size), 96)
            # The source photograph is what the render was made from, and it
            # must come back untouched.
            with Image.open(photos / "A.JPG") as original:
                self.assertEqual(original.size, (320, 200))

    def test_treatments_offer_only_what_can_actually_be_rendered(self):
        with tempfile.TemporaryDirectory() as temporary:
            photos = Path(temporary) / "photos"; photos.mkdir()
            workspace = development_workspace(photos, entries=[{
                "photo": "A.JPG",
                "standard_recipe": "warm it slightly",
                "standard_title": "Standard",
                "signature_recipe": "",
            }])
            offered = {item["id"] for item in workspace.treatments("A.JPG")}
            # The calibrated baseline needs no recipe, so it is always there.
            self.assertIn("calibrated", offered)
            self.assertIn("standard", offered)
            # A treatment with no recipe behind it would fail at the moment it
            # was chosen, which is worse than not offering it.
            self.assertNotIn("signature", offered)
            self.assertNotIn("creative", offered)

    def test_a_photograph_without_directions_still_has_the_baseline(self):
        # A folder that has only been culled has no edit directions, and that
        # is the ordinary case. Develop must still mean something there: the
        # baseline interprets nothing, so it needs nothing suggested first.
        with tempfile.TemporaryDirectory() as temporary:
            photos = Path(temporary) / "photos"; photos.mkdir()
            workspace = development_workspace(photos)
            self.assertEqual(
                [item["id"] for item in workspace.treatments("A.JPG")],
                ["calibrated"])

    def test_the_baseline_renders_without_any_edit_direction(self):
        with tempfile.TemporaryDirectory() as temporary:
            photos = Path(temporary) / "photos"; photos.mkdir()
            Image.new("RGB", (240, 160), (90, 120, 80)).save(photos / "A.JPG")
            workspace = development_workspace(photos)
            rendered = workspace.recipe_preview(
                "A.JPG", "calibrated", "default", "markesteijn-3-pass", 80)
            self.assertTrue(rendered.is_file())
            # Everything else does still need one, because it would be
            # inventing an intent nobody expressed.
            with self.assertRaises(ValueError):
                workspace.recipe_preview(
                    "A.JPG", "signature", "default", "markesteijn-3-pass", 80)

    def test_the_three_decoders_are_ranked_and_named_honestly(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"; photos.mkdir()
            raws = root / "raws"; raws.mkdir()
            (raws / "A.ARW").write_bytes(b"raw")
            workspace = development_workspace(
                photos, raw_root=raws, raw_matches={"A.JPG": ["A.ARW"]})

            best = workspace.decoder_for("A.JPG", {"darktable", "libraw"})
            self.assertEqual(best["id"], "darktable")
            self.assertEqual(best["engine"], "darktable")

            # Without darktable, OpenCull's own LibRaw renderer -- which is a
            # real decode, not the camera's rendering.
            fallback = workspace.decoder_for("A.JPG", {"libraw"})
            self.assertEqual(fallback["id"], "libraw")
            self.assertEqual(fallback["engine"], "default")
            self.assertIn("scene-linear", fallback["note"])

            last = workspace.decoder_for("A.JPG", set())
            self.assertEqual(last["id"], "embedded")
            self.assertIn("not a development", last["note"])

            # A photograph with no RAW behind it is none of the above.
            plain = workspace.decoder_for("B.JPG", {"darktable", "libraw"})
            self.assertEqual(plain["id"], "rendered")

    def test_a_raw_is_decoded_by_libraw_rather_than_read_from_the_camera(self):
        from opencull_gui import development

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"; photos.mkdir()
            # A blue reference JPEG beside a RAW that decodes to red. If the
            # render comes out blue, the decoder was never called.
            Image.new("RGB", (120, 90), (30, 60, 200)).save(photos / "A.JPG")
            raws = root / "raws"; raws.mkdir()
            (raws / "A.ARW").write_bytes(b"raw")
            workspace = development_workspace(
                photos, raw_root=raws, raw_matches={"A.JPG": ["A.ARW"]},
                decoders={"libraw"})

            decoded = root / "decoded.tiff"

            def fake_baseline(source, output_dir, **_kwargs):
                red = np.zeros((90, 120, 3), dtype=np.uint16)
                red[..., 0] = 40000
                tifffile.imwrite(decoded, red)
                return {"outputs": {"linear_tiff": {"path": str(decoded)}}}

            with patch.object(development, "render_baseline", fake_baseline):
                rendered = workspace.recipe_preview(
                    "A.JPG", "calibrated", "default", "markesteijn-3-pass", 64)

            with Image.open(rendered) as opened:
                pixels = np.asarray(opened.convert("RGB"), dtype=np.float32)
            self.assertGreater(pixels[..., 0].mean(), pixels[..., 2].mean())

    def test_a_full_render_is_the_photograph_s_own_size_and_is_recorded(self):
        with tempfile.TemporaryDirectory() as temporary:
            photos = Path(temporary) / "photos"; photos.mkdir()
            Image.new("RGB", (400, 260), (90, 110, 130)).save(photos / "A.JPG")
            workspace = development_workspace(photos)

            result = workspace.render_full(
                "A.JPG", "calibrated", "default", "markesteijn-3-pass")

            with Image.open(result["render"]["path"]) as rendered:
                # The proof on screen is bounded; what gets delivered is not.
                self.assertEqual(max(rendered.size), 400)
            renders = workspace.project["artifacts"]["renders"]
            self.assertEqual(len(renders), 1)
            self.assertEqual(renders[0]["source_photo"], "A.JPG")
            self.assertTrue(renders[0]["sha256"])

    def test_only_a_registered_render_can_be_exported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"; photos.mkdir()
            workspace = development_workspace(photos)
            stranger = root / "somebody-elses.jpg"
            stranger.write_bytes(b"not ours")
            with self.assertRaises(ValueError):
                workspace.export_render(str(stranger), str(root / "out.jpg"))
            self.assertFalse((root / "out.jpg").exists())

    def test_exporting_writes_the_render_where_it_was_asked_for(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"; photos.mkdir()
            Image.new("RGB", (120, 90), (90, 110, 130)).save(photos / "A.JPG")
            workspace = development_workspace(photos)
            result = workspace.render_full(
                "A.JPG", "calibrated", "default", "markesteijn-3-pass")

            record = workspace.export_render(
                str(result["render"]["path"]), str(root / "delivery" / "A.jpg"))

            written = Path(record["destination"])
            self.assertTrue(written.is_file())
            self.assertEqual(written.name, "A.jpg")
            self.assertEqual(
                workspace.project["artifacts"]["exports"][0]["sha256"],
                record["sha256"])

    def test_an_export_never_writes_over_a_file_that_is_already_there(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"; photos.mkdir()
            Image.new("RGB", (120, 90), (90, 110, 130)).save(photos / "A.JPG")
            workspace = development_workspace(photos)
            result = workspace.render_full(
                "A.JPG", "calibrated", "default", "markesteijn-3-pass")
            target = root / "A.jpg"
            target.write_bytes(b"somebody's existing file")

            record = workspace.export_render(
                str(result["render"]["path"]), str(target))

            self.assertEqual(target.read_bytes(), b"somebody's existing file")
            self.assertEqual(Path(record["destination"]).name, "A-2.jpg")
            # The record says both what was asked for and what was made, so
            # the difference is not silent.
            self.assertEqual(
                Path(record["requested_destination"]).name, "A.jpg")

    def test_a_delivery_name_says_the_frame_and_the_treatment(self):
        from opencull_gui.development import suggested_filename

        self.assertEqual(
            suggested_filename("DSCF0021.RAF", "personal-darktable-guided"),
            "DSCF0021-personal-darktable-guided.jpg")
        self.assertEqual(
            suggested_filename("A.JPG", "Calibrated Baseline!"),
            "A-calibrated-baseline.jpg")

    def test_shrinking_a_baseline_bounds_it_and_keeps_it_linear(self):
        from opencull_gui.development import shrink_linear

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "big.tiff"
            # Half the frame at full white, half at black. Averaged in linear
            # light the result is 0.5; averaged after display encoding it
            # would land near 0.73, which is the mistake this avoids.
            data = np.zeros((100, 200, 3), dtype=np.uint16)
            data[:, :100] = 65535
            tifffile.imwrite(source, data)
            output = shrink_linear(source, 50, root / "small.tiff")
            shrunk = np.asarray(tifffile.imread(output), dtype=np.float32)
            self.assertEqual(shrunk.shape[:2], (25, 50))
            self.assertAlmostEqual(
                float(shrunk.mean()) / 65535.0, 0.5, places=2)

    def test_a_baseline_smaller_than_the_proof_is_left_alone(self):
        from opencull_gui.development import shrink_linear

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "small.tiff"
            tifffile.imwrite(
                source, np.full((40, 60, 3), 12345, dtype=np.uint16))
            output = shrink_linear(source, 400, root / "out.tiff")
            self.assertEqual(
                np.asarray(tifffile.imread(output)).shape[:2], (40, 60))

    def test_full_resolution_darktable_job_uses_delivery_pipeline(self):
        manager = JobManager.__new__(JobManager)
        manager.python = "/usr/bin/python3"
        manager.project_root = Path("/Applications/OpenCull.app/Resources")
        command = manager._command({
            "kind": "renderer_export",
            "source": "/tmp/A.RAF", "reference": "/tmp/A.JPG",
            "directions": "/tmp/directions.json", "photo": "A.JPG",
            "style": "personal", "output_dir": "/tmp/full",
            "project": "/tmp/project.json",
            "demosaic": "markesteijn-3-pass",
        })
        self.assertTrue(command[1].endswith("renderer_export_pipeline.py"))
        self.assertIn("markesteijn-3-pass", command)
        self.assertNotIn("--opencull-render", command)

    def test_delivery_export_is_queued_and_rejects_an_active_duplicate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"; photos.mkdir()
            reference = photos / "A.JPG"
            Image.new("RGB", (32, 24), (80, 100, 120)).save(reference)
            directions = photos / ".darkimiya" / "directions.json"
            directions.parent.mkdir()
            directions.write_text('{"entries": []}', encoding="utf-8")
            project_path, _ = load_or_create_folder_project(photos)
            layout = ensure_project_layout(photos)
            manager = JobManager(
                root / "jobs.json", root, autostart=False,
                command_builder=lambda job: [])
            first = manager.add_delivery_export(
                str(reference), str(reference), str(directions), "A.JPG",
                "standard", "default", str(project_path),
                str(layout["Exports"] / "A-standard-default.jpg"))
            job = first["jobs"][-1]
            self.assertEqual(job["kind"], "delivery_export")
            self.assertEqual(job["status"], "queued")
            self.assertEqual(job["execution_lane"], "background_export")
            with self.assertRaisesRegex(JobError, "already queued for export"):
                manager.add_delivery_export(
                    str(reference), str(reference), str(directions), "A.JPG",
                    "standard", "default", str(project_path),
                    str(layout["Exports"] / "A-standard-default.jpg"))

    def test_delivery_export_command_carries_the_immutable_recipe_identity(self):
        manager = JobManager.__new__(JobManager)
        manager.python = "/usr/bin/python3"
        manager.project_root = Path("/Applications/Darkimiya.app/Contents/Resources")
        command = manager._command({
            "kind": "delivery_export", "source": "/tmp/A.RAF",
            "reference": "/tmp/A.JPG", "directions": "/tmp/directions.json",
            "photo": "A.JPG", "style": "standard", "engine": "darktable",
            "demosaic": "markesteijn-3-pass", "render_output_dir": "/tmp/render",
            "project": "/tmp/project.json", "destination": "/tmp/export.jpg",
            "export_key": "recipe-key", "output": "/tmp/receipt.json",
            "render": "",
        })
        self.assertTrue(command[1].endswith("delivery_export_pipeline.py"))
        self.assertIn("recipe-key", command)
        self.assertIn("/tmp/export.jpg", command)

    def test_delivery_export_atomically_copies_and_records_an_existing_render(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"; photos.mkdir()
            reference = photos / "A.JPG"
            Image.new("RGB", (32, 24), (80, 100, 120)).save(reference)
            project_path, _ = load_or_create_folder_project(photos)
            layout = ensure_project_layout(photos)
            rendered = layout["Developments"] / "A.standard.default.jpg"
            Image.new("RGB", (32, 24), (90, 110, 130)).save(rendered)
            register_render(project_path, {
                "created_at": "2026-08-02T00:00:00+00:00",
                "recipe": {"style": "standard", "source_photo": "A.JPG"},
                "output": {"path": str(rendered), "sha256": "render-sha"},
            })
            directions = layout["Recipes"] / "directions.json"
            directions.write_text('{"entries": []}', encoding="utf-8")
            destination = layout["Exports"] / "A-standard-default.jpg"
            receipt = layout["Operations"] / "export-test.json"
            record = run_delivery_export(
                source=reference, reference=reference, directions=directions,
                photo="A.JPG", style="standard", engine="default",
                demosaic="markesteijn-1-pass", render=rendered,
                render_output_dir=layout["Developments"], project=project_path,
                destination=destination, export_key="recipe-key", receipt=receipt)
            self.assertTrue(destination.is_file())
            self.assertTrue(receipt.is_file())
            self.assertEqual(record["destination"], str(destination))
            self.assertEqual(
                load_project(project_path)["artifacts"]["exports"][-1]["export_key"],
                "recipe-key")

    def test_full_resolution_export_has_an_isolated_background_resource_lane(self):
        manager = JobManager.__new__(JobManager)
        manager.project_root = Path("/Applications/Darkimiya.app/Contents/Resources")
        export_environment = manager._environment_for_job({
            "kind": "renderer_export",
        })
        preview_environment = manager._environment_for_job({
            "kind": "development_render",
        })
        self.assertEqual(export_environment["DARKIMIYA_BACKGROUND_EXPORT"], "1")
        self.assertEqual(export_environment["DARKIMIYA_EXPORT_NICE"], "10")
        self.assertEqual(export_environment["OMP_NUM_THREADS"], "2")
        self.assertEqual(export_environment["VECLIB_MAXIMUM_THREADS"], "2")
        self.assertNotIn("DARKIMIYA_BACKGROUND_EXPORT", preview_environment)

    def test_calibrated_is_a_supported_development_treatment(self):
        jobs = (Path(__file__).parents[1] / "opencull_gui" / "jobs.py").read_text(
            encoding="utf-8")
        javascript = (
            Path(__file__).parents[1] / "opencull_gui" / "static" / "app.js"
        ).read_text(encoding="utf-8")
        self.assertIn('"calibrated", "standard", "signature"', jobs)
        self.assertIn('active === "calibrated"', javascript)
        self.assertIn("developmentRenderFor", javascript)
        self.assertIn("Render with ${selectedEngine", javascript)

    def test_edit_direction_worker_receives_personal_style_profile(self):
        manager = JobManager.__new__(JobManager)
        manager.python = "/usr/bin/python3"
        manager.project_root = Path("/Applications/OpenCull.app/Resources")
        command = manager._command({
            "kind": "edit_suggestions",
            "program_path": "/tmp/edit_suggestions.kim",
            "shortlist": "/tmp/shortlist.json",
            "review": "/tmp/review.json",
            "photos": "/tmp/photos",
            "output": "/tmp/directions.json",
            "profile": "professional",
            "style_profile": "/tmp/personal-style.json",
        })
        self.assertIn(
            "style_profile=/tmp/personal-style.json",
            command,
        )
        self.assertIn("only_photo=", command)
        self.assertIn("only_photos=[]", command)

    def wait_for(self, manager, predicate, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = manager.public()
            if predicate(state):
                return state
            time.sleep(0.02)
        self.fail("job manager condition timed out")

    def test_development_image_is_not_parsed_as_json_progress(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "A.standard.jpg"
            output.write_bytes(b"\xff\xd8\xff\xe0binary-jpeg")

            progress = JobManager._progress({
                "kind": "development_pipeline",
                "output": str(output),
                "checkpoint": str(output),
            })

            self.assertEqual(progress["completed_items"], 1)
            self.assertEqual(progress["total_items"], 1)
            self.assertEqual(progress["fraction"], 1)
            self.assertTrue(progress["checkpoint_complete"])

    def test_sequential_queue_completes_in_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            order = root / "order.txt"

            def command(job):
                script = (
                    "import pathlib,time,sys;"
                    "order=pathlib.Path(sys.argv[1]);"
                    "output=pathlib.Path(sys.argv[2]);"
                    "order.open('a').write(output.stem+'-start\\n');"
                    "time.sleep(.08);"
                    "output.write_text('{}');"
                    "order.open('a').write(output.stem+'-end\\n')"
                )
                return [sys.executable, "-c", script, str(order), job["output"]]

            manager = JobManager(
                root / "jobs.json", root, command_builder=command)
            try:
                manager.add(str(first), str(root / "one.json"))
                manager.add(str(second), str(root / "two.json"))
                state = self.wait_for(
                    manager,
                    lambda value: len(value["jobs"]) == 2 and all(
                        job["status"] == "completed" for job in value["jobs"]),
                )
                self.assertIsNone(state["active_job_id"])
                self.assertEqual(order.read_text().splitlines(), [
                    "one-start", "one-end", "two-start", "two-end"])
            finally:
                manager.shutdown()

    def test_command_builder_failure_does_not_kill_queue_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()

            def command(job):
                if Path(job["photos"]).name == "first":
                    raise KeyError("style-only command field")
                return [
                    sys.executable, "-c",
                    "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('{}')",
                    job["output"],
                ]

            manager = JobManager(
                root / "jobs.json", root, command_builder=command)
            try:
                manager.add(str(first), str(root / "one.json"))
                manager.add(str(second), str(root / "two.json"))
                state = self.wait_for(
                    manager,
                    lambda value: len(value["jobs"]) == 2
                    and value["jobs"][0]["status"] == "failed"
                    and value["jobs"][1]["status"] == "completed",
                )
                self.assertIn(
                    "Could not start Kimiya", state["jobs"][0]["message"])
                self.assertTrue((root / "two.json").is_file())
            finally:
                manager.shutdown()

    def test_queued_root_style_output_is_migrated_to_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            results = root / "Results"
            results.mkdir()
            state_path = root / "jobs.json"
            state_path.write_text(json.dumps({
                "format": "opencull-job-queue-v1",
                "revision": 1,
                "jobs": [{
                    "id": "style-job", "kind": "style_profile",
                    "output": "/Personal_profile_v1.json",
                    "checkpoint": "/Personal_profile_v1.json",
                    "log": "/Personal_profile_v1.json.log",
                    "status": "queued",
                }],
            }), encoding="utf-8")
            manager = JobManager(
                state_path, root, output_root=results,
                command_builder=lambda job: [], autostart=False)
            try:
                job = manager.public()["jobs"][0]
                self.assertEqual(
                    Path(job["output"]),
                    results.resolve() / "Personal_profile_v1.json")
                self.assertEqual(Path(job["log"]).parent, results.resolve())
            finally:
                manager.shutdown()

    def test_worker_receives_private_job_specific_kimiya_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            workspaces = root / "private-kimiya"

            def command(job):
                script = (
                    "import os,pathlib,sys;"
                    "pathlib.Path(sys.argv[1]).write_text("
                    "os.environ.get('KIMIYA_WORKSPACE',''))")
                return [sys.executable, "-c", script, job["output"]]

            manager = JobManager(
                root / "jobs.json", root, command_builder=command,
                kimiya_workspace_root=workspaces)
            try:
                state = manager.add(
                    str(photos), str(root / "result.json"))
                job_id = state["jobs"][0]["id"]
                completed = self.wait_for(
                    manager,
                    lambda value:
                    value["jobs"][0]["status"] == "completed")
                reported = Path(
                    completed["jobs"][0]["output"]).read_text()
                expected = (workspaces / job_id).resolve()
                self.assertEqual(Path(reported), expected)
                self.assertTrue(expected.is_dir())
                self.assertEqual(expected.stat().st_mode & 0o077, 0)
            finally:
                manager.shutdown()

    def test_recovers_legacy_bundle_report_from_committed_certificate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "jobs.json"
            workspaces = root / "Kimiya"
            results = root / "Results"
            results.mkdir()
            legacy = (
                root / "dist" / "OpenCull.app" / "Contents" /
                "Frameworks" / "607_FUJI-results.json")
            state_path.write_text(json.dumps({
                "format": "opencull-job-queue-v1",
                "revision": 1,
                "jobs": [{
                    "id": "legacy-job",
                    "photos": str(root / "photos"),
                    "output": str(legacy),
                    "checkpoint": f"{legacy}.checkpoint.json",
                    "log": f"{legacy}.log",
                    "status": "completed",
                }, {
                    "id": "shortlist-job",
                    "kind": "professional_shortlist",
                    "report": str(legacy),
                    "output": str(results / "shortlist.json"),
                    "checkpoint": str(results / "shortlist.checkpoint.json"),
                    "log": str(results / "shortlist.log"),
                    "status": "completed",
                }],
            }), encoding="utf-8")
            certificate = workspaces / "legacy-job" / "certificate.json"
            certificate.parent.mkdir(parents=True)
            committed_text = json.dumps(report_data(), separators=(",", ":"))
            certificate.write_text(json.dumps({
                "status": "COMMITTED",
                "value": committed_text,
            }), encoding="utf-8")

            manager = JobManager(
                state_path, root, autostart=False,
                kimiya_workspace_root=workspaces, output_root=results)
            try:
                jobs = manager.public()["jobs"]
                recovered = (results / "607_FUJI-results.json").resolve()
                self.assertTrue(recovered.is_file())
                self.assertEqual(jobs[0]["output"], str(recovered))
                self.assertEqual(
                    jobs[0]["legacy_output"], str(legacy.resolve()))
                self.assertEqual(jobs[1]["report"], str(recovered))
                self.assertEqual(
                    manager.resolve_report_path(legacy), recovered)
                self.assertEqual(
                    json.loads(recovered.read_text())["format"],
                    "opencull-report-v2",
                )
                self.assertEqual(recovered.read_text(), committed_text)
            finally:
                manager.shutdown()

            recovered.write_text(
                json.dumps(report_data(), indent=2), encoding="utf-8")
            repaired = JobManager(
                state_path, root, autostart=False,
                kimiya_workspace_root=workspaces, output_root=results)
            try:
                self.assertEqual(recovered.read_text(), committed_text)
                self.assertIn(
                    "identity verified",
                    repaired.public()["jobs"][0]["message"],
                )
            finally:
                repaired.shutdown()

    def test_pause_and_resume_uses_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()

            def command(job):
                script = (
                    "import json,pathlib,time,sys;"
                    "checkpoint=pathlib.Path(sys.argv[1]);"
                    "output=pathlib.Path(sys.argv[2]);"
                    "exists=checkpoint.exists();"
                    "checkpoint.write_text(json.dumps({"
                    "'signature':{'cluster_ids':['a','b']},"
                    "'decisions':[{}],'completed':False}));"
                    "output.write_text('{}') if exists else time.sleep(5)"
                )
                return [
                    sys.executable, "-c", script,
                    job["checkpoint"], job["output"],
                ]

            manager = JobManager(
                root / "jobs.json", root, command_builder=command)
            try:
                state = manager.add(str(photos), str(root / "result.json"))
                job_id = state["jobs"][0]["id"]
                self.wait_for(
                    manager,
                    lambda value:
                    value["jobs"][0]["status"] == "running"
                    and value["jobs"][0]["progress"]["completed_clusters"] == 1)
                manager.action(job_id, "pause")
                paused = self.wait_for(
                    manager,
                    lambda value: value["jobs"][0]["status"] == "paused")
                self.assertEqual(
                    paused["jobs"][0]["progress"]["completed_clusters"], 1)
                manager.action(job_id, "resume")
                completed = self.wait_for(
                    manager,
                    lambda value: value["jobs"][0]["status"] == "completed")
                self.assertTrue(Path(completed["jobs"][0]["output"]).is_file())
            finally:
                manager.shutdown()

    def test_restart_marks_dead_process_paused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            state_path = root / "jobs.json"
            state_path.write_text(json.dumps({
                "format": "opencull-job-queue-v1",
                "revision": 3,
                "created_at": "now",
                "updated_at": "now",
                "jobs": [{
                    "id": "recover",
                    "photos": str(photos),
                    "output": str(root / "result.json"),
                    "checkpoint": str(root / "result.json.checkpoint.json"),
                    "log": str(root / "result.json.log"),
                    "status": "running",
                    "pid": 99999999,
                    "keep_per_group": 2,
                    "recursive": False,
                    "profile": "family",
                }],
            }), encoding="utf-8")
            manager = JobManager(
                state_path, root, command_builder=lambda job: [],
                autostart=False)
            try:
                recovered = manager.public()["jobs"][0]
                self.assertEqual(recovered["status"], "paused")
                self.assertIn("restart", recovered["message"].lower())
            finally:
                manager.shutdown()

    def test_rejects_duplicate_folder_and_existing_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            manager = JobManager(
                root / "jobs.json", root, command_builder=lambda job: [],
                autostart=False)
            try:
                manager.add(str(photos), str(root / "result.json"))
                with self.assertRaisesRegex(JobError, "already being culled"):
                    manager.add(str(photos), str(root / "other.json"))
                existing = root / "existing.json"
                existing.write_text("{}")
                another = root / "another"
                another.mkdir()
                with self.assertRaisesRegex(JobError, "already exists"):
                    manager.add(str(another), str(existing))
            finally:
                manager.shutdown()

    def test_a_failed_run_does_not_push_the_default_output_aside(self):
        """The retry inherits the failed run's output, and its checkpoint."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            reports = root / "Reports"
            reports.mkdir()
            manager = JobManager(
                root / "jobs.json", root, command_builder=lambda job: [],
                autostart=False)
            try:
                manager._state["jobs"].append({
                    "id": "f1", "kind": "culling", "photos": str(photos),
                    "output": str(reports / "photos-results.json"),
                    "checkpoint": str(reports / "photos-results.json"
                                      ".checkpoint.json"),
                    "log": str(reports / "photos-results.json.log"),
                    "status": "failed"})
                chosen = manager._default_output(photos, reports)
                self.assertEqual(chosen, reports / "photos-results.json")
                manager._state["jobs"][-1]["status"] = "queued"
                busy = manager._default_output(photos, reports)
                self.assertEqual(busy, reports / "photos-results-2.json")
            finally:
                manager.shutdown()

    def test_a_refused_certification_is_retried_once_with_a_fresh_panel(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            manager = JobManager(
                root / "jobs.json", root, command_builder=lambda job: [],
                autostart=False)
            try:
                checkpoint = root / "r.json.checkpoint.json"
                checkpoint.write_text("{}")
                failed = {
                    "id": "f1", "kind": "culling", "photos": str(photos),
                    "output": str(root / "r.json"),
                    "checkpoint": str(checkpoint),
                    "log": str(root / "r.log"), "status": "failed",
                    "exit_code": 2, "keep_per_group": 2,
                    "recursive": True, "profile": "family",
                    "judgment_policy": {"panel": ["C", "D"], "votes": 5,
                                        "required": 4},
                    "only_photos": []}
                manager._state["jobs"].append(failed)
                manager._maybe_retry_certification(failed)
                retried = manager._state["jobs"][-1]
                self.assertEqual(retried["status"], "queued")
                self.assertTrue(retried["auto_retry"])
                self.assertEqual(retried["output"], failed["output"])
                self.assertTrue(failed["auto_retried"])
                # The retry itself never retries: one exception, handled once.
                retried.update(status="failed", exit_code=2)
                before = len(manager._state["jobs"])
                manager._maybe_retry_certification(retried)
                self.assertEqual(len(manager._state["jobs"]), before)
                # A crash (not an abstention) is not retried either.
                failed2 = dict(failed, id="f2", auto_retried=None,
                               exit_code=1)
                manager._state["jobs"][0] = failed2
                manager._maybe_retry_certification(failed2)
                self.assertEqual(len(manager._state["jobs"]), before)
            finally:
                manager.shutdown()

    def test_a_detached_run_can_be_paused_when_the_pid_is_provably_ours(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = JobManager(
                root / "jobs.json", root, command_builder=lambda job: [],
                autostart=False)
            try:
                job = {
                    "id": "d1", "kind": "culling", "photos": str(root),
                    "output": str(root / "r.json"),
                    "checkpoint": str(root / "c.json"),
                    "log": str(root / "l.log"),
                    "status": "detached", "pid": 12345}
                manager._state["jobs"].append(job)
                import opencull_gui.jobs as jobs_module

                with patch.object(
                    jobs_module, "_pid_runs_job", return_value=True,
                ), patch.object(jobs_module.os, "kill") as kill:
                    manager.action("d1", "pause")
                kill.assert_called_once_with(
                    12345, jobs_module.signal.SIGTERM)
                self.assertEqual(job["status"], "detached")
                self.assertIn("checkpoint keeps", job["message"])
                with patch.object(
                    jobs_module, "_pid_runs_job", return_value=False,
                ):
                    with self.assertRaisesRegex(JobError, "running job"):
                        manager.action("d1", "pause")
            finally:
                manager.shutdown()

    def test_the_queue_guard_says_what_is_actually_happening(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            manager = JobManager(
                root / "jobs.json", root, command_builder=lambda job: [],
                autostart=False)
            try:
                blocking = {
                    "id": "b1", "kind": "culling", "photos": str(photos),
                    "output": str(root / "r.json"),
                    "checkpoint": str(root / "r.json.checkpoint.json"),
                    "log": str(root / "r.log"), "status": "running"}
                manager._state["jobs"].append(blocking)
                with self.assertRaisesRegex(JobError, "already being culled"):
                    manager.add(str(photos), str(root / "other.json"))
                blocking["status"] = "paused"
                with self.assertRaisesRegex(JobError, "paused mid-run"):
                    manager.add(str(photos), str(root / "other.json"))
            finally:
                manager.shutdown()

    def test_queue_guard_tolerates_jobs_that_have_no_folder(self):
        """A verification job carries no photos; it must not break culling."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            manager = JobManager(
                root / "jobs.json", root, command_builder=lambda job: [],
                autostart=False)
            try:
                manager._state["jobs"].append({
                    "id": "v1", "kind": "semantic_verification",
                    "checkpoint": str(root / "v1.json"),
                    "log": str(root / "v1.log"), "status": "queued"})
                manager.add(str(photos), str(root / "result.json"))
                self.assertEqual(
                    manager.public()["jobs"][-1]["kind"], "culling")
            finally:
                manager.shutdown()


class GuiHttpTests(unittest.TestCase):
    def test_development_payload_reloads_worker_registered_render(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            workspace = development_workspace(photos)

            # A render registered by a separate worker process, after the
            # workspace was constructed.
            output = root / "A.standard.jpg"
            output.write_bytes(b"render")
            update_project(workspace.project_path, artifacts={"renders": [{
                "variant": "standard", "source_photo": "A.JPG",
                "path": str(output), "sha256": "render-hash",
            }]})

            payload = workspace.payload()

            self.assertEqual(len(payload["variants"]), 1)
            self.assertEqual(payload["variants"][0]["source_photo"], "A.JPG")
            self.assertEqual(
                workspace.project["artifacts"]["renders"][0]["sha256"],
                "render-hash")
            self.assertEqual(
                payload["default_export_directory"],
                str((photos / ".darkimiya" / "Exports").resolve()))

    def test_export_payload_shows_latest_semantic_render_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            workspace = development_workspace(photos)
            older = root / "older.jpg"; older.write_bytes(b"older")
            current = root / "current.jpg"; current.write_bytes(b"current")
            update_project(workspace.project_path, artifacts={"renders": [
                {"variant": "personal-darktable-guided", "source_photo": "A.RAF",
                 "path": str(older), "recipe_revision": 1,
                 "created_at": "2026-01-01T00:00:00+00:00"},
                {"variant": "personal-darktable-guided", "source_photo": "A.RAF",
                 "path": str(current), "recipe_revision": 2,
                 "created_at": "2026-01-02T00:00:00+00:00"},
            ]})

            payload = workspace.export_payload()

            self.assertEqual(len(payload["renders"]), 1)
            self.assertEqual(payload["renders"][0]["path"], str(current))
            self.assertEqual(
                payload["renders"][0]["suggested_filename"],
                "A-personal-darktable-guided.jpg")
            self.assertEqual(payload["hidden_render_revisions"], 1)
            self.assertEqual(
                payload["default_export_directory"],
                str((photos / ".darkimiya" / "Exports").resolve()))

    def test_private_people_api_indexes_locally_and_serves_only_crops(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            faces, report, reviews = GuiFaceTests().make_store(
                root, FakeFaceEngine())
            photo_store = faces.photos
            server = ReviewServer(
                ("127.0.0.1", 0), report, photo_store, reviews, faces=faces)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=3)
                connection.request(
                    "POST", "/api/people/start", body=b"{}",
                    headers={
                        "Content-Type": "application/json",
                        "X-OpenCull-CSRF": server.csrf_token,
                    })
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 200)
                deadline = time.time() + 3
                while time.time() < deadline:
                    connection.request("GET", "/api/people")
                    response = connection.getresponse()
                    state = json.loads(response.read())
                    if state["status"]["state"] == "completed":
                        break
                    time.sleep(0.02)
                else:
                    self.fail("private face indexing did not finish")
                self.assertTrue(state["local_only"])
                self.assertFalse(state["embeddings_exposed"])
                raw = json.dumps(state)
                face_id = state["people"][0]["faces"][0]["id"]
                self.assertNotIn("embedding\":", raw)
                person_id = state["people"][0]["id"]
                connection.request(
                    "GET",
                    f"/api/person-faces?id={person_id}&offset=0&limit=1")
                response = connection.getresponse()
                page = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(page["person_id"], person_id)
                self.assertEqual(len(page["faces"]), 1)
                self.assertNotIn("embedding", json.dumps(page))
                connection.request("GET", f"/api/face?id={face_id}")
                response = connection.getresponse()
                crop = response.read()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Content-Type"), "image/jpeg")
                self.assertTrue(crop.startswith(b"\xff\xd8"))
                connection.request(
                    "POST", "/api/people/rename",
                    body=json.dumps({
                        "person_id": person_id,
                        "name": "Private Name",
                        "confirmed": True,
                    }),
                    headers={
                        "Content-Type": "application/json",
                        "X-OpenCull-CSRF": server.csrf_token,
                    })
                response = connection.getresponse()
                renamed = json.loads(response.read())
                self.assertEqual(response.status, 200)
                person = next(
                    item for item in renamed["people"]
                    if item["id"] == person_id)
                self.assertEqual(person["private_name"], "Private Name")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_provider_api_never_returns_submitted_secret(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            Image.new("RGB", (80, 60), "blue").save(photos / "A.JPG")
            Image.new("RGB", (80, 60), "green").save(photos / "B.JPG")
            report_path = root / "report.json"
            report_path.write_text(json.dumps(report_data()), encoding="utf-8")
            report = load_report(report_path)
            store = PhotoStore(photos, root / "cache")
            reviews = ReviewStore(root / "review.json", report, photos)
            providers, keychain = GuiProviderTests().make_store(root / "provider")
            server = ReviewServer(
                ("127.0.0.1", 0), report, store, reviews,
                providers=providers)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            secret = "api-secret-must-not-return"
            try:
                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=3)
                connection.request(
                    "POST", "/api/providers/save",
                    body=json.dumps({
                        "revision": 0,
                        "profile": provider_data(),
                        "secret": secret,
                    }),
                    headers={
                        "Content-Type": "application/json",
                        "X-OpenCull-CSRF": server.csrf_token,
                    },
                )
                response = connection.getresponse()
                raw = response.read().decode()
                self.assertEqual(response.status, 200)
                self.assertNotIn(secret, raw)
                saved = json.loads(raw)
                profile_id = saved["profiles"][0]["id"]
                self.assertEqual(keychain.get(profile_id), secret)
                connection.request("GET", "/api/providers")
                response = connection.getresponse()
                raw = response.read().decode()
                self.assertEqual(response.status, 200)
                self.assertNotIn(secret, raw)
                self.assertTrue(json.loads(raw)["secrets_returned"] is False)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_review_stays_responsive_during_background_culling(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            Image.new("RGB", (80, 60), "blue").save(photos / "A.JPG")
            Image.new("RGB", (80, 60), "green").save(photos / "B.JPG")
            report_path = root / "report.json"
            report_path.write_text(json.dumps(report_data()), encoding="utf-8")
            report = load_report(report_path)
            store = PhotoStore(photos, root / "cache")
            reviews = ReviewStore(root / "review.json", report, photos)
            manager = JobManager(
                root / "jobs.json", root,
                command_builder=lambda job: [
                    sys.executable, "-c", "import time;time.sleep(5)"])
            server = ReviewServer(
                ("127.0.0.1", 0), report, store, reviews, jobs=manager)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=3)
                connection.request(
                    "POST", "/api/jobs/add",
                    body=json.dumps({
                        "photos": str(photos),
                        "output": str(root / "culled.json"),
                    }),
                    headers={
                        "Content-Type": "application/json",
                        "X-OpenCull-CSRF": server.csrf_token,
                    },
                )
                response = connection.getresponse()
                queued = json.loads(response.read())
                self.assertEqual(response.status, 200)
                job_id = queued["jobs"][0]["id"]
                deadline = time.time() + 3
                while time.time() < deadline:
                    connection.request("GET", "/api/jobs")
                    response = connection.getresponse()
                    jobs = json.loads(response.read())
                    if jobs["jobs"][0]["status"] == "running":
                        break
                    time.sleep(0.02)
                else:
                    self.fail("background culling did not start")
                started = time.monotonic()
                connection.request("GET", "/api/report")
                response = connection.getresponse()
                payload = json.loads(response.read())
                elapsed = time.monotonic() - started
                self.assertEqual(response.status, 200)
                self.assertEqual(payload["summary"]["photos"], 2)
                self.assertLess(elapsed, 0.5)
                connection.request(
                    "POST", "/api/jobs/action",
                    body=json.dumps({"job_id": job_id, "action": "cancel"}),
                    headers={
                        "Content-Type": "application/json",
                        "X-OpenCull-CSRF": server.csrf_token,
                    },
                )
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 200)
                self.wait_for_job_status(manager, job_id, "cancelled")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def wait_for_job_status(self, manager, job_id, status):
        deadline = time.time() + 3
        while time.time() < deadline:
            job = next(
                item for item in manager.public()["jobs"]
                if item["id"] == job_id)
            if job["status"] == status:
                return
            time.sleep(0.02)
        self.fail(f"job did not reach {status}")

    def test_serves_report_image_and_protected_review_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photos = root / "photos"
            photos.mkdir()
            Image.new("RGB", (100, 80), "blue").save(photos / "A.JPG")
            Image.new("RGB", (100, 80), "green").save(photos / "B.JPG")
            report_path = root / "report.json"
            report_path.write_text(json.dumps(report_data()), encoding="utf-8")
            report = load_report(report_path)
            store = PhotoStore(photos, root / "cache")
            reviews = ReviewStore(root / "review.json", report, photos)
            server = ReviewServer(
                ("127.0.0.1", 0), report, store, reviews)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=5)
                connection.request("GET", "/api/report")
                response = connection.getresponse()
                payload = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(payload["summary"]["photos"], 2)
                self.assertEqual(payload["review"]["revision"], 0)
                self.assertEqual(response.getheader("X-Frame-Options"), "DENY")

                connection.request("GET", "/api/image?name=A.JPG&size=thumb")
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 202)

                connection.request(
                    "GET",
                    "/api/previews/file?name=A.JPG&size=thumb&revision=not-ready",
                )
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 404)

                focus_body = json.dumps({
                    "visible": ["A.JPG"], "prefetch": ["B.JPG"],
                    "size": "thumb",
                })
                connection.request(
                    "POST",
                    "/api/previews/focus",
                    body=focus_body,
                    headers={
                        "Content-Type": "application/json",
                        "X-OpenCull-CSRF": server.csrf_token,
                    },
                )
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 200)
                deadline = time.time() + 5
                while time.time() < deadline:
                    connection.request(
                        "GET", "/api/previews/status?size=thumb&name=A.JPG")
                    response = connection.getresponse()
                    status = json.loads(response.read())
                    if status["previews"]["A.JPG"]["status"] == "ready":
                        break
                    time.sleep(0.02)
                else:
                    self.fail("HTTP preview did not become ready")

                connection.request("GET", "/api/image?name=A.JPG&size=thumb")
                response = connection.getresponse()
                image = response.read()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Content-Type"), "image/jpeg")
                self.assertTrue(image.startswith(b"\xff\xd8"))

                revision = status["previews"]["A.JPG"]["revision"]
                self.assertTrue(revision)
                connection.request(
                    "GET",
                    "/api/previews/file?name=A.JPG&size=thumb&revision=" + revision,
                )
                response = connection.getresponse()
                immutable_image = response.read()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Content-Type"), "image/jpeg")
                self.assertIn("immutable", response.getheader("Cache-Control"))
                self.assertEqual(response.getheader("ETag"), f'"{revision}"')
                self.assertTrue(immutable_image.startswith(b"\xff\xd8"))

                connection.request("POST", "/api/review", body=b"{}")
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 403)

                body = json.dumps({
                    "cluster_id": "group-0001",
                    "keepers": ["B.JPG"],
                    "note": "human preference",
                    "reviewed": True,
                    "revision": 0,
                })
                connection.request(
                    "POST",
                    "/api/review/cluster",
                    body=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-OpenCull-CSRF": server.csrf_token,
                    },
                )
                response = connection.getresponse()
                updated = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(
                    updated["clusters"]["group-0001"]["keepers"], ["B.JPG"])

                connection.request("GET", "/api/export/xmp?policy=effective")
                response = connection.getresponse()
                xmp_archive = response.read()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Content-Type"), "application/zip")
                with zipfile.ZipFile(BytesIO(xmp_archive)) as archive:
                    self.assertIn("B.xmp", archive.namelist())

                connection.request(
                    "POST",
                    "/api/review/undo",
                    body=json.dumps({"revision": updated["revision"]}),
                    headers={
                        "Content-Type": "application/json",
                        "X-OpenCull-CSRF": server.csrf_token,
                    },
                )
                response = connection.getresponse()
                undone = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertNotIn("group-0001", undone["clusters"])
                body = json.dumps({
                    "cluster_id": "group-0001",
                    "keepers": ["B.JPG"],
                    "note": "human preference",
                    "reviewed": True,
                    "revision": undone["revision"],
                })
                connection.request(
                    "POST", "/api/review/cluster", body=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-OpenCull-CSRF": server.csrf_token,
                    },
                )
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 200)

                connection.request("GET", "/api/export")
                response = connection.getresponse()
                exported = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(exported["selected_photos"], ["B.JPG"])

                preflight_body = json.dumps({
                    "destination": str(root / "export"),
                    "policy": "human_only",
                    "action": "copy",
                    "layout": "opensull",
                    "preserve_relative": False,
                    "include_companions": False,
                })
                connection.request(
                    "POST",
                    "/api/action/preflight",
                    body=preflight_body,
                    headers={
                        "Content-Type": "application/json",
                        "X-OpenCull-CSRF": server.csrf_token,
                    },
                )
                response = connection.getresponse()
                plan = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertFalse(plan["summary"]["errors"])

                execute_body = json.dumps({
                    "plan_id": plan["plan_id"],
                    "confirmation": plan["confirmation_code"],
                })
                connection.request(
                    "POST",
                    "/api/action/execute",
                    body=execute_body,
                    headers={
                        "Content-Type": "application/json",
                        "X-OpenCull-CSRF": server.csrf_token,
                    },
                )
                response = connection.getresponse()
                operation = json.loads(response.read())
                self.assertEqual(response.status, 200)
                deadline = time.time() + 5
                while time.time() < deadline:
                    connection.request(
                        "GET",
                        f"/api/action/status?id={operation['plan_id']}",
                    )
                    response = connection.getresponse()
                    operation = json.loads(response.read())
                    if operation["status"] == "completed":
                        break
                    time.sleep(0.02)
                else:
                    self.fail("HTTP copy operation did not complete")
                self.assertTrue((root / "export" / "B.JPG").is_file())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
