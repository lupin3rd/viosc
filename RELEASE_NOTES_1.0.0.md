# viOSC 1.0.0

**Requires VJmix 1.3.0 and Vimix 0.9.3 or newer.**

- **One link.** Everything between VJmix and viOSC is HTTP on one port with one
  token: commands on a single long-lived `POST /link` stream, and state, deltas,
  thumbnails, preview, files and config on the same surface. OSC now exists only
  toward Vimix.
- **One instance.** A second launch refuses to start, and closing the window
  ends the daemon. The window is compact and only grows when asked.
- **Less traffic.** The state tier asks Vimix only for the 9 properties VJmix
  renders — 162.5 → 112.5 messages/s at 10 sources — and drops to a 10 s
  cadence while no consumer is connected: 162.5 → 13.5.
- **The upstream is heard at once.** Vimix's replies are ingested before anyone
  pairs, and the pairing code is hidden while the HTTP surface is not up.
