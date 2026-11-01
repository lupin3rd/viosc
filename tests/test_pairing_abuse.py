"""Pairing abuse resistance tests for viosc (e42s03).

A 4-digit code has 10^4 combinations: without limits it is brute-forced in
seconds. These tests cover the per-peer lockout, the global failure window with
code regeneration and auth freeze, the rotation on every start, and the daemon
wiring that keeps PAIRING_CODE in sync when the code regenerates.

Pure logic with an injected clock — no sockets, no sleeping.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import pairing
import viosc


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_gate(
    *, code="1234", lock_after=3, lock_seconds=60, global_lock_after=10, on_regenerate=None
):
    clock = FakeClock()
    registry = pairing.PeerRegistry(lease_seconds=300, clock=clock)
    gate = pairing.PairingGate(
        True,
        code,
        registry,
        300,
        clock,
        lock_after=lock_after,
        lock_seconds=lock_seconds,
        global_lock_after=global_lock_after,
        on_regenerate=on_regenerate,
    )
    return gate, clock


# ---------------------------------------------------------------------------
# e42s03t1 — RED: per-peer lockout
# ---------------------------------------------------------------------------


def test_peer_is_locked_after_the_threshold_and_unlocked_after_the_window():
    gate, clock = make_gate(lock_after=3, lock_seconds=60)
    for _ in range(3):
        assert gate.authenticate("0000", "10.0.0.5") is None
    # locked: even the RIGHT code is rejected while the lock holds
    assert gate.authenticate("1234", "10.0.0.5") is None
    clock.advance(61)
    assert gate.authenticate("1234", "10.0.0.5") is not None


def test_lockout_is_per_peer():
    gate, _clock = make_gate(lock_after=3)
    for _ in range(3):
        gate.authenticate("0000", "10.0.0.5")
    assert gate.authenticate("1234", "10.0.0.6") is not None


def test_a_success_resets_the_failure_counter():
    gate, _clock = make_gate(lock_after=3)
    gate.authenticate("0000", "10.0.0.5")
    gate.authenticate("0000", "10.0.0.5")
    assert gate.authenticate("1234", "10.0.0.5") is not None
    # the counter reset: two more failures must not lock
    gate.authenticate("0000", "10.0.0.5")
    gate.authenticate("0000", "10.0.0.5")
    assert gate.authenticate("1234", "10.0.0.5") is not None


# ---------------------------------------------------------------------------
# e42s03t2 — RED: global lock, regeneration and auth freeze
# ---------------------------------------------------------------------------


def test_global_threshold_regenerates_the_code_and_freezes_auth():
    seen = []
    gate, clock = make_gate(global_lock_after=5, on_regenerate=seen.append)
    before = gate.code
    for i in range(5):
        gate.authenticate("0000", f"10.0.0.{i}")
    assert gate.code != before
    assert seen == [gate.code]
    # frozen: the NEW correct code is rejected until the window ends
    assert gate.authenticate(gate.code, "10.0.0.9") is None
    clock.advance(61)
    assert gate.authenticate(gate.code, "10.0.0.9") is not None


def test_global_failures_outside_the_window_do_not_regenerate():
    gate, clock = make_gate(global_lock_after=5, lock_seconds=60)
    before = gate.code
    for i in range(4):
        gate.authenticate("0000", f"10.0.0.{i}")
    clock.advance(61)
    gate.authenticate("0000", "10.0.0.8")
    assert gate.code == before


# ---------------------------------------------------------------------------
# e42s03t3 — RED: config, rotation and the daemon wiring
# ---------------------------------------------------------------------------


def test_abuse_config_defaults_and_visibility():
    assert config.DEFAULTS["pairing_lock_after"] == 5
    assert config.DEFAULTS["pairing_lock_seconds"] == 60
    assert config.DEFAULTS["pairing_global_lock_after"] == 20
    for key in ("pairing_lock_after", "pairing_lock_seconds", "pairing_global_lock_after"):
        assert key in config.FIELD_SPEC
        assert hasattr(viosc, config.daemon_attr(key))


def test_code_rotation_across_starts(monkeypatch):
    class FakeClient:
        def __init__(self, *args):
            pass

        def send_message(self, *args):
            pass

    monkeypatch.setattr(viosc, "SimpleUDPClient", FakeClient)
    values, _, _ = config.effective({}, {})
    codes = set()
    for _ in range(5):
        viosc.boot(values)
        codes.add(viosc.PAIRING_CODE)
    assert len(codes) > 1


def test_daemon_keeps_pairing_code_in_sync_on_regeneration(monkeypatch):
    class FakeClient:
        def __init__(self, *args):
            pass

        def send_message(self, *args):
            pass

    monkeypatch.setattr(viosc, "SimpleUDPClient", FakeClient)
    values, _, _ = config.effective({"pairing_global_lock_after": 3})
    viosc.boot(values)
    before = viosc.PAIRING_CODE
    for i in range(3):
        viosc.PAIRING_GATE.authenticate("0000", f"10.0.0.{i}")
    assert before != viosc.PAIRING_CODE
    assert viosc.PAIRING_GATE.code == viosc.PAIRING_CODE
