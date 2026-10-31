import asyncio
import json
from pathlib import Path

import pytest

from app.models.prediction import BetDecision
from app.services.monitoring_service import _SHAPE_SKIP_RE
from app.strategies.contrarian_pari_mutuel import ContrarianPariMutuelStrategy
from tests.test_contrarian_pari_mutuel import _prediction
from tests.test_result_notification import _monitor
from tests.test_result_notification import _prediction as _resolved_prediction

# crowd_weight 1.0 = the pool is a fair price, so the edge model never bets.
_FAIR = {"crowd_weight": 1.0, "unrated_crowd_weight": 1.0}


def test_fallback_backs_the_pool_favourite_when_there_is_no_edge() -> None:
    strategy = ContrarianPariMutuelStrategy(**_FAIR, fav_fallback_pct=0.25)
    decision = strategy.decide(_prediction(("A", 700_000), ("B", 300_000), balance=20_000))
    assert decision.should_bet and decision.fallback
    assert decision.side == "A"
    assert decision.amount == 5_000  # 25% of 20,000
    assert decision.reason.startswith("no edge")
    assert "favourite fallback" in decision.reason


def test_fallback_stake_is_capped_at_max_bet() -> None:
    strategy = ContrarianPariMutuelStrategy(**_FAIR, fav_fallback_pct=0.25)
    decision = strategy.decide(
        _prediction(("A", 7_000_000), ("B", 3_000_000), balance=1_000_000, max_bet=50_000)
    )
    assert decision.fallback and decision.amount == 50_000


def test_fallback_below_min_bet_is_skipped() -> None:
    strategy = ContrarianPariMutuelStrategy(**_FAIR, fav_fallback_pct=0.25)
    decision = strategy.decide(_prediction(("A", 700_000), ("B", 300_000), balance=1_900))
    assert not decision.should_bet and not decision.fallback
    assert "< min_bet 500, skipped" in decision.reason


def test_tied_favourites_are_skipped() -> None:
    strategy = ContrarianPariMutuelStrategy(**_FAIR, fav_fallback_pct=0.25)
    decision = strategy.decide(_prediction(("A", 400_000), ("B", 400_000), ("C", 200_000)))
    assert not decision.should_bet and not decision.fallback
    assert "top two tied" in decision.reason


def test_zero_disables_the_fallback() -> None:
    strategy = ContrarianPariMutuelStrategy(**_FAIR, fav_fallback_pct=0.0)
    decision = strategy.decide(_prediction(("A", 700_000), ("B", 300_000)))
    assert not decision.should_bet and not decision.fallback
    assert "favourite fallback" not in decision.reason


def test_fallback_bets_a_four_way_kill_market_and_keeps_its_skip_log() -> None:
    strategy = ContrarianPariMutuelStrategy(fav_fallback_pct=0.25)
    decision = strategy.decide(
        _prediction(
            ("9 o menos", 100_000), ("10 a 14", 500_000), ("15 a 19", 300_000), ("20 o mas", 1)
        )
    )
    assert decision.fallback and decision.side == "10 a 14"
    # results_skipped.jsonl keeps logging the closing pool of these markets.
    assert _SHAPE_SKIP_RE.match(decision.reason)


def test_an_edge_bet_is_left_alone() -> None:
    strategy = ContrarianPariMutuelStrategy(crowd_weight=0.12, fav_fallback_pct=0.25)
    decision = strategy.decide(_prediction(("A", 800_000), ("B", 200_000)))
    assert decision.should_bet and not decision.fallback
    assert decision.side == "B"


def test_pct_out_of_range_is_refused() -> None:
    with pytest.raises(ValueError):
        ContrarianPariMutuelStrategy(fav_fallback_pct=1.5)


def test_fallback_is_marked_in_results_log_and_result_message(tmp_path: Path) -> None:
    monitor, notifier = _monitor(tmp_path, balance=10_000)
    monitor._results_log_path = tmp_path / "results.jsonl"
    decision = BetDecision(should_bet=True, side="TeamA", amount=2_500, reason="x", fallback=True)

    monitor._record_result(_resolved_prediction(), decision, "TeamA")
    asyncio.run(monitor._notify_result(_resolved_prediction(), decision, "TeamA"))

    record = json.loads((tmp_path / "results.jsonl").read_text())
    assert record["fallback"] is True and record["bet_side"] == "TeamA"
    assert "respaldo al favorito" in notifier.sent[-1]


def test_fallback_skips_a_favourite_at_or_above_the_max_share() -> None:
    strategy = ContrarianPariMutuelStrategy(
        **_FAIR, fav_fallback_pct=0.25, fav_fallback_max_share=0.75
    )
    decision = strategy.decide(_prediction(("Falcons", 800_000), ("BB TEAM", 200_000)))
    assert not decision.should_bet and not decision.fallback
    assert decision.reason.startswith("no edge")
    assert decision.reason.endswith(
        "favourite fallback skipped: Falcons holds 80.0% of the pool (limit 75%)"
    )
    # The limit itself is "at or above".
    at_limit = strategy.decide(_prediction(("A", 750_000), ("B", 250_000)))
    assert not at_limit.should_bet and "skipped: A holds 75.0%" in at_limit.reason


def test_fallback_still_bets_a_favourite_under_the_max_share() -> None:
    strategy = ContrarianPariMutuelStrategy(
        **_FAIR, fav_fallback_pct=0.25, fav_fallback_max_share=0.75
    )
    decision = strategy.decide(_prediction(("A", 740_000), ("B", 260_000), balance=20_000))
    assert decision.should_bet and decision.fallback
    assert decision.side == "A" and decision.amount == 5_000


def test_default_max_share_keeps_betting_heavy_favourites() -> None:
    strategy = ContrarianPariMutuelStrategy(**_FAIR, fav_fallback_pct=0.25)
    decision = strategy.decide(_prediction(("A", 800_000), ("B", 200_000)))
    assert decision.should_bet and decision.fallback and decision.side == "A"


def test_max_share_out_of_range_is_refused() -> None:
    for bad in (0.0, 1.5):
        with pytest.raises(ValueError):
            ContrarianPariMutuelStrategy(fav_fallback_max_share=bad)
