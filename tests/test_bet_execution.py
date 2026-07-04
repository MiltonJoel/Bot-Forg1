import asyncio
from datetime import datetime, timezone
from typing import Any

from app.models.prediction import BetDecision, Outcome, Prediction
from app.services.monitoring_service import MonitoringService


class FakeReader:
    def __init__(self, succeeds: bool = True) -> None:
        self.succeeds = succeeds
        self.bets: list[tuple[str, int]] = []
        self.balance_refreshes = 0

    async def place_bet(self, side: str, amount: int) -> bool:
        self.bets.append((side, amount))
        return self.succeeds

    async def refresh_balance(self) -> None:
        self.balance_refreshes += 1


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> None:
        self.messages.append(text)


class FakeStrategy:
    def __init__(self, decision: BetDecision) -> None:
        self.decision = decision

    def decide(self, prediction: Prediction) -> BetDecision:
        return self.decision


def _prediction() -> Prediction:
    return Prediction(
        id="abc123",
        title="A vs B",
        outcomes=[
            Outcome(id="o1", label="A", total_points=900, total_bettors=9),
            Outcome(id="o2", label="B", total_points=100, total_bettors=1),
        ],
        min_bet=10,
        max_bet=10000,
        locks_at=datetime.now(timezone.utc),
        available_points=1000,
        captured_at=datetime.now(timezone.utc),
    )


def _monitor(tmp_path: Any, reader: FakeReader, notifier: FakeNotifier, bet: bool) -> Any:
    decision = BetDecision(should_bet=bet, side="A", amount=350, reason="test")
    return MonitoringService(
        reader,  # type: ignore[arg-type]
        FakeStrategy(decision),  # type: ignore[arg-type]
        notifier,  # type: ignore[arg-type]
        tmp_path / "seen.json",
        1,
        1,
        10,
        0,
        120,
    )


def test_bet_is_executed_and_reported(tmp_path: Any) -> None:
    reader, notifier = FakeReader(), FakeNotifier()
    monitor = _monitor(tmp_path, reader, notifier, bet=True)
    asyncio.run(monitor._handle_prediction(_prediction()))
    assert reader.bets == [("A", 350)]
    assert "✅ Apuesta ejecutada" in notifier.messages[0]
    assert monitor._last_seen_id == "abc123"


def test_failed_bet_reports_failure(tmp_path: Any) -> None:
    reader, notifier = FakeReader(succeeds=False), FakeNotifier()
    monitor = _monitor(tmp_path, reader, notifier, bet=True)
    asyncio.run(monitor._handle_prediction(_prediction()))
    assert "❌" in notifier.messages[0]


def test_no_bet_decision_places_nothing(tmp_path: Any) -> None:
    reader, notifier = FakeReader(), FakeNotifier()
    monitor = _monitor(tmp_path, reader, notifier, bet=False)
    asyncio.run(monitor._handle_prediction(_prediction()))
    assert reader.bets == []
    assert "⏭️ Sin apuesta" in notifier.messages[0]


def test_balance_refresh_runs_when_due(tmp_path: Any) -> None:
    reader, notifier = FakeReader(), FakeNotifier()
    monitor = _monitor(tmp_path, reader, notifier, bet=False)
    monitor._last_balance_refresh = 0
    asyncio.run(monitor._maybe_refresh_balance())
    assert reader.balance_refreshes == 1
