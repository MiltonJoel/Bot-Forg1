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
    """What a strategy recommends. The read-only bot only ever notifies this."""

    should_bet: bool
    side: str | None
    amount: int
    reason: str
