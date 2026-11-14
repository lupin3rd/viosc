"""Read-only filesystem surface for the viOSC data plane (e43s01).

The file manager in VJmix browses machine A through HTTP; this module is the
pure, daemon-free half: Media Roots expansion and realpath containment, media
classification by **vimix's own extension list**, directory listing (hidden
filter, paging, dirs-first order, readable flags) and the file resolver behind
`/fs/raw` and `/fs/meta`.

Security posture (specs/FILE_MANAGER_LATEST.md §7): every request path is
resolved with ``realpath`` and must stay inside a configured root — a `..`
escape and a symlink pointing outside are both refused — and nothing here writes.
The HTTP handler and the Media Roots config live in the daemon / http_api;
this module never imports the daemon.
"""

import os

# vimix's own MEDIA_FILES_PATTERN (src/defines.h) plus the session extension, so
# the browser shows exactly what vimix can load.
VIDEO_EXTENSIONS = frozenset(
    {".mp4", ".mpg", ".mpeg", ".m2v", ".m4v", ".avi", ".mov", ".mkv", ".webm", ".mod", ".wmv"}
    | {".mxf", ".ogg", ".flv", ".hevc", ".asf"}
)
IMAGE_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".webp", ".bmp", ".ppm", ".svg"}
)
SESSION_EXTENSIONS = frozenset({".mix"})

KIND_DIR = "dir"
KIND_FILE = "file"
DEFAULT_THUMB_CACHE_ENTRIES = 256
MEDIA_VIDEO = "video"
MEDIA_IMAGE = "image"
MEDIA_SESSION = "session"
MEDIA_OTHER = "other"

# Listings are paged so a huge directory cannot produce an unbounded response.
DEFAULT_PAGE_SIZE = 500
# e100s01: a folder scan is bounded too, and it says when it stopped early, so a
# deep media tree can never turn one request into an unbounded walk.
MAX_SCAN_FILES = 2000


def classify(name: str) -> str:
    """Media kind of a file name: video | image | session | other."""
    ext = os.path.splitext(str(name))[1].lower()
    if ext in VIDEO_EXTENSIONS:
        return MEDIA_VIDEO
    if ext in IMAGE_EXTENSIONS:
        return MEDIA_IMAGE
    if ext in SESSION_EXTENSIONS:
        return MEDIA_SESSION
    return MEDIA_OTHER


def expand_root(root: str) -> str:
    """Absolute path of a configured root (`~` expanded, no realpath yet)."""
    return os.path.abspath(os.path.expanduser(str(root)))


def resolve_within_roots(path: str, roots: list[str]) -> str | None:
    """The real path of ``path`` when it lives inside a root, else None.

    Containment is checked on the ``realpath`` of both sides, so `..` and a
    symlink that points outside the roots are both refused. The root itself is
    allowed (it is a legitimate listing target).
    """
    if not path:
        return None
    candidate = os.path.realpath(os.path.abspath(os.path.expanduser(str(path))))
    for root in roots or []:
        real_root = os.path.realpath(expand_root(root))
        if candidate == real_root or candidate.startswith(real_root + os.sep):
            return candidate
    return None


def resolve_file(path: str, roots: list[str]) -> str | None:
    """The real path of a FILE inside a root, else None (dirs and missing)."""
    real = resolve_within_roots(path, roots)
    if real is None or not os.path.isfile(real):
        return None
    return real


def roots_info(roots: list[str]) -> list[dict]:
    """The configured Media Roots with a readable flag, for the UI's start list."""
    info = []
    for root in roots or []:
        path = expand_root(root)
        info.append(
            {
                "path": path,
                "name": os.path.basename(path) or path,
                "readable": os.path.isdir(path) and os.access(path, os.R_OK),
            }
        )
    return info


def _entry(directory: str, name: str) -> dict:
    """One listing entry; an unreadable/stat-failing entry is still listed."""
    full = os.path.join(directory, name)
    entry = {
        "name": name,
        "kind": KIND_FILE,
        "size": 0,
        "mtime": 0.0,
        "media_kind": MEDIA_OTHER,
        "readable": False,
    }
    try:
        stat = os.stat(full)
    except OSError:
        return entry
    is_dir = os.path.isdir(full)
    entry["kind"] = KIND_DIR if is_dir else KIND_FILE
    entry["size"] = 0 if is_dir else stat.st_size
    entry["mtime"] = stat.st_mtime
    entry["media_kind"] = MEDIA_OTHER if is_dir else classify(name)
    entry["readable"] = os.access(full, os.R_OK)
    return entry


def resolve_directory(path: str, roots: list[str]) -> tuple[str | None, str | None]:
    """The real path of a directory inside a root, or ``(None, tag)``.

    One place for the containment + existence + kind triple so the listing, the
    scan (e100s01) and the session writer's target directory cannot drift.
    """
    real = resolve_within_roots(path, roots)
    if real is None:
        return None, "forbidden"
    if not os.path.exists(real):
        return None, "not_found"
    if not os.path.isdir(real):
        return None, "not_dir"
    return real, None


