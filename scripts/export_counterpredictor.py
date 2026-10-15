"""Export the CounterPredictor model to .data/counterpredictor.json for the bot.

    cd ~/Desktop/CounterPredictor && python3 update.py           # optional: fresh data
    /usr/bin/python3 scripts/export_counterpredictor.py [~/Desktop/CounterPredictor]
    /usr/bin/python3 scripts/export_counterpredictor.py --v1 [~/Desktop/CounterPredictor]

Runs under the SYSTEM python (it has the pandas + sklearn CounterPredictor
needs; the bot's venv does not and should not). Writes two files:

  .data/counterpredictor.json           live snapshot: every team's feature
                                        state today + the v2 logistic weights
  .data/counterpredictor_backtest.json  per logged forg1 market, the two teams'
                                        state as of that market's day, and a
                                        logistic refitted ONLY on maps played
                                        before the first logged market

`--v1` exports the Kaggle-only v1 model (models/prematch.pkl) instead, over
the Kaggle history it was trained on, and writes its backtest to
.data/counterpredictor_v1_backtest.json. v1 replaced v2 as the live model on 2026-10-08:
on 89 logged head-to-heads it scored log loss 0.630 against 0.649 for v2 and
0.723 for the pool. Its backtest uses the frozen v1 weights rather than a
refit, so unlike v2's it is not fully leak-free.

The backtest model exists because v2 was trained on maps through October, which
include the very matches forg1 streamed — scoring v2 on them would be hindsight.
scripts/analyze_results.py scores the backtest file against the pool.
"""

import json
import math
import sys
from pathlib import Path

import pandas as pd

BOT = Path(__file__).resolve().parent.parent
V1 = "--v1" in sys.argv[1:]
_ARGS = [a for a in sys.argv[1:] if a != "--v1"]
SRC = Path(_ARGS[0] if _ARGS else "~/Desktop/CounterPredictor").expanduser()
sys.path[:0] = [str(SRC), str(BOT)]

import oos_eval as O  # noqa: E402
import prematch as P  # noqa: E402

from app.services.counter_predictor import CounterPredictor  # noqa: E402

MIN_GAMES = 10


def _num(x: float) -> float | None:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)


def team_state(st: P.State, t: str, map_elo: dict, n_map: dict) -> dict:
    s = st.side(t, "", st.lineup.get(t, []), pd.Timestamp.now())
    return {
        "elo": st.elo[t],
        "form": _num(s["form"]),
        "rdiff": _num(s["rdiff"]),
        "kd": _num(s["kd"]),
        "adr": _num(s["adr"]),
        "kast": _num(s["kast"]),
        "stable": _num(s["stable"]),
        "n": st.n[t],
        "last": st.last[t].isoformat() if t in st.last else None,
        "map_elo": map_elo.get(t, {}),
        "n_map": n_map.get(t, {}),
    }


def snapshot(st: P.State, teams: list[str], model, as_of: pd.Timestamp, maps: list[str]) -> dict:
    keep = set(teams)
    map_elo: dict = {}
    n_map: dict = {}
    for store, out in ((st.map_elo, map_elo), (st.n_map, n_map)):
        for (t, m), v in list(store.items()):
            if t in keep and m:  # m == "" is side()'s own defaultdict probe
                out.setdefault(t, {})[m] = v
    return {
        "as_of": as_of.isoformat(),
        "active_maps": maps,
        "teams": {t: team_state(st, t, map_elo, n_map) for t in teams},
        "h2h": {"|".join(p): w for p, w in st.h2h.items() if set(p) <= keep},
        "model": {
            "mean": model[0].mean_.tolist(),
            "scale": model[0].scale_.tolist(),
            "coef": model[-1].coef_[0].tolist(),
            "intercept": model[-1].intercept_.tolist(),
        },
    }


def replay(hist: pd.DataFrame, stops: list[pd.Timestamp]):
    """One chronological pass (P.build's loop); yields the state at each stop,
    containing only matches that started before it."""
    st, i = P.State(), 0
    for _, g in hist.groupby(["datetime", "match_id"], sort=True):
        while i < len(stops) and g.datetime.iloc[0] >= stops[i]:
            yield stops[i], st
            i += 1
        for r in g.itertuples():
            st.update(r, *P.lineups(r))
    for stop in stops[i:]:
        yield stop, st


