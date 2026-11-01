# Legacy `/viosc/*` OSC reference

Deprecated in viOSC 0.9.0, kept for VJmix 1.1.x compatibility and removed in
0.10.0. The supported UI transport is the HTTP data plane in the README
(`/state`, `/events`, `/interest`, `/thumb`, `POST /thumb/regen`).

## Legacy OSC protocol

### Addressing sources

Sources can be targeted **by name** (preferred) or **by index** in every viOSC command:

- `/viosc/thumb/bird 0` — by name
- `/viosc/thumb/2 0` — by index

Two things to know about how Vimix replies:

- Vimix **echoes the target you used**: a `get` sent to `/vimix/bird/get` is answered with `/vimix/bird/...` (name), one sent to `/vimix/2/get` with `/vimix/2/...` (index). viOSC resolves both on ingestion.
- **Avoid `#index` targets** (`/vimix/#2/...`): Vimix would reply with `/vimix/#2/...`, which viOSC does not resolve. Use the plain index or the name.

The monitor registry is **keyed by name** on purpose: Vimix re-indexes sources on reorder/removal, while a name only changes when a user renames the source manually.

### Deprecated: `/viosc/*` OSC lanes (removed in 0.10.0)

The lanes in this section are kept in 0.9.0 because VJmix 1.1.x still uses
`/viosc/regen_thumb` and keeps `/viosc/thumb` + `/viosc/replythumb` and
`/viosc/replydata` as last-resort fallbacks. Use the HTTP replacements:

| Deprecated OSC lane | Replacement |
|---|---|
| `/viosc/replydata` | `GET /state` (snapshot) + `GET /events` (delta stream) |
| `/viosc/thumb` + `/viosc/replythumb` | `GET /thumb/<name>/<index>` |
| `/viosc/regen_thumb/<name>` | `POST /thumb/regen` (`{"source": "<name>"}`) |
| `/viosc/monitor` + `/viosc/watch` | `POST /interest` |
| `/viosc/sync/<target>` | `POST /interest` (one-shot) |
| `/viosc/auth` | `POST /auth` (OSC-only clients may keep using it) |

### Commands — port 6666 (input)

> **Deprecated in 0.9.0.** The commands below still work (VJmix 1.1.x
> compatibility); the HTTP data plane is the supported path. Removal is
> planned for 0.10.0.

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

