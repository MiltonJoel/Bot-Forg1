import asyncio
from datetime import datetime, timezone
from typing import Any

from app.models.prediction import BetDecision, Outcome, Prediction
from app.services.monitoring_service import MonitoringService


class FakeReader:
    def __init__(self, succeeds: bool = True, logged_in: bool = True) -> None:
        self.succeeds = succeeds
        self.logged_in = logged_in
        self.bets: list[tuple[str, int]] = []
        self.balance_refreshes = 0
        self.reads = 0
        self.refreshes = 0

    async def read_open_predictions(self) -> list[Prediction]:
        self.reads += 1
        return []

    async def refresh(self) -> None:
        self.refreshes += 1

    async def read_resolution(self, prediction_id: str) -> None:
        return None  # still open

    async def place_bet(self, side: str, amount: int, **_: Any) -> bool:
        self.bets.append((side, amount))
        return self.succeeds

    async def is_logged_in(self) -> bool:
        return self.logged_in

    async def refresh_balance(self) -> None:
        self.balance_refreshes += 1

    async def read_balance(self) -> int:
        return 650  # what the site reports after the 350 stake left 1000


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
        seen_predictions_path=tmp_path / "seen.json",
        poll_idle_seconds=1,
        poll_active_seconds=1,
        refresh_after_idle_seconds=10,
        stabilization_seconds=0,
        balance_refresh_interval_seconds=120,
    )


def test_kill_switch_stops_betting_after_a_drawdown(tmp_path: Any) -> None:
    reader, notifier = FakeReader(), FakeNotifier()
    monitor = _monitor(tmp_path, reader, notifier, bet=True)
    monitor._bankroll_stop_pct = 40.0

    first = _prediction()  # available_points=1000 -> session reference
    asyncio.run(monitor._handle_prediction(first))
    assert reader.bets == [("A", 350)]

    # Balance has since fallen 50% — past the 40% limit. No further bets.
    drained = first.model_copy(update={"id": "next", "available_points": 500})
    asyncio.run(monitor._handle_prediction(drained))
    assert reader.bets == [("A", 350)]  # unchanged
    assert any("🛑" in m for m in notifier.messages)


def test_resolved_market_is_recorded_for_recalibration(tmp_path: Any) -> None:
    import json as _json

    reader, notifier = FakeReader(), FakeNotifier()
    monitor = _monitor(tmp_path, reader, notifier, bet=True)
    monitor._results_log_path = tmp_path / "results.jsonl"
    decision = BetDecision(should_bet=True, side="A", amount=350, reason="test")

    monitor._record_result(_prediction(), decision, "B")

    record = _json.loads((tmp_path / "results.jsonl").read_text().strip())
    assert record["winner"] == "B"
    assert record["bet_side"] == "A"
    # Both labels and their final pool points — what the text logs never kept.
    assert [o["label"] for o in record["outcomes"]] == ["A", "B"]
    assert [o["points"] for o in record["outcomes"]] == [900, 100]


def test_bet_is_executed_and_reported(tmp_path: Any) -> None:
    reader, notifier = FakeReader(), FakeNotifier()
    monitor = _monitor(tmp_path, reader, notifier, bet=True)
    asyncio.run(monitor._handle_prediction(_prediction()))
    assert reader.bets == [("A", 350)]
    assert "✅ Apuesta ejecutada" in notifier.messages[0]
    # the real post-bet balance, read from the page — not available_points - amount
    assert "quedan 650 pts" in notifier.messages[0]
    assert monitor._last_seen_id == "abc123"


def test_failed_bet_reports_failure(tmp_path: Any) -> None:
    reader, notifier = FakeReader(succeeds=False), FakeNotifier()
    monitor = _monitor(tmp_path, reader, notifier, bet=True)
    asyncio.run(monitor._handle_prediction(_prediction()))
    assert "❓" in notifier.messages[0]
    # Must not tell the owner to bet manually without checking: the bet may
    # have landed even though we could not confirm it.
    assert "revisá tus jugadas" in notifier.messages[0]


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


