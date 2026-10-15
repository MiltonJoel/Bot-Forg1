"""The SingletonLock guard: a lock from another machine must not wedge the launch."""

import os
import socket
import subprocess

import pytest

from app.bot import browser
from app.bot.browser import ProfileInUse, _assert_profile_not_in_use

SINGLETONS = ("SingletonLock", "SingletonCookie", "SingletonSocket")


def _profile(tmp_path, lock_target):
    for name in ("SingletonCookie", "SingletonSocket"):
        (tmp_path / name).symlink_to(f"/tmp/{name}")
    (tmp_path / "SingletonLock").symlink_to(lock_target)
    return tmp_path


def test_lock_from_another_machine_is_cleared(tmp_path, monkeypatch):
    """The exact production failure: the profile was rsynced from the dev box to the
    VPS with its lock, so Chrome kept exiting 21 — it cannot probe a pid on another
    host. Pretend to *be* the VPS, or this passes via the dead-pid branch instead."""
    monkeypatch.setattr(socket, "gethostname", lambda: "racknerd-3780d57")
    profile = _profile(tmp_path, f"milton-desktop-{os.getpid()}")

    _assert_profile_not_in_use(profile)

    for name in SINGLETONS:
        assert not (profile / name).is_symlink(), name


def test_lock_from_another_machine_is_cleared_even_if_the_pid_exists_here(tmp_path):
    """The pid alone is not evidence: an unrelated local process can reuse it."""
    profile = _profile(tmp_path, f"some-other-host-{os.getpid()}")

    _assert_profile_not_in_use(profile)

    assert not (profile / "SingletonLock").is_symlink()


def test_stale_local_lock_is_cleared(tmp_path):
    dead = 999_999_999  # far above /proc/sys/kernel/pid_max
    profile = _profile(tmp_path, f"{socket.gethostname()}-{dead}")

    _assert_profile_not_in_use(profile)

    assert not (profile / "SingletonLock").is_symlink()


def _fake_cmdline(monkeypatch, text):
    monkeypatch.setattr(browser, "_cmdline", lambda pid: text)


def test_live_local_chrome_still_refuses(tmp_path, monkeypatch):
    """The case the guard exists for: two Chromes on one profile corrupts it."""
    profile = _profile(tmp_path, f"{socket.gethostname()}-{os.getpid()}")
    _fake_cmdline(monkeypatch, f"/opt/google/chrome/chrome --user-data-dir={profile}")

    with pytest.raises(ProfileInUse, match="already running"):
        _assert_profile_not_in_use(profile)

    assert (profile / "SingletonLock").is_symlink(), "must not delete a live lock"


def test_live_pid_that_is_not_chrome_on_this_profile_is_cleared(tmp_path, monkeypatch):
    """A recycled pid (or a zombie, whose cmdline is empty) must not wedge the launch."""
    profile = _profile(tmp_path, f"{socket.gethostname()}-{os.getpid()}")
    _fake_cmdline(monkeypatch, "/usr/bin/some-unrelated-daemon")

    _assert_profile_not_in_use(profile)

    assert not (profile / "SingletonLock").is_symlink()


def test_a_live_sibling_bot_still_refuses(tmp_path, monkeypatch):
    """Automation flags alone are not permission to kill: the systemd-managed bot
    runs a Chrome that looks exactly like ours. Its Playwright driver is alive."""
    profile = _profile(tmp_path, f"{socket.gethostname()}-{os.getpid()}")
    _fake_cmdline(monkeypatch, f"chrome --remote-debugging-pipe --user-data-dir={profile}")
    monkeypatch.setattr(browser, "_launcher_is_alive", lambda pid: True)

    with pytest.raises(ProfileInUse, match="Another instance"):
        _assert_profile_not_in_use(profile)

    assert (profile / "SingletonLock").is_symlink()


def test_our_own_orphaned_chrome_is_killed(tmp_path, monkeypatch):
    """The 2026-08-19 wedge: a Chrome we launched outlived its session and kept the
    lock, so every relaunch refused — 170 of them. Nobody is there to close a window
    the bot itself opened, so kill it."""
    orphan = subprocess.Popen(["sleep", "60"])
    profile = _profile(tmp_path, f"{socket.gethostname()}-{orphan.pid}")
    _fake_cmdline(monkeypatch, f"chrome --enable-automation --user-data-dir={profile}")
    assert not browser._launcher_is_alive(orphan.pid), "parent is pytest, not a playwright driver"
    try:
        _assert_profile_not_in_use(profile)

        assert orphan.poll() is not None, "the orphan must be dead"
        assert not (profile / "SingletonLock").is_symlink()
    finally:
        orphan.kill()
        orphan.wait()


def test_hostnames_containing_dashes_are_parsed_correctly(tmp_path):
    """`racknerd-3780d57-123` splits at the LAST dash, not the first."""
    profile = _profile(tmp_path, "racknerd-3780d57-999999999")

    _assert_profile_not_in_use(profile)

    assert not (profile / "SingletonLock").is_symlink()


def test_no_lock_is_a_no_op(tmp_path):
    _assert_profile_not_in_use(tmp_path)
