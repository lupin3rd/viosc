"""Poll scheduling, sync coalescing and mirror locking (e03s02).

The tier and the interests used to keep separate deadlines: a 1 s interest
declared after the tier started doubled the gets of that source for ever, and a
session/open could send three /vimix/current/sync in 0.5 s. The mirror was also
iterated by HTTP while the OSC threads mutated it. These tests pin the merged
poll, the 500 ms sync gap, and the lock.
"""

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import viosc


class Recorder:
    def __init__(self):
        self.messages = []

    def send_message(self, address, args):
        self.messages.append((address, list(args)))


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    viosc.vimix_data.clear()
    for index, name in ((0, "clipA"), (1, "clipB")):
        entry = viosc.create_empty_vimix_entry()
        entry["name"] = name
        viosc.vimix_data[index] = entry
    viosc.interest_sources.clear()
    viosc._state_tier_last = 0.0
    viosc._last_core_sync = 0.0
    viosc._last_sync_sent = 0.0
    viosc._pending_session_sync_at = 0.0
    viosc._awaiting_roster = False
    viosc._sync_sent_at = 0.0
    viosc._probe_active = False
    viosc._probe_last_at = 0.0
    rec = Recorder()
    gets = []
    monkeypatch.setattr(viosc, "forward_client", rec)
    monkeypatch.setattr(viosc, "request_source_props", lambda n, p: gets.append((n, list(p))))
    monkeypatch.setattr(viosc, "start_roster_probe", lambda *a, **k: False)
    monkeypatch.setattr(viosc, "request_uri_once", lambda *a, **k: None)
    yield rec, gets
    viosc.vimix_data.clear()
    viosc.interest_sources.clear()


def _run(gets, start, end, step=0.05):
    """Drive the single tick over [start, end]; return (time, source, props)."""
    clock = start
    while clock <= end + 1e-9:
        gets.clear()
        viosc.sync_tick(now=round(clock, 2))
        for name, props in list(gets):
            yield round(clock, 2), name, tuple(props)
        clock = round(clock + step, 2)


def test_a_one_second_interest_does_not_double_the_gets(_clean):
    _rec, gets = _clean
    viosc.sync_tick(now=0.0)
    viosc.sync_tick(now=1.0)  # the tier's phase is now 1.0, like a running daemon
    viosc.set_interest("clipA", ["brightness"], 1000)

    seen = list(_run(gets, 1.05, 10.0))
    clipA = [item for item in seen if item[1] == "clipA"]
    assert len(clipA) <= 11, f"one get per second, not two: {clipA[:6]}"
    assert any(1.05 <= t <= 1.1 for t, _n, _p in clipA), "the immediate feedback stays"
    assert not [item for item in clipA if 2.04 < item[0] < 2.06], "2.05 is merged into 2.0"


def test_a_200_ms_interest_rides_the_tier_tick(_clean):
    _rec, gets = _clean
    viosc.sync_tick(now=0.0)
    viosc.sync_tick(now=1.0)
    viosc.set_interest("clipA", ["position"], 200)

    seen = list(_run(gets, 1.05, 3.0))
    clipA = [item for item in seen if item[1] == "clipA"]
    second_two = [item for item in clipA if 2.0 <= item[0] < 3.0]
    assert len(second_two) == 5, f"five polls in second two: {second_two}"
    assert len([item for item in second_two if item[0] == 2.0]) == 1
    assert "position" in second_two[0][2], "the tier tick carries the hot prop"
    assert not [item for item in clipA if 2.04 < item[0] < 2.06], "no duplicate at 2.05"


def test_session_open_costs_two_syncs_not_three(_clean):
    rec, _gets = _clean
    viosc._last_core_sync = 100.0  # the core is not due around the session event

    viosc.request_session_sync(now=100.0)
    viosc.sync_tick(now=100.2)
    viosc.sync_tick(now=100.5)
    viosc.sync_tick(now=101.0)

    syncs = [m for m in rec.messages if m[0] == "/vimix/current/sync"]
    assert len(syncs) == 2, f"immediate + retry, no core: {syncs}"


def test_a_skipped_immediate_is_replaced_by_the_retry(_clean):
    rec, _gets = _clean
    assert viosc._send_sync(100.0, "core") is True
    rec.messages.clear()

    viosc.request_session_sync(now=100.1)  # inside the 500 ms gap: skipped
    viosc.sync_tick(now=100.6)  # the retry lands at +500 ms

    syncs = [m for m in rec.messages if m[0] == "/vimix/current/sync"]
    assert len(syncs) == 1, "the retry replaces the skipped immediate"


def test_state_payload_survives_a_concurrent_writer(_clean):
    _rec, _gets = _clean
    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    errors: list[BaseException] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                viosc.state_payload()
            except BaseException as exc:
                errors.append(exc)
                return

    def writer():
        for index in range(1000):
            viosc.process_vimix_message(f"/vimix/{index}/name", [f"src{index}"])

    read_thread = threading.Thread(target=reader, daemon=True)
    read_thread.start()
    try:
        writer()
    finally:
        stop.set()
        read_thread.join(timeout=2.0)
        sys.setswitchinterval(old_interval)
    assert errors == [], f"state_payload raised under a writer: {errors[:3]}"


def test_record_change_revisions_are_unique_under_threads(_clean):
    _rec, _gets = _clean
    viosc.state_rev = 0
    viosc.change_log.clear()
    results: list[int] = []
    errors: list[BaseException] = []

    def writer():
        try:
            for _ in range(1000):
                results.append(viosc.record_change(0, "alpha", 1.0))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)
    assert errors == []
    assert len(results) == 4000
    assert len(set(results)) == 4000, "a lost revision breaks the SSE contract"
    assert viosc.state_rev == 4000
