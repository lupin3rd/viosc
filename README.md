# viOSC

**viOSC** is an OSC router and state mirror for [Vimix](https://github.com/brunoherbelin/vimix). It runs beside Vimix on the same machine, forwards `/vimix/*` commands, mirrors source state, and serves a small HTTP data plane for VJmix or any controller.

**0.9.0 requires Vimix 0.9.3+.** Linux: AppImage or source. macOS: x86_64/arm64 DMG. [Release assets](https://github.com/lupin3rd/viosc/releases/tag/v0.9.0) · [Release notes](RELEASE_NOTES_0.9.0.md).

## Install

### Linux (AppImage)

Download `viosc-0.9.0-x86_64.AppImage` from the release, then:

```bash
chmod +x viosc-0.9.0-x86_64.AppImage
./viosc-0.9.0-x86_64.AppImage            # GUI
./viosc-0.9.0-x86_64.AppImage --headless # console daemon
```

### macOS

Download `viOSC-0.9.0-x86_64.dmg` or `viOSC-0.9.0-arm64.dmg`.

- x86_64 runs on Intel and on Apple Silicon via Rosetta 2.
- arm64 is a cross-build; layout/arch is CI-verified.
- The app is **unsigned**: right-click → Open, or
  `xattr -dr com.apple.quarantine /Applications/viOSC.app`.
- Allow incoming connections at the firewall prompt (6666/7001/8686).

Both bundles include Python, Tk, python-osc and static ffmpeg/ffprobe.

### From source

Requires Python 3.10+, Vimix 0.9.3+, `python-osc`, and ffmpeg/ffprobe for thumbnails/preview:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python viosc.py              # GUI
.venv/bin/python viosc.py --headless   # console daemon
```

A missing ffmpeg is not fatal: thumbnails/preview are disabled and the OSC/HTTP paths keep running.

## Ports

| Role | Address | Purpose |
|---|---|---|
| Input | `0.0.0.0:6666` | Controller commands; `/viosc/*` handled locally, `/vimix/*` forwarded to Vimix |
| Vimix replies | `127.0.0.1:7001` | Vimix's default OSC send port; the mirror is fed here |
| Vimix receive | `127.0.0.1:7000` | Vimix's default OSC receive port; the forward target |
| HTTP data plane | `0.0.0.0:8686` | State, stream, interest, thumbnails, config, files |

## HTTP data plane

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/state` | Whole mirror; `rev` + `ETag`/`304` |
| `GET` | `/events` | Server-Sent Events, one ordered delta per change |
| `POST` | `/interest` | `{source, props, cadence_ms, once?}`; `"*"` for the full mirror |
| `GET` | `/thumb/<name>/<index>` | One cached JPEG |
| `POST` | `/thumb/regen` | `{"source": "<name>"}` |
| `GET` | `/preview/<name>/{file,meta}` | Media preview/meta |
| `GET` | `/fs/...` | Read-only file manager (`/fs/roots`, `/fs/list`, `/fs/meta`, `/fs/raw`) |
| `GET`/`POST` | `/config` | Effective config / remote-editable fields |
| `POST` | `/restart` | Apply staged bind changes |
| `POST` | `/auth` | Exchange the pairing code for a bearer token |
| `POST` | `/lane` | Mark a consumer active on HTTP (suppresses legacy UDP pushes) |

When pairing is enabled, every request except `POST /auth` needs
`Authorization: Bearer <token>`.

## OSC

`/vimix/*` is forwarded verbatim to Vimix. Target sources by **name** (preferred;
stable across reordering) or plain index. Avoid `#index` targets: Vimix echoes
them back and viOSC cannot resolve that form.

The legacy `/viosc/*` lanes (`replydata`, `thumb`, `regen_thumb`, `monitor`,
`watch`, `sync`, `auth`) still work in 0.9.0 and are removed in **0.10.0**.
Full reference: [`docs/LEGACY_OSC.md`](docs/LEGACY_OSC.md).

## Pairing

viOSC shows a 4-digit code at start. With `pairing_enabled` (default), a peer
must present it over HTTP `POST /auth` (or OSC `/viosc/auth`) before it can
drive Vimix or read the data plane. The code expires after
`pairing_code_ttl_hours` (24 h), is persisted across restarts, and rotates after
abuse. Pairing is a shared-code gate over plaintext traffic, **not encryption**;
restrict the ports at the network level for anything stronger.

## Configuration

`~/.config/viosc/config.json` (XDG), precedence **JSON > environment > default**.
Edit it from the viOSC GUI or from VJmix over HTTP. Key fields:

| Field | Default | Notes |
|---|---|---|
| `listen_port` | `6666` | Controller input |
| `from_vimix_port` | `7001` | Vimix replies |
| `tovimix_port` | `7000` | Vimix receive |
| `preview_ip` / `preview_port` | `0.0.0.0` / `8686` | HTTP data plane; local-only |
| `ffmpeg_path` / `ffprobe_path` | `ffmpeg` / `ffprobe` | Resolved from PATH, Homebrew/MacPorts and the bundle |
| `sync_interval_ms` | `2000` | Roster sync; the state tier is 1 s |
| `pairing_enabled` | `true` | See above |

Other fields (ports, pairing, pruning, thumbnails) are in `config.py` and the GUI.

## How it works

- **Roster** — `/vimix/current/sync` every 2 s; Vimix's one-datagram bundle is read whole. A round must be complete (`current/<i>` == `<i>/id`) before pruning; a large drop or a lost bundle is confirmed by an index probe.
- **State** — one coalesced scheduler: a 1 s tier of 14 properties for every source plus the `/interest` cadence. Session changes get an immediate sync + 500 ms retry; the core sync has a 500 ms minimum gap.
- **Thumbnails** — `uri` discovery, then ffmpeg JPEG frames for video/image types (10/12); audio/text/sequences are reported as `other`.
- **Echo** — a forwarded absolute `/vimix/<target>/<prop>` set is applied to the mirror as it passes through.
- **Mirror** — one lock guards the table, the revision journal and the registries; SSE revisions stay unique.

## Build

Linux AppImage:

```bash
bash scripts/build_appimage.sh
bash scripts/verify_appimage.sh
```

macOS: `Actions → build-macos` (free Intel runner; x64 runtime smoke + arm64
cross-build), or locally:

```bash
ARCH=arm64 bash scripts/build_macos_app.sh
ARCH=x86_64 bash scripts/build_macos_app.sh
```

Development checks:

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy
.venv/bin/python -m pytest tests/ -q
```

## Known limitations

- Vimix sends the roster in one UDP datagram; macOS caps a datagram at 9216 B. Very large sessions (~75+ sources) can lose the bundle; viOSC detects it and rebuilds the roster with a per-index probe.
- macOS arm64 is a cross-build; x86_64 is the runtime-verified artifact and runs on Apple Silicon via Rosetta 2.
- The legacy `/viosc/*` OSC lanes are deprecated and removed in 0.10.0.
- viOSC must run on the same machine as Vimix (localhost, default OSC ports).

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python viosc.py --headless
.venv/bin/python -m pytest tests/ -q
.venv/bin/pre-commit install
```

## Related

- [Vimix](https://github.com/brunoherbelin/vimix) — the VJ software
- [Vimix OSC API](https://github.com/brunoherbelin/vimix/wiki/Open-Sound-Control-API)
- VJmix — the reference controller

## Support

viOSC is free and GPL-3.0. If it is useful, you can
[buy me a coffee](https://buymeacoffee.com/lupin3rd) — a thank-you, never a requirement.

## License

[GNU General Public License v3.0](https://www.gnu.org/licenses/gpl-3.0.html).
