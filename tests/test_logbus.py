"""Log sink tests for viosc (e01s03).

LogBus is the single thread-safe sink every daemon message flows through:
workers only emit, listeners receive synchronously and FIFO. The console
listener keeps today's stdout behaviour (the legacy capsys tests assert its
text); the GUI story will subscribe a queue listener pumped on the main
thread. No tkinter, no daemon import.
"""

import threading

from logbus import LogBus, console_listener

# ---------------------------------------------------------------------------
# e01s03t1 — RED: subscribe / unsubscribe / ordering
# ---------------------------------------------------------------------------


def test_subscribed_listener_receives_emits_in_order():
    bus = LogBus()
    got = []
    bus.subscribe(lambda text, level: got.append((text, level)))

    bus.emit("one")
    bus.emit("two", level="error")

    assert got == [("one", "info"), ("two", "error")]


def test_unsubscribe_stops_delivery():
    bus = LogBus()
    got = []

    def listener(text, level):
        got.append(text)

    bus.subscribe(listener)
    bus.emit("before")
    bus.unsubscribe(listener)
    bus.emit("after")
    assert got == ["before"]


# ---------------------------------------------------------------------------
# e01s03t2 — RED: synchronous delivery + cross-thread safety
# ---------------------------------------------------------------------------


def test_emit_is_synchronous():
    bus = LogBus()
    seen = []
    bus.subscribe(lambda text, level: seen.append(text))

    bus.emit("now")

    assert seen == ["now"], "the listener side effect must be visible on return"


def test_cross_thread_emits_are_all_delivered():
    bus = LogBus()
    lock = threading.Lock()
    count = 0

    def listener(text, level):
        nonlocal count
        with lock:
            count += 1

    bus.subscribe(listener)
    barrier = threading.Barrier(8)
    threads = []

    def emit_many():
        barrier.wait()
        for _ in range(25):
            bus.emit("x")

    for _ in range(8):
        t = threading.Thread(target=emit_many)
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    assert count == 8 * 25, f"expected all 200 emits, got {count}"


# ---------------------------------------------------------------------------
# e01s03t3 — console listener keeps plain-print behaviour (capsys)
# ---------------------------------------------------------------------------


def test_console_listener_prints_the_emitted_text(capsys):
    bus = LogBus()
    bus.subscribe(console_listener)

    bus.emit("plain line", level="error")

    out = capsys.readouterr().out
    assert out == "plain line\n"


# ---------------------------------------------------------------------------
# e01s06 (console off) — history tail + replay for late subscribers
# ---------------------------------------------------------------------------


def test_history_tail_returns_recent_emits():
    bus = LogBus()
    for n in range(5):
        bus.emit(f"line {n}")

    assert bus.tail() == [(f"line {n}", "info") for n in range(5)]
    assert bus.tail(count=2) == [("line 3", "info"), ("line 4", "info")]


def test_history_is_bounded():
    bus = LogBus(history=3)
    for n in range(5):
        bus.emit(f"line {n}")

    assert bus.tail() == [("line 2", "info"), ("line 3", "info"), ("line 4", "info")]


def test_subscribe_with_replay_feeds_past_emits_before_future_ones():
    bus = LogBus()
    bus.emit("past one")
    bus.emit("past two")
    got = []

    bus.subscribe(lambda text, level: got.append(text), replay=True)
    bus.emit("future")

    assert got == ["past one", "past two", "future"]


def test_subscribe_without_replay_only_receives_future():
    bus = LogBus()
    bus.emit("past")
    got = []

    bus.subscribe(lambda text, level: got.append(text))
    bus.emit("future")

    assert got == ["future"]