def list_directory(
    path: str,
    roots: list[str],
    *,
    show_hidden: bool = False,
    page_size: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
) -> tuple[dict | None, str | None]:
    """One directory page: ``(payload, error)`` with error one of the tags below.

    Errors: ``forbidden`` (outside the roots), ``not_found``, ``not_dir``,
    ``unreadable``. Entries are directories-first then case-insensitive by name,
    so paging is stable across requests.
    """
    directory, error = resolve_directory(path, roots)
    if directory is None:
        return None, error
    try:
        names = os.listdir(directory)
    except OSError:
        return None, "unreadable"
    entries = [_entry(directory, name) for name in names if show_hidden or not name.startswith(".")]
    entries.sort(key=lambda e: (e["kind"] != KIND_DIR, e["name"].lower()))
    total = len(entries)
    try:
        start = max(0, int(offset))
        size = max(1, int(page_size))
    except (TypeError, ValueError):
        start, size = 0, DEFAULT_PAGE_SIZE
    return (
        {
            "path": directory,
            "entries": entries[start : start + size],
            "total": total,
            "offset": start,
            "page_size": size,
        },
        None,
    )


def _scan_entry(full: str, name: str) -> dict:
    """One scanned video: path, name, size and mtime (a stat failure keeps 0s)."""
    entry: dict = {"path": full, "name": name, "size": 0, "mtime": 0.0}
    try:
        stat = os.stat(full)
    except OSError:
        return entry
    entry["size"] = stat.st_size
    entry["mtime"] = stat.st_mtime
    return entry


def _collect_videos(
    directory: str,
    roots: list[str],
    *,
    recursive: bool,
    show_hidden: bool,
    limit: int,
) -> tuple[list[dict], int, bool]:
    """Bounded breadth-first walk of the VIDEO files.

    Returns ``(files, ignored, truncated)`` — the non-video count is what the
    wizard reports as "other entries ignored".

    Every directory entered is re-checked against the roots, so a symlink that
    points outside is skipped instead of walked. The cap is checked RIGHT BEFORE
    appending a video, so a folder with exactly ``limit`` clips is not called
    truncated.
    """
    found: list[dict] = []
    ignored = 0
    pending = [directory]
    truncated = False
    while pending and not truncated:
        current = pending.pop(0)
        try:
            names = sorted(os.listdir(current))
        except OSError:
            continue
        for name in names:
            if not show_hidden and name.startswith("."):
                continue
            full = os.path.join(current, name)
            if os.path.isdir(full):
                if recursive:
                    inside = resolve_within_roots(full, roots)
                    if inside is not None:
                        pending.append(inside)
                continue
            if classify(name) != MEDIA_VIDEO:
                ignored += 1
                continue
            if len(found) >= limit:
                truncated = True
                break
            found.append(_scan_entry(full, name))
    return found, ignored, truncated


def scan_videos(
    path: str,
    roots: list[str],
    *,
    recursive: bool = False,
    show_hidden: bool = False,
    max_files: int = MAX_SCAN_FILES,
) -> tuple[dict | None, str | None]:
    """The video files of a folder tree: ``(payload, error)`` (e100s01).

    Errors are the listing's own tags (``forbidden``, ``not_found``, ``not_dir``,
    ``unreadable``), so the wizard words them exactly like the browser does.
    ``truncated`` says the walk stopped at the cap, and ``total`` is then a lower
    bound, never a claim about the folder. ``ignored`` counts the non-video files
    the walk skipped, so the wizard can say what it left out.
    """
    directory, error = resolve_directory(path, roots)
    if directory is None:
        return None, error
    found, ignored, truncated = _collect_videos(
        directory, roots, recursive=recursive, show_hidden=show_hidden, limit=max(1, max_files)
    )
    found.sort(key=lambda entry: entry["name"].lower())
    return (
        {
            "path": directory,
            "files": found,
            "total": len(found),
            "ignored": ignored,
            "truncated": truncated,
            "recursive": bool(recursive),
        },
        None,
    )


class ThumbnailCache:
    """One JPEG thumbnail per file, invalidated by ``(size, mtime)`` (e43s03).

    The generator is injected (the daemon passes the ffmpeg extraction under the
    existing concurrency semaphore), so this stays a pure, testable cache: a hit
    is reused, an edited file re-generates, a failing generator caches nothing,
    and the bound keeps a big folder from growing memory without limit.
    """

    def __init__(self, generator, max_entries: int = DEFAULT_THUMB_CACHE_ENTRIES) -> None:
        self._generator = generator
        self._max = max(1, int(max_entries))
        self._cache: dict[str, tuple[tuple[int, float], bytes]] = {}

    def __len__(self) -> int:
        return len(self._cache)

    def clear(self) -> None:
        """Drop every cached frame (a manual regenerate / cache reset)."""
        self._cache.clear()

    def get(self, path: str) -> bytes | None:
        """The cached JPEG for ``path``, generating it on a miss/change."""
        try:
            stat = os.stat(path)
        except OSError:
            return None
        key = (stat.st_size, stat.st_mtime)
        hit = self._cache.get(path)
        if hit is not None and hit[0] == key:
            return hit[1]
        blob = self._generator(path)
        if not blob:
            return None
        self._cache[path] = (key, blob)
        self._evict()
        return blob

    def _evict(self) -> None:
        while len(self._cache) > self._max:
            self._cache.pop(next(iter(self._cache)))
