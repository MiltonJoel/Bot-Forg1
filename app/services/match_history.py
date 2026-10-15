"""MapForm — how strong a team is, and how strong it is *on this map*.

This is the part of TaiZo1/cs2-win-prediction that survives contact with what
this bot actually does. That project predicts ROUND winners from the freeze-time
state of a round in progress (economy, weapons, utility, momentum) parsed out of
.dem replays. None of those features exist at our decision time: forg1 opens
"Quien gana el 2do MAPA?" before the map is decided, and we bet from an RSC
payload, not a demo. Its round-level model cannot be ported.

What DOES port is its actual method, which is the reason it works at all:

  * team strength measured per MAP, not globally — a side's Inferno is not its
    Nuke, and forg1 settles map by map;
  * a strict date cutoff so a match is never predicted with statistics that
    already contain it (their `endDate = D - 1`);
  * map-specific numbers falling back to the team's global number when the
    per-map sample is thin (their "map -> global" NaN rule), here as shrinkage
    rather than a hard fallback, which is the same idea with a soft edge;
  * a temporal holdout for validation (scripts/fit_map_form.py).

Implementation is two-level Elo over bo3.gg map results, in one chronological
pass so the ratings at any date depend only on maps played before it:

    R(team, map) = global[team] + map_weight * delta[map, team]

`global` moves on every map a team plays; `delta` moves only on that map, and
starts at 0, so a team with no Inferno history is simply predicted at its global
strength. Every constant is fitted by scripts/fit_map_form.py against held-out
maps, never guessed — the same discipline STRATEGY_RATING_SCALE was fitted with.

Deliberately NOT modelled (ponytail: each is a real upgrade, none is free):
margin of victory (13-10 counts the same as 13-2), roster changes, event tier,
and online-vs-LAN. Add one only if scripts/fit_map_form.py says it lowers
held-out log loss.
"""

import json
from math import exp
from pathlib import Path

from loguru import logger

from app.services.team_ratings import _first_token, _normalize

START = 1500.0


def normalize_map(name: str) -> str:
    """'de_inferno' -> 'inferno'. forg1 writes 'INFERNO', bo3 writes 'de_inferno'."""
    flat = name.strip().lower()
    return flat[3:] if flat.startswith("de_") else flat


