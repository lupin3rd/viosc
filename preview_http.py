"""HTTP preview surface for viosc (e38s01) — the A-side file transport.

Serves the media files that vimix media sources point to over HTTP with a
correct RFC 7233 Range implementation (206 + Content-Range + Accept-Ranges,
416 on bad ranges, 200 full without Range), plus an ffprobe-backed /meta
endpoint. Sources are addressed by NAME through the daemon's live state table
(address-by-name, like the OSC surface); a name that is not a media source
(no uri) returns 404 and no path is ever opened from the request itself (no
traversal surface).

This module is deliberately free of ``import viosc``: the daemon runs as
``__main__`` and a by-name import would create a SECOND module copy with an
empty state table (the classic __main__ double-import trap). Instead the
caller injects its own live ``vimix_data`` table and probe function when the
server starts, so resolution always sees the daemon's real state.
"""

import http.server
import json
import os
import socketserver
from urllib.parse import unquote, urlparse

CHUNK = 65536  # bytes per write while streaming a file body


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


def make_preview_handler(vimix_data, probe_meta):
    """Build a handler class bound to the daemon's live state table and probe.

    ``probe_meta(path)`` must return the meta dict (or None); ``vimix_data``
    is the daemon's own mutable state object, so source churn is always seen.
    """

    class BoundPreviewHandler(http.server.BaseHTTPRequestHandler):
        """GET /preview/<source-name>/{file|meta} with RFC 7233 Range support."""

        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # no request log spam
            pass

        # -- routing -------------------------------------------------------

        def do_GET(self):
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


def start_preview_server(ip, port, vimix_data, probe_meta):
    """Bind (and return) the preview HTTP server against the daemon's state."""
    handler = make_preview_handler(vimix_data, probe_meta)

    class PreviewServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    return PreviewServer((ip, port), handler)
