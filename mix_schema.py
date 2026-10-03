"""vimix `.mix` schema: build and parse (e43s06).

The rig spike (specs/archive/spikes/SPIKE-file-manager.md, SPIKE-1 PASSED)
established the contract this module implements:

- a `.mix` is a **multi-root fragment** (`<vimix/>`, `<Session>`, ...), so it is
  built as text and parsed with a synthetic wrapper;
- a cloned `<Source>` element with **no `<MediaPlayer>`** loads (the player
  defaults), and every source sits centred, like vimix's own default;
- generated files open through `/vimix/session/open` with unique readable names.

Pure, daemon-free, stdlib only. The embedded template IS the rig-validated source
element, so the writer never depends on a vimix written file at runtime.
"""

import math
import os
import random
import re
import xml.etree.ElementTree as ET

XML_DECL = '<?xml version="1.0" encoding="UTF-8"?>\n'
WRAPPER = "mix_root"
# The SESSION XML version of the target vimix (src/defines.h: XML_VERSION_MAJOR
# 0, XML_VERSION_MINOR 5), NOT the config file's version (`~/.config/vimix/
# vimix.xml` carries minor="9" — a different line, and taking it from there made
# every generated file log "in a newer version of vimix session"). vimix warns
# only when the FILE is newer than the app, so declaring an older-or-equal
# version is always safe.
VIMIX_MAJOR = 0
VIMIX_MINOR = 5
RESOLUTION = "1920x1080px"
ACTIVATION_THRESHOLD = "1.3"
# e45s01: the per-source output alpha is encoded in the Mixing-circle radius
# (vimix recomputes `alphaFromCordinates` every frame and overwrites the saved
# Blending `w`). A negative alpha parks the source beyond the activation
# threshold, so the media player is DISABLED (stopped, not merely transparent).
DEFAULT_ALPHA = 1.0
PARKED_ALPHA = -0.3
PARKED_DISTANCE = 1.4
ALPHA_SPREAD = 2.39996  # golden angle, rad: spreads same-alpha sources
MAX_SOURCES = 256
MAX_NAME_LENGTH = 40
DEFAULT_NAME_PREFIX = "clip"
_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")

# One <MediaSource>, exactly the shape the rig validated (no <MediaPlayer>).
SOURCE_TEMPLATE = """<Source id="0" name="clip" locked="false" play="true"
        replay_on_deactivate="false" type="MediaSource">
    <Mixing>
        <Node visible="true" id="0" type="Group">
            <scale>
                <vec3 x="0.12" y="0.12" z="1" />
            </scale>
            <translation>
                <vec3 x="0" y="0" z="2.25" />
            </translation>
            <rotation>
                <vec3 x="0" y="0" z="0" />
            </rotation>
            <crop>
                <vec4 x="-1" y="1" z="1" w="-1" />
            </crop>
            <data>
                <mat4>
                    <vec4 x="0" y="0" z="0" w="0" row="0" />
                    <vec4 x="0" y="0" z="0" w="0" row="1" />
                    <vec4 x="0" y="0" z="0" w="0" row="2" />
                    <vec4 x="0" y="0" z="0" w="0" row="3" />
                </mat4>
            </data>
        </Node>
    </Mixing>
    <Geometry>
        <Node visible="true" id="0" type="Group">
            <scale>
                <vec3 x="1" y="1" z="1" />
            </scale>
            <translation>
                <vec3 x="0" y="0" z="2.25" />
            </translation>
            <rotation>
                <vec3 x="0" y="0" z="0" />
            </rotation>
            <crop>
                <vec4 x="-1" y="1" z="1" w="-1" />
            </crop>
            <data>
                <mat4>
                    <vec4 x="0" y="0" z="0" w="0" row="0" />
                    <vec4 x="0" y="0" z="0" w="0" row="1" />
                    <vec4 x="0" y="0" z="0" w="0" row="2" />
                    <vec4 x="0" y="0" z="0" w="0" row="3" />
                </mat4>
            </data>
        </Node>
    </Geometry>
    <Layer>
        <Node visible="true" id="0" type="Group">
            <scale>
                <vec3 x="1" y="1" z="1" />
            </scale>
            <translation>
                <vec3 x="0" y="0" z="2.25" />
            </translation>
            <rotation>
                <vec3 x="0" y="0" z="0" />
            </rotation>
            <crop>
                <vec4 x="-1" y="1" z="1" w="-1" />
            </crop>
            <data>
                <mat4>
                    <vec4 x="0" y="0" z="0" w="0" row="0" />
                    <vec4 x="0" y="0" z="0" w="0" row="1" />
                    <vec4 x="0" y="0" z="0" w="0" row="2" />
                    <vec4 x="0" y="0" z="0" w="0" row="3" />
                </mat4>
            </data>
        </Node>
    </Layer>
    <Texture mirrored="true">
        <Node visible="true" id="0" type="Group">
            <scale>
                <vec3 x="1" y="1" z="1" />
            </scale>
            <translation>
                <vec3 x="0" y="0" z="0" />
            </translation>
            <rotation>
                <vec3 x="0" y="0" z="0" />
            </rotation>
            <crop>
                <vec4 x="-1" y="1" z="1" w="-1" />
            </crop>
            <data>
                <mat4>
                    <vec4 x="0" y="0" z="0" w="0" row="0" />
                    <vec4 x="0" y="0" z="0" w="0" row="1" />
                    <vec4 x="0" y="0" z="0" w="0" row="2" />
                    <vec4 x="0" y="0" z="0" w="0" row="3" />
                </mat4>
            </data>
        </Node>
    </Texture>
    <Blending type="ImageShader" id="0">
        <color>
            <vec4 x="1" y="1" z="1" w="0" />
        </color>
        <blending mode="0" />
        <uniforms stipple="0" />
    </Blending>
    <Mask type="MaskShader" id="0" mode="0" shape="0">
        <color>
            <vec4 x="1" y="1" z="1" w="1" />
        </color>
        <blending mode="0" />
        <uniforms blur="0.5" option="0">
            <size>
                <vec2 x="1" y="1" />
            </size>
        </uniforms>
    </Mask>
    <ImageProcessing enabled="false" follow="0" type="ImageProcessingShader" id="0">
        <uniforms brightness="0" contrast="0" saturation="0" hueshift="0"
                  threshold="0" nbColors="0" invert="0" />
        <gamma>
            <vec4 x="1" y="1" z="1" w="1" />
        </gamma>
        <levels>
            <vec4 x="0" y="1" z="0" w="1" />
        </levels>
    </ImageProcessing>
    <Audio enabled="false" volume="0" volume_mix="24" />
    <uri></uri>
</Source>"""


