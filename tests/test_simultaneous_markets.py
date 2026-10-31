"""forg1 can run several markets at once since 2026-10-31.

The monitor used to look only at the first open market and remember only the
last id it handled. A second market next to the first was never bet, and if the
payload order flipped, "A, B, A" made A look new again: a second bet on a
market already bet. These pin one bet per market, every open market seen, and
every result reported.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.models.prediction import BetDecision, Outcome, Prediction
from app.services.monitoring_service import MonitoringService


def _market(pid: str, title: str) -> Prediction:
    return Prediction(
        id=pid,
        title=title,
        outcomes=[
            Outcome(id=f"{pid}1", label="A", total_points=900, total_bettors=9),
            Outcome(id=f"{pid}2", label="B", total_points=100, total_bettors=1),
        ],
        min_bet=10,
        max_bet=10_000,
        locks_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        available_points=1000,
        captured_at=datetime.now(timezone.utc),
    )


class _Reader:
    def __init__(self, polls: list[list[Prediction]]) -> None:
        self.polls = polls
        self.reads = 0
        self.bets: list[tuple[str, int, dict[str, Any]]] = []
        self.winners: dict[str, str] = {}

    async def read_open_predictions(self) -> list[Prediction]:
        poll = self.polls[min(self.reads, len(self.polls) - 1)]
        self.reads += 1
        return poll

    async def read_active_prediction(self, prediction_id: str | None = None) -> Any:
        return None

    async def place_bet(self, side: str, amount: int, **kwargs: Any) -> bool:
        self.bets.append((side, amount, kwargs))
        return True

    async def read_resolution(self, prediction_id: str) -> str | None:
        return self.winners.get(prediction_id)

    async def refresh(self) -> None:
        return None

    async def refresh_balance(self) -> None:
        return None

    async def read_balance(self) -> int:
        return 650

    async def is_logged_in(self) -> bool:
        return True


class _Notifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> None:
        self.messages.append(text)


class _Strategy:
    def decide(self, prediction: Prediction) -> BetDecision:
        return BetDecision(should_bet=True, side="A", amount=350, reason="test")


def _monitor(tmp_path: Path, reader: _Reader, notifier: _Notifier) -> MonitoringService:
    m = MonitoringService(
        reader,  # type: ignore[arg-type]
        _Strategy(),  # type: ignore[arg-type]
        notifier,  # type: ignore[arg-type]
        seen_predictions_path=tmp_path / "seen.json",
        poll_idle_seconds=0,
        poll_active_seconds=0,
        refresh_after_idle_seconds=10_000,
        stabilization_seconds=0,
        balance_refresh_interval_seconds=0,
    )

    async def no_sleep(_seconds: float) -> None:
        await asyncio.sleep(0)

    m._sleep = no_sleep  # type: ignore[method-assign]
    return m


def _poll(m: MonitoringService, reader: _Reader, polls: int) -> None:
    async def go() -> None:
        task = asyncio.ensure_future(m.run())
        while reader.reads < polls:
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(asyncio.wait_for(go(), timeout=5))


def test_second_open_market_is_bet_too(tmp_path: Path) -> None:
    a, b = _market("a", "Quien gana el 1er MAPA?"), _market("b", "Quien gana la serie?")
    reader = _Reader([[a, b]])
    _poll(_monitor(tmp_path, reader, _Notifier()), reader, 10)

    assert [bet[2]["market_title"] for bet in reader.bets] == [a.title, b.title]
    assert all(bet[2]["outcome_count"] == 2 for bet in reader.bets)


def test_order_flip_never_bets_a_market_twice(tmp_path: Path) -> None:
    # The double-bet the single "last id" allowed: A, then B, then A again.
    a, b = _market("a", "Mapa"), _market("b", "Serie")
    reader = _Reader([[a], [b, a], [a, b], [b, a], [a, b]])
    _poll(_monitor(tmp_path, reader, _Notifier()), reader, 12)

    assert [bet[2]["market_title"] for bet in reader.bets] == ["Mapa", "Serie"]


def test_handled_markets_survive_a_restart(tmp_path: Path) -> None:
    a, b = _market("a", "Mapa"), _market("b", "Serie")
    first = _Reader([[a, b]])
    _poll(_monitor(tmp_path, first, _Notifier()), first, 6)
    assert len(first.bets) == 2

    again = _Reader([[b, a]])
    _poll(_monitor(tmp_path, again, _Notifier()), again, 6)
    assert again.bets == []


def test_old_seen_file_is_still_honoured(tmp_path: Path) -> None:
    # Files written before 2026-10-31 hold only {"last_id": ...}.
    (tmp_path / "seen.json").write_text(json.dumps({"last_id": "a"}))
    reader = _Reader([[_market("a", "Mapa")]])
    m = _monitor(tmp_path, reader, _Notifier())
    _poll(m, reader, 4)
    assert reader.bets == []

    m._save_last_seen("b")
    saved = json.loads((tmp_path / "seen.json").read_text())
    assert saved["seen_ids"] == ["a", "b"] and saved["last_id"] == "b"


def test_every_simultaneous_result_is_reported(tmp_path: Path) -> None:
    a, b = _market("a", "Mapa"), _market("b", "Serie")
    reader, notifier = _Reader([[a, b]]), _Notifier()
    m = _monitor(tmp_path, reader, notifier)
    asyncio.run(m._handle_prediction(a))
    asyncio.run(m._handle_prediction(b))
    assert set(m._pending) == {"a", "b"}

    reader.winners = {"b": "A"}
    asyncio.run(m._check_pending_resolution())
    assert set(m._pending) == {"a"}
    reader.winners = {"a": "B", "b": "A"}
    asyncio.run(m._check_pending_resolution())
    assert m._pending == {}
    finals = [msg for msg in notifier.messages if msg.startswith("🏁")]
    assert len(finals) == 2 and "Serie" in finals[0] and "Mapa" in finals[1]
