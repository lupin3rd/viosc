"""Headless tests for the viOSC dashboard (e58s02) — pure helpers only.

tkinter cannot be driven headless (no display), so the widget wiring is verified
on the rig (e58s05). What is testable is the pure formatting logic the dashboard
uses: the idle-time rendering, the snake_case labels, and the read-only config
snapshot that shows every value (including hidden ones) with its source marker.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import viosc_gui


def test_pretty_name_spaces_snake_case():
    assert viosc_gui._pretty_name("http_port") == "http port"
    assert viosc_gui._pretty_name("from_vimix_port") == "from vimix port"


def test_format_idle_renders_durations():
    assert viosc_gui._format_idle(None) == "no data from Vimix yet"
    assert viosc_gui._format_idle(5) == "5s ago"
    assert viosc_gui._format_idle(65) == "1m 5s ago"
    assert viosc_gui._format_idle(0) == "0s ago"


def test_render_snapshot_includes_values_and_sources():
    values = {"from_vimix_port": 7001, "ffmpeg_path": "ffmpeg", "http_port": 8686}
    sources = {
        "from_vimix_port": "default",
        "ffmpeg_path": "default",
        "http_port": "default",
    }
    text = viosc_gui.render_snapshot(values, sources)
    assert "Vimix in: 7001" in text
    assert "HTTP: 8686" in text
    assert "default" in text
    # e98s06: six labelled rows, not a 24-key dump — a key outside the rows and
    # outside the effective values is not rendered, and neither is the noise.
    assert "ffmpeg_path" not in text
    assert "Everything else is configured from VJmix" in text


def test_dashboard_exposes_run_gui_and_window():
    assert callable(viosc_gui.run_gui)
    assert hasattr(viosc_gui, "VioscWindow")


def test_render_snapshot_shows_the_link_state():
    values = {"http_port": 8686, "from_vimix_port": 7001}
    text = viosc_gui.render_snapshot(values, {}, link="cannot bind 0.0.0.0:8686")
    assert "Link: cannot bind 0.0.0.0:8686" in text


def test_closing_the_window_ends_the_process():
    """Operator decision 2026-11-08: the window IS the app — close = quit."""
    actions = []

    class FakeRoot:
        def withdraw(self):
            actions.append("withdraw")

        def destroy(self):
            actions.append("destroy")

    window = object.__new__(viosc_gui.VioscWindow)
    window.root = FakeRoot()
    window._bus_listener = None

    window.on_close()

    assert actions == ["destroy"], "no hidden background router, no second instance"
    assert not hasattr(window, "quit_app") or not callable(getattr(window, "quit_app", None))


def test_the_pairing_code_is_hidden_when_the_http_surface_is_down():
    """e98s04: a second instance has a code but no surface — do not show it."""
    text, colour = viosc_gui.pairing_banner("1234", "up")
    assert "1234" in text and colour == "#0050a0"

    text, colour = viosc_gui.pairing_banner("1234", "cannot bind 0.0.0.0:8686 ([Errno 98])")
    assert "1234" not in text, "a code nobody can pair with must not be displayed"
    assert "pairing is impossible" in text
    assert colour == "#c00000"


def test_snapshot_rows_are_split_in_two_columns():
    rows = [
        "HTTP: 8686",
        "Vimix in: 7001",
        "Vimix out: 7000",
        "Roster: 2000",
        "Thumbs: 3",
        "Log: 1",
        "Link: up",
    ]

    columns = viosc_gui.split_columns(rows)

    assert len(columns) == viosc_gui.FORM_COLUMNS
    assert columns[0] + columns[1] == rows
    assert max(len(column) for column in columns) <= 4
    assert viosc_gui.split_columns([]) == [[], []]


def test_the_apply_button_shares_the_http_row():
    cells, button = viosc_gui.bind_cells(2)

    assert cells == [(0, 0), (0, 2)]
    assert button == (0, 4), "Apply & restart sits on the http ip/port line"


def test_render_snapshot_is_the_rows_plus_the_note():
    values = {"http_port": 8686}

    rows = viosc_gui.snapshot_rows(values, {})

    assert viosc_gui.render_snapshot(values, {}).splitlines() == [*rows, viosc_gui.SNAPSHOT_NOTE]


def test_log_toggle_says_which_way_the_next_click_goes():
    closed = viosc_gui.log_toggle_text(False)
    opened = viosc_gui.log_toggle_text(True)

    assert closed.startswith(viosc_gui.LOG_CLOSED_MARK) and "click to open" in closed
    assert opened.startswith(viosc_gui.LOG_OPEN_MARK) and "click to close" in opened
    assert viosc_gui.LOG_TITLE in closed


def test_the_log_starts_closed_and_a_click_opens_and_closes_it():
    class FakeWidget:
        def __init__(self):
            self.packed = None

        def pack(self, **kwargs):
            self.packed = kwargs

        def pack_forget(self):
            self.packed = None

        def pack_configure(self, **kwargs):
            self.packed = kwargs

        def configure(self, **kwargs):
            self.packed = kwargs

        def see(self, *_args):
            pass

    class FakeRoot:
        def __init__(self):
            self.geometry_value = "680x560"
            self.idle = 0

        def winfo_width(self):
            return 680

        def winfo_height(self):
            return 560

        def winfo_reqheight(self):
            return 330

        def update_idletasks(self):
            self.idle += 1

        def geometry(self, value):
            self.geometry_value = value

    window = object.__new__(viosc_gui.VioscWindow)
    window._log_open = False
    window._open_height = 0
    window.root = FakeRoot()
    window.log_section = FakeWidget()
    window.log_frame = FakeWidget()
    window.log_text = FakeWidget()
    window.log_toggle = FakeWidget()
    assert window.log_frame.packed is None, "the pane must start closed"

    window.toggle_log()
    assert window._log_open is True
    assert window.log_frame.packed is not None
    assert window.root.geometry_value == "680x330", "opening grows to the open height"

    window.toggle_log()
    assert window._log_open is False
    assert window.log_frame.packed is None
    assert window.root.geometry_value == "680x330", "closing shrinks to the content"


def test_the_window_height_follows_the_log_toggle():
    assert viosc_gui.fitted_height(requested=330, remembered=0, is_open=False) == 330
    assert viosc_gui.fitted_height(requested=330, remembered=0, is_open=True) == 330
    assert viosc_gui.fitted_height(requested=330, remembered=600, is_open=True) == 600
    assert viosc_gui.fitted_height(requested=330, remembered=600, is_open=False) == 330


def test_the_error_line_costs_a_row_only_while_it_has_text():
    """A packed-but-empty label is dead space (operator, 2026-11-08)."""

    class FakeLabel:
        def __init__(self):
            self.manager = ""

        def winfo_manager(self):
            return self.manager

        def pack(self, **kwargs):
            self.manager = "pack"

        def pack_forget(self):
            self.manager = ""

    class FakeVar:
        def __init__(self):
            self.value = ""

        def set(self, value):
            self.value = value

    window = object.__new__(viosc_gui.VioscWindow)
    window.error_label = FakeLabel()
    window.error_var = FakeVar()
    window._fit_window = lambda: None

    window._show_error("")
    assert window.error_label.manager == ""

    window._show_error("http_port: not a port")
    assert window.error_label.manager == "pack"
    assert window.error_var.value == "http_port: not a port"

    window._show_error("")
    assert window.error_label.manager == ""
