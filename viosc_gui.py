"""Minimal viOSC window (e01s05/s07) — tiered config form + timed log + status.

The ONLY module that imports tkinter: the daemon core, config and logbus are
GUI-free by construction. The window is a thin front-end over the live daemon
module (injected as ``daemon`` — never imported by name, avoiding the
``__main__`` double-import trap):

- a config form in two tiers (user decision 2026-09-08, option B): the
  ESSENTIAL wiring is always visible (2 rows), a collapsible 'Advanced
  settings' section holds the rest. JSON-only fields (ffmpeg resolution,
  internal tuning) are never shown and are preserved on save via ``base``,
- a live log pane with per-line wall-clock timestamps and an 'All / Errors
  only' view filter, fed by the logbus queue and pumped on the main thread,
- a status row fed by the e01s04 Vimix-activity markers,
- ONE "Apply & restart" button: validate -> save the JSON -> re-exec the
  process (os.execv, same pid).

GUI mode is the whole app: nothing is printed to the terminal. Labels are
English. Closing the window ends the process.
"""

import contextlib
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
LOG_QUEUE_MAX = 500
FORM_COLUMNS = 3
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


class VioscWindow:
    """Builds and runs the single window (widgets owned by this class)."""

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
        self.entry_vars: dict[str, tk.StringVar] = {}
        self.log_queue: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=LOG_QUEUE_MAX)
        self.log_lines: list[tuple[str, str]] = []  # (level, rendered line)
        self.log_filter = FILTER_ALL

        self.root = tk.Tk()
        version = getattr(self.daemon, "APP_VERSION", "")
        self.root.title(f"viOSC {version}" if version else "viOSC")
        self.root.geometry("680x540")
        self.root.minsize(560, 420)
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
        note = ttk.Label(
            outer,
            text="Settings are saved to the config file and applied on restart: "
            "'Apply & restart' relaunches the daemon with the new values.",
            wraplength=660,
        )
        note.pack(anchor="w", pady=(0, 4))

        if getattr(self.daemon, "PAIRING_ENABLED", False):
            code = getattr(self.daemon, "PAIRING_CODE", "")
            ttk.Label(
                outer,
                text=f"Pairing code: {code}",
                font=("TkDefaultFont", 16, "bold"),
                foreground="#0050a0",
            ).pack(anchor="w", pady=(0, 4))

        self._build_form(outer)
        self._build_log(outer)
        self._build_footer(outer)

        self.error_var = tk.StringVar()
        ttk.Label(outer, textvariable=self.error_var, foreground="#c00000").pack(anchor="w")
        for key, env_var in config.ENV_VAR_MAP.items():
            if self.sources.get(key) == "env":
                self.error_var.set(
                    f"note: {_pretty_name(key)} is currently set by the {env_var} "
                    "environment variable"
                )

    def _field_grid(self, parent, keys: list[str]) -> None:
        """Grid of (label, entry) pairs, FORM_COLUMNS field columns wide."""
        for column in range(FORM_COLUMNS * 2):
            parent.columnconfigure(column, weight=1, uniform="field")
        for index, key in enumerate(keys):
            row, col = divmod(index, FORM_COLUMNS)
            grid_col = col * 2
            active = getattr(self.daemon, config.daemon_attr(key))
            var = tk.StringVar(value=config.format_field(key, active))
            self.entry_vars[key] = var
            ttk.Label(parent, text=_pretty_name(key), anchor="e").grid(
                row=row, column=grid_col, sticky="e", padx=(2, 4), pady=1
            )
            ttk.Entry(parent, textvariable=var).grid(
                row=row, column=grid_col + 1, sticky="ew", padx=(0, 6), pady=1
            )

    def _build_form(self, outer) -> None:
        essential = ttk.LabelFrame(outer, text="Configuration", padding=4)
        essential.pack(fill="x", pady=(0, 2))
        self._field_grid(essential, config.essential_fields())
        hint = ttk.Label(
            outer,
            text="Ports match viseq/Vimix defaults — change them only in a non-standard setup.",
            foreground="#666666",
        )
        hint.pack(anchor="w", pady=(0, 2))

        self.show_advanced = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            outer,
            text="Advanced settings",
            variable=self.show_advanced,
            command=self._toggle_advanced,
        ).pack(anchor="w", pady=(0, 2))
        self.advanced_frame = ttk.LabelFrame(outer, text="Advanced", padding=4)
        self._field_grid(self.advanced_frame, config.advanced_fields())
        hint_row = (len(config.advanced_fields()) + FORM_COLUMNS - 1) // FORM_COLUMNS
        ttk.Label(
            self.advanced_frame,
            text="fs_roots: comma-separated absolute folders (e.g. /mnt/media, ~/videos)",
            foreground="#666666",
        ).grid(row=hint_row, column=0, columnspan=FORM_COLUMNS * 2, sticky="w", pady=(2, 0))

    def _toggle_advanced(self) -> None:
        if self.show_advanced.get():
            self.advanced_frame.pack(fill="x", pady=(0, 4))
        else:
            self.advanced_frame.pack_forget()

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

    def _build_footer(self, outer) -> None:
        footer = ttk.Frame(outer)
        footer.pack(fill="x")
        self.status_var = tk.StringVar(value="Vimix: no data from Vimix yet")
        ttk.Label(footer, textvariable=self.status_var).pack(side="left")
        ttk.Button(footer, text="Apply & restart", command=self._on_apply).pack(side="right")

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

    def _on_apply(self) -> None:
        raw = {key: var.get() for key, var in self.entry_vars.items()}
        values, problems = config.parse_form(raw, base=self.base_values)
        if problems:
            self.error_var.set(" ".join(problems))
            return
        self.error_var.set("")
        config.save(self.cfg_path, values)
        # Same-pid re-exec: fresh boot reads the just-saved JSON.
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
        self.root.mainloop()


def run_gui(daemon, cfg_path: str, sources: dict[str, str], base_values: dict[str, Any]) -> None:
    """Open the viOSC window and block until it closes (e01s05)."""
    window = VioscWindow(daemon, cfg_path, sources, base_values)
    window.run()
