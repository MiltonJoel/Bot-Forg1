"""The live strategy is built in one place (main.py and dry_run.py share it)."""

import json
from pathlib import Path

from app.config.settings import Settings
from app.services.counter_predictor import CounterPredictor
from app.services.kick_service import KickLiveChecker
from app.services.match_history import MapForm
from app.strategies.factory import build_form_model, build_strategy
from tests.test_counter_predictor import SNAPSHOT


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        telegram_bot_token="x",
        counterpredictor_path=tmp_path / "counterpredictor.json",
        match_history_path=tmp_path / "missing_history.json",
        team_ratings_path=tmp_path / "missing_ratings.json",
        **overrides,
    )


def test_counterpredictor_export_wins_when_present(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.counterpredictor_path.write_text(json.dumps(SNAPSHOT))
    assert isinstance(build_form_model(settings), CounterPredictor)


def test_truncated_export_falls_back_to_mapform_instead_of_crashing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.counterpredictor_path.write_text(json.dumps(SNAPSHOT)[:200])
    assert isinstance(build_form_model(settings), MapForm)


def test_export_missing_a_key_falls_back_to_mapform(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.counterpredictor_path.write_text(json.dumps({"teams": {}}))
    assert isinstance(build_form_model(settings), MapForm)


def test_strategy_gets_every_dial(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        strategy_unrated_crowd_weight=0.85,
        strategy_form_weight=0.3,
        strategy_min_form_edge=0.05,
        strategy_always_back=" 9z , ,FURIA",
    )
    strategy = build_strategy(settings)
    assert strategy._unrated_crowd_weight == 0.85
    assert strategy._form_weight == 0.3
    assert strategy._min_form_edge == 0.05
    assert strategy._always_back == ("9z", "FURIA")


def test_unknown_kick_state_is_not_offline() -> None:
    checker = KickLiveChecker("forg1", 30, notifier=None)  # type: ignore[arg-type]
    assert not checker.is_offline  # no successful check yet
    checker._was_live = False
    assert checker.is_offline
    checker._was_live = True
    assert not checker.is_offline
