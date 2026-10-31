import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from loguru import logger

from app.bot.prediction_page import (
    PredictionPageReader,
    _extract_prediction_objects,
    _is_bettable,
    _is_valid_prediction,
)

# Shape captured live from forg1.com/predicciones on 2026-07-02: a
# self.__next_f.push([1, "<JS-string-escaped JSON>"]) script tag embedding
# one resolved and one open prediction.
_RESOLVED = {
    "id": "404ef64a-3a2b-46d2-b71d-c934dd28cf81",
    "title": "Quien gana el mapa?",
    "status": "resolved",
    "min_bet": 500,
    "max_bet": 5000,
    "locks_at": "2026-07-02T16:15:53.049+00:00",
    "resolved_at": "2026-07-02T16:40:51.571323+00:00",
    "winning_outcome_id": "345898b1-2846-4802-ab52-5af493dc74a2",
    "outcomes": [
        {"id": "o1", "label": "B8", "position": 1, "total_points": 2986118, "total_bettors": 644},
        {
            "id": "o2",
            "label": "Nemesis",
            "position": 2,
            "total_points": 1234777,
            "total_bettors": 295,
        },
    ],
}
_OPEN = {
    "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    "title": "Quien gana la ronda?",
    "status": "open",
    "min_bet": 500,
    "max_bet": 5000,
    "locks_at": "2026-07-02T18:00:00.000+00:00",
    "outcomes": [
        {"id": "o3", "label": "Furia", "position": 1, "total_points": 900, "total_bettors": 9},
        {"id": "o4", "label": "MIBR", "position": 2, "total_points": 100, "total_bettors": 1},
    ],
}


def _fake_page_html(payload: dict) -> str:
    # forg1's server sends compact JSON (no spaces) - match that, since the
    # extractor's outcomes-key regex tolerates but doesn't require whitespace.
    inner = json.dumps({"recentPredictions": [payload]}, separators=(",", ":"))
    js_string_body = json.dumps(f"29:[$,$L2d,null,{inner}]")[
        1:-1
    ]  # escape as JS string literal body
    return f'<script>self.__next_f.push([1,"{js_string_body}"])</script>'


def test_extracts_resolved_and_open_predictions() -> None:
    html = _fake_page_html(_RESOLVED) + _fake_page_html(_OPEN)
    objects = _extract_prediction_objects(html)
    ids = {o["id"] for o in objects}
    assert ids == {_RESOLVED["id"], _OPEN["id"]}


def test_open_prediction_has_no_resolved_fields() -> None:
    html = _fake_page_html(_OPEN)
    [obj] = _extract_prediction_objects(html)
    assert obj["status"] == "open"
    assert "resolved_at" not in obj
    assert len(obj["outcomes"]) == 2


def test_bettable_only_while_open_and_unlocked() -> None:
    now = datetime(2026, 7, 4, 19, 45, tzinfo=timezone.utc)
    # not resolved, but its lock time has passed -> not bettable (the bug)
    assert not _is_bettable({"status": "open", "locks_at": "2026-07-04T19:15:00Z"}, now)
    # resolved is never bettable
    assert not _is_bettable({"status": "resolved", "locks_at": "2026-07-04T20:15:00Z"}, now)
    # open and still before its lock -> bettable
    assert _is_bettable({"status": "open", "locks_at": "2026-07-04T20:15:00.000+00:00"}, now)
    # missing/garbage timestamp falls back to the status check
    assert _is_bettable({"status": "open"}, now)


def test_truncated_chunk_does_not_loop_forever() -> None:
    # A chunk that starts mid-object: its enclosing "{" went out in an earlier
    # push, so the brace scan finds no wrapper and the forward scan closes a
    # SIBLING object before the "outcomes" key it started from. Advancing the
    # cursor to that closing brace rewinds it, and the same match is re-found
    # forever — a silent hang of the whole poll loop. It must simply return.
    body = json.dumps('2a:[{"id":"frag"},"outcomes":[{"id":"o"}]]')[1:-1]
    html = f'<script>self.__next_f.push([1,"{body}"])</script>'
    assert _extract_prediction_objects(html) == []


def test_valid_prediction_rejects_fragments() -> None:
    # a full prediction passes
    assert _is_valid_prediction(_OPEN)
    # a fragment with outcomes but no id/title/etc. (the KeyError: 'id' case) is skipped
    assert not _is_valid_prediction({"outcomes": [{"id": "x"}]})
    # an outcome missing a field is also rejected (would KeyError in the build)
    incomplete_outcome = {**_OPEN, "outcomes": [{"id": "o", "label": "A"}]}
    assert not _is_valid_prediction(incomplete_outcome)


class _FakeResponse:
    def __init__(self, html: str) -> None:
        self.status = 200
        self.headers: dict[str, str] = {}
        self._html = html

    async def text(self) -> str:
        return self._html


def _reader_serving(html: str) -> PredictionPageReader:
    async def get(_url: str) -> _FakeResponse:
        return _FakeResponse(html)

    page = SimpleNamespace(request=SimpleNamespace(get=get))
    reader = PredictionPageReader(page, "https://www.forg1.com/predicciones")  # type: ignore[arg-type]

    async def nothing() -> None:
        return None

    async def balance() -> int:
        return 100_000

    reader._ensure_live_page = nothing  # type: ignore[method-assign]
    reader.read_balance = balance  # type: ignore[method-assign]
    return reader


def _future_open(**overrides: Any) -> dict[str, Any]:
    return {**_OPEN, "locks_at": "2099-01-01T00:00:00+00:00", **overrides}


def test_object_that_does_not_fit_the_model_is_skipped_not_fatal() -> None:
    # 2026-10-31: every read died on "1 validation error for Prediction", even
    # with the stream offline. An open object that does not fit the model must
    # be skipped, and the real market behind it still read.
    junk = _future_open(id="junk", title="Jugar", max_bet=None)
    market = _future_open()
    reader = _reader_serving(_fake_page_html(junk) + _fake_page_html(market))

    prediction = asyncio.run(reader.read_active_prediction())

    assert prediction is not None
    assert prediction.id == _OPEN["id"]
    assert prediction.available_points == 100_000


def test_only_unreadable_objects_means_no_market() -> None:
    junk = _future_open(id="junk", locks_at=None)
    reader = _reader_serving(_fake_page_html(junk))
    assert asyncio.run(reader.read_active_prediction()) is None


def test_unreadable_object_is_reported_once_with_the_field_name() -> None:
    # short_error() keeps only "1 validation error for Prediction"; the skip
    # warning has to name the field, or the next format change is a guess.
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="WARNING")
    try:
        junk = _future_open(id="junk", max_bet=None)
        reader = _reader_serving(_fake_page_html(junk))
        for _ in range(3):
            asyncio.run(reader.read_active_prediction())
    finally:
        logger.remove(sink)

    skips = [line for line in lines if "Skipping a payload object" in line]
    assert len(skips) == 1
    assert "max_bet" in skips[0]


def test_every_open_market_is_read() -> None:
    # Several markets can be open at once since 2026-10-31.
    second = _future_open(id="second", title="Quien gana la serie?")
    reader = _reader_serving(_fake_page_html(_future_open()) + _fake_page_html(second))

    markets = asyncio.run(reader.read_open_predictions())

    assert [m.id for m in markets] == [_OPEN["id"], "second"]
    assert reader._open_market_count == 2
    found = asyncio.run(reader.read_active_prediction(prediction_id="second"))
    assert found is not None and found.title == "Quien gana la serie?"
    assert asyncio.run(reader.read_active_prediction(prediction_id="gone")) is None
