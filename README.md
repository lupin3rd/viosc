# viOSC

**viOSC is the Vimix half of [VJmix](https://github.com/lupin3rd/VJmix).** It runs
beside Vimix on the same machine, speaks Vimix's OSC API, and serves everything
VJmix needs over HTTP on one port. On its own it does nothing: VJmix is what
drives it.

**viOSC 1.0.0 requires VJmix 1.3.0 and Vimix 0.9.3+.** Linux: AppImage
(x86_64/aarch64) or source. macOS: x86_64/arm64 DMG.
[Release assets](https://github.com/lupin3rd/viosc/releases/tag/v1.0.0) ·
[Release notes](RELEASE_NOTES_1.0.0.md).

## What it does

- **Commands in.** VJmix sends `/vimix/*` commands over HTTP; viOSC forwards
  them to Vimix verbatim. One long-lived stream, in order, nothing lost.
- **State out.** viOSC keeps a mirror of the session (every source and its
  properties) and serves it as a snapshot or as an ordered delta stream.
- **What Vimix cannot.** Thumbnails, media preview and metadata, a read-only
  file manager and the configuration are computed or read here and exposed as
  plain HTTP resources.
- **Pairing.** A 4-digit code shown in viOSC's window must be exchanged for a
  bearer token before a controller may use any of it.

The result is one link: HTTP toward VJmix, OSC only toward Vimix.

## Install

### Linux (AppImage)

Download `viosc-1.0.0-x86_64.AppImage` (Intel/AMD) or
`viosc-1.0.0-aarch64.AppImage` (arm64) from the release, then:

```bash
chmod +x viosc-1.0.0-<arch>.AppImage
./viosc-1.0.0-<arch>.AppImage            # GUI
./viosc-1.0.0-<arch>.AppImage --headless # console daemon
```

### macOS

Download `viOSC-1.0.0-x86_64.dmg` or `viOSC-1.0.0-arm64.dmg`.

- x86_64 runs on Intel and on Apple Silicon via Rosetta 2.
- Both architectures are built and smoke-tested natively in CI on macOS 15.
- The app is **unsigned**: right-click → Open, or
  `xattr -dr com.apple.quarantine /Applications/viOSC.app`.
- Allow incoming connections at the firewall prompt (8686; 7001 is loopback).

Both bundles include Python, Tk, python-osc and static ffmpeg/ffprobe.

### From source

Requires Python 3.10+, Vimix 0.9.3+, `python-osc`, and ffmpeg/ffprobe for
thumbnails and preview:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python viosc.py              # GUI
.venv/bin/python viosc.py --headless   # console daemon
```

A missing ffmpeg is not fatal: thumbnails and preview are disabled and the
OSC/HTTP paths keep running.

## Ports

| Role | Address | Purpose |
|---|---|---|
| Vimix replies | `127.0.0.1:7001` | Vimix's OSC send port; the mirror is fed here |
| Vimix receive | `127.0.0.1:7000` | Vimix's OSC receive port; the forward target |
| HTTP | `0.0.0.0:8686` | **Everything toward VJmix** |

Two UDP sockets, both toward Vimix, and one TCP port. That is the whole
transport surface.

## The HTTP link

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/link` | **The command stream**: one long-lived chunked request, one JSON frame per chunk (`{"address": "/vimix/…", "args": [...]}`), with `{"ping": true}` keepalives |
| `GET` | `/state` | The whole mirror; `rev` + `ETag`/`304` |
| `GET` | `/events` | Server-Sent Events, one ordered delta per change |
| `POST` | `/interest` | `{source, props, cadence_ms, once?}`; `"*"` for the full mirror |
| `GET` | `/thumb/<name>/<index>` | One cached JPEG |
| `POST` | `/thumb/regen` | `{"source": "<name>"}` |
| `GET` | `/preview/<name>/{file,meta}` | Media preview and metadata |
| `GET` | `/fs/...` | Read-only file manager (`/fs/roots`, `/fs/list`, `/fs/meta`, `/fs/raw`) |
| `GET`/`POST` | `/config` | Effective config, and the fields a controller may change |
| `POST` | `/restart` | Apply staged bind changes |
| `POST` | `/auth` | Exchange the pairing code for a bearer token |

When pairing is enabled, every request except `POST /auth` needs
`Authorization: Bearer <token>`.

## OSC toward Vimix

`/vimix/*` is forwarded to `127.0.0.1:7000` unchanged. Target sources by
**name** (preferred: stable across reordering) or by plain index. Avoid
`#index` targets: Vimix echoes that form back and it cannot be resolved.

## One instance, one window

viOSC takes a lock beside its config (`viosc.lock`) at start: a second launch
refuses to run ("viOSC is already running, pid N"). Closing the window ends the
daemon and its servers, which releases the lock — stopping and restarting is
always one window, never a hidden copy fighting for port 8686.

## Pairing

viOSC shows a 4-digit code at start. With `pairing_enabled` (the default) a peer
must present it over HTTP `POST /auth` before it can drive Vimix or read the
link. The code expires after `pairing_code_ttl_hours` (24 h), survives a
restart, and rotates after abuse. Pairing is a shared-code gate over plaintext
traffic, **not encryption**: restrict the port at the network level if you need
more.

## Configuration

`~/.config/viosc/config.json` (XDG); precedence **JSON > environment >
default**. Editable from the viOSC window or from VJmix over HTTP.

| Field | Default | Notes |
|---|---|---|
| `from_vimix_port` | `7001` | Vimix replies |
| `tovimix_port` | `7000` | Vimix receive |
| `http_ip` / `http_port` | `0.0.0.0` / `8686` | The whole HTTP surface |
| `ffmpeg_path` / `ffprobe_path` | `ffmpeg` / `ffprobe` | Resolved from PATH, Homebrew/MacPorts and the bundle |
| `sync_interval_ms` | `2000` | Roster cadence; the state tier is 1 s |
| `pairing_enabled` | `true` | See above |

Every other field is documented in `config.py` and shown in the window.

## How the mirror stays fresh

- **Roster** — Vimix publishes the session as one bundle; viOSC reads it whole
  every 2 s. A round must be complete before a source is dropped, and a lost or
  truncated bundle is confirmed with a per-index probe instead of guessed.
- **State** — one scheduler asks Vimix for the 9 properties VJmix actually
  renders (`name`, `alpha`, `blending`, `correction`, `depth`, `play`, `lock`,
  `speed`, `seek`) once per second, plus whatever a controller declares
  interested. Geometry and identity are not polled: they are on demand.
- **Presence** — any request from VJmix counts as a consumer. After 15 s of
  quiet the tier and the roster stretch to 10 s and declared interests stop
  firing; the first request after that forces one full round, so a returning
  VJmix never reads a stale mirror.
- **Thumbnails and preview** — Vimix's `uri` is discovered per source, then
  ffmpeg produces the frames viOSC serves and caches.

## Build

Linux AppImage (x86_64 by default; `ARCH=aarch64` for arm64):

```bash
bash scripts/build_appimage.sh
bash scripts/verify_appimage.sh
```

macOS: `Actions → build` (x86_64 on `macos-15-intel`, arm64 on `macos-15`), or
locally:

```bash
ARCH=arm64 bash scripts/build_macos_app.sh
```

## Known limitations

- Vimix sends the roster in a single UDP datagram and macOS caps a datagram at
  9216 B: with ~75+ sources a bundle can be lost. viOSC detects it and rebuilds
  the roster with an index probe.
- viOSC must run on the same machine as Vimix (localhost, default OSC ports).
- The pairing token protects the link, not the wire.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python viosc.py --headless
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy
.venv/bin/python -m pytest tests/ -q
.venv/bin/pre-commit install
```

## Related

- [VJmix](https://github.com/lupin3rd/VJmix) — the controller this exists for
- [Vimix](https://github.com/brunoherbelin/vimix) — the VJ software
- [Vimix OSC API](https://github.com/brunoherbelin/vimix/wiki/Open-Sound-Control-API)

## Support

viOSC is free and GPL-3.0. If it is useful, you can
[buy me a coffee](https://buymeacoffee.com/lupin3rd) — a thank-you, never a
requirement.

## License

[GNU General Public License v3.0](https://www.gnu.org/licenses/gpl-3.0.html).
