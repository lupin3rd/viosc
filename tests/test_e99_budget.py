"""e99s03 — the OSC traffic budget, as a test.

A traffic ceiling that lives only in an audit document rots: the next property
added to the tier re-inflates the cost silently, exactly as `position`, `size`
and `angle` did. This file drives the REAL poller and replays Vimix's real reply
shape (one OSC message per requested attribute, one bundle per `get`), then
asserts the resulting messages per source per second — so adding a property
breaks a test whose message names it.

Provenance: specs/verifications/AUDIT-viosc-osc-traffic.md (measured 16.25
messages/s per source before e99s01; density ~5 % at rest).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import viosc

# The nine properties a VJmix screen renders at 1 Hz (e99s01).
CONSUMED = (
    "name",
    "alpha",
    "blending",
    "correction",
    "depth",
    "play",
    "lock",
    "speed",
    "seek",
)
# Left the tier because nothing consumes them at 1 Hz.
RETIRED = ("id", "index", "position", "size", "angle")

# The ceiling per source per second: 9 tier replies + (4 sources + 5 current
# attributes) of roster every 2 s -> 9 + 2.25 = 11.25 active, and 0.9 + 0.45 =
# 1.35 idle.
ACTIVE_BUDGET = 11.25
IDLE_BUDGET = 1.35
TOLERANCE = 0.05
SOURCES = 10


class Recorder:
    def __init__(self):
        self.messages = []

    def send_message(self, address, args):
        self.messages.append((address, list(args)))


def tier_offenders():
    """The tier names nothing consumes — the failure must name them."""
    return sorted(set(viosc.STATE_TIER_PROPS) - set(CONSUMED))


@pytest.fixture(autouse=True)
def _rig(monkeypatch):
    viosc.vimix_data.clear()
    viosc.known_indices.clear()
    viosc.interest_sources.clear()
    viosc.interest_full.update({"active": False, "remaining": None, "last_poll": 0.0})
    viosc._state_tier_last = 0.0
    viosc._last_core_sync = 0.0
    viosc._last_sync_sent = 0.0
    viosc._awaiting_roster = False
    viosc._sync_sent_at = 0.0
    viosc._probe_active = False
    viosc._probe_last_at = 0.0
    viosc._last_consumer_at = 0.0
    viosc._tier_idle = False
    viosc.install_event_hub(None)
    monkeypatch.setattr(viosc, "sync_interval_time", 2000)
    monkeypatch.setattr(viosc, "start_roster_probe", lambda *a, **k: False)
    rec = Recorder()
    monkeypatch.setattr(viosc, "forward_client", rec)
    for index in range(SOURCES):
        viosc.vimix_data[index] = viosc.create_empty_vimix_entry()
        viosc.vimix_data[index]["name"] = f"clip{index:02d}"
    yield rec
    viosc.vimix_data.clear()
    viosc.known_indices.clear()
    viosc.interest_sources.clear()


def measure(recorder: Recorder, seconds: float = 60.0, idle: bool = False) -> float:
    """Messages per source per second, driven tick by tick like the daemon."""
    tick = viosc.POLL_TICK_MS / 1000.0
    now = 100.0
    viosc.note_consumer_activity(now=now)
    if idle:
        while now < 100.0 + viosc.CONSUMER_IDLE_S + 1:
            now += 1.0
            viosc.sync_tick(now=now)
        viosc._last_consumer_at = now - viosc.CONSUMER_IDLE_S - 1
    started = now
    stamp_at = started
    answered = 0
    while now < started + seconds:
        now += tick
        if not idle and now >= stamp_at:
            viosc.note_consumer_activity(now=now)  # VJmix polls /state every 5 s
            stamp_at = now + 5.0
        del recorder.messages[:]
        viosc.sync_tick(now=now)
        for _address, props in recorder.messages:
            if _address.endswith("/get"):
                answered += len(props)  # Vimix: one reply message per attribute
            elif _address.endswith("/sync"):
                answered += 4 * SOURCES + 5  # the roster bundle
    return answered / seconds / SOURCES


def test_the_tier_has_no_unconsumed_property():
    offenders = tier_offenders()

    assert not offenders, f"the tier asks for properties nothing renders: {offenders}"


def test_the_active_budget_holds(_rig):
    rate = measure(_rig)

    assert rate < ACTIVE_BUDGET * (1 + TOLERANCE), f"active traffic is {rate:.2f} msg/s per source"
    assert rate > ACTIVE_BUDGET * (1 - TOLERANCE), f"the tier went quiet: {rate:.2f}"


def test_the_idle_budget_holds(_rig):
    rate = measure(_rig, seconds=30.0, idle=True)

    assert rate < IDLE_BUDGET * (1 + TOLERANCE), f"idle traffic is {rate:.2f} msg/s per source"
    assert rate > 0.5, "the idle keepwarm must still reconcile the mirror"


@pytest.mark.parametrize("prop", RETIRED)
def test_an_added_property_breaks_the_budget(monkeypatch, prop, _rig):
    """The guard's other half: re-adding a retired name must fail, by name."""
    monkeypatch.setattr(viosc, "STATE_TIER_PROPS", (*CONSUMED, prop))

    rate = measure(_rig, seconds=20.0)

    assert tier_offenders() == [prop], f"the test names {prop}"
    assert rate > ACTIVE_BUDGET * (1 + TOLERANCE), (
        f"{prop} costs one message per source per second ({rate:.2f})"
    )
