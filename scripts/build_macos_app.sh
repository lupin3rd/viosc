#!/usr/bin/env bash
# Build a self-contained viOSC .app + DMG (e04s02).
#
# Usage: [ARCH=arm64|x86_64] bash scripts/build_macos_app.sh [VERSION]
#   VERSION defaults to APP_VERSION in viosc.py. ARCH defaults to arm64.
#   MACOS_PYTHON overrides the interpreter (a universal2 python.org framework
#   is required for an arm64 cross-build on an Intel runner).
# Artifacts:
#   dist/viOSC.app
#   dist/viOSC-<VERSION>-<ARCH>.dmg
#
# The app bundles CPython + Tk + python-osc + the viOSC sources + a pinned
# static arm64 ffmpeg/ffprobe (GPL; licence/notice included). It is UNSIGNED
# (D2): the README documents the first-launch quarantine step.
set -euo pipefail

if [ "$(uname -s)" != "Darwin" ]; then
    echo "ERROR: build_macos_app.sh must run on macOS." >&2
    exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${1:-$(grep -o 'APP_VERSION: str = "[^"]*"' "$ROOT/viosc.py" | cut -d'"' -f2)}"
ARCH="${ARCH:-arm64}"
case "$ARCH" in
    arm64|x86_64) ;;
    *) echo "ERROR: ARCH must be arm64 or x86_64 (got '$ARCH')." >&2; exit 1 ;;
esac
MACOS_PYTHON="${MACOS_PYTHON:-python3}"
VENV="${MACOS_VENV:-$ROOT/.venv-macos-$ARCH}"
PY="$VENV/bin/python"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

FFMPEG_BASE="https://github.com/eugeneware/ffmpeg-static/releases/download/b6.1.1"
case "$ARCH" in
    arm64)
        FFMPEG_ASSET="darwin-arm64"
        FFMPEG_SHA="a90e3db6a3fd35f6074b013f948b1aa45b31c6375489d39e572bea3f18336584"
        FFPROBE_SHA="bb2db6f5d8cef919da12fbf592119a987202a8c060a886f3cab091f9cab90b64"
        LICENSE_SHA="cb48bf09a11f5fb576cddb0431c8f5ed0a60157a9ec942adffc13907cbe083f2"
        ;;
    x86_64)
        FFMPEG_ASSET="darwin-x64"
        FFMPEG_SHA="ebdddc936f61e14049a2d4b549a412b8a40deeff6540e58a9f2a2da9e6b18894"
        FFPROBE_SHA="fa3add0ce901f7241abe0dfc0155d958fc834aca3f8ce61f87cc712ae669c1e0"
        LICENSE_SHA="2e1d16c72fd74e12063776371da757322f8b77589386532f4fd8634bde7de1af"
        ;;
esac

echo "==> viOSC macOS build $VERSION ($ARCH)"
if [ "$ARCH" = "arm64" ] && [ "$(uname -m)" != "arm64" ]; then
    echo "    NOTE: cross-building arm64 on $(uname -m); MACOS_PYTHON must be universal2."
fi
echo "==> build venv: $VENV"
if [ ! -x "$PY" ]; then
    "$MACOS_PYTHON" -m venv "$VENV"
fi
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -r "$ROOT/requirements.txt" "pyinstaller>=6.14"

echo "==> generating the icon"
"$PY" "$ROOT/scripts/make_icon.py" "$WORK/icon-1024.png" 1024
ICONSET="$WORK/viOSC.iconset"
mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
    sips -z "$size" "$size" "$WORK/icon-1024.png" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
    double=$((size * 2))
    sips -z "$double" "$double" "$WORK/icon-1024.png" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$WORK/viOSC.icns"

echo "==> running PyInstaller"
cd "$ROOT"
rm -rf build dist/viOSC.app "dist/viOSC-${VERSION}-${ARCH}.dmg"
"$PY" -m PyInstaller \
    --noconfirm --clean --windowed --onedir \
    --name viOSC \
    --osx-bundle-identifier org.viosc.viosc \
    --target-arch "$ARCH" \
    --icon "$WORK/viOSC.icns" \
    --hidden-import viosc_gui \
    --hidden-import tkinter \
    --collect-all tkinter \
    --paths "$ROOT" \
    --add-data "$ROOT/LICENSE:." \
    --add-data "$ROOT/THIRD_PARTY_LICENSES.md:." \
    "$ROOT/viosc.py"

APP="$ROOT/dist/viOSC.app"
if [ ! -d "$APP" ]; then
    echo "ERROR: PyInstaller did not produce $APP" >&2
    exit 1
fi

echo "==> bundling the pinned static ffmpeg/ffprobe"
BIN="$APP/Contents/Resources/bin"
mkdir -p "$BIN"
fetch() { # fetch <url> <dest> <sha256>
    if [ ! -s "$2" ]; then
        curl -fL --retry 3 -o "$2.part" "$1"
        mv "$2.part" "$2"
    fi
    echo "$3  $2" | shasum -a 256 -c - >/dev/null
}
fetch "$FFMPEG_BASE/ffmpeg-$FFMPEG_ASSET" "$WORK/ffmpeg" "$FFMPEG_SHA"
fetch "$FFMPEG_BASE/ffprobe-$FFMPEG_ASSET" "$WORK/ffprobe" "$FFPROBE_SHA"
fetch "$FFMPEG_BASE/$FFMPEG_ASSET.LICENSE" "$WORK/ffmpeg-LICENSE" "$LICENSE_SHA"
cp "$WORK/ffmpeg" "$WORK/ffprobe" "$BIN/"
chmod +x "$BIN/ffmpeg" "$BIN/ffprobe"
cp "$WORK/ffmpeg-LICENSE" "$APP/Contents/Resources/ffmpeg-LICENSE"
cp "$ROOT/THIRD_PARTY_LICENSES.md" "$APP/Contents/Resources/"
cp "$ROOT/LICENSE" "$APP/Contents/Resources/"
if ! file "$BIN/ffmpeg" | grep -q "Mach-O 64-bit executable $ARCH"; then
    echo "ERROR: bundled ffmpeg is not $ARCH: $(file "$BIN/ffmpeg")" >&2
    exit 1
fi
if otool -L "$BIN/ffmpeg" | grep -Eq '/opt/homebrew|/usr/local'; then
    echo "ERROR: bundled ffmpeg references Homebrew libraries (not static)." >&2
    otool -L "$BIN/ffmpeg" >&2
    exit 1
fi

echo "==> writing Info.plist"
"$PY" - "$APP/Contents/Info.plist" "$VERSION" <<'PYEOF'
import plistlib
import sys

path, version = sys.argv[1], sys.argv[2]
with open(path, "rb") as handle:
    data = plistlib.load(handle)
data.update(
    {
        "CFBundleIdentifier": "org.viosc.viosc",
        "CFBundleName": "viOSC",
        "CFBundleDisplayName": "viOSC",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "LSMinimumSystemVersion": "13.0",
        "LSApplicationCategoryType": "public.app-category.video",
        "NSHighResolutionCapable": True,
    }
)
with open(path, "wb") as handle:
    plistlib.dump(data, handle)
PYEOF

echo "==> creating the DMG"
DMG="$ROOT/dist/viOSC-${VERSION}-${ARCH}.dmg"
hdiutil create -volname "viOSC" -srcfolder "$APP" -ov -format UDZO "$DMG" >/dev/null
shasum -a 256 "$DMG" | tee "$DMG.sha256"
echo "==> built $APP"
echo "==> built $DMG"
echo "    unsigned (D2): see README macOS install for the quarantine step."
