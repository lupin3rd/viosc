"""GUI-managed configuration for viosc (e01s01).

The daemon's settings used to live as module constants + env vars read at
import. This module is the single schema and persistence point: field
inventory (the current settings only), per-key precedence
(JSON > env > builtin default), validation that falls back per key and never
blocks boot, and atomic JSON writes. Headless by construction — no tkinter,
no OSC, no import of the daemon.

Precedence rule: env vars are honored only where the JSON file has no key,
so a GUI-saved JSON is authoritative once it exists (user decision d1).
"""

import json
import os
import tempfile
from typing import Any

# Field schema: one entry per current setting (SCOPE d4). group semantics:
#   restart - server bind ports/IPs: apply on next launch
#   live    - client destinations + per-use knobs: apply immediately
#   boot    - bound at boot (semaphore): apply on next launch
FIELD_SPEC: dict[str, dict[str, Any]] = {
    # Group A — restart required (server bind ports/IPs)
    "listen_ip": {"group": "restart", "type": "ip", "default": "0.0.0.0", "attr": "LISTEN_IP"},
    "listen_port": {"group": "restart", "type": "port", "default": 6666, "attr": "LISTEN_PORT"},
    "local_bind_ip": {
        "group": "restart",
        "type": "ip",
        "default": "127.0.0.1",
        "attr": "LOCAL_BIND_IP",
    },
    "from_vimix_port": {
        "group": "restart",
        "type": "port",
        "default": 7001,
        "attr": "FROMVIMIX_PORT",
    },
    "preview_ip": {"group": "restart", "type": "ip", "default": "0.0.0.0", "attr": "PREVIEW_IP"},
    "preview_port": {"group": "restart", "type": "port", "default": 8686, "attr": "PREVIEW_PORT"},
    # Group B — live apply (client destinations + per-use knobs)
    "tovimix_ip": {"group": "live", "type": "ip", "default": "127.0.0.1", "attr": "TOVIMIX_IP"},
    "tovimix_port": {"group": "live", "type": "port", "default": 7000, "attr": "TOVIMIX_PORT"},
    "ui_ip": {"group": "live", "type": "ip", "default": "127.0.0.1", "attr": "UI_IP"},
    "reply_port": {"group": "live", "type": "port", "default": 6667, "attr": "REPLY_PORT"},
    "ffmpeg_path": {"group": "live", "type": "path", "default": "ffmpeg", "attr": "FFMPEG_PATH"},
    "ffprobe_path": {"group": "live", "type": "path", "default": "ffprobe", "attr": "FFPROBE_PATH"},
    "log_level": {"group": "live", "type": "level", "default": 1, "attr": "LOG_LEVEL"},
    "sync_interval_ms": {
        "group": "live",
        "type": "ms",
        "default": 2000,
        "attr": "sync_interval_time",
    },
    "prune_delay_sec": {
        "group": "live",
        "type": "seconds",
        "default": 0.5,
        "attr": "PRUNE_DELAY_SEC",
    },
    "monitor_max_misses": {
        "group": "live",
        "type": "count",
        "default": 3,
        "attr": "MONITOR_MAX_MISSES",
    },
    "thumb_max_count": {"group": "live", "type": "count", "default": 3, "attr": "THUMB_MAX_COUNT"},
    # Group C — restart required (semaphore bound at boot)
    "thumb_max_concurrency": {
        "group": "boot",
        "type": "count",
        "default": 3,
        "attr": "THUMB_MAX_CONCURRENCY",
    },
}

DEFAULTS: dict[str, Any] = {k: spec["default"] for k, spec in FIELD_SPEC.items()}

