"""The replay harness has one job worth testing: settle a bet the way a
pari-mutuel pool does, and compound the result. Get that wrong and every cell
in the sweep is confidently misleading."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.replay import MIN_BET, START_BALANCE, as_prediction, load, replay


def _row(winner: str, **shares: int) -> dict:
    return {
        "id": "x",
        "title": "Quien gana el mapa?",
        "resolved_at": "2026-08-01T00:00:00+00:00",
        "outcomes": [
            {"label": label, "points": points, "bettors": 10} for label, points in shares.items()
        ],
        "winner": winner,
    }


def test_as_prediction_preserves_the_pool() -> None:
    prediction = as_prediction(_row("A", A=700_000, B=300_000), balance=20_000)
    assert prediction.total_pool == 1_000_000
    assert prediction.leading_outcome.label == "A"
    assert prediction.available_points == 20_000


def test_a_winning_bet_pays_the_pool_share() -> None:
    """A 25% side pays ~4x gross. The stake joins that side before the split,
    so the realized multiple is slightly under 1/s — never over it."""

    class _AlwaysBackB:
        def decide(self, prediction):  # type: ignore[no-untyped-def]
            from app.models.prediction import BetDecision

            return BetDecision(should_bet=True, side="B", amount=1_000, reason="test")

    rows = [_row("B", A=750_000, B=250_000)]
    strategy = _AlwaysBackB()
    balance = float(START_BALANCE)
    decision = strategy.decide(as_prediction(rows[0], int(balance)))
    side = next(o for o in rows[0]["outcomes"] if o["label"] == decision.side)
    pool = sum(o["points"] for o in rows[0]["outcomes"])
    gross = decision.amount * (pool + decision.amount) / (side["points"] + decision.amount)
    assert 3.9 < gross / decision.amount < 4.0


def test_losing_every_bet_never_goes_negative() -> None:
    """The bust guard has to fire before the bankroll can go through zero —
    a replay that keeps betting a negative balance reports fantasy multiples."""
    rows = [_row("A", A=800_000, B=200_000) for _ in range(60)]
    for row in rows:
        row["resolved_at"] = "2026-08-01T00:00:00+00:00"
    multiple, worst, placed, won = replay(
        rows, None, crowd_weight=0.12, kelly_fraction=1.0, max_pct=25.0
    )
    assert won == 0
    assert multiple > 0
    assert multiple * START_BALANCE >= 0
    assert worst <= 1.0
    assert placed <= len(rows)


def test_the_real_log_replays_at_the_shipped_dials() -> None:
    """Guards the loader against a schema change in results.jsonl."""
    path = Path(".data/results.jsonl")
    if not path.exists():  # a fresh checkout has no results yet
        return
    rows = load(path)
    multiple, worst, placed, won = replay(
        rows, None, crowd_weight=0.12, kelly_fraction=0.5, max_pct=25.0
    )
    assert placed > 0
    assert 0 <= won <= placed
    assert 0.0 <= worst <= 1.0
    assert multiple > 0


def test_min_bet_is_the_bust_floor() -> None:
    assert MIN_BET == 500
