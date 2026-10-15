import re
from functools import partial
from math import inf, log

from app.models.prediction import BetDecision, Outcome, Prediction

# Same matcher the ratings use, so "9z" catches forg1's "9z Globant" while
# "magic" still never catches "MAGICOS".
from app.services.counter_predictor import CounterPredictor
from app.services.match_history import MapForm
from app.services.team_ratings import TeamRatings, _first_token, _normalize
from app.strategies.base_strategy import BettingStrategy

# Forg1's map markets carry one or more overtime options, which WIN the market
# outright — both team bets lose. Nothing in the payload marks them, so they
# are matched by label; add a spelling here if the site starts writing them
# differently.
#
# On 2026-08-28 forg1 started splitting overtime into buckets, so a map market
# is now "Team / Team / OT / OT 2 o +" as often as the old three-way. The
# variants seen so far are "OT", "OT 2", "OT 2 + MAS" and "OT 2 o +", hence a
# prefix match rather than the old exact-label set. The `\b` guard is what
# stops a team whose name merely begins with "ot" from being read as overtime;
# verified against all 70 distinct outcome labels in .data/results.jsonl, where
# it matches the four OT spellings and nothing else.
_OVERTIME_RE = re.compile(r"^\s*(ot|overtime|tiempo\s+extra)\b", re.IGNORECASE)


