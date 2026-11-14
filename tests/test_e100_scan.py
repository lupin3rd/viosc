"""Folder scan for the session wizard (e100s01) — pure half.

Covers `fs_api.scan_videos`: videos only (vimix's extension list), the top-level
default, the recursive walk, hidden entries, the realpath containment on every
directory entered (a symlink escape is skipped, not walked), the cap with
`truncated`, and the listing-compatible error tags.

Headless: stdlib only, no daemon, no network.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fs_api


def _video(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def test_scan_returns_videos_only_top_level(tmp_path):
    _video(tmp_path / "b.mov")
    _video(tmp_path / "a.mp4")
    _video(tmp_path / "notes.txt")
    _video(tmp_path / "pic.png")
    (tmp_path / "sub").mkdir()
    _video(tmp_path / "sub" / "c.mkv")
    payload, error = fs_api.scan_videos(str(tmp_path), [str(tmp_path)])
    assert error is None
    assert [entry["name"] for entry in payload["files"]] == ["a.mp4", "b.mov"]
    assert payload["total"] == 2
    assert payload["ignored"] == 2, "the two non-video files are counted, not listed"
    assert payload["truncated"] is False


def test_scan_recursive_walks_subfolders(tmp_path):
    _video(tmp_path / "a.mp4")
    _video(tmp_path / "one" / "b.mp4")
    _video(tmp_path / "one" / "two" / "c.mp4")
    payload, error = fs_api.scan_videos(str(tmp_path), [str(tmp_path)], recursive=True)
    assert error is None
    assert [entry["name"] for entry in payload["files"]] == ["a.mp4", "b.mp4", "c.mp4"]
    assert payload["total"] == 3
    assert payload["recursive"] is True


def test_scan_skips_hidden_entries(tmp_path):
    _video(tmp_path / ".hidden.mp4")
    _video(tmp_path / "shown.mp4")
    payload, _ = fs_api.scan_videos(str(tmp_path), [str(tmp_path)])
    assert [entry["name"] for entry in payload["files"]] == ["shown.mp4"]


def test_scan_reports_absolute_paths_with_size_and_mtime(tmp_path):
    path = _video(tmp_path / "a.mp4", b"1234")
    payload, _ = fs_api.scan_videos(str(tmp_path), [str(tmp_path)])
    entry = payload["files"][0]
    assert entry["path"] == path
    assert entry["size"] == 4
    assert entry["mtime"] > 0


def test_scan_caps_and_says_truncated(tmp_path):
    for index in range(5):
        _video(tmp_path / f"clip{index}.mp4")
    payload, error = fs_api.scan_videos(str(tmp_path), [str(tmp_path)], max_files=3)
    assert error is None
    assert payload["total"] == 3
    assert payload["truncated"] is True


def test_scan_exactly_at_the_cap_is_not_truncated(tmp_path):
    for index in range(3):
        _video(tmp_path / f"clip{index}.mp4")
    payload, _ = fs_api.scan_videos(str(tmp_path), [str(tmp_path)], max_files=3)
    assert payload["total"] == 3
    assert payload["truncated"] is False


def test_scan_refuses_outside_the_roots(tmp_path):
    outside = tmp_path.parent / "outside-scan"
    outside.mkdir(exist_ok=True)
    _video(outside / "a.mp4")
    payload, error = fs_api.scan_videos(str(outside), [str(tmp_path)])
    assert payload is None
    assert error == "forbidden"


def test_scan_words_missing_and_not_a_directory(tmp_path):
    assert fs_api.scan_videos(str(tmp_path / "nope"), [str(tmp_path)])[1] == "not_found"
    path = _video(tmp_path / "a.mp4")
    assert fs_api.scan_videos(path, [str(tmp_path)])[1] == "not_dir"


def test_scan_does_not_walk_a_symlink_out_of_the_roots(tmp_path):
    outside = tmp_path.parent / "outside-link-target"
    outside.mkdir(exist_ok=True)
    _video(outside / "escape.mp4")
    link = tmp_path / "link"
    try:
        os.symlink(str(outside), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    _video(tmp_path / "inside.mp4")
    payload, error = fs_api.scan_videos(str(tmp_path), [str(tmp_path)], recursive=True)
    assert error is None
    assert [entry["name"] for entry in payload["files"]] == ["inside.mp4"]


def test_scan_ignores_an_unreadable_subfolder(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    _video(locked / "a.mp4")
    if os.geteuid() == 0:  # root ignores the mode, so the walk would succeed
        pytest.skip("root can read a 0o000 directory")
    os.chmod(locked, 0o000)
    try:
        payload, error = fs_api.scan_videos(str(tmp_path), [str(tmp_path)], recursive=True)
    finally:
        os.chmod(locked, 0o755)
    assert error is None
    assert payload["files"] == []
