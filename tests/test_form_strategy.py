"""Wiring MapForm into ContrarianPariMutuelStrategy.

The load-bearing test here is the first one: with STRATEGY_FORM_WEIGHT at 0 —
which is how this ships — the strategy has to price exactly as it did before
MapForm existed. Everything else is opt-in.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.models.prediction import Outcome, Prediction
from app.services.match_history import MapForm
from app.strategies.contrarian_pari_mutuel import ContrarianPariMutuelStrategy

MISSING = Path(".data/does-not-exist-for-tests.json")


def _prediction(*shares: tuple[str, int], map_name: str | None = None) -> Prediction:
    return Prediction(
        id="x",
        title="Quien gana el 2do MAPA?",
        outcomes=[
            Outcome(id=str(i), label=label, total_points=points, total_bettors=10)
            for i, (label, points) in enumerate(shares)
        ],
        min_bet=500,
        max_bet=50_000,
        locks_at=datetime.now(timezone.utc),
        available_points=20_000,
        captured_at=datetime.now(timezone.utc),
        map_name=map_name,
    )


def _form(**overrides: object) -> MapForm:
    """Alpha has beaten Beta 40 times on Mirage and lost to them 10 times on
    Nuke, so 'who is better' (Alpha, p=0.58) and 'who is better HERE' (Beta on
    Nuke, p=0.63) give opposite answers. That split is what these tests need;
    a gentler fixture nets out to a coin flip and measures nothing."""
    games = [
        {"at": f"2026-01-{d + 1:02d}", "map": "de_mirage", "w": "Alpha", "l": "Beta", "n": 1}
        for d in range(40)
    ] + [
        {"at": f"2026-03-{d + 1:02d}", "map": "de_nuke", "w": "Beta", "l": "Alpha", "n": 1}
        for d in range(10)
    ]
    params: dict[str, object] = {"min_games": 5, "k_map": 32.0}
    params.update(overrides)
    return MapForm(MISSING, games=games, **params)  # type: ignore[arg-type]


def test_form_weight_zero_prices_exactly_as_before() -> None:
    """The regression that matters: a bot that has the history file on disk but
    has not enabled the dial must bet identically to one that never heard of
    MapForm."""
    pool = (("Alpha", 7_000_000), ("Beta", 3_000_000))
    without = ContrarianPariMutuelStrategy(rating_weight=0.0)
    with_form = ContrarianPariMutuelStrategy(rating_weight=0.0, form=_form(), form_weight=0.0)

    a, b = without.decide(_prediction(*pool)), with_form.decide(_prediction(*pool))
    assert (a.should_bet, a.side, a.amount) == (b.should_bet, b.side, b.amount)


def test_weights_cannot_starve_the_crowd() -> None:
    """The crowd gets 1 - rating_weight - form_weight. Past 1 that is negative,
    which prices the pool backwards rather than merely ignoring it."""
    with pytest.raises(ValueError):
        ContrarianPariMutuelStrategy(rating_weight=0.6, form_weight=0.5)


def test_form_moves_the_price_when_it_is_switched_on() -> None:
    """An even pool prices both sides at 1.00x and offers no edge to anyone.
    MapForm says Alpha; only the strategy that listens to it finds a bet."""
    pool = (("Alpha", 5_000_000), ("Beta", 5_000_000))
    off = ContrarianPariMutuelStrategy(
        rating_weight=0.0, min_edge=0.0, form=_form(), form_weight=0.0
    )
    on = ContrarianPariMutuelStrategy(
        rating_weight=0.0, min_edge=0.0, form=_form(), form_weight=0.6
    )

    assert off.decide(_prediction(*pool)).should_bet is False
    assert on.decide(_prediction(*pool)).side == "Alpha"


def test_the_map_reaches_the_model() -> None:
    """Prediction.map_name has to arrive at MapForm, or every market is priced
    on overall strength and the map-specific half is dead code."""
    pool = (("Alpha", 5_000_000), ("Beta", 5_000_000))
    strategy = ContrarianPariMutuelStrategy(
        rating_weight=0.0, min_edge=0.0, form=_form(), form_weight=0.6
    )

    assert strategy.decide(_prediction(*pool)).side == "Alpha"  # Alpha is better overall
    assert strategy.decide(_prediction(*pool, map_name="de_nuke")).side == "Beta"  # not on Nuke


def test_a_model_with_nothing_to_say_forfeits_its_weight_to_the_crowd() -> None:
    """Unknown teams must not silently hand MapForm's share to the ratings (or
    the reverse) — the pool takes it back."""
    pool = (("Nobody", 7_000_000), ("Stranger", 3_000_000))
    quiet = ContrarianPariMutuelStrategy(rating_weight=0.0, form=_form(), form_weight=0.5)
    none_at_all = ContrarianPariMutuelStrategy(rating_weight=0.0)

    a, b = quiet.decide(_prediction(*pool)), none_at_all.decide(_prediction(*pool))
    assert (a.side, a.amount) == (b.side, b.amount)


def test_two_known_teams_count_as_a_team_market() -> None:
    """crowd_weight vs unrated_crowd_weight is about the market's TYPE. Valve
    ranks a few hundred teams; MapForm knows thousands, and a match between two
    of them is a team market whether or not Valve has heard of them.

    Asserted on the predicate rather than on a decision: any nonzero form_weight
    also injects MapForm's own opinion, which would move the stake for a second
    reason and make a decision-level assertion prove nothing about which crowd
    weight was picked.
    """
    outcomes = _prediction(("Alpha", 7_000_000), ("Beta", 3_000_000)).outcomes
    unknown = _prediction(("Nobody", 7_000_000), ("Stranger", 3_000_000)).outcomes

    off = ContrarianPariMutuelStrategy(rating_weight=0.0, form=_form(), form_weight=0.0)
    on = ContrarianPariMutuelStrategy(rating_weight=0.0, form=_form(), form_weight=0.3)

    assert on._is_team_market(outcomes) is True
    assert on._is_team_market(unknown) is False
    # With the dial off, nothing about this classification changes either.
    assert off._is_team_market(outcomes) is False


class _Fixed:
    """A form model with one fixed opinion: P(first label beats second)."""

    def __init__(self, probabilities: dict[str, float]) -> None:
        self._p = probabilities

    def win_probability(
        self, label: str, opponent: str, map_name: str | None = None
    ) -> float | None:
        return self._p.get(label)


def _filtered(model: _Fixed, min_form_edge: float | None = 0.05) -> ContrarianPariMutuelStrategy:
    return ContrarianPariMutuelStrategy(
        crowd_weight=0.85,
        unrated_crowd_weight=0.85,
        rating_weight=0.0,
        form=model,  # type: ignore[arg-type]
        form_weight=0.3,
        min_form_edge=min_form_edge,
    )


# Beta holds 20% of the pool and pays 4x. Shrinking the crowd toward a coin
# flip makes the underdog look cheap, so the blend backs Beta whatever the model
# says in these tests; only the filter decides whether the bet goes in.
UNDERDOG = (("Alpha", 8_000_000), ("Beta", 2_000_000))


def test_min_form_edge_off_by_default_changes_nothing() -> None:
    model = _Fixed({"Alpha": 0.81, "Beta": 0.19})
    off = _filtered(model, min_form_edge=None).decide(_prediction(*UNDERDOG))
    assert off.should_bet and off.side == "Beta"


def test_min_form_edge_skips_a_side_the_model_does_not_back() -> None:
    """The model gives Beta 19% against a 20% share: the blend still likes the
    price, the model alone would not bet it."""
    model = _Fixed({"Alpha": 0.81, "Beta": 0.19})
    decision = _filtered(model).decide(_prediction(*UNDERDOG))
    assert not decision.should_bet
    assert "does not back Beta" in decision.reason


def test_min_form_edge_keeps_a_side_the_model_backs() -> None:
    """Model says Beta 26% against a 20% share: +6 points clears the 5."""
    model = _Fixed({"Alpha": 0.74, "Beta": 0.26})
    decision = _filtered(model).decide(_prediction(*UNDERDOG))
    assert decision.should_bet and decision.side == "Beta"


def test_min_form_edge_is_a_threshold_not_just_a_direction() -> None:
    """+3 points backs Beta, but not by the 5 required."""
    model = _Fixed({"Alpha": 0.77, "Beta": 0.23})
    assert not _filtered(model).decide(_prediction(*UNDERDOG)).should_bet
    assert _filtered(model, min_form_edge=0.0).decide(_prediction(*UNDERDOG)).should_bet


def test_min_form_edge_skips_markets_the_model_cannot_price() -> None:
    """No opinion is not a yes: kill counts and unknown teams sit out."""
    decision = _filtered(_Fixed({})).decide(_prediction(*UNDERDOG))
    assert not decision.should_bet
    assert "no opinion" in decision.reason


def test_min_form_edge_prices_overtime_markets_against_the_teams_only() -> None:
    """Beta's share is 20% of the TEAMS' money (2M of 10M), not of the whole
    pool with OT in it (2M of 12.5M = 16%) — otherwise every OT market would
    look 4 points better to the filter than it is. At 23% the model is +3 on
    the teams' split (sit out) but would be +7 on the whole pool (bet)."""
    pool = (("Alpha", 8_000_000), ("Beta", 2_000_000), ("OT", 2_500_000))
    model = _Fixed({"Alpha": 0.77, "Beta": 0.23})
    assert not _filtered(model).decide(_prediction(*pool)).should_bet
    model = _Fixed({"Alpha": 0.74, "Beta": 0.26})
    decision = _filtered(model).decide(_prediction(*pool))
    assert decision.should_bet and decision.side == "Beta"


def test_min_form_edge_refuses_to_run_without_the_model() -> None:
    """With form_weight 0 the model never answers, so the filter would quietly
    stop every bet. Fail at startup instead."""
    with pytest.raises(ValueError):
        ContrarianPariMutuelStrategy(form_weight=0.0, min_form_edge=0.05)