def main_v1() -> None:
    """Live snapshot + backtest for v1. Same layout as main(), but v1's own
    Kaggle-only history and its pickled weights for both files."""
    hist = P.load(O.V5)
    end = hist.datetime.max()
    recent = hist[hist.datetime >= end - pd.Timedelta(days=90)].map_name
    maps = [m for m in recent.value_counts().index if m != "Unknown"][:7]
    with open(SRC / "models/prematch.pkl", "rb") as f:
        v1 = __import__("pickle").load(f)["logreg"]

    _, st = next(replay(hist, [end + pd.Timedelta(days=1)]))
    teams = [t for t in st.n if st.n[t] >= MIN_GAMES]
    live = snapshot(st, teams, v1, end, maps)
    (BOT / ".data/counterpredictor.json").write_text(json.dumps(live))
    names_today = CounterPredictor(live, min_games=0)
    print(f"v1 live snapshot: {len(teams):,} teams, data through {end:%Y-%m-%d}, maps {maps}")

    rows = [
        json.loads(line) for line in (BOT / ".data/results.jsonl").read_text().splitlines() if line
    ]
    days = sorted({pd.Timestamp(r["resolved_at"][:10]) for r in rows})
    by_day = {d: [r for r in rows if pd.Timestamp(r["resolved_at"][:10]) == d] for d in days}
    backtest = []
    for stop, st in replay(hist, days):
        for r in by_day[stop]:
            labels = [o["label"] for o in r["outcomes"]]
            names = [names_today.team(label) for label in labels]
            backtest.append(
                {
                    "id": r["id"],
                    "names": dict(zip(labels, names)),
                    "snapshot": snapshot(st, [n for n in names if n and n in st.n], v1, stop, maps),
                }
            )
    (BOT / ".data/counterpredictor_v1_backtest.json").write_text(json.dumps(backtest))
    print(f"v1 backtest: {len(backtest)} markets")


def main() -> None:
    hist = O.v2_history()
    end = hist.datetime.max()
    recent = hist[hist.datetime >= end - pd.Timedelta(days=90)].map_name
    maps = [m for m in recent.value_counts().index if m != "Unknown"][:7]

    rows = [
        json.loads(line) for line in (BOT / ".data/results.jsonl").read_text().splitlines() if line
    ]
    days = sorted({pd.Timestamp(r["resolved_at"][:10]) for r in rows})

    # Honest backtest model: features from one full replay, fitted only on maps
    # before the first logged market.
    X = O.features(hist)
    tr = ((hist.datetime >= P.BURN_IN_END) & (hist.datetime < days[0])).values
    Xs, ys = P.symmetrize(X[tr], hist.y.values[tr])
    backtest_model = P.make_pipeline(P.StandardScaler(), P.LogisticRegression(max_iter=1000))
    backtest_model.fit(Xs.fillna(0), ys)
    live_model = __import__("pickle").load(open(SRC / "models/prematch_v2.pkl", "rb"))["logreg"]

    _, st = next(replay(hist, [end + pd.Timedelta(days=1)]))
    teams = [t for t in st.n if st.n[t] >= MIN_GAMES]
    live = snapshot(st, teams, live_model, end, maps)
    (BOT / ".data/counterpredictor.json").write_text(json.dumps(live))
    names_today = CounterPredictor(live, min_games=0)
    print(f"live snapshot: {len(teams):,} teams, data through {end:%Y-%m-%d}, maps {maps}")

    # Second pass: label -> team uses today's names, but the state is the day's.
    backtest = []
    states = replay(hist, days)
    by_day = {d: [r for r in rows if pd.Timestamp(r["resolved_at"][:10]) == d] for d in days}
    for stop, st in states:
        for r in by_day[stop]:
            labels = [o["label"] for o in r["outcomes"]]
            names = [names_today.team(label) for label in labels]
            teams = [n for n in names if n and n in st.n]
            backtest.append(
                {
                    "id": r["id"],
                    "names": dict(zip(labels, names)),
                    "snapshot": snapshot(st, teams, backtest_model, stop, maps),
                }
            )
    (BOT / ".data/counterpredictor_backtest.json").write_text(json.dumps(backtest))
    print(f"backtest: {len(backtest)} markets, model fitted on maps before {days[0]:%Y-%m-%d}")


if __name__ == "__main__":
    main_v1() if V1 else main()
