"""State-broadcast coalescing tests (e41s01).

A burst of vimix property changes used to produce one whole-table
``/viosc/replydata`` serialization AND datagram per change: one monitor round
over N sources x M properties could cost up to N*M full-table JSON dumps, each
one taxing the consumer's UI (VJmix, SPIKE-perf 0.12-2.6 ms per push). The
broadcast is now coalesced inside a short window — the first change of a burst
goes out immediately (leading edge), the following ones collapse into ONE
trailing send once the window has elapsed — and the payload is unchanged.

No Vimix and no network: the handler is called directly, the reply client is a
recording fake and the clock is injected (no sleeping).
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import viosc


class FakeUDPClient:
    """Recording stand-in for a python-osc UDP client."""

    def __init__(self, ip, port):
        self.ip = ip
        self.port = port
        self.messages = []

    def send_message(self, address, args):
        self.messages.append((address, list(args)))


class FakeClock:
    """Deterministic clock for the coalescing window (tests never sleep)."""

    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _install_clock(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(viosc.time, "time", clock)
    monkeypatch.setattr(viosc.time, "monotonic", clock)
    return clock


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # raising=False so the RED run fails on the asserted BEHAVIOUR, not on a
    # missing module attribute; in GREEN these are the coalescing clock state.
    monkeypatch.setattr(viosc, "_last_state_broadcast_at", 0.0, raising=False)
    monkeypatch.setattr(viosc, "_state_broadcast_pending", False, raising=False)
    viosc.vimix_data.clear()
    viosc.source_current = None
    monkeypatch.setattr(viosc, "ui_reply_client", FakeUDPClient(viosc.UI_IP, viosc.REPLY_PORT))
    yield
    viosc.vimix_data.clear()


def _handler():
    return viosc.create_osc_handler(viosc.FROMVIMIX_PORT)


def _replydata(client):
    return [message for message in client.messages if message[0] == "/viosc/replydata"]


def test_burst_of_property_changes_sends_one_broadcast(monkeypatch):
    clock = _install_clock(monkeypatch)
    handler = _handler()

    for value in range(50):
        handler(("10.0.0.2", 5), "/vimix/1/alpha", 0.1 + value)
        clock.advance(0.001)

    assert len(_replydata(viosc.ui_reply_client)) == 1


def test_isolated_change_is_broadcast_immediately(monkeypatch):
    clock = _install_clock(monkeypatch)
    handler = _handler()

    handler(("10.0.0.2", 5), "/vimix/1/alpha", 0.5)
    clock.advance(5.0)
    handler(("10.0.0.2", 5), "/vimix/1/alpha", 0.6)

    assert len(_replydata(viosc.ui_reply_client)) == 2


def test_pending_broadcast_flushes_once_after_the_window(monkeypatch):
    clock = _install_clock(monkeypatch)
    handler = _handler()
    client = viosc.ui_reply_client

    handler(("10.0.0.2", 5), "/vimix/1/alpha", 0.1)
    clock.advance(0.001)
    handler(("10.0.0.2", 5), "/vimix/1/alpha", 0.2)
    assert len(_replydata(client)) == 1

    assert viosc.flush_state_broadcast(now=clock.now) is False
    assert len(_replydata(client)) == 1

    clock.advance(0.2)
    assert viosc.flush_state_broadcast(now=clock.now) is True
    assert len(_replydata(client)) == 2

    assert viosc.flush_state_broadcast(now=clock.now) is False
    assert len(_replydata(client)) == 2


def test_coalesced_payload_is_unchanged(monkeypatch):
    _install_clock(monkeypatch)
    viosc.vimix_data[1] = viosc.create_empty_vimix_entry()
    viosc.vimix_data[1]["name"] = "clip.mp4"
    viosc.source_current = 1

    _handler()(("10.0.0.2", 5), "/vimix/1/alpha", 0.42)

    address, args = _replydata(viosc.ui_reply_client)[0]
    payload = json.loads(args[0])
    assert address == "/viosc/replydata"
    assert set(payload) == {"rev", "current_source", "sources", "monitored"}
    assert payload["rev"] == viosc.state_rev, "e82s01: the payload carries the mirror's revision"
    assert payload["current_source"] == 1
    assert payload["sources"]["1"]["name"] == "clip.mp4"
    assert payload["sources"]["1"]["alpha"] == 0.42
    assert "thumbnails" not in payload["sources"]["1"]
