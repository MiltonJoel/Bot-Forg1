"""Re-measure the crowd calibration the strategy is tuned on.

    uv run python scripts/analyze_results.py

Reads .data/results.jsonl (written by MonitoringService as each market
resolves) and answers the one question that sets STRATEGY_CROWD_WEIGHT: when
the crowd puts X% of the pool on a side, how often does that side actually win?

Perfectly calibrated crowd => implied == actual => no edge exists => set
STRATEGY_CROWD_WEIGHT=1.0 and the bot correctly stops betting. The wider the
gap, the lower the weight. The value shipped (0.12) came from 19 markets mined
out of the text logs, which is thin — this script is how you replace it with
something better.

Team/Team/OT map markets count as head-to-heads here, priced on the two teams'
inner pool exactly as the strategy prices them. They used to be dropped for
being 3-outcome, which silently discarded more than half the sample.

It also scores Valve's ratings against the POOL, which is the comparison that
sets STRATEGY_RATING_WEIGHT. The ratings' published validation (log loss 0.664
over 385 matches) is against a coin flip, and beating a coin flip is not the
bar — the pool price is what we have to out-predict to have an edge at all. See
`report_ratings_vs_pool` for the two ways that comparison lies if you only read
the log loss column.
"""

import json
import sys
from math import log
from pathlib import Path
from random import Random
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.strategies.contrarian_pari_mutuel import _OVERTIME_RE

DEFAULT_RESULTS = Path(".data/results.jsonl")


def _overtime(row: dict[str, Any]) -> list[dict[str, Any]]:
    """The overtime options of a Team/Team/OT... market, or [] — same shape
    test as the strategy's `_overtime_outcomes`, on the raw result dict.

    Plural since 2026-08-28, when forg1 began splitting overtime into "OT" and
    "OT 2 o +" buckets. While this only matched the old exact three-way, every
    market in the new format was dropped from the calibration entirely.
    """
    ot = [o for o in row["outcomes"] if _OVERTIME_RE.match(o["label"])]
    return ot if ot and len(row["outcomes"]) - len(ot) == 2 else []


