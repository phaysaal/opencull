#!/bin/zsh
set -euo pipefail

project_dir=${0:A:h:h}
cd "$project_dir"

python_bin=${PYTHON_BIN:-"$project_dir/.macos-build-venv-arm64/bin/python"}
build_dir="$project_dir/build/macos"
dist_dir=${DIST_DIR:-"$project_dir/dist"}
export PYINSTALLER_CONFIG_DIR="$build_dir/pyinstaller-config"

if [[ ! -x "$python_bin" ]]; then
  echo "Missing native build environment: $python_bin" >&2
  echo "See README.md, Phase 10 macOS build." >&2
  exit 2
fi

"$project_dir/scripts/build_icon.sh"

"$python_bin" -m PyInstaller \
  --noconfirm \
  --clean \
  --distpath "$dist_dir" \
  --workpath "$build_dir" \
  OpenCull.spec

codesign --force --deep --sign - "$dist_dir/OpenCull.app"
codesign --verify --deep --strict --verbose=0 "$dist_dir/OpenCull.app"

if [[ ${SKIP_DMG:-0} != 1 ]]; then
  rm -f "$dist_dir/OpenCull-0.10.0-arm64.dmg"
  dmg_stage=$(mktemp -d)
  trap 'rm -rf "$dmg_stage"' EXIT
  ditto "$dist_dir/OpenCull.app" "$dmg_stage/OpenCull.app"
  ln -s /Applications "$dmg_stage/Applications"
  hdiutil create \
    -volname "OpenCull 0.10.0" \
    -srcfolder "$dmg_stage" \
    -ov \
    -format UDZO \
    "$dist_dir/OpenCull-0.10.0-arm64.dmg"
fi

echo "$dist_dir/OpenCull.app"
if [[ ${SKIP_DMG:-0} != 1 ]]; then
  echo "$dist_dir/OpenCull-0.10.0-arm64.dmg"
fi
