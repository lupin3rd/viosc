"""Discovery on the event (e82s06).

vimix answers the roster bundle (`current/N`, `N/alpha`, `N/name`) only when it is
ASKED, and `/vimix/session/open` — exactly the moment VJmix rewrites the session —
produces NO bundle at all (`receiveSessionAttribute` returns false for it). The
blind 2 s tick was therefore the only way a new session became visible.

viOSC forwards the session commands, so it can ask right there: one immediate
`/vimix/current/sync` plus one retry, because vimix loads the media
asynchronously. The forward itself stays byte-identical.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import viosc


class Recorder:
    def __init__(self):
        self.messages = []

    def send_message(self, address, args):
        self.messages.append((address, list(args)))


@pytest.fixture(autouse=True)
def _rig(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(viosc, "forward_client", recorder)
    # test_boot leaves the global at 500 ms: pin the cadence this suite reasons about.
    monkeypatch.setattr(viosc, "sync_interval_time", 2000)
    viosc._pending_session_sync_at = 0.0
    viosc._last_core_sync = 0.0
    viosc._state_tier_last = 0.0
    viosc._last_sync_sent = 0.0
    viosc._awaiting_roster = False
    viosc._sync_sent_at = 0.0
    viosc._probe_active = False
    viosc._probe_last_at = 0.0
    viosc.vimix_data.clear()
    viosc.interest_sources.clear()
    yield recorder
    viosc.vimix_data.clear()
    viosc.interest_sources.clear()


def _syncs(recorder):
    return [m for m in recorder.messages if m[0] == "/vimix/current/sync"]


def _command(address, *args):
    """One command through the 1.0 upstream: a frame on POST /link (e98s04)."""
    viosc.apply_link_frame(json.dumps({"address": address, "args": list(args)}).encode("utf-8"))


def test_session_open_asks_for_the_roster_immediately(_rig):
    _command("/vimix/session/open", "/tmp/s.mix")

    assert _syncs(_rig) == [("/vimix/current/sync", [])], "the session change is the event"


def test_the_forward_is_unchanged_and_comes_first(_rig):
    _command("/vimix/session/open", "/tmp/s.mix")

    assert _rig.messages[0] == ("/vimix/session/open", ["/tmp/s.mix"]), "verbatim, untouched"
    assert _syncs(_rig) == [("/vimix/current/sync", [])]


def test_one_retry_lands_half_a_second_later(_rig):
    viosc.request_session_sync(now=100.0)
    viosc._last_core_sync = 100.0  # the blind roster tick is not due in this window
    _rig.messages.clear()

    viosc.sync_tick(now=100.2)
    assert _syncs(_rig) == [], "not yet: vimix is still loading"

    viosc.sync_tick(now=100.5)
    assert _syncs(_rig) == [("/vimix/current/sync", [])], "the retry catches the async load"

    viosc.sync_tick(now=101.0)
    assert len(_syncs(_rig)) == 1, "one retry, not a loop"


def test_another_command_does_not_ask(_rig):
    _command("/vimix/clip/alpha", 0.4)

    assert _syncs(_rig) == []
