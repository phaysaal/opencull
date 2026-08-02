#!/bin/zsh
set -euo pipefail

project_dir=${0:A:h:h}
app_path=${1:-"$project_dir/dist/native/Darkimiya.app"}
receipt_dir=${2:-"$project_dir/dist/native/receipts"}
plist="$app_path/Contents/Info.plist"
frontend="$app_path/Contents/MacOS/Darkimiya"
backend="$app_path/Contents/MacOS/DarkimiyaBackend"

[[ -d "$app_path" ]] || { echo "Missing app: $app_path" >&2; exit 2; }
[[ -x "$frontend" ]] || { echo "Missing SwiftUI frontend" >&2; exit 2; }
[[ -x "$backend" ]] || { echo "Missing Python/Kimiya backend" >&2; exit 2; }

mkdir -p "$receipt_dir"

identifier=$(/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" "$plist")
version=$(/usr/libexec/PlistBuddy -c \
  "Print :CFBundleShortVersionString" "$plist")
[[ "$identifier" == "org.darkimiya.Darkimiya" ]]
[[ "$version" == "0.13.0" ]]

file "$frontend" | grep -q "arm64"
file "$backend" | grep -q "arm64"
otool -L "$frontend" | grep -q "SwiftUI.framework"
codesign --verify --deep --strict --verbose=0 "$app_path"

smoke_json="$receipt_dir/native-backend-smoke.json"
"$backend" --release-smoke-test "$smoke_json"
python3 - "$smoke_json" <<'PY'
import json
import sys
from pathlib import Path

receipt = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if not receipt.get("passed"):
    raise SystemExit("frozen backend smoke test failed")
PY

{
  echo "identifier=$identifier"
  echo "version=$version"
  echo "frontend=$(file "$frontend")"
  echo "backend=$(file "$backend")"
  echo "signature=valid"
  echo "backend_smoke=passed"
} > "$receipt_dir/native-release.txt"

echo "$receipt_dir/native-release.txt"
