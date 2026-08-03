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

# The bundle name and version follow OpenCull.spec and pyproject.toml. They
# were previously spelled out here and had drifted: this script signed
# OpenCull.app while the spec emitted Darkimiya.app, so it could not complete.
app_name=$("$python_bin" - "$project_dir" <<'PY'
import re, sys, tomllib
from pathlib import Path
root = Path(sys.argv[1])
spec = (root / "OpenCull.spec").read_text(encoding="utf-8")
match = re.search(r'name="([^"]+\.app)"', spec)
print(match.group(1) if match else "Darkimiya.app")
PY
)
version=$("$python_bin" -c \
  'import tomllib,sys;print(tomllib.load(open(sys.argv[1],"rb"))["project"]["version"])' \
  "$project_dir/pyproject.toml")
app_stem=${app_name%.app}
dmg_path="$dist_dir/$app_stem-$version-arm64.dmg"

"$project_dir/scripts/build_icon.sh"

"$python_bin" -m PyInstaller \
  --noconfirm \
  --clean \
  --distpath "$dist_dir" \
  --workpath "$build_dir" \
  OpenCull.spec

codesign --force --deep --sign - "$dist_dir/$app_name"
codesign --verify --deep --strict --verbose=0 "$dist_dir/$app_name"

if [[ ${SKIP_DMG:-0} != 1 ]]; then
  rm -f "$dmg_path"
  dmg_stage=$(mktemp -d)
  trap 'rm -rf "$dmg_stage"' EXIT
  ditto "$dist_dir/$app_name" "$dmg_stage/$app_name"
  ln -s /Applications "$dmg_stage/Applications"
  hdiutil create \
    -volname "$app_stem $version" \
    -srcfolder "$dmg_stage" \
    -ov \
    -format UDZO \
    "$dmg_path"
fi

echo "$dist_dir/$app_name"
if [[ ${SKIP_DMG:-0} != 1 ]]; then
  echo "$dmg_path"
fi
