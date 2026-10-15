"""The recalibration script sets STRATEGY_CROWD_WEIGHT, which sets bet sizing.
If its arithmetic is wrong the whole strategy is tuned wrong, so pin it."""

import json
from pathlib import Path
from typing import Any

from scripts.analyze_results import main, report_model_vs_pool


def _market(favourite_share: float, favourite_won: bool, pool: int = 1_000_000) -> str:
    favourite = int(pool * favourite_share)
    return json.dumps(
        {
            "id": "x",
            "title": "t",
            "pool": pool,
            "outcomes": [
                {"label": "Fav", "points": favourite, "bettors": 90},
                {"label": "Dog", "points": pool - favourite, "bettors": 10},
            ],
            "winner": "Fav" if favourite_won else "Dog",
            "bet_side": "Dog",
            "bet_amount": 500,
            "reason": "t",
        }
    )


def test_calibrated_crowd_yields_weight_one(tmp_path: Path, capsys: Any) -> None:
    # Favourites priced at 80% that win 80% of the time: the crowd is right,
    # so weight -> 1.0, and at that weight the strategy stops betting entirely.
    path = tmp_path / "results.jsonl"
    path.write_text("\n".join([_market(0.8, True)] * 8 + [_market(0.8, False)] * 2))

    assert main(path) == 0
    assert "STRATEGY_CROWD_WEIGHT=1.00" in capsys.readouterr().out


def test_overconfident_crowd_yields_low_weight(tmp_path: Path, capsys: Any) -> None:
    # Favourites priced at 70% that win only 50%: pure noise above a coin flip,
    # so the crowd's share earns no weight at all.
    path = tmp_path / "results.jsonl"
    path.write_text("\n".join([_market(0.7, True)] * 5 + [_market(0.7, False)] * 5))

    assert main(path) == 0
    out = capsys.readouterr().out
    assert "STRATEGY_CROWD_WEIGHT=0.00" in out
    assert "provisional" in out  # under 50 markets, must say so


def test_multi_outcome_markets_are_excluded(tmp_path: Path, capsys: Any) -> None:
    # The calibration is only meaningful head-to-head.
    three_way = json.dumps(
        {
            "id": "y",
            "title": "t",
            "pool": 300,
            "outcomes": [
                {"label": "A", "points": 100, "bettors": 1},
                {"label": "B", "points": 100, "bettors": 1},
                {"label": "C", "points": 100, "bettors": 1},
            ],
            "winner": "A",
            "bet_side": None,
            "bet_amount": 0,
            "reason": "t",
        }
    )
    path = tmp_path / "results.jsonl"
    path.write_text("\n".join([three_way, _market(0.8, True)]))

    assert main(path) == 0
    assert "2 resolved markets (1 head-to-head)" in capsys.readouterr().out


def _ot_map(winner: str, pool: int = 1000) -> str:
    """A Team/Team/OT map: OT holds 20% of the pool, the favourite 60%, so the
    favourite's share of the *inner* head-to-head is 600/800 = 75%."""
    return json.dumps(
        {
            "id": "z",
            "title": "Quien gana el 1er MAPA?",
            "pool": pool,
            "outcomes": [
                {"label": "Fav", "points": int(pool * 0.6), "bettors": 50},
                {"label": "Dog", "points": int(pool * 0.2), "bettors": 20},
                {"label": "OT", "points": int(pool * 0.2), "bettors": 5},
            ],
            "winner": winner,
            "bet_side": "Dog",
            "bet_amount": 500,
            "reason": "t",
        }
    )


def test_overtime_maps_count_as_head_to_heads(tmp_path: Path, capsys: Any) -> None:
    # The strategy prices the two teams against each other's pool, so the
    # calibration must too: 600/800 = 75%, NOT 600/1000 = 60%. Favourites win
    # 5/8, so the honest weight is (0.625-0.5)/(0.75-0.5) = 0.50. Measuring
    # against the full pool instead would read 1.25 and clamp to 1.00.
    path = tmp_path / "results.jsonl"
    path.write_text("\n".join([_ot_map("Fav")] * 5 + [_ot_map("Dog")] * 3))

    assert main(path) == 0
    out = capsys.readouterr().out
    assert "8 head-to-head" in out
    assert "STRATEGY_CROWD_WEIGHT=0.50" in out


