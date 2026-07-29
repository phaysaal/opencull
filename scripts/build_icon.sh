#!/bin/zsh
set -euo pipefail

project_dir=${0:A:h:h}
source_svg="$project_dir/assets/opencull-icon.svg"
iconset="$project_dir/build/OpenCull.iconset"
output="$project_dir/assets/OpenCull.icns"
magick_bin=${MAGICK_BIN:-/opt/homebrew/bin/magick}
master_png="$project_dir/build/OpenCull-1024.png"
makeicns_bin=${MAKEICNS_BIN:-/opt/homebrew/bin/makeicns}

if [[ ! -x "$magick_bin" ]]; then
  echo "ImageMagick is required: brew install imagemagick" >&2
  exit 2
fi
if [[ ! -x "$makeicns_bin" ]]; then
  echo "makeicns is required: brew install makeicns" >&2
  exit 2
fi

mkdir -p "$iconset"
"$magick_bin" -background none "$source_svg" -depth 8 \
  -resize 1024x1024 "PNG32:$master_png"
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" "$master_png" \
    --out "$iconset/icon_${size}x${size}.png" >/dev/null
  double=$((size * 2))
  sips -z "$double" "$double" "$master_png" \
    --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null
done

"$makeicns_bin" \
  -512 "$iconset/icon_512x512.png" \
  -256 "$iconset/icon_256x256.png" \
  -128 "$iconset/icon_128x128.png" \
  -32 "$iconset/icon_32x32.png" \
  -16 "$iconset/icon_16x16.png" \
  -out "$output"
echo "$output"
