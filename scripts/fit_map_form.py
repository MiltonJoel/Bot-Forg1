"""Fit MapForm's constants against held-out maps, and say whether the
map-specific half earns its keep.

    uv run python scripts/fit_map_form.py
    uv run python scripts/fit_map_form.py --after 2026-05-01 --quick

Nothing here is guessed. Every cell is scored prequentially: each map is
predicted from ratings built only out of maps played before it, so a parameter
set that merely memorises the history scores badly, as it should. This is the
same discipline STRATEGY_RATING_SCALE was fitted with, and the reason CLAUDE.md
distrusts any dial that was picked by eye.

The bar is 0.693 — the log loss of a 50/50 coin. A model that cannot clear that
has said nothing. Beating it is still NOT enough to bet on: see
scripts/analyze_results.py, which scores this model against the POOL, which is
the thing we actually have to out-predict.

The reference project (TaiZo1/cs2-win-prediction) found map identity worth at
most +0.01 AUC on top of round state. `--quick` off, this prints the same test
for our data: the best cell with `k_map` free, against the best cell with
`k_map` pinned to 0.
"""

import argparse
import json
import sys
from itertools import product
from math import log
from pathlib import Path
from random import Random

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.match_history import MapForm

DEFAULT_HISTORY = Path(".data/match_history.json")


def log_loss(probabilities: list[float]) -> float:
    """Mean negative log likelihood of the outcome that actually happened.
    `probabilities` is already P(the winner wins), so no flipping is needed —
    and log loss is orientation-invariant anyway."""
    return -sum(log(min(max(p, 1e-9), 1 - 1e-9)) for p in probabilities) / len(probabilities)


def score(games: list[dict[str, str]], after: str, **params: float) -> tuple[float, int, float]:
    """(held-out log loss, maps scored, accuracy) for one parameter set."""
    # games=[] builds an empty model without touching the disk; the scoring
    # pass is then the same fit() the bot runs, with recording switched on.
    form = MapForm(DEFAULT_HISTORY, games=[], **params)  # type: ignore[arg-type]
    predictions = form.fit(games, record_after=after)
    if not predictions:
        return float("inf"), 0, 0.0
    accuracy = sum(p > 0.5 for p in predictions) / len(predictions)
    return log_loss(predictions), len(predictions), accuracy


def calibration(games: list[dict[str, str]], after: str, **params: float) -> None:
    """Predicted vs actual, in buckets.

    Orientation is randomised here and ONLY here. The stored rows always put
    the winner first, so bucketing them as-is would show a 100% hit rate and
    look beautifully calibrated while meaning nothing. Log loss and accuracy
    above are orientation-invariant and need no such treatment.
    """
    form = MapForm(DEFAULT_HISTORY, games=[], **params)  # type: ignore[arg-type]
    predictions = form.fit(games, record_after=after)
    rng = Random(0)
    flipped = [(p, True) if rng.random() < 0.5 else (1 - p, False) for p in predictions]
    print("\n   calibration (orientation randomised)")
    print(f"      {'predicted':>12}{'maps':>8}{'actual':>10}")
    for low, high in ((0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0)):
        bucket = [won for p, won in flipped if low <= p < high]
        if bucket:
            print(
                f"      {low:.0%}-{high:.0%}".ljust(18)
                + f"{len(bucket):>2}".rjust(6)
                + f"{sum(bucket) / len(bucket) * 100:9.0f}%"
            )


