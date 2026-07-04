from datetime import datetime, timezone

from app.models.prediction import Outcome, Prediction
from app.strategies.aggressive_fractional_kelly import AggressiveFractionalKellyStrategy


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


def test_strong_signal_bets_more_aggressively() -> None:
    strategy = AggressiveFractionalKellyStrategy()
    weak = strategy.decide(_prediction(leader_points=55, other_points=45))
    strong = strategy.decide(_prediction(leader_points=950, other_points=50))

    assert weak.should_bet is True
    assert strong.should_bet is True
    assert strong.amount > weak.amount


def test_always_bets_when_minimum_is_available() -> None:
    strategy = AggressiveFractionalKellyStrategy()
    decision = strategy.decide(_prediction(leader_points=50, other_points=50, balance=600))

    assert decision.should_bet is True
    assert decision.amount >= 500


def test_below_minimum_balance_skips_bet() -> None:
    strategy = AggressiveFractionalKellyStrategy()
    decision = strategy.decide(_prediction(leader_points=90, other_points=10, balance=100))

    assert decision.should_bet is False
    assert decision.amount == 0
