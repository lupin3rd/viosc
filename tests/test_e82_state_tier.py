"""The state tier and the single due-based poller (e82s02).

Every property vimix does not push is known only because somebody polls it, and the
old policy was scattered per subscriber: `watch_poll` issued one `get` per
(source, requester) at that entry's cadence, while the properties the app
actually reads were never asked for at all.

This suite pins the new shape: ONE loop ticks every `POLL_TICK_MS`, each task runs
when its own deadline is due, and a source gets ONE `get` per due moment carrying
the UNION of what the state tier and the interest registry need (chunked at the
ten-property reply budget).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import viosc

TIER = set(viosc.STATE_TIER_PROPS)


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
    viosc.vimix_data[1] = viosc.create_empty_vimix_entry()
    viosc.vimix_data[1]["name"] = "clipA"
    viosc.vimix_data[2] = viosc.create_empty_vimix_entry()
    viosc.vimix_data[2]["name"] = "clipB"
    yield rec
    viosc.vimix_data.clear()
    viosc.known_indices.clear()
    viosc.interest_sources.clear()


def _gets(rec):
    return [message for message in rec.messages if message[0].endswith("/get")]


def test_the_tier_polls_every_source_once_per_second(_rig):
    viosc.sync_tick(now=100.0)

    gets = _gets(_rig)
    assert [address for address, _props in gets] == ["/vimix/clipA/get", "/vimix/clipB/get"]
    assert set(gets[0][1]) == TIER, "the tier's properties, nothing more"


def test_the_tier_does_not_poll_again_inside_the_second(_rig):
    viosc.sync_tick(now=100.0)
    _rig.messages.clear()

    viosc.sync_tick(now=100.5)

    assert _gets(_rig) == []


def test_the_tier_polls_again_after_one_second(_rig):
    viosc.sync_tick(now=100.0)
    _rig.messages.clear()

    viosc.sync_tick(now=101.0)

    assert len(_gets(_rig)) == 2


def test_a_source_without_a_name_is_not_polled(_rig):
    viosc.vimix_data[3] = viosc.create_empty_vimix_entry()

    viosc.sync_tick(now=100.0)

    assert len(_gets(_rig)) == 2, "a nameless entry has no address to ask"


def test_an_interest_prop_outside_the_tier_joins_the_same_get(_rig):
    viosc.set_interest("clipA", ["brightness"], 200)

    viosc.sync_tick(now=100.0)

    gets = [message for message in _gets(_rig) if message[0] == "/vimix/clipA/get"]
    carried = set().union(*(set(props) for _address, props in gets))
    assert carried == TIER | {"brightness"}, "the interest rides WITH the tier, not beside it"
    assert len(gets) == 1, "14 props still fit the reply budget: ONE message, never split by owner"


def test_an_interest_is_polled_at_its_own_cadence(_rig):
    viosc.set_interest("clipA", ["brightness"], 200)
    viosc.sync_tick(now=100.0)
    _rig.messages.clear()

    viosc.sync_tick(now=100.2)

    assert _gets(_rig) == [("/vimix/clipA/get", ["brightness"])], "only the hot prop, only clipA"


def test_a_large_interest_is_chunked_at_the_reply_budget(_rig):
    props = [
        "brightness",
        "contrast",
        "saturation",
        "hue",
        "threshold",
        "gamma",
        "posterize",
        "invert",
        "seek",
        "speed",
        "lock",
        "failed",
    ]
    viosc.set_interest("clipA", props, 100)

    viosc.sync_tick(now=100.0)

    gets = [message for message in _gets(_rig) if message[0] == "/vimix/clipA/get"]
    assert len(gets) == 2, "25 props at 15 per reply"
    assert all(len(p) <= viosc.INTEREST_MAX_PROPS_PER_GET for _address, p in gets)
    assert set().union(*(set(p) for _address, p in gets)) == TIER | set(props)


def test_the_core_sync_keeps_its_own_two_second_cadence(_rig):
    viosc.sync_tick(now=100.0)
    viosc.sync_tick(now=101.0)
    viosc.sync_tick(now=102.0)

    syncs = [message for message in _rig.messages if message[0] == "/vimix/current/sync"]
    assert len(syncs) == 2, "the roster poll follows sync_interval_time, not the tick"


def test_releasing_an_interest_stops_its_polling(_rig):
    viosc.set_interest("clipA", ["brightness"], 200)
    assert viosc.set_interest("clipA", [], 200) is False
    viosc.sync_tick(now=100.0)
    _rig.messages.clear()

    viosc.sync_tick(now=100.2)

    assert _gets(_rig) == []
