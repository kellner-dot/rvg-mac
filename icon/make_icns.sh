#!/bin/bash
# Build RVG Mac.icns from the 1024px master PNG. RUN ON THE MAC.
# Requires: iconutil (ships with macOS).
set -euo pipefail
cd "$(dirname "$0")"

ICONSET="RVG Mac.iconset"
rm -rf "$ICONSET"
mkdir -p "$ICONSET"

# iconutil wants specific sizes; sips generates them from the master
for s in 16 32 128 256 512; do
    sips -z $s $s rvg-icon-1024.png --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
    s2=$((s * 2))
    if [ "$s2" -le 1024 ]; then
        sips -z $s2 $s2 rvg-icon-1024.png --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
    fi
done

iconutil -c icns "$ICONSET" -o "RVG Mac.icns"
rm -rf "$ICONSET"
echo "Built: RVG Mac.icns"
echo "Copy it into the .app bundle:"
echo "  cp \"RVG Mac.icns\" \"../app/RVG Mac.app/Contents/Resources/\""
