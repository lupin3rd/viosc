# viOSC

**viOSC** is an OSC router, state mirror and monitoring layer for [Vimix](https://github.com/brunoherbelin/vimix), the live-mixing VJ software. It runs on the same machine as Vimix, talks to it over localhost using Vimix's **default OSC ports** (no Vimix configuration required), and exposes a small OSC API for an external UI/controller.

It keeps an up-to-date table of Vimix source properties in memory and pushes it to your UI:

- **Base table** — every source's `name` and `alpha`, refreshed every 2 seconds from Vimix's sync bundle, with automatic removal of sources that no longer exist.
- **Thumbnails** — for every media source: the `uri` is fetched once (on discovery) and a random JPEG frame (10%–90% of the video) is generated locally with FFmpeg.
- **On-demand monitoring** — tell viOSC to poll any property of any source (`depth`, `play`, `position`, …) every 2 seconds, and the values land in the table automatically.
- **Name-based addressing** — commands target sources by **name** (stable across reordering/removal), while indices are only used internally to parse Vimix's replies.

> This project talks the protocol documented in the [Vimix Open Sound Control API](https://github.com/brunoherbelin/vimix/wiki/Open-Sound-Control-API).

---

## Features

- Multi-port OSC router with transparent pass-through of standard `/vimix/...` commands
- In-memory state cache of all sources and their properties
- Automatic `uri` discovery + random thumbnail generation per media source (concurrency-limited)
- Per-source property **monitoring** with a 2 s polling loop
- Ghost-source pruning (sources removed in Vimix disappear from the table)
- JSON state broadcast to the UI, with the monitor registry included
- Start-up dependency checks (`python-osc`, `ffmpeg`, `ffprobe`) with clear error messages
- All communication with Vimix happens on localhost with Vimix's default OSC ports (7000 receive / 7001 send) — **zero Vimix configuration**

---

## Architecture

```
[ UI / Controller ]  ── commands ──▶  0.0.0.0:6666  ──forward──▶  127.0.0.1:7000  [ Vimix ]
                                          ▲                                       │
                                          │           Vimix replies on 7001       │
                                          │  (its default OSC *send* port)        │
                                      viosc ◀─────────────────────────────────┘
                                          │
[ UI / Controller ]  ◀── state / thumbnails / monitor ──  UI_IP:6667  ◀── viosc
```

viOSC runs two UDP servers and one client:

| Role | Bind / Destination | Purpose |
| :--- | :---: | :--- |
| **Input** | `0.0.0.0:6666` | Receives commands from the UI/controller. `/viosc/*` messages are handled locally; anything else is forwarded verbatim to Vimix on port 7000. |
| **Vimix replies** | `127.0.0.1:7001` | Listens on Vimix's default OSC **send** port. Vimix sends its sync bundles and `get` replies here; viOSC ingests them into the state table. |
| **Output** | `UI_IP:6667` | Sends state broadcasts, thumbnails and monitor replies to the UI. `UI_IP` defaults to `127.0.0.1` and can point to a remote UI machine. |

`TOVIMIX_PORT` (`7000`) is Vimix's default OSC **receive** port — where viOSC forwards all commands.

---

## Requirements

- **Python 3.8+**
- [**python-osc**](https://pypi.org/project/python-osc/) — `pip install python-osc`
- **FFmpeg / FFprobe** — required for thumbnail extraction (e.g. `sudo apt install ffmpeg` on Debian/Ubuntu)

At start-up viOSC verifies that `python-osc` is importable and that the `ffmpeg` / `ffprobe` executables are reachable, and exits with a clear error if any is missing.

> **AppImage:** from v0.3.0 a single-file AppImage is available that bundles
> CPython, python-osc, the tkinter GUI and a **static GPL ffmpeg/ffprobe**, so
> no Python or ffmpeg install is needed (see [Packaging](#packaging)).

---

## GUI

Since v0.3.0 viOSC opens a **minimal GUI window** by default (config fields +
live log with timestamps + Vimix status + "Apply & restart"). The window is
configured, logged and versioned there; nothing is printed to the terminal in
GUI mode. Run `python viosc.py --headless` for the console daemon behaviour.

## Packaging

Build the self-contained AppImage (one file with everything, including the
static ffmpeg/ffprobe used for thumbnails and preview):

```bash
bash scripts/build_appimage.sh          # dist/viosc-<APP_VERSION>-x86_64.AppImage
bash scripts/verify_appimage.sh         # layout + headless smoke
```

Run it like the source version (GUI by default, `--headless` for the console
daemon). Note: the AppImage bundles a **GPL-licensed static ffmpeg/ffprobe**
as separate executables invoked via subprocess — viOSC's own MIT license is
not affected (same boundary as installing ffmpeg with apt today).

---

## Installation

```bash
git clone <this-repository>
cd viosc
pip install -r requirements.txt
```

`requirements.txt` contains a single dependency: [`python-osc`](https://pypi.org/project/python-osc/).

---

## Usage

Run viOSC on the same machine as Vimix:

```bash
python viosc.py
```

Start-up banner:

```
==========================================================================================
                      MULTI-PORT OSC ROUTER 'viOSC' STARTED
   input   : 0.0.0.0:6666   (commands -> vimix 127.0.0.1:7000)
   from vimix: 127.0.0.1:7001   (vimix default OSC replies)
   output  : 127.0.0.1:6667   (state / thumbnails / monitor for the UI)
==========================================================================================
```

### Configuration

Settings are constants at the top of `viosc.py`, plus three environment variables:

| Environment variable | Default | Description |
| :--- | :--- | :--- |
| `VIOSC_UI_IP` | `127.0.0.1` | Destination for all replies (state, thumbnails, monitor registry). Set it to the IP of the UI machine when the UI runs remotely. |
| `VIOSC_FFMPEG` | `ffmpeg` | Path to the `ffmpeg` executable (e.g. `/usr/bin/ffmpeg`). |
| `VIOSC_FFPROBE` | `ffprobe` | Path to the `ffprobe` executable. |

```bash
VIOSC_UI_IP=192.168.1.50 python viosc.py
```

| Constant | Default | Description |
| :--- | :--- | :--- |
| `LISTEN_IP` / `LISTEN_PORT` | `0.0.0.0` / `6666` | Input interface/port for UI commands. |
| `LOCAL_BIND_IP` / `FROMVIMIX_PORT` | `127.0.0.1` / `7001` | Where Vimix replies are received (Vimix's default OSC send port). |
| `UI_IP` / `REPLY_PORT` | env / `6667` | Reply destination for the UI. |
| `TOVIMIX_IP` / `TOVIMIX_PORT` | `127.0.0.1` / `7000` | Vimix's default OSC receive port. |
| `LOG_LEVEL` | `1` | `0` = quiet, `1` = log property changes. |
| `sync_interval_time` | `2000` | Sync + monitor polling period, in milliseconds. |
| `PRUNE_DELAY_SEC` | `0.5` | Delay after a sync round before pruning removed sources. |
| `MONITOR_MAX_MISSES` | `3` | Consecutive failed polls before a monitor entry is dropped. |
| `THUMB_MAX_CONCURRENCY` | `3` | Maximum simultaneous FFmpeg thumbnail extractions. |

### Pairing (default ON)

viOSC shows a random **4-digit code** at every start (prominent in the GUI; in
the log/console under `--headless`) and, with `pairing.enabled` (the default),
accepts a peer only after that peer has presented the code:

- **HTTP data plane (`:8686`)** — `POST /auth` with `{"code": "1234"}` returns a
  bearer token; every other request needs `Authorization: Bearer <token>` and
  otherwise answers **401**.
- **OSC planes** — a successful HTTP auth binds the peer's source IP for the OSC
  input (including the `/vimix/*` forward) and the state/thumbnail output; an
  OSC-only client can bind with the additive `/viosc/auth <code>` message.

| Setting (config JSON) | Default | Description |
| :--- | :--- | :--- |
| `pairing_enabled` | `true` | Require the code on every surface. Set `false` to restore the previous open behaviour. |
| `pairing_code_length` | `4` | Digits in the code (rotates at every start, never persisted). |
| `pairing_lease_seconds` | `3600` | How long a bound peer stays bound (it re-authenticates after). |
| `pairing_trusted_peers` | `[]` | IPs that bypass pairing (the escape for third-party OSC controllers). |
| `pairing_lock_after` | `5` | Consecutive failed attempts before that peer is locked. |
| `pairing_lock_seconds` | `60` | Lock duration, and the window for the global failure counter. |
| `pairing_global_lock_after` | `20` | Failures across all peers before the code **regenerates** and auth freezes briefly. |

Honest limitation: this is a *pairing* measure, **not encryption**. The code and
the traffic are plaintext UDP/HTTP, and a spoofed source IP can impersonate a
bound peer. It keeps casual and misconfigured hosts out on a LAN; combine it with
a firewall for anything stronger.

---

## OSC Protocol Reference

### Addressing sources

Sources can be targeted **by name** (preferred) or **by index** in every viOSC command:

- `/viosc/thumb/bird 0` — by name
- `/viosc/thumb/2 0` — by index

Two things to know about how Vimix replies:

- Vimix **echoes the target you used**: a `get` sent to `/vimix/bird/get` is answered with `/vimix/bird/...` (name), one sent to `/vimix/2/get` with `/vimix/2/...` (index). viOSC resolves both on ingestion.
- **Avoid `#index` targets** (`/vimix/#2/...`): Vimix would reply with `/vimix/#2/...`, which viOSC does not resolve. Use the plain index or the name.

The monitor registry is **keyed by name** on purpose: Vimix re-indexes sources on reorder/removal, while a name only changes when a user renames the source manually.

### Commands — port 6666 (input)

#### `/viosc/auth <code>`

Present the pairing code to bind this peer's source IP for the OSC planes (the
additive counterpart of `POST /auth`, for OSC-only controllers). Ignored when
`pairing_enabled` is `false`.

```osc
/viosc/auth 1234
```

#### `/viosc/monitor/<name> [prop1 prop2 ...]`

Start or replace monitoring of the given properties for a source, and stop it when called with no arguments.

```osc
/viosc/monitor/bird depth play lock
/viosc/monitor/bird              # stop monitoring "bird"
```

Behavior: an immediate `get` is sent for fast feedback, then the properties are polled every `sync_interval_time` ms. New values are written to the state table and broadcast if they changed. A monitor entry whose name no longer resolves (source deleted) is dropped after `MONITOR_MAX_MISSES` consecutive polls.

#### `/viosc/sync/<target> [prop1 prop2 ...]`

One-shot `get` request forwarded to Vimix — no persistent monitoring. With no arguments (or `all`), all supported properties are requested.

```osc
/viosc/sync/bird position size angle
/viosc/sync/bird all
```

#### `/viosc/thumb/<target> [all|N]`

Fetch generated thumbnail(s) from the local cache. `N` is the thumbnail index, `all` (the default) sends every available thumbnail.

```osc
/viosc/thumb/bird 0
/viosc/thumb/bird all
```

Reply (on port 6667):

```
/viosc/replythumb/<target>/<N>   [JPEG blob]
```

If no thumbnail is ready yet, the command is silently ignored — re-issue it later.

#### `/viosc/regen_thumb/<target>`

Regenerate the thumbnail for a source (new random frame), e.g. after a failed extraction or to get a different frame.

```osc
/viosc/regen_thumb/bird
```

#### Transparent pass-through

Any other message on port 6666 is forwarded verbatim to Vimix on port 7000:

```osc
/vimix/bird/alpha 0.5
/vimix/bird/play 1
```

Vimix's reply arrives back on port 7001, is ingested into the state table, and is broadcast if the value changed.

### Replies — port 6667 (output)

#### `/viosc/replydata`

Full state broadcast, sent whenever a value changes (and at start-up/prune). Single string argument containing a JSON payload:

```json
{
  "current_source": 2,
  "sources": {
    "0": { "name": "bird",  "alpha": 0.998, "uri": "file:///videos/bird.mp4" },
    "1": { "name": "camera", "alpha": 0.3,   "depth": 6.0 }
  },
  "monitored": { "bird": ["depth", "play", "lock"] }
}
```

| Field | Description |
| :--- | :--- |
| `current_source` | Index of the source currently active in Vimix (informational; `null` when no source exists). |
| `sources` | Map of source index → properties. Always contains at least `name` and `alpha`; `uri` appears once discovered; monitored properties appear as they are polled. Thumbnails are **not** included (binary — fetch them via `/viosc/thumb/`). |
| `monitored` | The current monitor registry: source name → property list. |

### Messages from Vimix — port 7001 (ingested)

| Address | Meaning | Internal action |
| :--- | :--- | :--- |
| `/vimix/current/<i>` | Sync marker for source `i` (list-of-sources bundle) | Tracks `current_source`; detects sync rounds for pruning. |
| `/vimix/<target>/<prop>` | Property value (`name`, `alpha`, `uri`, `depth`, …) | Updates the state table; broadcasts when changed; on a new/changed `uri` triggers thumbnail generation. |

---

## How it works

1. **Sync loop** — every `sync_interval_time` ms, viOSC sends `/vimix/current/sync` to Vimix. The reply bundle carries the current-source markers plus `name` and `alpha` for every source, which builds the base table.
2. **Uri discovery** — the first time a source name is seen, viOSC sends a one-shot `/vimix/<name>/get uri`. On arrival, a thumbnail is generated: a random frame between 10% and 90% of the video duration, extracted with FFmpeg (320×180 JPEG), at most `THUMB_MAX_CONCURRENCY` extractions at once.
3. **Pruning** — 500 ms after a sync round begins, sources not reported in the round are removed from the cache (sources deleted in Vimix disappear from the table and from the broadcast).
4. **Monitoring** — for every entry in `monitored`, a `/vimix/<name>/get <props...>` is sent each sync cycle; replies are ingested and broadcast on change.
5. **Renames** — renaming a source keeps its cached entry (thumbnail included) and re-fetches the `uri` once. If the uri is unchanged (a true rename) the thumbnail is kept; if it changed (a replacement source on the same index) a new thumbnail is generated.

---

## Known limitations

- viOSC must run **on the same machine as Vimix** — the Vimix channel uses localhost and Vimix's default OSC ports.
- A monitor entry is keyed by name: it survives Vimix reordering, but **stops following a renamed source** (removed after `MONITOR_MAX_MISSES` missed polls).
- `get` replies addressed by name are ingested only for sources already present in the cache — a source enters the cache at the next sync round (≤ 2 s).
- If Vimix holds **zero sources**, no sync markers arrive and the cache keeps its last state until a non-empty session is synced.
- The input port binds `0.0.0.0`. Since pairing is **on by default** (see [Pairing](#pairing-default-on)) an unbound host cannot drive Vimix through viOSC, nor read the `:8686` data plane — but pairing is a shared-code gate over plaintext traffic, not encryption, so restrict the ports at the network level for anything stronger.

---

## Related

- [Vimix](https://github.com/brunoherbelin/vimix) — the VJ software this router talks to
- [Vimix OSC API documentation](https://github.com/brunoherbelin/vimix/wiki/Open-Sound-Control-API)

---

## License

This project is licensed under the [GNU General Public License v3.0](https://www.gnu.org/licenses/gpl-3.0.html) (GPL-3.0).
