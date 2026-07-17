import asyncio
import json
import re
import time
from datetime import datetime, timezone
from typing import Any

from loguru import logger
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from app.bot.login import looks_logged_out
from app.models.prediction import Outcome, Prediction
from app.utils.retry import flaky_page_action

# Verified 2026-07-02 against the live, logged-in page. Forg1 is a Next.js app
# that embeds its data as a React Server Components payload directly in the
# HTML (self.__next_f.push([1, "<escaped JSON>"])) — there's no separate REST
# call to hook into, and no CSS-selector guessing needed either: every
# prediction (resolved or open) shows up as an object with an "outcomes" array
# in that payload. This is far more robust than scraping rendered text, since
# it's the actual data model, not Tailwind classes that can change on a
# redeploy. Confirmed real fields (from 10 resolved predictions):
#   id, title, status, created_by, locks_at, resolved_at, winning_outcome_id,
#   created_at, min_bet, max_bet,
#   outcomes: [{id, color, label, position, total_points, total_bettors}, ...]
# A currently-open prediction just has status != "resolved" and no
# resolved_at/winning_outcome_id — never actually observed one live in this
# session (site was idle throughout), so that part is inferred from the
# schema, not directly confirmed.
_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)\s*</script>', re.DOTALL)
_OUTCOMES_KEY_RE = re.compile(r'"outcomes"\s*:\s*\[')

# "Tu balance" in the header lives in <div class="... text-accent font-mono ...">
# <span>N pts</span></div>. Scope to that div: the pattern alone also matches the
# pool/stat spans ("65.000 pts", "70.000 pts"), which use text-muted-foreground/
# text-foreground — only the balance is text-accent, so this avoids reading a
# pool total as the balance. The pattern still guards against the accent-colored
# clock span (no "pts"). Verified live against the real DOM.
_BALANCE_PATTERN = re.compile(r"^\d[\d.]*\s*pts$", re.IGNORECASE)
_BALANCE_VALUE_SELECTOR = "div.text-accent.font-mono span"

_OUTCOME_SELECTOR = "div[role='button'].forg1-outcome-clickable"
_AMOUNT_INPUT_SELECTOR = 'input[type="number"][data-slot="input"], input[type="number"]'
_PRIMARY_CONFIRM_TEXT = re.compile(r"confirmar jugada", re.IGNORECASE)
_FINAL_CONFIRM_TEXT = re.compile(r"^confirmar$", re.IGNORECASE)
_BALANCE_REFRESH_SELECTOR = 'button[aria-label="Refrescar balance"][title="Refrescar balance"]'

def _extract_prediction_objects(html: str) -> list[dict[str, Any]]:
    """Pulls every {"...", "outcomes": [...]} object out of the page's
    embedded RSC payload, resolved or open alike."""
    results: list[dict[str, Any]] = []
    for match in _PUSH_RE.finditer(html):
        try:
            text = json.loads('"' + match.group(1) + '"')  # unescape JS string literal
        except json.JSONDecodeError:
            continue
        if '"outcomes"' not in text:
            continue

        pos = 0
        while (key_match := _OUTCOMES_KEY_RE.search(text, pos)) is not None:
            i = key_match.start()
            start, depth = i, 0
            while start > 0:
                start -= 1
                if text[start] == "}":
                    depth += 1
                elif text[start] == "{":
                    if depth == 0:
                        break
                    depth -= 1
            end, depth = start, 0
            while end < len(text):
                if text[end] == "{":
                    depth += 1
                elif text[end] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                end += 1
            try:
                results.append(json.loads(text[start : end + 1]))
            except json.JSONDecodeError:
                pass
            pos = end
    return results


# Every field read_active_prediction dereferences to build a Prediction. Objects
# in the RSC payload that carry an "outcomes" array but aren't full predictions
# (fragments emitted while a prediction is live) lack some of these, so guarding
# on them turns a crash (KeyError: 'id') into a clean skip.
_PREDICTION_KEYS = ("id", "title", "outcomes", "min_bet", "max_bet", "locks_at")
_OUTCOME_KEYS = ("id", "label", "total_points", "total_bettors")


def _is_valid_prediction(raw: dict[str, Any]) -> bool:
    """True only for complete prediction objects. `_extract_prediction_objects`
    returns anything with an 'outcomes' array — including partial fragments the
    site embeds during a live prediction — so filter to objects that actually
    have every field we read, or the build below crashes on the first missing
    key and the whole read loop wedges (seen live: `Failed to read ... ('id')`)."""
    if not all(k in raw for k in _PREDICTION_KEYS):
        return False
    outcomes = raw.get("outcomes")
    if not isinstance(outcomes, list) or not outcomes:
        return False
    return all(
        isinstance(o, dict) and all(k in o for k in _OUTCOME_KEYS) for o in outcomes
    )


