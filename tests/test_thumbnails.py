"""Thumbnail pipeline tests for viosc (e10s01 hardening).

Covers BUG-2026-08-26T230838: failures must be logged with a reason, a failed
regeneration must never wipe a good cached thumbnail, and an on-demand request
with an empty cache but a valid URI must regenerate and reply.

External boundaries (subprocess/ffmpeg/ffprobe, filesystem, OSC send) are
monkeypatched; only the daemon's own logic runs.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import viosc


class FakeProc:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def seed_source(idx=0, uri="/mnt/fake/media.mp4", thumbs=None):
    viosc.vimix_data.clear()
    entry = viosc.create_empty_vimix_entry()
    entry["uri"] = uri
    if thumbs is not None:
        entry["thumbnails"] = list(thumbs)
    viosc.vimix_data[idx] = entry
    return idx


def fake_run(ffprobe_out=b"100.0\n", ffmpeg_proc=None):
    """subprocess.run fake that answers ffprobe with a duration and ffmpeg with ffmpeg_proc."""

    def _run(cmd, *a, **kw):
        if cmd[0] == viosc.FFPROBE_PATH:
            # e57s01: probe_media_meta runs the JSON probe first (video stream);
            # get_video_duration runs the plain-duration probe in extraction.
            if "-print_format" in cmd:
                return FakeProc(stdout=_video_probe_json())
            return FakeProc(stdout=ffprobe_out)
        if ffmpeg_proc is not None:
            return ffmpeg_proc
        return FakeProc(stdout=b"\xff\xd8" * 60)  # plausible jpeg payload > 100 B

    return _run


def _video_probe_json():
    """A minimal ffprobe JSON body that probe_media_meta reads as a video."""
    return json.dumps(
        {
            "streams": [
                {
                    "codec_type": "video",
                    "width": 320,
                    "height": 180,
                    "nb_frames": "10",
                    "r_frame_rate": "25/1",
                    "codec_name": "h264",
                }
            ],
            "format": {"duration": "100.0"},
        }
    ).encode()


@pytest.fixture(autouse=True)
def clean_state():
    viosc.vimix_data.clear()
    viosc.known_indices.clear()
    yield
    viosc.vimix_data.clear()


def fake_media_exists(monkeypatch):
    """Pretend the fake media paths exist on disk."""
    monkeypatch.setattr(viosc.os.path, "exists", lambda p: True)


class SyncThread:
    """Runs a spawned thread's target synchronously, so regen paths are testable."""

    def __init__(self, target=None, args=(), kwargs=None, **kw):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


# ---------------------------------------------------------------------------
# e10s01t2 — RED: reason-specific failure logging
# ---------------------------------------------------------------------------


def test_missing_file_logs_reason_and_keeps_cache(capsys):
    seed_source(0, uri="/nonexistent/definitely-missing.mp4", thumbs=[b"old-frame"])
    viosc.generate_thumbnails_worker(0, "/nonexistent/definitely-missing.mp4")
    out = capsys.readouterr().out
    assert "File not found" in out, f"expected a 'File not found' log, got: {out!r}"
    assert viosc.vimix_data[0]["thumbnails"] == [b"old-frame"], (
        "a missing file must not wipe the cached thumbnail"
    )


def test_ffmpeg_failure_logs_reason_and_keeps_cache(capsys, monkeypatch):
    seed_source(0, uri="/mnt/fake/media.mp4", thumbs=[b"old-frame"])
    fake_media_exists(monkeypatch)
    monkeypatch.setattr(
        viosc.subprocess,
        "run",
        fake_run(ffmpeg_proc=FakeProc(returncode=1, stderr=b"decoder not found")),
    )
    viosc.generate_thumbnails_worker(0, "/mnt/fake/media.mp4")
    out = capsys.readouterr().out
    assert "ffmpeg failed" in out, f"expected an 'ffmpeg failed' log, got: {out!r}"
    assert viosc.vimix_data[0]["thumbnails"] == [b"old-frame"], (
        "a failed extraction must not wipe the cached thumbnail"
    )


def test_empty_frame_logs_reason_and_keeps_cache(capsys, monkeypatch):
    seed_source(0, uri="/mnt/fake/media.mp4", thumbs=[b"old-frame"])
    fake_media_exists(monkeypatch)
    monkeypatch.setattr(
        viosc.subprocess,
        "run",
        fake_run(ffmpeg_proc=FakeProc(returncode=0, stdout=b"")),
    )
    viosc.generate_thumbnails_worker(0, "/mnt/fake/media.mp4")
    out = capsys.readouterr().out
    assert "empty frame" in out.lower(), f"expected an 'empty frame' log, got: {out!r}"
    assert viosc.vimix_data[0]["thumbnails"] == [b"old-frame"], (
        "an empty extraction must not wipe the cached thumbnail"
    )


