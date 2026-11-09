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
from collections import OrderedDict, deque
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
import instancelock
import mix_schema
import pairing
from logbus import bus as log_bus
from logbus import console_listener, strip_ansi

# Single version source for releases; the AppImage build script reads this.
APP_VERSION: str = "1.0.0"

LOCAL_BIND_IP = "127.0.0.1"
FROMVIMIX_PORT = 7001

# e41s01: explicit receive buffer for every UDP socket the daemon binds.
# socketserver inherits the kernel default (208 KiB on Linux), which is small for
# the bursts this daemon receives: one state broadcast plus the monitor/watch
# replies. 256 KiB is the named, reviewable value; the kernel caps it at
# net.core.rmem_max and contextlib.suppress keeps a refusing platform harmless.
RECV_BUFFER_BYTES = 262144
# e03s01: the roster bundle is ONE datagram. socketserver defaults to 8192 B,
# which truncates a 100-source bundle (~12 KB) before python-osc parses it.
# Read up to the maximum UDP payload so the daemon never cuts a Vimix bundle.
MAX_OSC_DATAGRAM_BYTES = 65535
# e03s03: Vimix unique ids are large (up to 10^18); source indices are small.
# A numeric target above this that matches no known id is an id, never an index.
MAX_SOURCE_INDEX = 4096

TOVIMIX_IP = "127.0.0.1"
TOVIMIX_PORT = 7000

FFMPEG_PATH = os.environ.get("VIOSC_FFMPEG", "ffmpeg")
FFPROBE_PATH = os.environ.get("VIOSC_FFPROBE", "ffprobe")
# e04s01: a missing ffmpeg disables thumbnails/preview, never the OSC router.
FFMPEG_AVAILABLE = True
# Finder-launched macOS apps get a minimal PATH; probe the usual package dirs.
DARWIN_BINARY_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin")

LOG_LEVEL = 1

sync_interval_time = 2000

# e82s02: THE STATE TIER — the properties a consumer RENDERS, polled with ONE `get`
# per source every second for EVERY source (no special cadence for the current one —
# user decision 2026-10-18). Vimix answers one OSC message per attribute, so every
# name here costs one message per source per second: a name earns its place only if
# a VJmix screen reads it at 1 Hz (e99s01, specs/verifications/
# AUDIT-viosc-osc-traffic.md — 162.5 msg/s at 10 sources, ~5 % density at rest).
# Measured reply: 360-632 B for a 6-32-char name, always inside IP_MTU_SIZE.
STATE_TIER_PROPS = (
    # The addressing key: a rename must reach a tile within one tick, so the 2 s
    # roster alone is not enough.
    "name",
    "alpha",
    "blending",
    "correction",
    "depth",
    # e82s08 (user request): the transport/visibility state a tile or a lever may
    # want to show without declaring an interest for it.
    "play",
    "lock",
    "speed",
    # e83: the tile's seek bar needs the position; the app dead-reckons it between
    # answers with play+speed (both above), so one get per second is enough.
    "seek",
    # NOT here, and never without a consumer (e99s01): `position`/`size`/`angle`
    # (the raw table is on demand, `Refresh all` sweeps them once), `id` (immutable,
    # already in every roster bundle) and `index` (the mirror's own dict key).
)
STATE_TIER_INTERVAL_MS = 1000
# e99s02: THE IDLE CADENCES. A daemon that keeps asking Vimix for every property
# of every source while nobody is connected buys nothing: with VJmix closed the
# 1 s tier and the 2 s roster cost 162.5 messages/s at 10 sources (measured,
# specs of e99). When a consumer HAS been seen and then went quiet for
# CONSUMER_IDLE_S, both stretch to this interval and the declared-interest polls
# stop; the first consumer request restores the active cadences and forces one
# full sweep, so nothing is stale on return.
STATE_TIER_IDLE_INTERVAL_MS = 10_000
CONSUMER_IDLE_S = 15.0
# One thread, one tick: every poll below runs on its OWN deadline, so the state
# tier (1 s) and the interest cadence (50 ms floor) fit next to the roster poll
# (sync_interval_time) without a thread per source.
POLL_TICK_MS = 50
# The per-`get` reply budget: the tier is 9 properties since e99s01 (measured
# 596-1060 B for 17 properties at 6-32-char names), so 15 keeps even the tier plus
# a handful of declared props as ONE message inside vimix's 1536-byte bundle buffer.
INTEREST_MAX_PROPS_PER_GET = 15
# The declared-interest cadence bounds (e82s03, e98s04): 50 ms is the fastest a
# mapping needs, 60 s the slowest a wildcard sweep may ask for.
INTEREST_MIN_INTERVAL_MS = 50
INTEREST_MAX_INTERVAL_MS = 60000
# e82s06: DISCOVERY ON THE EVENT. vimix answers the roster bundle only when asked,
# and /vimix/session/open produces NO bundle at all — so viOSC asks right after it
# has forwarded one, and once more 500 ms later because vimix loads the media
# asynchronously. The forward itself is untouched.
SESSION_SYNC_RETRY_MS = 500
# e03s02 (user decision D5): every /vimix/current/sync send goes through this
# gap. A skipped session immediate is replaced by its +500 ms retry, so the
# first post-session roster is never later than 500 ms and the routine burst is
# two sends instead of three. The core 2 s sync is skipped/reset the same way.
SYNC_MIN_GAP_S = 0.5
# e03s01: if a requested round produces no current/0 within this window, probe.
# 600 ms leaves the session retry (500 ms) a chance to deliver first.
SYNC_ROUND_TIMEOUT_S = 0.6
# The index probe used only when a round is lost/incomplete (macOS maxdgram,
# UDP loss, an oscpack overflow). Replies are per-index and small.
DISCOVERY_PROBE_MAX = 128
DISCOVERY_PROBE_TIMEOUT_S = 1.0
DISCOVERY_PROBE_MIN_INTERVAL_S = 5.0
# Prune policy: a round is complete when current/<i> and <i>/id agree; a big
# drop is confirmed by the probe before pruning. A source missing for K rounds
# also asks for a probe (never a blind prune).
PRUNE_MAX_MISSED_ROUNDS = 3
ROSTER_DROP_CONFIRM = 4
ROSTER_DROP_RATIO = 0.20
# Thumbnails only for the file-backed video/image types; 13 audio, 25 image
# sequence, 27 text, etc. are "other" (e03s03).
THUMBNAIL_TYPE_CODES = frozenset({10, 12})
# e03s03: one warning when Vimix answers sources but no id/type (pre-0.9.3).
VIMIX_COMPAT_TIMEOUT_S = 5.0
# The session mutations that change the roster and emit no bundle of their own.
SESSION_COMMAND_ADDRESSES = ("vimix/session/open", "vimix/session/version")


# e42s01: LINK PAIRING. With pairing enabled (the default) a peer must present
# the code shown at start before its OSC input (including the /vimix forward) or
# its :8686 HTTP requests are accepted. The code lives in memory only and
# expires after PAIRING_CODE_TTL_HOURS (BUG-2026-10-18: it used to live for ever);
# PAIRING_TRUSTED_PEERS bypasses pairing for known hosts.
PAIRING_ENABLED = True
PAIRING_CODE_LENGTH = 4
PAIRING_LEASE_SECONDS = 3600
PAIRING_TRUSTED_PEERS: list[str] = []
PAIRING_CODE = ""
# The code's lifetime and when the live one was generated. The config file is the
# authority across restarts (pairing_code_created_at).
PAIRING_CODE_TTL_HOURS = 24
PAIRING_CODE_CREATED_AT = 0.0
# Where a runtime rotation writes the code back (set by boot's save_path).
PAIRING_SAVE_PATH = ""
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
    "type",
    # e82s01: vimix answers both (ControlManager.cpp:1683, :1744) but they were
    # outside this gate, so process_vimix_message dropped the reply in silence.
    # `id` is READ-ONLY: the rename-proof key, never an OSC target (id targeting
    # is SOURCE_ID_LATEST Phase B, still parked).
    "correction",
    "id",
]

SUPPORTED_PROPERTIES = set(ALL_PROPERTIES)

COLOR_RESET = "\033[0m"
COLOR_THUMB = "\033[96m"
COLOR_RESET_EV = "\033[91m"
COLOR_TIMESTAMP = "\033[90m"


