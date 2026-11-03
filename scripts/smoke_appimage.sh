#!/usr/bin/env bash
# Smoke-test the viosc AppImage artifact (e02s03): boot it headless with a
# temp config on alternate ports and assert that
#   - the daemon keeps running until the timeout kills it (exit 124),
#   - the banner appears,
#   - the resolved ffmpeg/ffprobe point INSIDE the AppImage mount
#     (the bundled static binaries are the ones in use),
#   - no traceback / [SERVER ERROR] / [STARTUP] / ERROR line is printed.
#
# Usage: [ARCH=x86_64|aarch64] bash scripts/smoke_appimage.sh [VERSION]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${1:-$(grep -o 'APP_VERSION: str = "[^"]*"' "$ROOT/viosc.py" | cut -d'"' -f2)}"
ARCH="${ARCH:-x86_64}"
case "$ARCH" in
  x86_64|aarch64) ;;
  *) echo "ERROR: ARCH must be x86_64 or aarch64 (got '$ARCH')." >&2; exit 1 ;;
esac
APP="$ROOT/dist/viosc-$VERSION-$ARCH.AppImage"

if [ ! -x "$APP" ]; then
  echo "missing $APP — run ARCH=$ARCH bash scripts/build_appimage.sh first"
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
cat > "$TMP/config.json" <<'EOF'
{"listen_port": 8266, "from_vimix_port": 8201, "preview_port": 8286}
EOF

set +e
# GitHub Ubuntu runners have no libfuse2: extract and run this one process.
APPIMAGE_EXTRACT_AND_RUN=1 timeout 6 "$APP" --headless --config "$TMP/config.json" \
  > "$TMP/out.log" 2>&1
RC=$?
set -e

fail() { echo "SMOKE FAIL: $1"; sed -n '1,40p' "$TMP/out.log"; exit 1; }
[ "$RC" -eq 124 ] || fail "expected the daemon to keep running until timeout (exit 124), got $RC"
grep -q "'viOSC' STARTED" "$TMP/out.log" || fail "startup banner missing"
grep -Eq "ffmpeg: /[^ ]*/usr/bin/ffmpeg$" "$TMP/out.log" || fail "resolved ffmpeg is not the bundled binary"
grep -Eq "ffprobe: /[^ ]*/usr/bin/ffprobe$" "$TMP/out.log" || fail "resolved ffprobe is not the bundled binary"
grep -qE "Traceback|\[SERVER ERROR\]|\[STARTUP\]|ERROR:" "$TMP/out.log" && fail "unexpected error output"
echo "SMOKE PASS: viosc-$VERSION ($ARCH) AppImage boots headless with the bundled ffmpeg/ffprobe"
echo "  ffmpeg : $(grep 'ffmpeg:' "$TMP/out.log" | head -1)"
echo "  ffprobe: $(grep 'ffprobe:' "$TMP/out.log" | head -1)"
