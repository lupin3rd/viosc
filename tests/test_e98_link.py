"""The HTTP command stream (e98s01): chunked frames -> forward, echo, session sync.

Two levels, on purpose. The TRANSPORT tests drive the real ``http_api`` server
over a raw socket, because the chunked request body is hand-parsed there and a
mock would prove nothing. The DAEMON tests call ``viosc.apply_link_frame`` with a
recording forward client, because that is the only code that decides what leaves
towards Vimix and what lands in the mirror.
"""

import http.client
import json
import os
import socket
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import viosc
from http_api import start_http_server


class LinkServer:
    """A real preview server with a recording ``apply_link`` sink."""

    def __init__(self, gate=None):
        self.frames = []
        self.httpd = start_http_server(
            "127.0.0.1",
            0,
            {},
            lambda path: None,
            gate=gate,
            apply_link=self._sink,
        )
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def _sink(self, payload):
        frame = json.loads(payload.decode("utf-8"))
        if frame.get("raise"):
            raise ValueError("nope")
        self.frames.append(frame)

    def open(self, token=None):
        """A connected socket with the request head already written."""
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        head = [
            "POST /link HTTP/1.1",
            "Host: 127.0.0.1",
            "Transfer-Encoding: chunked",
            "Content-Type: application/x-viosc-command",
        ]
        if token:
            head.append(f"Authorization: Bearer {token}")
        sock.sendall(("\r\n".join(head) + "\r\n\r\n").encode("ascii"))
        return sock

    @staticmethod
    def frame(sock, payload):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        sock.sendall(b"%x\r\n" % len(body) + body + b"\r\n")

    @staticmethod
    def end(sock):
        sock.sendall(b"0\r\n\r\n")
        return sock.recv(4096)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture()
def link_server():
    server = LinkServer()
    yield server
    server.close()


def test_chunked_frames_arrive_in_order(link_server):
    sock = link_server.open()
    link_server.frame(sock, {"address": "/vimix/Kick/alpha", "args": [0.25]})
    link_server.frame(sock, {"address": "/vimix/current/next", "args": []})
    sock.close()
    _wait_for(lambda: len(link_server.frames) == 2)
    assert [f["address"] for f in link_server.frames] == [
        "/vimix/Kick/alpha",
        "/vimix/current/next",
    ]
    assert link_server.frames[0]["args"] == [0.25]


def test_a_frame_split_across_two_writes_still_arrives(link_server):
    """TCP is a stream: the reader must wait for the full chunk body."""
    sock = link_server.open()
    body = json.dumps({"address": "/vimix/Kick/alpha", "args": [0.5]}).encode("utf-8")
    sock.sendall(b"%x\r\n" % len(body))
    sock.sendall(body[:5])
    sock.sendall(body[5:] + b"\r\n")
    sock.close()
    _wait_for(lambda: len(link_server.frames) == 1)
    assert link_server.frames[0]["args"] == [0.5]


def test_ping_is_a_frame_but_never_a_command(link_server):
    sock = link_server.open()
    link_server.frame(sock, {"ping": True})
    link_server.frame(sock, {"address": "/vimix/Kick/alpha", "args": [0.1]})
    sock.close()
    _wait_for(lambda: len(link_server.frames) == 2)
    assert link_server.frames[0] == {"ping": True}


def test_a_clean_close_answers_204(link_server):
    sock = link_server.open()
    link_server.frame(sock, {"address": "/vimix/Kick/alpha", "args": [0.1]})
    response = link_server.end(sock)
    sock.close()
    assert b"204" in response
    _wait_for(lambda: len(link_server.frames) == 1)


def test_a_raising_frame_closes_the_stream_and_the_daemon_survives(link_server):
    sock = link_server.open()
    link_server.frame(sock, {"raise": True})
    sock.settimeout(3)
    assert sock.recv(4096) == b""  # the server closed: no half-applied stream
    sock.close()
    # The server is still alive: a fresh stream works.
    other = link_server.open()
    link_server.frame(other, {"address": "/vimix/Kick/alpha", "args": [0.2]})
    link_server.end(other)
    other.close()
    _wait_for(lambda: len(link_server.frames) == 1)