# The module-level client, so an import-time read (or a test's setattr) always
# has a target; boot() replaces it with the configured one (e98s04).
forward_client = SimpleUDPClient(TOVIMIX_IP, TOVIMIX_PORT)

vimix_data: dict[int, dict[str, Any]] = {}
source_current: int | None = None
seen_indices: set[int] = set()
sync_round = 0
prune_timer: threading.Timer | None = None
PRUNE_DELAY_SEC = 0.5
# e82s01: the mirror's revision journal. The unit is the MESSAGE python-osc
# dispatches (one property out of a vimix bundle, or one echo) — a bundle is not
# observable at the daemon. Bounded on purpose: the journal feeds the HTTP delta
# stream (e82s03) and is never the source of truth (the store is).
CHANGE_LOG_MAX = 512
# e82s01: the revision every consumer diffs against, plus the recent changes.
state_rev: int = 0
change_log: deque[dict[str, Any]] = deque(maxlen=CHANGE_LOG_MAX)
known_indices: set[int] = set()
# e82s02: what a consumer DECLARED it needs, per source:
# {name: {"props": set(), "cadence_ms": int, "last_poll": float}}. Fed by
# POST /interest (e82s03) and by the OSC watch lane while it still lives.
interest_sources: dict[str, dict[str, Any]] = {}
# The wildcard declaration (the raw window is open): every readable property of
# every source, chunked. One consumer needs it, so one global entry is enough.
# `remaining` (e82s08): None = keep sweeping, an int = the sweeps left (POST
# /interest with `once`), which is how the GUI's "Refresh all" asks ONCE.
interest_full: dict[str, Any] = {
    "active": False,
    "cadence_ms": STATE_TIER_INTERVAL_MS,
    "last_poll": 0.0,
    "remaining": None,
}
# The SSE fan-out, installed by boot() when the HTTP server starts (e82s03);
# record_change publishes every applied change here.
EVENT_HUB = None
_state_tier_last = 0.0
_last_core_sync = 0.0
# e99s02: presence. `_last_consumer_at` is stamped by every route only a consumer
# calls; 0.0 means "none seen yet", which keeps the historical cadences (a daemon
# that never saw a consumer must never be starved by this mechanism).
_last_consumer_at = 0.0
_tier_idle = False
_pending_session_sync_at = 0.0
# e03s01: roster-round tracking. `seen_current` is filled by current/<i>;
# `seen_id` by the index-addressed <i>/id that Vimix 0.9.3 puts in the same
# bundle. They are equal on a complete round. A cut exactly on a source
# boundary can still look complete, so a big drop is confirmed by the probe.
seen_current: set[int] = set()
seen_id: set[int] = set()
_roster_missed: dict[int, int] = {}
_awaiting_roster = False
_sync_sent_at = 0.0
_last_sync_sent = 0.0
_probe_active = False
_probe_started_at = 0.0
_probe_last_at = 0.0
_probe_requested: set[int] = set()
_probe_found: dict[int, str] = {}
_vimix_compat_warned = False
vimix_has_093 = False
_first_source_at = 0.0
# e03s02: one lock for the mirror and the registries that serialize it.
STATE_LOCK = threading.RLock()
THUMB_MAX_CONCURRENCY = 3
thumb_semaphore = threading.BoundedSemaphore(THUMB_MAX_CONCURRENCY)
# e10s02: up to 3 thumbs per media at distinct jittered anchors (~15/50/85 % of duration).
THUMB_MAX_COUNT = 3
THUMB_TARGET_ANCHORS = (0.15, 0.5, 0.85)
THUMB_JITTER = 0.05  # ±5 % of duration around each anchor

HTTP_IP = os.environ.get("VIOSC_HTTP_IP", "0.0.0.0")
HTTP_PORT = int(os.environ.get("VIOSC_HTTP_PORT", "8686"))
# e98s04: the HTTP listener is the ONLY surface toward VJmix, so it is no longer
# a degradable extra. A failed bind is loud and retried on this interval; the
# daemon never advertises itself as working without a link.
HTTP_BIND_RETRY_S = 5.0
_http_up = False
http_link_error = ""

# e01s02: boot() applies the GUI-managed effective config (JSON > env >
# default) to the module state below. Import keeps these defaults so the
# test harness and headless runs behave exactly as before e01.


def _create_clients() -> None:
    """(Re)create the Vimix client from the current destination globals (e98s04).

    One OSC client left: the frozen contract. Everything toward VJmix is HTTP.
    """
    global forward_client
    forward_client = SimpleUDPClient(TOVIMIX_IP, TOVIMIX_PORT)


def _on_pairing_code_regenerated(code: str) -> None:
    """A regenerated code must reach the operator (e42s03) and the disk.

    The gate calls this after the global failure threshold or when the code's TTL
    elapses. BUG-2026-10-18: it must PERSIST the new code, or a restart reloads
    the old one and silently undoes the rotation.
    """
    global PAIRING_CODE, PAIRING_CODE_CREATED_AT
    PAIRING_CODE = code
    PAIRING_CODE_CREATED_AT = time.time()
    if PAIRING_SAVE_PATH and PAIRING_ENABLED:
        with contextlib.suppress(OSError):
            config.save_pairing_code(PAIRING_SAVE_PATH, code, PAIRING_CODE_CREATED_AT)
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
    global LOCAL_BIND_IP, FROMVIMIX_PORT
    global HTTP_IP, HTTP_PORT, TOVIMIX_IP, TOVIMIX_PORT
    global FFMPEG_PATH, FFPROBE_PATH
    global LOG_LEVEL, sync_interval_time, PRUNE_DELAY_SEC
    global THUMB_MAX_CONCURRENCY, THUMB_MAX_COUNT
    global PAIRING_ENABLED, PAIRING_CODE_LENGTH, PAIRING_LEASE_SECONDS
    global PAIRING_TRUSTED_PEERS, PAIRING_CODE
    global PAIRING_CODE_TTL_HOURS, PAIRING_CODE_CREATED_AT, PAIRING_SAVE_PATH
    global PEER_REGISTRY, PAIRING_GATE
    global PAIRING_LOCK_AFTER, PAIRING_LOCK_SECONDS, PAIRING_GLOBAL_LOCK_AFTER
    global FS_ROOTS, FS_SHOW_HIDDEN, FS_PAGE_SIZE, FS_SESSIONS_DIR
    LOCAL_BIND_IP = values["local_bind_ip"]
    FROMVIMIX_PORT = values["from_vimix_port"]
    HTTP_IP = values["http_ip"]
    HTTP_PORT = values["http_port"]
    TOVIMIX_IP = values["tovimix_ip"]
    TOVIMIX_PORT = values["tovimix_port"]
    FFMPEG_PATH = values["ffmpeg_path"]
    FFPROBE_PATH = values["ffprobe_path"]
    LOG_LEVEL = values["log_level"]
    sync_interval_time = values["sync_interval_ms"]
    PRUNE_DELAY_SEC = values["prune_delay_sec"]
    THUMB_MAX_CONCURRENCY = values["thumb_max_concurrency"]
    THUMB_MAX_COUNT = values["thumb_max_count"]
    PAIRING_ENABLED = values["pairing_enabled"]
    PAIRING_CODE_LENGTH = values["pairing_code_length"]
    PAIRING_LEASE_SECONDS = values["pairing_lease_seconds"]
    PAIRING_TRUSTED_PEERS = list(values["pairing_trusted_peers"])
    PAIRING_SAVE_PATH = save_path or ""
    if PAIRING_ENABLED:
        PAIRING_CODE_TTL_HOURS = int(values.get("pairing_code_ttl_hours") or 24)
        stored = values.get("pairing_code", "")
        stored_at = float(values.get("pairing_code_created_at") or 0.0)
        now = time.time()
        code = pairing.persisted_code_if_valid(stored, PAIRING_CODE_LENGTH)
        if code is not None and not pairing.code_is_fresh(stored_at, PAIRING_CODE_TTL_HOURS, now):
            code = None  # BUG-2026-10-18: an expired (or un-stamped) code is never reused
        if code is None:
            code = pairing.generate_code(PAIRING_CODE_LENGTH)
            stored_at = now
            if save_path is not None:
                config.save_pairing_code(save_path, code, stored_at)
        PAIRING_CODE = code
        PAIRING_CODE_CREATED_AT = stored_at
    else:
        PAIRING_CODE = ""
        PAIRING_CODE_CREATED_AT = 0.0
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
        code_ttl_seconds=PAIRING_CODE_TTL_HOURS * 3600,
        code_created_at=PAIRING_CODE_CREATED_AT or None,
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
    global PAIRING_CODE, PAIRING_CODE_CREATED_AT
    if not PAIRING_ENABLED:
        PAIRING_CODE = ""
        PAIRING_CODE_CREATED_AT = 0.0
        return ""
    code = pairing.generate_code(PAIRING_CODE_LENGTH)
    PAIRING_CODE = code
    PAIRING_CODE_CREATED_AT = time.time()
    if save_path is not None:
        config.save_pairing_code(save_path, code, PAIRING_CODE_CREATED_AT)
    PAIRING_GATE.set_code(code, PAIRING_CODE_CREATED_AT)
    return code


