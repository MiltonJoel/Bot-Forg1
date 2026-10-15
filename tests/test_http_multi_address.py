"""Regression: the IPv4-first connector originally resolved the address list and
connected to infos[0] only, discarding the stdlib's walk over the remaining
records. kick.com publishes two A records; when the first blackholed on the VPS
the bot logged "urlopen error timed out" every 45s for over an hour while the
host was perfectly reachable on the second."""

import socket
from typing import Any

import pytest

from app.utils import http as http_ipv4

_BLACKHOLE = ("192.0.2.1", 443)  # TEST-NET-1: routable-looking, never answers


def _infos(*addresses: tuple[int, tuple[str, int]]) -> list[Any]:
    return [(family, socket.SOCK_STREAM, 6, "", addr) for family, addr in addresses]


def test_falls_through_to_the_second_address(monkeypatch: Any) -> None:
    attempted: list[tuple[str, int]] = []
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: _infos(
            (socket.AF_INET, _BLACKHOLE), (socket.AF_INET, ("203.0.113.7", 443))
        ),
    )

    class FakeSocket:
        def __init__(self, *a: Any) -> None:
            pass

        def settimeout(self, t: Any) -> None:
            pass

        def connect(self, addr: tuple[str, int]) -> None:
            attempted.append(addr)
            if addr == _BLACKHOLE:
                raise TimeoutError("timed out")

        def close(self) -> None:
            pass

    monkeypatch.setattr(socket, "socket", FakeSocket)
    http_ipv4._ipv4_first_create_connection(("kick.com", 443), 5)

    assert attempted == [_BLACKHOLE, ("203.0.113.7", 443)], "must try every address"


def test_ipv4_is_tried_before_ipv6(monkeypatch: Any) -> None:
    attempted: list[tuple[str, int]] = []
    # DNS commonly returns IPv6 first — that ordering is what stalled Telegram.
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: _infos(
            (socket.AF_INET6, ("2606:4700::1", 443)), (socket.AF_INET, ("162.159.1.1", 443))
        ),
    )

    class FakeSocket:
        def __init__(self, *a: Any) -> None:
            pass

        def settimeout(self, t: Any) -> None:
            pass

        def connect(self, addr: tuple[str, int]) -> None:
            attempted.append(addr)

        def close(self) -> None:
            pass

    monkeypatch.setattr(socket, "socket", FakeSocket)
    http_ipv4._ipv4_first_create_connection(("api.telegram.org", 443), 5)

    assert attempted == [("162.159.1.1", 443)], "IPv4 must be reordered to the front"


def test_ipv6_only_host_still_connects(monkeypatch: Any) -> None:
    attempted: list[tuple[str, int]] = []
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: _infos((socket.AF_INET6, ("2606:4700::1", 443)))
    )

    class FakeSocket:
        def __init__(self, *a: Any) -> None:
            pass

        def settimeout(self, t: Any) -> None:
            pass

        def connect(self, addr: tuple[str, int]) -> None:
            attempted.append(addr)

        def close(self) -> None:
            pass

    monkeypatch.setattr(socket, "socket", FakeSocket)
    http_ipv4._ipv4_first_create_connection(("v6only.example", 443), 5)

    assert attempted == [("2606:4700::1", 443)], "IPv6 kept as a fallback, not filtered out"


def test_every_address_failing_raises_the_last_error(monkeypatch: Any) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _infos((socket.AF_INET, _BLACKHOLE)))

    class FakeSocket:
        def __init__(self, *a: Any) -> None:
            pass

        def settimeout(self, t: Any) -> None:
            pass

        def connect(self, addr: tuple[str, int]) -> None:
            raise ConnectionRefusedError("nope")

        def close(self) -> None:
            pass

    monkeypatch.setattr(socket, "socket", FakeSocket)
    with pytest.raises(ConnectionRefusedError):
        http_ipv4._ipv4_first_create_connection(("dead.example", 443), 5)