class ContrarianPariMutuelStrategy(BettingStrategy):
    """Prices every outcome against what it actually pays, and bets only where
    the evidence disagrees with the market.

    Forg1 is pari-mutuel: the crowd's share of the pool IS the payout price. An
    outcome holding share `s` pays `1/s - 1`, so backing it needs a true win
    rate of exactly `s` just to break even. Backing favourites therefore has
    ZERO edge by construction, before any rake — which is why the previous
    strategy bled: it staked MORE as the price got WORSE, up to a full-size bet
    into a 100%-share market that could only lose (payout 0.000x).

    Probability comes from two sources, weighted separately:

    * `crowd_weight` shrinks the pool share toward a uniform prior. Measured at
      0.12 over 19 of this bot's binary markets (crowd implied 71.6%, favourite
      won 52.6%). Any value below 1.0 mathematically favours the underdog — that
      is the shape of the pool, not a preference — and only sets how good a
      price must be before staking.
    * `unrated_crowd_weight` does the same where no official rating exists (kill
      counts, player names, one-off questions), so team and non-team markets can
      be tuned apart. Same value by owner's decision: aggressive everywhere,
      because a cautious setting recovers the bankroll too slowly.
    * Valve's official standings blend in at `rating_weight` for head-to-heads
      between two ranked teams — the only outside information available.
    * `ot_crowd_weight` prices the "OT" option of a Team/Team/OT map market.
      Overtime is not a third team: it wins the market outright, so the two
      teams share only what it leaves. At 1.0 (the default) OT is priced at the
      pool's own share, which is exactly zero edge — it is never backed unless
      this weight is lowered on purpose.

    The 0.12 calibration was measured on CS2 map matches only; applying it to
    other market types is a deliberate assumption, not a measurement. Re-measure
    with scripts/analyze_results.py as results accumulate.
    """

    def __init__(
        self,
        crowd_weight: float = 0.12,
        kelly_fraction: float = 1.0,
        max_pct: float = 25.0,
        min_edge: float = 0.05,
        rating_weight: float = 0.35,
        max_outcomes: int = 2,
        unrated_crowd_weight: float = 0.12,
        ot_crowd_weight: float = 1.0,
        always_back: tuple[str, ...] = (),
        ratings: TeamRatings | None = None,
        form: MapForm | CounterPredictor | None = None,
        form_weight: float = 0.0,
        min_form_edge: float | None = None,
        fav_fallback_pct: float = 0.0,
    ) -> None:
        if not 0.0 <= crowd_weight <= 1.0:
            raise ValueError("crowd_weight must be between 0 and 1")
        if not 0.0 <= ot_crowd_weight <= 1.0:
            raise ValueError("ot_crowd_weight must be between 0 and 1")
        if not 0.0 < kelly_fraction <= 1.0:
            raise ValueError("kelly_fraction must be in (0, 1]")
        if not 0.0 < max_pct <= 100.0:
            raise ValueError("max_pct must be in (0, 100]")
        if not 0.0 <= rating_weight <= 1.0:
            raise ValueError("rating_weight must be between 0 and 1")
        if not 0.0 <= form_weight <= 1.0:
            raise ValueError("form_weight must be between 0 and 1")
        if rating_weight + form_weight > 1.0:
            # The crowd gets whatever these two leave. Letting them sum past 1
            # would give the pool a negative weight, which prices the market
            # backwards rather than merely ignoring it.
            raise ValueError("rating_weight + form_weight must not exceed 1")
        if min_form_edge is not None and form_weight <= 0.0:
            # The filter asks the form model to vouch for every bet. With the
            # model switched off it can never vouch, so the bot would silently
            # stop betting altogether — fail at startup instead.
            raise ValueError("min_form_edge needs form_weight > 0")
        if not 0.0 <= fav_fallback_pct <= 1.0:
            raise ValueError("fav_fallback_pct must be between 0 and 1")

        self._crowd_weight = crowd_weight
        self._kelly_fraction = kelly_fraction
        self._max_pct = max_pct
        self._min_edge = min_edge
        self._rating_weight = rating_weight
        self._max_outcomes = max_outcomes
        self._unrated_crowd_weight = unrated_crowd_weight
        self._ot_crowd_weight = ot_crowd_weight
        self._always_back = always_back
        self._ratings = ratings
        self._form = form
        self._form_weight = form_weight
        self._min_form_edge = min_form_edge
        self._fav_fallback_pct = fav_fallback_pct

    def replace_form(self, form: MapForm | CounterPredictor) -> None:
        """Swap in a re-exported form model (Telegram /actualizarmodelo). Weight
        and filter stay as configured; only the model's knowledge changes."""
        self._form = form

    def decide(self, prediction: Prediction) -> BetDecision:
        decision = self._decide(prediction)
        if decision.should_bet or self._fav_fallback_pct <= 0:
            return decision
        return self._favourite_fallback(prediction, decision)

    def _favourite_fallback(self, prediction: Prediction, declined: BetDecision) -> BetDecision:
        """Owner's decision 2026-10-15: whenever the edge model sits out (no
        edge, the form filter, a 3+ way market, a Kelly stake under min_bet),
        back the crowd favourite instead — the outcome holding the most pool at
        decision time — with a flat `fav_fallback_pct` of the balance, capped by
        the site's max_bet. A tie for the top spot has no favourite and is
        skipped, as is a stake below min_bet.

        The declined reason is kept FIRST in the new reason: the monitor's
        results_skipped.jsonl log keys on it starting with "N-way market", so a
        multi-way market the fallback bets keeps getting its closing pool logged.
        """
        ranked = sorted(prediction.outcomes, key=lambda o: o.total_points, reverse=True)
        first = ranked[0]
        if len(ranked) < 2 or first.total_points == ranked[1].total_points:
            return declined.model_copy(
                update={"reason": f"{declined.reason} — favourite fallback: top two tied, skipped"}
            )
        amount = min(
            int(prediction.available_points * self._fav_fallback_pct),
            prediction.max_bet,
            prediction.available_points,
        )
        if amount < prediction.min_bet:
            return declined.model_copy(
                update={
                    "reason": f"{declined.reason} — favourite fallback: stake {amount} "
                    f"< min_bet {prediction.min_bet}, skipped"
                }
            )
        share = self._share(prediction, first)
        payout = self._net_odds(prediction.total_pool, first.total_points, amount)
        return BetDecision(
            should_bet=True,
            side=first.label,
            amount=amount,
            fallback=True,
            reason=(
                f"{declined.reason} — favourite fallback: {first.label} holds "
                f"{share * 100:.1f}% of the pool -> pays {payout:.2f}x; stake {amount} "
                f"({amount / prediction.available_points * 100:.1f}% of balance)"
            ),
        )

    def _decide(self, prediction: Prediction) -> BetDecision:
        skip = self._unbettable_reason(prediction)
        if skip:
            return BetDecision(should_bet=False, side=None, amount=0, reason=skip)

        priced = [
            self._price(prediction, outcome, probability)
            for outcome, probability in zip(prediction.outcomes, self._probabilities(prediction))
        ]
        forced = self._standing_order(priced)
        outcome, edge, stake, probability = forced or max(priced, key=lambda p: p[1])

        if forced is None and edge < self._min_edge:
            return BetDecision(
                should_bet=False,
                side=None,
                amount=0,
                reason=(
                    f"no edge: best is {outcome.label} at {edge:+.2f} "
                    f"(need {self._min_edge:+.2f}) — sitting out"
                ),
            )
        if forced is None and self._min_form_edge is not None:
            form_edge = self._form_edge(prediction, outcome)
            if form_edge is None or form_edge < self._min_form_edge:
                return BetDecision(
                    should_bet=False,
                    side=None,
                    amount=0,
                    reason=(
                        f"form model does not back {outcome.label}: "
                        + ("no opinion" if form_edge is None else f"{form_edge * 100:+.1f} pts")
                        + f" over its pool share (need {self._min_form_edge * 100:+.1f})"
                        + " — sitting out"
                    ),
                )
        if forced is not None:
            # Kelly returns 0 at a bad price, and a standing order still has to
            # bet. The site minimum is the floor, so backing a heavy favourite
            # at 0.18x costs min_bet rather than the cap.
            stake = max(stake, float(prediction.min_bet))

        amount = min(int(stake), prediction.max_bet, prediction.available_points)
        if amount < prediction.min_bet:
            return BetDecision(
                should_bet=False,
                side=None,
                amount=0,
                reason=(
                    f"edge {edge:+.2f} on {outcome.label} but Kelly stake {int(stake)} "
                    f"< min_bet {prediction.min_bet}"
                ),
            )

        share = self._share(prediction, outcome)
        # Odds quoted at the amount actually staked, not at the pre-bet share:
        # our own money joins that side, and on a thin side that gap is large.
        payout = self._net_odds(prediction.total_pool, outcome.total_points, amount)
        return BetDecision(
            should_bet=True,
            side=outcome.label,
            amount=amount,
            reason=(
                ("standing order: always back this team — " if forced is not None else "")
                + f"{outcome.label} holds {share * 100:.1f}% of the pool -> pays {payout:.2f}x; "
                f"model p={probability:.2f} -> edge {edge:+.2f}, stake {amount} "
                f"({amount / prediction.available_points * 100:.1f}% of balance)"
            ),
        )

    # --- pricing -------------------------------------------------------------

    def _unbettable_reason(self, prediction: Prediction) -> str | None:
        if prediction.available_points < prediction.min_bet:
            return f"balance {prediction.available_points} < min_bet {prediction.min_bet}"
        if prediction.total_pool <= 0:
            return "empty pool — no price to bet against yet"
        # A Team/Team/OT... map market is exempt: it is a head-to-head with an
        # overtime side, and _probabilities models it as one.
        if len(prediction.outcomes) > self._max_outcomes and not self._overtime_outcomes(
            prediction
        ):
            return (
                f"{len(prediction.outcomes)}-way market (limit {self._max_outcomes}); "
                "the crowd calibration was only measured on head-to-head markets"
            )
        return None

    def _standing_order(
        self, priced: list[tuple[Outcome, float, float, float]]
    ) -> tuple[Outcome, float, float, float] | None:
        """The owner's "always bet on this team" list, or None.

        This deliberately bypasses the edge check — the whole point of the
        strategy is that a favourite's price carries no edge, and a standing
        order says to back them anyway. Sizing is still honest: the Kelly stake
        when the price is good, the site minimum when it is not.
        """
        for entry in priced:
            label = entry[0].label
            if any(
                _normalize(label) == _normalize(name) or _first_token(label) == _first_token(name)
                for name in self._always_back
            ):
                return entry
        return None

    def _overtime_outcomes(self, prediction: Prediction) -> list[Outcome]:
        """The overtime options of a Team/Team/OT... market, or [] otherwise.

        The shape the model assumes is "exactly two teams, plus one or more
        overtime buckets" — it does not care how many buckets there are, only
        that what is left is a head-to-head. That covers both the old
        Team/Team/OT three-way and the Team/Team/OT/OT-2-or-more four-way forg1
        started running on 2026-08-28.

        A four-way kill-count market has no overtime side at all, so it matches
        nothing here and stays skipped by the outcome limit.
        """
        overtime = [o for o in prediction.outcomes if _OVERTIME_RE.match(o.label)]
        teams = len(prediction.outcomes) - len(overtime)
        return overtime if overtime and teams == 2 else []

    def _both_rated(self, outcomes: list[Outcome]) -> bool:
        """True only for a head-to-head between two officially ranked teams —
        the one case where we hold information the pool might not."""
        if self._ratings is None or len(outcomes) != 2:
            return False
        return all(self._ratings.rating(o.label) is not None for o in outcomes)

    def _form_edge(self, prediction: Prediction, outcome: Outcome) -> float | None:
        """The form model's own win probability for `outcome` minus that team's
        share of the two teams' pool, or None where the model has no opinion
        (not a head-to-head, an overtime side, a team it does not know).

        This is the `min_form_edge` filter: the blended price already decides
        the side, and the filter only drops bets the form model alone would not
        make. Replayed 2026-10-08 on the 135 logged markets with CounterPredictor
        v1 at form_weight 0.3, a 5-point threshold kept 68 of 85 bets and tied or
        beat betting every edge in every 40-day simulation, gaining most when the
        logged edge is shrunk toward the live book's (see CLAUDE.md). Measured on
        the pool as the bot saw it at decision time, with an edge that is not
        proven yet.
        """
        overtime = self._overtime_outcomes(prediction)
        teams = [o for o in prediction.outcomes if o not in overtime]
        if len(teams) != 2 or outcome not in teams:
            return None
        inner = sum(o.total_points for o in teams)
        probabilities = self._form_probabilities(teams, prediction.map_name)
        if probabilities is None or inner <= 0:
            return None
        return probabilities[teams.index(outcome)] - outcome.total_points / inner

    def _share(self, prediction: Prediction, outcome: Outcome) -> float:
        return outcome.total_points / prediction.total_pool

    def _probabilities(self, prediction: Prediction) -> list[float]:
        """Win probability per outcome, from whichever evidence we actually have.

        Plain markets are priced by `_shrunk` against the whole pool. A
        Team/Team/OT... map market is not: overtime wins the market outright, so
        a team bet only pays when the map ends in regulation. Treating OT as a
        third team and shrinking it toward a 1/3 uniform prior would price an
        18%-share side at ~31% and invent a +0.7 edge on the longshot — the bot
        would stake its cap on overtime every single map. Instead each overtime
        bucket is priced on its own weight (1.0 = the pool's price = zero edge)
        and the two teams split what they all leave, priced against each other
        alone.

        Several overtime buckets are handled the same way as one: they are
        mutually exclusive outcomes that each end the map in overtime, so
        P(no overtime) is one minus their total.
        """
        overtime = self._overtime_outcomes(prediction)
        if not overtime:
            return self._shrunk(
                prediction.outcomes, float(prediction.total_pool), prediction.map_name
            )

        uniform = 1.0 / len(prediction.outcomes)
        is_overtime = {id(o) for o in overtime}
        overtime_probabilities = {
            id(o): min(
                max(uniform + self._ot_crowd_weight * (self._share(prediction, o) - uniform), 0.0),
                1.0,
            )
            for o in overtime
        }
        # Capped at 1.0: with several buckets each shrunk toward uniform, a low
        # ot_crowd_weight could otherwise push the total past 1 and hand the
        # teams a negative probability.
        total_overtime = min(sum(overtime_probabilities.values()), 1.0)
        teams = [o for o in prediction.outcomes if id(o) not in is_overtime]
        # P(team wins) = P(no overtime of any kind) x P(team wins | no overtime).
        conditional = iter(
            (1.0 - total_overtime) * p
            for p in self._shrunk(
                teams, float(sum(o.total_points for o in teams)), prediction.map_name
            )
        )
        return [
            overtime_probabilities[id(o)] if id(o) in is_overtime else next(conditional)
            for o in prediction.outcomes
        ]

    def _shrunk(
        self, outcomes: list[Outcome], pool: float, map_name: str | None = None
    ) -> list[float]:
        """Pool shares shrunk toward uniform, with Valve's rating blended in for
        a head-to-head between two ranked teams. Sums to 1.

        `crowd_weight` applies when both teams are officially ranked and
        `unrated_crowd_weight` otherwise — two knobs because the calibration was
        only ever measured on ranked CS2 matches, so team and non-team markets
        can be tuned apart.

        Any weight below 1.0 mathematically favours the underdog — that is the
        shape of a pari-mutuel pool, not a preference. The weight only sets how
        good a price must be before staking.

        `pool` is these outcomes' own pool, not necessarily the prediction's: in
        an OT market the two teams are priced against each other, with the
        overtime money excluded from the denominator.
        """
        uniform = 1.0 / len(outcomes)
        if pool <= 0:
            return [uniform] * len(outcomes)
        rated = self._both_rated(outcomes)
        weight = (
            self._crowd_weight if self._is_team_market(outcomes) else self._unrated_crowd_weight
        )
        crowd = [uniform + weight * (o.total_points / pool - uniform) for o in outcomes]

        # Each outside model takes its own weight and the crowd keeps the rest,
        # so switching one off returns the price to exactly what the other two
        # said. A model that has nothing to say about these teams contributes
        # nothing and forfeits its weight to the crowd rather than to the other
        # model — an unranked team must not silently double MapForm's say.
        sources = [
            (self._rating_weight, self._rating_probabilities(outcomes) if rated else None),
            (self._form_weight, self._form_probabilities(outcomes, map_name)),
        ]
        available = [(w, p) for w, p in sources if w > 0.0 and p is not None]
        if not available:
            return crowd

        outside = sum(w for w, _ in available)
        blended = [
            (1 - outside) * c + sum(w * p[i] for w, p in available if p is not None)
            for i, c in enumerate(crowd)
        ]
        total = sum(blended)
        return [b / total for b in blended]

    def _is_team_market(self, outcomes: list[Outcome]) -> bool:
        """Is this a match between two teams (rather than a kill count or a
        one-off question)? That is what picks `crowd_weight` over
        `unrated_crowd_weight` — a distinction about the market's TYPE, not
        about whether we happen to hold a rating.

        Valve's list alone used to answer it, which mislabels every market
        between two teams Valve does not rank as "unrated". MapForm knows
        thousands more teams and fixes that — but only once it is switched on,
        so a bot running with form_weight=0 prices exactly as it did before.
        """
        if self._both_rated(outcomes):
            return True
        return self._form_weight > 0.0 and self._form_rates_both(outcomes)

    def _rating_probabilities(self, outcomes: list[Outcome]) -> list[float] | None:
        """Head-to-head win probability from Valve's standings, or None when
        this isn't a two-team market with both teams rated (kill-count ranges,
        national teams, unranked orgs)."""
        if self._ratings is None or self._rating_weight <= 0:
            return None
        if len(outcomes) != 2:
            return None
        first, second = outcomes
        probability = self._ratings.win_probability(first.label, second.label)
        if probability is None:
            return None
        return [probability, 1.0 - probability]

    def _form_rates_both(self, outcomes: list[Outcome]) -> bool:
        """True when MapForm has enough history on both sides of a
        head-to-head to say anything at all."""
        if self._form is None or len(outcomes) != 2:
            return False
        first, second = outcomes
        return self._form.win_probability(first.label, second.label) is not None

    def _form_probabilities(
        self, outcomes: list[Outcome], map_name: str | None
    ) -> list[float] | None:
        """Head-to-head win probability from bo3.gg match history, on this map
        where we know it, or None when this isn't two teams MapForm knows.

        `map_name` is None for most markets — forg1's titles say "2do MAPA",
        not which map — and MapForm then answers with the teams' overall
        strength. That is a weaker answer, not a wrong one.
        """
        if self._form is None or self._form_weight <= 0 or len(outcomes) != 2:
            return None
        first, second = outcomes
        probability = self._form.win_probability(first.label, second.label, map_name)
        if probability is None:
            return None
        return [probability, 1.0 - probability]

    def _price(
        self, prediction: Prediction, outcome: Outcome, probability: float
    ) -> tuple[Outcome, float, float, float]:
        """(outcome, expected value per unit staked, fractional-Kelly stake, p).

        Our own stake joins the pool before it pays out, so the odds we get
        depend on how much we bet — betting big into a thin side means mostly
        betting against ourselves. Edge is quoted at the best price actually
        obtainable (a minimum-size bet); the stake is then whatever maximises
        expected log growth at the diluted odds.
        """
        pool = prediction.total_pool
        bankroll = prediction.available_points
        best_odds = self._net_odds(pool, outcome.total_points, prediction.min_bet)
        edge = probability * best_odds - (1.0 - probability)
        if edge <= 0:
            return outcome, edge, 0.0, probability

        cap = min(bankroll * self._max_pct / 100.0, float(bankroll))
        stake = self._kelly_fraction * self._growth_optimal_stake(
            pool, outcome.total_points, bankroll, probability, cap
        )
        if stake < prediction.min_bet:
            # Kelly wants less than the site will accept. That happens when a
            # side is so thin that the winnings are capped at the pool no matter
            # what we add — betting more buys no upside. The minimum bet is
            # still the right move whenever it grows the bankroll on average.
            growth = self._expected_log_growth(
                pool, outcome.total_points, bankroll, probability, float(prediction.min_bet)
            )
            stake = float(prediction.min_bet) if growth > 0 else 0.0
        return outcome, edge, stake, probability

    @staticmethod
    def _net_odds(pool: float, side_points: float, stake: float) -> float:
        """Profit per unit staked if this outcome wins, once `stake` has joined
        it. Pari-mutuel: the winning side splits the whole pool."""
        return (pool + stake) / (side_points + stake) - 1.0

    @staticmethod
    def _expected_log_growth(
        pool: float, side_points: float, bankroll: int, probability: float, stake: float
    ) -> float:
        """Expected log change in bankroll from staking `stake` on this outcome.
        Positive means the bet compounds the balance upward on average; zero is
        exactly what betting nothing achieves."""
        if stake <= 0:
            return 0.0
        remaining = bankroll - stake
        if remaining <= 0:
            return -inf  # never risk the whole balance
        won = remaining + stake * (pool + stake) / (side_points + stake)
        return probability * log(won / bankroll) + (1 - probability) * log(remaining / bankroll)

    def _growth_optimal_stake(
        self, pool: float, side_points: float, bankroll: int, probability: float, cap: float
    ) -> float:
        """Full-Kelly stake: the one maximising expected log wealth.

        Solved by ternary search rather than the textbook `p - q/b` formula,
        because here `b` is itself a function of the stake. That feedback makes
        the closed form oscillate instead of converge, and it is exactly what
        stops the bot from dumping its cap into a pool too thin to pay it back.
        """
        growth = partial(self._expected_log_growth, pool, side_points, bankroll, probability)
        low, high = 0.0, min(cap, bankroll * 0.99)
        for _ in range(60):  # ~1e-18 relative precision; expected log wealth is concave
            first = low + (high - low) / 3
            second = high - (high - low) / 3
            if growth(first) < growth(second):
                low = first
            else:
                high = second
        stake = (low + high) / 2
        return stake if growth(stake) > 0 else 0.0
