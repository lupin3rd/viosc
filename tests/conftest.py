"""Autouse console listener for daemon tests (console-off mode, e01s06).

Since e01s06 the daemon no longer subscribes the console listener at import —
a GUI-mode app does not print to the terminal. The legacy capsys tests assert
the daemon's console text, so this fixture subscribes the console listener on
the shared bus for the duration of each test.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from logbus import bus as log_bus
from logbus import console_listener


@pytest.fixture(autouse=True)
def daemon_console():
    log_bus.subscribe(console_listener)
    yield
    log_bus.unsubscribe(console_listener)


@pytest.fixture(autouse=True)
def neutral_pairing_gate():
    """Keep the pairing gate neutral between tests (e42s01).

    The gate is module state: a test that boots the daemon with pairing enabled
    would leak the armed gate into every later test, silently suppressing the
    state broadcast and the OSC forward those tests describe. Neutral =
    disabled, which is the import default (the daemon always boots before it
    starts serving), so a test sees the pre-e42 behaviour unless it installs its
    own gate or explicitly boots a paired configuration.
    """
    import pairing
    import viosc

    def neutralize():
        viosc.PAIRING_GATE = pairing.PairingGate(False, "", viosc.PEER_REGISTRY, 300)

    neutralize()
    yield
    neutralize()