# ---------------------------------------------------------------------------
# e10s01t4 — RED: failed regeneration keeps the previous cache
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# e10s02 — RED: up to 3 distinct frames per media
# ---------------------------------------------------------------------------


class RecordingRun:
    """subprocess.run fake that records every ffmpeg '-ss' target it sees."""

    def __init__(self, duration=100.0, frame=b"\xff\xd8" * 60):
        self.duration = duration
        self.frame = frame
        self.seek_times = []

    def __call__(self, cmd, *a, **kw):
        if cmd[0] == viosc.FFPROBE_PATH:
            if "-print_format" in cmd:
                return FakeProc(stdout=_video_probe_json())
            return FakeProc(stdout=f"{self.duration}\n".encode())
        if cmd[0] == viosc.FFMPEG_PATH:
            ss_idx = cmd.index("-ss") + 1
            self.seek_times.append(float(cmd[ss_idx]))
            return FakeProc(stdout=self.frame)
        return FakeProc()


def test_count3_requests_three_distinct_times(monkeypatch):
    """count=3 must seek 3 distinct jittered targets and return 3 frames."""
    monkeypatch.setattr(viosc.random, "uniform", lambda lo, hi: 0.0)  # no jitter
    run = RecordingRun(duration=100.0)
    monkeypatch.setattr(viosc.subprocess, "run", run)

    frames = viosc.extract_thumbnails_from_file("/mnt/fake/media.mp4", count=3)

    assert len(run.seek_times) == 3, f"expected 3 ffmpeg calls, got {run.seek_times}"
    assert len(set(run.seek_times)) == 3, f"seek targets must be distinct: {run.seek_times}"
    assert len(frames) == 3


def test_image_duration_zero_returns_single_frame(monkeypatch):
    """Images (no duration) yield exactly 1 frame at t=0."""
    monkeypatch.setattr(viosc.random, "uniform", lambda lo, hi: 0.0)
    run = RecordingRun(duration=0.0)  # ffprobe N/A -> duration 0
    monkeypatch.setattr(viosc.subprocess, "run", run)

    frames = viosc.extract_thumbnails_from_file("/mnt/fake/image.png", count=3)

    assert run.seek_times == [0.0], f"image must seek once at t=0, got {run.seek_times}"
    assert len(frames) == 1


def test_short_clip_returns_min_distinct_frames(monkeypatch):
    """Very short clips collapse to as many distinct frames as possible (min 1)."""
    monkeypatch.setattr(viosc.random, "uniform", lambda lo, hi: 0.0)
    run = RecordingRun(duration=0.05)  # all anchors round to the same second
    monkeypatch.setattr(viosc.subprocess, "run", run)

    frames = viosc.extract_thumbnails_from_file("/mnt/fake/short.mp4", count=3)

    assert len(frames) == 1, f"expected a single distinct frame, got {len(frames)}"


def test_generate_worker_stores_up_to_three_frames(capsys, monkeypatch):
    """The generation worker caches the multi-frame list (contract for VJmix)."""
    seed_source(0, uri="/mnt/fake/media.mp4", thumbs=[])
    fake_media_exists(monkeypatch)
    monkeypatch.setattr(viosc.random, "uniform", lambda lo, hi: 0.0)
    run = RecordingRun(duration=100.0)
    monkeypatch.setattr(viosc.subprocess, "run", run)

    viosc.generate_thumbnails_worker(0, "/mnt/fake/media.mp4")

    assert len(viosc.vimix_data[0]["thumbnails"]) == 3
    out = capsys.readouterr().out
    assert "3 random thumbnail(s)" in out


# ---------------------------------------------------------------------------
# e10s01t6 — RED: on-demand request with empty cache + valid URI regenerates
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# e57s01 — probe-first: the kind decides the count BEFORE any ffmpeg call
# ---------------------------------------------------------------------------


def _spy_extract(monkeypatch):
    """Replace the extractor with a spy that records the requested count."""
    calls = []

    def _extract(path, count=1, width=320, height=180):
        calls.append(count)
        return [b"frame"] * count

    monkeypatch.setattr(viosc, "extract_thumbnails_from_file", _extract)
    return calls


