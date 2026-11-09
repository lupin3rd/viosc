"""Vimix-activity marker tests for viosc (e01s04).

mark_vimix_activity() stamps last_vimix_seen (injected clock) and bumps the
monotonic vimix_messages counter whenever a packet from Vimix is ingested on
FROMVIMIX_PORT; vimix_idle_seconds() turns the stamp into a human "talking
to Vimix?" signal for the GUI status row. No OSC traffic, no contract change.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import viosc


def test_no_activity_before_any_mark():
    viosc.last_vimix_seen = None
    viosc.vimix_messages = 0
    assert viosc.vimix_idle_seconds(now=1000.0) is None


def test_mark_stamps_clock_and_increments_counter():
    viosc.last_vimix_seen = None
    viosc.vimix_messages = 0

    viosc.mark_vimix_activity(now=500.0)
    viosc.mark_vimix_activity(now=501.0)

    assert viosc.last_vimix_seen == 501.0
    assert viosc.vimix_messages == 2


def test_idle_seconds_advance_with_clock():
    viosc.last_vimix_seen = None
    viosc.vimix_messages = 0
    viosc.mark_vimix_activity(now=500.0)

    assert viosc.vimix_idle_seconds(now=500.0) == 0.0
    assert viosc.vimix_idle_seconds(now=512.5) == 12.5


def test_ingest_handler_marks_vimix_packets(monkeypatch):
    from pythonosc.osc_server import BlockingOSCUDPServer  # noqa: F401

    class FakeUDPClient:
        def __init__(self, *args):
            pass

        def send_message(self, *args):
            pass

    monkeypatch.setattr(viosc, "SimpleUDPClient", FakeUDPClient)
    monkeypatch.setattr(viosc.time, "time", lambda: 4242.0)
    viosc.last_vimix_seen = None
    viosc.vimix_messages = 0

    handler = viosc.create_osc_handler(viosc.FROMVIMIX_PORT)
    handler(("127.0.0.1", 7001), "/vimix/current/0", 1)

    assert viosc.vimix_messages == 1
    assert viosc.last_vimix_seen == 4242.0


def test_non_vimix_ingest_does_not_mark():
    viosc.last_vimix_seen = None
    viosc.vimix_messages = 0

    handler = viosc.create_osc_handler(viosc.FROMVIMIX_PORT)
    handler(("127.0.0.1", 7001), "/viosc/sync/0", "all")

    assert viosc.vimix_messages == 0
    assert viosc.last_vimix_seen is None
