"""e99s02 — the poller yields to absence.

The tier and the roster used to run at their cadences whether or not a consumer
was connected: with VJmix closed the daemon kept asking Vimix for every property
of every source, every second, for nobody. These tests pin the presence signal,
the idle cadences and the one full sweep a returning consumer gets.

Presence is POSITIVE evidence of a quiet consumer: a daemon that never saw one
keeps the historical 1 s / 2 s cadences, so an unknown consumer (a future route,
a test, a fresh boot) can never be starved by this mechanism.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import http_api
import viosc


class Recorder:
    """Recording stand-in for the forward client."""

    def __init__(self):
        self.messages = []

    def send_message(self, address, args):
        self.messages.append((address, list(args)))


def _gets(rec):
    return [m for m in rec.messages if m[0].endswith("/get")]


def _syncs(rec):
    return [m for m in rec.messages if m[0].endswith("/sync")]


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
    viosc.vimix_data[1] = viosc.create_empty_vimix_entry()
    viosc.vimix_data[1]["name"] = "clipA"
    yield rec
    viosc.vimix_data.clear()
    viosc.known_indices.clear()
    viosc.interest_sources.clear()
    viosc.install_event_hub(None)


def _seen(now: float = 100.0) -> None:
    """A consumer has just talked (the clock is the test's fake one)."""
    viosc.note_consumer_activity(now=now)


# -- the signal ---------------------------------------------------------------


def test_the_hub_counts_its_subscribers():
    hub = http_api.EventHub()

    assert hub.count() == 0
    channel = hub.subscribe()
    assert hub.count() == 1
    hub.unsubscribe(channel)
    assert hub.count() == 0


def test_an_open_stream_is_a_consumer():
    calls = []
    hub = http_api.EventHub(on_subscribe=lambda: calls.append(1))
    viosc.install_event_hub(hub)

    hub.subscribe()

    assert viosc.consumer_present(now=100.0) is True
    assert calls == [1], "the hub still notifies the daemon on subscribe"


def test_a_daemon_that_never_saw_a_consumer_keeps_the_active_cadences():
    assert viosc.consumer_present(now=100.0) is True


def test_a_consumer_that_went_quiet_is_absent():
    _seen(100.0)

    assert viosc.consumer_present(now=100.0 + viosc.CONSUMER_IDLE_S - 1) is True
    assert viosc.consumer_present(now=100.0 + viosc.CONSUMER_IDLE_S + 1) is False


def test_a_one_shot_sweep_keeps_the_poller_awake_while_it_runs():
    viosc.interest_full.update({"active": True, "last_poll": 100.0})
    _seen(100.0)

    assert viosc.consumer_present(now=100.0 + viosc.CONSUMER_IDLE_S + 1) is True


# -- the idle cadences --------------------------------------------------------


def test_the_tier_polls_every_ten_seconds_while_idle(_rig):
    _seen(100.0)
    viosc.sync_tick(now=100.0 + viosc.CONSUMER_IDLE_S + 1)
    del _rig.messages[:]

    viosc.sync_tick(now=100.0 + viosc.CONSUMER_IDLE_S + 2)

    assert _gets(_rig) == [], "inside the idle interval nothing is asked"
    viosc.sync_tick(now=100.0 + viosc.CONSUMER_IDLE_S + 1 + 10.0)
    assert len(_gets(_rig)) == 1, "one get per source per idle interval"


def test_the_roster_syncs_every_ten_seconds_while_idle(_rig):
    _seen(100.0)
    idle_from = 100.0 + viosc.CONSUMER_IDLE_S + 1
    viosc.sync_tick(now=idle_from)
    del _rig.messages[:]

    viosc.sync_tick(now=idle_from + 2.0)

    assert _syncs(_rig) == [], "the 2 s roster cadence is for a live consumer"
    viosc.sync_tick(now=idle_from + 10.0)
    assert len(_syncs(_rig)) == 1


def test_a_declared_interest_does_not_fire_while_idle(_rig):
    viosc.set_interest("clipA", ["brightness"], 100)
    _seen(100.0)
    idle_from = 100.0 + viosc.CONSUMER_IDLE_S + 1
    viosc.sync_tick(now=idle_from)
    del _rig.messages[:]

    viosc.sync_tick(now=idle_from + 0.5)

    assert all("brightness" not in props for _address, props in _gets(_rig))


# -- the sweep on return ------------------------------------------------------


def test_a_returning_consumer_gets_one_full_tier_round_and_one_roster_round(_rig):
    _seen(100.0)
    idle_from = 100.0 + viosc.CONSUMER_IDLE_S + 1
    viosc.sync_tick(now=idle_from)  # idle now
    del _rig.messages[:]

    _seen(idle_from + 20.0)
    viosc.sync_tick(now=idle_from + 20.0)

    gets = _gets(_rig)
    assert [address for address, _props in gets] == ["/vimix/clipA/get"]
    assert len(_syncs(_rig)) == 1, "the roster is refreshed on the same tick"
    assert viosc.consumer_present(now=idle_from + 20.0) is True


def test_the_active_cadences_come_back_after_the_sweep(_rig):
    _seen(100.0)
    idle_from = 100.0 + viosc.CONSUMER_IDLE_S + 1
    viosc.sync_tick(now=idle_from)
    _seen(idle_from + 20.0)
    viosc.sync_tick(now=idle_from + 20.0)
    del _rig.messages[:]

    viosc.sync_tick(now=idle_from + 21.0)

    assert len(_gets(_rig)) == 1, "back to one get per source per second"


# -- the whole loop -----------------------------------------------------------


def test_twenty_idle_seconds_cost_almost_nothing(_rig):
    """Integration: the 10 s idle cadence, driven tick by tick as the daemon does."""
    _seen(100.0)
    idle_from = 100.0 + viosc.CONSUMER_IDLE_S + 1
    viosc.sync_tick(now=idle_from)  # enter idle
    del _rig.messages[:]

    tick = viosc.POLL_TICK_MS / 1000.0
    answered = 0
    now = idle_from
    for _ in range(round(20.0 / tick)):
        del _rig.messages[:]
        now += tick
        viosc.sync_tick(now=now)
        for _address, props in _gets(_rig):
            answered += len(props)  # Vimix: one message per attribute
        if _syncs(_rig):
            answered += 4 + 5  # the roster bundle: 4 per source + 5 for current

    per_second = answered / 20.0
    assert per_second < 3.0, f"idle costs {per_second:.1f} msg/s for one source"
    assert per_second > 0.5, "the keepwarm still reconciles the mirror"
