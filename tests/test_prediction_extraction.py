import json
from datetime import datetime, timezone

from app.bot.prediction_page import (
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


def test_valid_prediction_rejects_fragments() -> None:
    # a full prediction passes
    assert _is_valid_prediction(_OPEN)
    # a fragment with outcomes but no id/title/etc. (the KeyError: 'id' case) is skipped
    assert not _is_valid_prediction({"outcomes": [{"id": "x"}]})
    # an outcome missing a field is also rejected (would KeyError in the build)
    incomplete_outcome = {**_OPEN, "outcomes": [{"id": "o", "label": "A"}]}
    assert not _is_valid_prediction(incomplete_outcome)
