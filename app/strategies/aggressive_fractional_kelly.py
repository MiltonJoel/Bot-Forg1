from math import log10

from app.models.prediction import BetDecision, Prediction
from app.strategies.base_strategy import BettingStrategy


class AggressiveFractionalKellyStrategy(BettingStrategy):
    """Aggressive always-bet sizing for a pari-mutuel pool.

    The site does not expose a true win probability or payout edge, so this
    strategy uses the leading outcome's share of the pool as a momentum proxy.
    It then converts that signal into a bankroll fraction using a steep curve:

    - weak consensus still gets a minimum exposure,
    - strong consensus ramps quickly toward the maximum percentage,
    - bets are always placed when the balance can satisfy the minimum bet.
    """

    def __init__(
        self,
        low_share_threshold: float = 45.0,
        high_share_threshold: float = 90.0,
        min_pct: float = 4.0,
        max_pct: float = 32.0,
        curve_power: float = 0.7,
    ) -> None:
        if high_share_threshold <= low_share_threshold:
            raise ValueError("high_share_threshold must be greater than low_share_threshold")
        if min_pct <= 0 or max_pct <= 0:
            raise ValueError("min_pct and max_pct must be positive")
        if max_pct < min_pct:
            raise ValueError("max_pct must be greater than or equal to min_pct")
        if curve_power <= 0:
            raise ValueError("curve_power must be positive")

        self._low_share_threshold = low_share_threshold
        self._high_share_threshold = high_share_threshold
        self._min_pct = min_pct
        self._max_pct = max_pct
        self._curve_power = curve_power

    def decide(self, prediction: Prediction) -> BetDecision:
        if prediction.available_points < prediction.min_bet:
            return BetDecision(
                should_bet=False,
                side=prediction.leading_outcome.label,
                amount=0,
                reason=(
                    f"available_points={prediction.available_points} < "
                    f"min_bet={prediction.min_bet}"
                ),
            )

        share = prediction.leading_share
        normalized = (share - self._low_share_threshold) / (
            self._high_share_threshold - self._low_share_threshold
        )
        normalized = max(0.0, min(1.0, normalized))

        # Make the curve aggressive: it ramps faster than linear as confidence
        # increases, but still keeps a floor so every valid prediction gets action.
        momentum = normalized**self._curve_power

        # Larger pools are less noisy, so nudge the size upward as the pool grows.
        pool_factor = max(0.85, min(1.10, log10(prediction.total_pool + 10) / 5))

        pct = (self._min_pct + (self._max_pct - self._min_pct) * momentum) * pool_factor
        amount = int(prediction.available_points * pct / 100)
        amount = max(prediction.min_bet, amount)
        amount = min(prediction.max_bet, amount)

        return BetDecision(
            should_bet=True,
            side=prediction.leading_outcome.label,
            amount=amount,
            reason=(
                f"leading_share={share:.1f}% normalized={normalized:.2f} "
                f"momentum={momentum:.2f} pool_factor={pool_factor:.2f} "
                f"-> stake={pct:.1f}%"
            ),
        )