# Form visibility tiers (e01s07, user decision 2026-09-08 — option B):
#   essential — everyday wiring, always in the main form
#   advanced  — less common ports/knobs, behind the 'Advanced settings' toggle
#   hidden    — JSON-only: auto-resolved (ffmpeg/ffprobe) or internal tuning;
#               still honoured when present in the config file, never shown
VISIBILITY: dict[str, str] = {
    "listen_ip": "advanced",
    "listen_port": "essential",
    "local_bind_ip": "hidden",
    "from_vimix_port": "essential",
    "preview_ip": "advanced",
    "preview_port": "essential",
    "tovimix_ip": "hidden",
    "tovimix_port": "essential",
    "ui_ip": "essential",
    "reply_port": "essential",
    "ffmpeg_path": "hidden",
    "ffprobe_path": "hidden",
    "log_level": "hidden",
    "sync_interval_ms": "advanced",
    "prune_delay_sec": "hidden",
    "monitor_max_misses": "hidden",
    "thumb_max_count": "advanced",
    "thumb_max_concurrency": "hidden",
}


def fields(tier: str) -> list[str]:
    """Config keys whose form visibility matches ``tier``."""
    return [k for k in FIELD_SPEC if VISIBILITY.get(k) == tier]


def essential_fields() -> list[str]:
    """Everyday wiring shown in the main form section."""
    return fields("essential")


def advanced_fields() -> list[str]:
    """Less common ports and knobs behind the 'Advanced settings' toggle."""
    return fields("advanced")


def daemon_attr(key: str) -> str:
    """Name of the daemon module attribute that holds a config value.

    Single source for the GUI prefill and boot symmetry; the daemon
    attribute names do NOT all equal the config key upper-cased
    (``from_vimix_port`` -> ``FROMVIMIX_PORT``), so an explicit map lives in
    the schema.
    """
    return FIELD_SPEC[key]["attr"]


# Legacy env override mechanism, honored only where the JSON has no key.
ENV_VAR_MAP: dict[str, str] = {
    "ui_ip": "VIOSC_UI_IP",
    "ffmpeg_path": "VIOSC_FFMPEG",
    "ffprobe_path": "VIOSC_FFPROBE",
    "preview_ip": "VIOSC_PREVIEW_IP",
    "preview_port": "VIOSC_PREVIEW_PORT",
}

_MIN_PORT = 1
_MAX_PORT = 65535
_MIN_SYNC_INTERVAL_MS = 100
ALLOWED_LOG_LEVELS = (0, 1)


def is_valid_value(key: str, value: Any) -> bool:
    """True when ``value`` satisfies the schema for ``key`` (type + bounds)."""
    spec = FIELD_SPEC[key]
    vtype = spec["type"]
    if vtype == "port":
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and _MIN_PORT <= value <= _MAX_PORT
        )
    if vtype == "count":
        return isinstance(value, int) and not isinstance(value, bool) and value >= 1
    if vtype == "level":
        return (
            isinstance(value, int) and not isinstance(value, bool) and value in ALLOWED_LOG_LEVELS
        )
    if vtype == "ms":
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and value >= _MIN_SYNC_INTERVAL_MS
        )
    if vtype == "seconds":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
    if vtype in ("ip", "path"):
        return isinstance(value, str) and bool(value.strip())
    return False


def validate(values: dict[str, Any]) -> list[str]:
    """Human-readable problems for every invalid value (empty = valid)."""
    problems = []
    for key, value in values.items():
        if key not in FIELD_SPEC:
            problems.append(f"unknown config key '{key}' (ignored)")
        elif not is_valid_value(key, value):
            problems.append(f"invalid value for '{key}': {value!r}")
    return problems


def parse_form(
    raw: dict[str, str], base: dict[str, Any] | None = None
) -> tuple[dict[str, Any], list[str]]:
    """Typed config from raw GUI entry strings (e01s05/s07).

    Starts from ``base`` (default: DEFAULTS) so fields not present in
    ``raw`` — the JSON-only hidden fields — keep their current value when
    the GUI saves the visible ones. An empty entry reverts that key to its
    builtin default; an entry that does not coerce or fails validation is
    reported in ``problems`` and keeps its base value. Returns
    ``(values, problems)``.
    """
    base = DEFAULTS if base is None else base
    problems = []
    values: dict[str, Any] = dict(base)
    for key, text in raw.items():
        if key not in FIELD_SPEC:
            problems.append(f"unknown config key '{key}' (ignored)")
            continue
        text = text.strip()
        if text == "":
            values[key] = DEFAULTS[key]
            continue
        coerced = _coerce(key, text)
        if coerced is None or not is_valid_value(key, coerced):
            problems.append(f"invalid value for '{key}': {text!r}")
            continue
        values[key] = coerced
    return values, problems


