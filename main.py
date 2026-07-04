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
    strategy = AggressiveFractionalKellyStrategy()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async with persistent_context(settings.chrome_profile_dir, settings.headless) as context:
        page = await ensure_logged_in(context, settings.forg1_url, settings.storage_state_path)
        reader = PredictionPageReader(page, settings.forg1_url)
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
        )
        # Alert-only (context omitted): the checker pings Kick's public API and
        # notifies on live/offline. It must NOT open kick.com in this shared
        # context — doing so rotates/clears the Kick session cookies that forg1
        # authenticates through, which logs you out of forg1.
        kick_checker = KickLiveChecker(
            settings.kick_channel,
            settings.kick_check_interval_seconds,
            notifier,
            context,
        )

        tasks = [
            asyncio.create_task(_run_forever("monitoring_service", monitor.run)),
            asyncio.create_task(_run_forever("kick_checker", kick_checker.run)),
        ]

        await stop.wait()
        logger.info("Shutdown signal received (Ctrl+C or SIGTERM) — stopping gracefully.")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    # persistent_context's __aexit__ has already closed the browser here —
    # any pages it holds (including a Kick tab, if one was open) go with it.

    logger.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
