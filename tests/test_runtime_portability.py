"""macOS runtime portability: ffmpeg discovery, frozen restart, ANSI (e04s01)."""

import os
import stat
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import logbus
import viosc


@pytest.fixture(autouse=True)
def _restore(monkeypatch):
    monkeypatch.setattr(viosc, "FFMPEG_PATH", "ffmpeg")
    monkeypatch.setattr(viosc, "FFPROBE_PATH", "ffprobe")
    monkeypatch.setattr(viosc, "FFMPEG_AVAILABLE", True)
    yield


def _executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_restart_command_in_a_frozen_bundle_does_not_pass_the_executable_as_a_script(
    monkeypatch,
):
    monkeypatch.setattr(viosc.sys, "frozen", True, raising=False)
    monkeypatch.setattr(viosc.sys, "executable", "/Applications/viOSC.app/Contents/MacOS/viOSC")
    monkeypatch.setattr(
        viosc.sys,
        "argv",
        ["/Applications/viOSC.app/Contents/MacOS/viOSC", "--headless"],
    )

    assert viosc.restart_command() == [
        "/Applications/viOSC.app/Contents/MacOS/viOSC",
        "--headless",
    ]


def test_resolve_binary_keeps_an_absolute_configured_path(tmp_path):
    binary = _executable(tmp_path / "ffmpeg")
    assert viosc.resolve_binary(str(binary)) == str(binary)


def test_resolve_binary_probes_the_macos_package_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(viosc.sys, "platform", "darwin")
    monkeypatch.setattr(viosc, "DARWIN_BINARY_DIRS", (str(tmp_path),))
    monkeypatch.setattr(viosc.shutil, "which", lambda _name: None)
    binary = _executable(tmp_path / "ffmpeg")

    assert viosc.resolve_binary("ffmpeg") == str(binary)


def test_resolve_binary_finds_the_bundled_resources(monkeypatch, tmp_path):
    monkeypatch.setattr(viosc.sys, "frozen", True, raising=False)
    macos = tmp_path / "viOSC.app" / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    monkeypatch.setattr(viosc.sys, "executable", str(macos / "viOSC"))
    monkeypatch.setattr(viosc.shutil, "which", lambda _name: None)
    binary = _executable(tmp_path / "viOSC.app" / "Contents" / "Resources" / "bin" / "ffmpeg")

    assert viosc.resolve_binary("ffmpeg") == str(binary)


def test_missing_ffmpeg_degrades_instead_of_exiting(monkeypatch, capsys):
    monkeypatch.setattr(viosc, "resolve_binary", lambda _name: None)

    assert viosc.verify_dependencies() is False
    assert viosc.FFMPEG_AVAILABLE is False
    out = capsys.readouterr().out
    assert "Thumbnails and preview are disabled" in out


def test_resolved_ffmpeg_paths_are_stored(monkeypatch):
    monkeypatch.setattr(viosc, "resolve_binary", lambda name: f"/bundle/bin/{name}")

    assert viosc.verify_dependencies() is True
    assert viosc.FFMPEG_PATH == "/bundle/bin/ffmpeg"
    assert viosc.FFPROBE_PATH == "/bundle/bin/ffprobe"
    assert viosc.FFMPEG_AVAILABLE is True


def test_no_ffmpeg_skips_the_thumbnail_worker(monkeypatch, capsys):
    viosc.vimix_data.clear()
    entry = viosc.create_empty_vimix_entry()
    entry["name"] = "clipA"
    viosc.vimix_data[0] = entry
    monkeypatch.setattr(viosc, "FFMPEG_AVAILABLE", False)

    viosc.generate_thumbnails_worker(0, "/tmp/clip.mp4")

    assert "ffmpeg is not available" in capsys.readouterr().out
    viosc.vimix_data.clear()


def test_strip_ansi_keeps_the_message():
    assert logbus.strip_ansi("\x1b[90m[SYNC]\x1b[0m ok") == "[SYNC] ok"
    assert logbus.strip_ansi("plain") == "plain"
