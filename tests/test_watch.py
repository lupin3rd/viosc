"""Targeted watch lane tests (e40s05).

A client subscribes to a few properties of one source at its own cadence and
gets a targeted DELTA reply (/viosc/reply/<source> <prop> <value>) at its own
address, instead of the whole-table /viosc/replydata broadcast. The monitor
registry and the broadcast stay byte-identical: this file covers the ADDITIVE
lane only (registration, cadence, coalescing, delta routing, cap).

No Vimix, no network: the handler is called directly with a fake client
address, the OSC clients and the property get are monkeypatched.
"""

import os
import sys
from typing import ClassVar

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import viosc


class FakeUDPClient:
    """Recording stand-in for a python-osc UDP client."""

    instances: ClassVar[list["FakeUDPClient"]] = []

    def __init__(self, ip, port):
        self.ip = ip
        self.port = port
        self.messages: list[tuple[str, list]] = []
        FakeUDPClient.instances.append(self)

    def send_message(self, address, args):
        self.messages.append((address, list(args)))


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    FakeUDPClient.instances = []
    viosc.watched_sources.clear()
    viosc.watch_clients.clear()
    viosc.vimix_data.clear()
    monkeypatch.setattr(viosc, "SimpleUDPClient", FakeUDPClient)
    yield
    viosc.watched_sources.clear()
    viosc.watch_clients.clear()
    viosc.vimix_data.clear()


def _handler():
    return viosc.create_osc_handler(viosc.LISTEN_PORT)


def _watch(client_address, name, *args):
    _handler()(client_address, f"/viosc/watch/{name}", *args)


def test_watch_subscribe_registers_and_drops():
    gets: list[tuple[str, list]] = []
    viosc.request_source_props = lambda name, props: gets.append((name, list(props)))
    _watch(("10.0.0.5", 51234), "clipA", 100, "seek")
    entry = viosc.watched_sources[("clipA", "10.0.0.5", 6667)]
    assert entry["props"] == ["seek"] and entry["interval_ms"] == 100
    assert gets == [("clipA", ["seek"])], "an immediate get gives fast feedback"
    _watch(("10.0.0.5", 51234), "clipA")  # no args: unsubscribe
    assert viosc.watched_sources == {}


def test_watch_index_resolves_to_the_cached_name():
    viosc.vimix_data[3] = {"name": "clipB"}
    viosc.request_source_props = lambda name, props: None
    _watch(("10.0.0.5", 1), 3, 200, "alpha")
    assert ("clipB", "10.0.0.5", 6667) in viosc.watched_sources


@pytest.mark.parametrize("cadence", [0, 10, "abc", None, -5])
def test_watch_rejects_a_bad_cadence(cadence):
    viosc.request_source_props = lambda name, props: None
    _watch(("10.0.0.5", 1), "clipA", cadence, "seek")
    assert viosc.watched_sources == {}


def test_watch_clamps_the_cadence_high():
    viosc.request_source_props = lambda name, props: None
    _watch(("10.0.0.5", 1), "clipA", 999_999, "seek")
    assert next(iter(viosc.watched_sources.values()))["interval_ms"] == viosc.WATCH_MAX_INTERVAL_MS


def test_watch_poll_coalesces_one_get_per_source():
    viosc.vimix_data[0] = {"name": "clipA"}
    viosc.vimix_data[1] = {"name": "clipB"}
    gets: list[tuple[str, list]] = []
    viosc.request_source_props = lambda name, props: gets.append((name, list(props)))
    _watch(("10.0.0.1", 1), "clipA", 100, "seek")
    _watch(("10.0.0.2", 2), "clipA", 100, "alpha")
    _watch(("10.0.0.3", 3), "clipB", 100, "hue")
    gets.clear()
    now = viosc.time.monotonic()
    assert viosc.watch_poll(now=now + 0.2) == 2, "two sources are due, coalesced"
    assert sorted(gets) == [("clipA", ["alpha", "seek"]), ("clipB", ["hue"])]
    # not due yet: a poll inside the interval does nothing
    assert viosc.watch_poll(now=now + 0.25) == 0


def test_notify_watchers_sends_only_the_changed_prop_to_the_right_client():
    viosc.vimix_data[0] = {"name": "clipA"}
    viosc.request_source_props = lambda name, props: None
    _watch(("10.0.0.1", 1), "clipA", 100, "seek")
    _watch(("10.0.0.2", 2), "clipA", 100, "alpha")
    assert viosc.notify_watchers(0, "seek", 0.4) == 1
    clients = {c.ip: c for c in FakeUDPClient.instances}
    assert clients["10.0.0.1"].messages == [("/viosc/reply/clipA", ["seek", 0.4])]
    assert "10.0.0.2" not in clients or clients["10.0.0.2"].messages == []
    assert viosc.notify_watchers(0, "unwatched", 1) == 0
    assert viosc.notify_watchers(99, "seek", 0.5) == 0, "unknown source: no reply"


def test_watch_capacity_cap(monkeypatch):
    monkeypatch.setattr(viosc, "WATCH_MAX_ENTRIES", 2)
    viosc.request_source_props = lambda name, props: None
    _watch(("10.0.0.1", 1), "clipA", 100, "seek")
    _watch(("10.0.0.2", 2), "clipA", 100, "seek")
    _watch(("10.0.0.3", 3), "clipA", 100, "seek")
    assert len(viosc.watched_sources) == 2, "the registry is capped"
    # replacing an existing entry is still allowed at capacity
    _watch(("10.0.0.2", 2), "clipA", 200, "alpha")
    assert viosc.watched_sources[("clipA", "10.0.0.2", 6667)]["interval_ms"] == 200
