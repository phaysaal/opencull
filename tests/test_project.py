import json
import tempfile
import unittest
from pathlib import Path

from opencull_gui.project import (
    create_project,
    import_legacy_development_artifacts,
    legacy_migration_preview,
    load_or_create,
    load_or_create_folder_project,
    load_project,
    migrate_legacy_project,
    preferred_project_path,
    project_manifest_path,
    project_sha256,
    register_file_artifact,
    register_job_output,
    register_render,
    update_project,
)


class PortableProjectTests(unittest.TestCase):
    """The project folder is the whole truth, wherever it goes."""

    def test_a_new_project_hides_its_managed_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "photos"
            source.mkdir()
            path, _ = load_or_create_folder_project(source, "Shoot")
            self.assertEqual(path.parent.name, ".darkimiya")

    def test_a_visible_era_project_keeps_its_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "photos"
            (source / "Darkimiya").mkdir(parents=True)
            path, _ = load_or_create_folder_project(source, "Shoot")
            self.assertEqual(path.parent.name, "Darkimiya")
            self.assertFalse((source / ".darkimiya").exists())

    def test_a_moved_folder_re_anchors_every_recorded_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_home = root / "on-the-laptop"
            old_home.mkdir()
            path, _ = load_or_create_folder_project(old_home, "Tour")
            update_project(path, artifacts={"culling_report": {
                "kind": "culling_report",
                "path": str(old_home / ".darkimiya" / "Reports" / "r.json"),
            }})
            new_home = root / "on-the-archive-drive"
            old_home.rename(new_home)

            healed = load_project(
                new_home / ".darkimiya" / "project.json")

            self.assertEqual(healed["source_folder"], str(new_home))
            self.assertEqual(
                healed["artifacts"]["culling_report"]["path"],
                str(new_home / ".darkimiya" / "Reports" / "r.json"))
            # Persisted, not just returned: the next reader sees the truth.
            raw = json.loads(
                (new_home / ".darkimiya" / "project.json").read_text())
            self.assertEqual(raw["source_folder"], str(new_home))

    def test_healing_leaves_paths_outside_the_folder_alone(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_home = root / "before"
            old_home.mkdir()
            path, _ = load_or_create_folder_project(old_home, "Tour")
            elsewhere = "/somewhere/global/style-profile.json"
            update_project(path, artifacts={"style_profile": {
                "kind": "style_profile", "path": elsewhere}})
            old_home.rename(root / "after")

            healed = load_project(root / "after" / ".darkimiya" / "project.json")

            self.assertEqual(
                healed["artifacts"]["style_profile"]["path"], elsewhere)

    def test_a_sibling_prefix_folder_is_not_rewritten(self):
        """/home/a must never capture /home/a-archive's paths."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_home = root / "tour"
            old_home.mkdir()
            path, _ = load_or_create_folder_project(old_home, "Tour")
            sibling = str(root / "tour-archive" / "x.json")
            update_project(path, artifacts={"calibration": {
                "kind": "calibration", "path": sibling}})
            old_home.rename(root / "moved")

            healed = load_project(root / "moved" / ".darkimiya" / "project.json")

            self.assertEqual(
                healed["artifacts"]["calibration"]["path"], sibling)

    def test_a_moved_folder_re_links_in_the_catalog_by_identity(self):
        from opencull_gui.project_catalog import ProjectCatalog

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_home = root / "photos"
            old_home.mkdir()
            catalog = ProjectCatalog(root / "support" / "projects.json")
            catalog.add(str(old_home))
            first = catalog.public()["projects"][0]
            new_home = root / "elsewhere"
            old_home.rename(new_home)

            state = catalog.add(str(new_home))

            self.assertEqual(len(state["projects"]), 1)
            listed = state["projects"][0]
            self.assertEqual(listed["id"], first["id"])
            self.assertEqual(listed["photos"], str(new_home))


class ProjectTests(unittest.TestCase):
    def test_project_is_versioned_and_updateable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "photos"
            source.mkdir()
            manifest = root / "project.json"
            value = create_project(manifest, "Trip", source)
            self.assertEqual(value["format"], "darkimiya-project-v1")
            self.assertEqual(value["product"], "Darkimiya")
            self.assertEqual(value["culling_engine"], "OpenCull")
            self.assertEqual(value["rendering"]["engine"], "darktable")
            updated = update_project(manifest, stage="style",
                                     active_style_profile="style-v2",
                                     rendering={"engine": "default",
                                                "demosaic": "markesteijn-1-pass"})
            self.assertEqual(updated["stage"], "style")
            self.assertEqual(updated["rendering"]["engine"], "default")
            self.assertEqual(load_project(manifest)["active_style_profile"], "style-v2")
            self.assertTrue(project_sha256(manifest))
            self.assertEqual(load_or_create(manifest, "ignored", source)["id"], value["id"])
            with self.assertRaisesRegex(ValueError, "rendering engine"):
                update_project(manifest, rendering={
                    "engine": "remote", "demosaic": "markesteijn-1-pass"})

    def test_folder_project_creates_inspectable_managed_layout(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "photos"
            source.mkdir()
            path, value = load_or_create_folder_project(source, "Family")
            self.assertEqual(path, project_manifest_path(source))
            self.assertEqual(
                path, source.resolve() / ".darkimiya" / "project.json")
            self.assertEqual(value["source_folder"], str(source.resolve()))
            for name in ("Reports", "Reviews", "Developments", "Exports",
                         "RAW Reserve", "Rejected"):
                self.assertTrue((source / ".darkimiya" / name).is_dir())

    def test_existing_legacy_manifest_is_opened_without_silent_migration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "photos"; source.mkdir()
            legacy = root / "trip.opencull-project.json"
            legacy.write_text(
                '{"format":"opencull-project-v1","artifacts":{},"id":"old"}\n')
            path, value = load_or_create_folder_project(source, "Trip", legacy)
            self.assertEqual(path, legacy.resolve())
            self.assertEqual(value["id"], "old")
            self.assertFalse(project_manifest_path(source).exists())

    def test_explicit_legacy_migration_preserves_manifest_and_unknown_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "photos"; source.mkdir()
            evidence = root / "report.json"; evidence.write_text('{"report":true}\n')
            legacy = root / "trip.opencull-project.json"
            legacy.write_text(json.dumps({
                "format": "opencull-project-v1", "id": "legacy-id",
                "name": "Legacy Trip", "stage": "shortlist",
                "source_folder": str(source.resolve()),
                "active_style_profile": "/profiles/mine.json",
                "artifacts": {
                    "culling_report": {"path": str(evidence)},
                    "future_evidence": [{"path": str(root / "missing.json"),
                                         "meaning": "preserve me"}],
                },
                "history": [{"updated_at": "before", "changes": ["legacy"]}],
            }, indent=2) + "\n")
            original = legacy.read_bytes()

            preview = legacy_migration_preview(legacy, source)
            self.assertTrue(preview["available"])
            self.assertEqual(preview["artifact_categories"], 2)
            self.assertEqual(preview["artifact_paths"], 2)
            self.assertEqual(len(preview["missing_artifact_paths"]), 1)
            self.assertFalse(project_manifest_path(source).exists())
            with self.assertRaisesRegex(ValueError, "confirmation code"):
                migrate_legacy_project(legacy, source, "99999")
            self.assertEqual(legacy.read_bytes(), original)
            self.assertFalse(project_manifest_path(source).exists())

            result = migrate_legacy_project(
                legacy, source, preview["confirmation_code"])
            migrated = load_project(project_manifest_path(source))
            self.assertEqual(migrated["format"], "darkimiya-project-v1")
            self.assertEqual(migrated["id"], "legacy-id")
            self.assertEqual(migrated["stage"], "shortlist")
            self.assertEqual(
                migrated["artifacts"]["future_evidence"][0]["meaning"],
                "preserve me")
            self.assertEqual(legacy.read_bytes(), original)
            self.assertEqual(result["journal"]["status"], "completed")
            self.assertTrue(Path(result["journal"]["journal_path"]).is_file())
            self.assertEqual(
                preferred_project_path(source, legacy),
                project_manifest_path(source))
            repeated = legacy_migration_preview(legacy, source)
            self.assertFalse(repeated["available"])
            self.assertIn("already exists", repeated["reason"])

    def test_render_registration_appends_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root / "photos"; source.mkdir()
            manifest = root / "project.json"; create_project(manifest, "Trip", source)
            output = root / "render.jpg"; output.write_bytes(b"render")
            value = register_render(manifest, {"created_at": "now",
                "recipe": {"style": "personal", "revision": 2},
                "output": {"path": str(output), "sha256": "hash"},
                "calibration": {"method": "jpeg"}})
            self.assertEqual(value["artifacts"]["renders"][0]["variant"], "personal")
            self.assertEqual(value["artifacts"]["renders"][0]["recipe_revision"], 2)

    def test_intact_legacy_renders_are_linked_without_moving_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root / "photos"; source.mkdir()
            current = root / "current.json"
            legacy = root / "legacy.json"
            create_project(current, "Trip", source)
            create_project(legacy, "Trip", source)
            image = root / "personal.jpg"; image.write_bytes(b"render")
            old = load_project(legacy)
            old["format"] = "opencull-project-v1"
            old["artifacts"]["renders"] = [{
                "variant": "personal", "source_photo": "A.JPG",
                "path": str(image), "sha256": "render-sha",
            }]
            legacy.write_text(json.dumps(old), encoding="utf-8")

            imported = import_legacy_development_artifacts(current, legacy)
            repeated = import_legacy_development_artifacts(current, legacy)

            self.assertEqual(len(imported["artifacts"]["renders"]), 1)
            self.assertEqual(len(repeated["artifacts"]["renders"]), 1)
            self.assertTrue(image.is_file())
            event = imported["history"][-1]
            self.assertEqual(event["kind"], "legacy-development-artifact-import")
            self.assertFalse(event["files_moved"])

    def test_supervised_outputs_are_registered_by_stage_without_duplicates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root / "photos"; source.mkdir()
            manifest = root / "project.json"; create_project(manifest, "Trip", source)
            report = root / "report.json"; report.write_text('{"ok":true}\n')
            first = register_job_output(manifest, {
                "id": "job-1", "kind": "culling", "output": str(report),
            })
            second = register_job_output(manifest, {
                "id": "job-1", "kind": "culling", "output": str(report),
            })
            self.assertEqual(
                Path(first["artifacts"]["culling_report"]["path"]),
                report.resolve())
            self.assertEqual(first["artifacts"]["culling_report"]["sha256"],
                             second["artifacts"]["culling_report"]["sha256"])
            review = root / "review.json"; review.write_text('{"revision":1}\n')
            value = register_file_artifact(
                manifest, "culling_review", review, stage="cull")
            value = register_file_artifact(
                manifest, "culling_review", review, stage="cull")
            self.assertEqual(len(value["artifacts"]["culling_review"]), 1)


if __name__ == "__main__":
    unittest.main()