def test_balance_refresh_skipped_while_stream_offline(tmp_path: Any) -> None:
    reader, notifier = FakeReader(), FakeNotifier()
    monitor = _monitor(tmp_path, reader, notifier, bet=False)
    live = False
    monitor._stream_is_live = lambda: live
    monitor._last_balance_refresh = 0
    asyncio.run(monitor._maybe_refresh_balance())
    assert reader.balance_refreshes == 0  # offline -> no clicks at all
    live = True
    asyncio.run(monitor._maybe_refresh_balance())
    assert reader.balance_refreshes == 1  # live again -> resumes


def test_logout_alerts_once_and_recovery_notifies(tmp_path: Any) -> None:
    reader, notifier = FakeReader(logged_in=False), FakeNotifier()
    monitor = _monitor(tmp_path, reader, notifier, bet=False)

    monitor._last_balance_refresh = 0
    asyncio.run(monitor._maybe_refresh_balance())  # logged out -> alert, no refresh
    monitor._last_balance_refresh = 0
    asyncio.run(monitor._maybe_refresh_balance())  # still out -> silent
    assert reader.balance_refreshes == 0
    assert len(notifier.messages) == 1 and "⚠️" in notifier.messages[0]

    reader.logged_in = True
    monitor._last_balance_refresh = 0
    asyncio.run(monitor._maybe_refresh_balance())  # back -> recovery msg + refresh
    assert reader.balance_refreshes == 1
    assert "✅" in notifier.messages[1]


def _run_briefly(coro: Any, seconds: float = 0.3) -> None:
    async def go() -> None:
        try:
            await asyncio.wait_for(coro, timeout=seconds)
        except asyncio.TimeoutError:
            pass

    asyncio.run(go())


def test_poll_loop_stops_reading_once_the_stream_is_over(tmp_path: Any) -> None:
    # forg1 only opens markets during the stream. Polling the page all night
    # finds nothing and just hands Cloudflare/Vercel a steady automated pattern.
    reader, notifier = FakeReader(), FakeNotifier()
    monitor = _monitor(tmp_path, reader, notifier, bet=False)
    monitor._stream_is_live = lambda: False

    _run_briefly(monitor.run())

    assert reader.reads == 0
    assert reader.refreshes == 0  # and no idle page reloads either


def test_poll_loop_keeps_running_for_a_pending_result(tmp_path: Any) -> None:
    # A bet placed just before the stream ended still has to be reported.
    reader, notifier = FakeReader(), FakeNotifier()
    monitor = _monitor(tmp_path, reader, notifier, bet=False)
    monitor._stream_is_live = lambda: False
    monitor._pending = {
        "abc123": (_prediction(), BetDecision(should_bet=True, side="A", amount=350, reason=""))
    }

    _run_briefly(monitor.run())

    assert reader.reads > 0


def test_poll_loop_reads_while_the_stream_is_live(tmp_path: Any) -> None:
    reader, notifier = FakeReader(), FakeNotifier()
    monitor = _monitor(tmp_path, reader, notifier, bet=False)
    monitor._stream_is_live = lambda: True

    _run_briefly(monitor.run())

    assert reader.reads > 0


def test_poll_loop_reloads_the_tab_on_each_stream_edge(tmp_path: Any) -> None:
    # Reads are HTTP, so without this the visible tab kept forg1's offline view
    # well into a live stream.
    reader, notifier = FakeReader(), FakeNotifier()
    monitor = _monitor(tmp_path, reader, notifier, bet=False)
    states = iter([False, False, True, True, False])
    monitor._stream_is_live = lambda: next(states, False)
    monitor._sleep = lambda _s: asyncio.sleep(0)  # type: ignore[method-assign]

    _run_briefly(monitor.run(), seconds=0.05)

    assert reader.refreshes == 2  # live edge + offline edge, not startup
