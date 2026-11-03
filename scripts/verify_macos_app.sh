#!/usr/bin/env bash
# Layout + headless smoke for the macOS .app (e04s02).
# Usage: [EXPECTED_ARCH=arm64|x86_64] [SKIP_RUNTIME=1] bash scripts/verify_macos_app.sh [dist/viOSC.app]
#   EXPECTED_ARCH defaults to arm64. SKIP_RUNTIME skips the headless launch
#   (used when the runner cannot execute the target architecture).
set -euo pipefail

if [ "$(uname -s)" != "Darwin" ]; then
    echo "ERROR: verify_macos_app.sh must run on macOS." >&2
    exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="${1:-$ROOT/dist/viOSC.app}"
EXPECTED_ARCH="${EXPECTED_ARCH:-arm64}"
case "$EXPECTED_ARCH" in
    arm64|x86_64) ;;
    *) echo "ERROR: EXPECTED_ARCH must be arm64 or x86_64." >&2; exit 1 ;;
esac
[ -d "$APP" ] || { echo "ERROR: app not found: $APP" >&2; exit 1; }

EXE="$APP/Contents/MacOS/viOSC"
[ -x "$EXE" ] || { echo "ERROR: missing executable $EXE" >&2; exit 1; }
file "$EXE" | grep -q "Mach-O 64-bit executable $EXPECTED_ARCH" || {
    echo "ERROR: app executable is not $EXPECTED_ARCH: $(file "$EXE")" >&2
    exit 1
}
file "$APP/Contents/Resources/bin/ffmpeg" | grep -q "Mach-O 64-bit executable $EXPECTED_ARCH" || {
    echo "ERROR: bundled ffmpeg is not $EXPECTED_ARCH" >&2
    exit 1
}

echo "==> layout"
plutil -p "$APP/Contents/Info.plist" | grep -E \
    'NSHighResolutionCapable|LSMinimumSystemVersion|CFBundleIdentifier|CFBundleShortVersionString' >/dev/null
find "$APP" -name "_tkinter" -o -name "_tkinter*.so" | grep -q . || {
    echo "ERROR: _tkinter is missing from the bundle" >&2
    exit 1
}
[ -x "$APP/Contents/Resources/bin/ffmpeg" ] || { echo "ERROR: bundled ffmpeg missing" >&2; exit 1; }
[ -x "$APP/Contents/Resources/bin/ffprobe" ] || { echo "ERROR: bundled ffprobe missing" >&2; exit 1; }
otool -L "$APP/Contents/Resources/bin/ffmpeg" | grep -Eq '/opt/homebrew|/usr/local' && {
    echo "ERROR: bundled ffmpeg is not static" >&2
    exit 1
}

echo "==> Mach-O architecture"
if find "$APP" -type f -exec file {} + | grep "Mach-O" | grep -v "$EXPECTED_ARCH" >/dev/null; then
    echo "ERROR: a bundled Mach-O is not $EXPECTED_ARCH:" >&2
    find "$APP" -type f -exec file {} + | grep "Mach-O" | grep -v "$EXPECTED_ARCH" >&2
    exit 1
fi

if [ "${SKIP_RUNTIME:-0}" = "1" ]; then
    echo "==> SKIP_RUNTIME=1: layout only (runtime skipped)"
    echo "==> PASS: layout, $EXPECTED_ARCH, bundled ffmpeg"
    exit 0
fi

echo "==> headless smoke (no Vimix required)"
CFG="$(mktemp -t viosc-cfg)"
LOG="$(mktemp -t viosc-log)"
PORTS="$(python3 - <<'PY'
import socket
socks = []
for _ in range(4):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    socks.append(sock)
print(" ".join(str(sock.getsockname()[1]) for sock in socks))
for sock in socks:
    sock.close()
PY
)"
set -- $PORTS
LISTEN_PORT="$1"; FROMVIMIX_PORT="$2"; PREVIEW_PORT="$3"; REPLY_PORT="$4"
python3 - "$CFG" "$LISTEN_PORT" "$FROMVIMIX_PORT" "$PREVIEW_PORT" "$REPLY_PORT" <<'PY'
import json
import sys

path, listen, from_vimix, preview, reply = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
with open(path, "w", encoding="utf-8") as handle:
    json.dump(
        {
            "listen_port": listen,
            "from_vimix_port": from_vimix,
            "preview_ip": "127.0.0.1",
            "preview_port": preview,
            "ui_ip": "127.0.0.1",
            "reply_port": reply,
            "pairing_enabled": False,
            "log_level": 1,
        },
        handle,
    )
PY
"$EXE" --headless --config "$CFG" >"$LOG" 2>&1 &
PID=$!
cleanup() {
    kill "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
    rm -f "$CFG" "$LOG"
}
trap cleanup EXIT

status="000"
for _ in $(seq 1 40); do
    status="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PREVIEW_PORT/state" || true)"
    [ "$status" = "200" ] && break
    sleep 0.25
done
if [ "$status" != "200" ]; then
    echo "ERROR: /state answered $status" >&2
    tail -30 "$LOG" >&2
    exit 1
fi
if [ -s "$LOG" ] && ! grep -q "Contents/Resources/bin/ffmpeg" "$LOG"; then
    echo "WARNING: the headless log did not show the bundled ffmpeg path." >&2
    echo "         (a --windowed bundle may have no stdout; the layout check covers it.)" >&2
fi
echo "==> PASS: layout, $EXPECTED_ARCH, bundled ffmpeg, /state 200"
