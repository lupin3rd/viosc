"""The state resource on the HTTP data plane (e41s04) — GET /state.

The state table becomes a PULL whose cadence VJmix owns, instead of a push fired
from 8 call sites on every changed vimix property. That is the whole point of the
epic's decisive change: the machine that pays the render cost (VJmix: 0.12-2.6 ms
per push, SPIKE-perf) gets to decide how often it reads.

One serializer, two transports: the HTTP resource and the /viosc/replydata
broadcast MUST be produced by the same function, or they drift. These tests pin
the body to be byte-identical to what the broadcast sends.

The server runs on an ephemeral port and is driven with http.client so a single
connection can carry two requests (keep-alive, which a regular poll needs).
"""

import http.client
import json
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import http_api
import viosc


def seed_source(idx=0, name="clip", **props):
    viosc.vimix_data.clear()
    entry = viosc.create_empty_vimix_entry()
    entry["name"] = name
    entry.update(props)
    viosc.vimix_data[idx] = entry
    return entry


@pytest.fixture(autouse=True)
def _clean():
    viosc.vimix_data.clear()
    viosc.source_current = None
    yield
    viosc.vimix_data.clear()


@pytest.fixture
def srv():
    server = http_api.start_http_server(
        "127.0.0.1",
        0,
        viosc.vimix_data,
        lambda p: None,
        None,
        viosc.state_json,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[0], server.server_address[1]
    server.shutdown()
    server.server_close()


def _get(host, port):
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request("GET", "/state")
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


def test_state_resource_is_json(srv):
    host, port = srv
    seed_source(name="clip", uri="/mnt/clip.mp4", alpha=0.5)

    status, headers, body = _get(host, port)

    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body)["sources"]["0"]["alpha"] == 0.5


def test_state_body_is_the_one_serializer(srv):
    """e98s04: the broadcast is gone; the resource IS the serializer.

    Before 1.0 the test compared the body to `/viosc/replydata`, because a drift
    between the two transports was possible. There is one transport now, so the
    contract is that the body equals `state_json()` — the same text every other
    consumer in the daemon sees.
    """
    host, port = srv
    seed_source(name="clip", uri="/mnt/clip.mp4", alpha=0.25)
    viosc.source_current = 0

    _status, _headers, body = _get(host, port)

    assert body.decode("utf-8") == viosc.state_json()


def test_state_body_excludes_thumbnails(srv):
    host, port = srv
    entry = seed_source(name="clip", uri="/mnt/clip.mp4")
    entry["thumbnails"] = [b"jpeg-bytes"]

    _status, _headers, body = _get(host, port)

    assert "thumbnails" not in json.loads(body)["sources"]["0"]


def test_state_resource_keeps_the_connection_alive(srv):
    host, port = srv
    seed_source(name="clip", uri="/mnt/clip.mp4")
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request("GET", "/state")
        first = conn.getresponse()
        first.read()
        assert first.status == 200

        # a second request on the SAME socket: a regular poll must not reconnect
        conn.request("GET", "/state")
        second = conn.getresponse()
        second.read()
        assert second.status == 200
    finally:
        conn.close()


def test_state_resource_is_404_without_a_provider():
    server = http_api.start_http_server("127.0.0.1", 0, viosc.vimix_data, lambda p: None)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[0], server.server_address[1]
        assert _get(host, port)[0] == 404
    finally:
        server.shutdown()
        server.server_close()
