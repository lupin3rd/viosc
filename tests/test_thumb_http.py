"""Thumbnails on the HTTP data plane (e41s03) — GET /thumb/<name>/<index>.

A thumbnail is 10-17 KB, i.e. ~12 IP fragments, and UDP never retransmits: one
lost fragment discarded the whole image (BUG-2026-08-27T201742 fixed only the
8 KB truncation). The blob is now a resource fetched by NAME through the
daemon's live state table, exactly like the OSC surface — no path is ever
opened from the request, so there is no traversal surface.

Boundaries monkeypatched; the real HTTP server runs on an ephemeral port and is
driven with urllib.
"""

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
    entry = viosc.create_empty_vimix_entry()
    entry["name"] = name
    entry["uri"] = uri
    viosc.vimix_data[idx] = entry
    return entry


class ThumbServerFixture:
    """A running data-plane server with the daemon's thumbnail resolver wired."""

    def __init__(self):
        self.httpd = preview_http.start_preview_server(
            "127.0.0.1",
            0,
            viosc.vimix_data,
            lambda p: viosc.probe_media_meta(p),
            viosc.resolve_thumbnail_blob,
        )
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def get(self, path):
        req = urllib.request.Request(self.base + path)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def srv():
    f = ThumbServerFixture()
    yield f
    f.close()


def test_serves_a_cached_frame(srv):
    entry = seed_source(name="clip", uri="/mnt/clip.mp4")
    entry["thumbnails"] = [b"frame0", b"frame1", b"frame2"]

    status, headers, body = srv.get("/thumb/clip/1")

    assert status == 200
    assert headers["Content-Type"] == "image/jpeg"
    assert headers["Content-Length"] == str(len(b"frame1"))
    assert body == b"frame1"


def test_serves_by_index_too(srv):
    entry = seed_source(name="clip", uri="/mnt/clip.mp4")
    entry["thumbnails"] = [b"frame0", b"frame1"]

    assert srv.get("/thumb/0/0")[2] == b"frame0"


def test_unknown_name_returns_404(srv):
    seed_source(name="clip", uri="/mnt/clip.mp4")

    assert srv.get("/thumb/nope/0")[0] == 404


def test_non_media_source_returns_404(srv):
    seed_source(name="shader", uri=None)

    assert srv.get("/thumb/shader/0")[0] == 404


def test_out_of_range_index_returns_404(srv):
    entry = seed_source(name="clip", uri="/mnt/clip.mp4")
    entry["thumbnails"] = [b"only-frame"]

    assert srv.get("/thumb/clip/3")[0] == 404


def test_non_numeric_index_returns_404(srv):
    entry = seed_source(name="clip", uri="/mnt/clip.mp4")
    entry["thumbnails"] = [b"only-frame"]

    assert srv.get("/thumb/clip/all")[0] == 404


def test_missing_index_returns_404(srv):
    entry = seed_source(name="clip", uri="/mnt/clip.mp4")
    entry["thumbnails"] = [b"only-frame"]

    assert srv.get("/thumb/clip")[0] == 404


def test_self_heals_an_empty_cache(srv, monkeypatch):
    entry = seed_source(name="clip", uri="/mnt/clip.mp4")
    entry["media_kind"] = "video"  # e57s01: other short-circuits the heal
    monkeypatch.setattr(viosc.os.path, "exists", lambda p: True)
    monkeypatch.setattr(viosc, "classify_media_kind", lambda p: "video")
    monkeypatch.setattr(
        viosc, "extract_thumbnails_from_file", lambda path, count=3: [b"healed0", b"healed1"]
    )

    status, _headers, body = srv.get("/thumb/clip/1")

    assert status == 200
    assert body == b"healed1"
