"""One viOSC per machine (e98s08) — an flock on a file next to the config.

Two instances fight for the same OSC and HTTP ports; the loser still opens a
window with a pairing code that nobody can use, which is exactly the support case
this prevents. An ``flock`` is used instead of a PID file because the kernel drops
it when the process dies: a crash leaves nothing stale to clean up by hand.
"""

import fcntl
import os
from typing import Any

LOCK_FILENAME = "viosc.lock"


def lock_path(config_file: str) -> str:
    """The lock lives beside the config file (same XDG directory)."""
    return os.path.join(os.path.dirname(os.path.abspath(config_file)), LOCK_FILENAME)


class InstanceLock:
    """Exclusive, process-wide: acquired once at boot, released at exit."""

    def __init__(self, path: str) -> None:
        self.path = str(path)
        self._handle: Any = None

    def acquire(self) -> bool:
        """True when this process now owns the lock; False when another one does."""
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        # The handle must OUTLIVE this call: the flock is bound to the open file.
        handle = open(self.path, "a+", encoding="utf-8")  # noqa: SIM115
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        self._handle = handle
        return True

    def holder(self) -> str:
        """The PID written by the owner (empty when the file says nothing)."""
        try:
            with open(self.path, encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            return ""

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()
