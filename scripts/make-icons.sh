#!/bin/sh
# Make the app icon files from desktop/build/icon.svg:
#   desktop/build/icon.png    1024px, the window and Dock icon when running from source
#   desktop/build/icon.icns   the packaged app's icon (electron-builder picks it up)
#   frontend/public/icon.svg  favicon and header logo: the same drawing cropped to the tile
# Needs rsvg-convert (brew install librsvg) and macOS's iconutil. The outputs are
# committed, so builds don't need either tool.
set -eu
root="$(cd "$(dirname "$0")/.." && pwd)"
src="$root/desktop/build/icon.svg"
out="$root/desktop/build"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

rsvg-convert -w 1024 -h 1024 "$src" -o "$out/icon.png"

set_dir="$tmp/icon.iconset"
mkdir "$set_dir"
for size in 16 32 128 256 512; do
  rsvg-convert -w "$size" -h "$size" "$src" -o "$set_dir/icon_${size}x${size}.png"
  double=$((size * 2))
  rsvg-convert -w "$double" -h "$double" "$src" -o "$set_dir/icon_${size}x${size}@2x.png"
done
iconutil -c icns "$set_dir" -o "$out/icon.icns"

sed -e 's|viewBox="0 0 1024 1024"|viewBox="100 100 824 824"|' \
    -e 's|<!-- Source for icon.png.*-->|<!-- Generated from desktop/build/icon.svg by scripts/make-icons.sh. -->|' \
    "$src" > "$root/frontend/public/icon.svg"

ls -l "$out/icon.png" "$out/icon.icns" "$root/frontend/public/icon.svg"
