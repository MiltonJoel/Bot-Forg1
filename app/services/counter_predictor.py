"""CounterPredictor — the owner's pre-match map-winner model, ported for the bot.

Trained in ~/Desktop/CounterPredictor (`oos_eval.py v2`: logistic regression on
team/map Elo, last-10 form, lineup K/D/ADR/KAST, rest, experience, roster
stability and head-to-head, fitted on Kaggle + Liquipedia maps). That project
needs pandas + sklearn and a full history replay per prediction; neither belongs
on the 2GB VPS. So `scripts/export_counterpredictor.py` runs the replay there
once and writes every team's feature state plus the model's weights to
`.data/counterpredictor.json`, and this file evaluates the same logistic in
stdlib. Stale snapshot = stale ratings: re-export after `update.py` pulls data.

Stdlib only on purpose — the export script (system python, no loguru) imports
it to backtest with exactly the code the bot runs.
"""

import json
import re
from datetime import datetime, timezone
from math import exp, log1p
from pathlib import Path
from typing import Any

# Order matters: it is the column order the logistic was fitted on.
DIFF = [
    "elo",
    "map_elo",
    "form",
    "rdiff",
    "kd",
    "adr",
    "kast",
    "rest",
    "exp",
    "map_exp",
    "stable",
    "h2h",
]
CTX = ["h2h_n", "tier", "bestOf"]

# forg1's short labels that no normalization can recover. Keys are `key()`-ed.
ALIASES = {
    "navi": "natusvincere",
    "vit": "vitality",
    "ef": "eternalfire",
    "100t": "100thieves",
    "lp": "largadosypelados",
    "faze": "fazeclan",
    "9zglobant": "9z",
}


def key(name: str) -> str:
    """'Team Vitality' -> 'vitality', 'FUT Esports' -> 'fut'. Same rule as
    CounterPredictor's oos_eval.key, so forg1 labels and Kaggle/Liquipedia
    names meet in the middle."""
    s = re.sub(r"[^a-z0-9]", "", name.lower())
    s = re.sub(r"^(the|team)|(esports?|gaming|club|team|cs|csgo|cs2)$", "", s) or s
    return ALIASES.get(s, s)


def _first_token(name: str) -> str:
    parts = re.sub(r"[^a-z0-9]+", " ", name.lower()).split()
    return parts[0] if parts else ""


def _sigmoid(z: float) -> float:
    return 1.0 / (1.0 + exp(-z))


class CounterPredictor:
    """P(team wins this map). Same contract as MapForm.win_probability: None
    whenever either team is unknown or thinner than `min_games`, so the
    strategy falls back to the pool instead of inventing a number."""

    def __init__(self, snapshot: dict[str, Any], min_games: int = 10) -> None:
        self._teams: dict[str, dict[str, Any]] = snapshot["teams"]
        self._h2h: dict[str, list[str]] = snapshot["h2h"]
        self._model: dict[str, list[float]] = snapshot["model"]
        self._maps: list[str] = snapshot["active_maps"]
        self._as_of = datetime.fromisoformat(snapshot["as_of"])
        self._min_games = min_games
        # Most-played spelling wins a collision ('Team Spirit' over 'Spirit Esports').
        by_key: dict[str, str] = {}
        by_token: dict[str, set[str]] = {}
        for name, team in sorted(self._teams.items(), key=lambda kv: kv[1]["n"]):
            by_key[key(name)] = name
            by_token.setdefault(_first_token(name), set()).add(key(name))
        self._by_key = by_key
        # First token only where unambiguous: '9z Globant' -> '9z', never 'LP' -> 'LPH'.
        self._by_token = {
            t: by_key[next(iter(ks))] for t, ks in by_token.items() if len(ks) == 1 and t
        }

    @classmethod
    def from_file(cls, path: Path, min_games: int = 10) -> "CounterPredictor | None":
        if not path.exists():
            return None
        return cls(json.loads(path.read_text()), min_games)

    @property
    def as_of(self) -> datetime:
        return self._as_of

    def team(self, label: str) -> str | None:
        name = self._by_key.get(key(label)) or self._by_token.get(_first_token(label))
        if name is None or self._teams[name]["n"] < self._min_games:
            return None
        return name

    def win_probability(
        self,
        label: str,
        opponent: str,
        map_name: str | None = None,
        when: datetime | None = None,
    ) -> float | None:
        a, b = self.team(label), self.team(opponent)
        if a is None or b is None or a == b:
            return None
        when = when or datetime.now(timezone.utc)
        if map_name:
            return self._probability(a, b, _map_title(map_name), when)
        # forg1 rarely names the map ("2do MAPA"); average over the active pool.
        return sum(self._probability(a, b, m, when) for m in self._maps) / len(self._maps)

    # --- the logistic, exactly as sklearn evaluates it ------------------------

    def _side(self, team: str, map_name: str, when: datetime) -> dict[str, float | None]:
        t = self._teams[team]
        last = datetime.fromisoformat(t["last"]) if t["last"] else None
        return {
            "elo": t["elo"],
            "map_elo": t["map_elo"].get(map_name, 1500.0),
            "form": t["form"],
            "rdiff": t["rdiff"],
            "kd": t["kd"],
            "adr": t["adr"],
            "kast": t["kast"],
            "rest": (_naive(when) - _naive(last)).days if last else None,
            "exp": log1p(t["n"]),
            "map_exp": log1p(t["n_map"].get(map_name, 0)),
            "stable": t["stable"],
        }

    def _features(self, a: str, b: str, map_name: str, when: datetime) -> list[float]:
        sa, sb = self._side(a, map_name, when), self._side(b, map_name, when)
        row: list[float] = []
        for f in DIFF[:-1]:
            x, y = sa[f], sb[f]
            row.append(0.0 if x is None or y is None else x - y)  # fillna(0), as trained
        h = self._h2h.get("|".join(sorted((a, b))), [])
        row.append(sum(w == a for w in h) / len(h) - 0.5 if h else 0.0)
        return row + [log1p(len(h)), 1.0, 3.0]  # tier/bestOf: fitted coefficients are 0

    def _logit(self, row: list[float]) -> float:
        m = self._model
        return m["intercept"][0] + sum(
            c * (x - mu) / s for c, x, mu, s in zip(m["coef"], row, m["mean"], m["scale"])
        )

    def _probability(self, a: str, b: str, map_name: str, when: datetime) -> float:
        # Symmetrized like CounterPredictor's sym_proba, so P(A)+P(B)=1 exactly.
        row = self._features(a, b, map_name, when)
        flip = [-x for x in row[: len(DIFF)]] + row[len(DIFF) :]
        return (_sigmoid(self._logit(row)) + 1 - _sigmoid(self._logit(flip))) / 2


def _map_title(name: str) -> str:
    """'de_inferno' / 'INFERNO' -> 'Inferno', the spelling the history uses."""
    flat = name.strip().lower()
    return (flat[3:] if flat.startswith("de_") else flat).capitalize()


def _naive(moment: datetime) -> datetime:
    return moment.replace(tzinfo=None) if moment.tzinfo else moment
