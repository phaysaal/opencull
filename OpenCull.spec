# -*- mode: python ; coding: utf-8 -*-
"""Reproducible Apple Silicon Darkimiya application bundle."""

import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


ROOT = Path(SPECPATH).resolve()
KIMIYA = ROOT.parent / "kimiya-lang"
MODELS = ROOT / ".opencull-models"

# pyproject.toml is the single source of the version. It previously appeared in
# five places across the spec and the build and verify scripts, and had already
# drifted between them.
with (ROOT / "pyproject.toml").open("rb") as handle:
    VERSION = tomllib.load(handle)["project"]["version"]

if not KIMIYA.is_dir():
    raise SystemExit(
        f"kimiya-lang is required to build but was not found at {KIMIYA}. "
        "Check it out beside this repository, or symlink it there."
    )

datas = [
    (str(ROOT / "opencull_gui" / "static"), "opencull_gui/static"),
    (str(ROOT / "opencull.kim"), "."),
    (str(ROOT / "professional_shortlist.kim"), "."),
    (str(ROOT / "edit_suggestions.kim"), "."),
    (str(ROOT / "style_profile.kim"), "."),
    (str(ROOT / "semantic_verification.kim"), "."),
    (str(ROOT / "agents.kim"), "."),
    # Kimiya hashes and loads these as explicit audited source extensions.
    (str(ROOT / "scan.py"), "."),
    (str(ROOT / "opencull_kernel.py"), "."),
    (str(ROOT / "shortlist_kernel.py"), "."),
    (str(ROOT / "edit_suggestion_kernel.py"), "."),
    (str(ROOT / "style_profile_kernel.py"), "."),
    (str(ROOT / "semantic_verification_kernel.py"), "."),
    (str(ROOT / "README.md"), "."),
    (str(ROOT / "LICENSE"), "."),
]
if MODELS.is_dir():
    datas.append((str(MODELS), ".opencull-models"))

hiddenimports = (
    collect_submodules("kimiya")
    + [
        "scan", "opencull_kernel", "shortlist_kernel",
        "edit_suggestion_kernel", "style_profile_kernel",
        "semantic_verification_kernel",
        "development_pipeline", "development_engine", "raw_developer",
        "comparison_pipeline", "renderer_export_pipeline", "delivery_export_pipeline",
        "renderer_comparison",
        "darktable_engine",
        "recipe_compiler",
        "cv2", "PIL._tkinter_finder",
        # rawpy carries the LibRaw extension. Omitting it produced a bundle
        # that silently fell back to one `sips` subprocess per photograph.
        "rawpy", "rawpy._rawpy",
    ]
)

a = Analysis(
    ["opencull_desktop.py"],
    pathex=[str(ROOT), str(KIMIYA)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "IPython", "notebook", "matplotlib"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Darkimiya",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    # Internal review and Kimiya worker processes invoke this executable
    # directly. LaunchServices argv emulation would intercept those launches.
    argv_emulation=False,
    target_arch="arm64",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Darkimiya",
)

app = BUNDLE(
    coll,
    name="Darkimiya.app",
    icon=str(ROOT / "assets" / "OpenCull.icns"),
    bundle_identifier="org.darkimiya.Darkimiya",
    version=VERSION,
    info_plist={
        "CFBundleDisplayName": "Darkimiya",
        "CFBundleName": "Darkimiya",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
    },
)