def test_overtime_win_is_excluded_from_calibration_but_counted(tmp_path: Path, capsys: Any) -> None:
    # Nobody's team won, so the map says nothing about crowd calibration — but
    # it is exactly the sample OT frequency is measured on.
    path = tmp_path / "results.jsonl"
    path.write_text("\n".join([_ot_map("Fav")] * 3 + [_ot_map("OT")]))

    assert main(path) == 0
    out = capsys.readouterr().out
    assert "3 head-to-head" in out
    assert "(1/4 maps)" in out


def test_missing_file_is_reported_not_crashed(tmp_path: Path) -> None:
    assert main(tmp_path / "nope.jsonl") == 1


def _sample(pool_p: float, rating_p: float, won: bool) -> tuple[float, float, bool]:
    return (pool_p, rating_p, won)


def test_a_timid_model_does_not_count_as_a_better_one(capsys: Any) -> None:
    """The trap this section exists to catch. Shaped like the real 2026-08 data:
    the ratings scored BETTER than the pool on log loss while picking FEWER
    winners, purely by hedging. Log loss alone reads that as an edge, so the
    accuracy column has to sit next to it or the next reader raises
    STRATEGY_RATING_WEIGHT on a calibration artifact."""
    # 15 markets where both models pick the same side, 10 of them right.
    agree = [_sample(0.75, 0.58, i < 10) for i in range(15)]
    # 5 where they disagree; the ratings are right on 2, the pool on 3.
    disagree = [_sample(0.80, 0.45, i < 3) for i in range(5)]
    report_model_vs_pool(agree + disagree, "Valve ratings", "STRATEGY_RATING_WEIGHT")
    out = capsys.readouterr().out

    assert "always 0.50 (nothing)" in out  # the no-information bar
    assert "0.693" in out
    # The table rows are indented; the section header also carries the model
    # name, so match on the indent or the header wins.
    pool_line = next(ln for ln in out.splitlines() if ln.startswith("   pool price"))
    rating_line = next(ln for ln in out.splitlines() if ln.startswith("   Valve ratings"))
    assert "65%" in pool_line and "60%" in rating_line  # ratings LESS accurate
    assert float(rating_line.split()[2]) < float(pool_line.split()[2])  # better log loss
    assert "CI straddles zero" in out  # ...and still not a real advantage


def test_a_genuinely_better_model_is_reported_as_such(capsys: Any) -> None:
    """The inverse must also fire, or the section can only ever say 'no'."""
    samples = [_sample(0.5, 0.95, True) for _ in range(30)]
    report_model_vs_pool(samples, "Valve ratings", "STRATEGY_RATING_WEIGHT")
    out = capsys.readouterr().out
    assert "Valve ratings genuinely beats the pool" in out


def test_disagreement_count_is_the_real_sample(capsys: Any) -> None:
    """Where the two models pick the same side the rating changes no bet, so the
    disagreement count is what the reader has to see."""
    agree = [_sample(0.8, 0.7, True) for _ in range(9)]
    disagree = [_sample(0.8, 0.3, False)]
    report_model_vs_pool(agree + disagree, "Valve ratings", "STRATEGY_RATING_WEIGHT")
    out = capsys.readouterr().out
    assert "they disagree on 1/10 markets; Valve ratings was right 1 of those." in out


def test_ratings_section_is_skipped_without_ratings(tmp_path: Path, capsys: Any) -> None:
    """It must stay runnable against a bare results file with no .env."""
    report_model_vs_pool([], "Valve ratings", "STRATEGY_RATING_WEIGHT")
    assert capsys.readouterr().out == ""
