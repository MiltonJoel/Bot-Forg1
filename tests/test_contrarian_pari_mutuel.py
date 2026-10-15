import json
from datetime import datetime, timezone
from pathlib import Path

from app.models.prediction import Outcome, Prediction
from app.services.team_ratings import TeamRatings
from app.strategies.contrarian_pari_mutuel import ContrarianPariMutuelStrategy


def _prediction(
    *shares: tuple[str, int],
    balance: int = 20_000,
    min_bet: int = 500,
    max_bet: int = 50_000,
) -> Prediction:
    """Pools default to the real scale seen in the logs (millions of points
    against a ~20k balance), because stake-vs-pool ratio drives the pricing."""
    return Prediction(
        id="x",
        title="Quien gana el mapa?",
        outcomes=[
            Outcome(id=str(i), label=label, total_points=points, total_bettors=10)
            for i, (label, points) in enumerate(shares)
        ],
        min_bet=min_bet,
        max_bet=max_bet,
        locks_at=datetime.now(timezone.utc),
        available_points=balance,
        captured_at=datetime.now(timezone.utc),
    )


def test_backs_the_underdog_not_the_favourite() -> None:
    # The exact shape that drained the bankroll: a 90/10 pool. The favourite
    # pays 0.11x and needs a 90% win rate to break even; the underdog pays 9x.
    strategy = ContrarianPariMutuelStrategy(rating_weight=0.0, unrated_crowd_weight=0.12)
    decision = strategy.decide(_prediction(("Favorito", 9_000_000), ("Perdedor", 1_000_000)))

    assert decision.should_bet is True
    assert decision.side == "Perdedor"


def test_never_bets_a_market_that_cannot_pay() -> None:
    # 100% of the pool on one side: backing it pays exactly 0.000x, so the bet
    # can only lose. The old strategy staked its maximum here and lost 12,279.
    strategy = ContrarianPariMutuelStrategy(rating_weight=0.0, unrated_crowd_weight=0.12)
    decision = strategy.decide(_prediction(("Magic", 10_000_000), ("3DMAX", 0)))

    assert decision.side != "Magic"


def test_takes_the_minimum_when_kelly_wants_less_than_the_site_allows() -> None:
    # Nobody has backed 3DMAX, so it pays the entire pool — but winnings are
    # capped at that pool however much we add, meaning extra stake buys risk and
    # no upside and full Kelly lands below min_bet. Passing on a bet this good
    # would be wrong; the minimum is the right size.
    strategy = ContrarianPariMutuelStrategy(rating_weight=0.0, unrated_crowd_weight=0.12)
    decision = strategy.decide(_prediction(("Magic", 10_000_000), ("3DMAX", 0), min_bet=500))

    assert decision.should_bet is True
    assert decision.side == "3DMAX"
    assert decision.amount == 500


def test_sits_out_when_the_price_is_fair() -> None:
    # An even pool prices both sides at 1.0x. Shrinking a 50/50 crowd toward
    # uniform leaves p=0.5, so there is no edge either way.
    strategy = ContrarianPariMutuelStrategy(rating_weight=0.0, unrated_crowd_weight=0.12)
    decision = strategy.decide(_prediction(("A", 5_000_000), ("B", 5_000_000)))

    assert decision.should_bet is False
    assert decision.amount == 0
    assert "no edge" in decision.reason


def test_stake_respects_the_hard_cap() -> None:
    strategy = ContrarianPariMutuelStrategy(
        rating_weight=0.0, max_pct=25.0, unrated_crowd_weight=0.12
    )
    decision = strategy.decide(
        _prediction(("Favorito", 9_900_000), ("Perdedor", 100_000), balance=20_000)
    )

    assert decision.should_bet is True
    assert decision.amount <= 5000  # 25% of balance, never more


def test_stake_self_limits_in_a_pool_too_thin_to_pay_out() -> None:
    # Nobody has backed the underdog and the whole pool is tiny: betting the cap
    # would just be betting against ourselves, so the growth-optimal stake is a
    # small fraction of it. This is the case where the closed-form Kelly
    # oscillates and would have staked the cap.
    strategy = ContrarianPariMutuelStrategy(
        rating_weight=0.0, max_pct=25.0, unrated_crowd_weight=0.12
    )
    thin = strategy.decide(_prediction(("A", 900), ("B", 100), balance=20_000))
    deep = strategy.decide(_prediction(("A", 9_000_000), ("B", 1_000_000), balance=20_000))

    assert thin.amount < deep.amount


