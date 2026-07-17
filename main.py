import asyncio
import signal
from collections.abc import Awaitable, Callable

from loguru import logger

from app.bot.browser import persistent_context
from app.bot.login import ensure_logged_in
from app.bot.prediction_page import PredictionPageReader
from app.config.settings import Settings
from app.services.kick_service import KickLiveChecker
from app.services.monitoring_service import MonitoringService
from app.services.notifier import TelegramNotifier
from app.strategies.aggressive_fractional_kelly import AggressiveFractionalKellyStrategy
from app.utils.logger import setup_logger


async def _run_forever(name: str, coro_factory: Callable[[], Awaitable[None]]) -> None:
    """Top-level safety net: a service that raises past its own internal
    handling gets logged and restarted instead of taking the process down."""
    while True:
        try:
            await coro_factory()
        except Exception as exc:
            logger.exception("{} crashed, restarting in 10s: {}", name, exc)
            await asyncio.sleep(10)


async def main() -> None:
    settings = Settings()  # type: ignore[call-arg]  # values load from .env at runtime
    setup_logger(settings.log_dir, settings.log_level)
    notifier = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
    await notifier.connect()  # fail fast: no Telegram, no bot
    strategy = AggressiveFractionalKellyStrategy(
        min_pct=settings.strategy_min_pct,
        max_pct=settings.strategy_max_pct,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async with persistent_context(settings.chrome_profile_dir, settings.headless) as context:
        page = await ensure_logged_in(
            context, settings.forg1_url, settings.storage_state_path, notifier
        )
        reader = PredictionPageReader(page, settings.forg1_url)
        # The context is passed ON PURPOSE (owner's decision): when the stream
        # goes live the checker opens kick.com in a tab, and closes it when the
        # stream ends. Known trade-offs, accepted: the tab plays the video
        # (bandwidth) and Kick may rotate the session cookies forg1 authenticates
        # through, logging forg1 out — if that happens the monitor detects it,
        # alerts on Telegram, and recovers after a manual re-login (VNC on the VPS).
        kick_checker = KickLiveChecker(
            settings.kick_channel,
            settings.kick_check_interval_seconds,
            notifier,
            context,
        )
        monitor = MonitoringService(
            reader,
            strategy,
            notifier,
            settings.seen_predictions_path,
            settings.poll_interval_idle_seconds,
            settings.poll_interval_active_seconds,
            settings.page_refresh_after_idle_seconds,
            settings.prediction_stabilization_seconds,
            settings.balance_refresh_interval_seconds,
            # balance-refresh clicks only happen while the stream is live —
            # no automated clicking around the clock for Cloudflare to flag.
            stream_is_live=lambda: kick_checker.is_live,
        )

        tasks = [
            asyncio.create_task(_run_forever("monitoring_service", monitor.run)),
            asyncio.create_task(_run_forever("kick_checker", kick_checker.run)),
        ]

        await stop.wait()
        logger.info("Shutdown signal received (Ctrl+C or SIGTERM) — stopping gracefully.")
        try:
            await notifier.send("🛑 Bot forg1 detenido")
        except Exception as exc:  # a failed notify must not block shutdown
            logger.error("Could not send shutdown notification: {}", exc)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    # persistent_context's __aexit__ has already closed the browser here —
    # any pages it holds (including a Kick tab, if one was open) go with it.

    logger.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
