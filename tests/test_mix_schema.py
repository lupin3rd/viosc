"""vimix .mix schema (e43s06) — build and parse.

Locks the contract the rig spike proved (SPIKE-file-manager.md, SPIKE-1 PASSED):
the multi-root fragment, one <Source> per file with no <MediaPlayer>, unique ids
(Source AND inner nodes), readable unique names, and the centred placement that
matches vimix's own default.

Headless: stdlib only, no daemon, no vimix.
"""

import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mix_schema


def _wrapper(text):
    return mix_schema.parse_fragment(text)


def test_build_session_emits_the_validated_fragment():
    text = mix_schema.build_session(["/m/a.mp4", "/m/b.mkv"])

    assert text.startswith("<?xml")
    assert '<vimix major="0" minor="5" size="2" total="2"' in text
    assert "<Session " in text and "</Session>" in text
    assert text.count("<Source ") == 2
    assert "<uri>/m/a.mp4</uri>" in text and "<uri>/m/b.mkv</uri>" in text
    assert "<MediaPlayer" not in text, "the loader defaults the player"


def test_ids_are_unique_across_sources_and_inner_nodes():
    text = mix_schema.build_session(["/m/a.mp4", "/m/b.mp4", "/m/c.mp4"])
    ids = re.findall(r'\bid="(\d+)"', text)
    assert len(ids) > 10, "every view node carries an id"
    assert len(ids) == len(set(ids)), "duplicate node ids would cross-link sources"


def test_names_come_from_the_stems_and_are_deduplicated():
    text = mix_schema.build_session(["/one/clip.mp4", "/two/clip.mp4", "/three/other.mp4"])
    names = [s["name"] for s in mix_schema.parse_sources(text)]
    assert names[0] == "clip" and names[2] == "other"
    assert names[1].startswith("clip_"), "the second 'clip' is deduplicated"
    assert len(set(names)) == 3


def test_source_names_are_vimix_safe():
    assert mix_schema.sanitize_name("Underwater dance scene.mp4") == "Underwater_dance_scene_mp4"
    assert mix_schema.sanitize_name("Häxan (1922)") == "H_xan_1922"
    assert mix_schema.sanitize_name("...") == mix_schema.DEFAULT_NAME_PREFIX
    assert len(mix_schema.sanitize_name("x" * 200)) == mix_schema.MAX_NAME_LENGTH


def test_paths_with_xml_characters_round_trip():
    path = "/m/A & B <take 2>/clip.mp4"
    text = mix_schema.build_session([path])
    assert "&amp;" in text and "&lt;" in text
    assert mix_schema.parse_sources(text)[0]["uri"] == path


def test_generated_sources_sit_inside_the_output_frame():
    """The output derives from the Geometry translation (`Source::update` copies
    Geometry -> Rendering) and the output frame is [-1, 1] (FrameBuffer's
    ortho(-1,1,1,-1)); the default Geometry view shows about |x| <= 2.78
    (SCENE_UNIT 5 / GEOMETRY_DEFAULT_SCALE 1.8). A source placed outside that box
    is invisible in the Geometry window and off the output — a 1-source draft
    rendered black (BUG-2026-09-15T164717). Generated sources use vimix's own
    default placement: centred, exactly as its "Fit all" action does."""
    text = mix_schema.build_session([f"/m/{i}.mp4" for i in range(6)])
    session = _wrapper(text).find("Session")
    for source in session.findall("Source"):
        translation = source.find("Geometry/Node/translation/vec3")
        assert translation is not None, "the writer emits the rig-validated Geometry node"
        assert float(translation.get("x")) == 0.0, "the source must be inside the output frame"
        assert float(translation.get("y")) == 0.0, "the source must be inside the output frame"


def test_sources_are_capped():
    text = mix_schema.build_session([f"/m/{i}.mp4" for i in range(mix_schema.MAX_SOURCES + 10)])
    assert text.count("<Source ") == mix_schema.MAX_SOURCES