def _apply_live_field(key: str, value: Any) -> None:
    """Set the daemon global for one live field and rebuild what depends on it."""
    attr = config.daemon_attr(key)
    globals()[attr] = value
    if key in ("tovimix_ip", "tovimix_port"):
        _create_clients()


def config_payload(path: str | None = None) -> dict[str, Any]:
    """The ``GET /config`` payload (e58s01, e98s03).

    The effective values, where each one came from, what a remote client may
    edit, and — since 1.0 — the daemon's own ``app_version``: the consumer gates
    on it before it drives anything, because a 1.0 daemon cannot announce itself
    to a client that no longer speaks the old protocol.
    """
    vals, srcs, _ = config.load_effective(path or config.config_path())
    return {
        "values": vals,
        "sources": srcs,
        "editable": config.remote_editable_fields(),
        "app_version": APP_VERSION,
    }


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


# absolute path -> probe_media_meta result. Bounded since e98s04: a long session
# over many files used to grow it for ever (one entry per file ever probed).
MEDIA_META_CACHE_MAX = 256
MEDIA_META_CACHE: OrderedDict[str, dict[str, Any] | None] = OrderedDict()

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
    with STATE_LOCK:
        for idx, data in vimix_data.items():
            if data.get("name") == name_str:
                return idx
    return None


def find_index_by_id(source_id: int) -> int | None:
    """The mirror index of a Vimix unique id (0.9.3 id targeting, e03s03)."""
    with STATE_LOCK:
        for idx, data in vimix_data.items():
            if data.get("id") == source_id:
                return idx
    return None


def _resolve_target_index(target_str: str) -> int | None:
    """Resolve a numeric target to an index or unique id, never a phantom.

    Vimix 0.9.3 accepts /vimix/<index> and /vimix/<unique-id>; it answers a
    get with the SAME target string. An existing index (or one announced by a
    current/<i> in this round, or requested by the index probe) wins, then a
    known unique id, then a name. A numeric target that matches none is dropped
    instead of creating a phantom entry.
    """
    if target_str.isdigit():
        number = int(target_str)
        if number in vimix_data or number in seen_indices:
            return number
        if _probe_active and number in _probe_requested:
            return number
        by_id = find_index_by_id(number)
        if by_id is not None:
            return by_id
        # A bare index that has not been announced yet is still a valid Vimix
        # target (a get reply can precede the roster); a huge number is an id.
        return number if number <= MAX_SOURCE_INDEX else None
    return find_index_by_name(target_str)


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
    if not FFMPEG_AVAILABLE:
        return 0.0
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
    if not FFMPEG_AVAILABLE:
        return None
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
    if not FFMPEG_AVAILABLE:
        return None
    if file_path in MEDIA_META_CACHE:
        MEDIA_META_CACHE.move_to_end(file_path)
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
    MEDIA_META_CACHE.move_to_end(file_path)
    while len(MEDIA_META_CACHE) > MEDIA_META_CACHE_MAX:
        MEDIA_META_CACHE.popitem(last=False)
    return meta


def classify_media_kind(file_path) -> str:
    """probe_media_meta + one retry, as video | image | other (e57s01).

    A None probe (a readable file with no video stream: audio-only, broken)
    drops the cached None and probes once more; a second None means "other" —
    no thumbnail can be produced, and VJmix is told so.
    """
    if not FFMPEG_AVAILABLE:
        return MEDIA_KIND_OTHER
    meta = probe_media_meta(file_path)
    if meta is None:
        MEDIA_META_CACHE.pop(file_path, None)
        meta = probe_media_meta(file_path)
    return meta["kind"] if meta else MEDIA_KIND_OTHER


def _source_type_is_thumbnailable(idx: int) -> bool:
    """False when Vimix already told us the type is not a video/image file."""
    with STATE_LOCK:
        source_type = vimix_data.get(idx, {}).get("type")
    if isinstance(source_type, bool) or not isinstance(source_type, int):
        return True  # type not known yet: let ffprobe decide
    return source_type in THUMBNAIL_TYPE_CODES


def _mark_media_kind_other(idx: int, reason: str) -> None:
    with STATE_LOCK:
        if vimix_data.get(idx, {}).get("media_kind") == MEDIA_KIND_OTHER:
            return
        vimix_data[idx]["media_kind"] = MEDIA_KIND_OTHER
        record_change(idx, "media_kind", MEDIA_KIND_OTHER)
    if LOG_LEVEL >= 1:
        log_bus.emit(f"{COLOR_THUMB}[THUMB SKIP]{COLOR_RESET} Index {idx}: {reason}; no thumbnails")


def publish_media_kind(idx, uri_value):
    """Classify a media source (video|image|other) and broadcast the ADDITIVE
    media_kind state key (e38s01, e57s01).

    Returns the kind, or None when the entry/uri is missing or the file is not
    present on disk (transient: keep the previous state, retried by the sync).
    """
    if idx not in vimix_data or not uri_value:
        return None
    if not _source_type_is_thumbnailable(idx):
        _mark_media_kind_other(idx, "source type is not a video/image file")
        return MEDIA_KIND_OTHER
    file_path = clean_uri_path(uri_value)
    if not os.path.exists(file_path):
        return None
    kind = classify_media_kind(file_path)
    with STATE_LOCK:
        if vimix_data[idx].get("media_kind") != kind:
            vimix_data[idx]["media_kind"] = kind
            record_change(idx, "media_kind", kind)
    # e83: the clip LENGTH, from the same cached probe. `seek` is a fraction of the
    # timeline, so the app's seek bar needs the duration to dead-reckon (a fraction
    # advanced by `speed` alone saturates to 100% in one second). A viOSC-local key
    # like media_kind: not an OSC property, no contract change.
    meta = probe_media_meta(file_path)
    duration = float(meta.get("duration_s") or 0.0) if meta else 0.0
    with STATE_LOCK:
        if (
            duration > 0.0
            and abs(float(vimix_data[idx].get("duration_s") or 0.0) - duration) > 0.001
        ):
            vimix_data[idx]["duration_s"] = duration
            record_change(idx, "duration_s", duration)
    return kind


def generate_thumbnails_worker(idx, uri_value):
    if not FFMPEG_AVAILABLE:
        if LOG_LEVEL >= 1:
            log_bus.emit(
                f"{COLOR_THUMB}[THUMB SKIP]{COLOR_RESET} ffmpeg is not available; "
                "thumbnails are disabled"
            )
        return
    if not _source_type_is_thumbnailable(idx):
        _mark_media_kind_other(idx, "source type is not a video/image file")
        return
    if not isinstance(uri_value, str):
        return
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
    with STATE_LOCK:
        present = idx in vimix_data
        if present and thumbnails:
            vimix_data[idx]["thumbnails"] = thumbnails
    if present:
        if thumbnails:
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


def install_event_hub(hub) -> None:
    """Install the SSE fan-out the change journal publishes to (e82s03)."""
    global EVENT_HUB
    EVENT_HUB = hub


def http_state_json() -> str:
    """``state_json`` for the HTTP resource (e82s04, e98s04).

    Since 1.0 there is no lane to arm: HTTP is the only transport toward VJmix.
    e99s02: the snapshot poll is also the clearest "a consumer is here" signal.
    """
    note_consumer_activity()
    return state_json()