def test_multi_outcome_markets_are_skipped_by_default() -> None:
    # The crowd calibration was measured on head-to-head markets only; all four
    # kill-count bets in the logs lost.
    strategy = ContrarianPariMutuelStrategy(rating_weight=0.0, unrated_crowd_weight=0.12)
    decision = strategy.decide(
        _prediction(("1 a 3", 100_000), ("4 a 6", 200_000), ("7 a 9", 300_000), ("10+", 400_000))
    )

    assert decision.should_bet is False
    assert "4-way market" in decision.reason


def test_map_market_with_overtime_is_played_on_the_teams() -> None:
    # The real market from 2026-08-13, skipped as "3-way" and lost to the gate:
    # a head-to-head plus an OT side. It must be bet, and never on OT.
    strategy = ContrarianPariMutuelStrategy(rating_weight=0.0, unrated_crowd_weight=0.12)
    decision = strategy.decide(_prediction(("FaZe", 383_437), ("Legacy", 340_541), ("OT", 162_213)))

    assert decision.should_bet is True
    assert decision.side == "Legacy"  # the underdog of the two teams
    assert decision.amount >= 500


def test_overtime_is_priced_at_the_pool_so_it_carries_no_edge() -> None:
    # The trap this exists to avoid: shrunk toward a 1/3 uniform prior like a
    # third team, OT's 18% share would read as ~31% and pay 4.5x — a fake +0.7
    # edge that would put the cap on overtime every single map. At weight 1.0 it
    # is priced exactly where the pool prices it, which is zero edge, and the
    # teams split only what OT leaves (a team bet loses when the map goes to OT).
    strategy = ContrarianPariMutuelStrategy(rating_weight=0.0, unrated_crowd_weight=0.12)
    market = _prediction(("FaZe", 383_437), ("Legacy", 340_541), ("OT", 162_213))

    faze, legacy, overtime = strategy._probabilities(market)

    assert abs(overtime - 162_213 / 886_191) < 1e-9
    assert abs(faze + legacy - (1 - overtime)) < 1e-9
    assert faze > legacy  # ordering survives the shrink; FaZe holds more of the pool

    reckless = ContrarianPariMutuelStrategy(
        rating_weight=0.0, unrated_crowd_weight=0.12, ot_crowd_weight=0.12
    )
    assert reckless.decide(market).side == "OT"  # the knob works, and why it defaults to 1.0


def test_map_market_with_two_overtime_buckets_is_played_on_the_teams() -> None:
    # The real market from 2026-08-30, in the format forg1 started running on
    # 2026-08-28: overtime split into "OT" and "OT 2 o +". Four outcomes, so the
    # old len==3 shape test rejected it and the bot skipped the market — 18% of
    # map markets in the four days after the change. Two teams plus any number
    # of overtime buckets is still a head-to-head.
    strategy = ContrarianPariMutuelStrategy(rating_weight=0.0, unrated_crowd_weight=0.12)
    decision = strategy.decide(
        _prediction(
            ("Aurora", 6_500_000),
            ("M80", 2_400_000),
            ("OT", 600_000),
            ("OT 2 o +", 500_000),
        )
    )

    assert decision.should_bet is True
    assert decision.side == "M80"  # the underdog of the two teams, never an OT bucket


def test_several_overtime_buckets_are_summed_into_one_no_overtime_term() -> None:
    # The buckets are mutually exclusive ways for the same event to happen, so
    # what the teams split is 1 - P(any overtime). Pricing each bucket against a
    # 1/4 uniform prior and letting the teams keep the rest would double-count.
    strategy = ContrarianPariMutuelStrategy(rating_weight=0.0, unrated_crowd_weight=0.12)
    market = _prediction(
        ("Falcons", 4_200_000),
        ("Mouz", 3_800_000),
        ("OT", 1_500_000),
        ("OT 2 o +", 500_000),
    )

    falcons, mouz, ot, ot2 = strategy._probabilities(market)

    # At ot_crowd_weight=1.0 each bucket sits exactly on its own pool share.
    assert abs(ot - 1_500_000 / 10_000_000) < 1e-9
    assert abs(ot2 - 500_000 / 10_000_000) < 1e-9
    assert abs(falcons + mouz - (1 - ot - ot2)) < 1e-9
    assert falcons > mouz  # ordering survives the shrink


