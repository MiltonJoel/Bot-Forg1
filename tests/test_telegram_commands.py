"""`/reset` from Telegram relaunches the browser session. It is a remote control
on a bot that spends points, so the chat check matters as much as the trigger."""

import asyncio
from typing import Any

import pytest

from app.services.telegram_commands import TelegramCommandListener


class _Notifier:
    """Serves one batch of updates per poll, mimicking getUpdates offsets."""

    def __init__(self, batches: list[list[str]]) -> None:
        self._batches = batches
        self.sent: list[str] = []
        self.polls = 0

    async def pending_commands(self, offset: int) -> tuple[list[str], int]:
        self.polls += 1
        texts = self._batches.pop(0) if self._batches else []
        return texts, offset + len(texts)

    async def send(self, text: str) -> None:
        self.sent.append(text)


def _listen(notifier: Any, reset: asyncio.Event, seconds: float = 0.25) -> None:
    listener = TelegramCommandListener(notifier, reset, poll_seconds=0.01)

    async def go() -> None:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(listener.run(), timeout=seconds)

    asyncio.run(go())


def test_reset_command_requests_a_relaunch() -> None:
    reset = asyncio.Event()
    notifier = _Notifier([[], ["/reainiciobot"]])  # first batch primes the offset
    _listen(notifier, reset)

    assert reset.is_set()
    assert any("Reiniciando" in m for m in notifier.sent)


def test_spanish_and_bare_spellings_work() -> None:
    for word in ("reainiciobot", "/reiniciobot", "reiniciarbot", "/REAINICIOBOT"):
        reset = asyncio.Event()
        _listen(_Notifier([[], [word]]), reset)
        assert reset.is_set(), word


def test_chatter_does_not_reset_the_bot() -> None:
    reset = asyncio.Event()
    _listen(_Notifier([[], ["hola", "cuánto va?", "resetear todo mañana"]]), reset)
    assert not reset.is_set()


def test_openclaw_commands_are_ignored() -> None:
    # This token is shared with OpenClaw, which owns /reset and /restart. Those
    # must never relaunch the browser — they belong to the other bot.
    reset = asyncio.Event()
    _listen(_Notifier([[], ["/reset", "/restart", "/stop", "/status"]]), reset)
    assert not reset.is_set()


def test_backlog_from_while_the_bot_was_down_is_ignored() -> None:
    # A /reset sent hours ago must not fire the instant the bot comes back and
    # drains the queue — that would loop a bot restarted by systemd.
    reset = asyncio.Event()
    _listen(_Notifier([["/reainiciobot"]]), reset)
    assert not reset.is_set()


def test_telegram_failure_does_not_kill_the_listener() -> None:
    class _Broken:
        def __init__(self) -> None:
            self.calls = 0
            self.sent: list[str] = []

        async def pending_commands(self, offset: int) -> tuple[list[str], int]:
            self.calls += 1
            raise OSError("telegram unreachable")

        async def send(self, text: str) -> None:
            self.sent.append(text)

    notifier = _Broken()
    reset = asyncio.Event()
    _listen(notifier, reset)  # must not raise
    assert notifier.calls > 1, "keeps polling after a failure"


def test_update_command_starts_the_model_update() -> None:
    started: list[bool] = []
    reset = asyncio.Event()
    notifier = _Notifier([[], ["/actualizarmodelo"]])
    listener = TelegramCommandListener(
        notifier,  # type: ignore[arg-type]
        reset,
        poll_seconds=0.01,
        start_model_update=lambda: started.append(True) or True,
    )

    async def go() -> None:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(listener.run(), timeout=0.25)

    asyncio.run(go())
    assert started == [True]
    assert not reset.is_set()
