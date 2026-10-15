"""Refresh the CounterPredictor snapshot from Telegram, without a restart.

`/actualizarmodelo` runs CounterPredictor's own `update.py` (new Kaggle and
Liquipedia data; anything dropped into its data/ folder by hand is picked up
too), re-exports `.data/counterpredictor.json`, and swaps the new model into
the running strategy. The export needs pandas/sklearn, which live in the SYSTEM
python, not the bot's venv, so both steps run as subprocesses.

A failed step never leaves the bot worse off: the previous snapshot file is
restored and the model in memory is not touched.
"""

import asyncio
import shutil
from pathlib import Path

from loguru import logger

from app.services.counter_predictor import CounterPredictor
from app.services.notifier import TelegramNotifier
from app.strategies.contrarian_pari_mutuel import ContrarianPariMutuelStrategy
from app.utils.retry import short_error

_PYTHON = "/usr/bin/python3"


async def _run(command: list[str], cwd: Path | None, timeout: float) -> str:
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError(f"{Path(command[1]).name} timed out after {timeout:.0f}s") from None
    text = output.decode("utf-8", "replace").strip()
    if process.returncode != 0:
        last = text.splitlines()[-1] if text else "(no output)"
        raise RuntimeError(f"{Path(command[1]).name} exited {process.returncode}: {last}")
    return text


class CounterPredictorUpdater:
    def __init__(
        self,
        notifier: TelegramNotifier,
        strategy: ContrarianPariMutuelStrategy,
        *,
        snapshot_path: Path,
        source_dir: Path,
        min_games: int,
        v1: bool,
    ) -> None:
        self._notifier = notifier
        self._strategy = strategy
        self._snapshot = snapshot_path
        self._source = source_dir.expanduser()
        self._min_games = min_games
        self._v1 = v1
        self._task: asyncio.Task[None] | None = None

    def start(self) -> bool:
        """Kick off an update in the background. False if one is already running.

        Held here, not in the Telegram listener: a browser relaunch rebuilds the
        listener, and the update has to survive that without a second one starting.
        """
        if self._task is not None and not self._task.done():
            return False
        self._task = asyncio.create_task(self._update_and_report())
        return True

    async def _update_and_report(self) -> None:
        await self._notifier.send(
            "⏳ Actualizando los datos de CounterPredictor y re-exportando el modelo "
            "(puede tardar 15 min o más). El bot sigue apostando con el modelo actual."
        )
        try:
            summary = await self.refresh()
        except Exception as exc:
            logger.error("CounterPredictor update failed: {}", short_error(exc))
            await self._notifier.send(
                f"❌ Falló la actualización del modelo; sigue el anterior.\n\n{short_error(exc)}"
            )
            return
        await self._notifier.send(f"✅ Modelo CounterPredictor actualizado.\n\n{summary}")

    async def refresh(self) -> str:
        backup = self._snapshot.with_name(self._snapshot.name + ".bak")
        had_snapshot = self._snapshot.exists()
        if had_snapshot:
            shutil.copy2(self._snapshot, backup)
        try:
            # ponytail: generous fixed timeouts; the v2 export alone takes ~13 min.
            fetched = await _run([_PYTHON, "update.py"], self._source, 1800)
            export = [_PYTHON, "scripts/export_counterpredictor.py", str(self._source)]
            if self._v1:
                export.insert(2, "--v1")
            exported = await _run(export, None, 3600)
            model = CounterPredictor.from_file(self._snapshot, self._min_games)
            if model is None:
                raise RuntimeError("the export wrote no snapshot")
        except Exception:
            if had_snapshot:
                shutil.copy2(backup, self._snapshot)
            raise
        self._strategy.replace_form(model)
        logger.info("CounterPredictor reloaded: data through {}.", model.as_of.date())
        lines = [
            line
            for line in (fetched + "\n" + exported).splitlines()
            if line.startswith(("kaggle", "liquipedia", "WARNING", "v1 live", "live snapshot"))
            or "update failed" in line
        ]
        return "\n".join(lines)[-1500:] or f"Datos hasta {model.as_of:%Y-%m-%d}."
