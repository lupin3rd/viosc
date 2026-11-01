"""Roster integrity: whole datagrams, complete-round prune, discovery probe (e03s01).

The Vimix roster is ONE UDP datagram built by sendSourcesStatus(). Python's
socketserver reads 8192 B by default, so a 100-source bundle was cut before
python-osc could parse it; current/0 started a prune round and the tail was
deleted. These tests pin the 64 KB intake, the completeness rule, the
large-drop probe and the index probe that rebuilds a lost roster.
"""

import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from pythonosc.osc_bundle_builder import IMMEDIATELY, OscBundleBuilder
from pythonosc.osc_message_builder import OscMessageBuilder

import viosc


class Recorder:
    def __init__(self):
        self.messages = []

    def send_message(self, address, args):
        self.messages.append((address, list(args)))


def _bundle(count: int, prefix: str = "source") -> bytes:
    bundle = OscBundleBuilder(IMMEDIATELY)
    for index in range(count):
        messages = (
            (f"/vimix/current/{index}", 1.0),
            (f"/vimix/{index}/alpha", 1.0),
            (f"/vimix/{index}/name", f"{prefix}-{index:03d}"),
            (f"/vimix/{index}/id", 10_000 + index),
        )
        for address, argument in messages:
            builder = OscMessageBuilder(address)
            builder.add_arg(argument)
            bundle.add_content(builder.build())
    return bundle.build().dgram


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    viosc.vimix_data.clear()
    viosc.known_indices.clear()
    viosc.seen_indices.clear()
    viosc.seen_current.clear()
    viosc.seen_id.clear()
    viosc._roster_missed.clear()
    viosc._probe_active = False
    viosc._probe_last_at = 0.0
    viosc._probe_requested.clear()
    viosc._probe_found.clear()
    viosc._awaiting_roster = False
    viosc._sync_sent_at = 0.0
    viosc._last_sync_sent = 0.0
    viosc.sync_round = 0
    if viosc.prune_timer is not None:
        viosc.prune_timer.cancel()
        viosc.prune_timer = None
    monkeypatch.setattr(viosc, "PRUNE_DELAY_SEC", 30.0)
    monkeypatch.setattr(viosc, "request_uri_once", lambda *a, **k: None)
    monkeypatch.setattr(viosc, "schedule_state_broadcast", lambda *a, **k: False)
    monkeypatch.setattr(viosc, "forward_client", Recorder())
    yield
    viosc.vimix_data.clear()
    viosc.known_indices.clear()
    viosc.seen_indices.clear()
    viosc.seen_current.clear()
    viosc.seen_id.clear()
    viosc._roster_missed.clear()
    viosc._probe_active = False
    if viosc.prune_timer is not None:
        viosc.prune_timer.cancel()
        viosc.prune_timer = None


def test_the_osc_server_reads_a_whole_datagram():
    server = viosc.create_osc_server("127.0.0.1", 0, role_port=viosc.FROMVIMIX_PORT)
    try:
        assert server.max_packet_size == viosc.MAX_OSC_DATAGRAM_BYTES
    finally:
        server.server_close()


def test_an_80_source_bundle_arrives_whole():
    """The ~8.7 KB bundle is cut at 8192 by the old server and fits macOS UDP."""
    server = viosc.create_osc_server("127.0.0.1", 0, role_port=viosc.FROMVIMIX_PORT)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(_bundle(80), ("127.0.0.1", port))
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and len(viosc.vimix_data) < 80:
            time.sleep(0.01)
        assert len(viosc.vimix_data) == 80, "the tail must survive the intake"
        assert viosc.vimix_data[79]["name"] == "source-079"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_an_incomplete_round_never_prunes(monkeypatch):
    for index in range(3):
        entry = viosc.create_empty_vimix_entry()
        entry["name"] = f"src{index}"
        viosc.vimix_data[index] = entry
    viosc.seen_current.update({0, 1})
    viosc.seen_id.add(0)
    probed = []
    monkeypatch.setattr(viosc, "start_roster_probe", lambda *a, **k: probed.append(True))

    viosc.prune_stale_sources(viosc.sync_round)

    assert set(viosc.vimix_data) == {0, 1, 2}, "a truncated round must not delete"
    assert probed, "the probe is the recovery path"


def test_a_complete_small_removal_prunes():
    for index in range(3):
        entry = viosc.create_empty_vimix_entry()
        entry["name"] = f"src{index}"
        viosc.vimix_data[index] = entry
    viosc.seen_current.update({0, 1})
    viosc.seen_id.update({0, 1})

    viosc.prune_stale_sources(viosc.sync_round)

    assert set(viosc.vimix_data) == {0, 1}


def test_a_large_drop_waits_for_the_probe(monkeypatch):
    for index in range(10):
        entry = viosc.create_empty_vimix_entry()
        entry["name"] = f"src{index:02d}"
        viosc.vimix_data[index] = entry
    viosc.seen_current.update(range(5))
    viosc.seen_id.update(range(5))
    probed = []
    monkeypatch.setattr(viosc, "start_roster_probe", lambda *a, **k: probed.append(True))

    viosc.prune_stale_sources(viosc.sync_round)

    assert len(viosc.vimix_data) == 10, "a cut can look complete; confirm first"
    assert probed


def test_the_probe_rebuilds_a_lost_roster_and_prunes(monkeypatch):
    for index in range(3):
        entry = viosc.create_empty_vimix_entry()
        entry["name"] = f"old{index}"
        viosc.vimix_data[index] = entry
    monkeypatch.setattr(viosc, "schedule_state_broadcast", lambda *a, **k: False)

    assert viosc.start_roster_probe(now=100.0) is True
    handler = viosc.create_osc_handler(viosc.FROMVIMIX_PORT)
    for index in (0, 1, 3):
        handler(("127.0.0.1", 7001), f"/vimix/{index}/name", f"new{index}")
    assert viosc.probe_poll(now=101.1) == 3
    assert set(viosc.vimix_data) == {0, 1, 3}, "index 2 is the removed source"
    assert viosc.vimix_data[3]["name"] == "new3"


def test_the_probe_rate_limit_avoids_a_storm():
    assert viosc.start_roster_probe(now=100.0) is True
    viosc.probe_poll(now=101.1)  # finalize the first (no replies: no prune)
    assert viosc.start_roster_probe(now=101.2) is False, "min interval still holds"
    assert viosc.start_roster_probe(now=106.0) is True
