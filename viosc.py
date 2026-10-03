import argparse
import contextlib
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.parse import unquote, urlparse

try:
    from pythonosc.dispatcher import Dispatcher
    from pythonosc.osc_server import BlockingOSCUDPServer
    from pythonosc.udp_client import SimpleUDPClient
except ImportError:
    print("ERROR: the 'python-osc' library is not installed.")
    print("Install it with: pip install python-osc")
    sys.exit(1)

import config
import fs_api
import mix_schema
import pairing
from logbus import bus as log_bus
from logbus import console_listener

# Single version source for releases; the AppImage build script reads this.
APP_VERSION: str = "0.7.0"

LISTEN_IP = "0.0.0.0"
LISTEN_PORT = 6666
LOCAL_BIND_IP = "127.0.0.1"
FROMVIMIX_PORT = 7001

# e41s01: explicit receive buffer for every UDP socket the daemon binds.
# socketserver inherits the kernel default (208 KiB on Linux), which is small for
# the bursts this daemon receives: one state broadcast plus the monitor/watch
# replies. 256 KiB is the named, reviewable value; the kernel caps it at
# net.core.rmem_max and contextlib.suppress keeps a refusing platform harmless.
RECV_BUFFER_BYTES = 262144

UI_IP = os.environ.get("VIOSC_UI_IP", "127.0.0.1")
REPLY_PORT = 6667

TOVIMIX_IP = "127.0.0.1"
TOVIMIX_PORT = 7000

FFMPEG_PATH = os.environ.get("VIOSC_FFMPEG", "ffmpeg")
FFPROBE_PATH = os.environ.get("VIOSC_FFPROBE", "ffprobe")

LOG_LEVEL = 1

sync_interval_time = 2000

# e42s01: LINK PAIRING. With pairing enabled (the default) a peer must present
# the code shown at start before its OSC input (including the /vimix forward) or
# its :8686 HTTP requests are accepted. The code lives in memory only and
# rotates on every boot; PAIRING_TRUSTED_PEERS bypasses pairing for known hosts.
PAIRING_ENABLED = True
PAIRING_CODE_LENGTH = 4
PAIRING_LEASE_SECONDS = 3600
PAIRING_TRUSTED_PEERS: list[str] = []
PAIRING_CODE = ""
PAIRING_LOCK_AFTER = 5
PAIRING_LOCK_SECONDS = 60
PAIRING_GLOBAL_LOCK_AFTER = 20
# e43s01: the read-only /fs media browser on machine A (see fs_api.py).
FS_ROOTS: list[str] = ["~"]
FS_SHOW_HIDDEN = False
FS_PAGE_SIZE = 500
FS_SESSIONS_DIR = "~/vimix-sessions"
PEER_REGISTRY = pairing.PeerRegistry(PAIRING_LEASE_SECONDS, PAIRING_TRUSTED_PEERS)
PAIRING_GATE = pairing.PairingGate(
    False, "", PEER_REGISTRY, PAIRING_LEASE_SECONDS
)  # import default keeps the pre-e42 behaviour; boot() arms the real gate

ALL_PROPERTIES = [
    "index",
    "name",
    "lock",
    "failed",
    "play",
    "pause",
    "blending",
    "alpha",
    "transparency",
    "depth",
    "position",
    "size",
    "corner",
    "angle",
    "seek",
    "speed",
    "brightness",
    "contrast",
    "saturation",
    "hue",
    "threshold",
    "gamma",
    "color",
    "posterize",
    "invert",
    "uri",
]

SUPPORTED_PROPERTIES = set(ALL_PROPERTIES)

COLOR_RESET = "\033[0m"
COLOR_THUMB = "\033[96m"
COLOR_RESET_EV = "\033[91m"
COLOR_TIMESTAMP = "\033[90m"

forward_client = SimpleUDPClient(TOVIMIX_IP, TOVIMIX_PORT)
ui_reply_client = SimpleUDPClient(UI_IP, REPLY_PORT)

vimix_data: dict[int, dict[str, Any]] = {}
source_current: int | None = None
seen_indices: set[int] = set()
sync_round = 0
prune_timer: threading.Timer | None = None
PRUNE_DELAY_SEC = 0.5
# e41s01: STATE BROADCAST COALESCING. broadcast_vimix_state() used to run on
# EVERY changed property (8 call sites), so one monitor round over N sources x
# M properties could cost up to N*M whole-table JSON serializations, each one
# taxing the consumer's UI (VJmix: 0.12-2.6 ms per push, SPIKE-perf). Changes
# inside one window now collapse into a single send: the FIRST change of a burst
# goes out immediately (leading edge, so the common single change is never
# delayed) and the rest wait for the trailing flush, which the sync loop runs.
# The payload is byte-identical — only the cadence changed.
STATE_BROADCAST_WINDOW_MS = 100
_last_state_broadcast_at = 0.0
_state_broadcast_pending = False
known_indices: set[int] = set()
monitored_sources: dict[str, list[str]] = {}
monitor_misses: dict[str, int] = {}
MONITOR_MAX_MISSES = 3
# e40s05: TARGETED WATCH LANE — a client asks for a few properties of one source
# at its own cadence and receives a targeted DELTA reply
# (/viosc/reply/<source> <prop> <value>) at its address, instead of the
# whole-table broadcast. Additive: the monitor registry and the broadcast above
# stay byte-identical for older clients.
watched_sources: dict[tuple[str, str, int], dict[str, Any]] = {}
watch_clients: dict[tuple[str, int], Any] = {}
WATCH_MAX_ENTRIES = 32
WATCH_MIN_INTERVAL_MS = 50
WATCH_MAX_INTERVAL_MS = 60000
THUMB_MAX_CONCURRENCY = 3
thumb_semaphore = threading.BoundedSemaphore(THUMB_MAX_CONCURRENCY)
# e10s02: up to 3 thumbs per media at distinct jittered anchors (~15/50/85 % of duration).
THUMB_MAX_COUNT = 3
THUMB_TARGET_ANCHORS = (0.15, 0.5, 0.85)
THUMB_JITTER = 0.05  # ±5 % of duration around each anchor

PREVIEW_IP = os.environ.get("VIOSC_PREVIEW_IP", "0.0.0.0")
PREVIEW_PORT = int(os.environ.get("VIOSC_PREVIEW_PORT", "8686"))

# e01s02: boot() applies the GUI-managed effective config (JSON > env >
# default) to the module state below. Import keeps these defaults so the
# test harness and headless runs behave exactly as before e01.


