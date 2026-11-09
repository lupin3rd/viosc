"""The 1.0 config surface (e98s03): `http_*` with a legacy alias, and app_version.

The rename is a *migration*, not a break: an operator's `~/.config/viosc/config.json`
written by 0.9.x must keep working untouched, the renamed key must win when both
are present, and the file must be rewritten with the new names only.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import viosc


def test_the_schema_is_renamed():
    assert "http_ip" in config.FIELD_SPEC and "http_port" in config.FIELD_SPEC
    assert "preview_ip" not in config.FIELD_SPEC
    assert "preview_port" not in config.FIELD_SPEC
    assert set(config.local_only_fields()) == {"http_ip", "http_port"}
    assert config.FIELD_SPEC["http_port"]["attr"] == "HTTP_PORT"


def test_the_daemon_attributes_are_renamed():
    assert hasattr(viosc, "HTTP_IP") and hasattr(viosc, "HTTP_PORT")
    assert not hasattr(viosc, "PREVIEW_IP") and not hasattr(viosc, "PREVIEW_PORT")


def test_an_old_key_still_loads(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"preview_port": 9999, "preview_ip": "127.0.0.1"}))
    values, sources, warnings = config.load_effective(str(path))
    assert values["http_port"] == 9999
    assert values["http_ip"] == "127.0.0.1"
    assert sources["http_port"] == "json"
    assert warnings == []


def test_the_renamed_key_wins_when_both_are_present(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"preview_port": 9100, "http_port": 9200}))
    values, sources, _ = config.load_effective(str(path))
    assert values["http_port"] == 9200
    assert sources["http_port"] == "json"


def test_an_old_env_name_still_wins_over_the_default(monkeypatch):
    monkeypatch.delenv("VIOSC_HTTP_PORT", raising=False)
    monkeypatch.setenv("VIOSC_PREVIEW_PORT", "9300")
    values, sources, _ = config.load_effective("/nonexistent/config.json")
    assert values["http_port"] == 9300
    assert sources["http_port"] == "env"


def test_the_new_env_name_wins_over_the_old(monkeypatch):
    monkeypatch.setenv("VIOSC_PREVIEW_PORT", "9300")
    monkeypatch.setenv("VIOSC_HTTP_PORT", "9400")
    values, _, _ = config.load_effective("/nonexistent/config.json")
    assert values["http_port"] == 9400


def test_a_save_writes_the_new_names_only(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"preview_port": 9999, "log_level": 1}))
    values, _, _ = config.load_effective(str(path))
    config.merge_save(str(path), {"http_port": values["http_port"], "http_ip": values["http_ip"]})
    written = json.loads(path.read_text())
    assert written["http_port"] == 9999
    assert "preview_port" not in written


def test_the_config_payload_carries_the_app_version(tmp_path):
    path = tmp_path / "config.json"
    payload = viosc.config_payload(str(path))
    assert payload["app_version"] == viosc.APP_VERSION
    assert payload["values"]["http_port"] == config.DEFAULTS["http_port"]
    # The HTTP bind is machine-A local (the management plane): readable, never
    # remote-editable. Everything else keeps its e58 visibility.
    assert "http_port" not in payload["editable"]
    assert "http_ip" not in payload["editable"]
    assert "sync_interval_ms" in payload["editable"]


@pytest.mark.parametrize("bad", [{"preview_port": -1}, {"http_port": 0}])
def test_an_invalid_legacy_or_new_port_is_rejected(tmp_path, bad):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(bad))
    _values, _sources, warnings = config.load_effective(str(path))
    assert warnings and "http_port" in warnings[0]


def test_a_retired_field_is_dropped_with_one_line_and_not_per_key(tmp_path):
    """An upgraded install must not print five 'unknown key' lines at boot."""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "listen_port": 6666,
                "ui_ip": "127.0.0.1",
                "reply_port": 6667,
                "monitor_max_misses": 3,
                "http_port": 8686,
            }
        )
    )
    values, _, warnings = config.load_effective(str(path))

    assert values["http_port"] == 8686
    assert len(warnings) == 1, warnings
    assert "retired" in warnings[0]
    assert "listen_port" in warnings[0]
    # ... and the next write cleans the file.
    config.merge_save(str(path), {})
    written = json.loads(path.read_text())
    assert "listen_port" not in written and "ui_ip" not in written
