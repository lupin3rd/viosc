"""Legacy `/viosc/*` UDP output on macOS (e03s04).

Darwin caps one datagram at net.inet.udp.maxdgram (9216) unless the sender
raises SO_SNDBUF. The small request lanes stay for VJmix 1.1.x compatibility;
the two large output lanes are made best-effort and logged instead of raising.
"""

import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import viosc


class FakeSock:
    def __init__(self):
        self.options = []

    def setsockopt(self, level, option, value):
        self.options.append((level, option, value))


class FakeClient:
    def __init__(self, error=None):
        self._sock = FakeSock()
        self.error = error
        self.messages = []

    def send_message(self, address, args):
        if self.error:
            raise self.error
        self.messages.append((address, list(args)))


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    viosc._legacy_udp_error_at = 0.0
    viosc.vimix_data.clear()
    yield
    viosc.vimix_data.clear()


def test_darwin_raises_the_ui_socket_send_buffer(monkeypatch):
    monkeypatch.setattr(viosc.sys, "platform", "darwin")
    client = FakeClient()

    viosc._tune_ui_client(client)

    assert (
        socket.SOL_SOCKET,
        socket.SO_SNDBUF,
        viosc.MAX_LEGACY_UDP_PAYLOAD,
    ) in client._sock.options


def test_linux_does_not_tune_the_ui_socket(monkeypatch):
    monkeypatch.setattr(viosc.sys, "platform", "linux")
    client = FakeClient()

    viosc._tune_ui_client(client)

    assert client._sock.options == []


def test_a_thumbnail_send_error_is_logged_not_raised(monkeypatch, capsys):
    entry = viosc.create_empty_vimix_entry()
    entry["name"] = "clipA"
    entry["media_kind"] = "video"
    entry["thumbnails"] = [b"jpeg"]
    viosc.vimix_data[0] = entry
    bad = FakeClient(error=OSError(40, "Message too long"))
    monkeypatch.setattr(viosc, "ui_reply_client", bad)
    monkeypatch.setattr(viosc, "_ui_allowed", lambda: True)

    viosc.send_thumbnail_blob("clipA", "all")  # must not raise

    out = capsys.readouterr().out
    assert "LEGACY UDP" in out


def test_a_broadcast_error_is_logged_not_raised(monkeypatch, capsys):
    entry = viosc.create_empty_vimix_entry()
    entry["name"] = "clipA"
    viosc.vimix_data[0] = entry
    bad = FakeClient(error=OSError(40, "Message too long"))
    monkeypatch.setattr(viosc, "ui_reply_client", bad)
    monkeypatch.setattr(viosc, "_ui_allowed", lambda: True)
    monkeypatch.setattr(viosc, "lane_active", lambda *a, **k: False)

    viosc.broadcast_vimix_state()  # must not raise

    out = capsys.readouterr().out
    assert "LEGACY UDP" in out