def test_a_four_way_kill_count_market_is_still_skipped() -> None:
    # The exemption is "two teams plus overtime", not "four outcomes". A kill
    # count has no overtime side and no model behind it, so it stays out.
    strategy = ContrarianPariMutuelStrategy(rating_weight=0.0, unrated_crowd_weight=0.12)
    decision = strategy.decide(
        _prediction(
            ("9 o menos", 400_000),
            ("10 a 14", 300_000),
            ("15 19", 200_000),
            ("20 o mas", 100_000),
        )
    )

    assert decision.should_bet is False
    assert "4-way market" in decision.reason


def test_a_team_whose_name_begins_with_ot_is_not_read_as_overtime() -> None:
    # The prefix match is guarded by \b for this: "OTB" is a team, "OT 2" is not.
    strategy = ContrarianPariMutuelStrategy(rating_weight=0.0, unrated_crowd_weight=0.12)
    decision = strategy.decide(_prediction(("OTB", 400_000), ("LP", 300_000), ("Empate", 100_000)))

    assert decision.should_bet is False
    assert "3-way market" in decision.reason


def test_a_third_option_that_is_not_overtime_is_still_skipped() -> None:
    # The exemption is for Team/Team/OT only. Anything else three-way has no
    # model behind it and stays out.
    strategy = ContrarianPariMutuelStrategy(rating_weight=0.0, unrated_crowd_weight=0.12)
    decision = strategy.decide(
        _prediction(("Bestia", 400_000), ("LP", 300_000), ("Empate", 100_000))
    )

    assert decision.should_bet is False
    assert "3-way market" in decision.reason


def test_overtime_market_still_uses_the_ratings_for_the_two_teams(tmp_path: Path) -> None:
    # The rating blend used to require exactly two outcomes, so an OT market
    # would have thrown Valve's standings away. It applies to the head-to-head
    # inside it: the pool calls Spirit/Shinden even, Valve does not.
    path = tmp_path / "ratings.json"
    path.write_text(json.dumps({"teams": {"Spirit": 1993, "Shinden": 1118}}))
    strategy = ContrarianPariMutuelStrategy(ratings=TeamRatings(path, scale=800.0))

    decision = strategy.decide(
        _prediction(("Spirit", 4_000_000), ("Shinden", 4_000_000), ("OT", 2_000_000))
    )

    assert decision.should_bet is True
    assert decision.side == "Spirit"


def test_standing_order_backs_the_team_at_any_price() -> None:
    # Owner's decision: 9z always gets the bet. Here they are the 90% favourite
    # the strategy exists to refuse — pays 0.11x, needs a 90% win rate just to
    # break even — so it bets the site minimum instead of the Kelly cap.
    strategy = ContrarianPariMutuelStrategy(
        rating_weight=0.0, unrated_crowd_weight=0.12, always_back=("9z",)
    )
    decision = strategy.decide(_prediction(("9z Globant", 9_000_000), ("Rival", 1_000_000)))

    assert decision.should_bet is True
    assert decision.side == "9z Globant"  # not "Rival", which the model prefers
    assert decision.amount == 500
    assert "standing order" in decision.reason


def test_standing_order_still_sizes_by_kelly_when_the_price_is_good() -> None:
    strategy = ContrarianPariMutuelStrategy(
        rating_weight=0.0, unrated_crowd_weight=0.12, always_back=("9z",)
    )
    decision = strategy.decide(_prediction(("Favorito", 9_000_000), ("9z", 1_000_000)))

    assert decision.side == "9z"
    assert decision.amount > 500  # underdog price, so the full stake applies


def test_standing_order_does_not_match_a_different_team() -> None:
    # Same trap the ratings matcher guards: "magic" must not catch "MAGICOS".
    strategy = ContrarianPariMutuelStrategy(
        rating_weight=0.0, unrated_crowd_weight=0.12, always_back=("Magic",)
    )
    decision = strategy.decide(_prediction(("MAGICOS", 9_000_000), ("Otro", 1_000_000)))

    assert decision.side == "Otro"


