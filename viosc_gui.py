"""Minimal viOSC dashboard (e58s02) — read-only status, local bind, log.

The ONLY module that imports tkinter: the daemon core, config and logbus are
GUI-free by construction. The window is a thin front-end over the live daemon
module (injected as ``daemon`` — never imported by name, avoiding the
``__main__`` double-import trap):

- the pairing code large (the operator reads it to pair VJmix),
- connection statistics (source count, Vimix last-seen, message counts),
- the LOCAL bind fields (preview_ip/preview_port) — the only editable inputs,
  applied locally via a same-pid restart (the management plane must never be
  reconfigured through itself from the remote UI),
- a READ-ONLY snapshot of the effective config with per-field source markers,
  and a live log pane with timestamps and an All/Errors filter.

Everything else is configured from VJmix over HTTP /config (e58s01). GUI mode is
the whole app: nothing is printed to the terminal. Closing the window ends the
process.
"""

import contextlib
import json
import os
import queue
import sys
import time
import tkinter as tk
from tkinter import ttk
from typing import Any

import config
from logbus import bus as log_bus

LOG_PUMP_MS = 50
STATUS_POLL_MS = 1000
CONFIG_POLL_MS = 2000
LOG_QUEUE_MAX = 500
FORM_COLUMNS = 2
TIMESTAMP_FMT = "%H:%M:%S"
FILTER_ALL = "All"
FILTER_ERRORS = "Errors only"


def _pretty_name(key: str) -> str:
    """Human-ish English label for a config key (snake_case -> spaced)."""
    return key.replace("_", " ")


def _format_idle(seconds: float | None) -> str:
    """'3s ago' / '2m 5s ago', or 'never' when Vimix did not talk yet."""
    if seconds is None:
        return "no data from Vimix yet"
    whole = max(0, int(seconds))
    minutes, secs = divmod(whole, 60)
    if minutes:
        return f"{minutes}m {secs}s ago"
    return f"{secs}s ago"


def render_snapshot(values: dict[str, Any], sources: dict[str, str]) -> str:
    """A read-only, sorted rendering of the effective config (e58s02).

    Every key is shown (including the hidden Vimix/ffmpeg fields) so the operator
    can see the full picture on machine A; the per-field source marker
    (json/env/default) explains where each value came from.
    """
    return "\n".join(
        f"{key} = {config.format_field(key, values[key])} ({sources.get(key, 'default')})"
        for key in sorted(values)
    )