def sanitize_name(value: str) -> str:
    """A vimix-safe source name (OSC targets it by name): alnum, `_`, `-` only."""
    cleaned = _NAME_UNSAFE.sub("_", str(value or "")).strip("_")
    return cleaned[:MAX_NAME_LENGTH] or DEFAULT_NAME_PREFIX


def source_name_for(path: str, index: int, taken: set[str]) -> str:
    """A readable, unique name for one file (the stem, deduplicated)."""
    stem = os.path.splitext(os.path.basename(str(path)))[0]
    base = sanitize_name(stem) if stem.strip() else f"{DEFAULT_NAME_PREFIX}{index:02d}"
    name = base
    suffix = 2
    while name in taken:
        name = f"{base}_{suffix}"
        suffix += 1
    taken.add(name)
    return name


def parse_fragment(text: str) -> ET.Element:
    """Parse a `.mix` despite its several top-level elements (not one-root XML)."""
    stripped = str(text or "").lstrip("\ufeff").lstrip()
    if stripped.startswith("<?xml"):
        stripped = stripped[stripped.index("?>") + 2 :]
    return ET.fromstring(f"<{WRAPPER}>{stripped}</{WRAPPER}>")


def _fresh_ids(node: ET.Element, rng: random.Random) -> None:
    """Give every element with an id (Source and its view Nodes) a fresh one."""
    for element in node.iter():
        if "id" in element.attrib:
            element.set("id", str(rng.getrandbits(62)))


def alpha_distance(alpha: float) -> float:
    """Mixing-circle radius whose runtime alpha equals ``alpha`` (e45s01).

    vimix computes ``alpha = 0.5 + 0.5*cos(pi * D^1.5)``
    (``SourceCore::alphaFromCordinates``), so the inverse is
    ``D = (acos(2a - 1) / pi)^(2/3)``. A negative alpha is PARKED: placed beyond
    the session's ``activationThreshold`` so the media player is disabled.
    """
    if alpha < 0:
        return PARKED_DISTANCE
    if alpha >= 1:
        return 0.0
    return (math.acos(2.0 * alpha - 1.0) / math.pi) ** (2.0 / 3.0)


def _place_source(node: ET.Element, index: int, alpha: float) -> None:
    """Encode one source's alpha in the Mixing node and the Blending colour.

    ``Source::update`` overwrites the saved ``<Blending><color w>`` with the
    runtime alpha derived from the Mixing position, so the POSITION is the real
    control; the colour is written consistently (``max(0, alpha)``) as native
    vimix files carry it. The Geometry translation stays centred (e44 / the
    e43 visibility fix).
    """
    color = node.find("Blending/color/vec4")
    if color is not None:
        color.set("x", "1")
        color.set("y", "1")
        color.set("z", "1")
        color.set("w", str(round(min(1.0, max(0.0, alpha)), 6)))
    position = node.find("Mixing/Node/translation/vec3")
    if position is None:
        return
    distance = alpha_distance(alpha)
    angle = index * ALPHA_SPREAD
    position.set("x", str(round(distance * math.cos(angle), 6)))
    position.set("y", str(round(distance * math.sin(angle), 6)))


