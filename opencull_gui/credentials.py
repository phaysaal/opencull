"""Per-platform storage for provider API credentials.

Credentials were stored only in the macOS Keychain through the `security`
command, so saving a provider from the interface raised FileNotFoundError on
Linux and Windows -- and not a ProviderError, so it escaped the server's
error handling as a traceback rather than a message.

Backends are probed in order of protection and the chosen one is reported,
because where a key is stored is something the person is entitled to know.
The file backend is last and deliberately explicit: many Linux installations
have no keyring command available, and refusing to store anything would make
the application unusable, but storing a key at rest without saying so would
be worse.

No backend passes a secret as a command-line argument, since that would
expose it in the process list to every other user on the machine.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ACCOUNT = "opencull"
SERVICE_PREFIX = "org.opencull.provider."
MAX_SECRET_LENGTH = 8192


class CredentialError(ValueError):
    """A credential could not be stored or retrieved."""


def _service(profile_id: str) -> str:
    return SERVICE_PREFIX + profile_id


def _check_secret(secret: str) -> None:
    if not secret or len(secret) > MAX_SECRET_LENGTH:
        raise CredentialError(
            f"credential must contain between 1 and {MAX_SECRET_LENGTH} characters")


class MacOSKeychain:
    """Generic-password adapter; secret values are never returned publicly."""

    name = "macos-keychain"
    label = "macOS Keychain"
    protected = True

    account = ACCOUNT
    prefix = SERVICE_PREFIX

    @staticmethod
    def available() -> bool:
        return sys.platform == "darwin" and bool(shutil.which("security"))

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
            raise CredentialError("provider credential is missing from macOS Keychain")
        secret = result.stdout.strip()
        if not secret:
            raise CredentialError("provider credential in macOS Keychain is empty")
        return secret

    def set(self, profile_id: str, secret: str) -> None:
        _check_secret(secret)
        # `security -w value` exposes value in the process argument list.
        # Interactive mode accepts the command on stdin; -X avoids shell-like
        # quoting entirely while storing the exact UTF-8 password bytes.
        command = (
            "add-generic-password -U "
            f"-a {self.account} -s {self._service(profile_id)} "
            f"-X {secret.encode('utf-8').hex()}\n"
        )
        result = subprocess.run(
            ["security", "-i"], input=command,
            capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise CredentialError(
                f"could not save credential in macOS Keychain: "
                f"{result.stderr.strip()}")

    def delete(self, profile_id: str) -> None:
        result = subprocess.run(
            [
                "security", "delete-generic-password",
                "-a", self.account, "-s", self._service(profile_id),
            ],
            capture_output=True, text=True, check=False)
        # 44 is "item not found", which is the state the caller wanted.
        if result.returncode not in {0, 44}:
            raise CredentialError(
                f"could not remove Keychain credential: {result.stderr.strip()}")


class SecretToolKeyring:
    """libsecret adapter, covering GNOME Keyring and KWallet's Secret Service."""

    name = "secret-service"
    label = "system keyring"
    protected = True

    @staticmethod
    def available() -> bool:
        if sys.platform == "darwin" or sys.platform == "win32":
            return False
        if not shutil.which("secret-tool"):
            return False
        # A keyring command with no running Secret Service would fail at the
        # first save rather than here, so require the session bus too.
        return bool(
            os.environ.get("DBUS_SESSION_BUS_ADDRESS")
            or Path(f"/run/user/{os.getuid()}/bus").exists()
        )

    def _lookup(self, profile_id: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "secret-tool", "lookup",
                "account", ACCOUNT, "service", _service(profile_id),
            ],
            capture_output=True, text=True, check=False)

    def has(self, profile_id: str) -> bool:
        result = self._lookup(profile_id)
        return result.returncode == 0 and bool(result.stdout.strip())

    def get(self, profile_id: str) -> str:
        result = self._lookup(profile_id)
        if result.returncode != 0:
            raise CredentialError(
                "provider credential is missing from the system keyring")
        secret = result.stdout.strip()
        if not secret:
            raise CredentialError(
                "provider credential in the system keyring is empty")
        return secret

    def set(self, profile_id: str, secret: str) -> None:
        _check_secret(secret)
        # `secret-tool store` reads the secret from stdin, so it never reaches
        # the process argument list.
        result = subprocess.run(
            [
                "secret-tool", "store", "--label",
                f"Darkimiya provider {profile_id}",
                "account", ACCOUNT, "service", _service(profile_id),
            ],
            input=secret, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise CredentialError(
                f"could not save credential in the system keyring: "
                f"{result.stderr.strip()}")

    def delete(self, profile_id: str) -> None:
        result = subprocess.run(
            [
                "secret-tool", "clear",
                "account", ACCOUNT, "service", _service(profile_id),
            ],
            capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise CredentialError(
                f"could not remove the keyring credential: {result.stderr.strip()}")


class FileCredentialStore:
    """Owner-only file storage, used when no keyring is available.

    This is the honest fallback rather than the preferred one: the secret is
    readable by anything running as this user. It is reported as such so the
    person can decide whether to install a keyring instead.
    """

    name = "file"
    label = "an owner-only file"
    protected = False

    def __init__(self, path: Path):
        self.path = Path(path)

    @staticmethod
    def available() -> bool:
        return True

    def _load(self) -> dict[str, str]:
        if not self.path.is_file():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CredentialError(
                f"stored credentials could not be read: {exc}") from exc
        return value if isinstance(value, dict) else {}

    def _save(self, value: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        temporary = self.path.with_suffix(".tmp")
        # Create with owner-only permissions from the start; writing first and
        # tightening afterwards would leave a readable window.
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def has(self, profile_id: str) -> bool:
        return bool(self._load().get(profile_id))

    def get(self, profile_id: str) -> str:
        secret = self._load().get(profile_id, "")
        if not secret:
            raise CredentialError("provider credential is missing")
        return secret

    def set(self, profile_id: str, secret: str) -> None:
        _check_secret(secret)
        value = self._load()
        value[profile_id] = secret
        self._save(value)

    def delete(self, profile_id: str) -> None:
        value = self._load()
        if value.pop(profile_id, None) is not None:
            self._save(value)


def default_store(fallback_path: Path | None = None):
    """Return the most protected credential store this machine supports."""
    for backend in (MacOSKeychain, SecretToolKeyring):
        if backend.available():
            return backend()
    if fallback_path is None:
        from .appdirs import support_dir

        fallback_path = support_dir() / "provider-credentials.json"
    return FileCredentialStore(fallback_path)


def describe(store: object) -> dict[str, object]:
    """Describe where credentials are kept, for the interface to report."""
    name = getattr(store, "name", "unknown")
    label = getattr(store, "label", "an unknown store")
    protected = bool(getattr(store, "protected", False))
    detail = f"Provider credentials are stored in {label}."
    if not protected:
        detail += (
            " No system keyring was found, so the file is readable by anything"
            " running as you. Install libsecret (secret-tool) for keyring"
            " storage."
        )
    return {"backend": name, "label": label, "protected": protected,
            "detail": detail}
