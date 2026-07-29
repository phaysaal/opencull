# -*- mode: python ; coding: utf-8 -*-
"""Reproducible Apple Silicon OpenCull application bundle."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


ROOT = Path(SPECPATH).resolve()
KIMIYA = ROOT.parent / "kimiya-lang"
MODELS = ROOT / ".opencull-models"

datas = [
    (str(ROOT / "opencull_gui" / "static"), "opencull_gui/static"),
    (str(ROOT / "opencull.kim"), "."),
    (str(ROOT / "agents.kim"), "."),
    # Kimiya hashes and loads these as explicit audited source extensions.
    (str(ROOT / "scan.py"), "."),
    (str(ROOT / "opencull_kernel.py"), "."),
    (str(ROOT / "README.md"), "."),
    (str(ROOT / "LICENSE"), "."),
]
if MODELS.is_dir():
    datas.append((str(MODELS), ".opencull-models"))

hiddenimports = (
    collect_submodules("kimiya")
    + ["scan", "opencull_kernel", "cv2", "PIL._tkinter_finder"]
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
    name="OpenCull",
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
    name="OpenCull",
)

app = BUNDLE(
    coll,
    name="OpenCull.app",
    icon=str(ROOT / "assets" / "OpenCull.icns"),
    bundle_identifier="org.opencull.OpenCull",
    version="0.10.0",
    info_plist={
        "CFBundleDisplayName": "OpenCull",
        "CFBundleName": "OpenCull",
        "CFBundleShortVersionString": "0.10.0",
        "CFBundleVersion": "10",
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
    },
)
