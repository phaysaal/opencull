"""The photographer's own Kimiya programs, kept and run like the built-ins.

The built-in programs -- the cull, the shortlist, the treatment -- are
code: tested, shipped, read-only. But the treatment proved a pattern
bigger than one program, and the person this application belongs to
writes Kimiya. So programs are a thing the Studio holds: the built-ins
to read and copy, and the photographer's own -- written in the editor
or duplicated from a built-in and bent to the new purpose -- checked by
the Kimiya compiler before they are kept, and run through exactly the
queue, provider bundles and credential handling every built-in uses.

What a program may reach is stated rather than hidden. A user program
lives in the Studio's Programs directory; its `use python` kernels
resolve first beside the program, then to the application's own audited
kernels -- so a new program can lean on treatment_kernel.py without
copying it. The Kimiya runtime prints what every run reaches for:
kernel hashes, network egress, image egress. Nothing here weakens that;
this is a door for the person who owns the machine.
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FORMAT = "darkimiya-program-store-v1"

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,60}$")

# The shipped programs, with one line each of what they are for. Offered
# read-only: duplicating one is how an edit starts, because a built-in
# that can drift is a built-in nobody can reason about.
BUILT_INS = (
    ("opencull.kim", "The cull: group near-duplicates, propose keepers."),
    ("professional_shortlist.kim",
     "Assess a culled folder against a stated policy."),
    ("edit_suggestions.kim",
     "Editing directions for chosen frames, one answer per frame."),
    ("semantic_verification.kim",
     "Certify that a rendering did what its suggestion promised."),
    ("style_profile.kim", "Read a personal style from example folders."),
    ("protect_then_reveal.kim",
     "The Kimiya Treatment: develop one frame in criticised rounds."),
    ("control_zones.kim",
     "Place the fine-tune sliders' advice bands for one frame."),
    ("eclipse_timelapse.kim",
     "Align a handheld eclipse sequence into timelapse frames."),
    ("subject_timelapse.kim",
     "Pin any subject for a timelapse: mark it once, track it free."),
    ("named_subject_timelapse.kim",
     "Pin a subject you can only name: a model anchors, the tracker "
     "carries."),
    ("dust_removal.kim",
     "Map sensor dust across a folder; every render heals it first."),
    ("learn_look.kim",
     "Learn a look from RAW+JPEG pairs: the camera itself is the "
     "teacher."),
    ("superimpose.kim",
     "Lay many frames of one sky on each other: trails, or a stack."),
    ("handheld_stack.kim",
     "Stack a handheld sky: a model recognises, the stars register."),
    ("clear_stack.kim",
     "Stack a sky, asking about the frames a threshold cannot call."),
    ("night_show.kim",
     "One click: night frames in, the finished starfield out, "
     "verified at every step."),
)

_PARAM_RE = re.compile(
    r"^param\s+([a-z_][a-z0-9_]*)\s*:\s*(text|num|bool)"
    r"(?:\s*=\s*(.+?))?\s*$")


class ProgramError(ValueError):
    """A program cannot be kept or offered."""


class ProgramStore:
    """The Studio's programs: built-ins read-only, the user's editable."""

    def __init__(self, directory: Path, project_root: Path,
                 python: str = "", kimiya_checkout: Path | None = None):
        self.directory = Path(directory).expanduser().resolve()
        self.project_root = Path(project_root).expanduser().resolve()
        self.python = python or "python3"
        self.kimiya_checkout = (
            Path(kimiya_checkout).resolve() if kimiya_checkout
            else self.project_root.parent / "kimiya-lang")

    # --- what there is ----------------------------------------------------

    def catalogue(self) -> list[dict[str, Any]]:
        """Every program, built-in first, the user's after."""
        found: list[dict[str, Any]] = []
        for name, purpose in BUILT_INS:
            path = self.project_root / name
            if path.is_file():
                found.append({
                    "name": name, "kind": "built-in", "purpose": purpose,
                    "path": str(path)})
        if self.directory.is_dir():
            for path in sorted(self.directory.glob("*.kim")):
                found.append({
                    "name": path.name, "kind": "yours",
                    "purpose": _first_comment(path),
                    "path": str(path)})
        return found

    def read(self, name: str) -> str:
        return self._resolve(name).read_text(encoding="utf-8")

    def _resolve(self, name: str) -> Path:
        """The file a name refers to, and never anything outside the two
        places programs live."""
        base = Path(str(name)).name
        if base != str(name):
            raise ProgramError(f"program names carry no directories: {name!r}")
        mine = self.directory / base
        if mine.is_file():
            return mine
        if base in {built for built, _purpose in BUILT_INS}:
            shipped = self.project_root / base
            if shipped.is_file():
                return shipped
        raise ProgramError(f"no such program: {base}")

    # --- keeping one --------------------------------------------------------

    def save(self, name: str, text: str) -> Path:
        """Keep one of the photographer's programs, never a built-in.

        The name is validated, the built-ins are shadow-proof -- a user
        program may not take a shipped program's name, or the queue
        could run something other than what the tests tested under the
        same label -- and the text must be a program, not emptiness.
        """
        base = str(name).strip()
        if not base.endswith(".kim"):
            base += ".kim"
        stem = base[:-4]
        if not NAME_RE.fullmatch(stem):
            raise ProgramError(
                "a program name is lowercase letters, digits, - and _, "
                "ending in .kim")
        if base in {built for built, _purpose in BUILT_INS}:
            raise ProgramError(
                f"{base} is a built-in; duplicate it under a new name "
                "instead of shadowing it")
        if not str(text).strip():
            raise ProgramError("an empty program is not a program")
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / base
        path.write_text(str(text), encoding="utf-8")
        return path

    def duplicate(self, source_name: str, new_name: str) -> Path:
        """A built-in (or any program) copied as the start of an edit."""
        text = self.read(source_name)
        stamp = datetime.now(UTC).strftime("%Y-%m-%d")
        note = (f"-- Duplicated from {Path(source_name).name} on {stamp}; "
                "edit freely, the original does not change.\n")
        return self.save(new_name, note + text)

    def delete(self, name: str) -> None:
        base = Path(str(name)).name
        path = self.directory / base
        if not path.is_file():
            raise ProgramError(
                "only your own programs can be deleted, and this is not one")
        path.unlink()

    # --- what a program asks for --------------------------------------------

    def parameters(self, name: str) -> list[dict[str, Any]]:
        """The program's own params, parsed off its head, for a run form."""
        found = []
        for line in self.read(name).splitlines():
            match = _PARAM_RE.match(line.strip())
            if match:
                default = (match.group(3) or "").strip().strip('"')
                found.append({"name": match.group(1),
                              "type": match.group(2),
                              "default": default})
        return found

    # --- the compiler's word --------------------------------------------------

    def check(self, name: str) -> tuple[bool, str]:
        """Run `kimiya check` on one program, kernels resolved as a run
        would resolve them, and return the compiler's own words."""
        staged = self._staged_for_check(name)
        environment = dict(os.environ)
        if (self.kimiya_checkout / "kimiya").is_dir():
            existing = environment.get("PYTHONPATH", "")
            environment["PYTHONPATH"] = ":".join(
                part for part in [str(self.kimiya_checkout), existing]
                if part)
        result = subprocess.run(
            [self.python, "-m", "kimiya", "check", str(staged)],
            cwd=self.project_root, env=environment,
            capture_output=True, text=True, check=False, timeout=120)
        said = (result.stdout + "\n" + result.stderr).strip()
        return result.returncode == 0, said

    def _staged_for_check(self, name: str) -> Path:
        """The program with its `use` lines resolved, in a scratch spot.

        The same resolution a run gets: agents.kim from the project
        root (the legacy one -- a check needs declarations, not a
        credential), kernels beside the program first, then the
        application's own.
        """
        source = self.read(name)
        staged_dir = self.directory / ".checked"
        staged_dir.mkdir(parents=True, exist_ok=True)
        staged = staged_dir / Path(str(name)).name
        staged.write_text(
            self.resolve_uses(source, agents=self.project_root / "agents.kim"),
            encoding="utf-8")
        return staged

    def resolve_uses(self, source: str, agents: Path) -> str:
        """Rewrite every relative `use` to the file it will really load."""
        def replace(match: re.Match[str]) -> str:
            kind, quoted = match.group(1) or "", match.group(2)
            if quoted == "agents.kim":
                return f'use {kind}"{agents}"'
            if Path(quoted).is_absolute():
                return match.group(0)
            beside = self.directory / quoted
            shipped = self.project_root / quoted
            chosen = beside if beside.is_file() else shipped
            if not chosen.is_file():
                raise ProgramError(
                    f"the program uses {quoted!r}, which is neither beside "
                    "it nor one of the application's own files")
            return f'use {kind}"{chosen}"'

        return re.sub(
            r'use\s+(python\s+)?"([^"]+)"',
            lambda match: replace(match), source)

    def describe(self) -> dict[str, Any]:
        return {"format": FORMAT, "directory": str(self.directory),
                "programs": self.catalogue()}


def _first_comment(path: Path) -> str:
    """The program's own first words, as its one-line purpose."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("--"):
                return stripped.lstrip("- ").strip()
            if stripped:
                break
    except OSError:
        pass
    return "One of your own programs."


def parse_arguments(pairs: dict[str, Any]) -> list[str]:
    """A run form's values as the key=value words kimiya run takes."""
    spoken = []
    for key, value in pairs.items():
        name = str(key).strip()
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", name):
            raise ProgramError(f"not a parameter name: {key!r}")
        spoken.append(f"{name}={value}")
    return spoken
