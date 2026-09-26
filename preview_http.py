"""HTTP data-plane surface for viosc (e38s01, e41s03) — the A-side transport.

Serves the media files that vimix media sources point to over HTTP with a
correct RFC 7233 Range implementation (206 + Content-Range + Accept-Ranges,
416 on bad ranges, 200 full without Range), plus an ffprobe-backed /meta
endpoint, plus (e41s03) one cached thumbnail frame per request. Sources are
addressed by NAME through the daemon's live state table (address-by-name, like
the OSC surface); a name that is not a media source (no uri) returns 404 and no
path is ever opened from the request itself (no traversal surface).

This module is deliberately free of ``import viosc``: the daemon runs as
``__main__`` and a by-name import would create a SECOND module copy with an
empty state table (the classic __main__ double-import trap). Instead the
caller injects its own live ``vimix_data`` table, probe function and thumbnail
resolver when the server starts, so resolution always sees the daemon's real
state.
"""

import http.server
import json
import os
import socketserver
from urllib.parse import parse_qs, unquote, urlparse

CHUNK = 65536  # bytes per write while streaming a file body
MAX_AUTH_BODY_BYTES = 4096  # POST /auth body cap (a 4-digit code is tiny)
MAX_SESSION_BODY_BYTES = 262144  # POST /fs/session: a request carries up to 256 paths


def clean_media_path(uri_value):
    """Normalize a media uri (file:// or plain path) to an absolute path."""
    if not uri_value:
        return ""
    uri_str = str(uri_value).strip()
    if uri_str.startswith("file://"):
        return os.path.abspath(unquote(urlparse(uri_str).path))
    return os.path.abspath(unquote(uri_str))


def resolve_media_path(name, vimix_data):
    """Absolute media path for a source name in ``vimix_data``, or None."""
    for data in vimix_data.values():
        if data.get("name") == str(name):
            uri = data.get("uri")
            if not uri:
                return None
            path = clean_media_path(uri)
            return path if os.path.isfile(path) else None
    return None


