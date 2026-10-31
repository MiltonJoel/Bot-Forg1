"""Polymarket's map price: the lookup must only ever return a certain match,
and the strategy must use it ahead of the form model and fall back otherwise."""

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from app.services import polymarket
from tests.test_form_strategy import UNDERDOG, _filtered, _Fixed, _prediction

NOW = datetime(2026, 10, 19, 15, 0, tzinfo=timezone.utc)


def _event(
    teams: tuple[tuple[str, str], tuple[str, str]],
    start: str = "2026-10-19T14:00:00Z",
    outcomes: tuple[str, str] | None = None,
    bid: float = 0.30,
    ask: float = 0.34,
) -> dict[str, Any]:
    outs = list(outcomes or (teams[0][0], teams[1][0]))
    market = {
        "question": f"Counter-Strike: {outs[0]} vs {outs[1]} - Map 2 Winner",
        "outcomes": json.dumps(outs),
        "clobTokenIds": json.dumps(["tok0", "tok1"]),
        "bestBid": bid,
        "bestAsk": ask,
    }
    return {
        "startTime": start,
        "teams": [{"name": n, "abbreviation": a} for n, a in teams],
        "markets": [market, dict(market, question=market["question"].replace("Map 2", "Map 1"))],
    }


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"events": [], "history": []}

    def fake_get(url: str, params: dict[str, Any]) -> Any:
        if url == polymarket.GAMMA:
            return state["events"] if params["offset"] == 0 else []
        return {"history": state["history"]}

    monkeypatch.setattr(polymarket, "_get", fake_get)
    return state


NAVI_PAIN = (("Natus Vincere", "navi"), ("paiN Gaming", "pain"))


def test_started_series_uses_last_price_before_start_oriented_by_team_list(api: dict) -> None:
    # Outcome 0 is PAIN here, and forg1 writes NAVI, which no outcome name says.
    api["events"] = [_event(NAVI_PAIN, outcomes=("paiN Gaming", "Natus Vincere"))]
    start = int(datetime(2026, 10, 19, 14, tzinfo=timezone.utc).timestamp())
    api["history"] = [{"t": start - 600, "p": 0.25}, {"t": start + 60, "p": 0.9}]
    got = polymarket.map_probability(("NAVI", "PAIN"), 2, NOW)
    assert got == {"PAIN": 0.25, "NAVI": 0.75}


def test_before_start_uses_midpoint_and_rejects_a_wide_spread(api: dict) -> None:
    api["events"] = [_event(NAVI_PAIN, start="2026-10-19T16:00:00Z")]
    got = polymarket.map_probability(("NAVI", "PAIN"), 2, NOW)
    assert got is not None and got["NAVI"] == pytest.approx(0.32)
    api["events"] = [_event(NAVI_PAIN, start="2026-10-19T16:00:00Z", bid=0.1, ask=0.5)]
    assert polymarket.map_probability(("NAVI", "PAIN"), 2, NOW) is None


@pytest.mark.parametrize(
    "events, labels, number",
    [
        ([_event(NAVI_PAIN), _event(NAVI_PAIN, start="2026-10-19T09:00:00Z")], ("NAVI", "PAIN"), 2),
        ([_event((("Spirit Academy", "spra"), ("BIG", "big")))], ("Spirit", "BIG"), 2),
        ([_event(NAVI_PAIN, start="2026-10-18T01:00:00Z")], ("NAVI", "PAIN"), 2),
        ([_event(NAVI_PAIN)], ("NAVI", "PAIN"), 3),
        ([_event(NAVI_PAIN)], ("NAVI", "Furia"), 2),
    ],
    ids=["same-pair-twice", "fuzzy-name", "outside-window", "no-such-map", "one-team-only"],
)
def test_anything_uncertain_is_none(api: dict, events: list, labels: tuple, number: int) -> None:
    api["events"] = events
    api["history"] = [{"t": 0, "p": 0.5}]
    assert polymarket.map_probability(labels, number, NOW) is None


def test_polymarket_overrides_the_form_model_both_ways() -> None:
    model = _Fixed({"Alpha": 0.81, "Beta": 0.19})  # alone it would skip Beta
    backed = _prediction(*UNDERDOG).model_copy(update={"polymarket": {"Alpha": 0.7, "Beta": 0.3}})
    decision = _filtered(model).decide(backed)
    assert decision.should_bet and decision.side == "Beta"
    assert "Polymarket" in decision.reason

    model = _Fixed({"Alpha": 0.70, "Beta": 0.30})  # alone it would bet Beta
    vetoed = _prediction(*UNDERDOG).model_copy(update={"polymarket": {"Alpha": 0.82, "Beta": 0.18}})
    decision = _filtered(model).decide(vetoed)
    assert not decision.should_bet and "Polymarket does not back Beta" in decision.reason


def test_polymarket_prices_teams_the_form_model_does_not_know() -> None:
    known = _prediction(*UNDERDOG).model_copy(update={"polymarket": {"Alpha": 0.7, "Beta": 0.3}})
    assert _filtered(_Fixed({})).decide(known).should_bet


def test_no_polymarket_match_prices_exactly_as_before() -> None:
    model = _Fixed({"Alpha": 0.74, "Beta": 0.26})
    plain = _filtered(model).decide(_prediction(*UNDERDOG))
    other = _prediction(*UNDERDOG).model_copy(update={"polymarket": {"Gamma": 0.9, "Delta": 0.1}})
    assert _filtered(model).decide(other) == plain