def test_worker_video_extracts_three_and_publishes_video(monkeypatch):
    seed_source(0, uri="/mnt/fake/media.mp4", thumbs=[])
    fake_media_exists(monkeypatch)
    seen = []
    monkeypatch.setattr(viosc, "classify_media_kind", lambda p: (seen.append(p), "video")[1])
    calls = _spy_extract(monkeypatch)

    viosc.generate_thumbnails_worker(0, "/mnt/fake/media.mp4")

    assert seen == ["/mnt/fake/media.mp4"], "the worker must classify before extracting"
    assert calls == [viosc.THUMB_MAX_COUNT]
    assert len(viosc.vimix_data[0]["thumbnails"]) == 3
    assert viosc.vimix_data[0]["media_kind"] == "video"


def test_worker_image_extracts_exactly_one(monkeypatch):
    seed_source(0, uri="/mnt/fake/image.png", thumbs=[])
    fake_media_exists(monkeypatch)
    monkeypatch.setattr(viosc, "classify_media_kind", lambda p: "image")
    calls = _spy_extract(monkeypatch)

    viosc.generate_thumbnails_worker(0, "/mnt/fake/image.png")

    assert calls == [1], f"an image must ask for exactly one frame, got {calls}"
    assert len(viosc.vimix_data[0]["thumbnails"]) == 1
    assert viosc.vimix_data[0]["media_kind"] == "image"


def test_worker_other_runs_no_ffmpeg_and_logs_no_error(capsys, monkeypatch):
    seed_source(0, uri="/mnt/fake/audio.mp4", thumbs=[])
    fake_media_exists(monkeypatch)
    monkeypatch.setattr(viosc, "classify_media_kind", lambda p: viosc.MEDIA_KIND_OTHER)
    calls = _spy_extract(monkeypatch)

    viosc.generate_thumbnails_worker(0, "/mnt/fake/audio.mp4")

    assert calls == [], "an other source must not run ffmpeg"
    out = capsys.readouterr().out
    assert "no frames extracted" not in out, f"other must not log a thumbnail error: {out!r}"
    assert viosc.vimix_data[0]["media_kind"] == viosc.MEDIA_KIND_OTHER
    assert viosc.vimix_data[0]["thumbnails"] == []


# ---------------------------------------------------------------------------
# e57s01 — an "other" source never re-enters the probe+extract cycle
# ---------------------------------------------------------------------------


def test_thumbnails_for_other_does_not_regenerate(monkeypatch):
    seed_source(0, uri="/mnt/fake/audio.mp4", thumbs=[])
    fake_media_exists(monkeypatch)
    calls = []
    monkeypatch.setattr(viosc, "generate_thumbnails_worker", lambda i, u: calls.append((i, u)))

    assert viosc.thumbnails_for("0") is None
    assert calls == [], "an other source must not re-enter the thumbnail worker"


def test_regen_for_an_other_source_is_a_noop(monkeypatch):
    """`POST /thumb/regen` (the only regen path since e98s04) refuses 'other'."""
    seed_source(0, uri="/tmp/audio.mp3", thumbs=[])
    viosc.vimix_data[0]["name"] = "clip"
    viosc.vimix_data[0]["media_kind"] = viosc.MEDIA_KIND_OTHER
    calls = []
    monkeypatch.setattr(viosc, "generate_thumbnails_worker", lambda i, u: calls.append((i, u)))

    assert viosc.regen_thumb_for("clip") is False
    assert calls == []


def test_regen_for_an_unknown_source_is_a_noop():
    seed_source(0, uri="/tmp/clip.mp4", thumbs=[])
    viosc.vimix_data[0]["name"] = "clip"
    assert viosc.regen_thumb_for("ghost") is False
    assert viosc.regen_thumb_for("") is False


def test_regen_starts_the_worker_for_a_media_source(monkeypatch):
    seed_source(0, uri="/tmp/clip.mp4", thumbs=[])
    viosc.vimix_data[0]["name"] = "clip"
    viosc.vimix_data[0]["media_kind"] = "video"
    calls = []
    monkeypatch.setattr(viosc, "generate_thumbnails_worker", lambda i, u: calls.append((i, u)))
    monkeypatch.setattr(viosc.threading, "Thread", SyncThread)

    assert viosc.regen_thumb_for("clip") is True
    assert calls == [(0, "/tmp/clip.mp4")]
