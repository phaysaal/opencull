import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from PIL import Image

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


class GuiHttpTests(unittest.TestCase):
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