def regen_thumb_for(source) -> bool:
    """Regenerate one source's cached thumbnails (POST /thumb/regen, e82s03)."""
    name = str(source or "").strip()
    if not name:
        return False
    idx = int(name) if name.isdigit() else find_index_by_name(name)
    if idx is None or idx not in vimix_data:
        return False
    uri_value = vimix_data[idx].get("uri")
    if not uri_value or vimix_data[idx].get("media_kind") == MEDIA_KIND_OTHER:
        return False
    threading.Thread(target=generate_thumbnails_worker, args=(idx, uri_value), daemon=True).start()
    return True


def apply_interest(payload) -> dict[str, Any]:
    """POST /interest: validate, apply and report per field (e82s03).

    Report shape mirrors ``POST /config``: ``ok`` (what was accepted), ``invalid``
    (a field and its offending value), ``unknown`` (a source or property viOSC does
    not know) and ``released``. An unknown PROPERTY is refused, never forwarded: it
    would vanish in vimix and the consumer could never see why.
    """
    note_consumer_activity()  # e99s02: only a consumer declares an interest
    report: dict[str, Any] = {"ok": [], "invalid": {}, "unknown": [], "released": False}
    source = str(payload.get("source") or "").strip()
    if not source:
        report["invalid"]["source"] = payload.get("source")
        return report
    raw_props = payload.get("props", [])
    if not isinstance(raw_props, list):
        report["invalid"]["props"] = raw_props
        return report
    props = [str(prop) for prop in raw_props]
    cadence = payload.get("cadence_ms", STATE_TIER_INTERVAL_MS)

    if source == "*":
        if not props:
            interest_full["active"] = False
            report["released"] = True
            return report
        interest_full.update(
            {
                "active": True,
                "cadence_ms": _clamp_interval(cadence),
                "last_poll": 0.0,
                "remaining": 1 if payload.get("once") else None,
            }
        )
        report["ok"] = ["*"]
        return report

    if find_index_by_name(source) is None:
        report["unknown"].append(source)
        return report
    unknown = [prop for prop in props if prop not in SUPPORTED_PROPERTIES]
    if unknown:
        report["unknown"].extend(unknown)
        return report
    if set_interest(source, props, cadence):
        report["ok"].append(source)
    else:
        report["released"] = True
    return report


def record_change(idx, prop, value, kind="changed") -> int:
    """Bump the mirror's revision and journal one applied change (e82s01).

    Called by every writer of ``vimix_data``: the vimix ingest, the current
    marker, the prune and the echo. ONE revision per applied message, because a
    vimix bundle arrives as independent messages and is not observable here.
    The source is the NAME (what VJmix addresses), falling back to the index.
    """
    global state_rev
    with STATE_LOCK:
        state_rev += 1
        source = vimix_data.get(idx, {}).get("name")
        event = {
            "rev": state_rev,
            "kind": kind,
            "source": str(source) if source else str(idx),
            "prop": str(prop),
            "value": value,
        }
        change_log.append(event)
        hub = EVENT_HUB
        if hub is not None:
            # Publish under the same lock: the SSE order must equal the rev order.
            hub.publish(event)
    return int(event["rev"])


def state_payload() -> dict[str, Any]:
    """The state table payload — ONE serializer, TWO transports (e41s04).

    The /viosc/replydata broadcast and the HTTP ``/state`` resource both build
    their body here, so a field can never reach one transport and not the other.
    Thumbnails are excluded: they are resources of their own (e41s03), and
    inlining the JPEG bytes would put megabytes in every state message.
    """
    with STATE_LOCK:
        safe_data = {
            idx: {k: v for k, v in data.items() if k != "thumbnails"}
            for idx, data in list(vimix_data.items())
        }
        revision = state_rev
        current = source_current
    return {
        "rev": revision,
        "current_source": current,
        "sources": safe_data,
    }


def state_json() -> str:
    """The state table as the exact JSON text both transports send (e41s04)."""
    return json.dumps(state_payload())


def start_new_sync_round():
    global sync_round, prune_timer, _awaiting_roster
    sync_round += 1
    seen_indices.clear()
    seen_current.clear()
    seen_id.clear()
    _awaiting_roster = False
    if prune_timer:
        prune_timer.cancel()
    prune_timer = threading.Timer(PRUNE_DELAY_SEC, prune_stale_sources, args=(sync_round,))
    prune_timer.daemon = True
    prune_timer.start()


def _round_is_complete() -> bool:
    """True when the roster round is safe to prune (e03s01).

    A 0.9.3 bundle sends both current/<i> and <i>/id for every source. A cut
    exactly on a source boundary can still make the two sets equal, so a large
    drop is additionally confirmed by the probe in prune_stale_sources().
    A missing id set is never treated as complete (the probe is the fallback).
    """
    if not seen_current:
        return False
    if not seen_id:
        # Vimix 0.9.3 always sends <i>/id in the same bundle. An empty id set
        # means a truncated/lost bundle (or a pre-0.9.3 Vimix); never prune on
        # it. The index probe is the safe confirmation path in both cases.
        return False
    return seen_current == seen_id


def _delete_source(idx: int) -> None:
    """Remove one source and journal the removal (caller holds STATE_LOCK)."""
    record_change(idx, "source", False, kind="removed")
    # e98s04: a declared interest dies with its source. Before the fix the poll
    # skipped an unresolvable name for ever, so a renamed or removed source kept
    # a live entry and a `get` per tick that could never answer.
    interest_sources.pop(str(vimix_data[idx].get("name") or ""), None)
    del vimix_data[idx]
    known_indices.discard(idx)
    _roster_missed.pop(idx, None)
    if LOG_LEVEL >= 1:
        log_bus.emit(f"{COLOR_TIMESTAMP}[PRUNE]{COLOR_RESET} Source removed: index {idx}")


def _note_missed_roster() -> bool:
    """Count the sources missing from an incomplete round; True at K misses."""
    trigger = False
    for idx in list(vimix_data):
        if idx in seen_current:
            _roster_missed.pop(idx, None)
            continue
        misses = _roster_missed.get(idx, 0) + 1
        _roster_missed[idx] = misses
        if misses >= PRUNE_MAX_MISSED_ROUNDS:
            trigger = True
    return trigger


def start_roster_probe(now: float | None = None) -> bool:
    """Ask every index for its identity (e03s01 discovery fallback).

    Used only when a round is lost or incomplete (macOS maxdgram, UDP loss, an
    oscpack overflow, a cut that looks complete). The replies are small
    per-index messages, so they are never subject to the one-datagram limit.
    """
    global _probe_active, _probe_started_at, _probe_last_at, _probe_requested, _probe_found
    clock = time.monotonic() if now is None else now
    with STATE_LOCK:
        if _probe_active:
            return False
        if _probe_last_at and clock - _probe_last_at < DISCOVERY_PROBE_MIN_INTERVAL_S:
            return False
        _probe_active = True
        _probe_started_at = clock
        _probe_last_at = clock
        _probe_found = {}
        _probe_requested = set(range(DISCOVERY_PROBE_MAX))
    sent = 0
    for idx in range(DISCOVERY_PROBE_MAX):
        with contextlib.suppress(Exception):
            forward_client.send_message(f"/vimix/{idx}/get", ["name", "index", "id"])
            sent += 1
    if LOG_LEVEL >= 1:
        log_bus.emit(
            f"{COLOR_TIMESTAMP}[ROSTER]{COLOR_RESET} round incomplete/lost: probing {sent} indices"
        )
    return True


def probe_poll(now: float | None = None) -> int:
    """Finalize a due roster probe and prune to its result (e03s01)."""
    global _probe_active, source_current
    clock = time.monotonic() if now is None else now
    with STATE_LOCK:
        if not _probe_active:
            return 0
        if clock - _probe_started_at < DISCOVERY_PROBE_TIMEOUT_S:
            return 0
        _probe_active = False
        found = {idx: name for idx, name in _probe_found.items() if idx in vimix_data and name}
        if not found:
            if LOG_LEVEL >= 1:
                log_bus.emit(
                    f"{COLOR_TIMESTAMP}[ROSTER]{COLOR_RESET} probe found no source; "
                    "keeping the current table"
                )
            return 0
        if max(found) >= DISCOVERY_PROBE_MAX - 1:
            log_bus.emit(
                f"{COLOR_TIMESTAMP}[ROSTER]{COLOR_RESET} probe range may be too small; not pruning",
                "error",
            )
            return 0
        for idx in list(vimix_data):
            # The probe only covers 0..DISCOVERY_PROBE_MAX-1; never delete a
            # source above that range because the probe could not see it.
            if idx >= DISCOVERY_PROBE_MAX:
                continue
            if idx not in found:
                _delete_source(idx)
        if not vimix_data:
            source_current = None
    if LOG_LEVEL >= 1:
        log_bus.emit(
            f"{COLOR_TIMESTAMP}[ROSTER]{COLOR_RESET} probe confirmed {len(found)} source(s)"
        )
    return len(found)


