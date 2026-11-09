"""Read-only filesystem surface for the viOSC data plane (e43s01).

Covers the pure half: Media Roots expansion/containment (realpath, symlink
escape, `..`), media classification by vimix's own extension list, directory
listing (hidden filter, paging, dirs-first order, unreadable entries) and the
file resolver used by /fs/raw and /fs/meta. The HTTP routes live in the
http_api tests; this file is pure filesystem logic on tmp_path.

Headless: standard library only, no daemon, no network, no display.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fs_api


def _touch(path, data=b"x"):
    path.write_bytes(data)
    return str(path)


# ---------------------------------------------------------------------------
# classification (vimix's own extension list)
# ---------------------------------------------------------------------------


def test_classify_video_image_session_and_other():
    assert fs_api.classify("clip.mp4") == "video"
    assert fs_api.classify("CLIP.MKV") == "video"
    assert fs_api.classify("frame.jpg") == "image"
    assert fs_api.classify("still.WEBP") == "image"
    assert fs_api.classify("session.mix") == "session"
    assert fs_api.classify("notes.txt") == "other"
    assert fs_api.classify("noext") == "other"


# ---------------------------------------------------------------------------
# roots and containment
# ---------------------------------------------------------------------------


def test_roots_info_expands_and_reports_readable(tmp_path):
    info = fs_api.roots_info([str(tmp_path)])
    assert info[0]["path"] == str(tmp_path)
    assert info[0]["readable"] is True


def test_resolve_within_roots_accepts_a_path_inside(tmp_path):
    inside = tmp_path / "a" / "clip.mp4"
    inside.parent.mkdir()
    inside.write_bytes(b"x")
    assert fs_api.resolve_within_roots(str(inside), [str(tmp_path)]) == str(inside)


def test_resolve_within_roots_rejects_an_outside_path(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_bytes(b"x")
    assert fs_api.resolve_within_roots(str(outside), [str(tmp_path)]) is None


def test_resolve_within_roots_rejects_dot_dot_escape(tmp_path):
    inside = tmp_path / "sub"
    inside.mkdir()
    escape = str(inside / ".." / ".." / "etc" / "passwd")
    assert fs_api.resolve_within_roots(escape, [str(tmp_path)]) is None


def test_resolve_within_roots_rejects_a_symlink_escape(tmp_path):
    outside = tmp_path.parent / "secret.txt"
    outside.write_bytes(b"x")
    link = tmp_path / "link"
    link.symlink_to(outside)
    assert fs_api.resolve_within_roots(str(link), [str(tmp_path)]) is None


def test_resolve_within_roots_accepts_the_root_itself(tmp_path):
    assert fs_api.resolve_within_roots(str(tmp_path), [str(tmp_path)]) == str(tmp_path)


def test_resolve_within_roots_empty_is_none(tmp_path):
    assert fs_api.resolve_within_roots("", [str(tmp_path)]) is None


# ---------------------------------------------------------------------------
# directory listing
# ---------------------------------------------------------------------------


def test_list_directory_reports_kind_media_kind_and_dirs_first(tmp_path):
    (tmp_path / "sub").mkdir()
    _touch(tmp_path / "clip.mp4")
    _touch(tmp_path / "pic.png")
    _touch(tmp_path / "readme.txt")

    payload, error = fs_api.list_directory(str(tmp_path), [str(tmp_path)])

    assert error is None
    names = [(e["name"], e["kind"], e["media_kind"]) for e in payload["entries"]]
    assert names == [
        ("sub", "dir", "other"),
        ("clip.mp4", "file", "video"),
        ("pic.png", "file", "image"),
        ("readme.txt", "file", "other"),
    ]


def test_list_directory_hides_dotfiles_by_default_and_shows_them_on_request(tmp_path):
    _touch(tmp_path / ".hidden.mp4")
    _touch(tmp_path / "clip.mp4")

    hidden_off, _ = fs_api.list_directory(str(tmp_path), [str(tmp_path)])
    assert [e["name"] for e in hidden_off["entries"]] == ["clip.mp4"]

    hidden_on, _ = fs_api.list_directory(str(tmp_path), [str(tmp_path)], show_hidden=True)
    assert sorted(e["name"] for e in hidden_on["entries"]) == [".hidden.mp4", "clip.mp4"]


def test_list_directory_pages_with_offset_and_reports_total(tmp_path):
    for i in range(5):
        _touch(tmp_path / f"clip{i}.mp4")

    payload, _ = fs_api.list_directory(str(tmp_path), [str(tmp_path)], page_size=2, offset=2)

    assert payload["total"] == 5
    assert payload["offset"] == 2
    assert [e["name"] for e in payload["entries"]] == ["clip2.mp4", "clip3.mp4"]


def test_list_directory_outside_roots_is_forbidden(tmp_path):
    payload, error = fs_api.list_directory(str(tmp_path.parent), [str(tmp_path)])
    assert payload is None
    assert error == "forbidden"


def test_list_directory_missing_is_not_found(tmp_path):
    payload, error = fs_api.list_directory(str(tmp_path / "nope"), [str(tmp_path)])
    assert payload is None
    assert error == "not_found"


def test_list_directory_on_a_file_is_not_dir(tmp_path):
    _touch(tmp_path / "clip.mp4")
    payload, error = fs_api.list_directory(str(tmp_path / "clip.mp4"), [str(tmp_path)])
    assert payload is None
    assert error == "not_dir"


def test_list_directory_unreadable_entry_is_listed_with_a_flag(tmp_path):
    f = tmp_path / "locked.mp4"
    f.write_bytes(b"x")
    os.chmod(f, 0o000)
    try:
        payload, error = fs_api.list_directory(str(tmp_path), [str(tmp_path)])
        assert error is None
        entry = next(e for e in payload["entries"] if e["name"] == "locked.mp4")
        assert entry["readable"] is False
    finally:
        os.chmod(f, 0o600)


# ---------------------------------------------------------------------------
# file resolver (used by /fs/raw and /fs/meta)
# ---------------------------------------------------------------------------


def test_resolve_file_accepts_a_file_inside_and_rejects_a_dir(tmp_path):
    _touch(tmp_path / "clip.mp4")
    assert fs_api.resolve_file(str(tmp_path / "clip.mp4"), [str(tmp_path)]) == str(
        tmp_path / "clip.mp4"
    )
    assert fs_api.resolve_file(str(tmp_path), [str(tmp_path)]) is None


def test_resolve_file_rejects_outside_and_missing(tmp_path):
    assert fs_api.resolve_file(str(tmp_path / "nope.mp4"), [str(tmp_path)]) is None
    outside = tmp_path.parent / "outside.mp4"
    outside.write_bytes(b"x")
    assert fs_api.resolve_file(str(outside), [str(tmp_path)]) is None


# ---------------------------------------------------------------------------
# thumbnail cache (e43s03)
# ---------------------------------------------------------------------------


def test_thumbnail_cache_reuses_a_hit_and_regenerates_on_change(tmp_path):
    f = tmp_path / "clip.mp4"
    _touch(f, b"aaa")
    calls = []

    def generator(path):
        calls.append(path)
        return b"jpeg-" + str(len(calls)).encode()

    cache = fs_api.ThumbnailCache(generator)
    assert cache.get(str(f)) == b"jpeg-1"
    assert cache.get(str(f)) == b"jpeg-1"
    assert calls == [str(f)], "a second get must hit the cache"

    f.write_bytes(b"bbbb")  # size changed -> a new key
    assert cache.get(str(f)) == b"jpeg-2"
    assert len(calls) == 2


def test_thumbnail_cache_returns_none_when_the_generator_fails(tmp_path):
    f = tmp_path / "clip.mp4"
    _touch(f)
    cache = fs_api.ThumbnailCache(lambda path: None)
    assert cache.get(str(f)) is None


def test_thumbnail_cache_returns_none_for_a_missing_file(tmp_path):
    cache = fs_api.ThumbnailCache(lambda path: b"x")
    assert cache.get(str(tmp_path / "nope.mp4")) is None


def test_thumbnail_cache_evicts_beyond_its_bound(tmp_path):
    paths = [tmp_path / f"clip{i}.mp4" for i in range(3)]
    for p in paths:
        _touch(p)
    cache = fs_api.ThumbnailCache(lambda path: b"x", max_entries=2)
    for p in paths:
        cache.get(str(p))
    assert len(cache) == 2


def test_thumbnail_cache_can_be_cleared(tmp_path):
    f = tmp_path / "clip.mp4"
    _touch(f)
    cache = fs_api.ThumbnailCache(lambda path: b"x")
    cache.get(str(f))
    cache.clear()
    assert len(cache) == 0
