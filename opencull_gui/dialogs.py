"""Native file and folder choosers, and file-manager reveal, per platform.

Every chooser in the review server was an inline `osascript` invocation
guarded by ``sys.platform != "darwin"``, so on Linux and Windows the folder
and file pickers simply refused. This module keeps macOS on `osascript` --
the behaviour that shipped -- and adds equivalents elsewhere, choosing the
first backend actually present on the machine rather than assuming one.

Backends are probed, never assumed: a Linux desktop may have zenity, kdialog,
both, or neither, and a headless session has no chooser at all. When nothing
is available the caller receives an explanation naming what to install, which
is more useful than a platform refusal.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

TIMEOUT = 300
IMAGE_PATTERNS = ("*.jpg", "*.jpeg", "*.png", "*.tif", "*.tiff", "*.webp")


class DialogError(ValueError):
    """A chooser is unavailable, failed, or the person cancelled it."""


class DialogCancelled(DialogError):
    """The person dismissed the chooser without choosing."""


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command, capture_output=True, text=True,
            timeout=TIMEOUT, check=False)
    except FileNotFoundError as exc:
        raise DialogError(f"chooser is unavailable: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DialogError("the chooser did not respond") from exc


def _has_display() -> bool:
    if sys.platform in {"darwin", "win32"}:
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _tk_available() -> bool:
    if not _has_display():
        return False
    try:
        import tkinter  # noqa: F401
    except Exception:
        return False
    return True


def backend() -> str:
    """Name the chooser this machine will use, or "none"."""
    if sys.platform == "darwin":
        return "osascript"
    if not _has_display():
        return "none"
    if sys.platform == "win32":
        return "tkinter" if _tk_available() else "none"
    for candidate in ("zenity", "kdialog"):
        if shutil.which(candidate):
            return candidate
    return "tkinter" if _tk_available() else "none"


def status() -> dict[str, object]:
    """Describe chooser availability for the interface to report."""
    name = backend()
    if name != "none":
        return {"backend": name, "available": True, "detail": ""}
    if not _has_display():
        detail = (
            "No graphical display is attached, so native choosers are "
            "unavailable. Type paths directly instead."
        )
    elif sys.platform.startswith("linux"):
        detail = (
            "No native chooser was found. Install zenity (GNOME) or kdialog "
            "(KDE), or install Tk support for Python."
        )
    else:
        detail = "No native chooser is available; type paths directly instead."
    return {"backend": "none", "available": False, "detail": detail}


def _require_backend() -> str:
    name = backend()
    if name == "none":
        raise DialogError(str(status()["detail"]))
    return name


# --- macOS -----------------------------------------------------------------

def _osascript(script: str, what: str) -> str:
    result = _run(["osascript", "-e", script])
    if result.returncode != 0:
        # -128 is AppleScript's "user cancelled".
        if "-128" in result.stderr:
            raise DialogCancelled(f"{what} was cancelled")
        raise DialogError(f"{what} failed: {result.stderr.strip()}")
    return result.stdout


# --- zenity / kdialog ------------------------------------------------------

def _zenity(args: list[str], what: str) -> str:
    result = _run(["zenity", *args])
    if result.returncode != 0:
        # zenity exits 1 on cancel and 5 on timeout.
        if result.returncode in (1, 5):
            raise DialogCancelled(f"{what} was cancelled")
        raise DialogError(f"{what} failed: {result.stderr.strip()}")
    return result.stdout


def _kdialog(args: list[str], what: str) -> str:
    result = _run(["kdialog", *args])
    if result.returncode != 0:
        if result.returncode == 1:
            raise DialogCancelled(f"{what} was cancelled")
        raise DialogError(f"{what} failed: {result.stderr.strip()}")
    return result.stdout


# --- Tk fallback -----------------------------------------------------------

def _tk(kind: str, prompt: str, image_only: bool, multiple: bool,
        default_name: str) -> str:
    import tkinter
    from tkinter import filedialog

    root = tkinter.Tk()
    root.withdraw()
    # Without this the chooser can open behind the browser window.
    root.attributes("-topmost", True)
    try:
        if kind == "folder":
            chosen = filedialog.askdirectory(title=prompt, mustexist=True)
        elif kind == "save":
            chosen = filedialog.asksaveasfilename(
                title=prompt, initialfile=default_name)
        else:
            types = (
                [("Images", " ".join(IMAGE_PATTERNS))] if image_only
                else [("All files", "*.*")]
            )
            if multiple:
                picked = filedialog.askopenfilenames(
                    title=prompt, filetypes=types)
                chosen = "\n".join(picked)
            else:
                chosen = filedialog.askopenfilename(
                    title=prompt, filetypes=types)
    finally:
        root.destroy()
    if not chosen:
        raise DialogCancelled(f"{prompt} was cancelled")
    return chosen


# --- public API ------------------------------------------------------------

def choose_folder(prompt: str) -> str:
    name = _require_backend()
    if name == "osascript":
        out = _osascript(
            f'POSIX path of (choose folder with prompt "{prompt}")', prompt)
    elif name == "zenity":
        out = _zenity(
            ["--file-selection", "--directory", f"--title={prompt}"], prompt)
    elif name == "kdialog":
        out = _kdialog(["--getexistingdirectory", str(Path.home())], prompt)
    else:
        out = _tk("folder", prompt, False, False, "")
    folder = out.strip().rstrip("/")
    if not folder:
        raise DialogCancelled(f"{prompt} was cancelled")
    return folder


def choose_files(
    prompt: str, *, image_only: bool = False, multiple: bool = False,
) -> list[str]:
    name = _require_backend()
    if name == "osascript":
        of_type = ' of type {"public.image"}' if image_only else ""
        selection = (
            " with multiple selections allowed" if multiple else "")
        script = (
            f'set picked to choose file with prompt "{prompt}"'
            f'{of_type}{selection}\n'
            'set output to ""\n'
            'repeat with itemRef in (picked as list)\n'
            'set output to output & (POSIX path of itemRef) & linefeed\n'
            'end repeat\n'
            'return output'
        )
        out = _osascript(script, prompt)
    elif name == "zenity":
        args = ["--file-selection", f"--title={prompt}"]
        if multiple:
            args += ["--multiple", "--separator=\n"]
        if image_only:
            args += [
                "--file-filter=Images | " + " ".join(IMAGE_PATTERNS),
                "--file-filter=All files | *",
            ]
        out = _zenity(args, prompt)
    elif name == "kdialog":
        filters = " ".join(IMAGE_PATTERNS) if image_only else "*"
        args = ["--getopenfilename", str(Path.home()), filters]
        if multiple:
            args.insert(0, "--multiple")
            args.append("--separate-output")
        out = _kdialog(args, prompt)
    else:
        out = _tk("file", prompt, image_only, multiple, "")
    chosen = [line.strip() for line in out.splitlines() if line.strip()]
    if not chosen:
        raise DialogCancelled(f"{prompt} was cancelled")
    return chosen if multiple else chosen[:1]


def choose_save_path(prompt: str, default_name: str = "") -> str:
    name = _require_backend()
    if name == "osascript":
        default = f' default name "{default_name}"' if default_name else ""
        out = _osascript(
            f'POSIX path of (choose file name with prompt "{prompt}"{default})',
            prompt)
    elif name == "zenity":
        args = ["--file-selection", "--save", "--confirm-overwrite",
                f"--title={prompt}"]
        if default_name:
            args.append(f"--filename={default_name}")
        out = _zenity(args, prompt)
    elif name == "kdialog":
        out = _kdialog(
            ["--getsavefilename", str(Path.home() / default_name), "*"], prompt)
    else:
        out = _tk("save", prompt, False, False, default_name)
    chosen = out.strip()
    if not chosen:
        raise DialogCancelled(f"{prompt} was cancelled")
    return chosen


def reveal(path: Path) -> None:
    """Show a file in the platform's file manager, selected where possible."""
    target = Path(path)
    if sys.platform == "darwin":
        command = ["open", "-R", str(target)]
    elif sys.platform == "win32":
        command = ["explorer", f"/select,{target}"]
    elif shutil.which("dbus-send"):
        # The FileManager1 interface selects the item rather than merely
        # opening its directory, which is what "reveal" means.
        command = [
            "dbus-send", "--session", "--print-reply",
            "--dest=org.freedesktop.FileManager1",
            "/org/freedesktop/FileManager1",
            "org.freedesktop.FileManager1.ShowItems",
            f"array:string:file://{target}", "string:",
        ]
    elif shutil.which("xdg-open"):
        command = ["xdg-open", str(target.parent)]
    else:
        raise DialogError(
            "no file manager is available; open the folder manually")
    try:
        # Detached and reaped: a file manager outlives this request, and an
        # un-waited child would otherwise be left as a zombie.
        subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
    except (OSError, FileNotFoundError) as exc:
        if sys.platform.startswith("linux") and shutil.which("xdg-open"):
            subprocess.Popen(
                ["xdg-open", str(target.parent)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
            return
        raise DialogError(f"could not open the file manager: {exc}") from exc
