"""Server-bind behaviour tests for viosc.

A busy port (a second viOSC instance, typically) must surface as a readable
log line, never as a thread traceback — the app is UI-first.
"""

import os
import sys
from typing import ClassVar

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import viosc


def test_start_server_instance_logs_bind_failure_not_traceback(capsys, monkeypatch):
    def busy_server(address, dispatcher):
        raise OSError(98, "Address already in use")

    monkeypatch.setattr(viosc, "BlockingOSCUDPServer", busy_server)

    viosc.start_server_instance("127.0.0.1", 7101)

    out = capsys.readouterr().out
    assert "cannot bind 127.0.0.1:7101" in out
    assert "another viOSC instance" in out
    assert "Traceback" not in out


# ---------------------------------------------------------------------------
# duplicate-instance startup probe (user request 2026-09-08)
# ---------------------------------------------------------------------------


def test_bind_probe_true_on_free_port():
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    assert viosc.bind_probe("127.0.0.1", port) is True


def test_bind_probe_false_on_busy_port():
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    try:
        assert viosc.bind_probe("127.0.0.1", port) is False
    finally:
        sock.close()


def test_warn_busy_ports_emits_duplicate_hint(capsys, monkeypatch):
    monkeypatch.setattr(viosc, "bind_probe", lambda ip, port, sock_type=0: False)

    viosc.warn_busy_ports()

    out = capsys.readouterr().out
    assert "another viOSC instance" in out
    assert "[STARTUP]" in out


# ---------------------------------------------------------------------------
# receive-buffer sizing (e41s01)
#
# socketserver leaves SO_RCVBUF at the kernel default (208 KiB here), which is
# small for the bursts this daemon receives. Every receive socket the daemon
# creates must get the named buffer explicitly, so the value is a reviewable
# decision instead of a host-dependent accident.
# ---------------------------------------------------------------------------


class RecordingSocket:
    """Stand-in for a server socket that records setsockopt calls."""

    def __init__(self):
        self.options = []

    def getsockopt(self, level, option):
        return 0

    def setsockopt(self, level, option, value):
        self.options.append((level, option, value))


class RecordingServer:
    """Stand-in for BlockingOSCUDPServer that never serves."""

    instances: ClassVar[list["RecordingServer"]] = []

    def __init__(self, address, dispatcher):
        self.address = address
        self.socket = RecordingSocket()
        RecordingServer.instances.append(self)

    def serve_forever(self):
        return


def test_start_server_instance_sizes_the_receive_buffer(monkeypatch):
    import socket

    RecordingServer.instances = []
    monkeypatch.setattr(viosc, "BlockingOSCUDPServer", RecordingServer)

    viosc.start_server_instance("0.0.0.0", 7101)
    viosc.start_server_instance("127.0.0.1", 7001)

    assert len(RecordingServer.instances) == 2
    for server in RecordingServer.instances:
        assert (
            socket.SOL_SOCKET,
            socket.SO_RCVBUF,
            viosc.RECV_BUFFER_BYTES,
        ) in server.socket.options


def test_receive_buffer_never_shrinks_the_socket():
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        before = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        viosc.size_receive_buffer(sock)
        after = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    finally:
        sock.close()

    assert after >= before
