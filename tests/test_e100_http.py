"""The e100 HTTP surface: scan, folder-targeted sessions, recipe round-trip.

Proves the wire contract the wizard relies on: `GET /fs/scan` (top level vs
recursive, videos only, the root containment, the error statuses),
`POST /fs/session` with `dir` (the file lands in the chosen folder, outside the
roots is 403, a file is 400, a missing folder is 404, a malformed recipe is 400,
the sidecar is written and read back) and `GET /fs/sessions?dir=` with the
`recipe` key.

The real HTTP server binds an ephemeral port and is driven with urllib.
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
import http_api
import mix_schema

RECIPE = {
    "format": "vjmix-session-recipe",
    "version": 1,
    "session": "Set",
    "first": "a.mp4",
    "assignments": [{"kind": "mapper", "key": "/vimix/1", "target": "a.mp4"}],
    "transition": 1.5,
}


class FakeFs:
    """The daemon adapter, bound to one tmp root."""

    def __init__(self, root):
        self.root = str(root)
        self.sessions_dir = str(root) + "_sessions"

    def roots(self):
        return fs_api.roots_info([self.root])

    def listing(self, path, offset):
        return fs_api.list_directory(path, [self.root], offset=offset)

    def resolve_file(self, path):
        return fs_api.resolve_file(path, [self.root])

    def thumb(self, path):
        return None

    def scan(self, path, recursive):
        return fs_api.scan_videos(path, [self.root], recursive=recursive)

    def sessions(self, directory=""):
        if directory:
            real, error = fs_api.resolve_directory(directory, [self.root])
            if real is None:
                return None, error
            return mix_schema.list_sessions(real), None
        return mix_schema.list_sessions(self.sessions_dir), None

    def write_session(
        self, name, files, *, overwrite=False, alphas=None, directory="", recipe=None
    ):
        target = self.sessions_dir
        if directory:
            real, error = fs_api.resolve_directory(directory, [self.root])
            if real is None:
                return None, error
            target = real
        return mix_schema.write_session(
            target, name, files, overwrite=overwrite, alphas=alphas, recipe=recipe
        )


class Server:
    def __init__(self, tmp_path):
        self.root = tmp_path
        self.fake = FakeFs(tmp_path)
        self.httpd = http_api.start_http_server(
            "127.0.0.1",
            0,
            {},
            lambda path: {"kind": "video"},
            None,
            None,
            None,
            self.fake,
        )
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def video(self, name="a.mp4"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        return str(path)

    def get(self, path):
        req = urllib.request.Request(self.base + path)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def post(self, path, payload):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base + path,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def srv(tmp_path):
    server = Server(tmp_path)
    yield server
    server.close()


def test_scan_returns_only_videos_with_the_counts(srv):
    srv.video("a.mp4")
    srv.video("sub/b.mov")
    (srv.root / "notes.txt").write_text("x")
    status, payload = srv.get(f"/fs/scan?path={srv.root}")
    assert status == 200
    assert [entry["name"] for entry in payload["files"]] == ["a.mp4"]
    assert payload["total"] == 1
    assert payload["ignored"] == 1
    assert payload["truncated"] is False


def test_scan_recursive_carries_the_subfolder_files(srv):
    srv.video("a.mp4")
    srv.video("sub/b.mov")
    status, payload = srv.get(f"/fs/scan?path={srv.root}&recursive=1")
    assert status == 200
    assert [entry["name"] for entry in payload["files"]] == ["a.mp4", "b.mov"]
    assert payload["recursive"] is True


def test_scan_outside_the_roots_is_forbidden(srv):
    status, _payload = srv.get(f"/fs/scan?path={srv.root.parent}")
    assert status == 403


def test_scan_missing_is_not_found_and_a_file_is_not_a_dir(srv):
    assert srv.get(f"/fs/scan?path={srv.root}/gone")[0] == 404
    assert srv.get(f"/fs/scan?path={srv.video()}")[0] == 400


def test_session_write_lands_in_the_chosen_folder_with_its_recipe(srv):
    media = srv.video("a.mp4")
    status, payload = srv.post(
        "/fs/session",
        {"name": "Set", "files": [media], "dir": str(srv.root), "recipe": RECIPE},
    )
    assert status == 200
    assert payload["file"] == str(srv.root / "Set.mix")
    assert os.path.isfile(srv.root / "Set.vjmix.json")
    assert not os.path.exists(str(srv.root) + "_sessions"), "nothing written in the sessions dir"


def test_session_write_outside_the_roots_is_forbidden(srv):
    media = srv.video("a.mp4")
    status, payload = srv.post(
        "/fs/session",
        {"name": "Set", "files": [media], "dir": str(srv.root.parent)},
    )
    assert status == 403
    assert payload["error"] == "forbidden"


def test_session_write_missing_folder_is_not_found(srv):
    media = srv.video("a.mp4")
    status, _payload = srv.post(
        "/fs/session", {"name": "Set", "files": [media], "dir": str(srv.root / "gone")}
    )
    assert status == 404


def test_session_write_a_file_as_dir_is_a_bad_request(srv):
    media = srv.video("a.mp4")
    status, _payload = srv.post("/fs/session", {"name": "Set", "files": [media], "dir": media})
    assert status == 400


def test_a_malformed_recipe_is_rejected_before_writing(srv):
    media = srv.video("a.mp4")
    status, payload = srv.post(
        "/fs/session",
        {"name": "Set", "files": [media], "dir": str(srv.root), "recipe": "nope"},
    )
    assert status == 400
    assert payload["error"] == "invalid JSON body"
    assert not (srv.root / "Set.mix").exists()


def test_session_write_without_dir_keeps_the_sessions_directory(srv):
    media = srv.video("a.mp4")
    status, payload = srv.post("/fs/session", {"name": "Set", "files": [media]})
    assert status == 200
    assert payload["file"].startswith(str(srv.root) + "_sessions")


def test_sessions_listing_in_a_folder_carries_the_recipe(srv):
    media = srv.video("a.mp4")
    srv.post(
        "/fs/session",
        {"name": "Set", "files": [media], "dir": str(srv.root), "recipe": RECIPE},
    )
    status, payload = srv.get(f"/fs/sessions?dir={srv.root}")
    assert status == 200
    entry = next(item for item in payload if item["name"] == "Set")
    assert entry["recipe"] == RECIPE
    assert entry["file"] == str(srv.root / "Set.mix")


def test_sessions_listing_outside_the_roots_is_forbidden(srv):
    status, _payload = srv.get(f"/fs/sessions?dir={srv.root.parent}")
    assert status == 403


def test_the_default_sessions_listing_still_answers_a_list(srv):
    media = srv.video("a.mp4")
    srv.post("/fs/session", {"name": "Set", "files": [media]})
    status, payload = srv.get("/fs/sessions")
    assert status == 200
    assert isinstance(payload, list)
    assert payload[0]["name"] == "Set"
    assert payload[0]["recipe"] is None
