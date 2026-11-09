"""e99s01 — the state tier asks only what a consumer renders.

The tier used to ask Vimix for fourteen properties of every source every second,
and Vimix answers one OSC message per attribute. Five of those properties have no
1 Hz consumer: `position`, `size` and `angle` are only ever shown by the raw
on-demand table (VJmix has no geometry panel), `id` is immutable and already in
every roster bundle, and `index` is the daemon's own dictionary key.

These tests pin the reduced set and the property that replaces `index`: it is
filled when the mirror creates a source entry, so `/state` and `/events` keep
carrying it without a message.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import viosc

# The properties a VJmix screen renders (or dead-reckons from) at 1 Hz.
CONSUMED = {
    "name",
    "alpha",
    "blending",
    "correction",
    "depth",
    "play",
    "lock",
    "speed",
    "seek",
}
RETIRED = {"id", "index", "position", "size", "angle"}


class Recorder:
    """Recording stand-in for the forward client."""

    def __init__(self):
        self.messages = []

    def send_message(self, address, args):
        self.messages.append((address, list(args)))


@pytest.fixture(autouse=True)
def _rig(monkeypatch):
    viosc.vimix_data.clear()
    viosc.known_indices.clear()
    viosc.interest_sources.clear()
    viosc.seen_current.clear()
    viosc.seen_indices.clear()
    viosc.seen_id.clear()
    viosc.source_current = None
    viosc.vimix_has_093 = False
    viosc._state_tier_last = 0.0
    viosc._last_core_sync = 0.0
    viosc._last_sync_sent = 0.0
    viosc._awaiting_roster = False
    viosc._sync_sent_at = 0.0
    viosc._probe_active = False
    viosc._probe_last_at = 0.0
    monkeypatch.setattr(viosc, "sync_interval_time", 2000)
    monkeypatch.setattr(viosc, "start_roster_probe", lambda *a, **k: False)
    rec = Recorder()
    monkeypatch.setattr(viosc, "forward_client", rec)
    yield rec
    viosc.vimix_data.clear()
    viosc.known_indices.clear()
    viosc.interest_sources.clear()


def _gets(rec):
    return [message for message in rec.messages if message[0].endswith("/get")]


def test_the_tier_keeps_only_the_properties_a_consumer_renders():
    tier = set(viosc.STATE_TIER_PROPS)
    extra = sorted(tier - CONSUMED)
    missing = sorted(CONSUMED - tier)
    assert tier == CONSUMED, f"unconsumed: {extra} / missing: {missing}"


@pytest.mark.parametrize("prop", sorted(RETIRED))
def test_a_property_without_a_consumer_leaves_the_tier(prop, _rig):
    viosc.vimix_data[1] = viosc.create_empty_vimix_entry()
    viosc.vimix_data[1]["name"] = "clipA"
    viosc.sync_tick(now=100.0)

    asked = set(_gets(_rig)[0][1])

    assert prop not in asked


def test_one_get_per_source_per_second_carries_the_consumed_set(_rig):
    viosc.vimix_data[1] = viosc.create_empty_vimix_entry()
    viosc.vimix_data[1]["name"] = "clipA"

    viosc.sync_tick(now=100.0)

    gets = _gets(_rig)
    assert len(gets) == 1
    assert gets[0] == ("/vimix/clipA/get", sorted(CONSUMED))


def test_creating_a_source_entry_fills_index_from_the_key(_rig):
    # The roster addresses a source by index; the mirror keys the entry by it.
    viosc.process_vimix_message("/vimix/3/name", ("clipC",))

    assert viosc.vimix_data[3]["index"] == 3
    assert viosc.vimix_data[3]["name"] == "clipC"


def test_the_state_payload_still_carries_index(_rig):
    viosc.process_vimix_message("/vimix/3/name", ("clipC",))

    payload = json.loads(viosc.state_json())

    assert payload["sources"]["3"]["index"] == 3


def test_the_compat_flag_and_the_roster_round_need_no_tier_id(_rig):
    assert "id" not in set(viosc.STATE_TIER_PROPS)

    # The roster bundle shape: /vimix/<i>/id next to /vimix/current/<i>.
    viosc.process_vimix_current_message("/vimix/current/0", (1.0,))
    viosc.process_vimix_message("/vimix/0/id", (123456789012345678,))

    assert viosc.vimix_has_093 is True
    assert 0 in viosc.seen_id
    assert viosc._round_is_complete() is True
