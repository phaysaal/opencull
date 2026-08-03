#!/usr/bin/env python3
"""Download and verify OpenCV's permissively licensed local face models."""

from __future__ import annotations

import argparse
import hashlib
import os
import tempfile
import urllib.request
from pathlib import Path

from opencull_gui.faces import (
    SFACE_NAME,
    SFACE_SHA256,
    YUNET_NAME,
    YUNET_SHA256,
)

MODELS = (
    (
        YUNET_NAME,
        "https://github.com/opencv/opencv_zoo/raw/main/"
        "models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        YUNET_SHA256,
        "MIT",
    ),
    (
        SFACE_NAME,
        "https://github.com/opencv/opencv_zoo/raw/main/"
        "models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
        SFACE_SHA256,
        "Apache-2.0",
    ),
)


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def install(destination: Path) -> None:
    destination = destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    for name, url, expected, license_name in MODELS:
        target = destination / name
        if target.is_file() and digest(target) == expected:
            print(f"verified {name} ({license_name})")
            continue
        handle = tempfile.NamedTemporaryFile(
            dir=destination, prefix=f".{name}.", suffix=".download",
            delete=False)
        temporary = Path(handle.name)
        handle.close()
        try:
            print(f"downloading {name} from OpenCV ({license_name})")
            urllib.request.urlretrieve(url, temporary)
            actual = digest(temporary)
            if actual != expected:
                raise RuntimeError(
                    f"hash mismatch for {name}: expected {expected}, got {actual}")
            os.chmod(temporary, 0o644)
            os.replace(temporary, target)
            print(f"installed {target} sha256={actual}")
        except Exception:
            temporary.unlink(missing_ok=True)
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "destination", nargs="?", type=Path, default=Path(".opencull-models"))
    args = parser.parse_args()
    install(args.destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
