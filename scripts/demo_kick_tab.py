"""Headed demo of the live-stream tab feature, no forg1 login needed.
Opens a real Chrome on the configured Kick channel when it's live.
Ctrl-C or `pkill -f demo_kick_tab` to stop."""
import asyncio

from playwright.async_api import async_playwright

from app.config.settings import Settings
from app.services.kick_service import KickLiveChecker
from app.services.notifier import TelegramNotifier


async def main() -> None:
    s = Settings()  # type: ignore[call-arg]
    notifier = TelegramNotifier(s.telegram_bot_token, s.telegram_chat_id)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context()
        checker = KickLiveChecker(
            s.kick_channel, s.kick_check_interval_seconds, notifier, context
        )
        await checker.run()  # loops forever; opens the tab on the live edge


if __name__ == "__main__":
    asyncio.run(main())
