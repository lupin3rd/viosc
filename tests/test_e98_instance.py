"""One viOSC per machine (e98s08).

The support case this closes: a second launch lost the HTTP port, kept running,
opened a window with a pairing code, and the operator typed that code against the
instance that DID own the port — "pairing failed" with no visible cause.

An ``flock`` is used instead of a PID file: the kernel releases it when the
process dies, so a crash or a `kill -9` never leaves a stale lock behind.
"""

import os

import pytest

import instancelock


def test_a_second_instance_cannot_acquire_the_same_lock(tmp_path):
    path = instancelock.lock_path(str(tmp_path / "config.json"))
    first = instancelock.InstanceLock(path)
    second = instancelock.InstanceLock(path)
    try:
        assert first.acquire() is True
        assert second.acquire() is False, "a second daemon must not start"
        assert second.holder() == str(os.getpid()), "the holder is readable"
    finally:
        first.release()


def test_the_lock_is_released_for_the_next_launch(tmp_path):
    path = instancelock.lock_path(str(tmp_path / "config.json"))
    first = instancelock.InstanceLock(path)
    assert first.acquire() is True
    first.release()

    again = instancelock.InstanceLock(path)
    try:
        assert again.acquire() is True, "closing viOSC frees the machine"
    finally:
        again.release()


def test_a_stale_file_left_by_a_crash_does_not_block(tmp_path):
    """A file on disk is not a holder: only the flock is (no stale-lock cleanup)."""
    path = instancelock.lock_path(str(tmp_path / "config.json"))
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("999999\n")

    lock = instancelock.InstanceLock(path)
    try:
        assert lock.acquire() is True, "a leftover PID file must not lock the app out"
        assert lock.holder() == str(os.getpid()), "and the file is rewritten with the live pid"
    finally:
        lock.release()


def test_the_lock_lives_beside_the_config(tmp_path):
    config_file = str(tmp_path / "nested" / "config.json")
    assert instancelock.lock_path(config_file) == str(tmp_path / "nested" / "viosc.lock")


def test_the_lock_directory_is_created(tmp_path):
    path = str(tmp_path / "does" / "not" / "exist" / "viosc.lock")
    lock = instancelock.InstanceLock(path)
    try:
        assert lock.acquire() is True
        assert os.path.isfile(path)
    finally:
        lock.release()


def test_releasing_twice_is_harmless(tmp_path):
    lock = instancelock.InstanceLock(instancelock.lock_path(str(tmp_path / "c.json")))
    assert lock.acquire() is True
    lock.release()
    lock.release()  # no exception, no double unlock


@pytest.mark.parametrize("mode", ["headless", "gui"])
def test_main_refuses_a_second_instance(monkeypatch, tmp_path, mode):
    """`main()` exits 2 (and shows the reason) when the lock is taken."""
    import sys

    import viosc

    path = instancelock.lock_path(str(tmp_path / "config.json"))
    held = instancelock.InstanceLock(path)
    assert held.acquire() is True
    monkeypatch.setattr(viosc.instancelock, "lock_path", lambda _cfg: path)
    argv = ["viosc.py", "--headless", "--config", str(tmp_path / "config.json")]
    if mode == "gui":
        argv.remove("--headless")
    monkeypatch.setattr(sys, "argv", argv)
    shown: list[str] = []
    monkeypatch.setattr(viosc, "_report_gui_fatal", lambda text=None: shown.append(text or ""))
    try:
        with pytest.raises(SystemExit) as exit_info:
            viosc.main()
    finally:
        held.release()
    assert exit_info.value.code == 2
    if mode == "gui":
        assert shown and "already running" in shown[0]