def prune_stale_sources(round_id):
    """Prune only a complete round; a big drop is confirmed by the probe."""
    global source_current
    need_probe = False
    reason = ""
    missing = False
    current_count = 0
    id_count = 0
    drop = 0
    total = 0
    with STATE_LOCK:
        if round_id != sync_round:
            return
        current_count = len(seen_current)
        id_count = len(seen_id)
        if not _round_is_complete():
            missing = _note_missed_roster()
            need_probe = True
            reason = "incomplete"
        else:
            stale = [idx for idx in list(vimix_data.keys()) if idx not in seen_current]
            if not stale:
                for idx in list(_roster_missed):
                    if idx in seen_current:
                        _roster_missed.pop(idx, None)
                return
            drop = len(stale)
            total = max(1, len(vimix_data))
            if drop > ROSTER_DROP_CONFIRM and drop / total > ROSTER_DROP_RATIO:
                _note_missed_roster()
                need_probe = True
                reason = "large-drop"
            else:
                for idx in stale:
                    _delete_source(idx)
                if not vimix_data:
                    source_current = None
    if need_probe:
        start_roster_probe()
        if LOG_LEVEL >= 1:
            log_bus.emit(
                f"{COLOR_TIMESTAMP}[ROSTER]{COLOR_RESET} {reason} round "
                f"(current={current_count}, id={id_count}, missing_k={missing}, "
                f"drop={drop}/{total}): no prune, probing"
            )
        return


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


def request_source_props(target_str, props):
    try:
        forward_client.send_message(f"/vimix/{target_str}/get", list(props))
    except Exception as e:
        log_bus.emit(f"Error sending get request for '{target_str}': {e}", "error")


def request_uri_once(name_str):
    """Ask vimix for a source's DISCOVERY attributes, once (e68s01).

    `uri` says whether a file backs the source; `type` says WHAT it is (the wiki's
    integer code table). vimix PUSHES neither, so the name's first sighting is the
    moment to ask — one `get` with both argument names, never a per-frame poll.
    """
    if not name_str:
        return
    request_source_props(name_str, ["uri", "type"])


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


def poll_due(last, interval_ms, now) -> bool:
    """True when a poll whose last run was ``last`` is due at ``now`` (e82s02).

    The 1e-6 tolerance keeps a 50 ms tick from missing a 1 s deadline by a float
    rounding error (1.05 + 1.0 - 2.05 is not exactly 0.0 in binary).
    """
    interval_s = max(0, int(interval_ms)) / 1000.0
    return float(now) - float(last) >= interval_s - 1e-6


def _clamp_interval(cadence_ms) -> int:
    """A legal interest cadence in ms (50..60000), the tier's 1 s on garbage."""
    try:
        ms = int(float(cadence_ms))
    except (TypeError, ValueError):
        return STATE_TIER_INTERVAL_MS
    return max(INTEREST_MIN_INTERVAL_MS, min(ms, INTEREST_MAX_INTERVAL_MS))


def set_interest(source, props, cadence_ms=STATE_TIER_INTERVAL_MS) -> bool:
    """Declare (or release, with no props) what a consumer needs (e82s02).

    Returns True when an entry exists afterwards. Only readable properties are
    kept: an unknown name would be sent to vimix and silently ignored, which is a
    bug the consumer could never see. The cadence is clamped, never fatal.
    """
    name = str(source)
    acceptable = [str(prop) for prop in props if str(prop) in SUPPORTED_PROPERTIES]
    with STATE_LOCK:
        if not acceptable:
            interest_sources.pop(name, None)
            return False
        entry = interest_sources.setdefault(
            name,
            {"props": set(), "cadence_ms": STATE_TIER_INTERVAL_MS, "last_poll": 0.0},
        )
        entry["props"].update(acceptable)
        entry["cadence_ms"] = _clamp_interval(cadence_ms)
    return True


def _interest_due_props(now, tier_due: bool = False) -> dict[str, set[str]]:
    """The props each declared source owes at ``now`` (e82s02/e03s02).

    When the tier is due, an interest whose next deadline is within one poll
    tick rides the tier's get instead of firing again a few milliseconds later.
    This is what keeps a 1 s interest from doubling the gets of that source.
    """
    due_props: dict[str, set[str]] = {}
    for name, entry in list(interest_sources.items()):
        cadence = entry.get("cadence_ms", STATE_TIER_INTERVAL_MS)
        due = poll_due(entry.get("last_poll", 0.0), cadence, now)
        if not due and tier_due:
            next_due = float(entry.get("last_poll", 0.0)) + max(0, int(cadence)) / 1000.0
            due = next_due - float(now) <= (POLL_TICK_MS + 1) / 1000.0
        if not due:
            continue
        entry["last_poll"] = now
        due_props.setdefault(name, set()).update(entry.get("props") or ())
    return due_props


def _chunks(props, size) -> list[list[str]]:
    """Split a prop set into reply-budget-sized, sorted chunks (e82s02)."""
    ordered = sorted(props)
    return [ordered[i : i + size] for i in range(0, len(ordered), size)]


def note_consumer_activity(now: float | None = None) -> None:
    """Stamp that a consumer just talked (e99s02).

    Called by the four routes only a consumer uses: `GET /state`, a new
    `/events` subscriber, `POST /interest` and `POST /link`. `now` is injectable
    for the tests, which drive the poller on a fake clock.
    """
    global _last_consumer_at
    with STATE_LOCK:
        _last_consumer_at = time.monotonic() if now is None else float(now)


def consumer_present(now: float | None = None) -> bool:
    """True when something is (or may be) consuming the mirror (e99s02).

    Absence must be EVIDENCED: a daemon that never saw a consumer answers True,
    so an unknown consumer cannot be starved. Idle requires all of: no `/events`
    subscriber, a consumer seen in the past, quiet for `CONSUMER_IDLE_S`, and no
    one-shot sweep in flight (which is a consumer's request still being served).
    """
    clock = time.monotonic() if now is None else float(now)
    hub = EVENT_HUB
    if hub is not None and hub.count() > 0:
        return True
    if _last_consumer_at <= 0.0:
        return True
    if bool(interest_full.get("active")):
        return True
    return clock - _last_consumer_at <= CONSUMER_IDLE_S


def _note_presence(clock: float) -> bool:
    """Settle the idle/active transition and return `consumer_present` (e99s02).

    On the tick that sees idle -> present, the two deadlines are cleared so the
    consumer gets ONE full tier round and ONE roster round immediately: the first
    `/state` it reads is at most one tick old, and the sources it is about to
    address are already in the roster.
    """
    global _tier_idle, _state_tier_last, _last_core_sync
    present = consumer_present(clock)
    with STATE_LOCK:
        if present and _tier_idle:
            _tier_idle = False
            _state_tier_last = 0.0
            _last_core_sync = 0.0
        elif not present:
            _tier_idle = True
    return present