def _two_team_market(row: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The two team outcomes of a market the strategy prices as a head-to-head,
    or None.

    That includes Team/Team/OT... maps: the strategy splits the overtime
    buckets off and prices the two teams against *each other's* pool, so every
    measurement here must be taken the same way. Dropping them threw away more
    than half the sample. Markets where overtime actually won have no team
    winner and are excluded — they are counted in the OT stats instead.
    """
    if not row["pool"]:
        return None
    outcomes = row["outcomes"]
    overtime = _overtime(row)
    if overtime:
        if _OVERTIME_RE.match(row["winner"]):
            return None
        overtime_ids = {id(o) for o in overtime}
        outcomes = [o for o in outcomes if id(o) not in overtime_ids]
    elif len(outcomes) != 2:
        return None
    return outcomes if sum(o["points"] for o in outcomes) else None


def head_to_heads(rows: list[dict[str, Any]]) -> list[tuple[float, bool]]:
    """(favourite's share, favourite won) for every head-to-head market."""
    pairs: list[tuple[float, bool]] = []
    for row in rows:
        outcomes = _two_team_market(row)
        if outcomes is None:
            continue
        inner = sum(o["points"] for o in outcomes)
        favourite = max(outcomes, key=lambda o: o["points"])
        pairs.append((favourite["points"] / inner, favourite["label"] == row["winner"]))
    return pairs


def overtime_stats(rows: list[dict[str, Any]]) -> tuple[int, int, float]:
    """(maps with an OT option, times any OT bucket won, mean share on OT).

    The buckets are mutually exclusive ways for the same event to happen, so
    they are summed: what is priced and measured is P(the map goes to
    overtime at all), which is what a team bet actually loses to.
    """
    maps = [r for r in rows if r["pool"] and _overtime(r)]
    if not maps:
        return 0, 0, 0.0
    won = sum(1 for r in maps if _OVERTIME_RE.match(r["winner"]))
    share = sum(sum(o["points"] for o in _overtime(r)) / r["pool"] for r in maps) / len(maps)
    return len(maps), won, share


def rated_head_to_heads(rows: list[dict[str, Any]]) -> list[tuple[float, float, bool]]:
    """(pool's p for the first team, Valve ratings' p for it, it won) for every
    head-to-head where BOTH teams carry an official rating.

    Returns [] when the ratings can't be loaded, so this stays runnable against
    a bare results file with no .env.
    """
    try:
        from app.config.settings import Settings
        from app.services.team_ratings import TeamRatings

        settings = Settings()  # type: ignore[call-arg]
        ratings = TeamRatings(settings.team_ratings_path, settings.strategy_rating_scale)
    except Exception:
        return []

    samples: list[tuple[float, float, bool]] = []
    for row in rows:
        outcomes = _two_team_market(row)
        if outcomes is None:
            continue
        first, second = outcomes
        probability = ratings.win_probability(first["label"], second["label"])
        if probability is None:  # one of them is unranked
            continue
        inner = sum(o["points"] for o in outcomes)
        samples.append((first["points"] / inner, probability, first["label"] == row["winner"]))
    return samples


def form_head_to_heads(
    rows: list[dict[str, Any]],
) -> tuple[list[tuple[float, float, bool]], int, int]:
    """(pool p for the first team, MapForm p for it, it won) per head-to-head,
    plus how many had a resolvable map and how many were skipped as too thin.

    LEAKAGE IS THE WHOLE DIFFICULTY HERE. .data/match_history.json runs to
    today, so a model fitted on all of it and then asked about a market from
    three weeks ago has already seen that market's map — it would score
    beautifully and mean nothing. The reference project guards this with
    `endDate = D - 1` on every HLTV request; the equivalent here is to refit
    MapForm from scratch on maps played strictly BEFORE each market's date.

    That means one full Elo pass per distinct market date. It is ~1s each and
    there are a couple of dozen dates, which is why this is a script and not
    something the bot does. Do not "optimise" it into a single fit.
    """
    try:
        from app.config.settings import Settings
        from app.services.map_resolver import map_number_in_title, resolve_stored_map
        from app.services.match_history import MapForm

        settings = Settings()  # type: ignore[call-arg]
        history: list[dict[str, Any]] = json.loads(settings.match_history_path.read_text())["games"]
    except Exception:
        return [], 0, 0

    params = {
        "scale": settings.strategy_form_scale,
        "k_global": settings.strategy_form_k_global,
        "k_map": settings.strategy_form_k_map,
        "min_games": settings.strategy_form_min_games,
    }
    by_date: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        # No date, no leak-free fit: a row we cannot place in time cannot be
        # scored honestly, so it is skipped rather than fitted on everything.
        resolved_at = row.get("resolved_at")
        if resolved_at:
            by_date.setdefault(str(resolved_at)[:10], []).append(row)

    samples: list[tuple[float, float, bool]] = []
    mapped = thin = 0
    for date in sorted(by_date):
        past = [g for g in history if str(g["at"]) < date]
        form = MapForm(settings.match_history_path, games=past, **params)  # type: ignore[arg-type]
        for row in by_date[date]:
            outcomes = _two_team_market(row)
            if outcomes is None:
                continue
            first, second = outcomes
            labels = (str(first["label"]), str(second["label"]))
            # Prefer the map the bot recorded live; fall back to recovering it
            # from history, which is how the markets logged before this feature
            # existed still get scored per map.
            #
            # This lookup deliberately uses the FULL history while the model is
            # fitted on `past` only. Which map is being played is information
            # the bot genuinely has at decision time — it reads it off bo3.gg's
            # live match — whereas who won it is not. Searching `past` here
            # would have excluded the market's own day and found nothing, which
            # is exactly what it did before this comment existed.
            map_name = row.get("map") or resolve_stored_map(
                history, date, labels, map_number_in_title(str(row["title"]))
            )
            probability = form.win_probability(labels[0], labels[1], map_name)
            if probability is None:
                thin += 1
                continue
            mapped += map_name is not None
            inner = sum(o["points"] for o in outcomes)
            samples.append((first["points"] / inner, probability, first["label"] == row["winner"]))
    return samples, mapped, thin


def counterpredictor_head_to_heads(
    rows: list[dict[str, Any]], backtest: Path = Path(".data/counterpredictor_backtest.json")
) -> tuple[list[tuple[float, float, bool]], int, int]:
    """Same as form_head_to_heads, for CounterPredictor. The leak guard lives in
    scripts/export_counterpredictor.py: each market's snapshot is the teams'
    state at the start of its day, priced by a logistic fitted only on maps
    before the first logged market."""
    try:
        from app.config.settings import Settings
        from app.services.counter_predictor import CounterPredictor
        from app.services.map_resolver import map_number_in_title, resolve_stored_map

        by_id = {b["id"]: b for b in json.loads(backtest.read_text())}
        settings = Settings()  # type: ignore[call-arg]
        history: list[dict[str, Any]] = json.loads(settings.match_history_path.read_text())["games"]
    except Exception:
        return [], 0, 0

    samples: list[tuple[float, float, bool]] = []
    mapped = thin = 0
    for row in rows:
        outcomes = _two_team_market(row)
        entry = by_id.get(row["id"])
        if outcomes is None or entry is None:
            continue
        model = CounterPredictor(entry["snapshot"], settings.strategy_form_min_games)
        labels = (str(outcomes[0]["label"]), str(outcomes[1]["label"]))
        names = [entry["names"].get(label) for label in labels]
        map_name = row.get("map") or resolve_stored_map(
            history, str(row["resolved_at"])[:10], labels, map_number_in_title(str(row["title"]))
        )
        probability = (
            model.win_probability(names[0], names[1], map_name, model.as_of)
            if names[0] and names[1]
            else None
        )
        if probability is None:
            thin += 1
            continue
        mapped += map_name is not None
        inner = sum(o["points"] for o in outcomes)
        samples.append(
            (outcomes[0]["points"] / inner, probability, outcomes[0]["label"] == row["winner"])
        )
    return samples, mapped, thin


def _log_loss(probabilities: list[float]) -> float:
    """Mean negative log likelihood. A no-information 0.50 predictor scores
    0.693, which is the bar any model has to clear to have said anything."""
    return -sum(log(min(max(p, 1e-9), 1 - 1e-9)) for p in probabilities) / len(probabilities)


def report_model_vs_pool(
    samples: list[tuple[float, float, bool]], name: str, dial: str, note: str = ""
) -> None:
    """Does `name` beat the POOL at picking winners?

    Not "beat a coin flip" — the ratings were fitted against a coin flip, but
    the pool price is the thing we actually have to out-predict to have an edge.
    Two traps this prints its way around:

    * Log loss rewards hedging. A model can score better than the pool purely by
      being less confident while picking fewer winners, which is a calibration
      artifact and not skill — hence the accuracy and confidence columns, and
      the 0.50 baseline row.
    * The two models mostly agree. Only the markets where they DISAGREE carry
      any information about which is better, so that count is the real n.
    """
    n = len(samples)
    if not n:
        return
    pool = _log_loss([p if won else 1 - p for p, _, won in samples])
    rated = _log_loss([q if won else 1 - q for _, q, won in samples])

    print(f"\n{name} vs the pool, on {n} head-to-heads it can price{note}")
    print(f"   {'model':22}{'log loss':>10}{'accuracy':>10}{'confidence':>12}")
    print(f"   {'always 0.50 (nothing)':22}{0.6931:10.3f}{'—':>10}{'50%':>12}")
    for label, probability in (("pool price", 0), (name, 1)):
        picks = [s[probability] for s in samples]
        accuracy = sum((p > 0.5) == won for p, (_, _, won) in zip(picks, samples)) / n
        confidence = sum(max(p, 1 - p) for p in picks) / n
        loss = pool if probability == 0 else rated
        print(f"   {label:22}{loss:10.3f}{accuracy * 100:9.0f}%{confidence * 100:11.0f}%")

    disagree = [s for s in samples if (s[0] > 0.5) != (s[1] > 0.5)]
    right = sum(1 for _, q, won in disagree if (q > 0.5) == won)
    print(f"\n   they disagree on {len(disagree)}/{n} markets; {name} was right {right} of those.")
    print("   That count IS the sample — where they agree, the model changes no bet.")

    low, high = _bootstrap_difference(samples)
    print(f"   log loss difference {pool - rated:+.3f} (positive = {name} better),")
    print(f"   bootstrap 95% CI [{low:+.3f}, {high:+.3f}]")
    if low < 0 < high:
        print("   => CI straddles zero: NO measurable advantage either way. Do not")
        print(f"      raise {dial} on the strength of this.")
    elif low > 0:
        print(f"   => {name} genuinely beats the pool. Worth raising {dial}.")
    else:
        print(f"   => the pool genuinely beats {name}. Consider lowering {dial}.")


def _bootstrap_difference(
    samples: list[tuple[float, float, bool]], draws: int = 20_000
) -> tuple[float, float]:
    """95% CI on (pool log loss - ratings log loss). Seeded, so re-running the
    script twice on the same data can't produce two different conclusions."""
    rng = Random(0)
    differences = []
    for _ in range(draws):
        drawn = [rng.choice(samples) for _ in samples]
        differences.append(
            _log_loss([p if won else 1 - p for p, _, won in drawn])
            - _log_loss([q if won else 1 - q for _, q, won in drawn])
        )
    differences.sort()
    return differences[int(0.025 * draws)], differences[int(0.975 * draws)]


def main(results: Path = DEFAULT_RESULTS) -> int:
    if not results.exists():
        print(f"No {results} yet — it fills up as markets resolve.")
        return 1

    rows = [json.loads(line) for line in results.read_text().splitlines() if line.strip()]
    pairs = head_to_heads(rows)
    print(f"{len(rows)} resolved markets ({len(pairs)} head-to-head)\n")
    if not pairs:
        return 1

    n = len(pairs)
    implied = sum(share for share, _ in pairs) / n
    actual = sum(won for _, won in pairs) / n

    print(f"crowd implied the favourite wins   {implied * 100:5.1f}%")
    print(f"the favourite actually won         {actual * 100:5.1f}%  ({int(actual * n)}/{n})")
    # Shrink factor that maps the crowd's average share onto the observed rate:
    # p = 0.5 + w * (share - 0.5).
    if abs(implied - 0.5) > 1e-9:
        weight = (actual - 0.5) / (implied - 0.5)
        print(f"\n=> STRATEGY_CROWD_WEIGHT={max(0.0, min(1.0, weight)):.2f}")
        if n < 50:
            print(f"   (only {n} markets — treat as provisional; 50+ before trusting it)")

    maps, ot_won, ot_share = overtime_stats(rows)
    if maps:
        print(
            f"\nOT priced at {ot_share * 100:5.1f}% of the pool on average (pays "
            f"{1 / ot_share - 1:.1f}x)"
        )
        print(f"OT actually won  {ot_won / maps * 100:5.1f}%  ({ot_won}/{maps} maps)")
        print("   NOT a signal to bet OT: see CLAUDE.md. Watch it, do not act on it")
        print("   until this sample is large enough to separate from noise.")

    report_model_vs_pool(rated_head_to_heads(rows), "Valve ratings", "STRATEGY_RATING_WEIGHT")

    form_samples, mapped, thin = form_head_to_heads(rows)
    if form_samples:
        note = f" ({mapped} with the map identified, {thin} skipped as too thin)"
        report_model_vs_pool(form_samples, "MapForm", "STRATEGY_FORM_WEIGHT", note)
        print("   Every prediction above was made by a model refitted on maps")
        print("   played strictly BEFORE that market — no hindsight.")

    cp_samples, mapped, thin = counterpredictor_head_to_heads(rows)
    if cp_samples:
        note = f" ({mapped} with the map identified, {thin} skipped as unknown/too thin)"
        report_model_vs_pool(cp_samples, "CounterPredictor", "STRATEGY_FORM_WEIGHT", note)

    staked = sum(r["bet_amount"] for r in rows if r["bet_amount"])
    bets = [r for r in rows if r["bet_amount"]]
    won = [r for r in bets if r["bet_side"] == r["winner"]]
    if bets:
        print(f"\nour bets: {len(won)}/{len(bets)} correct, {staked:,} pts staked")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_RESULTS))
