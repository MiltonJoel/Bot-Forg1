"""The Kick viewer runs as its own plain Chrome process, not a Playwright tab.

Kick's player fetches web.kick.com/api/v1/*, and Cloudflare 403s those calls
when Chrome is driven over CDP: the page rendered, the chat failed to connect
and <video> never got a src, so the tab was open but watching nothing. Same
profile and flags in a normally-launched Chrome plays fine (2026-08-12).
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.services.kick_service import KickLiveChecker


class _Notifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> None:
        self.messages.append(text)


class _FakeProc:
    """subprocess.Popen stand-in. `alive` False means Chrome exited on its own."""

    def __init__(self, alive: bool = True) -> None:
        self.pid = 4242
        self.alive = alive
        self.terminated = False

    def poll(self) -> int | None:
        return None if self.alive else 1

    def terminate(self) -> None:
        self.terminated = True
        self.alive = False

    def wait(self, timeout: float = 0) -> int:
        return 0


def _checker(monkeypatch: Any, live: bool = True) -> tuple[KickLiveChecker, list[list[str]]]:
    spawned: list[list[str]] = []
    checker = KickLiveChecker("forg1", 0, _Notifier(), Path("/tmp/kick-viewer"), 0.0)  # type: ignore[arg-type]
    monkeypatch.setattr(KickLiveChecker, "_check_live", lambda self: live)
    monkeypatch.setattr(
        "app.services.kick_service.subprocess.Popen",
        lambda cmd, **kw: (spawned.append(cmd), _FakeProc())[1],
    )
    # Never let the stray-viewer pkill run for real in a test.
    monkeypatch.setattr("app.services.kick_service.subprocess.call", lambda cmd: 1)
    return checker, spawned


def _tick(checker: KickLiveChecker) -> None:
    async def go() -> None:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(checker.run(), timeout=0.2)

    asyncio.run(go())


def test_going_live_spawns_a_plain_chrome_on_the_viewer_profile(monkeypatch: Any) -> None:
    checker, spawned = _checker(monkeypatch)
    _tick(checker)
    assert spawned, "the live edge must launch the viewer"
    command = spawned[0]
    assert command[-1] == "https://kick.com/forg1"
    assert "--user-data-dir=/tmp/kick-viewer" in command
    # The whole point: no CDP, no automation flags. Cloudflare blocks those.
    assert not any("remote-debugging" in arg for arg in command)


def test_viewer_that_died_is_relaunched_while_still_live(monkeypatch: Any) -> None:
    # The live edge does not fire again until the stream ends, so a crashed
    # viewer would stop the watch time for the rest of the stream.
    checker, spawned = _checker(monkeypatch)
    checker._was_live = True
    checker._viewer = _FakeProc(alive=False)  # type: ignore[assignment]
    _tick(checker)
    assert spawned, "a dead viewer must be relaunched"


def test_going_offline_terminates_the_viewer(monkeypatch: Any) -> None:
    checker, _spawned = _checker(monkeypatch, live=False)
    viewer = _FakeProc()
    checker._was_live = True
    checker._viewer = viewer  # type: ignore[assignment]
    _tick(checker)
    assert viewer.terminated
    assert checker._viewer is None


def test_a_viewer_that_exits_immediately_is_not_relaunched_forever(monkeypatch: Any) -> None:
    # Chrome hands the URL to an instance that already owns the profile and
    # exits at once. Tracking that as a live viewer made the checker relaunch it
    # every tick — the 30s loop seen on the VPS 2026-08-12.
    checker, _spawned = _checker(monkeypatch)
    monkeypatch.setattr(
        "app.services.kick_service.subprocess.Popen", lambda cmd, **kw: _FakeProc(alive=False)
    )
    asyncio.run(checker._open_stream())
    assert checker._viewer is None, "a viewer that died on launch must not be tracked as running"


def test_a_live_viewer_is_left_alone(monkeypatch: Any) -> None:
    checker, spawned = _checker(monkeypatch)
    checker._was_live = True
    checker._viewer = _FakeProc()  # type: ignore[assignment]
    _tick(checker)
    assert not spawned, "a healthy viewer must not be relaunched every tick"