def _create_clients() -> None:
    """(Re)create both OSC clients from the current destination globals."""
    global forward_client, ui_reply_client
    forward_client = SimpleUDPClient(TOVIMIX_IP, TOVIMIX_PORT)
    ui_reply_client = SimpleUDPClient(UI_IP, REPLY_PORT)
    watch_clients.clear()  # e40s05: the watch replies follow REPLY_PORT


def _on_pairing_code_regenerated(code: str) -> None:
    """A regenerated code must reach the operator (e42s03).

    The gate calls this after the global failure threshold: update the daemon
    state so the GUI re-renders it and log it, so a headless daemon prints the
    new code too.
    """
    global PAIRING_CODE
    PAIRING_CODE = code
    log_bus.emit(
        f"{COLOR_RESET_EV}[PAIRING]{COLOR_RESET} too many failed attempts — new code {code}",
        "error",
    )


def boot(values: dict[str, Any], save_path: str | None = None) -> None:
    """Apply an effective config dict (config.effective) to the daemon state.

    Assigns every field to its module global, rebuilds the thumbnail
    semaphore (bound at boot) and recreates the OSC clients from the booted
    destinations. Call once in main() after load_effective, before starting
    the servers. ``save_path`` (the config file) makes a first-run pairing
    code persistent: boot generates and writes it once, then reuses it.
    """
    global LISTEN_IP, LISTEN_PORT, LOCAL_BIND_IP, FROMVIMIX_PORT
    global PREVIEW_IP, PREVIEW_PORT, TOVIMIX_IP, TOVIMIX_PORT
    global UI_IP, REPLY_PORT, FFMPEG_PATH, FFPROBE_PATH
    global LOG_LEVEL, sync_interval_time, PRUNE_DELAY_SEC
    global MONITOR_MAX_MISSES, THUMB_MAX_CONCURRENCY, THUMB_MAX_COUNT
    global PAIRING_ENABLED, PAIRING_CODE_LENGTH, PAIRING_LEASE_SECONDS
    global PAIRING_TRUSTED_PEERS, PAIRING_CODE
    global PEER_REGISTRY, PAIRING_GATE
    global PAIRING_LOCK_AFTER, PAIRING_LOCK_SECONDS, PAIRING_GLOBAL_LOCK_AFTER
    global FS_ROOTS, FS_SHOW_HIDDEN, FS_PAGE_SIZE, FS_SESSIONS_DIR
    LISTEN_IP = values["listen_ip"]
    LISTEN_PORT = values["listen_port"]
    LOCAL_BIND_IP = values["local_bind_ip"]
    FROMVIMIX_PORT = values["from_vimix_port"]
    PREVIEW_IP = values["preview_ip"]
    PREVIEW_PORT = values["preview_port"]
    TOVIMIX_IP = values["tovimix_ip"]
    TOVIMIX_PORT = values["tovimix_port"]
    UI_IP = values["ui_ip"]
    REPLY_PORT = values["reply_port"]
    FFMPEG_PATH = values["ffmpeg_path"]
    FFPROBE_PATH = values["ffprobe_path"]
    LOG_LEVEL = values["log_level"]
    sync_interval_time = values["sync_interval_ms"]
    PRUNE_DELAY_SEC = values["prune_delay_sec"]
    MONITOR_MAX_MISSES = values["monitor_max_misses"]
    THUMB_MAX_CONCURRENCY = values["thumb_max_concurrency"]
    THUMB_MAX_COUNT = values["thumb_max_count"]
    PAIRING_ENABLED = values["pairing_enabled"]
    PAIRING_CODE_LENGTH = values["pairing_code_length"]
    PAIRING_LEASE_SECONDS = values["pairing_lease_seconds"]
    PAIRING_TRUSTED_PEERS = list(values["pairing_trusted_peers"])
    if PAIRING_ENABLED:
        stored = values.get("pairing_code", "")
        code = pairing.persisted_code_if_valid(stored, PAIRING_CODE_LENGTH)
        if code is None:
            code = pairing.generate_code(PAIRING_CODE_LENGTH)
            if save_path is not None:
                config.save_pairing_code(save_path, code)
        PAIRING_CODE = code
    else:
        PAIRING_CODE = ""
    PEER_REGISTRY = pairing.PeerRegistry(PAIRING_LEASE_SECONDS, PAIRING_TRUSTED_PEERS)
    PAIRING_LOCK_AFTER = values["pairing_lock_after"]
    PAIRING_LOCK_SECONDS = values["pairing_lock_seconds"]
    PAIRING_GLOBAL_LOCK_AFTER = values["pairing_global_lock_after"]
    FS_ROOTS = list(values["fs_roots"])
    FS_SHOW_HIDDEN = values["fs_show_hidden"]
    FS_PAGE_SIZE = values["fs_page_size"]
    FS_SESSIONS_DIR = values["fs_sessions_dir"]
    PAIRING_GATE = pairing.PairingGate(
        PAIRING_ENABLED,
        PAIRING_CODE,
        PEER_REGISTRY,
        PAIRING_LEASE_SECONDS,
        lock_after=PAIRING_LOCK_AFTER,
        lock_seconds=PAIRING_LOCK_SECONDS,
        global_lock_after=PAIRING_GLOBAL_LOCK_AFTER,
        on_regenerate=_on_pairing_code_regenerated,
    )
    global thumb_semaphore
    thumb_semaphore = threading.BoundedSemaphore(THUMB_MAX_CONCURRENCY)
    _create_clients()


def load_boot_config(config_arg: str | None) -> tuple[dict[str, Any], dict[str, str]]:
    """Load the effective config and boot, persisting the pairing code (e58s01).

    The code is written back to the SAME file boot() resolved, so a restart
    (os.execv) re-reads the identical code instead of rotating it — the whole
    point of the persisted code. main() must go through here, never boot()
    directly, or a restart silently re-pairs. Returns (values, source markers).
    """
    cfg_path = config_arg or config.config_path()
    values, sources, warnings = config.load_effective(cfg_path)
    for warning in warnings:
        log_bus.emit(warning)
    boot(values, save_path=cfg_path)
    return values, sources


def rotate_pairing_code(save_path: str | None = None) -> str:
    """Generate and persist a fresh code, updating the live gate (e58s01).

    The operator asks for a new code; the daemon generates it, persists it (so
    a restart keeps it) and updates the gate so the displayed code is the one
    authentication accepts. With pairing disabled it clears the code and is a
    no-op for persistence.
    """
    global PAIRING_CODE
    if not PAIRING_ENABLED:
        PAIRING_CODE = ""
        return ""
    code = pairing.generate_code(PAIRING_CODE_LENGTH)
    PAIRING_CODE = code
    if save_path is not None:
        config.save_pairing_code(save_path, code)
    PAIRING_GATE.set_code(code)
    return code


