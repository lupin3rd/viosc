"""The /fs routes on the viOSC data plane (e43s01).

The filesystem logic is covered by test_fs_api; here we prove the HTTP contract:
`/fs/roots`, `/fs/list` (paging, 403 outside the roots, 404 missing),
`/fs/raw` (RFC 7233 Range reuse), `/fs/meta`, the absence of the surface on an
older daemon (no fs injected -> 404) and the pairing gate (401 without a token).

The real preview server binds an ephemeral port and is driven with urllib.
"""

import json
import os
import sys
import threading
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fs_api
import mix_schema
import pairing
import preview_http

BODY = b"0123456789abcdef" * 32  # 512 bytes, for Range tests


class FakeFs:
    """The daemon adapter, bound to one tmp root."""

    def __init__(self, root):
        self.root = str(root)
        self.blob = None

    def roots(self):
        return fs_api.roots_info([self.root])

    def listing(self, path, offset):
        return fs_api.list_directory(path, [self.root], offset=offset)

    def resolve_file(self, path):
        return fs_api.resolve_file(path, [self.root])

    def thumb(self, path):
        return self.blob

    def sessions(self):
        return mix_schema.list_sessions(self.root + "_sessions")

    def write_session(self, name, files, *, overwrite=False, alphas=None):
        return mix_schema.write_session(
            self.root + "_sessions", name, files, overwrite=overwrite, alphas=alphas
        )


