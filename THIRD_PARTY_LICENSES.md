# Third-party licenses — viOSC

viOSC itself is distributed under **GPL-3.0** (see `LICENSE`).

The release bundles the following third-party components:

| Component | Version / source | License | How it is used |
|---|---|---|---|
| CPython | 3.13 (python.org / python-appimage) | PSF-2.0 | The bundled interpreter (AppImage, macOS `.app`) |
| Tcl/Tk | 8.6 (shipped with the CPython bundle) | BSD-style (Tcl/Tk) | The tkinter GUI |
| python-osc | `python-osc>=1.7` | MIT | OSC client/server |
| FFmpeg / FFprobe (Linux AppImage) | johnvansickle static amd64 build | GPL-3.0 | Thumbnails and preview, invoked via `subprocess` |
| FFmpeg / FFprobe (macOS arm64 `.app`) | `ffmpeg-static` `b6.1.1`, `darwin-arm64` binaries (build source: osxexperts.net) | GPL-3.0-or-later | Thumbnails and preview, invoked via `subprocess` |
| FFmpeg / FFprobe (macOS x86_64 `.app`) | `ffmpeg-static` `b6.1.1`, `darwin-x64` binaries | GPL-3.0-or-later | Thumbnails and preview, invoked via `subprocess` |

FFmpeg and FFprobe are **separate executables**, not linked into viOSC; they
are invoked via `subprocess` exactly as a host-installed ffmpeg would be. Their
licence texts are included in the release artifacts (`ffmpeg-LICENSE` inside
the macOS app bundle; the static build's licence in the AppImage cache).

The exact macOS binaries are pinned by sha256 in `scripts/build_macos_app.sh`:

```
ffmpeg-darwin-arm64   a90e3db6a3fd35f6074b013f948b1aa45b31c6375489d39e572bea3f18336584
ffprobe-darwin-arm64  bb2db6f5d8cef919da12fbf592119a987202a8c060a886f3cab091f9cab90b64
darwin-arm64.LICENSE  cb48bf09a11f5fb576cddb0431c8f5ed0a60157a9ec942adffc13907cbe083f2
ffmpeg-darwin-x64     ebdddc936f61e14049a2d4b549a412b8a40deeff6540e58a9f2a2da9e6b18894
ffprobe-darwin-x64    fa3add0ce901f7241abe0dfc0155d958fc834aca3f8ce61f87cc712ae669c1e0
darwin-x64.LICENSE    2e1d16c72fd74e12063776371da757322f8b77589386532f4fd8634bde7de1af
```
