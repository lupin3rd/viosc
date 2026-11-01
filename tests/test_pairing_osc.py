"""OSC pairing enforcement tests for viosc (e42s01).

Covers the additive `/viosc/auth` binding, the drop of an unbound peer's input
BEFORE the `/vimix` forward, the forwarding of a bound peer, and the output
gating (the state broadcast and the thumbnail replies reach only a bound UI).

The OSC handler is called directly (no sockets); clients are recorders.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pairing
import viosc


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Recorder:
    def __init__(self):
        self.sent = []

    def send_message(self, address, args=None):
        self.sent.append((address, args))


def armed_gate(code="1234", trusted=None):
    clock = FakeClock()
    registry = pairing.PeerRegistry(lease_seconds=300, trusted=trusted, clock=clock)
    gate = pairing.PairingGate(True, code, registry, 300, clock)
    return gate, registry


def install_gate(monkeypatch, gate):
    monkeypatch.setattr(viosc, "PAIRING_GATE", gate)


# ---------------------------------------------------------------------------
# e42s01t4 — RED: /viosc/auth and input gating
# ---------------------------------------------------------------------------


def test_osc_auth_with_the_right_code_binds_the_sender(monkeypatch):
    gate, registry = armed_gate()
    install_gate(monkeypatch, gate)
    handler = viosc.create_osc_handler(viosc.LISTEN_PORT)
    handler(("10.0.0.5", 1234), "/viosc/auth", "1234")
    assert registry.is_bound("10.0.0.5") is True


def test_osc_auth_with_a_wrong_code_does_not_bind(monkeypatch):
    gate, registry = armed_gate()
    install_gate(monkeypatch, gate)
    handler = viosc.create_osc_handler(viosc.LISTEN_PORT)
    handler(("10.0.0.5", 1234), "/viosc/auth", "0000")
    assert registry.is_bound("10.0.0.5") is False


def test_osc_unbound_peer_is_dropped_before_the_forward(monkeypatch):
    gate, _registry = armed_gate()
    install_gate(monkeypatch, gate)
    recorder = Recorder()
    monkeypatch.setattr(viosc, "forward_client", recorder)
    handler = viosc.create_osc_handler(viosc.LISTEN_PORT)
    handler(("10.0.0.5", 1234), "/vimix/clip/alpha", 0.5)
    assert recorder.sent == []


def test_osc_bound_peer_is_forwarded(monkeypatch):
    gate, _registry = armed_gate()
    install_gate(monkeypatch, gate)
    recorder = Recorder()
    monkeypatch.setattr(viosc, "forward_client", recorder)
    handler = viosc.create_osc_handler(viosc.LISTEN_PORT)
    handler(("10.0.0.5", 1234), "/viosc/auth", "1234")
    handler(("10.0.0.5", 1234), "/vimix/clip/alpha", 0.5)
    assert recorder.sent == [("/vimix/clip/alpha", [0.5])]


def test_osc_trusted_peer_is_forwarded_without_auth(monkeypatch):
    gate, _registry = armed_gate(trusted=["10.0.0.7"])
    install_gate(monkeypatch, gate)
    recorder = Recorder()
    monkeypatch.setattr(viosc, "forward_client", recorder)
    handler = viosc.create_osc_handler(viosc.LISTEN_PORT)
    handler(("10.0.0.7", 1234), "/vimix/clip/alpha", 0.5)
    assert recorder.sent == [("/vimix/clip/alpha", [0.5])]


def test_osc_pairing_disabled_forwards_without_auth(monkeypatch):
    gate = pairing.PairingGate(False, "", pairing.PeerRegistry(), 300, FakeClock())
    install_gate(monkeypatch, gate)
    recorder = Recorder()
    monkeypatch.setattr(viosc, "forward_client", recorder)
    handler = viosc.create_osc_handler(viosc.LISTEN_PORT)
    handler(("10.0.0.5", 1234), "/vimix/clip/alpha", 0.5)
    assert recorder.sent == [("/vimix/clip/alpha", [0.5])]


# ---------------------------------------------------------------------------
# e42s01t4 — RED: output gating (state broadcast + thumbnail replies)
# ---------------------------------------------------------------------------


def test_state_broadcast_reaches_only_a_bound_ui(monkeypatch):
    gate, _registry = armed_gate()
    install_gate(monkeypatch, gate)
    monkeypatch.setattr(viosc, "UI_IP", "10.0.0.9")
    recorder = Recorder()
    monkeypatch.setattr(viosc, "ui_reply_client", recorder)
    viosc.broadcast_vimix_state()
    assert recorder.sent == []
    gate.authenticate("1234", "10.0.0.9")
    viosc.broadcast_vimix_state()
    assert len(recorder.sent) == 1


def test_thumbnail_reply_reaches_only_a_bound_ui(monkeypatch):
    gate, _registry = armed_gate()
    install_gate(monkeypatch, gate)
    monkeypatch.setattr(viosc, "UI_IP", "10.0.0.9")
    recorder = Recorder()
    monkeypatch.setattr(viosc, "ui_reply_client", recorder)
    monkeypatch.setattr(viosc, "thumbnails_for", lambda target: [b"jpeg-bytes"])
    viosc.vimix_data.clear()
    viosc.vimix_data[0] = {"name": "clip", "thumbnails": [b"jpeg-bytes"]}
    viosc.send_thumbnail_blob("clip", 0)
    assert recorder.sent == []
    gate.authenticate("1234", "10.0.0.9")
    viosc.send_thumbnail_blob("clip", 0)
    assert recorder.sent == [("/viosc/replythumb/clip/0", b"jpeg-bytes")]