def _apply_live_field(key: str, value: Any) -> None:
    """Set the daemon global for one live field and rebuild what depends on it."""
    attr = config.daemon_attr(key)
    globals()[attr] = value
    if key in ("ui_ip", "reply_port", "tovimix_ip", "tovimix_port"):
        _create_clients()


def apply_config_changes(changes: dict[str, Any], cfg_path: str | None = None) -> dict[str, Any]:
    """Apply live fields now, persist every applied field (e58s01).

    Classifies the change set; live fields are applied to the daemon state
    immediately. EVERY applied field (live and restart/boot) is written back to
    ``cfg_path`` — a live change that vanished on the next restart would be
    surprising, and the dashboard's snapshot reads this file. Restart/boot
    fields only take effect on the next launch, but they are persisted too.
    Returns the full report:
    ``{"applied": [...], "staged": [...], "invalid": {...}, "unknown": [...]}``.
    """
    classified = config.classify_changes(changes)
    for key, value in classified["live"].items():
        _apply_live_field(key, value)
    staged = list(classified["restart"])
    persisted = {**classified["live"], **classified["restart"]}
    if cfg_path is not None and persisted:
        config.merge_save(cfg_path, persisted)
    return {
        "applied": list(classified["live"]),
        "staged": staged,
        "invalid": classified["invalid"],
        "unknown": classified["unknown"],
        "local_only": classified["local_only"],
    }


MEDIA_META_CACHE: dict[
    str, dict[str, Any] | None
] = {}  # absolute path -> probe_media_meta result (cached once)

# e57s01: the third media_kind value. A source is "other" when it has no file
# (non-media classes) or its file has no video stream at all (audio-only,
# broken): nothing can be thumbnailed and VJmix draws the source name instead.
MEDIA_KIND_OTHER = "other"

# e01s04: Vimix-activity markers for the GUI status row — stamped on every
# Vimix packet ingested from FROMVIMIX_PORT (no new OSC traffic).
last_vimix_seen: float | None = None
vimix_messages: int = 0


def create_empty_vimix_entry():
    entry = dict.fromkeys(SUPPORTED_PROPERTIES)
    entry["thumbnails"] = []
    # e57s01: a source with no uri is "other" from its first state message; a
    # media source flips to video/image when its uri answers (brief, accepted).
    entry["media_kind"] = MEDIA_KIND_OTHER
    return entry


def find_index_by_name(name_str):
    for idx, data in vimix_data.items():
        if data.get("name") == name_str:
            return idx
    return None


def clean_uri_path(uri_value):
    if not uri_value:
        return ""
    uri_str = str(uri_value).strip()
    if uri_str.startswith("file://"):
        raw_path = urlparse(uri_str).path
        clean_path = unquote(raw_path)
    else:
        clean_path = unquote(uri_str)
    return os.path.abspath(clean_path)


