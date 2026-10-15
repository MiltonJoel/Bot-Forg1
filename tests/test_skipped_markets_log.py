import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from app.models.prediction import BetDecision, Outcome, Prediction
from app.services.monitoring_service import MonitoringService

_SKIP = BetDecision(
    should_bet=False,
    side=None,
    amount=0,
    reason="4-way market (limit 2); the crowd calibration was only measured on head-to-head markets",
)
_NO_EDGE = BetDecision(should_bet=False, side=None, amount=0, reason="no edge: best is A at +0.01")


class _FakeReader:
    def __init__(self, final: dict[str, int] | None, fail_final: bool = False) -> None:
        self._final = final
        self._fail_final = fail_final

    async def read_resolution(self, prediction_id: str) -> str | None:
        return "13 a 17"

    async def read_final_pool(self, prediction_id: str) -> dict[str, int] | None:
        if self._fail_final:
            raise RuntimeError("boom")
        return self._final

    async def refresh(self) -> None:
        pass

    async def read_balance(self) -> int:
        return 10_000


class _FakeNotifier:
    async def send(self, text: str) -> None:
        pass


def _monitor(tmp_path: Path, reader: _FakeReader, log: Path) -> MonitoringService:
    return MonitoringService(
        reader=reader,  # type: ignore[arg-type]
        strategy=None,  # type: ignore[arg-type]
        notifier=_FakeNotifier(),  # type: ignore[arg-type]
        seen_predictions_path=tmp_path / "seen.json",
        poll_idle_seconds=1,
        poll_active_seconds=1,
        refresh_after_idle_seconds=1,
        stabilization_seconds=0,
        balance_refresh_interval_seconds=120,
        skipped_log_path=log,
    )


def _kills(points: tuple[int, int, int, int], pid: str = "k1") -> Prediction:
    labels = ("12 o menos", "13 a 17", "18 a 22", "23 o mas")
    return Prediction(
        id=pid,
        title="Cuantas kills donk?",
        outcomes=[
            Outcome(id=str(i), label=label, total_points=pts, total_bettors=1)
            for i, (label, pts) in enumerate(zip(labels, points))
        ],
        min_bet=500,
        max_bet=50_000,
        locks_at=datetime(2026, 10, 10, 20, 40, tzinfo=timezone.utc),
        available_points=10_000,
        captured_at=datetime.now(timezone.utc),
    )


def _run(m: MonitoringService, decision: BetDecision, reads: list[Prediction]) -> None:
    m._pending = (reads[0], decision)
    for p in reads:
        m._observe_skipped(p)
    m._observe_skipped(_kills((9, 9, 9, 9), pid="other"))  # a different market
    asyncio.run(m._check_pending_resolution())


def test_skipped_market_logs_reads_and_final_pool(tmp_path: Path) -> None:
    log = tmp_path / "skipped.jsonl"
    final = {"12 o menos": 110, "13 a 17": 440, "18 a 22": 539, "23 o mas": 410}
    m = _monitor(tmp_path, _FakeReader(final), log)
    reads = [_kills((10, 20, 30, 40)), _kills((10, 20, 30, 40)), _kills((100, 400, 500, 400))]
    _run(m, _SKIP, reads)

    (line,) = log.read_text().splitlines()
    record = json.loads(line)
    assert record["options"] == ["12 o menos", "13 a 17", "18 a 22", "23 o mas"]
    assert [r["points"]["13 a 17"] for r in record["reads"]] == [20, 400]  # dup dropped
    assert record["final_points"] == final
    assert record["winner"] == "13 a 17"
    assert record["locks_at"] == "2026-10-10T20:40:00+00:00"
    assert m._pending is None and m._skipped_reads == []


def test_priced_market_is_not_logged(tmp_path: Path) -> None:
    log = tmp_path / "skipped.jsonl"
    m = _monitor(tmp_path, _FakeReader({}), log)
    _run(m, _NO_EDGE, [_kills((1, 2, 3, 4))])
    assert not log.exists()


def test_logging_failures_never_raise(tmp_path: Path) -> None:
    # Final-pool read fails: the line is still written, without it.
    log = tmp_path / "skipped.jsonl"
    m = _monitor(tmp_path, _FakeReader(None, fail_final=True), log)
    _run(m, _SKIP, [_kills((1, 2, 3, 4))])
    assert json.loads(log.read_text())["final_points"] is None

    # Unwritable path (a directory): warning only, the resolution still clears.
    m = _monitor(tmp_path, _FakeReader({}), tmp_path)
    _run(m, _SKIP, [_kills((1, 2, 3, 4))])
    assert m._pending is None
