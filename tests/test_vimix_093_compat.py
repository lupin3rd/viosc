"""Vimix 0.9.3 id/type semantics (e03s03).

0.9.3 accepts a numeric target as an index OR a unique id and answers a get
with the same target string. A reply addressed by id used to create a phantom
source keyed by the 18-digit id; the echo of an id-targeted set was dropped.
The same release carries the 0.9.3 capability marker and the source-type guard
that keeps audio/text/sequence sources out of the thumbnail pipeline.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import viosc

SOURCE_ID = 123_456_789_012_345_678


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    viosc.vimix_data.clear()
    viosc.known_indices.clear()
    viosc.seen_indices.clear()
    viosc.seen_current.clear()
    viosc.seen_id.clear()
    viosc.vimix_has_093 = False
    viosc._vimix_compat_warned = False
    viosc._first_source_at = 0.0
    viosc._probe_active = False
    viosc._probe_requested.clear()
    viosc._probe_found.clear()
    monkeypatch.setattr(viosc, "request_uri_once", lambda *a, **k: None)
    monkeypatch.setattr(viosc, "schedule_state_broadcast", lambda *a, **k: False)
    yield
    viosc.vimix_data.clear()
    viosc.known_indices.clear()
    viosc.vimix_has_093 = False


def _source(idx=0, name="clipA", **props):
    entry = viosc.create_empty_vimix_entry()
    entry["name"] = name
    entry.update(props)
    viosc.vimix_data[idx] = entry
    return entry


def test_an_id_targeted_reply_updates_the_source_without_a_phantom():
    _source(0, id=SOURCE_ID)

    parsed = viosc.process_vimix_message(f"/vimix/{SOURCE_ID}/alpha", [0.25])

    assert parsed is not None and parsed[0] == 0
    assert list(viosc.vimix_data) == [0], "no phantom keyed by the 18-digit id"
    assert viosc.vimix_data[0]["alpha"] == 0.25


def test_an_id_targeted_echo_applies_to_the_source():
    _source(0, id=SOURCE_ID)

    assert viosc.apply_echo(f"/vimix/{SOURCE_ID}/alpha", [0.4]) is True
    assert viosc.vimix_data[0]["alpha"] == 0.4


def test_an_unknown_huge_id_is_dropped_not_made_a_source():
    assert viosc.process_vimix_message(f"/vimix/{SOURCE_ID}/alpha", [0.2]) is None
    assert viosc.vimix_data == {}


def test_a_bare_index_still_creates_its_entry():
    viosc.process_vimix_message("/vimix/5/name", ["clip"])
    assert 5 in viosc.vimix_data


def test_a_type_or_id_reply_marks_the_0_9_3_capability():
    _source(0)
    viosc.process_vimix_message("/vimix/0/type", [10])
    assert viosc.vimix_has_093 is True


def test_a_missing_capability_warns_once(monkeypatch, capsys):
    monkeypatch.setattr(viosc.time, "monotonic", lambda: 1000.0)
    viosc.process_vimix_message("/vimix/0/name", ["clip"])

    viosc.sync_tick(now=1005.1)
    out = capsys.readouterr().out
    assert "0.9.3" in out and "required" in out

    viosc.sync_tick(now=1010.0)
    assert capsys.readouterr().out.count("0.9.3") == 0, "one warning, not a loop"


def test_a_capability_reply_silences_the_warning(monkeypatch, capsys):
    monkeypatch.setattr(viosc.time, "monotonic", lambda: 1000.0)
    viosc.process_vimix_message("/vimix/0/name", ["clip"])
    viosc.process_vimix_message("/vimix/0/type", [10])

    viosc.sync_tick(now=1005.1)
    assert "0.9.3" not in capsys.readouterr().out


def test_an_audio_type_never_reaches_ffprobe(monkeypatch):
    _source(0, type=13)
    monkeypatch.setattr(
        viosc, "classify_media_kind", lambda _path: pytest.fail("ffprobe must not run")
    )

    assert viosc.publish_media_kind(0, "/tmp/track.mp3") == viosc.MEDIA_KIND_OTHER
    assert viosc.vimix_data[0]["media_kind"] == viosc.MEDIA_KIND_OTHER


def test_a_uri_list_does_not_start_a_thumbnail_worker(monkeypatch):
    _source(0)
    monkeypatch.setattr(
        viosc.threading, "Thread", lambda *a, **k: pytest.fail("no worker for a list uri")
    )

    viosc.process_vimix_message("/vimix/0/uri", [["a.mp4", "b.mp4"]])
    assert viosc.vimix_data[0]["uri"] == ["a.mp4", "b.mp4"]
