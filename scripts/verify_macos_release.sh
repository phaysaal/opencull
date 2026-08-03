#!/bin/zsh
set -euo pipefail

project_dir=${0:A:h:h}
app_path=${1:-"$project_dir/dist/OpenCull.app"}
receipt_dir=${2:-"$project_dir/dist/release-receipts"}
executable="$app_path/Contents/MacOS/OpenCull"
resources="$app_path/Contents/Resources"

if [[ ! -d "$app_path" ]]; then
  echo "Application bundle not found: $app_path" >&2
  exit 2
fi

mkdir -p "$receipt_dir"

codesign --verify --deep --strict --verbose=0 "$app_path"

architecture=$(lipo -archs "$executable")
if [[ "$architecture" != "arm64" ]]; then
  echo "Release executable must be arm64, found: $architecture" >&2
  exit 3
fi

identifier=$(/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" \
  "$app_path/Contents/Info.plist")
version=$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" \
  "$app_path/Contents/Info.plist")
if [[ "$identifier" != "org.opencull.OpenCull" ]]; then
  echo "Unexpected bundle identifier: $identifier" >&2
  exit 4
fi

required=(
  "$resources/opencull.kim"
  "$resources/professional_shortlist.kim"
  "$resources/agents.kim"
  "$resources/scan.py"
  "$resources/opencull_kernel.py"
  "$resources/shortlist_kernel.py"
  "$resources/opencull_gui/static/index.html"
  "$resources/opencull_gui/static/styles.css"
  "$resources/opencull_gui/static/app.js"
  "$resources/.opencull-models/face_detection_yunet_2023mar.onnx"
  "$resources/.opencull-models/face_recognition_sface_2021dec.onnx"
)
for item in "${required[@]}"; do
  if [[ ! -f "$item" ]]; then
    echo "Required bundled resource missing: $item" >&2
    exit 5
  fi
done

smoke_receipt="$receipt_dir/packaged-smoke.json"
"$executable" --release-smoke-test "$smoke_receipt"

SMOKE_RECEIPT="$smoke_receipt" /usr/bin/python3 - <<'PY'
import json
import os
from pathlib import Path

receipt = json.loads(Path(os.environ["SMOKE_RECEIPT"]).read_text())
if receipt.get("passed") is not True:
    raise SystemExit(f"packaged smoke failed: {receipt}")
if receipt.get("frozen") is not True:
    raise SystemExit("packaged smoke did not run from a frozen application")
if receipt.get("architecture") != "arm64":
    raise SystemExit(
        f"packaged runtime architecture is {receipt.get('architecture')}, not arm64")
PY

{
  echo "OpenCull macOS release verification"
  echo "bundle=$app_path"
  echo "identifier=$identifier"
  echo "version=$version"
  echo "architecture=$architecture"
  echo "signature=valid"
  echo "required_resources=${#required}"
  echo "packaged_smoke=passed"
} > "$receipt_dir/release-verification.txt"

echo "$receipt_dir/release-verification.txt"
echo "$smoke_receipt"
