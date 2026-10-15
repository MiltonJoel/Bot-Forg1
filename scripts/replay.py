"""Offline parameter sweep: replay `.data/results.jsonl` through the strategy.

Feeds every logged market back through `ContrarianPariMutuelStrategy.decide()`,
compounds a synthetic bankroll, and prints the final multiple and the worst
drawdown for each combination of dials. Stakes nothing, needs no browser.

    .venv/bin/python scripts/replay.py                    # the default sweep
    .venv/bin/python scripts/replay.py --detail 0.12 0.5 25   # bet-by-bet log

Two honest limits on what this can tell you:

* The logged shares are the pool as it stood when the strategy DECIDED (the
  `_pending` snapshot), which is the right price to replay — it is the price
  the bot actually faced. The pool kept moving until lock, so realized payouts
  differ from these; that affects the multiples, not the pick.
* The sample is still small (121 markets / 86 head-to-heads as of 2026-08-31).
  A cell that wins here can win it on a handful of longshots that carry the
  live P&L. Read the drawdown column, not the multiple.
* It replays the pick and the sizing, but NOT the self-dilution drag measured
  on the live book (-6.98% per unit staked if the pool price is true), because
  it re-prices each market from the logged pool as if our stake were the first
  to arrive. Multiples here are therefore optimistic across every cell.
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from typing import Any
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.settings import Settings  # noqa: E402
from app.models.prediction import Outcome, Prediction  # noqa: E402
from app.services.team_ratings import TeamRatings  # noqa: E402
from app.strategies.contrarian_pari_mutuel import ContrarianPariMutuelStrategy  # noqa: E402

RESULTS = Path(".data/results.jsonl")
START_BALANCE = 33857  # the first session bankroll reference in the logs
MIN_BET = 500  # forg1's floor; the per-market value is not logged
# forg1's per-market ceiling. NOT unbounded — that assumption lived here until
# 2026-08-31 and made every sweep this file has ever printed optimistic.
# Measured from the logged bets: 23 of 78 landed on a round number while
# sitting at 7-24% of balance, i.e. Kelly asked for more and the site refused.
# The values seen are 10k/20k/30k/50k/100k, and since 2026-08-22 it has been a
# flat 50,000 regardless of pool size (a 9.7M pool got the same cap as a 572k
# one). The per-market value is not in results.jsonl, so the current regime is
# used for every market rather than guessed per row.
MAX_BET = 50_000

CROWD_WEIGHTS = (0.12, 0.35, 0.67, 0.85, 1.0)  # 0.85 is the shipped value
KELLY_FRACTIONS = (0.25, 0.5, 1.0)
MAX_PCTS = (5.0, 10.0, 25.0)


def load(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return sorted(rows, key=lambda r: r["resolved_at"])


def as_prediction(row: dict[str, Any], balance: int) -> Prediction:
    """A results.jsonl row as the Prediction the strategy would have seen."""
    now = datetime.now(UTC)
    return Prediction(
        id=row["id"],
        title=row["title"],
        outcomes=[
            Outcome(
                id=str(i),
                label=o["label"],
                total_points=o["points"],
                total_bettors=o["bettors"],
            )
            for i, o in enumerate(row["outcomes"])
        ],
        min_bet=MIN_BET,
        max_bet=MAX_BET,
        locks_at=now,
        available_points=balance,
        captured_at=now,
    )


def replay(
    rows: list[dict[str, Any]],
    ratings: TeamRatings | None,
    *,
    detail: bool = False,
    **dials: Any,
) -> tuple[float, float, int, int]:
    """Compound START_BALANCE through every market. Returns
    (final multiple, worst drawdown as a fraction, bets placed, bets won)."""
    strategy = ContrarianPariMutuelStrategy(ratings=ratings, **dials)
    balance = float(START_BALANCE)
    peak = balance
    worst = 0.0
    placed = won = 0

    for row in rows:
        decision = strategy.decide(as_prediction(row, int(balance)))
        if not decision.should_bet:
            continue
        placed += 1
        side = next(o for o in row["outcomes"] if o["label"] == decision.side)
        pool = sum(o["points"] for o in row["outcomes"])
        balance -= decision.amount
        if row["winner"] == decision.side:
            won += 1
            # Pari-mutuel: our stake joins the winning side before the split.
            balance += (
                decision.amount * (pool + decision.amount) / (side["points"] + decision.amount)
            )
        peak = max(peak, balance)
        worst = max(worst, 1 - balance / peak)
        if detail:
            mark = "W" if row["winner"] == decision.side else "L"
            print(
                f"  {row['resolved_at'][:10]} {str(decision.side)[:20]:20} "
                f"{side['points'] / pool * 100:5.1f}% stake {decision.amount:7} "
                f"{mark} -> balance {balance:12,.0f}"
            )
        if balance < MIN_BET:
            break  # busted; nothing further is bettable

    return balance / START_BALANCE, worst, placed, won


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--detail",
        nargs=3,
        type=float,
        metavar=("CROWD_WEIGHT", "KELLY_FRACTION", "MAX_PCT"),
        help="print every bet for one cell instead of sweeping",
    )
    args = parser.parse_args()

    if not RESULTS.exists():
        print(f"No {RESULTS} — nothing to replay.", file=sys.stderr)
        return 1
    rows = load(RESULTS)
    # Same ratings and same untested dials as production, so the sweep only
    # varies the three under test.
    settings = Settings()  # type: ignore[call-arg]
    ratings = TeamRatings(settings.team_ratings_path, settings.strategy_rating_scale)
    fixed: dict[str, Any] = {
        "min_edge": settings.strategy_min_edge,
        "rating_weight": settings.strategy_rating_weight,
        "max_outcomes": settings.strategy_max_outcomes,
        "ot_crowd_weight": settings.strategy_ot_crowd_weight,
    }

    if args.detail:
        crowd, kelly, cap = args.detail
        print(f"crowd_weight={crowd} kelly_fraction={kelly} max_pct={cap}\n")
        mult, worst, placed, won = replay(
            rows,
            ratings,
            detail=True,
            **fixed,
            crowd_weight=crowd,
            unrated_crowd_weight=crowd,
            kelly_fraction=kelly,
            max_pct=cap,
        )
        print(f"\n{placed} bets, {won} won -> {mult:.2f}x, worst drawdown {worst * 100:.0f}%")
        return 0

    print(f"{len(rows)} logged markets, starting bankroll {START_BALANCE:,}\n")
    print(f"{'crowd':>6} {'kelly':>6} {'cap%':>5} {'bets':>5} {'won':>4} {'final':>9} {'maxDD':>6}")
    for crowd in CROWD_WEIGHTS:
        for kelly in KELLY_FRACTIONS:
            for cap in MAX_PCTS:
                mult, worst, placed, won = replay(
                    rows,
                    ratings,
                    **fixed,
                    crowd_weight=crowd,
                    unrated_crowd_weight=crowd,
                    kelly_fraction=kelly,
                    max_pct=cap,
                )
                print(
                    f"{crowd:6.2f} {kelly:6.2f} {cap:5.0f} {placed:5} {won:4} "
                    f"{mult:8.2f}x {worst * 100:5.0f}%"
                )
        print()
    print("Read maxDD, not final: this is a small sample. See the module docstring.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