def _env_values(env) -> dict[str, Any]:
    """Env overrides, coerced into schema types (env vars arrive as strings)."""
    env_values: dict[str, Any] = {}
    for key, env_var in ENV_VAR_MAP.items():
        raw = env.get(env_var)
        if raw is None or raw == "":
            continue
        coerced = _coerce(key, raw)
        if coerced is not None and is_valid_value(key, coerced):
            env_values[key] = coerced
    return env_values


def _to_int(raw: Any) -> int | None:
    """Coerce a JSON/env scalar into an int, or None when unusable."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if raw.is_integer() else None
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            return None
    return None


def _to_float(raw: Any) -> float | None:
    """Coerce a JSON/env scalar into a float, or None when unusable."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw.strip())
        except ValueError:
            return None
    return None


def _coerce(key: str, raw: Any) -> Any:
    """Coerce a JSON/env scalar into the schema type, or None when unusable."""
    spec = FIELD_SPEC[key]
    vtype = spec["type"]
    if vtype in ("port", "count", "level", "ms"):
        return _to_int(raw)
    if vtype == "seconds":
        return _to_float(raw)
    if vtype in ("ip", "path"):
        return raw if isinstance(raw, str) else None
    return None


def effective(
    base_json: dict[str, Any] | None, env=None
) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    """Merge precedence JSON > env > default; per-key fallback on bad values.

    Returns ``(values, sources, warnings)`` where ``sources[key]`` is
    'json', 'env' or 'default' — the GUI uses it to show when an env var is
    silently winning.
    """
    env = os.environ if env is None else env
    values: dict[str, Any] = dict(DEFAULTS)
    sources: dict[str, str] = dict.fromkeys(DEFAULTS, "default")
    warnings: list[str] = []
    for key, raw in _env_values(env).items():
        if not is_valid_value(key, raw):
            continue
        values[key] = raw
        sources[key] = "env"
    for key, raw in (base_json or {}).items():
        if key not in FIELD_SPEC:
            warnings.append(f"config: unknown key '{key}' ignored")
            continue
        coerced = _coerce(key, raw)
        if coerced is None or not is_valid_value(key, coerced):
            warnings.append(
                f"config: invalid value for '{key}' ({raw!r}) — using {sources[key]} default"
            )
            continue
        values[key] = coerced
        sources[key] = "json"
    return values, sources, warnings


def load_effective(
    path: str | None = None, env=None
) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    """Effective config from the JSON file at ``path`` (default: config_path)."""
    path = config_path(env) if path is None else path
    env = os.environ if env is None else env
    base_json: dict[str, Any] = {}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                base_json = loaded
            else:
                return (
                    dict(DEFAULTS),
                    dict.fromkeys(DEFAULTS, "default"),
                    [f"config: {path} is not a JSON object — running on defaults"],
                )
        except (OSError, json.JSONDecodeError) as e:
            return (
                dict(DEFAULTS),
                dict.fromkeys(DEFAULTS, "default"),
                [f"config: cannot read {path} ({e}) — running on defaults"],
            )
    return effective(base_json, env)


def save(path: str, values: dict[str, Any]) -> None:
    """Atomic write (temp file + rename); never leaves a half-written JSON."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".config-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(values, f, indent=2, sort_keys=True)
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def config_path(env=None) -> str:
    """$XDG_CONFIG_HOME|~/.config + viosc/config.json (user decision d1)."""
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME", ""), ".config")
    return os.path.join(base, "viosc", "config.json")
