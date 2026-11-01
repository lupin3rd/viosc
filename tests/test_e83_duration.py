"""The clip duration in the state table (e83).

`seek` is a FRACTION of the timeline, so the app's seek bar needs the clip length
to dead-reckon between the 1 s answers: a fraction advanced by `speed` alone
saturates to 100% in one second (the operator's sawtooth). viOSC already probes
the duration for the thumbnails, so publishing it costs one cached lookup — and it
is a viOSC-local key like `media_kind`, not an OSC property.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import viosc


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    viosc.vimix_data.clear()
    viosc.MEDIA_META_CACHE.clear()
    monkeypatch.setattr(viosc, "classify_media_kind", lambda path: "video")
    monkeypatch.setattr(viosc, "schedule_state_broadcast", lambda *a, **kw: False)
    monkeypatch.setattr(viosc.os.path, "exists", lambda path: True)
    yield
    viosc.vimix_data.clear()
    viosc.MEDIA_META_CACHE.clear()


def test_the_duration_is_published_with_the_kind(monkeypatch):
    viosc.vimix_data[5] = viosc.create_empty_vimix_entry()
    viosc.vimix_data[5]["name"] = "clipA"
    monkeypatch.setattr(
        viosc, "probe_media_meta", lambda path: {"kind": "video", "duration_s": 12.5}
    )

    viosc.publish_media_kind(5, "/m/fake.mp4")

    payload = json.loads(viosc.state_json())
    assert payload["sources"]["5"]["duration_s"] == 12.5


def test_a_still_image_publishes_no_duration(monkeypatch):
    viosc.vimix_data[5] = viosc.create_empty_vimix_entry()
    viosc.vimix_data[5]["name"] = "still"
    monkeypatch.setattr(viosc, "classify_media_kind", lambda path: "image")
    monkeypatch.setattr(
        viosc, "probe_media_meta", lambda path: {"kind": "image", "duration_s": 0.0}
    )

    viosc.publish_media_kind(5, "/m/still.png")

    assert "duration_s" not in viosc.vimix_data[5], "no timeline, no dead reckoning"


def test_the_duration_is_reported_once_per_change(monkeypatch):
    viosc.vimix_data[5] = viosc.create_empty_vimix_entry()
    viosc.vimix_data[5]["name"] = "clipA"
    monkeypatch.setattr(
        viosc, "probe_media_meta", lambda path: {"kind": "video", "duration_s": 12.5}
    )
    viosc.state_rev = 0

    viosc.publish_media_kind(5, "/m/fake.mp4")
    rev = viosc.state_rev
    viosc.publish_media_kind(5, "/m/fake.mp4")

    assert rev == viosc.state_rev, "the same duration is not a change"
