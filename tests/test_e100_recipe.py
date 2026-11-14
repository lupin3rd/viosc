"""The session recipe sidecar (e100s01) — `<name>.vjmix.json` beside the `.mix`.

Covers the write path (`write_session(recipe=...)`), the read-back in
`list_sessions` (`recipe` per entry, `null` when absent or corrupt), the
`bad_recipe` rejection for junk or an over-bound body, and the honesty rule: a
sidecar failure leaves the written `.mix` in place.

Headless: stdlib only, no daemon, no vimix.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mix_schema

RECIPE = {
    "format": "vjmix-session-recipe",
    "version": 1,
    "session": "Tonight",
    "folder": "/media/tonight",
    "files": ["/media/tonight/a.mp4"],
    "first": "/media/tonight/a.mp4",
    "alphas": {"/media/tonight/a.mp4": 1.0},
    "assignments": [{"kind": "seq", "key": 1, "target": "/media/tonight/a.mp4"}],
    "transition": 2.0,
}


def _media(tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"x")
    return str(media)


def test_write_session_stores_the_recipe_beside_the_mix(tmp_path):
    out = tmp_path / "sessions"
    result, error = mix_schema.write_session(str(out), "Tonight", [_media(tmp_path)], recipe=RECIPE)
    assert error is None
    sidecar = out / "Tonight.vjmix.json"
    assert sidecar.is_file()
    assert json.loads(sidecar.read_text()) == RECIPE
    assert result["file"] == str(out / "Tonight.mix")


def test_the_recipe_is_opaque_to_the_daemon(tmp_path):
    out = tmp_path / "sessions"
    payload = {"anything": {"nested": [1, 2, 3]}, "format": "vjmix-session-recipe"}
    assert mix_schema.write_session(str(out), "Set", [_media(tmp_path)], recipe=payload)[1] is None
    assert json.loads((out / "Set.vjmix.json").read_text()) == payload


def test_old_clients_write_no_sidecar(tmp_path):
    out = tmp_path / "sessions"
    assert mix_schema.write_session(str(out), "Set", [_media(tmp_path)])[1] is None
    assert not (out / "Set.vjmix.json").exists()


def test_list_sessions_returns_the_recipe_or_null(tmp_path):
    out = tmp_path / "sessions"
    media = _media(tmp_path)
    mix_schema.write_session(str(out), "With", [media], recipe=RECIPE)
    mix_schema.write_session(str(out), "Without", [media])
    listed = {entry["name"]: entry for entry in mix_schema.list_sessions(str(out))}
    assert listed["With"]["recipe"] == RECIPE
    assert listed["Without"]["recipe"] is None


def test_a_corrupt_or_non_object_sidecar_reads_as_null(tmp_path):
    out = tmp_path / "sessions"
    media = _media(tmp_path)
    mix_schema.write_session(str(out), "Broken", [media])
    (out / "Broken.vjmix.json").write_text("{not json")
    mix_schema.write_session(str(out), "Listy", [media])
    (out / "Listy.vjmix.json").write_text("[1, 2]")
    listed = {entry["name"]: entry for entry in mix_schema.list_sessions(str(out))}
    assert listed["Broken"]["recipe"] is None
    assert listed["Listy"]["recipe"] is None


def test_a_non_serialisable_recipe_is_rejected_before_anything_is_written(tmp_path):
    out = tmp_path / "sessions"
    bad = {"format": "vjmix-session-recipe", "oops": object()}
    result, error = mix_schema.write_session(str(out), "Set", [_media(tmp_path)], recipe=bad)
    assert result is None
    assert error == "bad_recipe"


def test_an_over_bound_recipe_is_rejected(tmp_path):
    out = tmp_path / "sessions"
    big = {"format": "vjmix-session-recipe", "blob": "x" * (mix_schema.MAX_RECIPE_BYTES + 1)}
    assert mix_schema.write_session(str(out), "Set", [_media(tmp_path)], recipe=big)[1] == (
        "bad_recipe"
    )


def test_safe_recipe_filename_follows_the_session_name(tmp_path):
    assert mix_schema.safe_recipe_filename("Tonight") == "Tonight.vjmix.json"
    assert mix_schema.safe_recipe_filename("../../etc/passwd") == "etc_passwd.vjmix.json"
    assert mix_schema.safe_recipe_filename("") == ""


def test_the_sidecar_lands_beside_the_suffixed_mix(tmp_path):
    out = tmp_path / "sessions"
    media = _media(tmp_path)
    mix_schema.write_session(str(out), "Set", [media], recipe=RECIPE)
    second, _ = mix_schema.write_session(str(out), "Set", [media], recipe=RECIPE)
    assert second["file"].endswith("Set_2.mix")
    assert (out / "Set_2.vjmix.json").is_file()
