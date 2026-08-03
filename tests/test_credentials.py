"""Credential storage across platforms.

Saving a provider from the interface used to raise FileNotFoundError off
macOS, so these cover each backend's behaviour and, more importantly, that a
secret never reaches a process argument list or a public payload.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from opencull_gui import credentials  # noqa: E402
from opencull_gui.providers import AGENTS, ProviderStore  # noqa: E402

SECRET = "sk-or-v1-secret-value"


class BackendSelectionTests(unittest.TestCase):
    def test_macos_selects_the_keychain(self):
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(
                    credentials.shutil, "which", return_value="/usr/bin/security"):
            self.assertIsInstance(
                credentials.default_store(Path("/tmp/x.json")),
                credentials.MacOSKeychain)

    def test_linux_selects_the_keyring_when_present(self):
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.object(
                    credentials.shutil, "which", return_value="/usr/bin/secret-tool"), \
                mock.patch.dict(
                    os.environ, {"DBUS_SESSION_BUS_ADDRESS": "unix:x"}, clear=True):
            self.assertIsInstance(
                credentials.default_store(Path("/tmp/x.json")),
                credentials.SecretToolKeyring)

    def test_a_keyring_command_without_a_session_bus_is_not_used(self):
        # secret-tool present but no Secret Service would fail at first save.
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.object(
                    credentials.shutil, "which", return_value="/usr/bin/secret-tool"), \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(Path, "exists", return_value=False):
            self.assertIsInstance(
                credentials.default_store(Path("/tmp/x.json")),
                credentials.FileCredentialStore)

    def test_a_machine_without_a_keyring_still_has_a_store(self):
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.object(credentials.shutil, "which", return_value=None):
            store = credentials.default_store(Path("/tmp/x.json"))
        self.assertIsInstance(store, credentials.FileCredentialStore)

    def test_an_unprotected_store_says_so_and_names_the_remedy(self):
        described = credentials.describe(
            credentials.FileCredentialStore(Path("/tmp/x.json")))
        self.assertFalse(described["protected"])
        self.assertIn("secret-tool", str(described["detail"]))

    def test_a_protected_store_does_not_warn(self):
        described = credentials.describe(credentials.MacOSKeychain())
        self.assertTrue(described["protected"])
        self.assertNotIn("readable by anything", str(described["detail"]))


class FileStoreTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.store = credentials.FileCredentialStore(
            Path(self._temporary.name) / "secrets" / "creds.json")

    def tearDown(self):
        self._temporary.cleanup()

    def test_round_trip(self):
        self.assertFalse(self.store.has("p1"))
        self.store.set("p1", SECRET)
        self.assertTrue(self.store.has("p1"))
        self.assertEqual(self.store.get("p1"), SECRET)

    def test_the_file_is_readable_only_by_its_owner(self):
        self.store.set("p1", SECRET)
        mode = stat.S_IMODE(os.stat(self.store.path).st_mode)
        self.assertEqual(mode & 0o077, 0, f"credential file is {oct(mode)}")
        directory = stat.S_IMODE(os.stat(self.store.path.parent).st_mode)
        self.assertEqual(directory & 0o077, 0)

    def test_delete_removes_only_the_named_profile(self):
        self.store.set("p1", SECRET)
        self.store.set("p2", "other")
        self.store.delete("p1")
        self.assertFalse(self.store.has("p1"))
        self.assertEqual(self.store.get("p2"), "other")

    def test_deleting_an_absent_profile_is_not_an_error(self):
        self.store.delete("absent")

    def test_a_missing_credential_raises_rather_than_returning_empty(self):
        with self.assertRaises(credentials.CredentialError):
            self.store.get("absent")

    def test_an_empty_or_oversized_secret_is_refused(self):
        with self.assertRaises(credentials.CredentialError):
            self.store.set("p1", "")
        with self.assertRaises(credentials.CredentialError):
            self.store.set("p1", "x" * (credentials.MAX_SECRET_LENGTH + 1))

    def test_unreadable_stored_state_is_an_explained_error(self):
        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        self.store.path.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(credentials.CredentialError):
            self.store.has("p1")


class SecretNeverReachesArgumentListTests(unittest.TestCase):
    """A secret in argv is visible to every process on the machine."""

    def test_keyring_passes_the_secret_on_stdin(self):
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(
            credentials.subprocess, "run", return_value=completed
        ) as run:
            credentials.SecretToolKeyring().set("p1", SECRET)
        command, kwargs = run.call_args[0][0], run.call_args[1]
        self.assertNotIn(SECRET, " ".join(command))
        self.assertEqual(kwargs["input"], SECRET)

    def test_keychain_passes_the_secret_on_stdin(self):
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(
            credentials.subprocess, "run", return_value=completed
        ) as run:
            credentials.MacOSKeychain().set("p1", SECRET)
        command, kwargs = run.call_args[0][0], run.call_args[1]
        self.assertNotIn(SECRET, " ".join(command))
        self.assertIn(SECRET.encode("utf-8").hex(), kwargs["input"])


class ProviderStoreIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.store = ProviderStore(
            self.root / "providers.json", self.root,
            keychain=credentials.FileCredentialStore(self.root / "creds.json"))

    def tearDown(self):
        self._temporary.cleanup()

    def profile(self):
        return {
            "name": "Test", "kind": "openrouter",
            "models": dict.fromkeys(AGENTS, "openai/gpt-4.1-mini"),
        }

    def test_saving_a_provider_with_a_credential_succeeds_on_any_platform(self):
        result = self.store.save(
            self.profile(), self.store.public()["revision"], SECRET)
        saved = result["profiles"][-1]
        self.assertEqual(saved["credential"], "stored")
        self.assertEqual(self.store.credential(saved["id"]), SECRET)

    def test_the_secret_never_appears_in_the_public_payload(self):
        self.store.save(self.profile(), self.store.public()["revision"], SECRET)
        self.assertNotIn(SECRET, str(self.store.public()))
        self.assertFalse(self.store.public()["secrets_returned"])

    def test_the_payload_reports_where_credentials_are_stored(self):
        storage = self.store.public()["credential_storage"]
        self.assertIn("backend", storage)
        self.assertIn("protected", storage)


if __name__ == "__main__":
    unittest.main()
