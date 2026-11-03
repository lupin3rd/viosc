viOSC 0.9.1 — requires Vimix 0.9.3+.

- Linux AppImage for x86_64 and arm64.
- CI migrated to the supported macOS 15 runners; the macOS x86_64/arm64
  DMGs are unchanged.
- No OSC or HTTP contract change.

The arm64 AppImage is built and smoke-tested natively on `ubuntu-24.04-arm`.
armhf is not shipped: the AppImage base has no armhf build.
