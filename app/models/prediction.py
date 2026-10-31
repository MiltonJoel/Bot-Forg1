from datetime import datetime

from pydantic import BaseModel


class Outcome(BaseModel):
    """One selectable option within a prediction (e.g. one team)."""

    id: str
    label: str
    total_points: int
    total_bettors: int


class Prediction(BaseModel):
    """Domain object read from forg1's own embedded data. No Playwright, no
    HTML in here. Forg1 is a pari-mutuel pool ("el pool se reparte
    proporcional entre los que la pegaron") — there is no win-probability
    field anywhere on the site, fixed-odds betting doesn't apply here."""

    id: str
    title: str
    outcomes: list[Outcome]
    min_bet: int
    max_bet: int
    locks_at: datetime
    available_points: int  # the account's own balance ("Tu balance"), not the pool
    captured_at: datetime
    # Which CS2 map this market is about ("inferno"), when it could be worked
    # out. Forg1 almost never says — titles read "2do MAPA" — so it is resolved
    # from bo3.gg by app/services/map_resolver.py before the strategy runs, and
    # stays None whenever that is not certain. Strategies must treat None as
    # "no map information", never as a default map.
    map_name: str | None = None
    # Polymarket's probability per team label for this exact map, attached by
    # app/services/polymarket.py before the strategy runs. None whenever the
    # match is not certain; the strategy then prices exactly as before.
    polymarket: dict[str, float] | None = None

    @property
    def total_pool(self) -> int:
        return sum(o.total_points for o in self.outcomes)

    @property
    def leading_outcome(self) -> Outcome:
        return max(self.outcomes, key=lambda o: o.total_points)

    @property
    def leading_share(self) -> float:
        """Crowd-consensus proxy for "probability": the leading outcome's
        share of the pool so far. 0 if nobody has bet yet."""
        pool = self.total_pool
        return (self.leading_outcome.total_points / pool * 100) if pool else 0.0


class BetDecision(BaseModel):
    """What a strategy recommends. MonitoringService places it on the live UI
    and reports the outcome — this is not a suggestion-only object."""

    should_bet: bool
    side: str | None
    amount: int
    reason: str
    # True when the edge model sat out and the bet backs the crowd favourite
    # instead (STRATEGY_FAV_FALLBACK_PCT). Reported on Telegram and in results.jsonl.
    fallback: bool = False
