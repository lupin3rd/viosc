"""The lane declaration and the interest API at the daemon (e82s03).

`POST /lane` is the safety valve that lets the producer stop broadcasting even
against an older consumer; `POST /interest` replaces the OSC watch/monitor
registries for a consumer that speaks HTTP; `GET /state` refreshes the lane because
asking for the table IS evidence that the consumer reads over HTTP.

These tests pin the daemon side (validation, reporting, the suppression window and
the full sweep); the transport side lives in tests/test_e82_http_surface.py.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import preview_http
import viosc


class Recorder:
    def __init__(self):
        self.messages = []

    def send_message(self, address, args):
        self.messages.append((address, list(args)))


@pytest.fixture(autouse=True)
def _rig(monkeypatch):
    viosc.vimix_data.clear()
    viosc.known_indices.clear()
    viosc.interest_sources.clear()
    viosc.interest_full.update({"active": False, "cadence_ms": 1000, "last_poll": 0.0})
    viosc._lane_until = 0.0
    viosc._state_tier_last = 0.0
    viosc._last_core_sync = 0.0
    viosc._last_monitor_poll = 0.0
    viosc.EVENT_HUB = None
    monkeypatch.setattr(viosc, "sync_interval_time", 2000)
    monkeypatch.setattr(viosc, "schedule_state_broadcast", lambda *a, **kw: False)
    rec = Recorder()
    monkeypatch.setattr(viosc, "forward_client", rec)
    viosc.vimix_data[1] = viosc.create_empty_vimix_entry()
    viosc.vimix_data[1]["name"] = "clipA"
    viosc.vimix_data[2] = viosc.create_empty_vimix_entry()
    viosc.vimix_data[2]["name"] = "clipB"
    yield rec
    viosc.vimix_data.clear()
    viosc.known_indices.clear()
    viosc.interest_sources.clear()
    viosc.interest_full.update({"active": False, "cadence_ms": 1000, "last_poll": 0.0})
    viosc._lane_until = 0.0
    viosc.EVENT_HUB = None


def _gets(rec, name):
    return [args for address, args in rec.messages if address == f"/vimix/{name}/get"]


def test_the_lane_suppresses_the_broadcast_until_it_expires(monkeypatch):
    sent = []
    fake_ui = type("UI", (), {"send_message": lambda s, a, b: sent.append(a)})()
    monkeypatch.setattr(viosc, "ui_reply_client", fake_ui)

    viosc.note_lane_traffic()
    assert viosc.lane_active() is True
    viosc.broadcast_vimix_state()
    assert sent == [], "a consumer reading over HTTP must not be pushed to"

    viosc._lane_until = time.time() - 1.0  # the TTL elapsed
    assert viosc.lane_active() is False
    viosc.broadcast_vimix_state()
    assert len(sent) == 1, "once the lane goes quiet the fallback comes back"


def test_reading_the_table_arms_the_lane():
    viosc._lane_until = 0.0

    viosc.http_state_json()

    assert viosc.lane_active() is True, "asking for /state is evidence of the HTTP lane"


def test_a_full_interest_sweeps_every_readable_property(_rig):
    report = viosc.apply_interest({"source": "*", "props": ["*"], "cadence_ms": 1000})
    assert report["ok"] == ["*"]

    viosc.sync_tick(now=100.0)

    carried = set()
    for chunk in _gets(_rig, "clipA"):
        carried.update(chunk)
    assert carried == set(viosc.ALL_PROPERTIES), "the wildcard is the whole readable set"
    assert len(_gets(_rig, "clipA")) == 2, "29 properties at 15 per reply"


def test_releasing_the_full_interest_stops_the_sweep(_rig):
    viosc.apply_interest({"source": "*", "props": ["*"], "cadence_ms": 1000})
    viosc.sync_tick(now=100.0)
    _rig.messages.clear()

    report = viosc.apply_interest({"source": "*", "props": []})
    viosc.sync_tick(now=100.5)

    assert report["released"] is True
    assert _gets(_rig, "clipA") == [], "nothing is due inside the second, and the wildcard is off"


def test_a_one_shot_wildcard_sweeps_once(_rig):
    viosc.apply_interest({"source": "*", "props": ["*"], "once": True})

    viosc.sync_tick(now=100.0)
    assert len(_gets(_rig, "clipA")) == 2, "the full mirror"
    _rig.messages.clear()

    viosc.sync_tick(now=101.0)
    carried = set().union(*(set(props) for props in _gets(_rig, "clipA")))
    assert carried == set(viosc.STATE_TIER_PROPS), "back to the tier only"
    assert viosc.interest_full["active"] is False, "a one-shot request does not linger"


def test_an_unknown_property_is_reported_and_not_stored():
    report = viosc.apply_interest({"source": "clipA", "props": ["bogus"]})

    assert report["unknown"] == ["bogus"]
    assert "clipA" not in viosc.interest_sources


def test_an_unknown_source_is_reported():
    report = viosc.apply_interest({"source": "ghost", "props": ["alpha"]})

    assert report["unknown"] == ["ghost"]
    assert viosc.interest_sources == {}


def test_a_named_interest_is_stored_at_its_cadence():
    report = viosc.apply_interest({"source": "clipA", "props": ["alpha"], "cadence_ms": 100})

    assert report["ok"] == ["clipA"]
    assert viosc.interest_sources["clipA"]["cadence_ms"] == 100


def test_an_illegal_cadence_is_clamped_not_rejected():
    viosc.apply_interest({"source": "clipA", "props": ["alpha"], "cadence_ms": 5})

    assert viosc.interest_sources["clipA"]["cadence_ms"] == viosc.WATCH_MIN_INTERVAL_MS


def test_opening_the_stream_arms_the_lane():
    hub = preview_http.EventHub(on_subscribe=viosc.note_lane_traffic)
    viosc._lane_until = 0.0

    hub.subscribe()

    assert viosc.lane_active() is True, "a stream consumer reads over HTTP by definition"


def test_a_change_publishes_to_the_event_hub():
    hub = preview_http.EventHub()
    channel = hub.subscribe()
    viosc.EVENT_HUB = hub

    viosc.process_vimix_message("/vimix/1/alpha", [0.4])

    event = channel.get_nowait()
    assert event["kind"] == "changed"
    assert event["source"] == "clipA"
    assert event["prop"] == "alpha"
    assert event["value"] == 0.4
    assert event["rev"] == viosc.state_rev
