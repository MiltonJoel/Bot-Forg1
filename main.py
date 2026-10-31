import asyncio
import contextlib
import signal
import traceback
from collections.abc import Awaitable, Callable

from loguru import logger

from app.bot.browser import ProfileInUse, persistent_context
from app.bot.login import ensure_logged_in
from app.bot.prediction_page import PredictionPageReader
from app.config.settings import Settings
from app.services.boxes_service import BoxOpener
from app.services.kick_service import KickLiveChecker
from app.services.model_update import CounterPredictorUpdater
from app.services.monitoring_service import MonitoringService
from app.services.notifier import TelegramNotifier
from app.services.telegram_commands import TelegramCommandListener
from app.strategies.base_strategy import BettingStrategy
from app.strategies.factory import build_strategy
from app.utils.logger import setup_logger
from app.utils.retry import short_error


def _frames(exc: BaseException) -> str:
    """Where it crashed, without the exception text. logger.exception() would
    print str(exc) in full, and a Playwright error's "Call log" carries the
    forg1_session cookie (see short_error)."""
    return "".join(traceback.format_tb(exc.__traceback__)).rstrip()


async def _run_forever(name: str, coro_factory: Callable[[], Awaitable[None]]) -> None:
    """Top-level safety net: a service that raises past its own internal
    handling gets logged and restarted instead of taking the process down."""
    while True:
        try:
            await coro_factory()
        except Exception as exc:
            logger.error(
                "{} crashed, restarting in 10s: {}\n{}", name, short_error(exc), _frames(exc)
            )
            await asyncio.sleep(10)


async def _browser_session(
    settings: Settings,
    notifier: TelegramNotifier,
    strategy: BettingStrategy,
    model_updater: CounterPredictorUpdater,
    stop: asyncio.Event,
    reset: asyncio.Event,
    announce_relaunch: bool,
) -> None:
    """One full browser lifetime: launch Chrome, log in, run the services, and
    return when the user stops the bot, the browser dies, or a Telegram /reset
    asks for a relaunch.

    Chrome dying (crash, manual close, GPU fault) used to strand the bot: the
    page-level reopener can't help because new_page() needs a live context, so
    the monitor just logged "Backing off" forever while looking alive. Now the
    context's close event is watched here, and a death tears this session down
    so the caller can relaunch a fresh browser.
    """
    async with persistent_context(settings.chrome_profile_dir, settings.headless) as context:
        browser_dead = asyncio.Event()
        context.on("close", lambda _context: browser_dead.set())

        page = await ensure_logged_in(
            context, settings.forg1_url, settings.storage_state_path, notifier
        )
        if announce_relaunch:
            await notifier.send("✅ Navegador relanzado y sesión recuperada — el bot sigue.")
        reader = PredictionPageReader(page, settings.forg1_url)
        # The Kick viewer stays (owner's decision) but is no longer a tab in THIS
        # browser: Cloudflare 403s the player's API when Chrome is driven over
        # CDP, so the tab watched nothing. It is now its own plain Chrome process
        # on its own profile copy — which also ends the old hazard of Kick
        # rotating the cookies forg1 authenticates through, since the two
        # browsers no longer share a cookie jar.
        kick_checker = KickLiveChecker(
            settings.kick_channel,
            settings.kick_check_interval_seconds,
            notifier,
            settings.kick_viewer_profile_dir,
            viewer_extension=settings.kick_viewer_extension_dir,
        )
        monitor = MonitoringService(
            reader,
            strategy,
            notifier,
            seen_predictions_path=settings.seen_predictions_path,
            poll_idle_seconds=settings.poll_interval_idle_seconds,
            poll_active_seconds=settings.poll_interval_active_seconds,
            refresh_after_idle_seconds=settings.page_refresh_after_idle_seconds,
            stabilization_seconds=settings.prediction_stabilization_seconds,
            balance_refresh_interval_seconds=settings.balance_refresh_interval_seconds,
            bankroll_stop_pct=settings.bankroll_stop_pct,
            results_log_path=settings.results_log_path,
            skipped_log_path=settings.skipped_log_path,
            resolve_maps=settings.resolve_map_names,
            polymarket_lookup=settings.polymarket_lookup,
            # Reads and balance-refresh clicks pause only while Kick CONFIRMS the
            # stream is offline — no automated traffic around the clock for
            # Cloudflare to flag, and an unknown Kick state never blinds the bot.
            stream_is_live=lambda: not kick_checker.is_offline,
        )

        commands = TelegramCommandListener(notifier, reset, start_model_update=model_updater.start)
        boxes = BoxOpener(context, settings.boxes_url, notifier)

        tasks = [
            asyncio.create_task(_run_forever("monitoring_service", monitor.run)),
            asyncio.create_task(_run_forever("kick_checker", kick_checker.run)),
            asyncio.create_task(_run_forever("telegram_commands", commands.run)),
            asyncio.create_task(_run_forever("boxes", boxes.run)),
            asyncio.create_task(reset.wait()),
            asyncio.create_task(stop.wait()),
            asyncio.create_task(browser_dead.wait()),
        ]
        # First thing to finish is either a stop signal or the browser dying
        # (service tasks run forever). Everything else gets cancelled.
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    # persistent_context's __aexit__ has closed (or tolerated an already-dead)
    # browser here — any pages it held (including a Kick tab) go with it.


