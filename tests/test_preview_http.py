"""HTTP preview surface tests for viosc (e38s01).

Covers the /preview/<name>/file Range contract (206/Content-Range/Accept-
Ranges/416/404), the /preview/<name>/meta endpoint (ffprobe JSON cached per
path, video-vs-image kind), and the additive media_kind publish at
discovery time (state entry + broadcast). External boundaries (ffprobe via
probe_media_meta, OSC send) are monkeypatched; only the daemon's own logic
runs. The HTTP server binds an ephemeral port and is driven with urllib.
"""

import json
import os
import sys
import threading
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import preview_http
import viosc


def seed_source(idx=0, name="clip", uri=None):
    viosc.vimix_data.clear()
    viosc.MEDIA_META_CACHE.clear()
    entry = viosc.create_empty_vimix_entry()
    entry["name"] = name
    entry["uri"] = uri
    viosc.vimix_data[idx] = entry
    return entry


class PreviewServerFixture:
    """A running preview HTTP server on an ephemeral port for one test."""

    def __init__(self, tmp_path):
        self.httpd = preview_http.start_preview_server(
            "127.0.0.1", 0, viosc.vimix_data, lambda p: viosc.probe_media_meta(p)
        )
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"
        self.tmp = tmp_path

    def media_file(self, data=b"0123456789abcdef" * 32, name="clip.mp4"):
        p = self.tmp / name
        p.write_bytes(data)
        return str(p)

    def get(self, path, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def srv(tmp_path):
    f = PreviewServerFixture(tmp_path)
    yield f
    f.close()


VIDEO_META = {
    "kind": "video",
    "duration_s": 60.0,
    "width": 640,
    "height": 360,
    "fps": 30.0,
    "codec": "h264",
}
IMAGE_META = {
    "kind": "image",
    "duration_s": 0.04,
    "width": 640,
    "height": 360,
    "fps": 0.0,
    "codec": "mjpeg",
}


# --- Task 1: the /preview/<name>/file Range contract ---------------------


def test_file_range_exact_slice(srv):
    data = bytes(range(256))
    path = srv.media_file(data)
    seed_source(uri=path)
    status, headers, body = srv.get("/preview/clip/file", {"Range": "bytes=100-199"})
    assert status == 206
    assert headers["Content-Range"] == f"bytes 100-199/{len(data)}"
    assert headers["Accept-Ranges"] == "bytes"
    assert int(headers["Content-Length"]) == 100
    assert body == data[100:200]


def test_file_range_open_ended(srv):
    data = bytes(range(256))
    path = srv.media_file(data)
    seed_source(uri=path)
    status, headers, body = srv.get("/preview/clip/file", {"Range": "bytes=100-"})
    assert status == 206
    assert headers["Content-Range"] == f"bytes 100-255/{len(data)}"
    assert body == data[100:]


def test_file_range_unsatisfiable_returns_416(srv):
    data = bytes(range(256))
    path = srv.media_file(data)
    seed_source(uri=path)
    status, headers, _ = srv.get("/preview/clip/file", {"Range": f"bytes={len(data)}-"})
    assert status == 416
    assert headers["Content-Range"] == f"bytes */{len(data)}"


def test_file_no_range_returns_full_200(srv):
    data = bytes(range(256))
    path = srv.media_file(data)
    seed_source(uri=path)
    status, headers, body = srv.get("/preview/clip/file")
    assert status == 200
    assert headers["Accept-Ranges"] == "bytes"
    assert int(headers["Content-Length"]) == len(data)
    assert body == data


def test_file_unknown_name_returns_404(srv):
    seed_source(uri="/nonexistent/x.mp4")
    status, _, _ = srv.get("/preview/ghost/file")
    assert status == 404


def test_file_non_media_source_returns_404(srv):
    # a source present in the table but with no uri (shader/text/…) is not media
    seed_source(name="shader", uri=None)
    status, _, _ = srv.get("/preview/shader/file")
    assert status == 404


def test_file_missing_on_disk_returns_404(srv):
    seed_source(uri="/definitely/not/here.mp4")
    status, _, _ = srv.get("/preview/clip/file")
    assert status == 404


# --- Task 2: the /preview/<name>/meta endpoint ---------------------------


def test_meta_video_source(srv, monkeypatch):
    path = srv.media_file(name="clip.mp4")
    seed_source(uri=path)
    monkeypatch.setattr(viosc, "probe_media_meta", lambda p: dict(VIDEO_META))
    status, _, body = srv.get("/preview/clip/meta")
    assert status == 200
    assert json.loads(body) == VIDEO_META


def test_meta_image_source(srv, monkeypatch):
    path = srv.media_file(name="still.jpg")
    seed_source(uri=path)
    monkeypatch.setattr(viosc, "probe_media_meta", lambda p: dict(IMAGE_META))
    status, _, body = srv.get("/preview/clip/meta")
    assert status == 200
    assert json.loads(body)["kind"] == "image"


FFPROBE_VIDEO_JSON = json.dumps(
    {
        "format": {"duration": "60.0"},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 640,
                "height": 360,
                "r_frame_rate": "30/1",
                "nb_frames": "1800",
            }
        ],
    }
)


