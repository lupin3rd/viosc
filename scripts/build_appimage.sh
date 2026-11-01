#!/usr/bin/env bash
# Build a self-contained viosc AppImage (e02s02). Local only — the artifact
# lands in dist/. One file carries everything: CPython 3.13 + python-osc +
# tkinter (in the base) + the viosc sources + a STATIC ffmpeg/ffprobe, so no
# Python install and no host ffmpeg are needed.
#
# Usage: bash scripts/build_appimage.sh [VERSION]
#   VERSION defaults to the APP_VERSION string in viosc.py.
#
# Pipeline (VJmix e21 recipe — extract -> pip -> repack):
#   1. python-appimage CPython 3.13 manylinux_2_28 base (tkinter proven in
#      specs/archive/spikes/SPIKE-tkinter-in-python-appimage.md).
#   2. --appimage-extract the base to an AppDir.
#   3. pip install python-osc into the AppDir site-packages.
#   4. copy viosc.py config.py logbus.py viosc_gui.py preview_http.py to
#      AppDir/opt/viosc.
#   5. static ffmpeg+ffprobe (johnvansickle build) into AppDir/usr/bin.
#   6. custom AppRun: PATH -> $APPDIR/usr/bin first (bundled ffmpeg wins),
#      PYTHONPATH -> opt/viosc, interpreter run with -s.
#   7. repack with appimagetool (type-2 runtime) -> dist/viosc-<VERSION>-x86_64.AppImage.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for tool in curl sha256sum file; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "ERROR: '$tool' is required (e.g. sudo apt-get install file)." >&2
    exit 1
  }
done
VERSION="${1:-$(grep -o 'APP_VERSION: str = "[^"]*"' "$ROOT/viosc.py" | cut -d'"' -f2)}"
BASE_FILE="python3.13.17-cp313-cp313-manylinux_2_28_x86_64.AppImage"
BASE_URL="https://github.com/niess/python-appimage/releases/download/python3.13/${BASE_FILE}"
TOOL_URL="https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage"
RUNTIME_URL="https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-x86_64"
# Static GPL ffmpeg/ffprobe for linux-x64, republished on GitHub by
# ffmpeg-static (the same pinned b6.1.1 release used by the macOS build).
# johnvansickle's site is not reliably reachable from CI runners.
FFMPEG_BASE="https://github.com/eugeneware/ffmpeg-static/releases/download/b6.1.1"
FFMPEG_SHA="e7e7fb30477f717e6f55f9180a70386c62677ef8a4d4d1a5d948f4098aa3eb99"
FFPROBE_SHA="4f231a1960d83e403d08f7971e271707bec278a9ae18e21b8b5b03186668450d"
LICENSE_SHA="8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903"

CACHE="${CACHE_DIR:-$ROOT/.appimage-cache}"
mkdir -p "$CACHE" "$ROOT/dist"

fetch() { # fetch <url> <dest>
  if [ ! -s "$2" ]; then
    # Reuse the VJmix cache when present (same tools) to save bandwidth.
    if [ -s "${VJMIX_CACHE:-$ROOT/../vjmix/.appimage-cache}/$(basename "$2")" ]; then
      cp "${VJMIX_CACHE:-$ROOT/../vjmix/.appimage-cache}/$(basename "$2")" "$2"
      echo "==> $(basename "$2") copied from the VJmix cache"
      return
    fi
    echo "==> downloading $(basename "$2")"
    curl -fsSL --retry 3 -o "$2.part" "$1"
    mv "$2.part" "$2"
  fi
}

echo "==> viosc AppImage build (version $VERSION)"
fetch "$BASE_URL" "$CACHE/$BASE_FILE"
fetch "$TOOL_URL" "$CACHE/appimagetool-x86_64.AppImage"
fetch "$RUNTIME_URL" "$CACHE/type2-runtime-x86_64"
fetch "$FFMPEG_BASE/ffmpeg-linux-x64" "$CACHE/ffmpeg-linux-x64"
fetch "$FFMPEG_BASE/ffprobe-linux-x64" "$CACHE/ffprobe-linux-x64"
fetch "$FFMPEG_BASE/linux-x64.LICENSE" "$CACHE/linux-x64.LICENSE"
echo "$FFMPEG_SHA  $CACHE/ffmpeg-linux-x64" | sha256sum -c - >/dev/null
echo "$FFPROBE_SHA  $CACHE/ffprobe-linux-x64" | sha256sum -c - >/dev/null
echo "$LICENSE_SHA  $CACHE/linux-x64.LICENSE" | sha256sum -c - >/dev/null
chmod +x "$CACHE/$BASE_FILE" "$CACHE/appimagetool-x86_64.AppImage" "$CACHE/type2-runtime-x86_64"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
APPDIR="$WORK/viosc.AppDir"

echo "==> extracting base runtime"
( cd "$WORK" && "$CACHE/$BASE_FILE" --appimage-extract >/dev/null )
mv "$WORK/squashfs-root" "$APPDIR"
# The python-appimage base ships its own AppStream file; it is not viOSC's and
# the CI appstreamcli rejects it. Remove it and skip the AppStream validation.
rm -rf "$APPDIR/usr/share/metainfo"

PY="$(ls -d "$APPDIR"/usr/bin/python3* | head -1)"
echo "==> installing runtime dependencies (interpreter: $PY)"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet "python-osc>=1.7"

echo "==> bundling viosc sources"
mkdir -p "$APPDIR/opt/viosc"
for module in viosc.py config.py logbus.py viosc_gui.py preview_http.py fs_api.py mix_schema.py pairing.py; do
  cp "$ROOT/$module" "$APPDIR/opt/viosc/"
done
cp "$ROOT/THIRD_PARTY_LICENSES.md" "$APPDIR/opt/viosc/" 2>/dev/null || true

echo "==> bundling static ffmpeg/ffprobe"
cp "$CACHE/ffmpeg-linux-x64" "$APPDIR/usr/bin/ffmpeg"
cp "$CACHE/ffprobe-linux-x64" "$APPDIR/usr/bin/ffprobe"
chmod +x "$APPDIR/usr/bin/ffmpeg" "$APPDIR/usr/bin/ffprobe"
mkdir -p "$APPDIR/usr/share/licenses"
cp "$CACHE/linux-x64.LICENSE" "$APPDIR/usr/share/licenses/ffmpeg-GPLv3.txt"

echo "==> writing AppRun + desktop entry + icon"
PY_NAME="$(basename "$PY")"
# The base AppDir's AppRun is a symlink to usr/bin/python3.13 (the runtime
# wrapper); write to a temp file and mv over the symlink so AppRun becomes a
# regular file and the wrapper stays.
cat > "$APPDIR/AppRun.tmp" <<EOF
#!/bin/sh
# viosc launcher (e02s02): bundled ffmpeg/ffprobe first on PATH, isolated
# interpreter (-s), app sources under \$APPDIR/opt/viosc.
export PATH="\$APPDIR/usr/bin:\$PATH"
export PYTHONPATH="\$APPDIR/opt/viosc\${PYTHONPATH:+:\$PYTHONPATH}"
# -u: unbuffered stdout so --headless output survives a SIGTERM (smoke).
exec "\$APPDIR/usr/bin/$PY_NAME" -s -u "\$APPDIR/opt/viosc/viosc.py" "\$@"
EOF
mv "$APPDIR/AppRun.tmp" "$APPDIR/AppRun"
chmod +x "$APPDIR/AppRun"

mkdir -p "$APPDIR/usr/share/applications" "$APPDIR/usr/share/icons/hicolor/256x256/apps"
cat > "$APPDIR/usr/share/applications/viosc.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=viOSC
Comment=OSC router / state mirror for Vimix
Exec=viosc
Icon=viosc
Categories=AudioVideo;Audio;
Terminal=false
EOF
# Minimal icon drawn with the bundled interpreter (stdlib only — PNG via zlib).
"$PY" - <<'PYEOF' > "$APPDIR/usr/share/icons/hicolor/256x256/apps/viosc.png"
import struct, zlib

S = 256
rows = bytearray()
BG = (24, 24, 26)
FG = (70, 230, 120)
for y in range(S):
    row = bytearray()
    for x in range(S):
        edge = x < 10 or x > S - 11 or y < 10 or y > S - 11
        # 'V' made of two slanted segments, thickness ~9 px
        def near(x1, y1, x2, y2, px, py, th):
            dx, dy = x2 - x1, y2 - y1
            ln = dx * dx + dy * dy
            t = 0.0 if ln == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / ln))
            qx, qy = x1 + t * dx, y1 + t * dy
            return (px - qx) ** 2 + (py - qy) ** 2 <= th * th
        on_v = near(52, 42, 122, 206, x, y, 8) or near(122, 206, 204, 42, x, y, 8)
        color = FG if (edge or on_v) else BG
        row += bytes(color)
    rows += bytes(row) + b"\x00" * 0  # filter byte added below