async def main() -> None:
    settings = Settings()  # type: ignore[call-arg]  # values load from .env at runtime
    setup_logger(settings.log_dir, settings.log_level)
    notifier = TelegramNotifier(
        settings.telegram_bot_token,
        settings.telegram_chat_id,
        settings.telegram_timeout_seconds,
        settings.telegram_attempts,
    )
    await notifier.connect()  # fail fast: no Telegram, no bot
    strategy = build_strategy(settings)
    # Built here, not per browser session, so a relaunch neither loses a
    # running update nor lets a second one start.
    model_updater = CounterPredictorUpdater(
        notifier,
        strategy,
        snapshot_path=settings.counterpredictor_path,
        source_dir=settings.counterpredictor_source_dir,
        min_games=settings.strategy_form_min_games,
        v1=settings.counterpredictor_v1,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    reset = asyncio.Event()
    relaunches = 0
    while not stop.is_set():
        try:
            await _browser_session(
                settings, notifier, strategy, model_updater, stop, reset, relaunches > 0
            )
        except ProfileInUse as exc:
            # Not transient: a relaunch hits the same live Chrome. Say it once and go.
            logger.error("{}", exc)
            with contextlib.suppress(Exception):
                await notifier.send(f"🛑 Bot detenido: {exc}")
            raise SystemExit(1)
        except Exception as exc:
            logger.error("Browser session crashed: {}\n{}", short_error(exc), _frames(exc))
        if stop.is_set():
            break
        relaunches += 1

        if reset.is_set():
            # Asked for on purpose from Telegram, so skip the "Chrome died"
            # alarm and the cooldown — the profile lock is released cleanly here.
            reset.clear()
            logger.info("Relaunching the browser on request (relaunch #{}).", relaunches)
            continue

        logger.warning("Chrome closed itself (relaunch #{}) — relaunching in 15s.", relaunches)
        try:
            await notifier.send(
                "⚠️ La ventana de Chrome se cerró sola — relanzando el navegador en 15s…"
            )
        except Exception as exc:
            logger.error("Could not send relaunch notification: {}", short_error(exc))
        # ponytail: fixed 15s pause; enough to let a crashed Chrome release the
        # profile lock, and it rate-limits a relaunch loop if Chrome dies at boot.
        await asyncio.sleep(15)

    logger.info("Shutdown signal received (Ctrl+C or SIGTERM) — stopping gracefully.")
    try:
        await notifier.send("🛑 Bot forg1 detenido")
    except Exception as exc:  # a failed notify must not block shutdown
        logger.error("Could not send shutdown notification: {}", short_error(exc))
    logger.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