def test_parse_sources_reads_a_real_fragment_shape():
    text = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<vimix major="0" minor="5" size="1" total="1"/>\n\n'
        "<!-- a comment between top-level elements -->\n"
        '<Session id="7" activationThreshold="1.3">\n'
        '  <Source id="1" name="one" type="MediaSource"><uri>/m/one.mp4</uri></Source>\n'
        "</Session>\n<Snapshots/>\n"
    )
    assert mix_schema.parse_sources(text) == [{"name": "one", "uri": "/m/one.mp4"}]


def test_parse_sources_of_garbage_is_empty():
    assert mix_schema.parse_sources("not xml at all") == []
    assert mix_schema.parse_sources("<vimix major='0'/>") == []


# ---------------------------------------------------------------------------
# the session store (POST /fs/session + GET /fs/sessions)
# ---------------------------------------------------------------------------


def test_write_session_creates_the_file_atomically(tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"x")
    out = tmp_path / "sessions"

    result, error = mix_schema.write_session(str(out), "Tonight", [str(media)])

    assert error is None
    assert result["file"].endswith("Tonight.mix")
    assert os.path.isfile(result["file"])
    assert not os.path.exists(result["file"] + ".tmp"), "atomic write leaves no tmp"
    assert result["sources"] == [{"name": "clip", "uri": str(media)}]


def test_alpha_is_encoded_in_the_mixing_circle_and_the_blending_colour():
    text = mix_schema.build_session(
        ["/m/a.mp4", "/m/b.mp4"], alphas={"/m/a.mp4": 1.0, "/m/b.mp4": 0.0}
    )
    sources = mix_schema.parse_fragment(text).find("Session").findall("Source")
    centre = sources[0].find("Mixing/Node/translation/vec3")
    assert (float(centre.get("x")), float(centre.get("y"))) == (0.0, 0.0)
    clear = sources[1].find("Mixing/Node/translation/vec3")
    radius = (float(clear.get("x")) ** 2 + float(clear.get("y")) ** 2) ** 0.5
    assert abs(radius - 1.0) < 1e-6, "alpha 0 sits at the circle edge"
    assert float(sources[1].find("Blending/color/vec4").get("w")) == 0.0


def test_a_parked_alpha_sits_beyond_the_activation_threshold():
    text = mix_schema.build_session(["/m/a.mp4"], alphas={"/m/a.mp4": -0.3})
    node = mix_schema.parse_fragment(text).find("Session").find("Source")
    radius = node.find("Mixing/Node/translation/vec3")
    distance = (float(radius.get("x")) ** 2 + float(radius.get("y")) ** 2) ** 0.5
    assert distance > float(mix_schema.ACTIVATION_THRESHOLD)
    assert float(node.find("Blending/color/vec4").get("w")) == 0.0


def test_absent_alphas_keep_the_legacy_full_opacity():
    text = mix_schema.build_session(["/m/a.mp4"])
    node = mix_schema.parse_fragment(text).find("Session").find("Source")
    radius = node.find("Mixing/Node/translation/vec3")
    assert (float(radius.get("x")), float(radius.get("y"))) == (0.0, 0.0)
    assert float(node.find("Blending/color/vec4").get("w")) == 1.0


def test_alpha_distance_is_the_inverse_of_vimixs_transfer_function():
    for alpha in (0.0, 0.25, 0.5, 0.75, 1.0):
        distance = mix_schema.alpha_distance(alpha)
        back = 0.5 + 0.5 * math.cos(math.pi * min(max(distance * math.sqrt(distance), 0.0), 1.0))
        assert abs(back - alpha) < 1e-6, f"alpha {alpha} round-trips"


def test_write_session_overwrite_replaces_the_named_file(tmp_path):
    """Save with the chosen name replaces it (user decision 2026-09-15, e44s02)."""
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"x")
    out = tmp_path / "sessions"

    first, _ = mix_schema.write_session(str(out), "Set", [str(media)])
    second, _ = mix_schema.write_session(str(out), "Set", [str(media)], overwrite=True)

    assert second["file"] == first["file"]
    assert not (out / "Set_2.mix").exists(), "an overwrite writes no suffixed copy"


