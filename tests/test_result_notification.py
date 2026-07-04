import asyncio
from datetime import datetime, timezone
from pathlib import Path

from app.models.prediction import BetDecision, Outcome, Prediction
from app.services.monitoring_service import MonitoringService


class _FakeReader:
    def __init__(self, balance: int) -> None:
        self._balance = balance

    async def refresh(self) -> None:
        pass

    async def read_balance(self) -> int:
        return self._balance


class _FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


def _monitor(tmp_path: Path, balance: int) -> tuple[MonitoringService, _FakeNotifier]:
    notifier = _FakeNotifier()
    m = MonitoringService(
        reader=_FakeReader(balance),  # type: ignore[arg-type]
        strategy=None,  # type: ignore[arg-type]
        notifier=notifier,  # type: ignore[arg-type]
        seen_predictions_path=tmp_path / "seen.json",
        poll_idle_seconds=1,
        poll_active_seconds=1,
        refresh_after_idle_seconds=1,
        stabilization_seconds=0,
        balance_refresh_interval_seconds=120,
    )
    return m, notifier


def _prediction() -> Prediction:
    return Prediction(
        id="p1",
        title="Quien gana el mapa?",
        outcomes=[
            Outcome(id="a", label="TeamA", total_points=900, total_bettors=9),
            Outcome(id="b", label="TeamB", total_points=100, total_bettors=1),
        ],
        min_bet=500,
        max_bet=5000,
        locks_at=datetime.now(timezone.utc),
        available_points=10_000,
        captured_at=datetime.now(timezone.utc),
    )


def _bet(side: str) -> BetDecision:
    return BetDecision(should_bet=True, side=side, amount=1000, reason="x")


def test_result_win(tmp_path: Path) -> None:
    m, notifier = _monitor(tmp_path, balance=12_500)
    asyncio.run(m._notify_result(_prediction(), _bet("TeamA"), "TeamA"))
    msg = notifier.sent[-1]
    assert "✅" in msg and "acertó" in msg
    assert "Balance actualizado: 12500 pts" in msg


def test_result_loss(tmp_path: Path) -> None:
    m, notifier = _monitor(tmp_path, balance=9_000)
    asyncio.run(m._notify_result(_prediction(), _bet("TeamB"), "TeamA"))
    msg = notifier.sent[-1]
    assert "❌" in msg and "no acertó" in msg
    assert "Balance actualizado: 9000 pts" in msg


def test_result_no_bet(tmp_path: Path) -> None:
    m, notifier = _monitor(tmp_path, balance=10_000)
    decision = BetDecision(should_bet=False, side=None, amount=0, reason="x")
    asyncio.run(m._notify_result(_prediction(), decision, "TeamA"))
    msg = notifier.sent[-1]
    assert "no se había sugerido apuesta" in msg
