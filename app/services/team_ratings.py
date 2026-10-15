import json
import re
from math import pow
from pathlib import Path

from loguru import logger

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _normalize(name: str) -> str:
    """'The MongolZ' -> 'mongolz'. Drops case, punctuation and a leading 'the'
    so forg1's labels line up with Valve's spelling."""
    flat = _NON_ALNUM.sub("", name.lower())
    return flat[3:] if flat.startswith("the") and len(flat) > 3 else flat


def _first_token(name: str) -> str:
    """'9z Globant' -> '9z'. Forg1 often carries a sponsor suffix Valve omits."""
    parts = _NON_ALNUM.sub(" ", name.lower()).split()
    return parts[0] if parts else ""


class TeamRatings:
    """Official Valve CS2 standings points, used as a prior on who actually wins.

    Refreshed by scripts/fetch_team_ratings.py. Missing file, missing team, or a
    non-team market (kill counts, "Brasil" vs "Argentina") all resolve to None,
    and the strategy falls back to pricing off the pool alone.
    """

    def __init__(self, ratings_path: Path, scale: float = 800.0) -> None:
        self._scale = scale
        self._by_name: dict[str, int] = {}
        self._by_token: dict[str, int] = {}
        self._load(ratings_path)

    def _load(self, path: Path) -> None:
        if not path.exists():
            logger.warning(
                "No team ratings at {} — betting on pool price alone. "
                "Run scripts/fetch_team_ratings.py to enable the rating prior.",
                path,
            )
            return
        try:
            teams: dict[str, int] = json.loads(path.read_text())["teams"]
        except (json.JSONDecodeError, OSError, KeyError) as exc:
            logger.error("Unreadable team ratings at {} ({}); ignoring them.", path, exc)
            return

        self._by_name = {_normalize(name): points for name, points in teams.items()}
        # Only keep first-token keys that are UNAMBIGUOUS: "LP" and "LPH" are
        # different teams, and a sloppy prefix match would silently rate one as
        # the other. Same for "magic" vs "MAGICOS".
        counts: dict[str, int] = {}
        for name in teams:
            counts[_first_token(name)] = counts.get(_first_token(name), 0) + 1
        self._by_token = {
            _first_token(name): points
            for name, points in teams.items()
            if counts[_first_token(name)] == 1
        }
        logger.info("Loaded {} official team ratings from {}.", len(teams), path)

    def rating(self, label: str) -> int | None:
        """Valve standings points for a forg1 outcome label, or None."""
        if not self._by_name:
            return None
        normalized = _normalize(label)
        if normalized in self._by_name:
            return self._by_name[normalized]
        # "9z Globant" -> "9z". Only fires when the token maps to exactly one team.
        return self._by_token.get(_first_token(label))

    def win_probability(self, label: str, opponent: str) -> float | None:
        """P(label beats opponent) from the ratings gap, or None if either team
        is unrated. Elo-style logistic; `scale` is how many points a 10:1
        favourite is ahead by."""
        mine, theirs = self.rating(label), self.rating(opponent)
        if mine is None or theirs is None:
            return None
        return 1.0 / (1.0 + pow(10.0, -(mine - theirs) / self._scale))