def test_meta_cached_per_path(srv, monkeypatch):
    # caching lives inside probe_media_meta (keyed by path): two requests
    # must run ONE subprocess probe on the real function
    path = srv.media_file(name="clip.mp4")
    seed_source(uri=path)
    calls = []

    class FakeProc:
        returncode = 0
        stdout = FFPROBE_VIDEO_JSON
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return FakeProc()

    monkeypatch.setattr(viosc.subprocess, "run", fake_run)
    assert srv.get("/preview/clip/meta")[0] == 200
    assert srv.get("/preview/clip/meta")[0] == 200
    assert len(calls) == 1  # second request served from the cache


def test_meta_unknown_or_non_media_returns_404(srv):
    seed_source(name="shader", uri=None)
    assert srv.get("/preview/shader/meta")[0] == 404
    assert srv.get("/preview/ghost/meta")[0] == 404


def test_meta_probe_failure_returns_404(srv, monkeypatch):
    path = srv.media_file(name="broken.mp4")
    seed_source(uri=path)
    monkeypatch.setattr(viosc, "probe_media_meta", lambda p: None)
    assert srv.get("/preview/clip/meta")[0] == 404


# --- Task 3: additive media_kind at discovery time ------------------------


def test_publish_media_kind_sets_entry_and_broadcasts(srv, monkeypatch):
    path = srv.media_file(name="clip.mp4")
    seed_source(uri=path)
    monkeypatch.setattr(viosc, "probe_media_meta", lambda p: dict(VIDEO_META))
    sent = []
    monkeypatch.setattr(
        viosc.ui_reply_client, "send_message", lambda addr, args: sent.append((addr, args))
    )
    viosc.publish_media_kind(0, path)
    assert viosc.vimix_data[0]["media_kind"] == "video"
    assert len(sent) == 1
    payload = json.loads(sent[0][1][0])
    assert payload["sources"]["0"]["media_kind"] == "video"


def test_publish_media_kind_skips_missing_file(srv, monkeypatch):
    seed_source(uri="/definitely/not/here.mp4")
    monkeypatch.setattr(viosc, "probe_media_meta", lambda p: dict(VIDEO_META))
    sent = []
    monkeypatch.setattr(
        viosc.ui_reply_client, "send_message", lambda addr, args: sent.append((addr, args))
    )
    viosc.publish_media_kind(0, "/definitely/not/here.mp4")
    # e57s01: the entry is seeded other; a missing file keeps it (transient)
    assert viosc.vimix_data[0]["media_kind"] == viosc.MEDIA_KIND_OTHER
    assert sent == []


def test_publish_media_kind_leaves_non_media_entries_untouched(srv, monkeypatch):
    seed_source(name="shader", uri=None)
    monkeypatch.setattr(viosc, "probe_media_meta", lambda p: dict(VIDEO_META))
    sent = []
    monkeypatch.setattr(
        viosc.ui_reply_client, "send_message", lambda addr, args: sent.append((addr, args))
    )
    viosc.publish_media_kind(0, None)
    # e57s01: no uri means other, and there is nothing to re-broadcast
    assert viosc.vimix_data[0]["media_kind"] == viosc.MEDIA_KIND_OTHER
    assert sent == []


def test_entry_seeded_other_flips_to_video_when_uri_arrives(srv, monkeypatch):
    """A media source answers its uri a moment later: other -> video."""
    entry = seed_source(name="clip", uri=None)
    assert entry["media_kind"] == viosc.MEDIA_KIND_OTHER
    path = srv.media_file(name="clip.mp4")
    entry["uri"] = path
    monkeypatch.setattr(viosc, "probe_media_meta", lambda p: dict(VIDEO_META))
    sent = []
    monkeypatch.setattr(
        viosc.ui_reply_client, "send_message", lambda addr, args: sent.append((addr, args))
    )

    viosc.publish_media_kind(0, path)

    assert entry["media_kind"] == "video"
    assert len(sent) == 1, "the flip must be broadcast"