def test_no_standing_order_means_the_favourite_is_refused() -> None:
    # The shipped state since 2026-08-16: STRATEGY_ALWAYS_BACK is empty, so the
    # 90% favourite gets no exemption and the edge gate picks the underdog.
    strategy = ContrarianPariMutuelStrategy(
        rating_weight=0.0, unrated_crowd_weight=0.12, always_back=()
    )
    decision = strategy.decide(_prediction(("9z Globant", 9_000_000), ("Rival", 1_000_000)))

    assert decision.side == "Rival"
    assert "standing order" not in decision.reason


def test_blank_standing_order_entry_matches_nothing() -> None:
    # The footgun in turning the feature off: "".split(",") is [""], and an
    # entry that matched every label would silently exempt EVERY bet from the
    # edge gate — the exact opposite of switching it off. main.py filters the
    # blank out, and the matcher must not match it either.
    strategy = ContrarianPariMutuelStrategy(
        rating_weight=0.0, unrated_crowd_weight=0.12, always_back=("",)
    )
    decision = strategy.decide(_prediction(("9z Globant", 9_000_000), ("Rival", 1_000_000)))

    assert decision.side == "Rival"
    assert "standing order" not in decision.reason


def test_empty_pool_is_not_priceable() -> None:
    strategy = ContrarianPariMutuelStrategy(rating_weight=0.0, unrated_crowd_weight=0.12)
    decision = strategy.decide(_prediction(("A", 0), ("B", 0)))

    assert decision.should_bet is False
    assert "empty pool" in decision.reason


def test_balance_below_min_bet_skips() -> None:
    strategy = ContrarianPariMutuelStrategy(rating_weight=0.0, unrated_crowd_weight=0.12)
    decision = strategy.decide(_prediction(("A", 9_000_000), ("B", 1_000_000), balance=100))

    assert decision.should_bet is False


def test_rating_prior_moves_the_probability(tmp_path: Path) -> None:
    path = tmp_path / "ratings.json"
    path.write_text(json.dumps({"teams": {"Spirit": 1993, "Shinden": 1118}}))
    ratings = TeamRatings(path, scale=800.0)

    # Pool says the two are even; Valve says Spirit is far stronger.
    prediction = _prediction(("Spirit", 5_000_000), ("Shinden", 5_000_000))
    informed = ContrarianPariMutuelStrategy(
        rating_weight=0.9, ratings=ratings, crowd_weight=0.12
    ).decide(prediction)
    blind = ContrarianPariMutuelStrategy(rating_weight=0.0, unrated_crowd_weight=0.12).decide(
        prediction
    )

    assert blind.should_bet is False  # fair price, no opinion
    assert informed.should_bet is True
    assert informed.side == "Spirit"  # the rating prior supplies the edge


def test_unrated_teams_fall_back_to_pool_pricing(tmp_path: Path) -> None:
    path = tmp_path / "ratings.json"
    path.write_text(json.dumps({"teams": {"Spirit": 1993}}))
    ratings = TeamRatings(path, scale=800.0)
    strategy = ContrarianPariMutuelStrategy(rating_weight=0.9, ratings=ratings, crowd_weight=0.12)

    # "Brasil"/"Argentina" are not in Valve's standings — must not crash, and
    # must price off the pool alone.
    decision = strategy.decide(_prediction(("Brasil", 9_000_000), ("Argentina", 1_000_000)))
    assert decision.side == "Argentina"


def test_unrated_markets_are_bet_at_full_aggression_by_default(tmp_path: Path) -> None:
    # Owner's decision: kill counts, player names and one-off questions get the
    # same aggression as ranked matches. Backing off costs too much growth
    # (+152% vs +27% on a replay of the logged markets).
    path = tmp_path / "ratings.json"
    path.write_text(json.dumps({"teams": {"Spirit": 1993, "FaZe": 1404}}))
    strategy = ContrarianPariMutuelStrategy(ratings=TeamRatings(path, scale=800.0))

    decision = strategy.decide(_prediction(("Largados y Pelados", 3_350_000), ("Otro", 6_650_000)))

    assert decision.should_bet is True
    assert decision.amount > 1000


