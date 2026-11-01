"""Link pairing core tests for viosc (e42s01).

Covers the pairing configuration schema (enabled / code length / lease /
trusted peers), the random code generator, and the daemon boot state.

Pure headless logic — no tkinter, no OSC servers, no network. The enforcement
tests (auth, bound peers) live in the same file in the later tasks of the story.
"""

import json
import os
import sys
import time
from typing import ClassVar

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import pairing
import viosc

PAIRING_KEYS = (
    "pairing_enabled",
    "pairing_code_length",
    "pairing_lease_seconds",
    "pairing_trusted_peers",
)


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
# e42s01t1 — RED: the pairing config schema (defaults, types, precedence)
# ---------------------------------------------------------------------------


def test_pairing_defaults_are_safe():
    assert config.DEFAULTS["pairing_enabled"] is True
    assert config.DEFAULTS["pairing_code_length"] == 4
    assert config.DEFAULTS["pairing_lease_seconds"] == 3600
    assert config.DEFAULTS["pairing_trusted_peers"] == []


def test_pairing_keys_are_declared_with_attributes():
    for key in PAIRING_KEYS:
        assert key in config.FIELD_SPEC
        assert hasattr(viosc, config.daemon_attr(key)), key


def test_bool_validation():
    assert config.is_valid_value("pairing_enabled", True)
    assert config.is_valid_value("pairing_enabled", False)
    assert not config.is_valid_value("pairing_enabled", 1)
    assert not config.is_valid_value("pairing_enabled", "true")


def test_bool_coercion_from_form_strings():
    values, problems = config.parse_form({"pairing_enabled": "false"})
    assert values["pairing_enabled"] is False
    assert problems == []
    values, problems = config.parse_form({"pairing_enabled": "true"})
    assert values["pairing_enabled"] is True
    assert problems == []


def test_list_validation():
    assert config.is_valid_value("pairing_trusted_peers", [])
    assert config.is_valid_value("pairing_trusted_peers", ["10.0.0.5", "192.168.1.9"])
    assert not config.is_valid_value("pairing_trusted_peers", "")
    assert not config.is_valid_value("pairing_trusted_peers", ["", "10.0.0.5"])
    assert not config.is_valid_value("pairing_trusted_peers", [123])


def test_list_coercion_from_comma_separated_form_string():
    values, problems = config.parse_form({"pairing_trusted_peers": "10.0.0.5, 192.168.1.9"})
    assert values["pairing_trusted_peers"] == ["10.0.0.5", "192.168.1.9"]
    assert problems == []


def test_effective_accepts_pairing_json_and_reports_the_source():
    values, sources, warnings = config.effective(
        {"pairing_enabled": False, "pairing_trusted_peers": ["10.0.0.5"]}
    )
    assert values["pairing_enabled"] is False
    assert values["pairing_trusted_peers"] == ["10.0.0.5"]
    assert sources["pairing_enabled"] == "json"
    assert warnings == []


def test_effective_falls_back_per_key_on_a_bad_pairing_value():
    values, _sources, warnings = config.effective({"pairing_code_length": 0})
    assert values["pairing_code_length"] == config.DEFAULTS["pairing_code_length"]
    assert any("pairing_code_length" in warning for warning in warnings)


# ---------------------------------------------------------------------------
# e42s01t1 — RED: the random code generator and the boot-time code
# ---------------------------------------------------------------------------


def test_generate_code_has_the_requested_length_and_is_numeric():
    for length in (4, 6, 8):
        code = pairing.generate_code(length)
        assert len(code) == length
        assert code.isdigit()


def test_generate_code_rotates():
    codes = {pairing.generate_code(4) for _ in range(200)}
    assert len(codes) > 1


def test_boot_generates_a_code_with_the_configured_length(monkeypatch):
    monkeypatch.setattr(viosc, "SimpleUDPClient", FakeClient)
    values = make_values(pairing_enabled=True, pairing_code_length=6)
    viosc.boot(values)
    assert len(viosc.PAIRING_CODE) == 6
    assert viosc.PAIRING_CODE.isdigit()


def test_boot_with_pairing_disabled_has_no_code(monkeypatch):
    monkeypatch.setattr(viosc, "SimpleUDPClient", FakeClient)
    values = make_values(pairing_enabled=False)
    viosc.boot(values)
    assert viosc.PAIRING_CODE == ""


def test_boot_applies_the_pairing_config_to_the_daemon_state(monkeypatch):
    monkeypatch.setattr(viosc, "SimpleUDPClient", FakeClient)
    values = make_values(
        pairing_enabled=True,
        pairing_code_length=5,
        pairing_lease_seconds=120,
        pairing_trusted_peers=["10.0.0.5"],
    )
    viosc.boot(values)
    assert viosc.PAIRING_ENABLED is True
    assert viosc.PAIRING_CODE_LENGTH == 5
    assert viosc.PAIRING_LEASE_SECONDS == 120
    assert viosc.PAIRING_TRUSTED_PEERS == ["10.0.0.5"]


