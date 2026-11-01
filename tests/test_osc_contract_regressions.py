"""Frozen-OSC-surface regression guards (e41s01 task 3).

e41s01 changed WHEN the state broadcast goes out (coalesced inside a window),
so the surfaces a mistake would silently break are the ones that share the
handler: the verbatim ``/vimix/*`` passthrough (the frozen contract's upstream)
and the monitor registry. The suite had no coverage for either — these are
characterisation tests: they pin the CURRENT behaviour so the coalescing can
never quietly reroute or swallow it.

No Vimix and no network: the handler is called directly and both OSC clients are
recording fakes.
"""

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


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(viosc, "_last_state_broadcast_at", 0.0, raising=False)
    monkeypatch.setattr(viosc, "_state_broadcast_pending", False, raising=False)
    monkeypatch.setattr(viosc, "ui_reply_client", FakeUDPClient(viosc.UI_IP, viosc.REPLY_PORT))
    monkeypatch.setattr(viosc, "forward_client", FakeUDPClient("127.0.0.1", 7000))
    viosc.vimix_data.clear()
    viosc.source_current = None
    saved_monitors = dict(viosc.monitored_sources)
    viosc.monitored_sources.clear()
    yield
    viosc.vimix_data.clear()
    viosc.monitored_sources.clear()
    viosc.monitored_sources.update(saved_monitors)


def _handler(port=None):
    return viosc.create_osc_handler(port if port is not None else viosc.LISTEN_PORT)


def test_vimix_commands_are_forwarded_verbatim():
    _handler()(("10.0.0.2", 5), "/vimix/clip.mp4/alpha", 0.5)

    assert viosc.forward_client.messages == [("/vimix/clip.mp4/alpha", [0.5])]


def test_vimix_multi_argument_command_keeps_its_payload():
    _handler()(("10.0.0.2", 5), "/vimix/clip.mp4/position", 0.25, 0.75)

    assert viosc.forward_client.messages == [("/vimix/clip.mp4/position", [0.25, 0.75])]


def test_viosc_addresses_are_not_forwarded_to_vimix():
    _handler()(("10.0.0.2", 5), "/viosc/thumb/clip.mp4", "all")

    assert all(address.startswith("/vimix/") for address, _ in viosc.forward_client.messages)


def test_monitor_subscribe_and_stop_unchanged():
    handler = _handler()

    handler(("10.0.0.2", 5), "/viosc/monitor/clip.mp4", "alpha", "play")
    assert viosc.monitored_sources["clip.mp4"] == ["alpha", "play"]
    assert viosc.forward_client.messages[-1] == (
        "/vimix/clip.mp4/get",
        ["alpha", "play"],
    )

    handler(("10.0.0.2", 5), "/viosc/monitor/clip.mp4")
    assert "clip.mp4" not in viosc.monitored_sources


def test_monitor_subscribe_still_reaches_the_ui():
    _handler()(("10.0.0.2", 5), "/viosc/monitor/clip.mp4", "alpha")

    sent = [
        message for message in viosc.ui_reply_client.messages if message[0] == "/viosc/replydata"
    ]
    assert len(sent) == 1
