from datetime import datetime

from app.services.counter_predictor import CounterPredictor


def _team(elo: float, n: int = 50) -> dict[str, object]:
    return {
        "elo": elo,
        "form": 0.5,
        "rdiff": 0.0,
        "kd": 1.0,
        "adr": 75.0,
        "kast": 70.0,
        "stable": 5.0,
        "n": n,
        "last": "2026-10-01T00:00:00",
        "map_elo": {"Mirage": elo + 50},
        "n_map": {"Mirage": 10},
    }


SNAPSHOT = {
    "as_of": "2026-10-06T00:00:00",
    "active_maps": ["Mirage", "Nuke"],
    "teams": {
        "Team Vitality": _team(1700),
        "Natus Vincere": _team(1600),
        "9z Team": _team(1500),
        "9z Academy": _team(1400, n=12),
        "Thin": _team(1500, n=3),
    },
    "h2h": {},
    # elo diff only, scaled so 100 Elo points = 1 logit
    "model": {
        "mean": [0.0] * 15,
        "scale": [100.0] + [1.0] * 14,
        "coef": [1.0] + [0.0] * 14,
        "intercept": [0.0],
    },
}
WHEN = datetime(2026, 10, 7)


def test_names_symmetry_and_unknowns() -> None:
    cp = CounterPredictor(SNAPSHOT)  # type: ignore[arg-type]
    p = cp.win_probability("Vit", "NAVI", "de_mirage", WHEN)
    assert p is not None and abs(p - 1 / (1 + 2.718281828**-1)) < 1e-6
    q = cp.win_probability("NAVI", "Vit", "MIRAGE", WHEN)
    assert q is not None and abs(p + q - 1) < 1e-12
    assert cp.team("9z Globant") == "9z Team"  # sponsor suffix, academy not confused
    assert cp.team("FaZe") is None  # unknown
    assert cp.win_probability("Thin", "NAVI", None, WHEN) is None  # under min_games
    assert cp.win_probability("Vitality", "Vitality", None, WHEN) is None
