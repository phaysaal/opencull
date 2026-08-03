#!/bin/zsh
set -euo pipefail

project_dir=${0:A:h:h}
cd "$project_dir"

python_bin=${PYTHON_BIN:-"$project_dir/.macos-build-venv-arm64/bin/python"}
output_root=${DIST_DIR:-"$project_dir/dist/native"}
swift_scratch="$project_dir/build/native-swift"
pyinstaller_work="$project_dir/build/native-pyinstaller"
backend_dist="$project_dir/build/native-backend-dist"
app_path="$output_root/Darkimiya.app"
export CLANG_MODULE_CACHE_PATH="$swift_scratch/module-cache"
export SWIFTPM_MODULECACHE_OVERRIDE="$swift_scratch/module-cache"
export SWIFTPM_CONFIG_PATH="$swift_scratch/swiftpm-config"
mkdir -p "$CLANG_MODULE_CACHE_PATH" "$SWIFTPM_CONFIG_PATH"

if [[ ! -x "$python_bin" ]]; then
  echo "Missing native Python build environment: $python_bin" >&2
  exit 2
fi

# The build environment is intentionally reusable, but OpenCull's packaged
# runtime grows as features are added.  Keep an existing environment in sync
# instead of producing an application that only fails when a worker starts.
if ! "$python_bin" -c 'import numpy, PIL, tifffile' 2>/dev/null; then
  echo "Synchronizing native runtime dependencies..."
  "$python_bin" -m pip install \
    --disable-pip-version-check \
    -r "$project_dir/requirements.txt"
fi

compiler_version=$(swift --version | head -1)
sdk_path=$(xcrun --sdk macosx --show-sdk-path)
echo "Swift toolchain: $compiler_version"
echo "macOS SDK: $sdk_path"

rm -rf "$backend_dist"
mkdir -p "$output_root"

swift build \
  --disable-sandbox \
  --package-path "$project_dir/native-macos" \
  --scratch-path "$swift_scratch" \
  -c release
swift_bin=$(find "$swift_scratch" -type f \
  -path "*/release/OpenCullNative" -perm -111 | head -1)
if [[ -z "$swift_bin" ]]; then
  echo "SwiftUI release executable was not produced" >&2
  exit 3
fi

"$project_dir/scripts/build_icon.sh"
PYINSTALLER_CONFIG_DIR="$pyinstaller_work/config" \
  "$python_bin" -m PyInstaller \
    --noconfirm \
    --clean \
    --distpath "$backend_dist" \
    --workpath "$pyinstaller_work" \
    OpenCull.spec

rm -rf "$app_path"
ditto "$backend_dist/Darkimiya.app" "$app_path"
mv "$app_path/Contents/MacOS/Darkimiya" \
  "$app_path/Contents/MacOS/DarkimiyaBackend"
cp "$swift_bin" "$app_path/Contents/MacOS/Darkimiya"
chmod 755 \
  "$app_path/Contents/MacOS/Darkimiya" \
  "$app_path/Contents/MacOS/DarkimiyaBackend"

/usr/libexec/PlistBuddy -c \
  "Set :CFBundleShortVersionString 0.13.0" \
  "$app_path/Contents/Info.plist"
/usr/libexec/PlistBuddy -c \
  "Set :CFBundleVersion 14" \
  "$app_path/Contents/Info.plist"

codesign --force --sign - "$app_path/Contents/MacOS/DarkimiyaBackend"
codesign --force --deep --sign - "$app_path"
codesign --verify --deep --strict --verbose=0 "$app_path"

"$project_dir/scripts/verify_native_macos_release.sh" \
  "$app_path" "$output_root/receipts"

archive="$output_root/Darkimiya-0.13.0-arm64.zip"
rm -f "$archive"
ditto -c -k --sequesterRsrc --keepParent "$app_path" "$archive"
shasum -a 256 "$archive" > "$archive.sha256"

echo "$app_path"
echo "$archive"