def build_session(
    files: list[str],
    *,
    alphas: dict[str, float] | None = None,
    session_id: int | None = None,
    seed: int = 1234,
) -> str:
    """A `.mix` fragment with one MediaSource per file (rig-validated shape).

    Files beyond MAX_SOURCES are ignored; names come from the file stems, are made
    unique and vimix-safe; ids are fresh for the Source and every node inside it;
    no `<MediaPlayer>` is emitted (the loader defaults the player). ``alphas``
    (e45s01) maps a path to its output alpha; absent keeps the legacy full
    opacity, and a path missing from a GIVEN map is parked.
    """
    alpha_map = dict(alphas) if isinstance(alphas, dict) else None
    rng = random.Random(seed)
    taken: set[str] = set()
    sources: list[str] = []
    for index, path in enumerate(list(files)[:MAX_SOURCES]):
        node = ET.fromstring(SOURCE_TEMPLATE)
        _fresh_ids(node, rng)
        node.set("name", source_name_for(path, index, taken))
        uri = node.find("uri")
        if uri is not None:
            uri.text = str(path)
        if alpha_map is None:
            alpha = DEFAULT_ALPHA
        else:
            try:
                alpha = float(alpha_map.get(path, PARKED_ALPHA))
            except (TypeError, ValueError):
                alpha = PARKED_ALPHA
        _place_source(node, index, alpha)
        sources.append(ET.tostring(node, encoding="unicode"))
    sid = rng.getrandbits(62) if session_id is None else int(session_id)
    header = (
        f'<vimix major="{VIMIX_MAJOR}" minor="{VIMIX_MINOR}" '
        f'size="{len(sources)}" total="{len(sources)}" resolution="{RESOLUTION}" />'
    )
    body = "\n".join(sources)
    return (
        f"{XML_DECL}{header}\n"
        f'<Session id="{sid}" activationThreshold="{ACTIVATION_THRESHOLD}">\n'
        f"{body}\n"
        "</Session>\n"
    )


def parse_sources(text: str) -> list[dict[str, str]]:
    """``[{name, uri}]`` of a `.mix` fragment; ``[]`` when it cannot be parsed."""
    try:
        wrapper = parse_fragment(text)
    except ET.ParseError:
        return []
    session = wrapper.find("Session")
    if session is None:
        return []
    sources = []
    for node in session.findall("Source"):
        name = str(node.get("name") or "")
        uri = str(node.findtext("uri") or "")
        sources.append({"name": name, "uri": uri})
    return sources


# --- the session store: POST /fs/session + GET /fs/sessions (e43s06) ---------

SESSION_EXT = ".mix"
MAX_SESSION_NAME = 64
_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9 _-]+")


def safe_session_filename(name: str) -> str:
    """A safe ``<name>.mix`` filename, or ``""`` when nothing usable remains.

    Separators and anything exotic collapse to ``_``, so a request can never
    escape the sessions directory, and the extension is always ours.
    """
    base = _UNSAFE_FILENAME.sub("_", str(name or "")).strip(" _")
    if not base:
        return ""
    return f"{base[:MAX_SESSION_NAME]}{SESSION_EXT}"


def _unique_path(directory: str, filename: str) -> str:
    """``filename`` or a deterministic ``_2``, ``_3`` … suffix on collision."""
    path = os.path.join(directory, filename)
    if not os.path.exists(path):
        return path
    stem = filename[: -len(SESSION_EXT)]
    suffix = 2
    while True:
        candidate = os.path.join(directory, f"{stem}_{suffix}{SESSION_EXT}")
        if not os.path.exists(candidate):
            return candidate
        suffix += 1


def write_session(
    directory: str,
    name: str,
    files: list[str],
    *,
    overwrite: bool = False,
    alphas: dict[str, float] | None = None,
) -> tuple[dict | None, str | None]:
    """Write a `.mix` from a file list; ``(result, None)`` or ``(None, error)``.

    ``overwrite`` (e44s02) writes the exact ``safe_session_filename(name)`` path,
    replacing an existing file; the default keeps the deterministic ``_2``
    suffix, so older clients are unaffected.

    Errors: ``bad_name``, ``no_files``, ``missing_file`` (re-checked here: a file
    deleted between composing and committing must be reported, not silently
    written), ``unwritable``. The write is atomic (temp + rename).
    """
    filename = safe_session_filename(name)
    if not filename:
        return None, "bad_name"
    wanted = [str(path) for path in (files or []) if str(path).strip()][:MAX_SOURCES]
    if not wanted:
        return None, "no_files"
    if any(not os.path.isfile(path) for path in wanted):
        return None, "missing_file"
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        return None, "unwritable"
    path = os.path.join(directory, filename) if overwrite else _unique_path(directory, filename)
    text = build_session(wanted, alphas=alphas)
    try:
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except OSError:
        return None, "unwritable"
    return (
        {
            "file": path,
            "name": os.path.basename(path)[: -len(SESSION_EXT)],
            "sources": parse_sources(text),
        },
        None,
    )


def list_sessions(directory: str) -> list[dict]:
    """Every ``*.mix`` in the directory with its parsed sources (a broken file
    is still listed, with an empty source list — the UI can show it and the user
    can delete it)."""
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return []
    sessions = []
    for entry in entries:
        if not entry.endswith(SESSION_EXT):
            continue
        path = os.path.join(directory, entry)
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        sessions.append(
            {
                "file": path,
                "name": entry[: -len(SESSION_EXT)],
                "mtime": mtime,
                "sources": parse_sources(text),
            }
        )
    return sessions