def interest_poll(now=None) -> int:
    """One coalesced poll pass: state tier union declared interests (e82s02).

    ONE `get` per source per due moment (chunked only at the reply budget), so the
    app pays one message per source instead of one per subscriber. A source that no
    longer resolves is skipped: its name has nothing to ask.
    """
    global _state_tier_last
    clock = time.monotonic() if now is None else now
    present = _note_presence(clock)
    tier_ms = STATE_TIER_INTERVAL_MS if present else STATE_TIER_IDLE_INTERVAL_MS
    with STATE_LOCK:
        tier_due = poll_due(_state_tier_last, tier_ms, clock)
        if tier_due:
            _state_tier_last = clock
        # e99s02: an interest declared by a consumer that is no longer there has
        # nobody to serve; the declared entries are kept, they just do not fire.
        due_props = _interest_due_props(clock, tier_due) if present else {}
        if tier_due:
            for data in list(vimix_data.values()):
                name = data.get("name")
                if name:
                    due_props.setdefault(str(name), set()).update(STATE_TIER_PROPS)
        full = interest_full
        if full.get("active") and poll_due(
            full.get("last_poll", 0.0), full.get("cadence_ms", STATE_TIER_INTERVAL_MS), clock
        ):
            full["last_poll"] = clock
            for data in list(vimix_data.values()):
                name = data.get("name")
                if name:
                    due_props.setdefault(str(name), set()).update(ALL_PROPERTIES)
            remaining = full.get("remaining")
            if remaining is not None:
                full["remaining"] = remaining - 1
                if full["remaining"] <= 0:
                    full["active"] = False  # e82s08: a ONE-SHOT sweep ends here
        due_snapshot = {name: set(props) for name, props in due_props.items()}
    sent = 0
    for name, props in due_snapshot.items():
        if find_index_by_name(name) is None:
            continue
        for chunk in _chunks(props, INTEREST_MAX_PROPS_PER_GET):
            request_source_props(name, chunk)
            sent += 1
    return sent


def _send_sync(now: float, reason: str) -> bool:
    """Send /vimix/current/sync through the D5 minimum gap (e03s02).

    Returns True when the message went out. A skipped send is not queued here:
    the session retry / the next core deadline is the coalescing slot.
    """
    global _last_sync_sent, _awaiting_roster, _sync_sent_at
    with STATE_LOCK:
        if now - _last_sync_sent < SYNC_MIN_GAP_S:
            if LOG_LEVEL >= 2:
                log_bus.emit(f"{COLOR_TIMESTAMP}[SYNC]{COLOR_RESET} {reason} coalesced")
            return False
        with contextlib.suppress(Exception):
            forward_client.send_message("/vimix/current/sync", [])
            _last_sync_sent = now
            _awaiting_roster = True
            _sync_sent_at = now
            return True
        return False


def request_session_sync(now=None) -> None:
    """Ask vimix for the roster right after a session change (e82s06).

    One immediate `sync` (the session command itself produces nothing) and ONE
    retry at SESSION_SYNC_RETRY_MS, because the media load is asynchronous: the
    retry is what catches a session whose sources appear a beat later. Both go
    through the same 500 ms gap (D5); a skipped immediate is replaced by the
    retry.
    """
    global _pending_session_sync_at, _awaiting_roster, _sync_sent_at
    clock = time.monotonic() if now is None else now
    with STATE_LOCK:
        _send_sync(clock, "session")
        _awaiting_roster = True
        _sync_sent_at = clock
        _pending_session_sync_at = clock + SESSION_SYNC_RETRY_MS / 1000.0


def _check_vimix_compat(clock: float) -> None:
    """One non-fatal warning when id/type never arrive (Vimix < 0.9.3, e03s03)."""
    global _vimix_compat_warned
    if vimix_has_093 or _vimix_compat_warned or not _first_source_at:
        return
    if clock - _first_source_at < VIMIX_COMPAT_TIMEOUT_S:
        return
    _vimix_compat_warned = True
    log_bus.emit(
        f"{COLOR_RESET_EV}[VIMIX COMPAT]{COLOR_RESET} Vimix 0.9.3+ is required "
        "(id/type are missing); some features are degraded",
        "error",
    )


def sync_tick(now=None) -> None:
    """One due-based pass of the single poller (e82s02/e03s02).

    The old loop slept ``sync_interval_time`` and did everything at once; a small
    tick lets the 1 s state tier and a 50 ms interest live beside the roster poll.
    Every task keeps its own deadline, so the cadences never drift into each other.
    """
    global _last_core_sync, _last_monitor_poll, _pending_session_sync_at, _awaiting_roster
    clock = time.monotonic() if now is None else now
    need_probe = False
    with STATE_LOCK:
        if _pending_session_sync_at and clock >= _pending_session_sync_at:
            _pending_session_sync_at = 0.0
            _send_sync(clock, "retry")
        core_ms = sync_interval_time if _note_presence(clock) else STATE_TIER_IDLE_INTERVAL_MS
        if poll_due(_last_core_sync, core_ms, clock):
            _last_core_sync = clock
            _send_sync(clock, "core")
        if _awaiting_roster and clock - _sync_sent_at >= SYNC_ROUND_TIMEOUT_S:
            _awaiting_roster = False
            need_probe = True
    if need_probe:
        start_roster_probe(clock)
    with contextlib.suppress(Exception):
        probe_poll(clock)
    with contextlib.suppress(Exception):
        interest_poll(clock)
    with contextlib.suppress(Exception):
        interest_poll(clock)
    _check_vimix_compat(clock)


# e82s01: THE ECHO. A forwarded absolute `/vimix/<target>/<prop>` set is applied to
# the mirror as it passes through, because vimix sends no feedback on a set: our own
# writes would otherwise stay invisible until the next poll (a beat-fade emits ~100
# absolute alpha values/s). ECHO_ARITY is the wiki's argument count WITHOUT the
# optional duration — a longer list is an ANIMATED set and is skipped, because vimix
# animates to the target and the mirror must not claim a value that is not there yet.
# Rates (loom/turn/ffwd/grab/resize) are RELATIVE and are absent on purpose; so are
# the triggers (flag/replay/reset/reload) and the identity attributes (name/uri/
# type/index/id): the core sync bundle is their reconciliation.
ECHO_ADDRESS_RE = re.compile(r"^/?vimix/([^/]+)/(.+)$")
ECHO_ARITY: dict[str, int] = {
    "alpha": 1,
    "transparency": 1,
    "depth": 1,
    "angle": 1,
    "speed": 1,
    "brightness": 1,
    "contrast": 1,
    "saturation": 1,
    "hue": 1,
    "threshold": 1,
    "gamma": 1,
    "posterize": 1,
    "seek": 1,
    "blending": 1,
    "correction": 1,
    "lock": 1,
    "play": 1,
    "pause": 1,
    "position": 2,
    "size": 2,
    "color": 3,
    "corner": 8,
}


def _echo_target_index(target_str):
    """The mirror index a forwarded target names, or None (e82s01/e03s03)."""
    if target_str == "current":
        return source_current
    return _resolve_target_index(str(target_str))


def _echo_values_are_numbers(values) -> bool:
    """True for a numeric set with at least one real value and no foreign type (e82s01)."""
    if any(value is not None and not isinstance(value, (int, float)) for value in values):
        return False
    return any(isinstance(value, (int, float)) for value in values)


def _merge_echo_vector(current, values):
    """The stored vector with the non-None components replaced (nil keeps the axis)."""
    merged = (
        list(current)
        if isinstance(current, (list, tuple)) and len(current) == len(values)
        else [None] * len(values)
    )
    for i, value in enumerate(values):
        if value is not None:
            merged[i] = float(value)
    return merged


def apply_echo(address, args) -> bool:
    """Apply ONE forwarded absolute set to the mirror; True when it applied (e82s01).

    The safety rules, in order: the target must resolve to a source already in the
    mirror (a phantom is never created), the property must be readable and echoable
    at the EXACT argument count (a trailing duration means an animation), and only
    the addressed property is written — never a linked one (`alpha`->`transparency`).
    """
    match = ECHO_ADDRESS_RE.match(str(address))
    if match is None:
        return False
    target_str, prop = match.group(1), match.group(2)
    arity = ECHO_ARITY.get(prop)
    if arity is None or prop not in SUPPORTED_PROPERTIES:
        return False
    values = list(args) if args else []
    if len(values) != arity or not _echo_values_are_numbers(values):
        return False
    with STATE_LOCK:
        idx = _echo_target_index(target_str)
        if idx is None or idx not in vimix_data:
            return False
        current = vimix_data[idx].get(prop)
        new_value = float(values[0]) if arity == 1 else _merge_echo_vector(current, values)
        if new_value == current:
            return False
        vimix_data[idx][prop] = new_value
        record_change(idx, prop, new_value)
    return True


def process_vimix_current_message(address, args):
    global source_current
    match = re.match(r"^/?vimix/current/(\d+)$", address)
    if match:
        idx = int(match.group(1))
        with STATE_LOCK:
            if idx == 0:
                start_new_sync_round()
            seen_indices.add(idx)
            seen_current.add(idx)
            val = args[0] if args else None
            if val in (1, 1.0, "1"):
                if source_current != idx:
                    source_current = idx
                    record_change(idx, "current", 1.0, kind="current")
                    return True, idx, True
                return True, idx, False
    return False, None, False


