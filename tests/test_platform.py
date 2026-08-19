"""Cross-platform behaviour: application paths, choosers, and trash."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from opencull_gui import appdirs, dialogs  # noqa: E402
from opencull_gui.actions import _trash_root_for  # noqa: E402


class AppDirTests(unittest.TestCase):
    def test_macos_keeps_the_locations_that_shipped(self):
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(
                appdirs.support_dir(),
                Path.home() / "Library" / "Application Support" / "Darkimiya")
            self.assertEqual(
                appdirs.cache_dir(),
                Path.home() / "Library" / "Caches" / "Darkimiya")
            self.assertEqual(
                appdirs.logs_dir(),
                Path.home() / "Library" / "Logs" / "Darkimiya")

    def test_linux_follows_xdg_defaults(self):
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                appdirs.support_dir(),
                Path.home() / ".local" / "share" / "Darkimiya")
            self.assertEqual(
                appdirs.cache_dir(), Path.home() / ".cache" / "Darkimiya")
            self.assertEqual(
                appdirs.logs_dir(),
                Path.home() / ".local" / "state" / "Darkimiya")

    def test_linux_honours_absolute_xdg_overrides(self):
        env = {"XDG_DATA_HOME": "/custom/data", "XDG_CACHE_HOME": "/custom/cache"}
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                appdirs.support_dir(), Path("/custom/data/Darkimiya"))
            self.assertEqual(
                appdirs.cache_dir(), Path("/custom/cache/Darkimiya"))

    def test_a_relative_xdg_value_is_ignored_as_the_specification_requires(self):
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.dict(os.environ, {"XDG_DATA_HOME": "relative"}, clear=True):
            self.assertEqual(
                appdirs.support_dir(),
                Path.home() / ".local" / "share" / "Darkimiya")

    def test_windows_uses_local_app_data(self):
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.dict(
                    os.environ, {"LOCALAPPDATA": r"C:\Users\p\AppData\Local"},
                    clear=True):
            self.assertEqual(
                appdirs.support_dir().as_posix(),
                "C:\\Users\\p\\AppData\\Local/Darkimiya")

    def test_every_platform_lands_under_a_distinct_named_directory(self):
        for platform in ("darwin", "linux", "win32"):
            with mock.patch.object(sys, "platform", platform), \
                    mock.patch.dict(
                        os.environ, {"LOCALAPPDATA": "/tmp/lad"}, clear=True):
                self.assertEqual(appdirs.support_dir().name, "Darkimiya")


class DialogBackendTests(unittest.TestCase):
    def test_macos_always_reports_osascript(self):
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(dialogs.backend(), "osascript")

    def test_linux_prefers_zenity_then_kdialog(self):
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.dict(os.environ, {"DISPLAY": ":0"}, clear=True):
            with mock.patch.object(
                dialogs.shutil, "which",
                side_effect=lambda name: "/usr/bin/zenity" if name == "zenity" else None,
            ):
                self.assertEqual(dialogs.backend(), "zenity")
            with mock.patch.object(
                dialogs.shutil, "which",
                side_effect=lambda name: "/usr/bin/kdialog" if name == "kdialog" else None,
            ):
                self.assertEqual(dialogs.backend(), "kdialog")

    def test_a_headless_linux_session_reports_no_chooser(self):
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(dialogs.backend(), "none")
            state = dialogs.status()
            self.assertFalse(state["available"])
            self.assertIn("display", str(state["detail"]))

    def test_an_unavailable_chooser_explains_what_to_install(self):
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.dict(os.environ, {"DISPLAY": ":0"}, clear=True), \
                mock.patch.object(dialogs.shutil, "which", return_value=None), \
                mock.patch.object(dialogs, "_tk_available", return_value=False):
            with self.assertRaises(dialogs.DialogError) as caught:
                dialogs.choose_folder("Pick something")
            self.assertIn("zenity", str(caught.exception))

    def test_zenity_cancellation_is_reported_as_cancellation(self):
        completed = mock.Mock(returncode=1, stdout="", stderr="")
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.dict(os.environ, {"DISPLAY": ":0"}, clear=True), \
                mock.patch.object(
                    dialogs.shutil, "which",
                    side_effect=lambda n: "/usr/bin/zenity" if n == "zenity" else None), \
                mock.patch.object(dialogs.subprocess, "run", return_value=completed):
            with self.assertRaises(dialogs.DialogCancelled):
                dialogs.choose_folder("Choose export folder")

    def test_zenity_returns_the_chosen_folder(self):
        completed = mock.Mock(returncode=0, stdout="/home/p/Pictures/\n", stderr="")
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.dict(os.environ, {"DISPLAY": ":0"}, clear=True), \
                mock.patch.object(
                    dialogs.shutil, "which",
                    side_effect=lambda n: "/usr/bin/zenity" if n == "zenity" else None), \
                mock.patch.object(dialogs.subprocess, "run", return_value=completed):
            self.assertEqual(
                dialogs.choose_folder("Choose"), "/home/p/Pictures")

    def test_multiple_selection_splits_on_lines(self):
        completed = mock.Mock(
            returncode=0, stdout="/a/one.jpg\n/a/two.jpg\n", stderr="")
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.dict(os.environ, {"DISPLAY": ":0"}, clear=True), \
                mock.patch.object(
                    dialogs.shutil, "which",
                    side_effect=lambda n: "/usr/bin/zenity" if n == "zenity" else None), \
                mock.patch.object(dialogs.subprocess, "run", return_value=completed):
            self.assertEqual(
                dialogs.choose_files("Add", image_only=True, multiple=True),
                ["/a/one.jpg", "/a/two.jpg"])

    def test_a_cancelled_macos_chooser_is_still_reported_as_cancellation(self):
        completed = mock.Mock(returncode=1, stdout="", stderr="User cancelled. (-128)")
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(dialogs.subprocess, "run", return_value=completed):
            with self.assertRaises(dialogs.DialogCancelled):
                dialogs.choose_folder("Choose export folder")


class RevealTests(unittest.TestCase):
    def test_macos_reveals_with_open_dash_r(self):
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(dialogs.subprocess, "Popen") as popen:
            dialogs.reveal(Path("/a/b.jpg"))
        self.assertEqual(popen.call_args[0][0][:2], ["open", "-R"])

    def test_windows_selects_the_item_in_explorer(self):
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(dialogs.subprocess, "Popen") as popen:
            dialogs.reveal(Path("/a/b.jpg"))
        self.assertEqual(popen.call_args[0][0][0], "explorer")
        self.assertIn("/select,", popen.call_args[0][0][1])

    def test_linux_prefers_the_file_manager_interface_that_selects_the_item(self):
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.object(
                    dialogs.shutil, "which",
                    side_effect=lambda n: "/usr/bin/dbus-send" if n == "dbus-send" else None), \
                mock.patch.object(dialogs.subprocess, "Popen") as popen:
            dialogs.reveal(Path("/a/b.jpg"))
        command = popen.call_args[0][0]
        self.assertEqual(command[0], "dbus-send")
        self.assertIn("array:string:file:///a/b.jpg", command)

    def test_linux_falls_back_to_opening_the_containing_folder(self):
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.object(
                    dialogs.shutil, "which",
                    side_effect=lambda n: "/usr/bin/xdg-open" if n == "xdg-open" else None), \
                mock.patch.object(dialogs.subprocess, "Popen") as popen:
            dialogs.reveal(Path("/a/b.jpg"))
        self.assertEqual(popen.call_args[0][0], ["xdg-open", "/a"])

    def test_no_file_manager_is_an_explained_error(self):
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.object(dialogs.shutil, "which", return_value=None):
            with self.assertRaises(dialogs.DialogError):
                dialogs.reveal(Path("/a/b.jpg"))


class DarktableDiscoveryTests(unittest.TestCase):
    def test_path_is_preferred_over_the_platform_defaults(self):
        import darktable_engine

        with mock.patch.object(
            darktable_engine.shutil, "which", return_value="/usr/bin/darktable-cli"
        ), mock.patch.object(Path, "is_file", return_value=True), \
                mock.patch.object(darktable_engine.os, "access", return_value=True):
            self.assertEqual(
                darktable_engine.find_darktable_cli(),
                Path("/usr/bin/darktable-cli"))

    def test_defaults_cover_linux_and_windows_not_only_macos(self):
        import darktable_engine

        defaults = {str(path) for path in darktable_engine.DARKTABLE_FALLBACK_CLI}
        self.assertTrue(any("Applications" in item for item in defaults))
        self.assertTrue(any(item.startswith("/usr/") for item in defaults))
        self.assertTrue(any("Program Files" in item for item in defaults))

    def test_a_missing_darktable_is_an_explained_error(self):
        import darktable_engine

        with mock.patch.object(darktable_engine.shutil, "which", return_value=None), \
                mock.patch.object(Path, "is_file", return_value=False):
            with self.assertRaises(darktable_engine.DarktableError):
                darktable_engine.find_darktable_cli()


class TrashLocationTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name).resolve()

    def tearDown(self):
        self._temporary.cleanup()

    def test_macos_home_volume_uses_the_user_trash(self):
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(
                _trash_root_for(self.root / "A.JPG", "batch"),
                Path.home() / ".Trash" / "batch")

    def test_linux_home_volume_uses_the_home_trash(self):
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch("opencull_gui.actions._mount_point_for",
                           return_value=Path.home()):
            result = _trash_root_for(self.root / "A.JPG", "batch")
        self.assertEqual(
            result, Path.home() / ".local" / "share" / "Trash" / "files" / "batch")

    def test_linux_separate_volume_trashes_on_that_volume(self):
        # A move must stay on one device, so a photograph on an external drive
        # belongs in that drive's .Trash-<uid>, not the home trash.
        def mount_for(path):
            return Path("/") if path == Path.home() else Path("/media/photos")

        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch("opencull_gui.actions._mount_point_for",
                           side_effect=mount_for):
            result = _trash_root_for(self.root / "A.JPG", "batch")
        self.assertEqual(
            result,
            Path("/media/photos") / f".Trash-{os.getuid()}" / "files" / "batch")

    def test_linux_honours_an_absolute_xdg_data_home(self):
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.dict(
                    os.environ, {"XDG_DATA_HOME": "/custom/data"}, clear=True), \
                mock.patch("opencull_gui.actions._mount_point_for",
                           return_value=Path.home()):
            self.assertEqual(
                _trash_root_for(self.root / "A.JPG", "batch"),
                Path("/custom/data/Trash/files/batch"))

    def test_windows_uses_a_named_recoverable_folder(self):
        with mock.patch.object(sys, "platform", "win32"):
            result = _trash_root_for(self.root / "A.JPG", "batch")
        self.assertEqual(result, Path.home() / "Darkimiya Trash" / "batch")

    def test_no_platform_ever_returns_a_path_inside_the_source_folder(self):
        for platform in ("darwin", "linux", "win32"):
            with mock.patch.object(sys, "platform", platform), \
                    mock.patch.dict(os.environ, {}, clear=True):
                result = _trash_root_for(self.root / "A.JPG", "batch")
            self.assertNotIn(
                self.root.resolve(), [result, *result.parents],
                f"{platform} would trash into the source folder")


if __name__ == "__main__":
    unittest.main()
