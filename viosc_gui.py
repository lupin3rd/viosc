"""Minimal viOSC dashboard (e58s02) — read-only status, local bind, log.

The ONLY module that imports tkinter: the daemon core, config and logbus are
GUI-free by construction. The window is a thin front-end over the live daemon
module (injected as ``daemon`` — never imported by name, avoiding the
``__main__`` double-import trap):

- the pairing code large (the operator reads it to pair VJmix),
- connection statistics (source count, Vimix last-seen, message counts),
- the LOCAL bind fields (http_ip/http_port) — the only editable inputs,
  applied locally via a same-pid restart (the management plane must never be
  reconfigured through itself from the remote UI),
- a READ-ONLY snapshot of the effective config with per-field source markers,
  and a live log pane (closed by default) with timestamps and an All/Errors
  filter, opened from its one-line toggle.

Everything else is configured from VJmix over HTTP /config (e58s01). GUI mode is
the whole app: nothing is printed to the terminal. **Closing the window ends the
process and its servers** (operator decision, 2026-11-08) — one viOSC per machine,
guarded by an instance lock, so the next launch starts from a clean state.
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
from logbus import strip_ansi

LOG_PUMP_MS = 50
STATUS_POLL_MS = 1000
CONFIG_POLL_MS = 2000
LOG_QUEUE_MAX = 500
GUI_LOG_MAX_LINES = 2000
FORM_COLUMNS = 2
SNAPSHOT_HEIGHT = 4
SNAPSHOT_NOTE = "Everything else is configured from VJmix (Settings > viOSC)."
HEADER_WRAP = 600  # the pairing line and the HTTP-down reason share the header row
WINDOW_SIZE = (680, 560)
MINSIZE = (560, 260)  # the compact window is ~282 px tall: do not floor it higher
LOG_TITLE = "Log"
LOG_OPEN_MARK = "▾"
LOG_CLOSED_MARK = "▸"
PAIRING_FONT_UP = ("TkDefaultFont", 16, "bold")
PAIRING_FONT_DOWN = ("TkDefaultFont", 10, "bold")
TIMESTAMP_FMT = "%H:%M:%S"
FILTER_ALL = "All"
FILTER_ERRORS = "Errors only"


def _pretty_name(key: str) -> str:
    """Human-ish English label for a config key (snake_case -> spaced)."""
    return key.replace("_", " ")


def pairing_banner(code: str, link: str) -> tuple[str, str]:
    """The big line at the top: the code, or why it cannot be used (e98s04).

    A second viOSC instance that loses the HTTP port still opened a window and
    generated/loaded a code — but nobody can pair with it, because it serves no
    surface. Printing that code is a trap: the operator types it against the
    instance that DOES own the port and gets "pairing failed" for no visible
    reason. So the code is shown only while the HTTP listener is up.
    """
    if link == "up":
        return (f"Pairing code: {code}", "#0050a0")
    return (
        f"HTTP surface DOWN ({link}) — pairing is impossible from this window.\n"
        "Another viOSC is probably running: close it and restart.",
        "#c00000",
    )


def _format_idle(seconds: float | None) -> str:
    """'3s ago' / '2m 5s ago', or 'never' when Vimix did not talk yet."""
    if seconds is None:
        return "no data from Vimix yet"
    whole = max(0, int(seconds))
    minutes, secs = divmod(whole, 60)
    if minutes:
        return f"{minutes}m {secs}s ago"
    return f"{secs}s ago"


def log_toggle_text(is_open: bool) -> str:
    """The log toggle line: the arrow shows which way the NEXT click goes."""
    if is_open:
        return f"{LOG_OPEN_MARK} {LOG_TITLE} (click to close)"
    return f"{LOG_CLOSED_MARK} {LOG_TITLE} (click to open)"


def fitted_height(requested: int, remembered: int, is_open: bool) -> int:
    """The window height after a toggle: grow back to the open size, shrink to content."""
    return max(requested, remembered) if is_open else requested


SNAPSHOT_ROWS: tuple[tuple[str, str], ...] = (
    ("http_port", "HTTP"),
    ("from_vimix_port", "Vimix in"),
    ("tovimix_port", "Vimix out"),
    ("sync_interval_ms", "Roster poll"),
    ("thumb_max_count", "Thumbs"),
    ("log_level", "Log level"),
)


def snapshot_rows(values: dict[str, Any], sources: dict[str, str], link: str = "") -> list[str]:
    """The six rows an operator actually reads, plus the link state (e98s06).

    Everything else is configured from VJmix over `GET/POST /config`; the window
    is a viewer with two editable fields, so a raw key dump was noise. The source
    marker (json/env/default) stays: it explains where a value came from.
    """
    rows = [
        f"{label}: {config.format_field(key, values[key])} ({sources.get(key, 'default')})"
        for key, label in SNAPSHOT_ROWS
        if key in values
    ]
    if link:
        rows.append(f"Link: {link}")
    return rows


def render_snapshot(values: dict[str, Any], sources: dict[str, str], link: str = "") -> str:
    """The snapshot as plain text (the pure form the dashboard splits)."""
    return "\n".join([*snapshot_rows(values, sources, link), SNAPSHOT_NOTE])


def split_columns(rows: list[str], columns: int = FORM_COLUMNS) -> list[list[str]]:
    """Split ``rows`` into ``columns`` chunks, filling the left column first."""
    if not rows:
        return [[] for _ in range(columns)]
    size = -(-len(rows) // columns)
    chunks = [rows[index : index + size] for index in range(0, len(rows), size)]
    return (chunks + [[] for _ in range(columns)])[:columns]


def bind_cells(
    count: int, per_row: int = FORM_COLUMNS
) -> tuple[list[tuple[int, int]], tuple[int, int]]:
    """(row, label column) of every bind field, and the Apply button's own cell.

    The button shares the LAST field row (operator, 2026-11-08: "Apply & restart
    on the same line as http ip and port"), in the column right after the fields.
    """
    cells = [(row, column * 2) for row, column in (divmod(i, per_row) for i in range(count))]
    return cells, (cells[-1][0] if cells else 0, per_row * 2)


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
        self._open_height = 0  # the height the operator had while the log was open

        self.root = tk.Tk()
        version = getattr(self.daemon, "APP_VERSION", "")
        self.root.title(f"viOSC {version}" if version else "viOSC")
        self.root.geometry(f"{WINDOW_SIZE[0]}x{WINDOW_SIZE[1]}")
        self.root.minsize(*MINSIZE)
        self._build()
        # Start COMPACT: the log is closed, so the window is as tall as the
        # content, not the 560 px placeholder (operator, 2026-11-08).
        self._fit_window()

    # -- layout -------------------------------------------------------------

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=(6, 6, 6, 2))
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 2))
        ttk.Label(header, text="viOSC", font=("TkDefaultFont", 12, "bold")).pack(side="left")
        version = getattr(self.daemon, "APP_VERSION", "")
        if version:
            ttk.Label(header, text=f"v{version}", foreground="#666666").pack(
                side="left", padx=(6, 0), pady=(2, 0)
            )

        self.pairing_label = None
        if getattr(self.daemon, "PAIRING_ENABLED", False):
            # Operator, 2026-11-08: ONE line — the version and the code together,
            # the code flush right (the version is static, the code is what the
            # operator reads and retypes).
            self.pairing_var = tk.StringVar()
            self.pairing_label = ttk.Label(
                header,
                textvariable=self.pairing_var,
                font=PAIRING_FONT_UP,
                justify="right",
                wraplength=HEADER_WRAP,
            )
            self.pairing_label.pack(side="right", padx=(16, 0), pady=(2, 0))
            self._refresh_pairing()

        self._build_bind(outer)
        self._build_snapshot(outer)
        self._build_log(outer)

        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(4, 0))
        self.status_var = tk.StringVar(value="Vimix: no data from Vimix yet")
        ttk.Label(footer, textvariable=self.status_var).pack(side="left")

        # The error line is created UNPACKED: an empty label still costs a row,
        # and a row of dead space is exactly what the compact window avoids
        # (operator, 2026-11-08). It appears only while there is an error.
        self.error_var = tk.StringVar()
        self.error_label = ttk.Label(outer, textvariable=self.error_var, foreground="#c00000")

    def _build_bind(self, outer) -> None:
        frame = ttk.LabelFrame(outer, text="Local bind (machine A only)", padding=4)
        frame.pack(fill="x", pady=(0, 4))
        for column in range(FORM_COLUMNS * 2):
            frame.columnconfigure(column, weight=1)
        keys = config.local_only_fields()
        cells, button = bind_cells(len(keys))
        for key, (row, grid_col) in zip(keys, cells, strict=True):
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
            row=button[0], column=button[1], sticky="e", padx=(2, 0), pady=1
        )
        ttk.Label(outer, text=SNAPSHOT_NOTE, foreground="#666666").pack(anchor="w", pady=(0, 2))

    def _build_snapshot(self, outer) -> None:
        frame = ttk.LabelFrame(outer, text="Configuration (read-only)", padding=4)
        frame.pack(fill="x", pady=(0, 4))
        self.snapshot_texts: list[tk.Text] = []
        for column in range(FORM_COLUMNS):
            cell = ttk.Frame(frame)
            cell.pack(side="left", fill="both", expand=True)
            # width=1: let pack's expand share the row 50/50 (a Text defaults to
            # 80 chars and would push the right column out of the frame).
            text = tk.Text(cell, width=1, height=SNAPSHOT_HEIGHT, wrap="none", state="disabled")
            text.pack(side="left", fill="both", expand=True, padx=(0 if column == 0 else 8, 0))
            self.snapshot_texts.append(text)
        self._config_mtime = self._stat_mtime()
        self._set_snapshot(self.base_values, self.sources)

    def _stat_mtime(self) -> int:
        try:
            return os.stat(self.cfg_path).st_mtime_ns
        except OSError:
            return 0

    def _set_snapshot(self, values: dict[str, Any], sources: dict[str, str]) -> None:
        """Replace the read-only snapshot body, two columns (main thread only)."""
        rows = snapshot_rows(
            values, sources, link=getattr(self.daemon, "http_link_status", lambda: "")()
        )
        for text, chunk in zip(self.snapshot_texts, split_columns(rows), strict=True):
            text.configure(state="normal")
            text.delete("1.0", "end")
            text.insert("1.0", "\n".join(chunk))
            text.configure(state="disabled")

    def _build_log(self, outer) -> None:
        """A CLOSED-by-default log pane behind a one-line toggle (operator, 2026-11-08).

        The log is noise until something goes wrong, so it opens on a click: the
        toggle stays visible, the pane is packed only while open, and the bus
        keeps filling ``log_lines`` in the background so opening it shows the
        history, not just what arrives next.
        """
        section = ttk.Frame(outer)
        section.pack(fill="x")
        self.log_section = section
        self.log_toggle = ttk.Button(section, text=log_toggle_text(False), command=self.toggle_log)
        self.log_toggle.pack(anchor="w")

        frame = ttk.LabelFrame(section, text=LOG_TITLE, padding=4)
        self.log_frame = frame
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
        self._log_open = False

    def toggle_log(self) -> None:
        """Open or close the log pane from its one-line toggle (main thread only).

        The window HEIGHT follows the pane (operator, 2026-11-08): collapsing
        shrinks it to the content, reopening grows it back to the height the
        operator had, instead of leaving a dead empty area. The width is never
        touched — that one is the operator's.
        """
        self._log_open = not self._log_open
        if self._log_open:
            self.log_frame.pack(fill="both", expand=True, pady=(2, 4))
            self.log_section.pack_configure(fill="both", expand=True)
            self.log_text.see("end")
        else:
            self._open_height = self.root.winfo_height()
            self.log_frame.pack_forget()
            self.log_section.pack_configure(fill="x", expand=False)
        self.log_toggle.configure(text=log_toggle_text(self._log_open))
        self._fit_window()

    def _fit_window(self) -> None:
        """Resize only the height to the content (a manual resize is remembered)."""
        self.root.update_idletasks()
        width = self.root.winfo_width()
        height = fitted_height(self.root.winfo_reqheight(), self._open_height, self._log_open)
        self.root.geometry(f"{width}x{height}")

    # -- log rendering ------------------------------------------------------

    def _render_line(self, text: str) -> str:
        stamp = time.strftime(TIMESTAMP_FMT)
        return f"[{stamp}] {text}"

    def _visible(self, level: str) -> bool:
        return self.log_filter == FILTER_ALL or level == "error"

    def _append_log(self, text: str, level: str) -> None:
        line = self._render_line(strip_ansi(text))
        self.log_lines.append((level, line))
        if len(self.log_lines) > GUI_LOG_MAX_LINES:
            del self.log_lines[: len(self.log_lines) - GUI_LOG_MAX_LINES]
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

    def _show_error(self, text: str) -> None:
        """Show or hide the error line, and keep the window fitted to it."""
        self.error_var.set(text)
        packed = bool(self.error_label.winfo_manager())
        if text and not packed:
            self.error_label.pack(anchor="w")
        elif not text and packed:
            self.error_label.pack_forget()
        else:
            return
        self._fit_window()

    def _on_apply_bind(self) -> None:
        raw = {key: var.get() for key, var in self.bind_vars.items()}
        values, problems = config.parse_form(raw, base=self.base_values)
        if problems:
            self._show_error(" ".join(problems))
            return
        self._show_error("")
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

    def _refresh_pairing(self) -> None:
        """One header line: the code while the surface is up, the reason while it is down."""
        if self.pairing_label is None:
            return
        link = getattr(self.daemon, "http_link_status", lambda: "starting")()
        text, colour = pairing_banner(getattr(self.daemon, "PAIRING_CODE", ""), link)
        if self.pairing_var.get() != text:
            self.pairing_var.set(text)
        self.pairing_label.configure(
            foreground=colour, font=PAIRING_FONT_UP if link == "up" else PAIRING_FONT_DOWN
        )

    def poll_status(self) -> None:
        daemon = self.daemon
        self._refresh_pairing()
        idle = daemon.vimix_idle_seconds()
        sources = len(daemon.vimix_data)
        self.status_var.set(
            f"Vimix: {_format_idle(idle)} · sources: {sources} · "
            f"messages from Vimix: {daemon.vimix_messages}"
        )
        self.root.after(STATUS_POLL_MS, self.poll_status)

    def poll_config(self) -> None:
        """Re-render the snapshot when the on-disk config changed (e58s02, e98s06).

        The config file is the single source of truth (a save from VJmix already
        persists every applied field), so a periodic re-read keeps the dashboard
        honest without a cross-thread GUI call from the HTTP handler. The re-read
        is gated by ``st_mtime_ns``: an unchanged file costs one ``stat`` per tick
        instead of a full load plus a ``json.dumps`` of every key, for ever.
        """
        try:
            mtime = os.stat(self.cfg_path).st_mtime_ns
        except OSError:
            mtime = 0
        if mtime != self._config_mtime:
            self._config_mtime = mtime
            values, sources, _ = config.load_effective(self.cfg_path)
            self.base_values = values
            self._set_snapshot(values, sources)
        self.root.after(CONFIG_POLL_MS, self.poll_config)

    def on_close(self) -> None:
        """Closing the window ends the daemon (operator decision, 2026-11-08).

        The window IS the app: no hidden background router, no second instance.
        The single-instance lock is released with the process, so the next launch
        starts clean.
        """
        with contextlib.suppress(ValueError, AttributeError):
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
