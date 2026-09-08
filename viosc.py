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
from logbus import bus as log_bus
from logbus import console_listener

# Single version source for releases; the AppImage build script reads this.
APP_VERSION: str = "0.3.0"

LISTEN_IP = "0.0.0.0"
LISTEN_PORT = 6666
LOCAL_BIND_IP = "127.0.0.1"
FROMVIMIX_PORT = 7001

UI_IP = os.environ.get("VIOSC_UI_IP", "127.0.0.1")
REPLY_PORT = 6667

TOVIMIX_IP = "127.0.0.1"
TOVIMIX_PORT = 7000

FFMPEG_PATH = os.environ.get("VIOSC_FFMPEG", "ffmpeg")
FFPROBE_PATH = os.environ.get("VIOSC_FFPROBE", "ffprobe")

LOG_LEVEL = 1

sync_interval_time = 2000

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
known_indices: set[int] = set()
monitored_sources: dict[str, list[str]] = {}
monitor_misses: dict[str, int] = {}
MONITOR_MAX_MISSES = 3
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


def boot(values: dict[str, Any]) -> None:
    """Apply an effective config dict (config.effective) to the daemon state.

    Assigns every field to its module global, rebuilds the thumbnail
    semaphore (bound at boot) and recreates the OSC clients from the booted
    destinations. Call once in main() after load_effective, before starting
    the servers.
    """
    global LISTEN_IP, LISTEN_PORT, LOCAL_BIND_IP, FROMVIMIX_PORT
    global PREVIEW_IP, PREVIEW_PORT, TOVIMIX_IP, TOVIMIX_PORT
    global UI_IP, REPLY_PORT, FFMPEG_PATH, FFPROBE_PATH
    global LOG_LEVEL, sync_interval_time, PRUNE_DELAY_SEC
    global MONITOR_MAX_MISSES, THUMB_MAX_CONCURRENCY, THUMB_MAX_COUNT
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
    global thumb_semaphore
    thumb_semaphore = threading.BoundedSemaphore(THUMB_MAX_CONCURRENCY)
    _create_clients()


MEDIA_META_CACHE: dict[
    str, dict[str, Any] | None
] = {}  # absolute path -> probe_media_meta result (cached once)

# e01s04: Vimix-activity markers for the GUI status row — stamped on every
# Vimix packet ingested from FROMVIMIX_PORT (no new OSC traffic).
last_vimix_seen: float | None = None
vimix_messages: int = 0


def create_empty_vimix_entry():
    entry = dict.fromkeys(SUPPORTED_PROPERTIES)
    entry["thumbnails"] = []
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


def publish_media_kind(idx, uri_value):
    """Classify a media source (video|image) and broadcast the ADDITIVE
    media_kind state key (e38s01). Non-media sources (no uri) and files that
    are not present on disk stay unclassified (no key, no broadcast).
    """
    if idx not in vimix_data or not uri_value:
        return
    file_path = clean_uri_path(uri_value)
    if not os.path.exists(file_path):
        return
    meta = probe_media_meta(file_path)
    if meta is None:
        return
    kind = meta["kind"]
    if vimix_data[idx].get("media_kind") != kind:
        vimix_data[idx]["media_kind"] = kind
        broadcast_vimix_state()


def generate_thumbnails_worker(idx, uri_value):
    file_path = clean_uri_path(uri_value)
    if not os.path.exists(file_path):
        log_bus.emit(
            f"{COLOR_RESET_EV}[THUMBNAIL ERROR]{COLOR_RESET} File not found: '{file_path}'",
            "error",
        )
        return  # keep the previous cache (if any); new sources stay empty

    publish_media_kind(idx, file_path)  # e38s01: media_kind rides the state feed

    with thumb_semaphore:
        thumbnails = extract_thumbnails_from_file(file_path, count=THUMB_MAX_COUNT)
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


def broadcast_vimix_state():
    safe_data = {
        idx: {k: v for k, v in data.items() if k != "thumbnails"}
        for idx, data in vimix_data.items()
    }
    payload = {
        "current_source": source_current,
        "sources": safe_data,
        "monitored": dict(monitored_sources),
    }
    try:
        ui_reply_client.send_message("/viosc/replydata", [json.dumps(payload)])
    except Exception as e:
        log_bus.emit(f"Broadcast error: {e}", "error")


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
    broadcast_vimix_state()


def send_thumbnail_blob(target_identifier, thumb_arg):
    idx = (
        int(target_identifier)
        if str(target_identifier).isdigit()
        else find_index_by_name(str(target_identifier))
    )
    if idx is None or idx not in vimix_data:
        return

    thumbnails = vimix_data[idx].get("thumbnails", [])
    if not thumbnails:
        # e10s01: an empty cache with a valid URI self-heals on demand instead of
        # silently dropping the request. Unloadable sources (no URI / file still
        # missing) keep the silent no-op.
        uri_value = vimix_data[idx].get("uri")
        if not uri_value:
            return
        generate_thumbnails_worker(idx, uri_value)
        thumbnails = vimix_data[idx].get("thumbnails", [])
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
            broadcast_vimix_state()
        return
    monitored_sources[name] = props
    monitor_misses.pop(name, None)
    if LOG_LEVEL >= 1:
        log_bus.emit(f"{COLOR_TIMESTAMP}[MONITOR]{COLOR_RESET} monitoring '{name}' -> {props}")
    request_source_props(name, props)
    broadcast_vimix_state()


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
                broadcast_vimix_state()
            continue
        monitor_misses[name] = 0
        request_source_props(name, props)


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
                if uri_val:
                    # e10s01: do NOT wipe the cache before regenerating — the
                    # worker replaces it only on success, so a failed regen keeps
                    # the previous good thumbnail.
                    threading.Thread(
                        target=generate_thumbnails_worker, args=(idx, uri_val), daemon=True
                    ).start()
        elif clean_address.startswith("viosc/sync/"):
            process_dynamic_sync_request(clean_address, args)
        elif clean_address.startswith("viosc/monitor/"):
            target_name = clean_address.replace("viosc/monitor/", "", 1)
            process_monitor_command(target_name, args)
        elif clean_address.startswith("viosc"):
            pass
        elif server_port == FROMVIMIX_PORT and clean_address.startswith("vimix/"):
            mark_vimix_activity()
            if clean_address.startswith("vimix/current/"):
                is_valid, idx, is_changed = process_vimix_current_message(address, args)
                if is_valid and is_changed:
                    broadcast_vimix_state()
            else:
                parsed = process_vimix_message(address, args)
                if parsed:
                    _, _, _, is_changed = parsed
                    if is_changed:
                        broadcast_vimix_state()
        elif server_port == LISTEN_PORT:
            forward_client.send_message(address, list(args))

    return osc_handler


def sync_loop():
    while True:
        with contextlib.suppress(Exception):
            forward_client.send_message("/vimix/current/sync", [])
        monitor_poll()
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
    values, _sources, warnings = config.load_effective(args.config)
    for warning in warnings:
        log_bus.emit(warning)
    boot(values)
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

    # e38s01: the HTTP preview surface is a separate daemon; a busy/missing
    # port degrades preview only — the OSC role keeps running. The server is
    # bound to THIS module's live state table and probe (the module runs as
    # __main__ — an import-by-name inside the server would see an empty copy).
    try:
        from preview_http import start_preview_server

        preview_server = start_preview_server(
            PREVIEW_IP, PREVIEW_PORT, vimix_data, probe_media_meta
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
