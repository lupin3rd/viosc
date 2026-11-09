#!/usr/bin/env bash
# Verify the viosc AppImage artifact (e02s03): build if missing, run the
# smoke test, then extract the artifact and assert the layout — bundled
# sources, bundled static ffmpeg/ffprobe, PATH-prepending AppRun, desktop
# entry and icon.
#
# Usage: [ARCH=x86_64|aarch64] bash scripts/verify_appimage.sh [VERSION]
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
  echo "==> artifact missing — building"
  ARCH="$ARCH" bash "$ROOT/scripts/build_appimage.sh" "$VERSION"
fi

ARCH="$ARCH" bash "$ROOT/scripts/smoke_appimage.sh" "$VERSION"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
echo "==> extracting artifact layout"
( cd "$WORK" && "$APP" --appimage-extract >/dev/null 2>&1 )
DIR="$WORK/squashfs-root"

fail() { echo "VERIFY FAIL: $1"; exit 1; }
for module in viosc.py config.py logbus.py viosc_gui.py http_api.py fs_api.py mix_schema.py pairing.py; do
  [ -f "$DIR/opt/viosc/$module" ] || fail "missing opt/viosc/$module"
done
[ -x "$DIR/usr/bin/ffmpeg" ] || fail "missing bundled usr/bin/ffmpeg"
[ -x "$DIR/usr/bin/ffprobe" ] || fail "missing bundled usr/bin/ffprobe"
grep -q 'APPDIR/usr/bin' "$DIR/AppRun" || fail "AppRun does not prepend the bundled PATH"
grep -q 'opt/viosc' "$DIR/AppRun" || fail "AppRun does not point at opt/viosc"
[ -f "$DIR/usr/share/applications/viosc.desktop" ] || fail "missing desktop entry"
[ -f "$DIR/usr/share/icons/hicolor/256x256/apps/viosc.png" ] || fail "missing icon"
echo "VERIFY PASS: viosc-$VERSION ($ARCH) AppImage layout + smoke OK"
