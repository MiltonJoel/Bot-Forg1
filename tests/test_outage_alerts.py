"""A real incident: the Kick check timed out every ~45s for over an hour and
produced nothing but log warnings. The bot was dead to Kick and Telegram never
heard about it. Blips stay quiet; sustained outages must escalate."""

import asyncio
import socket
from pathlib import Path
from typing import Any

import pytest

from app.services import kick_service
from app.services.kick_service import KickLiveChecker
from app.services.monitoring_service import MonitoringService


class _Notifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> None:
        self.messages.append(text)


def _warnings(notifier: _Notifier) -> list[str]:
    return [m for m in notifier.messages if "⚠️" in m]


def _recoveries(notifier: _Notifier) -> list[str]:
    return [m for m in notifier.messages if "✅" in m]


# --- Kick ------------------------------------------------------------------


def _run_briefly(coro: Any, seconds: float = 0.3) -> None:
    async def go() -> None:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(coro, timeout=seconds)

    asyncio.run(go())


def test_single_kick_blip_stays_quiet(monkeypatch: Any) -> None:
    notifier = _Notifier()
    checker = KickLiveChecker("forg1", 0, notifier)  # type: ignore[arg-type]
    calls = {"n": 0}

    def check(self: Any) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("timed out")
        return False

    monkeypatch.setattr(KickLiveChecker, "_check_live", check)
    _run_briefly(checker.run())
    assert _warnings(notifier) == [], "one blip must not page the user"


def test_sustained_kick_outage_alerts_once_then_recovers(monkeypatch: Any) -> None:
    notifier = _Notifier()
    checker = KickLiveChecker("forg1", 0, notifier)  # type: ignore[arg-type]
    calls = {"n": 0}
    threshold = kick_service._FAILURES_BEFORE_ALERT

    def check(self: Any) -> bool:
        calls["n"] += 1
        if calls["n"] <= threshold + 4:  # long outage, then it comes back
            raise socket.gaierror("Name or service not known")
        return False

    monkeypatch.setattr(KickLiveChecker, "_check_live", check)
    _run_briefly(checker.run())

    assert len(_warnings(notifier)) == 1, "alert once per outage, not per attempt"
    assert len(_recoveries(notifier)) == 1, "must say when it comes back"


# --- prediction reads -------------------------------------------------------


class _FailingReader:
    def __init__(self, failures: int) -> None:
        self.remaining = failures

    async def read_active_prediction(self) -> Any:
        if self.remaining > 0:
            self.remaining -= 1
            raise TimeoutError("APIRequestContext.get: Timeout 30000ms exceeded")
        return None


def _monitor(tmp_path: Path, reader: Any, notifier: Any) -> MonitoringService:
    return MonitoringService(
        reader,
        None,  # type: ignore[arg-type]
        notifier,
        seen_predictions_path=tmp_path / "seen.json",
        poll_idle_seconds=0,
        poll_active_seconds=0,
        refresh_after_idle_seconds=10_000,
        stabilization_seconds=0,
        balance_refresh_interval_seconds=0,
    )


def test_single_read_failure_stays_quiet(tmp_path: Path) -> None:
    notifier = _Notifier()
    _run_briefly(_monitor(tmp_path, _FailingReader(1), notifier).run())
    assert _warnings(notifier) == []


def test_sustained_read_outage_alerts_once_then_recovers(tmp_path: Path) -> None:
    from app.services import monitoring_service

    notifier = _Notifier()
    failures = monitoring_service._READ_FAILURES_BEFORE_ALERT + 3
    _run_briefly(_monitor(tmp_path, _FailingReader(failures), notifier).run())

    assert len(_warnings(notifier)) == 1
    assert "no está viendo ni apostando" in _warnings(notifier)[0]
    assert len(_recoveries(notifier)) == 1