def _edge_warning(grid: dict[str, list[float]], winner: dict[str, float]) -> None:
    """Shout if the best cell sits on the edge of the search.

    A winner on a boundary means the true optimum is outside the grid and the
    number printed below is the grid's opinion, not the data's. Widen and re-run
    rather than shipping it.
    """
    on_edge = [
        name
        for name, value in winner.items()
        if len(grid[name]) > 1 and value in (min(grid[name]), max(grid[name]))
    ]
    if on_edge:
        print(f"\n   !! best cell is on the grid edge for: {', '.join(on_edge)}")
        print("      The optimum is outside the search. Widen the grid and re-run")
        print("      before putting any of this in .env.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    parser.add_argument(
        "--after",
        default=None,
        help="score maps after this date (default: the newest 25%% of the history)",
    )
    parser.add_argument("--quick", action="store_true", help="skip the map-identity ablation")
    parser.add_argument("--scale", type=float, default=400.0, help="pinned Elo scale (see grid)")
    args = parser.parse_args()

    if not args.history.exists():
        print(f"No {args.history} — run scripts/fetch_match_history.py first.")
        return 1
    games: list[dict[str, str]] = json.loads(args.history.read_text())["games"]
    dates = sorted(str(g["at"]) for g in games)
    after = args.after or dates[int(len(dates) * 0.75)]
    print(f"{len(games):,} maps, {dates[0]} .. {dates[-1]}")
    print(f"scoring maps after {after} (everything before is warm-up)\n")

    # Wide on purpose. The first version of this grid topped out at k_global=36
    # and bottomed out at scale=300, and the winner sat on BOTH edges — which
    # means the grid, not the data, chose it. A fit that lands on its own
    # boundary has not found an optimum; `_edge_warning` below shouts if it
    # happens again.
    # `scale` is PINNED, not searched, and that is deliberate. In Elo, scale and
    # K are redundant: enlarging both by the same factor leaves every prediction
    # unchanged, so a 2-D search over them walks a ridge and the winner lands on
    # whichever edge the grid happened to stop at. (It did exactly that twice —
    # first at scale=300, then at 400, for a log loss difference of 0.0002.)
    # 400 is the classic Elo decade and fixes the units; K then carries all the
    # fitting. Pass --scale to override if you ever want to see the ridge.
    grid = {
        "scale": [args.scale],
        "k_global": [16.0, 24.0, 36.0, 44.0, 52.0, 64.0, 80.0],
        "k_map": [0.0, 4.0, 8.0, 16.0, 24.0, 32.0, 48.0],
        "map_weight": [1.0],
    }
    results = []
    print(f"   {'scale':>7}{'k_global':>10}{'k_map':>8}{'log loss':>11}{'acc':>7}{'maps':>9}")
    for scale, k_global, k_map, map_weight in product(*grid.values()):
        loss, n, accuracy = score(
            games, after, scale=scale, k_global=k_global, k_map=k_map, map_weight=map_weight
        )
        results.append((loss, scale, k_global, k_map, map_weight, n, accuracy))
        print(
            f"   {scale:7.0f}{k_global:10.0f}{k_map:8.0f}{loss:11.4f}{accuracy * 100:6.0f}%{n:9,}"
        )

    results.sort()
    loss, scale, k_global, k_map, map_weight, n, accuracy = results[0]
    _edge_warning(grid, {"scale": scale, "k_global": k_global, "k_map": k_map})
    print(f"\nbest: scale={scale:.0f} k_global={k_global:.0f} k_map={k_map:.0f}")
    print(
        f"      log loss {loss:.4f} vs 0.6931 for a coin flip, accuracy {accuracy * 100:.1f}% on {n:,} maps"
    )
    if loss >= 0.6931:
        print("      => WORSE THAN A COIN. Do not enable STRATEGY_FORM_WEIGHT.")

    if not args.quick:
        flat = min(r for r in results if r[3] == 0.0)
        print("\nis the per-map half worth anything?")
        print(f"   best with k_map free : {loss:.4f}")
        print(f"   best with k_map = 0  : {flat[0]:.4f}   (global strength only)")
        gain = flat[0] - loss
        print(f"   map identity buys {gain:+.4f} log loss.")
        # 0.005 is where a gain stops being rounding noise on ~10k held-out
        # maps. The threshold used to sit at 0.002, which is where the measured
        # gain actually lands, so two runs of the same data printed opposite
        # verdicts depending on the fourth decimal place.
        if gain < 0.005:
            print("   => essentially nothing, which is what the reference project found")
            print("      too (map identity: at most +0.01 AUC over round state). Keep")
            print("      k_map for the shrinkage it gives thin maps, but do NOT claim")
            print("      a map-specific edge on the strength of this.")
        else:
            print("   => a real per-map signal. Worth keeping k_map above 0.")

    calibration(games, after, scale=scale, k_global=k_global, k_map=k_map, map_weight=map_weight)
    print("\nPut the winning cell in .env:")
    print(f"   STRATEGY_FORM_SCALE={scale:.0f}")
    print(f"   STRATEGY_FORM_K_GLOBAL={k_global:.0f}")
    print(f"   STRATEGY_FORM_K_MAP={k_map:.0f}")
    print("\nThen run scripts/analyze_results.py — beating a coin flip is not the")
    print("bar, beating the POOL is, and only that script measures it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
