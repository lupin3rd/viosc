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
    assert viosc_gui._pretty_name("listen_port") == "listen port"
    assert viosc_gui._pretty_name("from_vimix_port") == "from vimix port"


def test_format_idle_renders_durations():
    assert viosc_gui._format_idle(None) == "no data from Vimix yet"
    assert viosc_gui._format_idle(5) == "5s ago"
    assert viosc_gui._format_idle(65) == "1m 5s ago"
    assert viosc_gui._format_idle(0) == "0s ago"


def test_render_snapshot_includes_values_and_sources():
    values = {"listen_port": 6666, "ffmpeg_path": "ffmpeg", "preview_port": 8686}
    sources = {
        "listen_port": "default",
        "ffmpeg_path": "default",
        "preview_port": "default",
    }
    text = viosc_gui.render_snapshot(values, sources)
    assert "listen_port = 6666" in text
    assert "ffmpeg_path = ffmpeg" in text
    assert "default" in text


def test_dashboard_exposes_run_gui_and_window():
    assert callable(viosc_gui.run_gui)
    assert hasattr(viosc_gui, "VioscWindow")
