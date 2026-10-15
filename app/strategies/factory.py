"""Builds the live strategy from Settings, in one place.

main.py and scripts/dry_run.py both need it, and the dry run used to build its
own copy that skipped half the dials (unrated weight, OT weight, the form model,
the form-edge filter), so it printed stakes the live bot would never place.
"""

from loguru import logger

from app.config.settings import Settings
from app.services.counter_predictor import CounterPredictor
from app.services.match_history import MapForm
from app.services.team_ratings import TeamRatings
from app.strategies.contrarian_pari_mutuel import ContrarianPariMutuelStrategy
from app.utils.retry import short_error


def build_form_model(settings: Settings) -> CounterPredictor | MapForm:
    """CounterPredictor when its export exists and loads, MapForm otherwise.

    A truncated or stale-schema export (an interrupted or outdated export) used to
    crash the bot at startup; it now falls back to MapForm with a loud log line.
    """
    try:
        counter = CounterPredictor.from_file(
            settings.counterpredictor_path, settings.strategy_form_min_games
        )
    except (ValueError, KeyError, TypeError, OSError) as exc:
        logger.error(
            "Unreadable CounterPredictor export at {} ({}); falling back to MapForm.",
            settings.counterpredictor_path,
            short_error(exc),
        )
        counter = None
    if counter is not None:
        return counter
    return MapForm(
        settings.match_history_path,
        scale=settings.strategy_form_scale,
        k_global=settings.strategy_form_k_global,
        k_map=settings.strategy_form_k_map,
        min_games=settings.strategy_form_min_games,
    )


def build_strategy(settings: Settings) -> ContrarianPariMutuelStrategy:
    return ContrarianPariMutuelStrategy(
        crowd_weight=settings.strategy_crowd_weight,
        kelly_fraction=settings.strategy_kelly_fraction,
        max_pct=settings.strategy_max_pct,
        min_edge=settings.strategy_min_edge,
        rating_weight=settings.strategy_rating_weight,
        max_outcomes=settings.strategy_max_outcomes,
        unrated_crowd_weight=settings.strategy_unrated_crowd_weight,
        ot_crowd_weight=settings.strategy_ot_crowd_weight,
        always_back=tuple(
            name.strip() for name in settings.strategy_always_back.split(",") if name.strip()
        ),
        ratings=TeamRatings(settings.team_ratings_path, settings.strategy_rating_scale),
        # Built even when form_weight is 0 so the log line reports what the
        # model WOULD know.
        form=build_form_model(settings),
        form_weight=settings.strategy_form_weight,
        min_form_edge=settings.strategy_min_form_edge,
        fav_fallback_pct=settings.strategy_fav_fallback_pct,
    )