def test_unrated_weight_can_be_calmed_without_touching_team_bets(tmp_path: Path) -> None:
    # The two knobs are separate on purpose: the 0.12 calibration was measured
    # on CS2 maps only, so raising the unrated one alone must shrink non-team
    # stakes while leaving ranked matches exactly as they were.
    path = tmp_path / "ratings.json"
    path.write_text(json.dumps({"teams": {"Spirit": 1993, "Shinden": 1118}}))
    ratings = TeamRatings(path, scale=800.0)
    unrated = _prediction(("Largados y Pelados", 3_350_000), ("Otro", 6_650_000))
    ranked = _prediction(("Spirit", 9_000_000), ("Shinden", 1_000_000))

    hot = ContrarianPariMutuelStrategy(ratings=ratings)
    calm = ContrarianPariMutuelStrategy(unrated_crowd_weight=0.9, ratings=ratings)

    assert calm.decide(unrated).amount < hot.decide(unrated).amount
    assert calm.decide(ranked).amount == hot.decide(ranked).amount


def test_ranked_match_backs_the_rating_against_the_pool(tmp_path: Path) -> None:
    # The case the ratings exist for: the pool prices Spirit/Shinden as a
    # coin flip, Valve says Spirit is far stronger -> bet Spirit, the FAVOURITE
    # by rating. Nothing contrarian about it; the ranking simply disagrees.
    path = tmp_path / "ratings.json"
    path.write_text(json.dumps({"teams": {"Spirit": 1993, "Shinden": 1118}}))
    strategy = ContrarianPariMutuelStrategy(ratings=TeamRatings(path, scale=800.0))

    decision = strategy.decide(_prediction(("Spirit", 5_000_000), ("Shinden", 5_000_000)))

    assert decision.should_bet is True
    assert decision.side == "Spirit"


def test_crowd_weight_one_means_the_pool_is_a_fair_price(tmp_path: Path) -> None:
    # The escape hatch, and the proof the contrarian tilt is a CHOICE and not
    # hardcoded: at 1.0 the crowd share is taken at face value, Valve agrees
    # Falcons are heavy favourites, so no edge exists and it sits out.
    path = tmp_path / "ratings.json"
    path.write_text(json.dumps({"teams": {"Falcons": 1988, "100 Thieves": 1251}}))
    ratings = TeamRatings(path, scale=800.0)
    market = _prediction(("Falcons", 8_850_000), ("100 Thieves", 1_150_000))

    assert (
        ContrarianPariMutuelStrategy(crowd_weight=1.0, ratings=ratings).decide(market).should_bet
        is False
    )
    # ...while the shipped 0.12 takes the 7.7x price on the underdog.
    assert ContrarianPariMutuelStrategy(ratings=ratings).decide(market).should_bet is True


def test_ranked_match_backs_the_underdog_when_the_pool_overrates_the_favourite(
    tmp_path: Path,
) -> None:
    # Spirit and FaZe are much closer than a 90/10 pool implies, so the ranking
    # says FaZe is badly underpriced at 9x. Contrarian here is informed, not an
    # assumption about the crowd.
    path = tmp_path / "ratings.json"
    path.write_text(json.dumps({"teams": {"Spirit": 1993, "FaZe": 1404}}))
    strategy = ContrarianPariMutuelStrategy(ratings=TeamRatings(path, scale=800.0))

    decision = strategy.decide(_prediction(("Spirit", 9_000_000), ("FaZe", 1_000_000)))

    assert decision.should_bet is True
    assert decision.side == "FaZe"


def test_name_matching_is_exact_enough_to_be_safe(tmp_path: Path) -> None:
    path = tmp_path / "ratings.json"
    path.write_text(
        json.dumps({"teams": {"LP": 1103, "LPH": 424, "The MongolZ": 1644, "9z": 1684}})
    )
    ratings = TeamRatings(path, scale=800.0)

    assert ratings.rating("LP") == 1103  # not confused with LPH
    assert ratings.rating("LPH") == 424
    assert ratings.rating("The Mongolz") == 1644  # case + leading "the"
    assert ratings.rating("9z Globant") == 1684  # sponsor suffix forg1 adds
    assert ratings.rating("Vitality") is None
