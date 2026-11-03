"""Release pipeline guards (BUG-2026-11-03).

The daemon tests cannot exercise GitHub-hosted runner allocation, so these
tests assert the release configuration itself: supported macOS runner labels,
a version-agnostic release job, and a verifier that reports and checks the
architecture it actually built.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"


def load_workflow(name: str) -> dict:
    """Load a workflow as plain strings (YAML 1.1 would turn ``on`` into True)."""
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    return yaml.load(text, Loader=yaml.BaseLoader)


def test_workflows_use_supported_macos_runners() -> None:
    workflow = load_workflow("build.yml")

    assert workflow["name"] == "build"
    jobs = workflow["jobs"]
    assert jobs["macos-x64"]["runs-on"] == "macos-15-intel"
    assert "macos-15" in jobs["macos-arm64"]["runs-on"]
    assert workflow["on"]["workflow_dispatch"]["inputs"]["arm64_runner"]["default"] == "macos-15"
    assert "macos-arm64" in jobs["release"]["needs"]

    preflight = load_workflow("preflight.yml")
    matrix = preflight["jobs"]["preflight"]["strategy"]["matrix"]["os"]
    assert matrix == ["ubuntu-latest", "macos-15"]

    for name in ("build.yml", "preflight.yml"):
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        assert "macos-14" not in text, f"{name} still requests the retired macOS 14 image"


def test_release_job_is_version_agnostic() -> None:
    workflow = load_workflow("build.yml")
    steps = workflow["jobs"]["release"]["steps"]

    notes = [step for step in steps if step.get("id") == "notes"]
    assert len(notes) == 1, "the release job must resolve the notes path once"
    assert "RELEASE_NOTES_${GITHUB_REF_NAME#v}.md" in notes[0]["run"]

    release = next(
        step for step in steps if step.get("uses", "").startswith("softprops/action-gh-release")
    )
    assert release["with"]["name"] == "viOSC ${{ github.ref_name }}"
    assert release["with"]["body_path"] == "${{ steps.notes.outputs.path }}"
    assert any("steps.notes.outputs.path" in step.get("run", "") for step in steps)

    text = (WORKFLOWS / "build.yml").read_text(encoding="utf-8")
    assert "0.9.0" not in text, "the release job still hardcodes a version"


def test_macos_verifier_reports_and_checks_expected_arch() -> None:
    text = (ROOT / "scripts" / "verify_macos_app.sh").read_text(encoding="utf-8")

    assert "PASS: layout, $EXPECTED_ARCH, bundled ffmpeg, /state 200" in text
    assert 'find "$APP" -type f -exec file {} +' in text
    assert 'grep -v "$EXPECTED_ARCH"' in text
    assert "PASS: layout, arm64, bundled ffmpeg, /state 200" not in text


def test_docs_do_not_claim_arm64_is_a_cross_build() -> None:
    for name in ("README.md", "RELEASE_NOTES_0.9.0.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "arm64 is a cross-build" not in text
        assert "x86_64 is the runtime-verified artifact" not in text

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "free Intel runner" not in readme
    assert "Actions → build-macos" not in readme


def test_linux_appimage_matrix_includes_arm64() -> None:
    workflow = load_workflow("build.yml")
    linux = workflow["jobs"]["linux-appimage"]
    matrix = {entry["arch"]: entry["runner"] for entry in linux["strategy"]["matrix"]["include"]}
    assert matrix == {
        "x86_64": "ubuntu-latest",
        "aarch64": "ubuntu-24.04-arm",
    }
    assert "linux-appimage" in workflow["jobs"]["release"]["needs"]

    build = (ROOT / "scripts" / "build_appimage.sh").read_text(encoding="utf-8")
    assert 'ARCH="${ARCH:-x86_64}"' in build
    assert 'FFMPEG_ARCH="arm64"' in build
    assert "ffmpeg-linux-${FFMPEG_ARCH}" in build
    assert "6bb182d0d75d23028db82e9e4f723ca69b853d055698486e6984ddb2c06fb8ce" in build
    assert "dist/viosc-$VERSION-$ARCH.AppImage" in build

    for script in ("verify_appimage.sh", "smoke_appimage.sh"):
        text = (ROOT / "scripts" / script).read_text(encoding="utf-8")
        assert 'ARCH="${ARCH:-x86_64}"' in text
        assert "dist/viosc-$VERSION-$ARCH.AppImage" in text


def test_linux_arm64_release_notes_and_version() -> None:
    assert 'APP_VERSION: str = "0.9.1"' in (ROOT / "viosc.py").read_text(encoding="utf-8")
    notes = (ROOT / "RELEASE_NOTES_0.9.1.md").read_text(encoding="utf-8")
    assert "arm64" in notes


def test_linux_appimage_runtime_url_matches_asset() -> None:
    build = (ROOT / "scripts" / "build_appimage.sh").read_text(encoding="utf-8")

    assert 'RUNTIME_ASSET="runtime-${TOOL_ARCH}"' in build
    assert (
        'RUNTIME_URL="https://github.com/AppImage/type2-runtime/releases/download/'
        'continuous/${RUNTIME_ASSET}"' in build
    )
