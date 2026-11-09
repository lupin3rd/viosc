"""The source mirror: the ingest gate, the revision and the echo (e82s01).

vimix answers `get correction` (`ControlManager.cpp:1683`) and `get id` (`:1744`),
but viOSC's ingest dropped both because they were outside `SUPPORTED_PROPERTIES`.
The mirror also had no revision (nothing could say "this is newer than what you
have") and no way to see our OWN writes: a set produces no feedback, so a value
viOSC has just forwarded was unknown until the next poll.

These tests pin the three behaviours: the gate accepts the two attributes, every
applied change bumps a revision the payload carries, and a forwarded absolute set
WITHOUT a duration is applied to the mirror exactly once.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import viosc


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    viosc.vimix_data.clear()
    viosc.known_indices.clear()
    viosc.seen_indices.clear()
    viosc.seen_current.clear()
    viosc.seen_id.clear()
    viosc._roster_missed.clear()
    viosc.source_current = None
    viosc.state_rev = 0
    viosc.change_log.clear()
    monkeypatch.setattr(viosc, "request_source_props", lambda *a, **kw: None)
    yield
    viosc.vimix_data.clear()
    viosc.known_indices.clear()
    viosc.seen_indices.clear()
    viosc.seen_current.clear()
    viosc.seen_id.clear()
    viosc._roster_missed.clear()
    viosc.source_current = None
    viosc.state_rev = 0
    viosc.change_log.clear()


def test_correction_and_id_are_supported_properties():
    assert "correction" in viosc.ALL_PROPERTIES, "vimix answers it; the gate must accept it"
    assert "id" in viosc.ALL_PROPERTIES, "the rename-proof key: vimix answers it"
    assert set(viosc.ALL_PROPERTIES) == viosc.SUPPORTED_PROPERTIES


def test_the_correction_answer_lands_in_the_entry():
    viosc.process_vimix_message("/vimix/5/name", ["clip"])

    viosc.process_vimix_message("/vimix/5/correction", [1.0])

    assert viosc.vimix_data[5]["correction"] == 1.0


def test_the_id_answer_lands_as_an_int64():
    viosc.process_vimix_message("/vimix/5/name", ["clip"])

    viosc.process_vimix_message("/vimix/5/id", [3588656247536713657])

    assert viosc.vimix_data[5]["id"] == 3588656247536713657
    assert isinstance(viosc.vimix_data[5]["id"], int), "an 18-digit id must not become a float"


def test_an_applied_change_bumps_the_revision():
    viosc.process_vimix_message("/vimix/5/name", ["clip"])

    assert viosc.state_rev == 1, "one applied change = one revision"


def test_an_unchanged_value_does_not_bump_the_revision():
    viosc.process_vimix_message("/vimix/5/name", ["clip"])
    viosc.process_vimix_message("/vimix/5/name", ["clip"])

    assert viosc.state_rev == 1, "a repeated answer is not a change"


def test_a_current_source_change_bumps_the_revision():
    viosc.process_vimix_message("/vimix/5/name", ["clip"])

    viosc.process_vimix_current_message("/vimix/current/5", [1.0])

    assert viosc.state_rev == 2, "the current marker is a change the stream must carry"


def test_a_pruned_source_bumps_the_revision():
    viosc.process_vimix_message("/vimix/5/name", ["clip"])
    viosc.seen_indices.clear()
    # A prune needs a complete round; index 0 is the one source still present.
    viosc.seen_current.update({0})
    viosc.seen_id.update({0})

    viosc.prune_stale_sources(viosc.sync_round)

    assert 5 not in viosc.vimix_data
    assert viosc.state_rev == 2, "a removal is a change"


def test_the_payload_carries_the_revision():
    viosc.process_vimix_message("/vimix/5/name", ["clip"])

    assert viosc.state_payload()["rev"] == viosc.state_rev


class FakeUDPClient:
    """Recording stand-in for a python-osc UDP client."""

    def __init__(self, ip, port):
        self.ip = ip
        self.port = port
        self.messages = []

    def send_message(self, address, args):
        self.messages.append((address, list(args)))


def test_a_forwarded_scalar_set_updates_the_mirror():
    viosc.process_vimix_message("/vimix/5/name", ["clip"])
    rev = viosc.state_rev

    assert viosc.apply_echo("/vimix/clip/alpha", [0.4]) is True

    assert viosc.vimix_data[5]["alpha"] == pytest.approx(0.4)
    assert viosc.state_rev == rev + 1, "our own write is a change like any other"


def test_a_set_with_a_duration_is_not_echoed():
    viosc.process_vimix_message("/vimix/5/name", ["clip"])

    assert viosc.apply_echo("/vimix/clip/alpha", [0.4, 1000.0]) is False

    assert viosc.vimix_data[5]["alpha"] is None, (
        "vimix animates: the mirror must not claim the target"
    )


def test_a_partial_vector_keeps_the_other_axis():
    viosc.process_vimix_message("/vimix/5/name", ["clip"])
    viosc.process_vimix_message("/vimix/5/position", [0.1, 0.2])

    assert viosc.apply_echo("/vimix/clip/position", [None, 0.9]) is True

    assert viosc.vimix_data[5]["position"] == pytest.approx([0.1, 0.9])


def test_a_trigger_is_never_echoed():
    viosc.process_vimix_message("/vimix/5/name", ["clip"])
    rev = viosc.state_rev

    assert viosc.apply_echo("/vimix/clip/flag", [2.0]) is False
    assert viosc.state_rev == rev


def test_a_rename_is_not_echoed():
    viosc.process_vimix_message("/vimix/5/name", ["clip"])
    rev = viosc.state_rev

    assert viosc.apply_echo("/vimix/clip/name", ["renamed"]) is False
    assert viosc.vimix_data[5]["name"] == "clip"
    assert viosc.state_rev == rev


def test_an_unknown_target_is_never_created():
    assert viosc.apply_echo("/vimix/ghost/alpha", [0.4]) is False
    assert viosc.vimix_data == {}


def test_the_forward_is_unchanged_and_the_echo_applies(monkeypatch):
    viosc.process_vimix_message("/vimix/5/name", ["clip"])
    forward = FakeUDPClient("127.0.0.1", 7000)
    monkeypatch.setattr(viosc, "forward_client", forward)

    import json

    viosc.apply_link_frame(json.dumps({"address": "/vimix/clip/alpha", "args": [0.25]}).encode())

    assert forward.messages == [("/vimix/clip/alpha", [0.25])], "the frozen forward is untouched"
    assert viosc.vimix_data[5]["alpha"] == pytest.approx(0.25)
