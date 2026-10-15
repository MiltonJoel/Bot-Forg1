"""Recovering which CS2 map a forg1 market is about.

A wrong map is worse than no map — it prices one map's form onto another — so
every case that isn't certain has to come back None.
"""

from app.services.map_resolver import (
    map_in_title,
    map_number_in_title,
    match_for,
    resolve_stored_map,
)


def game(at: str, map_name: str, winner: str, loser: str, number: int) -> dict[str, object]:
    return {"at": at, "map": map_name, "w": winner, "l": loser, "ws": 13, "ls": 7, "n": number}


# --- titles ------------------------------------------------------------------


def test_a_title_that_names_the_map() -> None:
    assert map_in_title("Quien gana el INFERNO?") == "inferno"
    assert map_in_title("Quien gana el 2do MAPA?") is None


def test_map_ordinals_as_forg1_actually_writes_them() -> None:
    assert map_number_in_title("Quien gana el 1er MAPA?") == 1
    assert map_number_in_title("Quien gana el 2do MAPA?") == 2
    assert map_number_in_title("Quien gana el 3er MAPA?") == 3
    assert map_number_in_title("Quien gana el 4to MAPA?") == 4
    assert map_number_in_title("Cuantas kills DONK? MAPA 2") == 2


def test_titles_with_no_map_at_all() -> None:
    """'OT' is a market about overtime, not about map 1, and a bare 'GANA EL
    MAPA?' names no ordinal. Both must stay unresolved."""
    assert map_number_in_title("Quien gana el OT?") is None
    assert map_number_in_title("M80 GANA EL MAPA?") is None
    assert map_number_in_title("Y AHORA?") is None


# --- matching a bo3 slug ------------------------------------------------------


def test_both_teams_have_to_line_up() -> None:
    assert match_for("spirit-vs-g2-28-08-2026", ("Spirit", "G2"))
    assert match_for("spirit-vs-g2-28-08-2026", ("G2", "Spirit"))  # either orientation
    # Same Spirit, different opponent playing at the same moment.
    assert not match_for("spirit-vs-faze-28-08-2026", ("Spirit", "G2"))


def test_suffixes_bridge_but_short_names_must_be_exact() -> None:
    assert match_for("sparta-vs-inner-circle-academy-28-08-2026", ("Inner Circle", "Sparta"))
    # 'LP' inside 'LPH' is the exact confusion TeamRatings refuses to make.
    assert not match_for("lph-vs-beta-28-08-2026", ("LP", "Beta"))


def test_a_slug_that_is_not_a_match_is_rejected() -> None:
    assert not match_for("some-tournament-page", ("Spirit", "G2"))


# --- recovering a finished market's map --------------------------------------

HISTORY = [
    game("2026-08-27", "de_nuke", "Spirit", "G2", 1),
    game("2026-08-27", "de_inferno", "G2", "Spirit", 2),
    game("2026-08-27", "de_mirage", "Falcons", "MOUZ", 1),
]


def test_the_ordinal_picks_the_map_out_of_the_series() -> None:
    assert resolve_stored_map(HISTORY, "2026-08-27", ("Spirit", "G2"), 1) == "nuke"
    assert resolve_stored_map(HISTORY, "2026-08-27", ("Spirit", "G2"), 2) == "inferno"


def test_a_series_that_straddles_midnight_still_resolves() -> None:
    assert resolve_stored_map(HISTORY, "2026-08-28", ("Spirit", "G2"), 1) == "nuke"


def test_ambiguity_returns_none_rather_than_a_guess() -> None:
    """Without an ordinal, two maps between the same teams are two answers."""
    assert resolve_stored_map(HISTORY, "2026-08-27", ("Spirit", "G2"), None) is None


def test_teams_that_did_not_play_resolve_to_none() -> None:
    assert resolve_stored_map(HISTORY, "2026-08-27", ("Spirit", "Falcons"), 1) is None
    assert resolve_stored_map(HISTORY, "2026-08-20", ("Spirit", "G2"), 1) is None
