"""A Cloudflare block used to be invisible: the challenge page parses to zero
predictions, which is indistinguishable from a quiet stream, and it carries
neither login marker so is_logged_in() reported a healthy session. The bot sat
there looking fine while locked out. These pin the detection and the alert."""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.bot.login import is_logged_out_html
from app.services.monitoring_service import MonitoringService
from app.utils.http import CloudflareBlocked, cloudflare_reason

_CHALLENGE = (
    "<!DOCTYPE html><html><head><title>Just a moment...</title>"
    '<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"></script>'
    "</head><body>Checking your browser before accessing forg1.com</body></html>"
)
_HEALTHY = (
    '<html><body><script>self.__next_f.push([1,"29:{\\"isLoggedIn\\":true}"])</script>'
    "</body></html>"
)


def test_challenge_page_is_detected_even_with_http_200() -> None:
    # Cloudflare does serve interstitials with a 200; status alone is not enough.
    assert cloudflare_reason(200, {"server": "cloudflare"}, _CHALLENGE) is not None


def test_blocking_status_from_cloudflare_is_detected() -> None:
    assert cloudflare_reason(403, {"Server": "cloudflare"}, "denied") is not None
    assert cloudflare_reason(503, {"server": "cloudflare"}, "") is not None


def test_cf_mitigated_header_is_detected() -> None:
    assert cloudflare_reason(200, {"cf-mitigated": "challenge"}, "") is not None


def test_healthy_page_through_cloudflare_is_not_a_block() -> None:
    # The critical false positive: forg1 is served THROUGH Cloudflare, so every
    # healthy response carries `server: cloudflare`. That must never alert.
    assert cloudflare_reason(200, {"server": "cloudflare"}, _HEALTHY) is None


def test_ordinary_404_is_not_a_cloudflare_block() -> None:
    assert cloudflare_reason(404, {"server": "cloudflare"}, "<h1>Not found</h1>") is None


def test_challenge_page_reads_as_logged_in_which_is_why_detection_matters() -> None:
    # Documents the trap: the login check cannot see a block, so callers must
    # test for Cloudflare BEFORE trusting this.
    assert is_logged_out_html(_CHALLENGE) is False


class _BlockedReader:
    def __init__(self) -> None:
        self.calls = 0

    async def read_active_prediction(self) -> Any:
        self.calls += 1
        if self.calls > 2:  # recovers on the third poll
            return None
        raise CloudflareBlocked("challenge page (HTTP 403)")


class _Notifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> None:
        self.messages.append(text)


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


def test_block_alerts_once_then_reports_recovery(tmp_path: Path) -> None:
    reader, notifier = _BlockedReader(), _Notifier()
    monitor = _monitor(tmp_path, reader, notifier)

    async def run_a_few_polls() -> None:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(monitor.run(), timeout=0.3)

    asyncio.run(run_a_few_polls())

    blocks = [m for m in notifier.messages if "🚫" in m]
    recoveries = [m for m in notifier.messages if "✅" in m]
    assert len(blocks) == 1, "must alert once per block, not once per poll"
    assert "Cloudflare" in blocks[0]
    assert len(recoveries) == 1, "must say when it comes back"
