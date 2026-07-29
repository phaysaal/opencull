import http.client
import json
import tempfile
import threading
import time
import unittest
import zipfile
from io import BytesIO
import sys
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image
import numpy as np

from opencull_gui.actions import (
    ActionError,
    Operation,
    build_plan,
    create_contact_sheets,
    export_bytes,
    policy_clusters,
)
from opencull_gui.measurements import ManifestError, load_measurements
from opencull_gui.photos import PhotoError, PhotoStore, PreviewManager
from opencull_gui.report import ReportError, load_report
from opencull_gui.reviews import ReviewError, ReviewStore
from opencull_gui.server import ReviewServer
from opencull_gui.xmp import xmp_zip
from opencull_gui.jobs import JobError, JobManager
from opencull_gui.macos import InstanceLock, MacOSPaths
from opencull_gui.providers import ProviderError, ProviderStore
from opencull_gui.faces import FaceError, FaceStore
from opencull_desktop import run_release_smoke_test


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

    def wait_faces(self, store):
        deadline = time.time() + 3
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
                state = self.wait_faces(store)
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
        for name in ("opencull.kim", "scan.py", "opencull_kernel.py"):
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


class GuiDesignFoundationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        static = Path(__file__).parents[1] / "opencull_gui" / "static"
        cls.html = (static / "index.html").read_text(encoding="utf-8")
        cls.css = (static / "styles.css").read_text(encoding="utf-8")
        cls.javascript = (static / "app.js").read_text(encoding="utf-8")

    def test_application_shell_has_stable_navigation_and_landmarks(self):
        self.assertIn('class="skip-link"', self.html)
        self.assertIn('class="app-navigation"', self.html)
        self.assertIn('aria-current="page"', self.html)
        self.assertIn('id="review-workspace"', self.html)
        self.assertIn('aria-label="Review status and actions"', self.html)

    def test_dom_ids_are_unique_and_javascript_contract_is_present(self):
        html_ids = re.findall(r'\bid="([^"]+)"', self.html)
        self.assertEqual(len(html_ids), len(set(html_ids)))
        referenced = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', self.javascript))
        self.assertFalse(referenced.difference(html_ids))

    def test_design_tokens_focus_and_reduced_motion_are_defined(self):
        for token in (
            "--bg:", "--panel:", "--text:", "--muted:", "--accent:",
            "--blue:", "--warning:", "--danger:", "--space-4:",
        ):
            self.assertIn(token, self.css)
        self.assertIn(":focus-visible", self.css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", self.css)
        self.assertIn("dialog::backdrop", self.css)

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
        self.assertIn("void acceptAIAndNext();", self.javascript)
        self.assertIn(
            "cluster.photos[number - 1] && !event.repeat", self.javascript)
        self.assertIn(
            "A accepts the AI recommendation and advances", self.html)

    def test_active_group_stays_visible_during_keyboard_navigation(self):
        self.assertIn(
            'target.querySelector(".cluster-item.active")', self.javascript)
        self.assertIn("activeItem.scrollIntoView({", self.javascript)
        self.assertIn('block: "nearest"', self.javascript)

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
        self.assertIn("Accept and Next", self.html)
        self.assertIn("async function acceptAIAndNext()", self.javascript)
        self.assertIn(
            "if (saved && state.activeClusterId === clusterId) navigate(1);",
            self.javascript)
        self.assertIn(
            '$("#accept-ai-next").addEventListener("click", acceptAIAndNext);',
            self.javascript)
        self.assertIn('event.key.toLowerCase() === "a" && !event.repeat',
                      self.javascript)

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
    def wait_for(self, manager, predicate, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = manager.public()
            if predicate(state):
                return state
            time.sleep(0.02)
        self.fail("job manager condition timed out")

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
                with self.assertRaisesRegex(JobError, "already queued"):
                    manager.add(str(photos), str(root / "other.json"))
                existing = root / "existing.json"
                existing.write_text("{}")
                another = root / "another"
                another.mkdir()
                with self.assertRaisesRegex(JobError, "already exists"):
                    manager.add(str(another), str(existing))
            finally:
                manager.shutdown()


class GuiHttpTests(unittest.TestCase):
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
                    "confirmation": f"COPY {plan['plan_id']}",
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