def process_vimix_message(address, args):
    global vimix_has_093, _first_source_at
    match = re.match(r"^/?vimix/([^/]+)/(.+)$", address)
    if match:
        target_str, prop_name = match.group(1), match.group(2)
        if prop_name not in SUPPORTED_PROPERTIES:
            return None

        idx = _resolve_target_index(target_str)
        if idx is None:
            return None
        val = args[0] if len(args) == 1 else list(args) if len(args) > 1 else None

        with STATE_LOCK:
            if idx not in vimix_data:
                vimix_data[idx] = create_empty_vimix_entry()
                # e99s01: the dictionary key IS the live-session index (the roster
                # addresses sources by index and `_resolve_target_index` derives the
                # key from this same table), so the tier no longer asks Vimix for it.
                # It keeps reaching /state and /events exactly as before.
                vimix_data[idx]["index"] = idx

            if prop_name in ("id", "type") and val is not None:
                vimix_has_093 = True

            if (
                prop_name == "id"
                and target_str.isdigit()
                and int(target_str) == idx
                and idx in seen_current
            ):
                seen_id.add(idx)

            if (
                _probe_active
                and prop_name == "name"
                and target_str.isdigit()
                and val
                and int(target_str) in _probe_requested
            ):
                _probe_found[idx] = str(val)

            if prop_name == "name":
                current_name = vimix_data[idx].get("name")
                if current_name is None:
                    if not _first_source_at:
                        _first_source_at = time.monotonic()
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
            created = prop_name == "name" and current_val is None
            vimix_data[idx][prop_name] = val
            if is_changed:
                record_change(idx, prop_name, val, kind="added" if created else "changed")
            if is_changed and LOG_LEVEL >= 1:
                log_bus.emit(
                    f"{COLOR_TIMESTAMP}[VIMIX DATA]{COLOR_RESET} Source {idx} | {prop_name}: {val}"
                )

            if prop_name == "uri" and is_changed and isinstance(val, str) and val:
                threading.Thread(
                    target=generate_thumbnails_worker, args=(idx, val), daemon=True
                ).start()

            return idx, prop_name, val, is_changed
    return None


def forward_command(address: str, args: Any) -> None:
    """Forward one decoded command to Vimix and keep the mirror in step (e98s01).

    The single forwarding path, shared by the legacy OSC input (until e98s04) and
    the HTTP command stream: the bytes toward Vimix are produced here and nowhere
    else, so the frozen contract cannot drift between the two entries. The echo
    only READS the message, and its failure must never cost the command itself.
    """
    forward_client.send_message(address, list(args))
    if str(address).lstrip("/") in SESSION_COMMAND_ADDRESSES:
        request_session_sync()
    try:
        apply_echo(address, args)
    except Exception as e:
        if LOG_LEVEL >= 1:
            log_bus.emit(f"{COLOR_RESET_EV}[ECHO]{COLOR_RESET} ignored: {e}", "error")


def apply_link_frame(payload: bytes) -> None:
    """Apply one raw frame from the HTTP command stream (e98s01).

    Raises on anything the link must not accept: the transport closes the stream
    on a raise, so a malformed frame costs a reconnect and never a half-applied
    command. A ``{"ping": true}`` keepalive is not a command and is not forwarded.
    """
    try:
        frame = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        log_bus.emit(f"{COLOR_RESET_EV}[LINK]{COLOR_RESET} malformed frame", "error")
        raise ValueError("malformed frame") from None
    if not isinstance(frame, dict):
        log_bus.emit(f"{COLOR_RESET_EV}[LINK]{COLOR_RESET} frame is not an object", "error")
        raise ValueError("frame is not an object")
    note_consumer_activity()  # e99s02: a live command stream is a live consumer
    if frame.get("ping"):
        return
    address = frame.get("address")
    if not isinstance(address, str) or not address.startswith("/"):
        log_bus.emit(f"{COLOR_RESET_EV}[LINK]{COLOR_RESET} frame has no OSC address", "error")
        raise ValueError("frame has no OSC address")
    args = frame.get("args")
    if args is None:
        args = []
    if not isinstance(args, list):
        log_bus.emit(f"{COLOR_RESET_EV}[LINK]{COLOR_RESET} args is not a list", "error")
        raise ValueError("args is not a list")
    forward_command(address, args)


def create_osc_handler(server_port):
    def osc_handler(client_address, address, *args):
        clean_address = address.lstrip("/")
        peer_ip = (
            client_address[0]
            if isinstance(client_address, (tuple, list)) and client_address
            else str(client_address)
        )

        # e42s01: the authenticator is always accepted; every other message from
        # an unbound peer is dropped BEFORE it can reach vimix.
        if clean_address.rstrip("/") == "viosc/auth":
            code = args[0] if args else ""
            if not PAIRING_GATE.authenticate_peer(code, peer_ip) and LOG_LEVEL >= 1:
                log_bus.emit(f"{COLOR_RESET_EV}[PAIRING]{COLOR_RESET} rejected {peer_ip}", "error")
            return

        if clean_address.startswith("viosc"):
            # BUG-2026-11-08: the gate covers the CONTROLLER lane only. It used to
            # sit above every branch, so Vimix's own replies (the local OSC
            # upstream, no pairing to give) were dropped until a controller
            # happened to authenticate from the same IP — the mirror stayed empty
            # while VJmix was not connected.
            if not PAIRING_GATE.is_bound(peer_ip):
                return
            if clean_address.startswith("viosc/regen_thumb/"):
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
        elif server_port == FROMVIMIX_PORT and clean_address.startswith("vimix/"):
            mark_vimix_activity()
            if clean_address.startswith("vimix/current/"):
                # The change is recorded by the ingest (record_change) and reaches
                # VJmix on /events: there is no broadcast to schedule any more.
                process_vimix_current_message(address, args)
            else:
                process_vimix_message(address, args)

    return osc_handler


def sync_loop():
    """The daemon's single polling thread (e82s02): a tick, not a 2 s batch."""
    while True:
        with contextlib.suppress(Exception):
            sync_tick()
        time.sleep(POLL_TICK_MS / 1000.0)


def bind_probe(ip: str, port: int, sock_type: int = socket.SOCK_DGRAM) -> bool:
    """True when a socket of ``sock_type`` can bind (ip, port) right now.

    The OSC endpoints are UDP and the HTTP surface is TCP; probing the wrong
    family always succeeds (separate port spaces), which is how a second instance
    kept looking healthy (e98s04).
    """
    sock = socket.socket(socket.AF_INET, sock_type)
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
    for role, ip, port in (("vimix replies", LOCAL_BIND_IP, FROMVIMIX_PORT),):
        if not bind_probe(ip, port):
            busy.append(f"{role} {ip}:{port}")
    # e98s04: the HTTP surface is the link. A second instance loses this bind and
    # serves nobody, so say it at boot (before the retry loop starts) instead of
    # letting a window with a pairing code pretend it is usable.
    http_ip = "127.0.0.1" if HTTP_IP in ("0.0.0.0", "") else HTTP_IP
    if not bind_probe(http_ip, HTTP_PORT, socket.SOCK_STREAM):
        busy.append(f"HTTP {HTTP_IP}:{HTTP_PORT} (another viOSC owns the link)")
    for item in busy:
        log_bus.emit(
            f"{COLOR_RESET_EV}[STARTUP]{COLOR_RESET} port busy ({item}) — is "
            "another viOSC instance already running?",
            "error",
        )


def size_receive_buffer(sock) -> None:
    """Keep a UDP receive socket at least RECV_BUFFER_BYTES (e41s01).

    Best-effort on purpose: a kernel cap or a platform refusing the option must
    never stop the daemon from serving (Defensive Code, AGENTS.md). A kernel
    default larger than RECV_BUFFER_BYTES is left alone, never shrunk.
    """
    with contextlib.suppress(OSError):
        current = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        if current < RECV_BUFFER_BYTES:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RECV_BUFFER_BYTES)


