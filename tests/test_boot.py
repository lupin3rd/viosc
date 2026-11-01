"""Daemon boot-from-config tests for viosc (e01s02).

boot(values) must apply the effective config to the module state (JSON >
env > default), rebuild the thumbnail semaphore, and (re)create the two OSC
clients from the booted destinations — while module import keeps today's
default behaviour (the legacy 25 tests prove that).

Headless: no servers started, no display, monkeypatched clients.
"""

import json
import os
import sys
from typing import ClassVar

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import viosc


class FakeClient:
    instances: ClassVar[list[tuple[str, int]]] = []

    def __init__(self, ip, port):
        self.ip = ip
        self.port = port
        FakeClient.instances.append((ip, port))

    def send_message(self, *args):
        pass


def make_values(**overrides):
    values, _, _ = config.effective({}, {})
    values.update(overrides)
    return values


# ---------------------------------------------------------------------------
# e01s02t1 — RED: boot() applies the effective values to module state
# ---------------------------------------------------------------------------


def test_boot_applies_live_fields_to_module_state(monkeypatch):
    monkeypatch.setattr(viosc, "SimpleUDPClient", FakeClient)
    values = make_values(
        ui_ip="10.0.0.9",
        reply_port=6699,
        log_level=0,
        sync_interval_ms=500,
        ffmpeg_path="/opt/bin/ffmpeg",
        thumb_max_count=2,
    )

    viosc.boot(values)

    assert viosc.UI_IP == "10.0.0.9"
    assert viosc.REPLY_PORT == 6699
    assert viosc.LOG_LEVEL == 0
    assert viosc.sync_interval_time == 500
    assert viosc.FFMPEG_PATH == "/opt/bin/ffmpeg"
    assert viosc.THUMB_MAX_COUNT == 2


def test_boot_applies_restart_fields_to_module_state(monkeypatch):
    monkeypatch.setattr(viosc, "SimpleUDPClient", FakeClient)
    values = make_values(
        listen_port=7777,
        from_vimix_port=7555,
        preview_ip="0.0.0.0",
        preview_port=9090,
    )

    viosc.boot(values)

    assert viosc.LISTEN_PORT == 7777
    assert viosc.FROMVIMIX_PORT == 7555
    assert viosc.PREVIEW_IP == "0.0.0.0"
    assert viosc.PREVIEW_PORT == 9090


# ---------------------------------------------------------------------------
# e01s02t2 — RED: thumbnail semaphore rebuilt from config
# ---------------------------------------------------------------------------


def test_boot_rebuilds_thumb_semaphore(monkeypatch):
    monkeypatch.setattr(viosc, "SimpleUDPClient", FakeClient)

    viosc.boot(make_values(thumb_max_concurrency=5))

    acquired = []
    for _ in range(5):
        acquired.append(viosc.thumb_semaphore.acquire(blocking=False))
    sixth = viosc.thumb_semaphore.acquire(blocking=False)
    for _ in acquired:
        viosc.thumb_semaphore.release()
    assert all(acquired) and not sixth


# ---------------------------------------------------------------------------
# e01s02t3 — RED: clients recreated with the booted destinations
# ---------------------------------------------------------------------------


def test_boot_recreates_clients_with_booted_destinations(monkeypatch):
    FakeClient.instances = []
    monkeypatch.setattr(viosc, "SimpleUDPClient", FakeClient)

    viosc.boot(make_values(ui_ip="10.0.0.9", tovimix_ip="10.1.1.1"))

    destinations = set(FakeClient.instances)
    assert ("10.1.1.1", viosc.TOVIMIX_PORT) in destinations
    assert ("10.0.0.9", viosc.REPLY_PORT) in destinations


# ---------------------------------------------------------------------------
# e01s02t4 — RED: end-to-end precedence drives module state once booted
# ---------------------------------------------------------------------------


def test_boot_respects_json_over_env(tmp_path, monkeypatch):
    monkeypatch.setenv("VIOSC_UI_IP", "10.8.8.8")
    monkeypatch.setenv("VIOSC_FFMPEG", "/env/bin/ffmpeg")
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"ui_ip": "10.9.9.9"}))

    values, _, _ = config.load_effective(str(cfg_path))
    viosc.boot(values)

    assert viosc.UI_IP == "10.9.9.9", "JSON must win over the env var"
    assert viosc.FFMPEG_PATH == "/env/bin/ffmpeg", "env wins where JSON has no key"


def test_boot_with_no_config_keeps_defaults(monkeypatch):
    env_vars = (
        "VIOSC_UI_IP",
        "VIOSC_FFMPEG",
        "VIOSC_FFPROBE",
        "VIOSC_PREVIEW_IP",
        "VIOSC_PREVIEW_PORT",
    )
    for var in env_vars:
        monkeypatch.delenv(var, raising=False)

    values, _, _ = config.load_effective("/nonexistent/config.json")
    viosc.boot(values)

    assert viosc.LISTEN_PORT == 6666
    assert viosc.UI_IP == "127.0.0.1"
    assert viosc.FFMPEG_PATH == "ffmpeg"


# ---------------------------------------------------------------------------
# e01s05t3 — RED: restart_command (same-pid re-exec argv)
# ---------------------------------------------------------------------------


def test_restart_command_rebuilds_argv_with_real_script_path(monkeypatch, tmp_path):
    script = tmp_path / "viosc.py"
    script.write_text("")
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(sys, "argv", [str(script), "--config", "/tmp/c.json"])

    assert viosc.restart_command() == [
        "/usr/bin/python3",
        str(script),
        "--config",
        "/tmp/c.json",
    ]


def test_restart_command_with_no_args(monkeypatch, tmp_path):
    script = tmp_path / "viosc.py"
    script.write_text("")
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")
    monkeypatch.setattr(sys, "argv", [str(script)])

    assert viosc.restart_command() == ["/usr/bin/python3", str(script)]


# ---------------------------------------------------------------------------
# e01s05 regression — every config key maps to a real daemon attribute
# (fix: FROMVIMIX_PORT lacks the underscore; key.upper() broke the GUI)
# ---------------------------------------------------------------------------


def test_every_config_key_maps_to_a_daemon_attribute():
    for key in config.FIELD_SPEC:
        attr = config.daemon_attr(key)
        assert hasattr(viosc, attr), f"config key '{key}' -> daemon attr '{attr}' missing"
