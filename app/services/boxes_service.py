"""Opens forg1's drop boxes (/cajas) as soon as their cooldown expires.

Each box is an <article> card with one CTA button whose state says everything:
`disabled` + a HH:MM:SS countdown = still cooling down, `disabled` + "Nivel N
para abrir" = locked behind a level, enabled = openable right now. So readiness
is "the CTA is not disabled" — no label matching, which survives a copy change
or a redeploy the way the Tailwind classes would not.

Polling is over HTTP (context.request, which shares the browser's cookies), not
by driving a tab: the countdown ships in the server HTML, so the next check can
sleep exactly until the earliest box is due instead of waking on a fixed timer.
A cooldown is ~24h, so this is a handful of requests a day — Chrome is only
driven on the rare tick where a box is actually openable.
"""

import asyncio
import html as html_module
import random
import re
import time
from pathlib import Path

from loguru import logger
from playwright.async_api import BrowserContext, Error as PlaywrightError, Page

from app.bot.prediction_page import _TURNSTILE_URL_MARK
from app.services.notifier import TelegramNotifier
from app.utils.http import CloudflareBlocked, cloudflare_reason
from app.utils.retry import short_error

# Box name comes from the "Ver premios" button's aria-label ("Ver los premios de
# Caja Bronce") — the one human-readable string in the card that is not styling.
_NAME_RE = re.compile(r'aria-label="Ver los premios de ([^"]+)"')
_TIMER_RE = re.compile(r"\b(\d{2}):(\d{2}):(\d{2})\b")
_BUTTON_RE = re.compile(r"<button\b[^>]*>.*?</button>", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
# Attribute values blanked out before looking for `disabled`: the CTA's class
# list carries Tailwind's `disabled:pointer-events-none disabled:opacity-50`,
# so a plain substring test read EVERY button as disabled and no box was ever
# opened (logs from 2026-10 show "Abrir caja" skipped for days).
_ATTR_VALUE_RE = re.compile(r'"[^"]*"')
_DISABLED_ATTR_RE = re.compile(r"\sdisabled\b")

# Nothing is due for ~24h after an open, but a box can also unlock for reasons
# the page does not count down (a level-up). Cap the wait so those get noticed.
_MAX_SLEEP = 6 * 3600
_MIN_SLEEP = 60.0
# Re-read shortly after opening: the card needs a moment to show its new
# cooldown, and reading it too early would schedule the next check off a stale
# timer (or off no timer at all).
_RECHECK_AFTER_OPEN = 90.0
# forg1 flags instant clicks as spam, so wait a random few seconds on the
# loaded page before clicking — like a person who opened the tab and looked.
_CLICK_DELAY_RANGE = (3.0, 8.0)

# An *enabled* box card has never been seen (every read so far caught both boxes
# cooling down), so the open flow is unobserved markup. When one is finally
# ready we keep the DOM — the served page, and the page again after the click —
# next to the screenshot, so the enabled CTA and whatever the open renders can
# be read afterwards instead of guessed at.
_DUMP_DIR = Path("logs")


def _dump_dom(label: str, page_html: str) -> None:
    path = _DUMP_DIR / f"cajas_{label}_{time.strftime('%Y%m%d-%H%M%S')}.html"
    try:
        _DUMP_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(page_html, encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not save the /cajas DOM to {}: {}", path, short_error(exc))
        return
    logger.info("Saved the /cajas DOM ({} bytes) to {}.", len(page_html), path)


class Box:
    """One box card, as the server rendered it."""

    def __init__(self, name: str, ready: bool, seconds_left: float | None, cta: str) -> None:
        self.name = name
        self.ready = ready
        self.seconds_left = seconds_left
        self.cta = cta

    def __repr__(self) -> str:  # log-friendly
        return f"Box({self.name!r}, ready={self.ready}, left={self.seconds_left}, cta={self.cta!r})"


def _text_of(fragment: str) -> str:
    return html_module.unescape(_TAG_RE.sub(" ", fragment)).strip()


def parse_boxes(page_html: str) -> list[Box]:
    """Every box card on /cajas. The CTA is the card's LAST button — "Ver
    premios" comes first in the markup and is always enabled, so taking the
    last one is what keeps a prize-list button from reading as an open box."""
    boxes = []
    for card in page_html.split("<article ")[1:]:
        card = card.split("</article>")[0]
        name_match = _NAME_RE.search(card)
        buttons = _BUTTON_RE.findall(card)
        # One button means the server sent the card without its CTA (seen
        # 2026-09-07): the lone button is "Ver premios", never an open.
        if not name_match or len(buttons) < 2:
            continue
        cta = buttons[-1]
        cta_tag = _ATTR_VALUE_RE.sub('""', cta.split(">", 1)[0])
        timer = _TIMER_RE.search(_text_of(card))
        boxes.append(
            Box(
                name=name_match.group(1),
                ready=not _DISABLED_ATTR_RE.search(cta_tag),
                seconds_left=(
                    int(timer.group(1)) * 3600 + int(timer.group(2)) * 60 + int(timer.group(3))
                    if timer
                    else None
                ),
                cta=" ".join(_text_of(cta).split()),
            )
        )
    return boxes


def next_check_delay(boxes: list[Box]) -> float:
    """Seconds to sleep: until the soonest countdown expires, clamped. The
    margin absorbs clock skew between the server's render and our read —
    without it the next fetch lands a second early and finds the button still
    disabled, then sleeps another full cycle."""
    timers = [b.seconds_left for b in boxes if b.seconds_left is not None]
    if not timers:
        return float(_MAX_SLEEP)
    return max(_MIN_SLEEP, min(float(min(timers) + 15), float(_MAX_SLEEP)))


class BoxOpener:
    """Watches /cajas and clicks a box open the moment its cooldown ends."""

    def __init__(
        self,
        context: BrowserContext,
        boxes_url: str,
        notifier: TelegramNotifier,
    ) -> None:
        self._context = context
        self._url = boxes_url
        self._notifier = notifier

    async def run(self) -> None:
        while True:
            try:
                delay = await self._tick()
            except CloudflareBlocked as exc:
                # The monitor already alerts on blocks and this is a free
                # reward, not a bet — retry later instead of adding noise.
                logger.warning("Boxes page is Cloudflare-blocked ({}); retrying later.", exc)
                delay = 1800.0
            except PlaywrightError as exc:
                logger.warning("Boxes check failed: {}", short_error(exc))
                delay = 1800.0
            logger.debug("Next boxes check in {:.0f}s.", delay)
            await asyncio.sleep(delay)

    async def _tick(self) -> float:
        page_html = await self._fetch_html()
        boxes = parse_boxes(page_html)
        if not boxes:
            logger.warning("No box cards found on {} — page layout may have changed.", self._url)
            return 1800.0
        logger.info("Boxes: {}", ", ".join(f"{b.name} [{b.cta}]" for b in boxes))

        ready = [b for b in boxes if b.ready]
        if not ready:
            return next_check_delay(boxes)
        _dump_dom("ready", page_html)
        for box in ready:
            await self._open(box)
        return _RECHECK_AFTER_OPEN

    async def _fetch_html(self) -> str:
        response = await self._context.request.get(self._url)
        page_html = await response.text()
        reason = cloudflare_reason(response.status, dict(response.headers), page_html)
        if reason:
            raise CloudflareBlocked(reason)
        return page_html

    async def _open(self, box: Box) -> None:
        """Clicks the box's CTA in its own tab.

        Its own tab on purpose: the prediction tab is navigated back to
        /predicciones by `_ensure_live_page` on every read, so borrowing it
        would either fight that or strand the monitor on the wrong page.
        """
        logger.info("Opening {} (CTA reads {!r}).", box.name, box.cta)
        slug = box.name.replace(" ", "_").lower()
        page = await self._context.new_page()
        try:
            await page.goto(self._url, wait_until="domcontentloaded")
            if self._turnstile_showing(page, box, "before the click"):
                return
            card = page.locator("article").filter(has_text=box.name).first
            cta = card.locator("button:not([disabled])").last
            await cta.wait_for(timeout=15_000)
            delay = random.uniform(*_CLICK_DELAY_RANGE)
            logger.info("Waiting {:.1f}s before clicking {}.", delay, box.name)
            await page.wait_for_timeout(delay * 1000)
            await cta.click(timeout=15_000)
            # No known second step, but the reward renders with an animation —
            # give it a beat before the screenshot and the DOM dump, which are
            # the only record of what the flow actually looks like.
            await page.wait_for_timeout(4000)
            if self._turnstile_showing(page, box, "after the click"):
                await page.screenshot(path=str(_DUMP_DIR / f"box_{slug}_turnstile.png"))
                return
            await page.screenshot(path=str(_DUMP_DIR / f"box_{slug}.png"))
            _dump_dom(f"opened_{slug}", await page.content())
            await self._notifier.send(f"🎁 Caja abierta: {box.name} (botón: {box.cta!r})")
            logger.info("Opened {}.", box.name)
        except PlaywrightError as exc:
            logger.error("Could not open {}: {}", box.name, short_error(exc))
        finally:
            # Closing the tab also disposes of whatever modal the open left up.
            await page.close()

    @staticmethod
    def _turnstile_showing(page: Page, box: Box, when: str) -> bool:
        """A challenge on /cajas is left alone: a free box is not worth
        fighting Cloudflare over. Logged so it is visible, then the tab closes
        and the next tick tries again."""
        if not any(_TURNSTILE_URL_MARK in f.url for f in page.frames):
            return False
        logger.warning("Turnstile on /cajas {} for {}; leaving it.", when, box.name)
        return True
