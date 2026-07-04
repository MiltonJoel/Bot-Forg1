from abc import ABC, abstractmethod

from app.models.prediction import BetDecision, Prediction


class BettingStrategy(ABC):
    """Pure decision logic. Receives a Prediction, returns a BetDecision. No I/O."""

    @abstractmethod
    def decide(self, prediction: Prediction) -> BetDecision: ...