def _is_bettable(raw: dict[str, Any], now: datetime) -> bool:
    """A prediction accepts bets only while it's open. It's NOT bettable once it
    resolves, and also not once it locks (its cutoff passes): a locked-but-not-
    yet-resolved prediction still lingers in the payload with a frozen pool, which
    is what made the bot re-read a finished prediction ("Kills Jcobb ANUBIS") as
    if it were live for 30+ minutes."""
    if raw.get("status") == "resolved":
        return False
    locks_at = raw.get("locks_at")
    if locks_at:
        try:
            return datetime.fromisoformat(locks_at) > now
        except (ValueError, TypeError):
            pass  # unparseable timestamp -> fall back to the status check alone
    return True


class PredictionPageReader:
    """Turns the live predictions page into a Prediction domain object.
    All scraping lives here — strategies never see a Page."""

    def __init__(self, page: Page, url: str) -> None:
        self._page = page
        self._url = url

    async def _ensure_live_page(self) -> None:
        """Recover from a closed tab: if the page went away (renderer/tab crash),
        open a fresh one in the SAME context. Cookies live on the context, so the
        new page is still logged in. If the whole browser/context is gone,
        new_page() raises and the caller's retry+backoff handles it — a fully dead
        browser needs a process-level restart (e.g. systemd Restart=always)."""
        if not self._page.is_closed():
            return
        logger.warning("Prediction page was closed; reopening it in the same context.")
        self._page = await self._page.context.new_page()
        await self._page.goto(self._url, wait_until="domcontentloaded")
        logger.info("Reopened prediction page at {}.", self._url)

    @flaky_page_action
    async def read_active_prediction(self) -> Prediction | None:
        await self._ensure_live_page()
        response = await self._page.request.get(self._page.url)
        html = await response.text()
        predictions = [p for p in _extract_prediction_objects(html) if _is_valid_prediction(p)]
        now = datetime.now(timezone.utc)
        open_predictions = [p for p in predictions if _is_bettable(p, now)]
        if not open_predictions:
            return None

        raw = open_predictions[0]
        balance = await self.read_balance()
        prediction = Prediction(
            id=raw["id"],
            title=raw["title"],
            outcomes=[
                Outcome(
                    id=o["id"],
                    label=o["label"],
                    total_points=o["total_points"],
                    total_bettors=o["total_bettors"],
                )
                for o in raw["outcomes"]
            ],
            min_bet=raw["min_bet"],
            max_bet=raw["max_bet"],
            locks_at=raw["locks_at"],
            available_points=balance,
            captured_at=datetime.now(timezone.utc),
        )
        logger.info(
            "Prediction read: {} | leading={} ({:.1f}% of {} pool) | balance={}",
            prediction.title,
            prediction.leading_outcome.label,
            prediction.leading_share,
            prediction.total_pool,
            balance,
        )
        return prediction

    @flaky_page_action
    async def read_resolution(self, prediction_id: str) -> str | None:
        """Winning outcome label for a now-resolved prediction, or None if it
        isn't resolved yet (or dropped off the recent list). Lets the monitor
        report the final result after a prediction closes."""
        response = await self._page.request.get(self._page.url)
        html = await response.text()
        for p in _extract_prediction_objects(html):
            if p.get("id") == prediction_id and p.get("status") == "resolved":
                win_id = p.get("winning_outcome_id")
                for outcome in p.get("outcomes", []):
                    if outcome["id"] == win_id:
                        return str(outcome["label"])
                return None
        return None

    async def is_logged_in(self) -> bool:
        """Server-authoritative login check (same one the startup login uses).
        A logged-out session has no header balance/refresh button, so callers
        use this to tell 'you got logged out' apart from a flaky locator."""
        return not await looks_logged_out(self._page, self._url)

    async def read_balance(self) -> int:
        """Reads "Tu balance" from the header — the account's own points,
        not the prediction pool total."""
        span = self._page.locator(_BALANCE_VALUE_SELECTOR).filter(has_text=_BALANCE_PATTERN).first
        # Short timeout: the span is missing entirely when the session is logged
        # out, and the default 30s (x5 flaky retries) wedged the poll loop for
        # minutes per cycle.
        text = await span.inner_text(timeout=5000)
        return int(re.sub(r"[^\d]", "", text))

    async def refresh_balance(self) -> None:
        """Clicks the balance refresh button in the header.

        The button only exists on the logged-in predicciones view. Over a long
        VPS run the single visible tab can drift off it (stale render, Cloudflare
        re-check, session degrade) while HTTP reads keep working — so if the
        button isn't there, reload once to re-render the header before giving up.
        A failure that survives the reload means the visible page is genuinely
        logged out / challenged, which betting also needs to work.
        """
        await self._ensure_live_page()
        button = self._page.locator(_BALANCE_REFRESH_SELECTOR).first
        try:
            await button.wait_for(state="visible", timeout=5000)
        except PlaywrightError:
            logger.warning("Balance button missing; reloading page to recover the header.")
            await self._page.reload(wait_until="domcontentloaded")
            await button.wait_for(state="visible", timeout=15_000)
        await button.click(timeout=5000)
        logger.info("Clicked balance refresh button.")

    async def place_bet(self, side: str, amount: int) -> bool:
        """Executes the bet on the live UI in two steps.

        Flow:
        1. Click the outcome card.
        2. Fill the amount.
        3. Click the "confirmar jugada" button.
        4. Wait for the Cloudflare-gated final "Confirmar" button.
        5. Click the final confirmation and wait for the post-bet summary.

        One attempt only: a partially submitted bet should not be retried.
        """
        logger.info("place_bet start: side={} amount={}", side, amount)
        await self._ensure_live_page()
        # The monitor reads predictions over HTTP (page.request.get), which never
        # renders anything — so the visible DOM still shows whatever was last
        # reloaded, usually a stale/empty state without this prediction's outcome
        # cards. Reload here so the DOM actually reflects the live prediction
        # before we try to click it. (This is why the outcome locator timed out.)
        # Retry the reload + card wait: a reload occasionally draws a Cloudflare
        # re-check (or slow hydration) and the cards don't render in time — seen
        # live, one bet lost this way. Retrying here is safe because nothing has
        # been submitted yet; only the steps AFTER the outcome click must stay
        # single-attempt.
        outcome = self._page.locator(_OUTCOME_SELECTOR).filter(has_text=side).first
        for attempt in range(1, 4):
            await self._page.reload(wait_until="domcontentloaded")
            try:
                await outcome.wait_for(state="visible", timeout=15_000)
                break
            except PlaywrightError:
                logger.warning(
                    "Outcome card for {} not visible (attempt {}/3) — reloading again.",
                    side,
                    attempt,
                )
        else:
            raise PlaywrightError(
                f"Outcome card for {side!r} never became visible after 3 reloads."
            )
        logger.info("Selecting outcome card for {}", side)
        await outcome.click(timeout=5000)

        amount_input = self._page.locator(_AMOUNT_INPUT_SELECTOR).first
        await amount_input.wait_for(state="visible", timeout=5000)
        logger.info("Filling bet amount {}", amount)
        await amount_input.fill(str(amount), timeout=5000)
        await amount_input.press("Tab")

        primary_confirm = self._page.get_by_role("button", name=_PRIMARY_CONFIRM_TEXT).first
        await primary_confirm.wait_for(state="visible", timeout=5000)
        await self._wait_until_enabled(primary_confirm, timeout_seconds=10)
        logger.info("Clicking primary confirm button")
        await primary_confirm.click(timeout=5000)

        final_confirm = self._page.get_by_role("button", name=_FINAL_CONFIRM_TEXT).first
        try:
            logger.info("Waiting for final Confirmar button (Cloudflare gate)")
            await final_confirm.wait_for(state="visible", timeout=35_000)
            await self._wait_until_enabled(final_confirm, timeout_seconds=35)
            logger.info("Clicking final Confirmar button")
            await final_confirm.click(timeout=5000)
        except PlaywrightError as exc:
            logger.error("Final confirmation did not appear or was not clickable: {}", exc)
            return False

        try:
            logger.info("Waiting for post-bet confirmation text")
            await self._page.wait_for_function(
                """() => /tu jugada\\s+\\d[\\d.]*\\s*pts/i.test(
                    document.body.innerText.replace(/\\s+/g, ' ')
                )""",
                timeout=15_000,
            )
        except PlaywrightError:
            logger.error(
                "Bet confirmation click completed, but the post-bet summary never appeared."
            )
            return False

        logger.info("Bet placed: {} points on {}", amount, side)
        return True

    async def _wait_until_enabled(self, locator: Any, timeout_seconds: float) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                if await locator.is_enabled():
                    return
            except PlaywrightError:
                pass
            await asyncio.sleep(0.2)
        raise PlaywrightError(f"Timed out waiting for enabled element: {locator}")

    async def refresh(self) -> None:
        logger.debug("Refreshing predictions page.")
        await self._page.reload(wait_until="domcontentloaded")
