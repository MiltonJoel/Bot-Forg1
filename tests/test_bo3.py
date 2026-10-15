"""The bo3.gg client. Network is faked — these pin the request shape and the
parsing, which is where this breaks silently.
"""

import json
from typing import Any

import pytest

from app.services import bo3
from app.utils.http import CloudflareBlocked


class _Response:
    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self._body = json.dumps(payload).encode()
        self.status = status
        self.headers = {"server": "cloudflare", "content-type": "application/json"}

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: object) -> None:
        return None


def _page(results: list[dict[str, Any]], count: int | None = None) -> dict[str, Any]:
    return {"total": {"count": count if count is not None else len(results)}, "results": results}


def _game(**overrides: Any) -> dict[str, Any]:
    game = {
        "match_id": 1,
        "number": 1,
        "map_name": "de_inferno",
        "status": "finished",
        "begin_at": "2026-08-27T09:00:00.000+00:00",
        "winner_clan_name": "Spirit",
        "loser_clan_name": "G2",
        "winner_clan_score": 13,
        "loser_clan_score": 7,
    }
    game.update(overrides)
    return game


# --- trim ---------------------------------------------------------------------


def test_trim_keeps_only_what_the_model_reads() -> None:
    assert bo3.trim(_game()) == {
        "at": "2026-08-27",
        "map": "de_inferno",
        "w": "Spirit",
        "l": "G2",
        "ws": 13,
        "ls": 7,
        "n": 1,
        "m": 1,
    }


@pytest.mark.parametrize("missing", ["winner_clan_name", "loser_clan_name", "map_name", "begin_at"])
def test_trim_rejects_a_row_it_cannot_use(missing: str) -> None:
    """A live map has no winner yet and an unnamed map is not a data point.
    Both must be dropped, not stored as empty strings."""
    assert bo3.trim(_game(**{missing: None})) is None


# --- fetching -----------------------------------------------------------------


def test_only_finished_maps_with_a_winner_are_yielded(monkeypatch: Any) -> None:
    """`status=current` rows carry a map_name but no result. They are what makes
    live map resolution work and they must never reach the history."""
    payload = _page([_game(), _game(status="current", winner_clan_name=None)])
    monkeypatch.setattr(bo3, "_get", lambda *a, **k: payload)

    from datetime import datetime

    games = list(bo3.finished_games(datetime(2026, 8, 1)))
    assert len(games) == 1 and games[0]["winner_clan_name"] == "Spirit"


def test_pagination_stops_on_an_empty_page(monkeypatch: Any) -> None:
    """`total.count` moves while we walk (matches finish mid-scrape), so an
    empty page has to end the loop or this spins forever."""
    pages = [_page([_game()], count=999), _page([], count=999)]
    monkeypatch.setattr(bo3, "_get", lambda *a, **k: pages.pop(0))

    from datetime import datetime

    assert len(list(bo3.finished_games(datetime(2026, 8, 1)))) == 1


def test_a_cloudflare_challenge_is_raised_not_parsed(monkeypatch: Any) -> None:
    """A challenge page parses to zero maps, which is indistinguishable from a
    quiet day. It has to raise instead, the way the forg1 reader does."""
    challenge = _Response({}, status=403)
    challenge._body = (
        b"<html><title>Just a moment...</title>attention required! | cloudflare</html>"
    )
    monkeypatch.setattr(bo3.http_ipv4, "urlopen", lambda *a, **k: challenge)

    with pytest.raises(CloudflareBlocked):
        bo3._get("games", {})


def test_a_healthy_response_served_through_cloudflare_is_not_a_block(monkeypatch: Any) -> None:
    """bo3.gg sits behind Cloudflare like forg1 does, so `server: cloudflare`
    on a 200 must never trip the detector."""
    monkeypatch.setattr(bo3.http_ipv4, "urlopen", lambda *a, **k: _Response(_page([_game()])))
    assert bo3._get("games", {})["results"][0]["map_name"] == "de_inferno"
