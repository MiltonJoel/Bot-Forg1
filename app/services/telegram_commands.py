import asyncio
from collections.abc import Callable

from loguru import logger

from app.services.notifier import TelegramNotifier
from app.utils.retry import short_error

# Accepted spellings for the one command there is. Kept deliberately small: this
# is a remote control on a bot that spends real points, not a chat interface.
#
# Deliberately NOT /reset or /restart. This bot token is shared with OpenClaw,
# which has ~60 commands of its own registered on it — including /reset ("Reset
# the current session") and /restart ("Restart OpenClaw"). Either would be
# swallowed by that bot and never reach the betting loop. These spellings
# collide with nothing it registers.
_RESET_COMMANDS = {
    "/reainiciobot",
    "reainiciobot",
    "/reiniciobot",
    "reiniciobot",
    "/reiniciarbot",
    "reiniciarbot",
}
# Re-download CounterPredictor's data, re-export it and reload it (model_update.py).
_UPDATE_COMMANDS = {"/actualizarmodelo", "actualizarmodelo"}


class TelegramCommandListener:
    """Watches the owner's Telegram chat for a reset request.

    Sending `/reainiciobot` tears the browser session down and relaunches it — a fresh
    Chrome, a fresh login from the saved cookies, and any stuck modal or wedged
    tab gone. It is the remote equivalent of restarting the service, for when
    the bot is misbehaving and VNC is inconvenient.

    Only messages from the configured chat are honoured (see
    TelegramNotifier.pending_commands): the token is a shared secret that lives
    in .env and on the VPS, so the chat check is what actually stops a stranger
    from resetting the bot.
    """

    def __init__(
        self,
        notifier: TelegramNotifier,
        request_reset: asyncio.Event,
        poll_seconds: float = 10.0,
        start_model_update: Callable[[], bool] | None = None,
    ) -> None:
        self._notifier = notifier
        self._request_reset = request_reset
        self._start_model_update = start_model_update
        self._poll_seconds = poll_seconds
        # Start from whatever is already queued so a command sent while the bot
        # was down does not fire the moment it comes back.
        self._offset = 0
        self._primed = False
        self._conflict_warned = False

    async def run(self) -> None:
        logger.info(
            "Telegram command listener started (send /reainiciobot to relaunch the browser)."
        )
        while True:
            try:
                texts, self._offset = await self._notifier.pending_commands(self._offset)
            except Exception as exc:
                # Telegram being unreachable must never take the bot down; the
                # command channel is a convenience, not a dependency.
                reason = short_error(exc)
                if "409" in reason and not self._conflict_warned:
                    # Two programs polling getUpdates on one token. Whoever polls
                    # first consumes the update, so commands vanish at random
                    # rather than failing loudly. Say so once, plainly.
                    self._conflict_warned = True
                    logger.error(
                        "Telegram 409 Conflict: another program is polling this bot token "
                        "(OpenClaw is registered on it). Commands to this bot will be missed "
                        "at random until it gets its own token from @BotFather."
                    )
                logger.warning("Could not poll Telegram commands: {}", reason)
                await asyncio.sleep(self._poll_seconds)
                continue

            if not self._primed:
                # First pass only advances the offset past the backlog.
                self._primed = True
                texts = []

            if any(text.lower() in _RESET_COMMANDS for text in texts):
                logger.warning("Reset requested from Telegram — relaunching the browser session.")
                await self._notifier.send("🔄 Reiniciando el navegador del bot…")
                self._request_reset.set()

            if self._start_model_update is not None and any(
                text.lower() in _UPDATE_COMMANDS for text in texts
            ):
                logger.info("Model update requested from Telegram.")
                if not self._start_model_update():
                    await self._notifier.send("⏳ Ya hay una actualización del modelo en curso.")

            await asyncio.sleep(self._poll_seconds)
