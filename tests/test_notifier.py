"""api.telegram.org answers in 10-20s from this network, so a send must survive
a slow/transient failure — but must NOT retry a permanent rejection."""

import asyncio
import io
import json
import urllib.error
from typing import Any

import pytest

from app.services import notifier as notifier_module
from app.services.notifier import TelegramNotifier


class _Response(io.BytesIO):
    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def _fake_urlopen(monkeypatch: Any, outcomes: list[Any]) -> list[Any]:
    """Each call pops one outcome: an exception to raise, or a dict to return."""
    calls: list[Any] = []

    def urlopen(request: Any, timeout: float = 0) -> _Response:
        calls.append(getattr(request, "data", None))
        result = outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return _Response(json.dumps(result).encode())

    monkeypatch.setattr(notifier_module.http_ipv4, "urlopen", urlopen)
    return calls


def test_transient_failure_is_retried(monkeypatch: Any) -> None:
    # The exact failure from the logs: SSL handshake timeout.
    calls = _fake_urlopen(
        monkeypatch, [urllib.error.URLError("_ssl.c:1064: handshake timed out"), {"ok": True}]
    )
    asyncio.run(TelegramNotifier("t", "1").send("hola"))
    assert len(calls) == 2  # retried, and the message got through


def test_permanent_rejection_is_not_retried(monkeypatch: Any) -> None:
    # A bad chat_id returns 400 forever; retrying just delays the log line.
    error = urllib.error.HTTPError(
        "u", 400, "Bad Request", {}, io.BytesIO(b'{"description":"chat not found"}')
    )
    calls = _fake_urlopen(monkeypatch, [error])
    asyncio.run(TelegramNotifier("t", "1").send("hola"))
    assert len(calls) == 1


def test_send_never_raises_even_when_every_attempt_fails(monkeypatch: Any) -> None:
    # A dead Telegram link must not take the betting bot down.
    _fake_urlopen(monkeypatch, [urllib.error.URLError("down"), urllib.error.URLError("down")])
    asyncio.run(TelegramNotifier("t", "1").send("hola"))  # must not raise


def test_connect_fails_loudly_on_a_bad_token(monkeypatch: Any) -> None:
    error = urllib.error.HTTPError(
        "u", 401, "Unauthorized", {}, io.BytesIO(b'{"description":"Unauthorized"}')
    )
    _fake_urlopen(monkeypatch, [error])
    with pytest.raises(RuntimeError, match="401"):
        asyncio.run(TelegramNotifier("bad", "1").connect())


def test_timeout_is_the_configured_one(monkeypatch: Any) -> None:
    seen: list[float] = []

    def urlopen(request: Any, timeout: float = 0) -> _Response:
        seen.append(timeout)
        return _Response(b'{"ok": true}')

    monkeypatch.setattr(notifier_module.http_ipv4, "urlopen", urlopen)
    asyncio.run(TelegramNotifier("t", "1", timeout_seconds=20.0).send("hola"))
    assert seen == [20.0]  # not the old 10s, which sat below real latency
