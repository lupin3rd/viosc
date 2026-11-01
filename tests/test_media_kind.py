"""e57s01 — probe-first media classification (viOSC).

`classify_media_kind` is the single decision point added before any thumbnail
extraction: it probes the file and returns video | image | other. A None probe
(a file with no video stream, e.g. audio-only) is retried exactly once (the
cached None is refreshed) and then reported as "other" — the value that tells
VJmix to draw the source name instead of requesting a thumbnail.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import viosc


@pytest.fixture(autouse=True)
def clean_meta_cache():
    viosc.MEDIA_META_CACHE.clear()
    yield
    viosc.MEDIA_META_CACHE.clear()


def test_classify_video_probes_once(monkeypatch):
    calls = []

    def probe(path):
        calls.append(path)
        return {"kind": "video"}

    monkeypatch.setattr(viosc, "probe_media_meta", probe)

    assert viosc.classify_media_kind("/m/fake.mp4") == "video"
    assert calls == ["/m/fake.mp4"], "a successful probe must run exactly once"


def test_classify_image(monkeypatch):
    monkeypatch.setattr(viosc, "probe_media_meta", lambda p: {"kind": "image"})

    assert viosc.classify_media_kind("/m/fake.png") == "image"


def test_classify_none_retries_once_then_other(monkeypatch):
    calls = []

    def probe(path):
        calls.append(path)
        viosc.MEDIA_META_CACHE[path] = None  # real probe_media_meta caches the failure
        return None

    monkeypatch.setattr(viosc, "probe_media_meta", probe)

    assert viosc.classify_media_kind("/m/audio.mp4") == "other"
    assert len(calls) == 2, "a None probe must be retried exactly once"


def test_classify_none_then_success_uses_the_retry(monkeypatch):
    """The cached None is refreshed, so the second probe can succeed."""
    calls = []

    def probe(path):
        if path in viosc.MEDIA_META_CACHE and viosc.MEDIA_META_CACHE[path] is not None:
            return viosc.MEDIA_META_CACHE[path]
        calls.append(path)
        result = None if len(calls) == 1 else {"kind": "video"}
        viosc.MEDIA_META_CACHE[path] = result
        return result

    monkeypatch.setattr(viosc, "probe_media_meta", probe)

    assert viosc.classify_media_kind("/m/slow.mp4") == "video"
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# e57s01 — a source with no uri is "other" from the moment its entry exists
# ---------------------------------------------------------------------------


def test_create_empty_entry_seeds_other():
    entry = viosc.create_empty_vimix_entry()

    assert entry["media_kind"] == viosc.MEDIA_KIND_OTHER


def test_new_source_entry_starts_other(monkeypatch):
    """A non-media source never answers the uri get, so its entry stays other."""
    viosc.vimix_data.clear()
    monkeypatch.setattr(viosc, "request_uri_once", lambda name: None)

    viosc.process_vimix_message("/vimix/5/name", ["shader"])

    assert viosc.vimix_data[5]["media_kind"] == viosc.MEDIA_KIND_OTHER