def get_video_duration(file_path):
    cmd = [
        FFPROBE_PATH,
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        file_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        lines = [
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip() and line.strip() != "N/A"
        ]
        for line in lines:
            try:
                val = float(line)
                if val > 0:
                    return val
            except ValueError:
                continue
        return 0.0
    except Exception:
        return 0.0


def extract_single_frame(file_path, timestamp_sec, width=320, height=180):
    cmd = [
        FFMPEG_PATH,
        "-y",
        "-ss",
        f"{timestamp_sec:.2f}",
        "-accurate_seek",
        "-i",
        file_path,
        "-vframes",
        "1",
        "-s",
        f"{width}x{height}",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            log_bus.emit(
                f"{COLOR_RESET_EV}[THUMBNAIL ERROR]{COLOR_RESET} ffmpeg failed "
                f"(exit {result.returncode}) for '{file_path}'",
                "error",
            )
            return None
        if len(result.stdout) <= 100:
            log_bus.emit(
                f"{COLOR_RESET_EV}[THUMBNAIL ERROR]{COLOR_RESET} empty frame for '{file_path}'",
                "error",
            )
            return None
        return result.stdout
    except Exception as e:
        log_bus.emit(
            f"{COLOR_RESET_EV}[THUMBNAIL ERROR]{COLOR_RESET} Unable to extract frame: {e}",
            "error",
        )
        return None


def thumb_target_times(duration, count=THUMB_MAX_COUNT):
    """Distinct jittered seek targets for a media of the given duration (seconds).

    Images (duration <= 0) yield a single target at t=0. Very short clips collapse
    to as many distinct targets as possible (min 1) so frames are never duplicated.
    """
    if duration <= 0:
        return [0.0]
    count = max(1, min(int(count), THUMB_MAX_COUNT))
    times = []
    for anchor in THUMB_TARGET_ANCHORS[:count]:
        t = anchor * duration + random.uniform(-THUMB_JITTER, THUMB_JITTER) * duration
        times.append(max(0.0, min(t, duration * 0.98)))
    seen, distinct = set(), []
    for t in times:
        key = round(t, 1)
        if key not in seen:
            seen.add(key)
            distinct.append(t)
    return distinct or [0.0]


def extract_thumbnails_from_file(file_path, count=1, width=320, height=180):
    thumbnails = []
    duration = get_video_duration(file_path)
    for timestamp_sec in thumb_target_times(duration, count):
        frame = extract_single_frame(
            file_path, timestamp_sec=timestamp_sec, width=width, height=height
        )
        if frame:
            thumbnails.append(frame)
    return thumbnails


def probe_media_meta(file_path):
    """One ffprobe JSON call per file, cached by absolute path (e38s01).

    Returns {kind, duration_s, width, height, fps, codec} or None when the
    file has no video stream or the probe fails. kind is "video" when the
    media moves (duration > 0.25 s or several frames), "image" for a still
    decoded as a single video frame.
    """
    if file_path in MEDIA_META_CACHE:
        return MEDIA_META_CACHE[file_path]
    meta = None
    try:
        cmd = [
            FFPROBE_PATH,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            file_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            streams = data.get("streams") or []
            vstream = next((s for s in streams if s.get("codec_type") == "video"), None)
            if vstream is not None:
                fmt = data.get("format") or {}
                try:
                    duration_s = float(fmt.get("duration") or 0.0)
                except (TypeError, ValueError):
                    duration_s = 0.0
                try:
                    nb_frames = int(vstream.get("nb_frames") or 0)
                except (TypeError, ValueError):
                    nb_frames = 0
                kind = "video" if (duration_s > 0.25 or nb_frames > 1) else "image"
                fps = 0.0
                rate = str(vstream.get("r_frame_rate") or "0/1")
                if "/" in rate:
                    try:
                        num, den = rate.split("/")
                        fps = float(num) / float(den) if float(den) else 0.0
                    except (ValueError, ZeroDivisionError):
                        fps = 0.0
                meta = {
                    "kind": kind,
                    "duration_s": round(duration_s, 3),
                    "width": int(vstream.get("width") or 0),
                    "height": int(vstream.get("height") or 0),
                    "fps": round(fps, 3),
                    "codec": vstream.get("codec_name") or "",
                }
    except Exception:
        meta = None
    MEDIA_META_CACHE[file_path] = meta
    return meta


def classify_media_kind(file_path) -> str:
    """probe_media_meta + one retry, as video | image | other (e57s01).

    A None probe (a readable file with no video stream: audio-only, broken)
    drops the cached None and probes once more; a second None means "other" —
    no thumbnail can be produced, and VJmix is told so.
    """
    meta = probe_media_meta(file_path)
    if meta is None:
        MEDIA_META_CACHE.pop(file_path, None)
        meta = probe_media_meta(file_path)
    return meta["kind"] if meta else MEDIA_KIND_OTHER


def publish_media_kind(idx, uri_value):
    """Classify a media source (video|image|other) and broadcast the ADDITIVE
    media_kind state key (e38s01, e57s01).

    Returns the kind, or None when the entry/uri is missing or the file is not
    present on disk (transient: keep the previous state, retried by the sync).
    """
    if idx not in vimix_data or not uri_value:
        return None
    file_path = clean_uri_path(uri_value)
    if not os.path.exists(file_path):
        return None
    kind = classify_media_kind(file_path)
    if vimix_data[idx].get("media_kind") != kind:
        vimix_data[idx]["media_kind"] = kind
        schedule_state_broadcast()
    return kind


def generate_thumbnails_worker(idx, uri_value):
    file_path = clean_uri_path(uri_value)
    if not os.path.exists(file_path):
        log_bus.emit(
            f"{COLOR_RESET_EV}[THUMBNAIL ERROR]{COLOR_RESET} File not found: '{file_path}'",
            "error",
        )
        return  # keep the previous cache (if any); new sources stay empty

    # e57s01: probe FIRST — the kind decides whether and how much to extract.
    kind = publish_media_kind(idx, file_path)
    if kind is None:
        return
    if kind == MEDIA_KIND_OTHER:
        if LOG_LEVEL >= 1:
            log_bus.emit(
                f"{COLOR_THUMB}[THUMB SKIP]{COLOR_RESET} Index {idx}: "
                f"'{os.path.basename(file_path)}' has no video stream; no thumbnails"
            )
        return

    count = THUMB_MAX_COUNT if kind == "video" else 1
    with thumb_semaphore:
        thumbnails = extract_thumbnails_from_file(file_path, count=count)
    if idx in vimix_data:
        if thumbnails:
            vimix_data[idx]["thumbnails"] = thumbnails
            if LOG_LEVEL >= 1:
                log_bus.emit(
                    f"{COLOR_THUMB}[THUMB READY]{COLOR_RESET} Index {idx}: generated "
                    f"{len(thumbnails)} random thumbnail(s) for '{os.path.basename(file_path)}'"
                )
        else:
            log_bus.emit(
                f"{COLOR_RESET_EV}[THUMBNAIL ERROR]{COLOR_RESET} no frames extracted "
                f"from '{file_path}'",
                "error",
            )
            # keep the previous cache; a failed run must not wipe a good thumbnail


def state_payload() -> dict[str, Any]:
    """The state table payload — ONE serializer, TWO transports (e41s04).

    The /viosc/replydata broadcast and the HTTP ``/state`` resource both build
    their body here, so a field can never reach one transport and not the other.
    Thumbnails are excluded: they are resources of their own (e41s03), and
    inlining the JPEG bytes would put megabytes in every state message.
    """
    safe_data = {
        idx: {k: v for k, v in data.items() if k != "thumbnails"}
        for idx, data in vimix_data.items()
    }
    return {
        "current_source": source_current,
        "sources": safe_data,
        "monitored": dict(monitored_sources),
    }


def state_json() -> str:
    """The state table as the exact JSON text both transports send (e41s04)."""
    return json.dumps(state_payload())


def broadcast_vimix_state():
    if not _ui_allowed():
        return
    try:
        ui_reply_client.send_message("/viosc/replydata", [state_json()])
    except Exception as e:
        log_bus.emit(f"Broadcast error: {e}", "error")


def _ui_allowed() -> bool:
    """True when the configured UI peer may receive replies (e42s01)."""
    return PAIRING_GATE.is_bound(UI_IP)


def schedule_state_broadcast(now: float | None = None) -> bool:
    """Send the state broadcast now, or mark it pending inside the window (e41s01).

    Leading edge: the first change after a quiet window goes out immediately, so
    an isolated change (the common case) is never delayed. A change arriving
    inside the window only sets the pending flag; ``flush_state_broadcast()``
    delivers it. Returns True when a message went out now.
    """
    global _last_state_broadcast_at, _state_broadcast_pending
    clock = time.time() if now is None else now
    if clock - _last_state_broadcast_at >= STATE_BROADCAST_WINDOW_MS / 1000.0:
        _last_state_broadcast_at = clock
        _state_broadcast_pending = False
        broadcast_vimix_state()
        return True
    _state_broadcast_pending = True
    return False


def flush_state_broadcast(now: float | None = None) -> bool:
    """Deliver the coalesced broadcast once the window has elapsed (e41s01).

    Returns True when a message went out, so the caller (the sync loop) can log
    or assert it; a second call with nothing pending is a no-op.
    """
    global _last_state_broadcast_at, _state_broadcast_pending
    if not _state_broadcast_pending:
        return False
    clock = time.time() if now is None else now
    if clock - _last_state_broadcast_at < STATE_BROADCAST_WINDOW_MS / 1000.0:
        return False
    _last_state_broadcast_at = clock
    _state_broadcast_pending = False
    broadcast_vimix_state()
    return True


def start_new_sync_round():
    global sync_round, prune_timer
    sync_round += 1
    seen_indices.clear()
    if prune_timer:
        prune_timer.cancel()
    prune_timer = threading.Timer(PRUNE_DELAY_SEC, prune_stale_sources, args=(sync_round,))
    prune_timer.daemon = True
    prune_timer.start()


def prune_stale_sources(round_id):
    global source_current
    if round_id != sync_round:
        return
    stale = [idx for idx in list(vimix_data.keys()) if idx not in seen_indices]
    if not stale:
        return
    for idx in stale:
        del vimix_data[idx]
        known_indices.discard(idx)
        if LOG_LEVEL >= 1:
            log_bus.emit(f"{COLOR_TIMESTAMP}[PRUNE]{COLOR_RESET} Source removed: index {idx}")
    if not vimix_data:
        source_current = None
    schedule_state_broadcast()


def resolve_thumbnail_blob(name, index):
    """JPEG bytes of one cached thumbnail frame, or None (e41s03).

    The HTTP data plane's resolver: `thumbnails_for` does the by-name lookup and
    the on-demand self-heal, so the HTTP and OSC transports can never disagree
    about which frames exist. None becomes a 404 (unknown name, non-media
    source, empty cache, out-of-range index).
    """
    thumbs = thumbnails_for(name)
    if thumbs is None or index < 0 or index >= len(thumbs):
        return None
    return thumbs[index]


def thumbnails_for(target_identifier):
    """The cached thumbnail frames of a source, generating on demand (e41s03).

    None for an unknown name, a source that is not in the table, or a non-media
    source (no uri). An EMPTY cache with a valid uri self-heals once (e10s01) —
    the behaviour the OSC lane always had, now shared by both transports so they
    cannot drift.
    """
    idx = (
        int(target_identifier)
        if str(target_identifier).isdigit()
        else find_index_by_name(str(target_identifier))
    )
    if idx is None or idx not in vimix_data:
        return None
    thumbnails = vimix_data[idx].get("thumbnails") or []
    if thumbnails:
        return thumbnails
    if vimix_data[idx].get("media_kind") == MEDIA_KIND_OTHER:
        return None  # e57s01: nothing can be produced, do not re-probe
    uri_value = vimix_data[idx].get("uri")
    if not uri_value:
        return None
    generate_thumbnails_worker(idx, uri_value)
    return vimix_data[idx].get("thumbnails") or None


def send_thumbnail_blob(target_identifier, thumb_arg):
    if not _ui_allowed():
        return
    idx = (
        int(target_identifier)
        if str(target_identifier).isdigit()
        else find_index_by_name(str(target_identifier))
    )
    if idx is None or idx not in vimix_data:
        return

    thumbnails = thumbnails_for(target_identifier)
    if not thumbnails:
        return

    arg_str = str(thumb_arg).lower().strip()
    if arg_str == "all":
        indices_to_send = list(range(len(thumbnails)))
    elif arg_str.isdigit() and 0 <= int(arg_str) < len(thumbnails):
        indices_to_send = [int(arg_str)]
    else:
        return

    for t_idx in indices_to_send:
        blob_data = thumbnails[t_idx]
        osc_reply_path = f"/viosc/replythumb/{target_identifier}/{t_idx}"
        ui_reply_client.send_message(osc_reply_path, blob_data)


def request_source_props(target_str, props):
    try:
        forward_client.send_message(f"/vimix/{target_str}/get", list(props))
    except Exception as e:
        log_bus.emit(f"Error sending get request for '{target_str}': {e}", "error")


def request_uri_once(name_str):
    if not name_str:
        return
    request_source_props(name_str, ["uri"])


def mark_vimix_activity(now: float | None = None) -> None:
    """Stamp the last-Vimix-contact clock and bump the ingest counter."""
    global last_vimix_seen, vimix_messages
    last_vimix_seen = time.time() if now is None else now
    vimix_messages += 1


def vimix_idle_seconds(now: float | None = None) -> float | None:
    """Seconds since the last Vimix packet, or None when Vimix never talked."""
    if last_vimix_seen is None:
        return None
    clock = time.time() if now is None else now
    return clock - last_vimix_seen


def process_dynamic_sync_request(clean_address, args):
    target = clean_address.replace("viosc/sync/", "", 1)
    req_args = [str(arg) for arg in args]
    props_to_send = (
        ALL_PROPERTIES if not req_args or "all" in [a.lower() for a in req_args] else req_args
    )
    request_source_props(target, props_to_send)


def process_monitor_command(target_identifier, args):
    name = str(target_identifier)
    if name.isdigit():
        cached_name = vimix_data.get(int(name), {}).get("name")
        if cached_name is not None:
            name = str(cached_name)
    props = [str(a) for a in args if str(a).strip()]
    if not props:
        if name in monitored_sources:
            del monitored_sources[name]
            monitor_misses.pop(name, None)
            if LOG_LEVEL >= 1:
                log_bus.emit(
                    f"{COLOR_TIMESTAMP}[MONITOR]{COLOR_RESET} stopped monitoring: '{name}'"
                )
            schedule_state_broadcast()
        return
    monitored_sources[name] = props
    monitor_misses.pop(name, None)
    if LOG_LEVEL >= 1:
        log_bus.emit(f"{COLOR_TIMESTAMP}[MONITOR]{COLOR_RESET} monitoring '{name}' -> {props}")
    request_source_props(name, props)
    schedule_state_broadcast()


def monitor_poll():
    for name in list(monitored_sources.keys()):
        props = monitored_sources.get(name)
        if props is None:
            continue
        if find_index_by_name(name) is None:
            monitor_misses[name] = monitor_misses.get(name, 0) + 1
            if monitor_misses[name] >= MONITOR_MAX_MISSES:
                del monitored_sources[name]
                del monitor_misses[name]
                if LOG_LEVEL >= 1:
                    log_bus.emit(
                        f"{COLOR_TIMESTAMP}[MONITOR]{COLOR_RESET} removed: '{name}' "
                        f"(source no longer present)"
                    )
                schedule_state_broadcast()
            continue
        monitor_misses[name] = 0
        request_source_props(name, props)


def _watch_interval(token: Any) -> int | None:
    """A legal watch cadence in ms, or None (e40s05: clamped 50..60000)."""
    try:
        ms = int(float(token))
    except (TypeError, ValueError):
        return None
    if ms < WATCH_MIN_INTERVAL_MS:
        return None
    return min(ms, WATCH_MAX_INTERVAL_MS)


def process_watch_command(client_address, target_identifier, args) -> bool:
    """Register/replace (or drop) a targeted watch for one requester (e40s05).

    `/viosc/watch/<name> <cadence_ms> <prop...>` → subscribe; without
    arguments → unsubscribe. The requester identity is the sender IP (its listen
    port is REPLY_PORT, the same the UI uses); the reply goes to
    `<sender_ip>:REPLY_PORT`. Returns True when the registry changed.
    """
    name = str(target_identifier)
    if name.isdigit():
        cached_name = vimix_data.get(int(name), {}).get("name")
        if cached_name is not None:
            name = str(cached_name)
    ip = str(client_address[0]) if client_address else UI_IP
    key = (name, ip, int(REPLY_PORT))
    tokens = [str(a) for a in args if str(a).strip()]
    if not tokens:
        if key in watched_sources:
            del watched_sources[key]
            if LOG_LEVEL >= 1:
                log_bus.emit(f"{COLOR_TIMESTAMP}[WATCH]{COLOR_RESET} stopped: '{name}' from {ip}")
            return True
        return False
    interval = _watch_interval(tokens[0])
    props = tokens[1:]
    if interval is None or not props:
        log_bus.emit(
            f"{COLOR_TIMESTAMP}[WATCH]{COLOR_RESET} ignored '{name}' from {ip}: "
            f"need <cadence_ms {WATCH_MIN_INTERVAL_MS}..{WATCH_MAX_INTERVAL_MS}> <prop...>"
        )
        return False
    if key not in watched_sources and len(watched_sources) >= WATCH_MAX_ENTRIES:
        log_bus.emit(
            f"{COLOR_TIMESTAMP}[WATCH]{COLOR_RESET} ignored '{name}' from {ip}: "
            f"at capacity ({WATCH_MAX_ENTRIES})"
        )
        return False
    watched_sources[key] = {
        "props": props,
        "interval_ms": interval,
        "last_poll": time.time(),
    }
    if LOG_LEVEL >= 1:
        log_bus.emit(
            f"{COLOR_TIMESTAMP}[WATCH]{COLOR_RESET} watching '{name}' from {ip} "
            f"every {interval} ms -> {props}"
        )
    request_source_props(name, props)  # immediate fast feedback, like the monitor
    return True


def watch_poll(now: float | None = None) -> int:
    """Poll the due watch entries, coalescing one get per source (e40s05).

    Returns how many sources were polled this round. An entry whose source no
    longer resolves is skipped (the reply simply never comes); the registry is
    cleaned by the unsubscribe command or the entry cap.
    """
    now = time.time() if now is None else now
    due: dict[str, set[str]] = {}
    for (name, _ip, _port), entry in watched_sources.items():
        interval_s = max(1, int(entry["interval_ms"])) / 1000.0
        if now - float(entry.get("last_poll", 0.0)) < interval_s:
            continue
        entry["last_poll"] = now
        due.setdefault(name, set()).update(str(p) for p in entry["props"])
    for name, props in due.items():
        if find_index_by_name(name) is not None:
            request_source_props(name, sorted(props))
    return len(due)


def _watch_client(ip: str) -> Any:
    """The cached reply client of one requester IP (e40s05)."""
    key = (str(ip), int(REPLY_PORT))
    client = watch_clients.get(key)
    if client is None:
        client = SimpleUDPClient(key[0], key[1])
        watch_clients[key] = client
    return client


def notify_watchers(idx: int, prop: str, value: Any) -> int:
    """Send the targeted delta to every watcher of a source (e40s05).

    Returns how many replies were sent. Only the CHANGED property is sent, only
    to the requesters that asked for it: the fast lane stays O(watchers of that
    prop), never a whole-table broadcast.
    """
    if not watched_sources:
        return 0
    name = vimix_data.get(idx, {}).get("name")
    if not name:
        return 0
    sent = 0
    for (watched_name, ip, _port), entry in list(watched_sources.items()):
        if watched_name != name or prop not in entry["props"]:
            continue
        try:
            _watch_client(ip).send_message(f"/viosc/reply/{name}", [prop, value])
            sent += 1
        except Exception as e:
            log_bus.emit(
                f"{COLOR_TIMESTAMP}[WATCH]{COLOR_RESET} reply to {ip} failed: {e}", "error"
            )
    return sent


def process_vimix_current_message(address, args):
    global source_current
    match = re.match(r"^/?vimix/current/(\d+)$", address)
    if match:
        idx = int(match.group(1))
        if idx == 0:
            start_new_sync_round()
        seen_indices.add(idx)
        val = args[0] if args else None
        if val in (1, 1.0, "1"):
            if source_current != idx:
                source_current = idx
                return True, idx, True
            return True, idx, False
    return False, None, False


def process_vimix_message(address, args):
    match = re.match(r"^/?vimix/([^/]+)/(.+)$", address)
    if match:
        target_str, prop_name = match.group(1), match.group(2)
        if prop_name not in SUPPORTED_PROPERTIES:
            return None

        idx = int(target_str) if target_str.isdigit() else find_index_by_name(target_str)
        if idx is None:
            return None
        val = args[0] if len(args) == 1 else list(args) if len(args) > 1 else None

        if idx not in vimix_data:
            vimix_data[idx] = create_empty_vimix_entry()

        if prop_name == "name":
            current_name = vimix_data[idx].get("name")
            if current_name is None:
                if idx not in known_indices and val:
                    known_indices.add(idx)
                    request_uri_once(str(val))
            elif current_name != val:
                if LOG_LEVEL >= 1:
                    log_bus.emit(
                        f"{COLOR_TIMESTAMP}[NAME]{COLOR_RESET} Source {idx}: "
                        f"'{current_name}' -> '{val}'"
                    )
                if val:
                    request_uri_once(str(val))

        current_val = vimix_data[idx].get(prop_name)
        is_changed = current_val != val
        vimix_data[idx][prop_name] = val
        if is_changed:
            notify_watchers(idx, prop_name, val)  # e40s05: the targeted fast lane

        if is_changed and LOG_LEVEL >= 1:
            log_bus.emit(
                f"{COLOR_TIMESTAMP}[VIMIX DATA]{COLOR_RESET} Source {idx} | {prop_name}: {val}"
            )

        if prop_name == "uri" and is_changed and val:
            threading.Thread(
                target=generate_thumbnails_worker, args=(idx, val), daemon=True
            ).start()

        return idx, prop_name, val, is_changed
    return None


def create_osc_handler(server_port):
    def osc_handler(client_address, address, *args):
        clean_address = address.lstrip("/")
        peer_ip = (
            client_address[0]
            if isinstance(client_address, (tuple, list)) and client_address
            else str(client_address)
        )

        # e42s01: the authenticator is always accepted; every other message from
        # an unbound peer is dropped BEFORE it can reach vimix (the forward and
        # the /viosc/* commands alike).
        if clean_address.rstrip("/") == "viosc/auth":
            code = args[0] if args else ""
            if not PAIRING_GATE.authenticate_peer(code, peer_ip) and LOG_LEVEL >= 1:
                log_bus.emit(f"{COLOR_RESET_EV}[PAIRING]{COLOR_RESET} rejected {peer_ip}", "error")
            return
        if not PAIRING_GATE.is_bound(peer_ip):
            return

        if clean_address.startswith("viosc/thumb/"):
            target_identifier = clean_address.replace("viosc/thumb/", "", 1)
            thumb_arg = args[0] if args else "all"
            send_thumbnail_blob(target_identifier, thumb_arg)
        elif clean_address.startswith("viosc/regen_thumb/"):
            target_identifier = clean_address.replace("viosc/regen_thumb/", "", 1)
            idx = (
                int(target_identifier)
                if target_identifier.isdigit()
                else find_index_by_name(target_identifier)
            )
            if idx is not None and idx in vimix_data:
                uri_val = vimix_data[idx].get("uri")
                if uri_val and vimix_data[idx].get("media_kind") != MEDIA_KIND_OTHER:
                    # e10s01: do NOT wipe the cache before regenerating — the
                    # worker replaces it only on success, so a failed regen keeps
                    # the previous good thumbnail. (e57s01: an "other" source has
                    # nothing to regenerate and is skipped.)
                    threading.Thread(
                        target=generate_thumbnails_worker, args=(idx, uri_val), daemon=True
                    ).start()
        elif clean_address.startswith("viosc/sync/"):
            process_dynamic_sync_request(clean_address, args)
        elif clean_address.startswith("viosc/monitor/"):
            target_name = clean_address.replace("viosc/monitor/", "", 1)
            process_monitor_command(target_name, args)
        elif clean_address.startswith("viosc/watch/"):
            target_name = clean_address.replace("viosc/watch/", "", 1)
            process_watch_command(client_address, target_name, args)
        elif clean_address.startswith("viosc"):
            pass
        elif server_port == FROMVIMIX_PORT and clean_address.startswith("vimix/"):
            mark_vimix_activity()
            if clean_address.startswith("vimix/current/"):
                is_valid, idx, is_changed = process_vimix_current_message(address, args)
                if is_valid and is_changed:
                    schedule_state_broadcast()
            else:
                parsed = process_vimix_message(address, args)
                if parsed:
                    _, _, _, is_changed = parsed
                    if is_changed:
                        schedule_state_broadcast()
        elif server_port == LISTEN_PORT:
            forward_client.send_message(address, list(args))

    return osc_handler


def sync_loop():
    while True:
        with contextlib.suppress(Exception):
            forward_client.send_message("/vimix/current/sync", [])
        monitor_poll()
        watch_poll()
        flush_state_broadcast()
        time.sleep(sync_interval_time / 1000.0)


def bind_probe(ip: str, port: int) -> bool:
    """True when a UDP socket can bind (ip, port) right now."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((ip, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def warn_busy_ports() -> None:
    """Early duplicate-instance signal: OSC binds already taken at boot.

    Runs before the server threads spawn so a second viOSC instance is
    announced in the GUI log (red) instead of surfacing as silent dead
    threads or a bind race later.
    """
    busy = []
    for role, ip, port in (
        ("input", LISTEN_IP, LISTEN_PORT),
        ("vimix replies", LOCAL_BIND_IP, FROMVIMIX_PORT),
    ):
        if not bind_probe(ip, port):
            busy.append(f"{role} {ip}:{port}")
    for item in busy:
        log_bus.emit(
            f"{COLOR_RESET_EV}[STARTUP]{COLOR_RESET} port busy ({item}) — is "
            "another viOSC instance already running?",
            "error",
        )


def size_receive_buffer(sock) -> None:
    """Give a UDP receive socket an explicit buffer (e41s01).

    Best-effort on purpose: a kernel cap or a platform refusing the option must
    never stop the daemon from serving (Defensive Code, AGENTS.md).
    """
    with contextlib.suppress(OSError):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RECV_BUFFER_BYTES)


def start_server_instance(ip, port):
    dispatcher = Dispatcher()
    handler = create_osc_handler(port)
    dispatcher.set_default_handler(handler, needs_reply_address=True)
    try:
        server = BlockingOSCUDPServer((ip, port), dispatcher)
    except OSError as e:
        # A busy port (usually a second viOSC instance) must surface as a
        # readable log line, not a thread traceback — the app is UI-first.
        log_bus.emit(
            f"{COLOR_RESET_EV}[SERVER ERROR]{COLOR_RESET} cannot bind {ip}:{port} "
            f"({e}) — is another viOSC instance running?",
            "error",
        )
        return
    size_receive_buffer(server.socket)
    server.serve_forever()


def verify_dependencies():
    if shutil.which(FFMPEG_PATH) is None:
        log_bus.emit(f"ERROR: ffmpeg executable not found ('{FFMPEG_PATH}').", "error")
        log_bus.emit(
            "Install ffmpeg, or set the VIOSC_FFMPEG environment variable "
            "to the full path of the executable.",
            "error",
        )
        sys.exit(1)
    if shutil.which(FFPROBE_PATH) is None:
        log_bus.emit(f"ERROR: ffprobe executable not found ('{FFPROBE_PATH}').", "error")
        log_bus.emit(
            "Install ffprobe (part of ffmpeg), or set the VIOSC_FFPROBE "
            "environment variable to the full path of the executable.",
            "error",
        )
        sys.exit(1)
    # e02s01: resolved paths let the AppImage smoke test assert the bundled
    # binaries are the ones in use (AppRun prepends $APPDIR/usr/bin to PATH).
    log_bus.emit(f"ffmpeg: {shutil.which(FFMPEG_PATH)}")
    log_bus.emit(f"ffprobe: {shutil.which(FFPROBE_PATH)}")


def restart_command() -> list[str]:
    """argv for a same-pid re-exec (Apply & restart, e01s05).

    The window closes nothing: os.execv replaces this process with a fresh
    boot of the same script and command-line arguments, so the servers
    rebind and the GUI reopens with the saved config.
    """
    return [sys.executable, os.path.realpath(sys.argv[0]), *sys.argv[1:]]


def _report_gui_fatal() -> None:
    """GUI-mode fatal-boot dialog (console is off): show the log tail."""
    detail = "\n".join(text for text, _level in log_bus.tail(count=25))
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("viOSC startup failed", detail or "startup failed")
        root.destroy()
    except Exception:
        print(detail or "viOSC startup failed")


def _generate_fs_thumb(path: str) -> bytes | None:
    """One JPEG frame for the File Manager browser (e43s03).

    Reuses the daemon's ffmpeg extraction under the EXISTING concurrency
    semaphore, so a folder full of videos cannot fan out unbounded.
    """
    with thumb_semaphore:
        blobs = extract_thumbnails_from_file(path, count=1)
    return blobs[0] if blobs else None


FS_THUMB_CACHE = fs_api.ThumbnailCache(_generate_fs_thumb)


class _FsAccess:
    """Bind the read-only fs surface to the booted Media Roots (e43s01).

    The HTTP handler only sees these methods, so the config and the
    containment rules stay on this side of the boundary; the globals are read at
    call time, so a restart with new roots needs no rebuild of the server.
    """

    def roots(self) -> list[dict[str, Any]]:
        return fs_api.roots_info(FS_ROOTS)

    def listing(self, path: str, offset: int) -> tuple[dict[str, Any] | None, str | None]:
        return fs_api.list_directory(
            path, FS_ROOTS, show_hidden=FS_SHOW_HIDDEN, page_size=FS_PAGE_SIZE, offset=offset
        )

    def resolve_file(self, path: str) -> str | None:
        return fs_api.resolve_file(path, FS_ROOTS)

    def thumb(self, path: str) -> bytes | None:
        """The cached JPEG of a media file inside the roots, else None (e43s03)."""
        target = fs_api.resolve_file(path, FS_ROOTS)
        if target is None:
            return None
        if fs_api.classify(target) not in (fs_api.MEDIA_VIDEO, fs_api.MEDIA_IMAGE):
            return None
        return FS_THUMB_CACHE.get(target)

    def sessions(self) -> list[dict[str, Any]]:
        """The written Session Drafts on disk (e43s06)."""
        return mix_schema.list_sessions(os.path.expanduser(FS_SESSIONS_DIR))

    def write_session(
        self,
        name: str,
        files: list[str],
        *,
        overwrite: bool = False,
        alphas: dict[str, float] | None = None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Write a `.mix` draft into the configured sessions directory (e43s06).

        ``overwrite`` (e44s02) replaces the exact named file instead of suffixing;
        ``alphas`` (e45s01) encodes each source's output alpha.
        """
        return mix_schema.write_session(
            os.path.expanduser(FS_SESSIONS_DIR),
            name,
            files,
            overwrite=overwrite,
            alphas=alphas,
        )


def main():
    parser = argparse.ArgumentParser(
        prog="viosc", description="viOSC OSC router/state mirror for Vimix"
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="console only — do not open the GUI window (e01s05 default is GUI-on)",
    )
    parser.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help="config JSON (default: ~/.config/viosc/config.json)",
    )
    args = parser.parse_args()
    gui_mode = not args.headless
    if not gui_mode:
        log_bus.subscribe(console_listener, replay=True)
    values, _sources = load_boot_config(args.config)
    try:
        verify_dependencies()
    except SystemExit:
        if gui_mode:
            _report_gui_fatal()
        raise
    log_bus.emit(
        "=========================================================================================="
    )
    log_bus.emit(
        "                      MULTI-PORT OSC ROUTER 'viOSC' STARTED                           "
    )
    log_bus.emit(
        f"   input   : {LISTEN_IP}:{LISTEN_PORT}   (commands -> vimix {TOVIMIX_IP}:{TOVIMIX_PORT})"
    )
    log_bus.emit(f"   from vimix: {LOCAL_BIND_IP}:{FROMVIMIX_PORT}   (vimix default OSC replies)")
    log_bus.emit(f"   output  : {UI_IP}:{REPLY_PORT}   (state / thumbnails / monitor for the UI)")
    log_bus.emit(f"   preview : {PREVIEW_IP}:{PREVIEW_PORT}   (HTTP file/meta transport, e38s01)")
    if PAIRING_ENABLED:
        log_bus.emit(f"   pairing : code {PAIRING_CODE}   (pairing ON — enter this code in the UI)")
    log_bus.emit(
        "=========================================================================================="
    )
    warn_busy_ports()
    threading.Thread(target=sync_loop, daemon=True).start()

    threading.Thread(
        target=start_server_instance, args=(LISTEN_IP, LISTEN_PORT), daemon=True
    ).start()
    threading.Thread(
        target=start_server_instance, args=(LOCAL_BIND_IP, FROMVIMIX_PORT), daemon=True
    ).start()

    def _config_provider():
        vals, srcs, _ = config.load_effective(args.config or config.config_path())
        return {
            "values": vals,
            "sources": srcs,
            "editable": config.remote_editable_fields(),
        }

    def _apply_config(changes):
        return apply_config_changes(changes, args.config or config.config_path())

    def _restart():
        os.execv(sys.executable, restart_command())

    # e38s01: the HTTP preview surface is a separate daemon; a busy/missing
    # port degrades preview only — the OSC role keeps running. The server is
    # bound to THIS module's live state table and probe (the module runs as
    # __main__ — an import-by-name inside the server would see an empty copy).
    try:
        from preview_http import start_preview_server

        preview_server = start_preview_server(
            PREVIEW_IP,
            PREVIEW_PORT,
            vimix_data,
            probe_media_meta,
            resolve_thumbnail_blob,
            state_json,
            PAIRING_GATE,
            _FsAccess(),
            provide_config=_config_provider,
            apply_config=_apply_config,
            restart=_restart,
        )
        threading.Thread(target=preview_server.serve_forever, daemon=True).start()
        if LOG_LEVEL >= 1:
            log_bus.emit(f"[PREVIEW] HTTP server listening on {PREVIEW_IP}:{PREVIEW_PORT}")
    except OSError as e:
        log_bus.emit(
            f"{COLOR_RESET_EV}[PREVIEW ERROR]{COLOR_RESET} cannot bind {PREVIEW_IP}:{PREVIEW_PORT} "
            f"({e}) — preview degraded, OSC role continues",
            "error",
        )
    except Exception as e:
        log_bus.emit(
            f"{COLOR_RESET_EV}[PREVIEW ERROR]{COLOR_RESET} {e} — "
            f"preview degraded, OSC role continues",
            "error",
        )

    if gui_mode:
        try:
            from viosc_gui import run_gui

            run_gui(
                sys.modules[__name__],
                args.config or config.config_path(),
                _sources,
                values,
            )
            return
        except Exception as e:
            log_bus.subscribe(console_listener, replay=True)
            log_bus.emit(f"GUI unavailable ({e}) — running in console mode", "error")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log_bus.emit("\nAll servers stopped.")


if __name__ == "__main__":
    main()
