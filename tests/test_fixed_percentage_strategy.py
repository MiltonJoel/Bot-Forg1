from datetime import datetime, timezone

from app.models.prediction import Outcome, Prediction
from app.strategies.fixed_percentage import FixedPercentageStrategy


def _prediction(leader_points: int, other_points: int, balance: int = 10_000) -> Prediction:
    return Prediction(
        id="x",
        title="Quien gana el mapa?",
        outcomes=[
            Outcome(id="a", label="TeamA", total_points=leader_points, total_bettors=10),
            Outcome(id="b", label="TeamB", total_points=other_points, total_bettors=5),
        ],
        min_bet=500,
        max_bet=5000,
        locks_at=datetime.now(timezone.utc),
        available_points=balance,
        captured_at=datetime.now(timezone.utc),
    )


def test_above_threshold_bets_high_percentage() -> None:
    strategy = FixedPercentageStrategy(threshold=90, high_pct=35, low_pct=20)
    # leader has 95% of the pool
    decision = strategy.decide(_prediction(leader_points=950, other_points=50))
    assert decision.should_bet is True
    assert decision.side == "TeamA"
    assert decision.amount == 3500


def test_at_threshold_bets_low_percentage() -> None:
    strategy = FixedPercentageStrategy(threshold=90, high_pct=35, low_pct=20)
    decision = strategy.decide(_prediction(leader_points=90, other_points=10))
    assert decision.amount == 2000


def test_below_threshold_bets_low_percentage() -> None:
    strategy = FixedPercentageStrategy(threshold=90, high_pct=35, low_pct=20)
    decision = strategy.decide(_prediction(leader_points=50, other_points=50))
    assert decision.amount == 2000


def test_amount_capped_at_max_bet() -> None:
    strategy = FixedPercentageStrategy(threshold=90, high_pct=35, low_pct=20)
    decision = strategy.decide(_prediction(leader_points=95, other_points=5, balance=100_000))
    assert decision.amount == 5000  # max_bet, not 35_000


def test_below_min_bet_means_no_bet() -> None:
    strategy = FixedPercentageStrategy(threshold=90, high_pct=35, low_pct=20)
    decision = strategy.decide(_prediction(leader_points=50, other_points=50, balance=100))
    assert decision.should_bet is False
    assert decision.amount == 0
