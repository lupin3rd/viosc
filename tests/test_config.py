"""Config module tests for viosc (e01s01).

Covers the field schema (current settings only, mirroring the 0.2.0 module
constants), per-key precedence (JSON > env > default), validation that falls
back per key without crashing boot, and atomic JSON persistence.

Pure headless logic — no tkinter, no OSC, no filesystem beyond tmp_path.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

# ---------------------------------------------------------------------------
# e01s01t1 — RED: DEFAULTS mirrors the 0.2.0 daemon constants
# ---------------------------------------------------------------------------


def test_defaults_mirror_daemon_ports():
    assert config.DEFAULTS["listen_ip"] == "0.0.0.0"
    assert config.DEFAULTS["listen_port"] == 6666
    assert config.DEFAULTS["local_bind_ip"] == "127.0.0.1"
    assert config.DEFAULTS["from_vimix_port"] == 7001
    assert config.DEFAULTS["tovimix_ip"] == "127.0.0.1"
    assert config.DEFAULTS["tovimix_port"] == 7000
    assert config.DEFAULTS["ui_ip"] == "127.0.0.1"
    assert config.DEFAULTS["reply_port"] == 6667
    assert config.DEFAULTS["preview_ip"] == "0.0.0.0"
    assert config.DEFAULTS["preview_port"] == 8686


def test_defaults_mirror_daemon_knobs_and_paths():
    assert config.DEFAULTS["ffmpeg_path"] == "ffmpeg"
    assert config.DEFAULTS["ffprobe_path"] == "ffprobe"
    assert config.DEFAULTS["log_level"] == 1
    assert config.DEFAULTS["sync_interval_ms"] == 2000
    assert config.DEFAULTS["prune_delay_sec"] == 0.5
    assert config.DEFAULTS["monitor_max_misses"] == 3
    assert config.DEFAULTS["thumb_max_concurrency"] == 3
    assert config.DEFAULTS["thumb_max_count"] == 3


def test_field_spec_covers_apply_groups_and_env_map():
    # Every README-listed group is represented.
    assert {config.FIELD_SPEC[k]["group"] for k in config.FIELD_SPEC} == {
        "restart",
        "live",
        "boot",
    }
    assert config.ENV_VAR_MAP == {
        "ui_ip": "VIOSC_UI_IP",
        "ffmpeg_path": "VIOSC_FFMPEG",
        "ffprobe_path": "VIOSC_FFPROBE",
        "preview_ip": "VIOSC_PREVIEW_IP",
        "preview_port": "VIOSC_PREVIEW_PORT",
    }


# ---------------------------------------------------------------------------
# e01s01t3 — RED: precedence JSON > env > default, sources() report
# ---------------------------------------------------------------------------


def test_json_wins_over_env_wins_over_default(tmp_path, monkeypatch):
    monkeypatch.setenv("VIOSC_UI_IP", "10.0.0.8")
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"ui_ip": "10.0.0.9"}))

    values, sources, warnings = config.load_effective(str(cfg_path))

    assert values["ui_ip"] == "10.0.0.9"
    assert sources["ui_ip"] == "json"
    assert warnings == []


def test_env_wins_over_default_when_json_lacks_key(tmp_path, monkeypatch):
    monkeypatch.setenv("VIOSC_FFMPEG", "/opt/ffmpeg/bin/ffmpeg")
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"ui_ip": "10.0.0.9"}))

    values, sources, _ = config.load_effective(str(cfg_path))

    assert values["ffmpeg_path"] == "/opt/ffmpeg/bin/ffmpeg"
    assert sources["ffmpeg_path"] == "env"
    assert values["ui_ip"] == "10.0.0.9"
    assert sources["ui_ip"] == "json"


def test_missing_json_falls_back_to_env_and_default(tmp_path, monkeypatch):
    monkeypatch.setenv("VIOSC_PREVIEW_PORT", "9000")
    values, sources, _ = config.load_effective(str(tmp_path / "nope.json"))

    assert values["preview_port"] == 9000
    assert sources["preview_port"] == "env"
    assert values["listen_port"] == 6666
    assert sources["listen_port"] == "default"


# ---------------------------------------------------------------------------
# e01s01t5 — RED: validation + per-key fallback
# ---------------------------------------------------------------------------


def test_validate_reports_bad_ports_levels_and_intervals():
    assert config.validate({"listen_port": 0})
    assert config.validate({"listen_port": 70000})
    assert config.validate({"preview_port": -1})
    assert config.validate({"log_level": 2})
    assert config.validate({"sync_interval_ms": 50})
    assert config.validate({"thumb_max_concurrency": 0})
    assert config.validate({"thumb_max_count": "many"})
    assert config.validate({"ui_ip": ""})


def test_valid_values_validate_clean():
    assert config.validate(config.DEFAULTS) == []


def test_invalid_json_value_falls_back_with_warning(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"listen_port": "not-a-port", "ui_ip": "10.0.0.9"}))

    values, sources, warnings = config.load_effective(str(cfg_path))

    assert values["listen_port"] == 6666
    assert sources["listen_port"] == "default"
    assert values["ui_ip"] == "10.0.0.9"
    assert any("listen_port" in w for w in warnings), warnings


def test_malformed_json_warns_and_keeps_defaults(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{broken json")

    values, _, warnings = config.load_effective(str(cfg_path))

    assert values == config.DEFAULTS
    assert warnings, "malformed JSON must surface a warning"


# ---------------------------------------------------------------------------
# e01s01t7 — RED: atomic save + config_path
# ---------------------------------------------------------------------------


def test_save_round_trips_and_creates_directory(tmp_path):
    target = tmp_path / "deep" / "dir" / "config.json"
    overrides = dict(config.DEFAULTS, ui_ip="192.168.1.50", log_level=0)

    config.save(str(target), overrides)

    values, sources, _ = config.load_effective(str(target))
    assert values["ui_ip"] == "192.168.1.50"
    assert sources["ui_ip"] == "json"
    assert values["log_level"] == 0


def test_save_is_atomic_no_stray_temp_files(tmp_path):
    target = tmp_path / "config.json"
    config.save(str(target), config.DEFAULTS)

    assert list(tmp_path.iterdir()) == [target], "no temp file may remain"


def test_config_path_honors_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.config_path() == str(tmp_path / "viosc" / "config.json")


def test_config_path_falls_back_to_home(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config.config_path() == str(tmp_path / ".config" / "viosc" / "config.json")


# ---------------------------------------------------------------------------
# e01s05t1 — RED: parse_form (raw GUI entry strings -> typed values)
# ---------------------------------------------------------------------------


def test_parse_form_types_strings_and_keeps_unspecified_defaults():
    values, problems = config.parse_form(
        {"ui_ip": "10.0.0.9", "reply_port": "6699", "prune_delay_sec": "1.5", "log_level": "0"}
    )
    assert problems == []
    assert values["ui_ip"] == "10.0.0.9"
    assert values["reply_port"] == 6699
    assert values["prune_delay_sec"] == 1.5
    assert values["log_level"] == 0
    assert values["listen_port"] == 6666, "unspecified keys keep the default"


def test_parse_form_empty_entries_use_defaults():
    values, problems = config.parse_form(dict.fromkeys(config.FIELD_SPEC, ""))
    assert problems == []
    assert values == config.DEFAULTS


def test_parse_form_reports_invalid_and_falls_back_per_key():
    values, problems = config.parse_form({"listen_port": "70000", "log_level": "2"})
    assert len(problems) == 2
    assert values["listen_port"] == 6666
    assert values["log_level"] == 1


# ---------------------------------------------------------------------------
# e01s07 — form visibility tiers (option B) + base-preserving parse_form
# ---------------------------------------------------------------------------


def test_visibility_tiers_cover_every_field():
    assert set(config.VISIBILITY) == set(config.FIELD_SPEC)
    assert set(config.VISIBILITY.values()) == {"essential", "advanced", "hidden"}
    assert config.essential_fields() == [
        "listen_port",
        "preview_port",
        "ui_ip",
        "reply_port",
        "pairing_enabled",
    ]
    assert config.advanced_fields() == [
        "listen_ip",
        "preview_ip",
        "sync_interval_ms",
        "thumb_max_count",
        "pairing_code_length",
        "pairing_code_ttl_hours",
        "pairing_trusted_peers",
        "fs_roots",
        "fs_show_hidden",
        "fs_sessions_dir",
    ]


def test_visibility_hides_auto_and_internal_fields():
    hidden = {k for k, tier in config.VISIBILITY.items() if tier == "hidden"}
    assert hidden == {
        "local_bind_ip",
        "from_vimix_port",
        "tovimix_ip",
        "tovimix_port",
        "ffmpeg_path",
        "ffprobe_path",
        "log_level",
        "prune_delay_sec",
        "monitor_max_misses",
        "thumb_max_concurrency",
        "pairing_lease_seconds",
        "pairing_lock_after",
        "pairing_lock_seconds",
        "pairing_global_lock_after",
        "pairing_code",
        "pairing_code_created_at",
        "fs_page_size",
    }


def test_parse_form_base_preserves_hidden_fields():
    base = dict(config.DEFAULTS, thumb_max_concurrency=7, ui_ip="10.0.0.9")

    values, problems = config.parse_form({"ui_ip": "10.1.1.1"}, base=base)

    assert problems == []
    assert values["thumb_max_concurrency"] == 7, "hidden fields keep their current value"
    assert values["sync_interval_ms"] == 2000
    assert values["ui_ip"] == "10.1.1.1"


def test_parse_form_empty_entry_reverts_that_key_to_default():
    base = dict(config.DEFAULTS, ui_ip="10.0.0.9")

    values, problems = config.parse_form({"ui_ip": ""}, base=base)

    assert problems == []
    assert values["ui_ip"] == "127.0.0.1"
    assert values["thumb_max_count"] == 3


def test_format_field_renders_a_list_with_commas():
    """BUG-2026-09-15T201625: the GUI entry must not show a Python list repr
    (`['~']`), which `_to_list` would then save back as a bogus single root."""
    assert config.format_field("fs_roots", ["/mnt/media", "~/videos"]) == "/mnt/media, ~/videos"
    assert config.format_field("fs_roots", []) == ""
    assert config.format_field("preview_port", 8686) == "8686"
    assert config.format_field("fs_show_hidden", False) == "False"


# ---------------------------------------------------------------------------
# e58s02t1 — RED: schema ownership markers (local-only bind + hidden Vimix fields)
# ---------------------------------------------------------------------------


def test_local_only_fields_are_the_management_bind():
    assert set(config.local_only_fields()) == {"preview_ip", "preview_port"}


def test_classify_changes_rejects_local_only():
    classified = config.classify_changes({"preview_port": 9999})
    assert "preview_port" in classified["local_only"]
    assert classified["live"] == {}
    assert classified["restart"] == {}


def test_remote_editable_excludes_hidden_and_local_only():
    editable = config.remote_editable_fields()
    assert "preview_ip" not in editable
    assert "preview_port" not in editable
    assert "from_vimix_port" not in editable
    assert "tovimix_port" not in editable
    assert "ffmpeg_path" not in editable
    assert "sync_interval_ms" in editable


def test_hidden_field_still_honoured_from_json(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"from_vimix_port": 7100}))
    values, _, _ = config.load_effective(str(cfg_path))
    assert values["from_vimix_port"] == 7100