# ---------------------------------------------------------------------------
# e42s01t2 — RED: the bound-peer registry (bind, lease, expiry, trusted)
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_registry_binds_an_ip_for_the_lease():
    clock = FakeClock()
    registry = pairing.PeerRegistry(lease_seconds=60, clock=clock)
    assert registry.is_bound("10.0.0.5") is False
    registry.bind("10.0.0.5")
    assert registry.is_bound("10.0.0.5") is True


def test_registry_lease_expires():
    clock = FakeClock()
    registry = pairing.PeerRegistry(lease_seconds=60, clock=clock)
    registry.bind("10.0.0.5")
    clock.advance(61)
    assert registry.is_bound("10.0.0.5") is False


def test_registry_trusted_peers_are_always_bound():
    registry = pairing.PeerRegistry(trusted=["192.168.1.9"])
    assert registry.is_bound("192.168.1.9") is True


def test_registry_unrelated_ip_is_never_bound():
    registry = pairing.PeerRegistry(trusted=["192.168.1.9"])
    assert registry.is_bound("10.0.0.5") is False


def test_registry_unbind_and_sweep_drop_entries():
    clock = FakeClock()
    registry = pairing.PeerRegistry(lease_seconds=60, clock=clock)
    registry.bind("10.0.0.5")
    registry.bind("10.0.0.6")
    registry.unbind("10.0.0.5")
    clock.advance(61)
    registry.sweep()
    assert registry.is_bound("10.0.0.5") is False
    assert registry.is_bound("10.0.0.6") is False
    assert registry.bound_count() == 0


def test_boot_creates_the_peer_registry(monkeypatch):
    monkeypatch.setattr(viosc, "SimpleUDPClient", FakeClient)
    values = make_values(pairing_trusted_peers=["10.0.0.5"], pairing_lease_seconds=30)
    viosc.boot(values)
    assert viosc.PEER_REGISTRY.is_bound("10.0.0.5") is True
    assert viosc.PEER_REGISTRY.is_bound("10.0.0.6") is False


# ---------------------------------------------------------------------------
# e58s01t1 — RED: the pairing code persists across boot and rotates manually
# ---------------------------------------------------------------------------


def test_pairing_code_persists_across_boot(monkeypatch, tmp_path):
    monkeypatch.setattr(viosc, "SimpleUDPClient", FakeClient)
    cfg_path = tmp_path / "config.json"
    viosc.boot(
        make_values(pairing_enabled=True, pairing_code_length=4),
        save_path=str(cfg_path),
    )
    first = viosc.PAIRING_CODE
    assert first.isdigit() and len(first) == 4
    loaded = json.loads(cfg_path.read_text())
    assert loaded["pairing_code"] == first
    # a second boot reads the SAME code (no regeneration)
    values, _, _ = config.load_effective(str(cfg_path))
    viosc.boot(values)
    assert first == viosc.PAIRING_CODE


def test_pairing_code_reuses_a_valid_persisted_code(monkeypatch, tmp_path):
    monkeypatch.setattr(viosc, "SimpleUDPClient", FakeClient)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps({"pairing_code": "1234", "pairing_code_created_at": time.time()})
    )
    values, _, _ = config.load_effective(str(cfg_path))
    viosc.boot(values)
    assert viosc.PAIRING_CODE == "1234"


def test_pairing_code_empty_when_disabled_with_save(monkeypatch, tmp_path):
    monkeypatch.setattr(viosc, "SimpleUDPClient", FakeClient)
    cfg_path = tmp_path / "config.json"
    viosc.boot(make_values(pairing_enabled=False), save_path=str(cfg_path))
    assert viosc.PAIRING_CODE == ""


def test_rotate_pairing_code_regenerates_and_persists(monkeypatch, tmp_path):
    monkeypatch.setattr(viosc, "SimpleUDPClient", FakeClient)
    cfg_path = tmp_path / "config.json"
    viosc.boot(
        make_values(pairing_enabled=True, pairing_code_length=4),
        save_path=str(cfg_path),
    )
    first = viosc.PAIRING_CODE
    new = viosc.rotate_pairing_code(str(cfg_path))
    assert new != first
    assert new == viosc.PAIRING_CODE
    loaded = json.loads(cfg_path.read_text())
    assert loaded["pairing_code"] == new
    assert viosc.PAIRING_GATE.code == new


def test_load_boot_config_persists_the_pairing_code(monkeypatch, tmp_path):
    """main()'s boot path must persist the code, or a restart rotates it.

    The e58s01 bug: boot(save_path) existed and was tested, but main() called
    boot(values) without the path, so a restart through the GUI/HTTP never wrote
    the code and generated a fresh one (the operator lost the pairing).
    """
    monkeypatch.setattr(viosc, "SimpleUDPClient", FakeClient)
    cfg_path = tmp_path / "config.json"

    viosc.load_boot_config(str(cfg_path))
    first = viosc.PAIRING_CODE
    assert first and first.isdigit()
    assert json.loads(cfg_path.read_text())["pairing_code"] == first

    # a restart (main() again) reloads the SAME code
    viosc.load_boot_config(str(cfg_path))
    assert first == viosc.PAIRING_CODE