class MapForm:
    """Two-level Elo over bo3.gg's finished maps.

    Missing file, unknown team, or a team with too little history all resolve to
    None, exactly like TeamRatings — the strategy then prices off the pool alone
    rather than inventing a number.
    """

    def __init__(
        self,
        history_path: Path,
        scale: float = 400.0,
        k_global: float = 24.0,
        k_map: float = 8.0,
        map_weight: float = 1.0,
        min_games: int = 10,
        games: list[dict[str, str]] | None = None,
    ) -> None:
        self._scale = scale
        self._k_global = k_global
        self._k_map = k_map
        self._map_weight = map_weight
        self._min_games = min_games
        self._global: dict[str, float] = {}
        self._delta: dict[tuple[str, str], float] = {}
        self._played: dict[str, int] = {}
        self._by_token: dict[str, str] = {}
        self._games = 0
        # `games` bypasses the file so scripts/fit_map_form.py can sweep
        # hundreds of parameter cells against the SHIPPING Elo, not a copy of
        # it that could quietly drift out of step with this one.
        if games is None:
            self._load(history_path)
        else:
            self.fit(games)

    # --- fitting -------------------------------------------------------------

    def _load(self, path: Path) -> None:
        if not path.exists():
            logger.warning(
                "No match history at {} — MapForm disabled, pricing off the pool and "
                "Valve ratings alone. Run scripts/fetch_match_history.py to enable it.",
                path,
            )
            return
        try:
            games: list[dict[str, str]] = json.loads(path.read_text())["games"]
        except (json.JSONDecodeError, OSError, KeyError) as exc:
            logger.error("Unreadable match history at {} ({}); ignoring it.", path, exc)
            return
        self.fit(games)
        logger.info(
            "MapForm fitted on {:,} maps ({:,} teams) from {}.",
            self._games,
            len(self._global),
            path,
        )

    def fit(self, games: list[dict[str, str]], record_after: str | None = None) -> list[float]:
        """One chronological pass. Sorting by date here is what makes the
        ratings honest: every update uses only maps that had already been
        played, so a snapshot taken at any point is free of hindsight.

        With `record_after` set it also returns P(the team that actually won)
        for every map after that date, predicted BEFORE that map updated the
        ratings. That is a prequential (rolling) evaluation: each prediction is
        genuinely out of sample, and there is no train/test boundary to leak
        across. scripts/fit_map_form.py scores parameter cells on it.
        """
        recorded: list[float] = []
        for game in sorted(games, key=lambda g: str(g["at"])):
            winner, loser = _normalize(str(game["w"])), _normalize(str(game["l"]))
            if not winner or not loser or winner == loser:
                continue
            map_name = normalize_map(str(game["map"]))
            if record_after is not None and str(game["at"]) > record_after:
                seen = min(self._played.get(winner, 0), self._played.get(loser, 0))
                if seen >= self._min_games:
                    recorded.append(self._expected(winner, loser, map_name))
            self._update(winner, loser, map_name)
            self._games += 1
        self._index_tokens(games)
        return recorded

    def _update(self, winner: str, loser: str, map_name: str) -> None:
        expected = self._expected(winner, loser, map_name)
        error = 1.0 - expected
        for team, sign in ((winner, 1.0), (loser, -1.0)):
            self._global[team] = self._global.get(team, START) + sign * self._k_global * error
            key = (map_name, team)
            self._delta[key] = self._delta.get(key, 0.0) + sign * self._k_map * error
            self._played[team] = self._played.get(team, 0) + 1

    def _index_tokens(self, games: list[dict[str, str]]) -> None:
        """First-token lookup, kept only where the token is UNAMBIGUOUS — the
        same rule TeamRatings uses so 'LP' can never be served 'LPH'."""
        counts: dict[str, set[str]] = {}
        for game in games:
            for raw in (str(game["w"]), str(game["l"])):
                counts.setdefault(_first_token(raw), set()).add(_normalize(raw))
        self._by_token = {
            token: next(iter(names)) for token, names in counts.items() if len(names) == 1 and token
        }

    # --- prediction ----------------------------------------------------------

    def _rating(self, team: str, map_name: str | None) -> float:
        rating = self._global.get(team, START)
        if map_name is not None:
            rating += self._map_weight * self._delta.get((map_name, team), 0.0)
        return rating

    def _expected(self, first: str, second: str, map_name: str | None) -> float:
        gap = self._rating(first, map_name) - self._rating(second, map_name)
        return 1.0 / (1.0 + exp(-gap / self._scale))

    def _key(self, label: str) -> str | None:
        """A forg1 outcome label -> the key this model stores, or None."""
        if not self._global:
            return None
        normalized = _normalize(label)
        if normalized in self._global:
            return normalized
        token = self._by_token.get(_first_token(label))
        return token if token in self._global else None

    def rating(self, label: str, map_name: str | None = None) -> float | None:
        """Elo for a forg1 label on a map (or overall), or None when the team is
        unknown or too thin to be worth a number."""
        key = self._key(label)
        if key is None or self._played.get(key, 0) < self._min_games:
            return None
        return self._rating(key, None if map_name is None else normalize_map(map_name))

    def games_played(self, label: str) -> int:
        key = self._key(label)
        return 0 if key is None else self._played.get(key, 0)

    def win_probability(
        self, label: str, opponent: str, map_name: str | None = None
    ) -> float | None:
        """P(label beats opponent) on this map, or None if either side is
        unknown or under `min_games`. Same contract as
        TeamRatings.win_probability, plus the map."""
        mine, theirs = self._key(label), self._key(opponent)
        if mine is None or theirs is None or mine == theirs:
            return None
        if min(self._played.get(mine, 0), self._played.get(theirs, 0)) < self._min_games:
            return None
        return self._expected(mine, theirs, None if map_name is None else normalize_map(map_name))
