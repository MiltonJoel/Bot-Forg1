import asyncio
import json
import time
from pathlib import Path

from loguru import logger

from app.bot.prediction_page import PredictionPageReader
from app.models.prediction import BetDecision, Prediction
from app.services.notifier import TelegramNotifier
from app.strategies.base_strategy import BettingStrategy


class MonitoringService:
    """Detects new predictions, runs the strategy, executes the bet on the
    page, and reports the result on Telegram."""

    def __init__(
        self,
        reader: PredictionPageReader,
        strategy: BettingStrategy,
        notifier: TelegramNotifier,
        seen_predictions_path: Path,
        poll_idle_seconds: int,
        poll_active_seconds: int,
        refresh_after_idle_seconds: int,
        stabilization_seconds: int,
        balance_refresh_interval_seconds: int,
    ) -> None:
        self._reader = reader
        self._strategy = strategy
        self._notifier = notifier
        self._seen_path = seen_predictions_path
        self._poll_idle = poll_idle_seconds
        self._poll_active = poll_active_seconds
        self._refresh_after_idle = refresh_after_idle_seconds
        self._stabilization_seconds = stabilization_seconds
        self._balance_refresh_interval_seconds = balance_refresh_interval_seconds
        self._last_seen_id = self._load_last_seen()
        # ponytail: one in-memory pending slot (predictions run one at a time on
        # forg1). Lost on restart -> a result mid-flight won't be reported; fine.
        self._pending: tuple[Prediction, BetDecision] | None = None
        self._last_balance_refresh = time.monotonic()

    def _load_last_seen(self) -> str | None:
        if self._seen_path.exists():
            data: dict[str, str] = json.loads(self._seen_path.read_text())
            return data.get("last_id")
        return None

    def _save_last_seen(self, prediction_id: str) -> None:
        self._seen_path.parent.mkdir(parents=True, exist_ok=True)
        self._seen_path.write_text(json.dumps({"last_id": prediction_id}))
        self._last_seen_id = prediction_id

    async def run(self) -> None:
        logger.info("Monitoring started. Waiting for predictions.")
        idle_elapsed = 0
        while True:
            try:
                prediction = await self._reader.read_active_prediction()
            except Exception as exc:
                logger.error("Failed to read prediction page ({}). Backing off.", exc)
                await asyncio.sleep(self._poll_idle)
                continue

            # A pending prediction may have resolved since last poll — report it.
            await self._check_pending_resolution()
            await self._maybe_refresh_balance()

            if prediction is None:
                idle_elapsed += self._poll_idle
                if idle_elapsed >= self._refresh_after_idle:
                    await self._reader.refresh()
                    idle_elapsed = 0
                await asyncio.sleep(self._poll_idle)
                continue

            idle_elapsed = 0
            if prediction.id == self._last_seen_id:
                await asyncio.sleep(self._poll_active)
                continue

            logger.info("New prediction detected: {}", prediction.id)
            await self._handle_prediction(prediction)
            await asyncio.sleep(self._poll_active)

    async def _check_pending_resolution(self) -> None:
        if self._pending is None:
            return
        prediction, decision = self._pending
        try:
            winner = await self._reader.read_resolution(prediction.id)
        except Exception as exc:
            logger.error("Failed to read resolution for {}: {}", prediction.id, exc)
            return
        if winner is None:
            return  # not resolved yet
        await self._notify_result(prediction, decision, winner)
        self._pending = None

    async def _handle_prediction(self, prediction: Prediction) -> None:
        # Mark it immediately so the same snapshot is not re-stabilized on the
        # next polling cycle while we wait for the pool to settle.
        self._save_last_seen(prediction.id)

        if self._stabilization_seconds > 0:
            logger.info(
                "Stabilizing pool for {}s before betting on {}",
                self._stabilization_seconds,
                prediction.id,
            )
            await asyncio.sleep(self._stabilization_seconds)
            try:
                refreshed = await self._reader.read_active_prediction()
            except Exception as exc:
                logger.error("Failed to re-read stabilized prediction {}: {}", prediction.id, exc)
                return
            if refreshed is None or refreshed.id != prediction.id:
                logger.info(
                    "Prediction {} changed during stabilization; skipping stale snapshot.",
                    prediction.id,
                )
                return
            prediction = refreshed
            logger.info(
                "Stabilization window elapsed for {}; proceeding with refreshed snapshot.",
                prediction.id,
            )

        decision = self._strategy.decide(prediction)
        logger.info(
            "Bet decision for {}: should_bet={} side={} amount={} reason={}",
            prediction.id,
            decision.should_bet,
            decision.side,
            decision.amount,
            decision.reason,
        )
        placed = False
        if decision.should_bet and decision.side and decision.amount > 0:
            try:
                logger.info(
                    "Placing bet now for {}: {} points on {}",
                    prediction.id,
                    decision.amount,
                    decision.side,
                )
                placed = await self._reader.place_bet(decision.side, decision.amount)
            except Exception as exc:
                logger.error("Bet execution failed: {}", exc)
        else:
            logger.info(
                "Skipping bet for {} because the strategy did not return a placeable amount.",
                prediction.id,
            )
        await self._notify(prediction, decision, placed)
        # remember it so we can report the result once it resolves
        self._pending = (prediction, decision)

    async def _notify(self, prediction: Prediction, decision: BetDecision, placed: bool) -> None:
        if not decision.should_bet:
            result = "⏭️ Sin apuesta"
        elif placed:
            result = f"✅ Apuesta ejecutada: {decision.amount} puntos a {decision.side}"
        else:
            result = (
                f"❌ No se pudo ejecutar la apuesta de {decision.amount} puntos "
                f"a {decision.side} — apostá manualmente"
            )
        # available_points is the pre-bet balance (still counts the stake). When a
        # bet is placed, also show what's left after it, so the number isn't
        # confusing next to a "bet executed" line.
        if placed:
            remaining = prediction.available_points - decision.amount
            balance_line = (
                f"Tu balance: {prediction.available_points} pts "
                f"(quedan {remaining} pts tras apostar {decision.amount})"
            )
        else:
            balance_line = f"Tu balance: {prediction.available_points} pts"
        message = (
            "🔔 Nueva predicción\n"
            f"{prediction.title}\n"
            f"Líder: {prediction.leading_outcome.label} "
            f"({prediction.leading_share:.1f}% de {prediction.total_pool} pts en juego)\n"
            f"{balance_line}\n\n"
            f"{result}\n({decision.reason})"
        )
        await self._notifier.send(message)
        logger.info("Notification sent for prediction {}", prediction.id)

    async def _maybe_refresh_balance(self) -> None:
        if self._balance_refresh_interval_seconds <= 0:
            return

        now = time.monotonic()
        if now - self._last_balance_refresh < self._balance_refresh_interval_seconds:
            return

        try:
            await self._reader.refresh_balance()
        except Exception as exc:
            logger.error("Failed to refresh balance via header button: {}", exc)
            return

        self._last_balance_refresh = now

    async def _notify_result(
        self, prediction: Prediction, decision: BetDecision, winning_label: str
    ) -> None:
        # reload so "Tu balance" reflects the payout the site just applied
        try:
            await self._reader.refresh()
            balance = await self._reader.read_balance()
        except Exception as exc:
            logger.error("Could not read updated balance after resolution: {}", exc)
            balance = None

        if not decision.should_bet:
            verdict = f"Ganó: {winning_label} (no se había sugerido apuesta)"
        elif decision.side == winning_label:
            verdict = f"✅ Ganó: {winning_label} — el lado sugerido acertó"
        else:
            verdict = f"❌ Ganó: {winning_label} — el lado sugerido ({decision.side}) no acertó"

        balance_line = f"Balance actualizado: {balance} pts" if balance is not None else ""
        message = "\n".join(
            filter(None, ["🏁 Predicción finalizada", prediction.title, verdict, balance_line])
        )
        await self._notifier.send(message)
        logger.info(
            "Result notification sent for prediction {} (winner={})", prediction.id, winning_label
        )
