from app.models.prediction import BetDecision, Prediction
from app.strategies.base_strategy import BettingStrategy


class FixedPercentageStrategy(BettingStrategy):
    """Forg1 has no probability field (pari-mutuel pool, not fixed odds) — this
    uses the leading outcome's pool share as the proxy: >threshold% -> high_pct
    of the account's points, else low_pct. Respects the prediction's own
    min_bet/max_bet so the suggestion is always placeable."""

    def __init__(self, threshold: float, high_pct: float, low_pct: float) -> None:
        self._threshold = threshold
        self._high_pct = high_pct
        self._low_pct = low_pct

    def decide(self, prediction: Prediction) -> BetDecision:
        share = prediction.leading_share
        pct = self._high_pct if share > self._threshold else self._low_pct
        amount = min(int(prediction.available_points * pct / 100), prediction.max_bet)
        should_bet = amount >= prediction.min_bet
        return BetDecision(
            should_bet=should_bet,
            side=prediction.leading_outcome.label,
            amount=amount if should_bet else 0,
            reason=(
                f"leading_share={share:.1f}% of pool -> {pct}% of "
                f"{prediction.available_points} pts (min_bet={prediction.min_bet})"
            ),
        )