def test_without_a_token_nothing_reaches_the_sink(link_server):
    class Gate:
        enabled = True

        def verify(self, token):
            return token == "good"

        def authenticate(self, code, ip):
            return None

    server = LinkServer(gate=Gate())
    try:
        sock = server.open()
        sock.settimeout(3)
        head = sock.recv(4096)
        assert b"401" in head
        sock.close()
        assert server.frames == []
        # A paired stream is accepted.
        good = server.open(token="good")
        server.frame(good, {"address": "/vimix/Kick/alpha", "args": [0.3]})
        server.end(good)
        good.close()
        _wait_for(lambda: len(server.frames) == 1)
    finally:
        server.close()


def test_a_non_chunked_body_is_refused(link_server):
    conn = http.client.HTTPConnection("127.0.0.1", link_server.port, timeout=5)
    try:
        conn.request(
            "POST",
            "/link",
            body=b'{"address": "/vimix/Kick/alpha", "args": [0.1]}',
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        assert resp.status == 400
    finally:
        conn.close()
    assert link_server.frames == []


# -- the daemon side -------------------------------------------------------


class FakeForward:
    def __init__(self):
        self.messages = []

    def send_message(self, address, args):
        self.messages.append((address, list(args)))


@pytest.fixture()
def daemon(monkeypatch):
    """The module's forward client replaced by a recorder, and one source present."""
    forward = FakeForward()
    monkeypatch.setattr(viosc, "forward_client", forward)
    with viosc.STATE_LOCK:
        viosc.vimix_data.clear()
        viosc.vimix_data[0] = viosc.create_empty_vimix_entry()
        viosc.vimix_data[0].update({"name": "Kick", "index": 0, "alpha": 1.0})
    yield forward
    with viosc.STATE_LOCK:
        viosc.vimix_data.clear()


def test_a_command_is_forwarded_and_echoed(daemon):
    before = viosc.state_rev
    viosc.apply_link_frame(b'{"address": "/vimix/Kick/alpha", "args": [0.42]}')
    assert daemon.messages == [("/vimix/Kick/alpha", [0.42])]
    assert viosc.vimix_data[0]["alpha"] == 0.42
    assert viosc.state_rev > before


def test_a_ping_frame_is_not_forwarded_and_not_echoed(daemon):
    before = viosc.state_rev
    viosc.apply_link_frame(b'{"ping": true}')
    assert daemon.messages == []
    assert viosc.state_rev == before


def test_a_session_command_triggers_the_immediate_sync(daemon, monkeypatch):
    calls = []
    monkeypatch.setattr(viosc, "request_session_sync", lambda: calls.append(1))
    viosc.apply_link_frame(b'{"address": "/vimix/session/open", "args": ["/tmp/x.mix"]}')
    assert daemon.messages == [("/vimix/session/open", ["/tmp/x.mix"])]
    assert calls == [1]


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b'["a list"]',
        b'{"args": [1]}',
        b'{"address": "vimix/Kick/alpha", "args": [1]}',
        b'{"address": "/vimix/Kick/alpha", "args": "nope"}',
    ],
)
def test_a_bad_frame_raises_and_never_forwards(daemon, payload):
    with pytest.raises(ValueError):
        viosc.apply_link_frame(payload)
    assert daemon.messages == []


def test_an_ms_carrying_set_is_not_echoed(daemon):
    """The echo rules are e82's: an animated set must not claim the target value."""
    before = viosc.state_rev
    viosc.apply_link_frame(b'{"address": "/vimix/Kick/alpha", "args": [0.1, 500]}')
    assert daemon.messages == [("/vimix/Kick/alpha", [0.1, 500])]
    assert viosc.state_rev == before
    assert viosc.vimix_data[0]["alpha"] == 1.0


def _wait_for(predicate, timeout=3.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met in time")
