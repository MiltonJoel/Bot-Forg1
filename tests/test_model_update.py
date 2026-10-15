"""/actualizarmodelo swaps a re-exported CounterPredictor into the live strategy.
A failed update must leave both the snapshot file and the live model untouched."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from app.services import model_update
from app.services.model_update import CounterPredictorUpdater

SNAPSHOT = {
    "as_of": "2026-10-09T00:00:00",
    "active_maps": [],
    "h2h": {},
    "model": {"mean": [], "scale": [], "coef": [], "intercept": [0.0]},
    "teams": {},
}


class _Strategy:
    def __init__(self) -> None:
        self.form: Any = "old"

    def replace_form(self, form: Any) -> None:
        self.form = form


class _Notifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


def _updater(tmp_path: Path, strategy: _Strategy, notifier: Any = None) -> CounterPredictorUpdater:
    return CounterPredictorUpdater(
        notifier or _Notifier(),  # type: ignore[arg-type]
        strategy,  # type: ignore[arg-type]
        snapshot_path=tmp_path / "counterpredictor.json",
        source_dir=tmp_path,
        min_games=10,
        v1=True,
    )


def test_successful_update_reloads_the_model(tmp_path: Path, monkeypatch: Any) -> None:
    snapshot = tmp_path / "counterpredictor.json"
    snapshot.write_text(json.dumps({**SNAPSHOT, "as_of": "2026-10-01T00:00:00"}))
    calls: list[list[str]] = []

    async def fake_run(command: list[str], cwd: Any, timeout: float) -> str:
        calls.append(command)
        if "export" in command[1]:
            snapshot.write_text(json.dumps(SNAPSHOT))
            return "v1 live snapshot: 5 teams"
        return "kaggle: updated to 7\nnoise"

    monkeypatch.setattr(model_update, "_run", fake_run)
    strategy = _Strategy()
    summary = asyncio.run(_updater(tmp_path, strategy).refresh())

    assert strategy.form.as_of.day == 9
    assert "--v1" in calls[1]
    assert summary == "kaggle: updated to 7\nv1 live snapshot: 5 teams"


def test_failed_export_restores_the_old_snapshot(tmp_path: Path, monkeypatch: Any) -> None:
    snapshot = tmp_path / "counterpredictor.json"
    snapshot.write_text(json.dumps(SNAPSHOT))

    async def fake_run(command: list[str], cwd: Any, timeout: float) -> str:
        if "export" in command[1]:
            snapshot.write_text("{truncated")
            raise RuntimeError("export_counterpredictor.py exited 1")
        return ""

    monkeypatch.setattr(model_update, "_run", fake_run)
    strategy = _Strategy()
    with pytest.raises(RuntimeError):
        asyncio.run(_updater(tmp_path, strategy).refresh())

    assert json.loads(snapshot.read_text()) == SNAPSHOT
    assert strategy.form == "old"


def test_second_request_while_running_is_refused(tmp_path: Path, monkeypatch: Any) -> None:
    release = asyncio.Event()

    async def slow_refresh(self: Any) -> str:
        await release.wait()
        return "done"

    monkeypatch.setattr(CounterPredictorUpdater, "refresh", slow_refresh)
    notifier = _Notifier()
    updater = _updater(tmp_path, _Strategy(), notifier)

    async def go() -> list[bool]:
        started = [updater.start(), updater.start()]
        await asyncio.sleep(0)
        release.set()
        await updater._task  # type: ignore[misc]
        return started

    assert asyncio.run(go()) == [True, False]
    assert "✅" in notifier.sent[-1]


def test_run_reports_a_failing_command(tmp_path: Path) -> None:
    script = tmp_path / "boom.py"
    script.write_text("print('kaggle: nope'); raise SystemExit(3)")
    import sys

    with pytest.raises(RuntimeError, match="boom.py exited 3"):
        asyncio.run(model_update._run([sys.executable, str(script)], None, 30))