def make_preview_handler(
    vimix_data,
    probe_meta,
    resolve_thumb=None,
    provide_state=None,
    gate=None,
    fs=None,
    provide_config=None,
    apply_config=None,
    restart=None,
):
    """Build a handler class bound to the daemon's live state table and probe.

    ``probe_meta(path)`` must return the meta dict (or None); ``vimix_data``
    is the daemon's own mutable state object, so source churn is always seen.
    ``resolve_thumb(name, index)`` returns one thumbnail frame's JPEG bytes or
    None (e41s03) and ``provide_state()`` returns the state table as the exact
    JSON text the OSC broadcast sends (e41s04): the daemon injects both for the
    same reason it injects the probe — this module never imports viosc, so the
    __main__ double-import trap cannot create a second, empty state table.

    ``gate`` (e42s01) is an optional pairing authenticator: when present and
    enabled, every request except ``POST /auth`` needs an
    ``Authorization: Bearer <token>`` header. It is injected exactly like the
    other collaborators so this module stays free of the daemon.

    ``provide_config`` (e58s01) returns the effective config payload
    (``{"values", "sources", "editable"}``) for the ``GET /config`` resource;
    when absent the route answers 404.

    ``apply_config`` (e58s01) accepts a ``POST /config`` change dict and returns
    the ``{"applied", "staged", "invalid", "unknown"}`` report; when absent the
    route answers 404.

    ``restart`` (e58s01) is called by ``POST /restart`` after the 200 is flushed
    (the daemon re-execs the process); when absent the route answers 404.

    ``fs`` (e43s01) is the optional read-only filesystem surface: an object with
    ``roots() -> list``, ``listing(path, offset) -> (payload, error)`` and
    ``resolve_file(path) -> str | None``. When it is absent (an older daemon) the
    ``/fs`` routes answer 404.
    """

    class BoundPreviewHandler(http.server.BaseHTTPRequestHandler):
        """GET /thumb/<name>/<index> and /preview/<name>/{file|meta}."""

        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # no request log spam
            pass

        # -- routing -------------------------------------------------------

        def do_POST(self):
            """POST /auth (pairing), POST /fs/session and POST /config (e58s01)."""
            route = urlparse(self.path).path
            if route == "/fs/session":
                if not self._authorized():
                    self._send_unauthorized()
                    return
                self._serve_fs_session_write()
                return
            if route == "/config":
                if not self._authorized():
                    self._send_unauthorized()
                    return
                self._serve_config_write()
                return
            if route == "/restart":
                if not self._authorized():
                    self._send_unauthorized()
                    return
                self._serve_restart()
                return
            if route != "/auth" or gate is None or not gate.enabled:
                self.send_error(404)
                return
            body = self._read_body()
            try:
                code = json.loads(body).get("code")
            except (ValueError, AttributeError):
                self._send_json(400, {"error": "invalid JSON body"})
                return
            token = gate.authenticate(code, self.client_address[0])
            if token is None:
                self._send_unauthorized()
                return
            self._send_json(200, {"token": token})

        def do_GET(self):
            if not self._authorized():
                self._send_unauthorized()
                return
            if self.path.startswith("/thumb/"):
                self._serve_thumb()
                return
            if self.path.startswith("/state"):
                self._serve_state()
                return
            if self.path.startswith("/fs/"):
                self._serve_fs()
                return
            if self.path.startswith("/config"):
                self._serve_config()
                return
            if not self.path.startswith("/preview/"):
                self.send_error(404)
                return
            rest = self.path[len("/preview/") :]
            if "/" not in rest:
                self.send_error(404)
                return
            name, action = rest.split("/", 1)
            name = unquote(name)
            if action not in ("file", "meta"):
                self.send_error(404)
                return
            path = resolve_media_path(name, vimix_data)
            if path is None:
                self.send_error(404)
                return
            if action == "meta":
                self._serve_meta(path)
            else:
                self._serve_file(path)

        # -- endpoints -----------------------------------------------------

        def _read_body(self, max_bytes: int = MAX_AUTH_BODY_BYTES) -> bytes:
            """The request body, capped (a code is tiny, a file list is not)."""
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return b""
            if length <= 0 or length > max_bytes:
                return b""
            return self.rfile.read(length)

        def _authorized(self) -> bool:
            """True when pairing is off or the request carries a live token."""
            if gate is None or not gate.enabled:
                return True
            header = self.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                return False
            return bool(gate.verify(header[len("Bearer ") :].strip()))

        def _send_unauthorized(self) -> None:
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("WWW-Authenticate", "Bearer")
            body = b'{"error": "pairing required"}'
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def _send_jpeg(self, blob: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)
            self.close_connection = True

        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def _serve_state(self):
            """The state table as JSON, KEEPING the connection alive (e41s04).

            The body comes from the daemon's provider, which is the same
            serializer that feeds the OSC broadcast, so the two transports
            cannot drift. Keep-alive is deliberate: a regular poll (typically
            1 Hz) must not open a TCP connection every time — unlike the file
            and thumbnail endpoints, whose responses are large and final.
            """
            if provide_state is None:
                self.send_error(404)
                return
            body = provide_state().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_config(self):
            """The effective config + source markers + editable set (e58s01)."""
            if provide_config is None:
                self.send_error(404)
                return
            self._send_json(200, provide_config())

        def _serve_config_write(self):
            """POST /config: validate + apply, answering a per-field report (e58s01)."""
            if apply_config is None:
                self.send_error(404)
                return
            body = self._read_body(MAX_SESSION_BODY_BYTES)
            try:
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise ValueError("body must be an object")
            except (ValueError, TypeError):
                self._send_json(400, {"error": "invalid JSON body"})
                return
            report = apply_config(payload)
            has_problems = bool(
                report.get("invalid") or report.get("unknown") or report.get("local_only")
            )
            self._send_json(400 if has_problems else 200, report)

        def _serve_restart(self):
            """POST /restart: answer 200, flush, then re-exec (e58s01)."""
            if restart is None:
                self.send_error(404)
                return
            self._send_json(200, {"restarting": True})
            self.wfile.flush()
            restart()

        def _serve_thumb(self):
            """One cached thumbnail frame as JPEG (e41s03).

            Addressed by NAME through the injected resolver: no path is ever
            built from the request, so an unknown name or a bad index is simply
            a 404 and there is no traversal surface.
            """
            rest = self.path[len("/thumb/") :]
            name, sep, raw_index = rest.partition("/")
            if not sep or not raw_index.isdigit():
                self.send_error(404)
                return
            blob = resolve_thumb(unquote(name), int(raw_index)) if resolve_thumb else None
            if blob is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)
            self.close_connection = True

        def _serve_fs(self):
            """The read-only filesystem surface (e43s01) and the sessions list.

            Every path is resolved by the injected adapter (realpath containment
            against the Media Roots), and an absent adapter means an older daemon
            with no surface at all — 404, never a fallback to an arbitrary path.
            """
            if fs is None:
                self.send_error(404)
                return
            parsed = urlparse(self.path)
            route = parsed.path
            if route == "/fs/roots":
                self._send_json(200, fs.roots())
                return
            if route == "/fs/sessions":
                self._send_json(200, fs.sessions())
                return
            params = parse_qs(parsed.query)
            path = (params.get("path") or [""])[0]
            if route == "/fs/list":
                try:
                    offset = int((params.get("offset") or ["0"])[0])
                except ValueError:
                    offset = 0
                payload, error = fs.listing(path, offset)
                if payload is None:
                    self._send_fs_error(error)
                    return
                self._send_json(200, payload)
                return
            if route == "/fs/thumb":
                blob = fs.thumb(path)
                if blob is None:
                    self.send_error(404)
                    return
                self._send_jpeg(blob)
                return
            if route in ("/fs/raw", "/fs/meta"):
                target = fs.resolve_file(path)
                if target is None:
                    self.send_error(404)
                    return
                if route == "/fs/meta":
                    self._serve_meta(target)
                else:
                    self._serve_file(target)
                return
            self.send_error(404)

        def _serve_fs_session_write(self):
            """POST /fs/session: write a `.mix` draft from {name, files} (e43s06).

            Body: JSON ``{"name": "Tonight", "files": ["/abs/clip.mp4", ...]}``.
            The adapter owns the target directory, the containment, the atomic
            write and the existence re-check; this handler only shapes the reply.
            """
            if fs is None or not hasattr(fs, "write_session"):
                self.send_error(404)
                return
            body = self._read_body(MAX_SESSION_BODY_BYTES)
            try:
                payload = json.loads(body)
                name = str(payload.get("name") or "")
                files = payload.get("files")
                if not isinstance(files, list):
                    raise ValueError("files must be a list")
                overwrite = bool(payload.get("overwrite"))
                alphas = payload.get("alphas")
                if alphas is not None and not isinstance(alphas, dict):
                    raise ValueError("alphas must be an object")
            except (ValueError, AttributeError, TypeError):
                self._send_json(400, {"error": "invalid JSON body"})
                return
            result, error = fs.write_session(
                name,
                [str(item) for item in files],
                overwrite=overwrite,
                alphas=alphas,
            )
            if result is None:
                status = {
                    "bad_name": 400,
                    "no_files": 400,
                    "missing_file": 409,
                    "unwritable": 500,
                }.get(error, 400)
                self._send_json(status, {"error": error or "error"})
                return
            self._send_json(200, result)

        def _send_fs_error(self, error):
            status = {
                "forbidden": 403,
                "not_found": 404,
                "not_dir": 400,
                "unreadable": 403,
            }.get(error, 400)
            self._send_json(status, {"error": error or "error"})

        def _serve_meta(self, path):
            meta = probe_meta(path)
            if meta is None:
                self.send_error(404)
                return
            body = json.dumps(meta).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def _serve_file(self, path):
            size = os.path.getsize(path)
            rng = self.headers.get("Range")
            if rng:
                self._serve_range(path, size, rng.strip())
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(CHUNK)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            self.close_connection = True

        # -- helpers -------------------------------------------------------

        def _serve_range(self, path, size, spec):
            if not spec.startswith("bytes="):
                self._send_416(size)
                return
            start_s, sep, end_s = spec[len("bytes=") :].partition("-")
            if not sep:
                self._send_416(size)
                return
            try:
                start = int(start_s) if start_s else 0
                end = int(end_s) if end_s else size - 1
            except ValueError:
                self._send_416(size)
                return
            if start >= size or end < start:
                self._send_416(size)
                return
            end = min(end, size - 1)
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(length))
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining:
                    chunk = f.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            self.close_connection = True

        def _send_416(self, size):
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            self.close_connection = True

    return BoundPreviewHandler


def start_preview_server(
    ip,
    port,
    vimix_data,
    probe_meta,
    resolve_thumb=None,
    provide_state=None,
    gate=None,
    fs=None,
    provide_config=None,
    apply_config=None,
    restart=None,
):
    """Bind (and return) the preview HTTP server against the daemon's state."""
    handler = make_preview_handler(
        vimix_data,
        probe_meta,
        resolve_thumb,
        provide_state,
        gate,
        fs,
        provide_config,
        apply_config,
        restart,
    )

    class PreviewServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    return PreviewServer((ip, port), handler)
