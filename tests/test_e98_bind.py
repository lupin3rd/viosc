"""The HTTP listener is the only surface (e98s04).

Before 1.0 a busy `:8686` was a degraded *preview* while the OSC lanes kept the
link alive — the daemon kept working and the operator had a fallback. There is no
fallback now, so the same failure must be loud, retried, and visible in the link
status instead of producing a daemon that looks healthy and answers nobody.
"""

import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import http_api
import viosc
from logbus import bus as log_bus


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(viosc, "_http_up", False)
    monkeypatch.setattr(viosc, "http_link_error", "")
    yield


def _lines():
    return [(text, level) for text, level in log_bus.tail(50)]


def test_a_busy_port_still_fails_the_bind():
    """The failure is real — the retry loop is what makes it survivable."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.listen(1)
    try:
        with pytest.raises(OSError):
            http_api.start_http_server("127.0.0.1", port, {}, lambda p: None)
    finally:
        sock.close()


def test_the_status_reports_starting_then_up(monkeypatch):
    assert viosc.http_link_status() == "starting"
    monkeypatch.setattr(viosc, "_http_up", True)
    assert viosc.http_link_status() == "up"


def test_the_status_reports_a_bind_error(monkeypatch):
    monkeypatch.setattr(viosc, "http_link_error", "cannot bind 0.0.0.0:8686 (busy)")
    assert viosc.http_link_status().startswith("cannot bind")


def test_a_busy_port_is_retried_loudly_and_then_binds():
    attempts = []
    served = []

    class FakeServer:
        def serve_forever(self):
            served.append(1)

    def factory():
        attempts.append(1)
        if len(attempts) < 3:
            raise OSError(98, "Address already in use")
        return FakeServer()

    viosc.start_http_forever(factory, retry_s=0.0)

    assert len(attempts) == 3
    assert served == [1], "the server runs as soon as the port frees up"
    assert viosc.http_link_status() == "up"
    errors = [text for text, level in _lines() if level == "error" and "HTTP ERROR" in text]
    assert len(errors) == 2, "every failed attempt is one error line"
    assert all("retrying" in text for text in errors)


def test_a_factory_that_cannot_even_build_is_also_retried(monkeypatch):
    attempts = []

    class FakeServer:
        def serve_forever(self):
            pass

    def factory():
        attempts.append(1)
        if len(attempts) < 2:
            raise RuntimeError("no handler")
        return FakeServer()

    viosc.start_http_forever(factory, retry_s=0.0)

    assert len(attempts) == 2
    assert viosc.http_link_status() == "up"


def test_the_boot_warning_includes_the_http_port(monkeypatch):
    """A second instance must be told at boot that it owns no link."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.listen(1)
    monkeypatch.setattr(viosc, "HTTP_IP", "127.0.0.1")
    monkeypatch.setattr(viosc, "HTTP_PORT", port)
    try:
        viosc.warn_busy_ports()
    finally:
        sock.close()
    errors = [text for text, level in log_bus.tail(20) if level == "error"]
    assert any("HTTP 127.0.0.1" in text and "owns the link" in text for text in errors), errors