def test_write_session_suffixes_a_collision_deterministically(tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"x")
    out = tmp_path / "sessions"

    first, _ = mix_schema.write_session(str(out), "Set", [str(media)])
    second, _ = mix_schema.write_session(str(out), "Set", [str(media)])

    assert first["file"] != second["file"]
    assert second["file"].endswith("Set_2.mix")


def test_write_session_rejects_bad_names_and_empty_or_missing_files(tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"x")
    out = tmp_path / "sessions"

    assert mix_schema.write_session(str(out), "", [str(media)])[1] == "bad_name"
    assert mix_schema.write_session(str(out), "...", [str(media)])[1] == "bad_name"
    assert mix_schema.write_session(str(out), "Set", [])[1] == "no_files"
    missing = mix_schema.write_session(str(out), "Set", [str(tmp_path / "gone.mp4")])
    assert missing[1] == "missing_file"
    assert mix_schema.safe_session_filename("../../etc/passwd") == "etc_passwd.mix"
    assert mix_schema.safe_session_filename("a/b") == "a_b.mix"


def test_list_sessions_reads_every_mix_with_its_sources(tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"x")
    out = tmp_path / "sessions"
    mix_schema.write_session(str(out), "One", [str(media)])
    (out / "notes.txt").write_text("ignore me")
    (out / "broken.mix").write_text("not xml")

    listed = mix_schema.list_sessions(str(out))

    names = {entry["name"] for entry in listed}
    assert names == {"One", "broken"}
    one = next(entry for entry in listed if entry["name"] == "One")
    assert one["sources"][0]["uri"] == str(media)
    assert next(entry for entry in listed if entry["name"] == "broken")["sources"] == []


def test_list_sessions_of_a_missing_directory_is_empty(tmp_path):
    assert mix_schema.list_sessions(str(tmp_path / "nope")) == []


def test_session_header_declares_the_vimix_session_version():
    """vimix warns ONLY when the FILE version is NEWER than the app's session-XML
    version (SessionCreator.cpp: `version_minor > XML_VERSION_MINOR`). The
    installed vimix has XML_VERSION_MINOR 5 (src/defines.h) — NOT the 9 of the
    config file `~/.config/vimix/vimix.xml`, a different version line."""
    text = mix_schema.build_session(["/m/a.mp4"])
    assert f'major="{mix_schema.VIMIX_MAJOR}" minor="{mix_schema.VIMIX_MINOR}"' in text
    assert (mix_schema.VIMIX_MAJOR, mix_schema.VIMIX_MINOR) == (0, 5)


def test_sources_sit_inside_the_mixing_circle():
    """vimix encodes the OUTPUT alpha in the MIXING node: its distance from the
    centre maps to alpha (0 -> alpha 1, 1 -> alpha 0, >1 -> inactive). The
    rig-validated template carried a source parked at (-1, 1) — distance 1.41,
    i.e. TRANSPARENT — so a loaded draft rendered a black output. Every generated
    source must sit at the centre (vimix's own default for a new source)."""
    text = mix_schema.build_session(["/m/a.mp4", "/m/b.mp4", "/m/c.mp4"])
    session = _wrapper(text).find("Session")
    for source in session.findall("Source"):
        translation = source.find("Mixing/Node/translation/vec3")
        assert translation is not None
        distance = (float(translation.get("x")) ** 2 + float(translation.get("y")) ** 2) ** 0.5
        assert distance < 0.1, "a source outside the mixing circle is transparent"


def test_generated_sources_are_opaque():
    """`Source::alpha()` returns `blendingShader()->color.a`, i.e. the `w` of the
    <Blending><color> node. The template came from a session where the source was
    FADED OUT (w=0), so every generated source was alpha 0 and the output was
    black. Generated sources must be fully opaque."""
    text = mix_schema.build_session(["/m/a.mp4", "/m/b.mp4"])
    session = _wrapper(text).find("Session")
    for source in session.findall("Source"):
        color = source.find("Blending/color/vec4")
        assert color is not None, "the Blending node carries the alpha"
        assert (color.get("x"), color.get("y"), color.get("z")) == ("1", "1", "1")
        assert float(color.get("w")) == 1.0, "alpha 0 makes the source invisible"