class FsServerFixture:
    def __init__(self, tmp_path, *, fs=True, gate=None, probe=None):
        self.root = tmp_path
        (tmp_path / "sub").mkdir(exist_ok=True)
        (tmp_path / "clip.mp4").write_bytes(BODY)
        (tmp_path / "pic.png").write_bytes(b"png")
        (tmp_path / "readme.txt").write_bytes(b"txt")
        self.fake_fs = FakeFs(tmp_path) if fs else None
        self.httpd = preview_http.start_preview_server(
            "127.0.0.1",
            0,
            {},
            probe or (lambda path: {"kind": "video"}),
            None,
            None,
            gate,
            self.fake_fs,
        )
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    @property
    @property
    def fs_thumb_blob(self):
        return self.fake_fs.blob if self.fake_fs else None

    @fs_thumb_blob.setter
    def fs_thumb_blob(self, value):
        if self.fake_fs is not None:
            self.fake_fs.blob = value

    def media_file(self, name="clip.mp4", data=BODY):
        path = self.root / name
        path.write_bytes(data)
        return str(path)

    def get(self, path, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def post_json(self, path, payload, headers=None):
        data = json.dumps(payload).encode("utf-8")
        merged = {"Content-Type": "application/json", **(headers or {})}
        req = urllib.request.Request(self.base + path, data=data, headers=merged, method="POST")
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
    f = FsServerFixture(tmp_path)
    yield f
    f.close()


def test_fs_roots_lists_the_configured_root(srv):
    status, _headers, body = srv.get("/fs/roots")
    assert status == 200
    payload = json.loads(body)
    assert payload[0]["path"] == str(srv.root)
    assert payload[0]["readable"] is True


def test_fs_list_returns_entries_with_media_kinds(srv):
    status, _headers, body = srv.get(f"/fs/list?path={srv.root}")
    assert status == 200
    names = [(e["name"], e["kind"], e["media_kind"]) for e in json.loads(body)["entries"]]
    assert names == [
        ("sub", "dir", "other"),
        ("clip.mp4", "file", "video"),
        ("pic.png", "file", "image"),
        ("readme.txt", "file", "other"),
    ]


def test_fs_list_pages(srv):
    status, _headers, body = srv.get(f"/fs/list?path={srv.root}&offset=1")
    assert status == 200
    payload = json.loads(body)
    assert payload["offset"] == 1
    assert [e["name"] for e in payload["entries"]] == ["clip.mp4", "pic.png", "readme.txt"]


def test_fs_list_outside_the_roots_is_forbidden(srv):
    status, _headers, _body = srv.get(f"/fs/list?path={srv.root.parent}")
    assert status == 403


def test_fs_list_missing_is_not_found(srv):
    status, _headers, _body = srv.get(f"/fs/list?path={srv.root}/nope")
    assert status == 404


def test_fs_raw_serves_the_file_with_range(srv):
    status, headers, body = srv.get(f"/fs/raw?path={srv.root}/clip.mp4")
    assert status == 200
    assert headers.get("Accept-Ranges") == "bytes"
    assert body == BODY

    status, headers, body = srv.get(
        f"/fs/raw?path={srv.root}/clip.mp4", headers={"Range": "bytes=0-9"}
    )
    assert status == 206
    assert headers.get("Content-Range") == f"bytes 0-9/{len(BODY)}"
    assert body == BODY[:10]


def test_fs_raw_rejects_a_directory_and_a_missing_file(srv):
    assert srv.get(f"/fs/raw?path={srv.root}")[0] == 404
    assert srv.get(f"/fs/raw?path={srv.root}/nope.mp4")[0] == 404


def test_fs_meta_uses_the_injected_probe(tmp_path):
    f = FsServerFixture(tmp_path, probe=lambda path: {"kind": "video", "duration_s": 3.5})
    try:
        status, _headers, body = f.get(f"/fs/meta?path={f.root}/clip.mp4")
        assert status == 200
        assert json.loads(body)["duration_s"] == 3.5
        # a non-media path is the probe's 404
        f2 = FsServerFixture(tmp_path, probe=lambda path: None)
        try:
            assert f2.get(f"/fs/meta?path={f2.root}/clip.mp4")[0] == 404
        finally:
            f2.close()
    finally:
        f.close()


def test_older_daemon_without_the_surface_returns_404(tmp_path):
    f = FsServerFixture(tmp_path, fs=False)
    try:
        assert f.get("/fs/roots")[0] == 404
        assert f.get(f"/fs/list?path={f.root}")[0] == 404
        assert f.get(f"/fs/raw?path={f.root}/clip.mp4")[0] == 404
    finally:
        f.close()


def test_fs_routes_are_behind_the_pairing_gate(tmp_path):
    gate = pairing.PairingGate(True, "1234", pairing.PeerRegistry(), 300)
    f = FsServerFixture(tmp_path, gate=gate)
    try:
        assert f.get("/fs/roots")[0] == 401
        token = gate.authenticate("1234", "127.0.0.1")
        status, _headers, _body = f.get("/fs/roots", headers={"Authorization": f"Bearer {token}"})
        assert status == 200
    finally:
        f.close()


def test_fs_thumb_serves_a_jpeg_for_a_media_file(srv):
    srv.fs_thumb_blob = b"jpeg-bytes"
    status, headers, body = srv.get(f"/fs/thumb?path={srv.root}/clip.mp4")
    assert status == 200
    assert headers.get("Content-Type") == "image/jpeg"
    assert body == b"jpeg-bytes"


def test_fs_thumb_is_404_for_a_non_media_file_or_outside(srv):
    srv.fs_thumb_blob = None
    assert srv.get(f"/fs/thumb?path={srv.root}/readme.txt")[0] == 404
    assert srv.get(f"/fs/thumb?path={srv.root}/nope.mp4")[0] == 404


def test_fs_session_write_then_list(srv):
    media = srv.media_file(name="clip.mp4")

    status, _headers, body = srv.post_json("/fs/session", {"name": "Tonight", "files": [media]})
    assert status == 200
    result = json.loads(body)
    assert result["name"] == "Tonight"
    assert result["file"].endswith("Tonight.mix")

    status, _headers, body = srv.get("/fs/sessions")
    assert status == 200
    listed = json.loads(body)
    assert listed[0]["name"] == "Tonight"
    assert listed[0]["sources"][0]["uri"] == media


def test_fs_session_write_rejects_bad_name_and_missing_file(srv):
    assert srv.post_json("/fs/session", {"name": "", "files": ["/nope"]})[0] == 400
    assert srv.post_json("/fs/session", {"name": "X", "files": "nope"})[0] == 400
    media = srv.media_file(name="clip.mp4")
    assert srv.post_json("/fs/session", {"name": "X", "files": [media + ".gone"]})[0] == 409


def test_fs_session_write_accepts_an_alphas_map(srv):
    media = srv.media_file(name="clip.mp4")

    status, _headers, _body = srv.post_json(
        "/fs/session", {"name": "Set", "files": [media], "alphas": {media: -0.3}}
    )

    assert status == 200
    assert srv.post_json("/fs/session", {"name": "X", "files": [media], "alphas": "nope"})[0] == 400


def test_fs_session_write_overwrite_replaces_the_named_file(srv):
    media = srv.media_file(name="clip.mp4")

    first = json.loads(srv.post_json("/fs/session", {"name": "Set", "files": [media]})[2])
    second = json.loads(
        srv.post_json("/fs/session", {"name": "Set", "files": [media], "overwrite": True})[2]
    )

    assert second["file"] == first["file"]


def test_fs_session_write_is_behind_the_pairing_gate(tmp_path):
    gate = pairing.PairingGate(True, "1234", pairing.PeerRegistry(), 300)
    f = FsServerFixture(tmp_path, gate=gate)
    try:
        assert f.post_json("/fs/session", {"name": "X", "files": ["/nope"]})[0] == 401
        token = gate.authenticate("1234", "127.0.0.1")
        status, _headers, _body = f.post_json(
            "/fs/session",
            {"name": "X", "files": [f.media_file(name="clip.mp4")]},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert status == 200
    finally:
        f.close()