class VioscWindow:
    """Builds and runs the single dashboard window (widgets owned by this class)."""

    def __init__(
        self,
        daemon,
        cfg_path: str,
        sources: dict[str, str],
        base_values: dict[str, Any],
    ) -> None:
        self.daemon = daemon
        self.cfg_path = cfg_path
        self.sources = sources
        self.base_values = base_values
        self.bind_vars: dict[str, tk.StringVar] = {}
        self.log_queue: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=LOG_QUEUE_MAX)
        self.log_lines: list[tuple[str, str]] = []  # (level, rendered line)
        self.log_filter = FILTER_ALL

        self.root = tk.Tk()
        version = getattr(self.daemon, "APP_VERSION", "")
        self.root.title(f"viOSC {version}" if version else "viOSC")
        self.root.geometry("680x560")
        self.root.minsize(560, 440)
        self._build()

    # -- layout -------------------------------------------------------------

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=6)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 2))
        ttk.Label(header, text="viOSC", font=("TkDefaultFont", 12, "bold")).pack(side="left")
        version = getattr(self.daemon, "APP_VERSION", "")
        if version:
            ttk.Label(header, text=f"v{version}", foreground="#666666").pack(
                side="left", padx=(6, 0), pady=(2, 0)
            )

        if getattr(self.daemon, "PAIRING_ENABLED", False):
            code = getattr(self.daemon, "PAIRING_CODE", "")
            ttk.Label(
                outer,
                text=f"Pairing code: {code}",
                font=("TkDefaultFont", 18, "bold"),
                foreground="#0050a0",
            ).pack(anchor="w", pady=(0, 4))

        self._build_bind(outer)
        self._build_snapshot(outer)
        self._build_log(outer)

        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(4, 0))
        self.status_var = tk.StringVar(value="Vimix: no data from Vimix yet")
        ttk.Label(footer, textvariable=self.status_var).pack(side="left")

        self.error_var = tk.StringVar()
        ttk.Label(outer, textvariable=self.error_var, foreground="#c00000").pack(anchor="w")

    def _build_bind(self, outer) -> None:
        frame = ttk.LabelFrame(outer, text="Local bind (machine A only)", padding=4)
        frame.pack(fill="x", pady=(0, 4))
        for column in range(FORM_COLUMNS * 2):
            frame.columnconfigure(column, weight=1)
        for index, key in enumerate(config.local_only_fields()):
            row, col = divmod(index, FORM_COLUMNS)
            grid_col = col * 2
            active = getattr(self.daemon, config.daemon_attr(key))
            var = tk.StringVar(value=config.format_field(key, active))
            self.bind_vars[key] = var
            ttk.Label(frame, text=_pretty_name(key), anchor="e").grid(
                row=row, column=grid_col, sticky="e", padx=(2, 4), pady=1
            )
            ttk.Entry(frame, textvariable=var).grid(
                row=row, column=grid_col + 1, sticky="ew", padx=(0, 6), pady=1
            )
        ttk.Button(frame, text="Apply & restart", command=self._on_apply_bind).grid(
            row=1, column=FORM_COLUMNS * 2 - 1, sticky="e", pady=(2, 0)
        )
        ttk.Label(
            outer,
            text="Everything else is configured from VJmix (Settings > viOSC).",
            foreground="#666666",
        ).pack(anchor="w", pady=(0, 2))

    def _build_snapshot(self, outer) -> None:
        frame = ttk.LabelFrame(outer, text="Configuration (read-only)", padding=4)
        frame.pack(fill="x", pady=(0, 4))
        text = tk.Text(frame, height=10, wrap="none", state="disabled")
        scroll = ttk.Scrollbar(frame, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.snapshot_text = text
        self._config_sig = json.dumps(self.base_values, sort_keys=True, default=str)
        self._set_snapshot(self.base_values, self.sources)

    def _set_snapshot(self, values: dict[str, Any], sources: dict[str, str]) -> None:
        """Replace the read-only snapshot body (main thread only)."""
        self.snapshot_text.configure(state="normal")
        self.snapshot_text.delete("1.0", "end")
        self.snapshot_text.insert("1.0", render_snapshot(values, sources))
        self.snapshot_text.configure(state="disabled")

    def _build_log(self, outer) -> None:
        frame = ttk.LabelFrame(outer, text="Log", padding=4)
        frame.pack(fill="both", expand=True, pady=(0, 4))
        toolbar = ttk.Frame(frame)
        toolbar.pack(fill="x", pady=(0, 2))
        ttk.Label(toolbar, text="Show:").pack(side="left")
        self.filter_var = tk.StringVar(value=FILTER_ALL)
        filter_box = ttk.Combobox(
            toolbar,
            textvariable=self.filter_var,
            values=(FILTER_ALL, FILTER_ERRORS),
            state="readonly",
            width=12,
        )
        filter_box.pack(side="left", padx=(4, 0))
        filter_box.bind("<<ComboboxSelected>>", self._apply_filter)

        self.log_text = tk.Text(frame, height=8, wrap="none", state="disabled")
        scroll = ttk.Scrollbar(frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.tag_configure("err", foreground="#c00000")
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    # -- log rendering ------------------------------------------------------

    def _render_line(self, text: str) -> str:
        stamp = time.strftime(TIMESTAMP_FMT)
        return f"[{stamp}] {text}"

    def _visible(self, level: str) -> bool:
        return self.log_filter == FILTER_ALL or level == "error"

    def _append_log(self, text: str, level: str) -> None:
        line = self._render_line(text)
        self.log_lines.append((level, line))
        if self._visible(level):
            self._insert_line(line, level)

    def _insert_line(self, line: str, level: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n", "err" if level == "error" else ())
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _apply_filter(self, _event=None) -> None:
        self.log_filter = self.filter_var.get()
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        for level, line in self.log_lines:
            if self._visible(level):
                self._insert_line(line, level)

    # -- tk callbacks -------------------------------------------------------

    def _on_apply_bind(self) -> None:
        raw = {key: var.get() for key, var in self.bind_vars.items()}
        values, problems = config.parse_form(raw, base=self.base_values)
        if problems:
            self.error_var.set(" ".join(problems))
            return
        self.error_var.set("")
        config.merge_save(self.cfg_path, {k: values[k] for k in config.local_only_fields()})
        # Same-pid re-exec: fresh boot reads the just-saved bind.
        os.execv(sys.executable, self.daemon.restart_command())

    # -- pumps --------------------------------------------------------------

    def pump_logs(self) -> None:
        try:
            while True:
                text, level = self.log_queue.get_nowait()
                self._append_log(text, level)
        except queue.Empty:
            pass
        self.root.after(LOG_PUMP_MS, self.pump_logs)

    def poll_status(self) -> None:
        daemon = self.daemon
        idle = daemon.vimix_idle_seconds()
        sources = len(daemon.vimix_data)
        self.status_var.set(
            f"Vimix: {_format_idle(idle)} · sources: {sources} · "
            f"messages from Vimix: {daemon.vimix_messages}"
        )
        self.root.after(STATUS_POLL_MS, self.poll_status)

    def poll_config(self) -> None:
        """Re-render the snapshot when the on-disk config changed (e58s02).

        The config file is the single source of truth (a save from VJmix already
        persists every applied field), so a periodic re-read keeps the dashboard
        honest without a cross-thread GUI call from the HTTP handler.
        """
        values, sources, _ = config.load_effective(self.cfg_path)
        sig = json.dumps(values, sort_keys=True, default=str)
        if sig != self._config_sig:
            self._config_sig = sig
            self._set_snapshot(values, sources)
        self.root.after(CONFIG_POLL_MS, self.poll_config)

    def on_close(self) -> None:
        log_bus.unsubscribe(self._bus_listener)
        self.root.destroy()

    # -- lifecycle ----------------------------------------------------------

    def run(self) -> None:
        """Subscribe the log listener (replaying the bus history) and loop."""

        def listener(text: str, level: str) -> None:
            with contextlib.suppress(queue.Full):
                self.log_queue.put_nowait((text, level))

        self._bus_listener = listener
        log_bus.subscribe(listener, replay=True)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.pump_logs()
        self.poll_status()
        self.poll_config()
        self.root.mainloop()


def run_gui(daemon, cfg_path: str, sources: dict[str, str], base_values: dict[str, Any]) -> None:
    """Open the viOSC dashboard and block until it closes (e58s02)."""
    window = VioscWindow(daemon, cfg_path, sources, base_values)
    window.run()
