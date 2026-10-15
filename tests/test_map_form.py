"""MapForm: the Elo built from bo3.gg map results.

Fakes are plain dicts in the shape app/services/bo3.py::trim writes, so these
tests break if that shape changes — which is the point.
"""

from pathlib import Path

import pytest

from app.services.match_history import MapForm, normalize_map

MISSING = Path(".data/does-not-exist-for-tests.json")


def game(at: str, map_name: str, winner: str, loser: str, number: int = 1) -> dict[str, str]:
    return {"at": at, "map": map_name, "w": winner, "l": loser, "ws": 13, "ls": 7, "n": number}  # type: ignore[dict-item]


def repeated(winner: str, loser: str, times: int, map_name: str = "de_mirage") -> list[dict]:
    return [game(f"2026-01-{d + 1:02d}", map_name, winner, loser) for d in range(times)]


def test_normalize_map_strips_the_de_prefix() -> None:
    assert normalize_map("de_inferno") == "inferno"
    assert normalize_map("INFERNO") == "inferno"


def test_missing_history_disables_the_model_instead_of_guessing() -> None:
    form = MapForm(MISSING)
    assert form.win_probability("Spirit", "G2") is None
    assert form.rating("Spirit") is None


def test_the_team_that_keeps_winning_is_favoured() -> None:
    form = MapForm(MISSING, games=repeated("Alpha", "Beta", 20), min_games=5)
    probability = form.win_probability("Alpha", "Beta")
    assert probability is not None and probability > 0.7
    # And the mirror image agrees, or the model is not a probability at all.
    mirrored = form.win_probability("Beta", "Alpha")
    assert mirrored == pytest.approx(1 - probability)


def test_a_team_with_too_little_history_gets_no_number() -> None:
    """An Elo built from three games is noise wearing a number. Better to say
    nothing and let the strategy price off the pool."""
    form = MapForm(
        MISSING,
        games=repeated("Alpha", "Beta", 20) + [game("2026-02-01", "de_nuke", "Alpha", "Newcomer")],
        min_games=10,
    )
    assert form.win_probability("Alpha", "Beta") is not None
    assert form.win_probability("Alpha", "Newcomer") is None


def test_unknown_teams_resolve_to_none() -> None:
    form = MapForm(MISSING, games=repeated("Alpha", "Beta", 20), min_games=5)
    assert form.win_probability("Alpha", "Nobody At All") is None
    assert form.win_probability("Alpha", "Alpha") is None


def test_map_specific_form_moves_the_price() -> None:
    """Alpha beats Beta everywhere but loses to them on Nuke. Asking about Nuke
    has to come back lower than asking in general, or k_map is doing nothing."""
    games = repeated("Alpha", "Beta", 20, "de_mirage") + [
        game(f"2026-02-{d + 1:02d}", "de_nuke", "Beta", "Alpha") for d in range(10)
    ]
    form = MapForm(MISSING, games=games, min_games=5, k_map=16.0)
    overall = form.win_probability("Alpha", "Beta")
    on_nuke = form.win_probability("Alpha", "Beta", "de_nuke")
    assert overall is not None and on_nuke is not None
    assert on_nuke < overall


def test_k_map_zero_makes_the_map_irrelevant() -> None:
    games = repeated("Alpha", "Beta", 20, "de_mirage") + [
        game(f"2026-02-{d + 1:02d}", "de_nuke", "Beta", "Alpha") for d in range(10)
    ]
    form = MapForm(MISSING, games=games, min_games=5, k_map=0.0)
    assert form.win_probability("Alpha", "Beta") == form.win_probability("Alpha", "Beta", "de_nuke")


def test_an_ambiguous_first_token_is_never_resolved() -> None:
    """'LP' and 'LPH' are different teams. TeamRatings refuses to guess between
    them and so must this, or the model rates one org as another."""
    games = repeated("LP", "Beta", 20) + repeated("LPH", "Beta", 20, "de_nuke")
    form = MapForm(MISSING, games=games, min_games=5)
    assert form.win_probability("LP", "Beta") is not None  # exact name still works
    assert form.win_probability("LPX", "Beta") is None  # a token shared by two orgs


def test_a_sponsor_suffix_still_matches() -> None:
    """forg1 writes '9z Globant' where bo3 may write '9z' — an unambiguous
    first token is allowed to bridge that."""
    form = MapForm(MISSING, games=repeated("9z", "Beta", 20), min_games=5)
    assert form.win_probability("9z Globant", "Beta") is not None


def test_fit_is_chronological_regardless_of_input_order() -> None:
    """Ratings must depend on when maps were played, not on the order rows
    happen to sit in the file — that ordering is what makes a snapshot at any
    date free of hindsight."""
    games = repeated("Alpha", "Beta", 12)
    forwards = MapForm(MISSING, games=games, min_games=5).win_probability("Alpha", "Beta")
    backwards = MapForm(MISSING, games=list(reversed(games)), min_games=5).win_probability(
        "Alpha", "Beta"
    )
    assert forwards == backwards


def test_record_after_only_scores_later_maps_and_never_peeks() -> None:
    """The prequential score the fit script reads: every recorded prediction is
    made before that map updates the ratings."""
    games = repeated("Alpha", "Beta", 20)
    form = MapForm(MISSING, games=[], min_games=5)
    recorded = form.fit(games, record_after="2026-01-10")
    assert 0 < len(recorded) < len(games)
    assert all(0.0 < p < 1.0 for p in recorded)
