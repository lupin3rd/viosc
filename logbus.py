"""Thread-safe log sink for viosc (e01s03).

The daemon used to report through scattered colored ``print`` calls. Every
message now flows through a :class:`LogBus`: worker threads only emit lines,
listeners receive them synchronously and FIFO. The console listener prints
exactly what the daemon printed before (same text, same ANSI colors) so
stdout behaviour — and the legacy capsys assertions — are unchanged; the GUI
story (e01s05) subscribes a queue listener that is pumped on the main
thread. emit() delivers synchronously on the calling thread: console output
and test capture behave exactly like a plain print.

Headless by construction: no tkinter, no OSC, no daemon import.
"""

import threading
from collections import deque
from collections.abc import Callable

Listener = Callable[[str, str], None]

# Lines kept for late subscribers (GUI replay: the window must show boot
# lines emitted before it subscribed).
DEFAULT_HISTORY = 300


class LogBus:
    """FIFO delivery to subscribed listeners, safe from any thread.

    Every emit is appended to a bounded history so a listener that
    subscribes late (the GUI window, the console fallback) can replay what
    happened before it existed.
    """

    def __init__(self, history: int = DEFAULT_HISTORY) -> None:
        self._listeners: list[Listener] = []
        self._history: deque[tuple[str, str]] = deque(maxlen=history)
        self._lock = threading.Lock()

    def subscribe(self, listener: Listener, *, replay: bool = False) -> None:
        """Add a listener; with ``replay`` it first receives the history."""
        with self._lock:
            if replay:
                for item in self._history:
                    listener(*item)
            self._listeners.append(listener)

    def unsubscribe(self, listener: Listener) -> None:
        """Remove a listener (raises ValueError when not subscribed)."""
        with self._lock:
            self._listeners.remove(listener)

    def emit(self, text: str, level: str = "info") -> None:
        """Deliver ``text`` to every listener, in subscription order.

        Listeners run on the calling thread; hold the lock only to snapshot
        the list so a slow listener never blocks other emitters.
        """
        with self._lock:
            self._history.append((text, level))
            listeners = list(self._listeners)
        for listener in listeners:
            listener(text, level)

    def tail(self, count: int | None = None) -> list[tuple[str, str]]:
        """Recent ``(text, level)`` history; last ``count`` when given."""
        with self._lock:
            items = list(self._history)
        return items if count is None else items[-count:]


def console_listener(text: str, level: str = "info") -> None:
    """Default listener: print the line exactly as the legacy daemon did.

    ``level`` is ignored here (the GUI tinting story reads it instead); the
    ANSI colors live in ``text`` and pass through unchanged.
    """
    print(text)


# Shared daemon bus. viosc.py subscribes console_listener at import and
# routes every operational message through log().
bus = LogBus()


def log(text: str, level: str = "info") -> None:
    """Emit on the shared daemon bus (module-level convenience)."""
    bus.emit(text, level)
