import asyncio
import json
import socket
import urllib.error
import urllib.request

from loguru import logger
from playwright.async_api import BrowserContext, Page

from app.services.notifier import TelegramNotifier

# Kick sits behind Cloudflare, which 403s a bare "Mozilla/5.0". A full
# desktop-browser UA passes the bot check — no headless browser needed.
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class KickLiveChecker:
    """Pings Kick's public channel API on an interval.

    When the stream goes live it notifies and, if a browser context is
    available, opens Kick after a short delay in a new tab. The delay avoids
    racing the initial Forg1 login/bootstrap sequence. When the stream ends it
    notifies and closes the tab.
    """

    def __init__(
        self,
        channel: str,
        check_interval_seconds: int,
        notifier: TelegramNotifier,
        context: BrowserContext | None = None,
        open_delay_seconds: float = 2.0,
    ) -> None:
        self._api_url = f"https://kick.com/api/v2/channels/{channel}"
        self._channel_url = f"https://kick.com/{channel}"
        self._interval = check_interval_seconds
        self._notifier = notifier
        self._context = context
        self._open_delay_seconds = open_delay_seconds
        self._was_live: bool | None = None
        self._live_page: Page | None = None

    async def run(self) -> None:
        logger.info("Kick live-check started (every {}s).", self._interval)
        while True:
            try:
                is_live = await asyncio.to_thread(self._check_live)
            except (urllib.error.URLError, socket.gaierror, TimeoutError) as exc:
                # Transient network loss (e.g. the machine just woke from sleep and
                # DNS isn't up yet — this is the "Name or service not known" case).
                # Not a real failure: stay quiet and retry on the next tick. State
                # is left untouched, so no false offline notification is sent.
                logger.warning("Kick live-check skipped (network unavailable): {}", exc)
                await asyncio.sleep(self._interval)
                continue
            except Exception as exc:
                logger.error("Kick live-check failed: {}", exc)
                await asyncio.sleep(self._interval)
                continue

            if is_live and not self._was_live:
                await self._notifier.send(f"🟢 El stream está en vivo: {self._channel_url}")
                logger.info("Kick stream went live.")
                await self._open_stream()
            elif self._was_live and not is_live:
                await self._notifier.send("🔴 El stream de Kick se cayó o terminó.")
                logger.warning("Kick stream went offline.")
                await self._close_stream()

            self._was_live = is_live
            await asyncio.sleep(self._interval)

    async def _open_stream(self) -> None:
        if self._context is None:
            return
        try:
            if self._open_delay_seconds > 0:
                await asyncio.sleep(self._open_delay_seconds)
            self._live_page = await self._context.new_page()
            await self._live_page.goto(self._channel_url, wait_until="domcontentloaded")
            logger.info("Opened stream tab: {}", self._channel_url)
        except Exception as exc:  # a failed tab must never kill the checker
            logger.error("Could not open stream tab: {}", exc)
            self._live_page = None

    async def _close_stream(self) -> None:
        if self._live_page is None:
            return
        try:
            await self._live_page.close()
        except Exception as exc:
            logger.error("Could not close stream tab: {}", exc)
        finally:
            self._live_page = None

    def _check_live(self) -> bool:
        request = urllib.request.Request(self._api_url, headers={"User-Agent": _BROWSER_UA})
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.loads(response.read())
        return data.get("livestream") is not None