# RGBA rows with filter byte 0 each
raw = bytearray()
for y in range(S):
    raw.append(0)
    raw += rows[y * S * 3 : (y + 1) * S * 3]
png = b"\x89PNG\r\n\x1a\n"
png += struct.pack(">I", 13) + b"IHDR" + struct.pack(">IIBBBBB", S, S, 8, 2, 0, 0, 0)
png += struct.pack(">I", zlib.crc32(b"IHDR" + struct.pack(">IIBBBBB", S, S, 8, 2, 0, 0, 0)))
comp = zlib.compress(bytes(raw))
png += struct.pack(">I", len(comp)) + b"IDAT" + comp
png += struct.pack(">I", zlib.crc32(b"IDAT" + comp))
png += struct.pack(">I", 0) + b"IEND" + struct.pack(">I", zlib.crc32(b"IEND"))
import sys
sys.stdout.buffer.write(png)
PYEOF

echo "==> repacking with appimagetool"
OUT="$ROOT/dist/viosc-$VERSION-x86_64.AppImage"
OUT_TMP="$OUT.tmp"
TOOL="$CACHE/appimagetool-x86_64.AppImage"
RUNTIME="$CACHE/type2-runtime-x86_64"
if ! "$TOOL" --no-appstream --runtime-file "$RUNTIME" "$APPDIR" "$OUT_TMP" >/dev/null 2>&1; then
  echo "    (FUSE unavailable — retrying with --appimage-extract-and-run)"
  "$TOOL" --appimage-extract-and-run --no-appstream --runtime-file "$RUNTIME" "$APPDIR" "$OUT_TMP" >/dev/null
fi
mv "$OUT_TMP" "$OUT"
chmod +x "$OUT"
echo "==> built $OUT ($(du -h "$OUT" | cut -f1))"