def create_osc_server(ip, port, role_port=None):
    """Bind the OSC server without serving (boot + tests).

    socketserver defaults max_packet_size to 8192; a Vimix roster for 100
    sources is ~12.7 KB and was silently cut before python-osc parsed it. The
    instance attribute keeps the test fakes and the real class on one path.
    """
    dispatcher = Dispatcher()
    handler = create_osc_handler(port if role_port is None else role_port)
    dispatcher.set_default_handler(handler, needs_reply_address=True)
    server = BlockingOSCUDPServer((ip, port), dispatcher)
    server.max_packet_size = MAX_OSC_DATAGRAM_BYTES
    size_receive_buffer(server.socket)
    return server


def start_server_instance(ip, port):
    try:
        server = create_osc_server(ip, port)
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


def resolve_binary(name: str) -> str | None:
    """Resolve a bundled/configured/PATH binary, including macOS app bundles."""
    if os.path.isabs(name) and os.access(name, os.X_OK):
        return name
    found = shutil.which(name)
    if found is not None:
        return found
    candidates: list[str] = []
    if sys.platform == "darwin":
        candidates.extend(DARWIN_BINARY_DIRS)
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        # Contents/MacOS/viOSC -> Contents/Resources/bin
        candidates.extend(
            [
                os.path.join(exe_dir, "..", "Resources", "bin"),
                os.path.join(exe_dir, "Resources", "bin"),
            ]
        )
    for directory in candidates:
        candidate = os.path.join(directory, name)
        if os.access(candidate, os.X_OK):
            return os.path.realpath(candidate)
    return None


def verify_dependencies() -> bool:
    """Resolve ffmpeg/ffprobe; a miss degrades the app, it never exits (e04s01)."""
    global FFMPEG_PATH, FFPROBE_PATH, FFMPEG_AVAILABLE
    ffmpeg = resolve_binary(FFMPEG_PATH)
    ffprobe = resolve_binary(FFPROBE_PATH)
    if ffmpeg is None or ffprobe is None:
        FFMPEG_AVAILABLE = False
        log_bus.emit(
            f"ERROR: ffmpeg/ffprobe not found ('{FFMPEG_PATH}' / '{FFPROBE_PATH}').",
            "error",
        )
        log_bus.emit(
            "Thumbnails and preview are disabled; the OSC router and the HTTP "
            "state plane keep running. Install ffmpeg or set VIOSC_FFMPEG/VIOSC_FFPROBE.",
            "error",
        )
        return False
    FFMPEG_PATH = ffmpeg
    FFPROBE_PATH = ffprobe
    FFMPEG_AVAILABLE = True
    # e02s01: resolved paths let the AppImage smoke test assert the bundled
    # binaries are the ones in use (AppRun prepends $APPDIR/usr/bin to PATH).
    log_bus.emit(f"ffmpeg: {ffmpeg}")
    log_bus.emit(f"ffprobe: {ffprobe}")
    return True


def restart_command() -> list[str]:
    """argv for a same-pid re-exec (Apply & restart, e01s05).

    The window closes nothing: os.execv replaces this process with a fresh
    boot of the same script and command-line arguments, so the servers
    rebind and the GUI reopens with the saved config. A frozen bundle
    (PyInstaller/py2app) is the executable itself; do not pass it as a script.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, *sys.argv[1:]]
    return [sys.executable, os.path.realpath(sys.argv[0]), *sys.argv[1:]]


def _report_gui_fatal(headline: str | None = None) -> None:
    """GUI-mode fatal-boot dialog (console is off): show ``headline`` or the log tail."""
    detail = headline or "\n".join(strip_ansi(text) for text, _level in log_bus.tail(count=25))
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


def http_link_status() -> str:
    """``up`` / ``starting`` / the bind error — the window's link row (e98s04)."""
    if _http_up:
        return "up"
    return http_link_error or "starting"


def start_http_forever(make_server, retry_s: float = HTTP_BIND_RETRY_S) -> None:
    """Bind the only surface, retrying loudly and for ever (e98s04).

    Before 1.0 a busy ``:8686`` was a degraded preview while the OSC lanes kept
    the link alive. There is no second path now: a daemon that cannot bind must
    say so every ``retry_s`` and must never look healthy. ``make_server`` returns
    a bound server (it raises ``OSError`` on a busy port).
    """
    global _http_up, http_link_error
    while True:
        try:
            server = make_server()
        except OSError as e:
            _http_up = False
            http_link_error = f"cannot bind {HTTP_IP}:{HTTP_PORT} ({e})"
            log_bus.emit(
                f"{COLOR_RESET_EV}[HTTP ERROR]{COLOR_RESET} {http_link_error} — "
                f"retrying in {int(retry_s)} s",
                "error",
            )
            time.sleep(retry_s)
            continue
        except Exception as e:
            _http_up = False
            http_link_error = str(e)
            log_bus.emit(
                f"{COLOR_RESET_EV}[HTTP ERROR]{COLOR_RESET} {http_link_error} — "
                f"retrying in {int(retry_s)} s",
                "error",
            )
            time.sleep(retry_s)
            continue
        _http_up = True
        http_link_error = ""
        if LOG_LEVEL >= 1:
            log_bus.emit(f"[HTTP] listening on {HTTP_IP}:{HTTP_PORT}")
        server.serve_forever()
        return


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
    # e98s08: ONE daemon per machine. The lock is taken before anything is bound
    # or loaded, so a second launch cannot fight for the ports or show a pairing
    # code it could never serve.
    lock = instancelock.InstanceLock(instancelock.lock_path(args.config or config.config_path()))
    if not lock.acquire():
        holder = lock.holder()
        message = (
            "viOSC is already running"
            + (f" (pid {holder})" if holder else "")
            + " — one instance per machine. Close the other window first."
        )
        log_bus.emit(f"{COLOR_RESET_EV}[STARTUP]{COLOR_RESET} {message}", "error")
        if gui_mode:
            _report_gui_fatal(message)
        else:
            print(f"ERROR: {message}", file=sys.stderr)
        sys.exit(2)
    values, _sources = load_boot_config(args.config)
    verify_dependencies()
    log_bus.emit(
        "=========================================================================================="
    )
    log_bus.emit(
        "            viOSC STARTED — OSC toward Vimix, HTTP toward the controller            "
    )
    log_bus.emit(f"   vimix out : {TOVIMIX_IP}:{TOVIMIX_PORT}   (commands; OSC only toward Vimix)")
    log_bus.emit(f"   vimix in  : {LOCAL_BIND_IP}:{FROMVIMIX_PORT}   (vimix default OSC replies)")
    log_bus.emit(f"   http      : {HTTP_IP}:{HTTP_PORT}   (state/events/commands/files)")
    if PAIRING_ENABLED:
        log_bus.emit(f"   pairing : code {PAIRING_CODE}   (pairing ON — enter this code in the UI)")
    log_bus.emit(
        "=========================================================================================="
    )
    warn_busy_ports()
    threading.Thread(target=sync_loop, daemon=True).start()

    threading.Thread(
        target=start_server_instance, args=(LOCAL_BIND_IP, FROMVIMIX_PORT), daemon=True
    ).start()

    def _config_provider():
        return config_payload(args.config or config.config_path())

    def _apply_config(changes):
        return apply_config_changes(changes, args.config or config.config_path())

    def _restart():
        os.execv(sys.executable, restart_command())

    # e98s04: the HTTP surface is the link. It binds in its own thread so a busy
    # port cannot stop the window from opening and showing WHY the link is down.
    # The server is bound to THIS module's live state table and probe (the module
    # runs as __main__ — an import-by-name inside the server would see an empty
    # copy).
    from http_api import EventHub, start_http_server

    install_event_hub(EventHub(on_subscribe=note_consumer_activity))  # e99s02

    def _make_http_server():
        return start_http_server(
            HTTP_IP,
            HTTP_PORT,
            vimix_data,
            probe_media_meta,
            resolve_thumbnail_blob,
            http_state_json,
            PAIRING_GATE,
            _FsAccess(),
            provide_config=_config_provider,
            apply_config=_apply_config,
            restart=_restart,
            provide_rev=lambda: state_rev,
            events=EVENT_HUB,
            apply_interest=apply_interest,
            regen_thumb=regen_thumb_for,
            state_lock=STATE_LOCK,
            apply_link=apply_link_frame,
        )

    threading.Thread(target=start_http_forever, args=(_make_http_server,), daemon=True).start()

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
