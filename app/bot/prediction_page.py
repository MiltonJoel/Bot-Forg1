import asyncio
import json
import re
import time
from enum import Enum, auto
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from loguru import logger
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Frame, Locator, Page
from pydantic import ValidationError

from app.bot.login import is_logged_out_html
from app.models.prediction import Outcome, Prediction
from app.utils.http import CloudflareBlocked, cloudflare_reason
from app.utils.retry import flaky_page_action, short_error

__all__ = ["CloudflareBlocked", "PredictionPageReader"]


# --- RSC payload parsing -----------------------------------------------------
# Verified 2026-07-02 against the live, logged-in page. Forg1 is a Next.js app
# that embeds its data as a React Server Components payload in the HTML
# (self.__next_f.push([1, "<escaped JSON>"])) — there is no REST call to hook
# into. Every prediction, resolved or open, appears as an object with an
# "outcomes" array. Parsing the actual data model beats scraping rendered text,
# which is just Tailwind classes that change on a redeploy.
#
# Confirmed fields (from 10 resolved predictions): id, title, status,
# created_by, locks_at, resolved_at, winning_outcome_id, created_at, min_bet,
# max_bet, outcomes: [{id, color, label, position, total_points,
# total_bettors}, ...]. An open prediction has status != "resolved" and no
# resolved_at/winning_outcome_id — inferred from the schema, never observed
# live (the site was idle throughout that session).
_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)\s*</script>', re.DOTALL)
_OUTCOMES_KEY_RE = re.compile(r'"outcomes"\s*:\s*\[')

# --- DOM selectors -----------------------------------------------------------
# "Tu balance" lives in <div class="... text-accent font-mono ..."><span>N pts</span></div>.
# Scoping to that div matters: the "N pts" pattern alone also matches the pool
# stat spans, which use text-muted-foreground/text-foreground — only the balance
# is text-accent. The pattern also rules out the accent-colored clock (no "pts").
_BALANCE_VALUE_SELECTOR = "div.text-accent.font-mono span"
_BALANCE_PATTERN = re.compile(r"^\d[\d.]*\s*pts$", re.IGNORECASE)
_BALANCE_REFRESH_SELECTOR = 'button[aria-label="Refrescar balance"][title="Refrescar balance"]'

_OUTCOME_SELECTOR = "div[role='button'].forg1-outcome-clickable"
_AMOUNT_INPUT_SELECTOR = 'input[type="number"][data-slot="input"], input[type="number"]'
_PRIMARY_CONFIRM_TEXT = re.compile(r"confirmar jugada", re.IGNORECASE)
_FINAL_CONFIRM_TEXT = re.compile(r"^confirmar$", re.IGNORECASE)
# Radix renders the bet modal's backdrop as this; while it exists it swallows
# every click on the page, including the header's balance refresh.
_DIALOG_OVERLAY_SELECTOR = '[data-slot="dialog-overlay"]'
_DIALOG_CONTENT_SELECTOR = '[data-slot="dialog-content"], [role="dialog"]'
# Turnstile renders cross-origin. In MANAGED mode it shows a checkbox that has
# to actually be clicked; in non-interactive mode it solves itself. Both were
# happening at random, which is why the confirm gate failed ~25% of the time.
# Matched against frame URLs, NOT as an iframe CSS selector — see _turnstile_frame.
_TURNSTILE_URL_MARK = "challenges.cloudflare.com"
_TURNSTILE_CHECKBOX_SELECTOR = 'input[type="checkbox"]'
# Turnstile encodes the widget's state in its own iframe URL: a fresh challenge
# says "new", one that already errored says this. Seen live on the VPS.
_TURNSTILE_FAILED_MARK = "failure_retry"
# Cloudflare writes the solved token into this hidden field; while it is empty
# the site keeps CONFIRMAR disabled. Logging it says whether the widget solved.
_TURNSTILE_TOKEN_SELECTOR = 'input[name="cf-turnstile-response"], input[type="hidden"]'
# forg1 added a "Jugar" screen in front of /predicciones on 2026-10-31: the URL
# stays the same, but the outcome cards only render after it is clicked.
# Matched as the WHOLE text of a clickable element, so "Confirmar jugada" (the
# bet modal's first step) and anything else that merely contains the word
# can never match.
_PLAY_GATE_SELECTOR = "button, a, [role='button']"
_PLAY_GATE_TEXT = re.compile(r"^\W*jugar(\s+ahora)?\W*$", re.IGNORECASE)
# If a click does not make the screen go away, do not hammer it every poll.
_PLAY_GATE_COOLDOWN_SECONDS = 60.0
_play_gate_clock = time.monotonic
# A challenge widget clicked 50 times in 25s is useless and looks like abuse.
_MAX_TURNSTILE_CLICKS = 3
# For each matching outcome card, is it inside the market box titled `title`?
# See PredictionPageReader._card_in_market.
_CARD_IN_MARKET_JS = """(cards, {title, count, card}) => {
    const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
    const want = norm(title);
    const hits = [];
    cards.forEach((el, i) => {
        let node = el.parentElement;
        while (node && !norm(node.textContent).includes(want)) node = node.parentElement;
        if (node && node.querySelectorAll(card).length === count) hits.push(i);
    });
    return hits;
}"""
_POST_BET_SUMMARY_JS = """() => /tu jugada\\s+\\d[\\d.]*\\s*pts/i.test(
    document.body.innerText.replace(/\\s+/g, ' ')
)"""
# click() auto-waits for actionability, so these are the total budget for the
# button to become clickable — not a poll interval. The final one is generous
# because it sits behind a Turnstile challenge that can take a while to clear.
_CONFIRM_CLICK_TIMEOUT_MS = 15_000
# The Turnstile gate either opens within a few seconds or not at all, so waiting
# a full minute mostly burns the betting window. Wait less, retry more.
_FINAL_CONFIRM_CLICK_TIMEOUT_MS = 25_000
# Gap between confirm attempts while the Turnstile widget settles.
_CONFIRM_POLL_SECONDS = 0.5


class _AmountNotAccepted(RuntimeError):
    """The stake field would not hold the intended amount. Never confirm after
    this — the site would take whatever the field actually contains."""


class _AmbiguousOutcome(RuntimeError):
    """More than one outcome card matches the side's label. Raised before any
    click, so no bet exists."""


class _ConfirmResult(Enum):
    """Only PLACED is certain. Everything else means "unknown" — the site's bet
    history showed bets landing that this code had reported as failures, so an
    unclickable Confirmar is NOT evidence that nothing was submitted."""

    PLACED = auto()  # post-bet summary rendered; the bet is definitely on
    UNKNOWN = auto()  # may or may not have landed; never retry, tell a human


# Every field read_active_prediction dereferences. The payload also carries
# partial fragments with an "outcomes" array but none of the rest, so these are
# what tell a real prediction apart from one (see _is_valid_prediction).
_PREDICTION_KEYS = ("id", "title", "outcomes", "min_bet", "max_bet", "locks_at")
_OUTCOME_KEYS = ("id", "label", "total_points", "total_bettors")


def _enclosing_object_span(text: str, index: int) -> tuple[int, int]:
    """Span of the JSON object literal wrapping `index`, as (open, close).

    Brace-counts outward in both directions. A truncated payload chunk has no
    balanced pair to find, in which case the span falls back to the text bounds
    and json.loads rejects the slice — that is the intended failure mode.
    """
    start, depth = index, 0
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
    return start, end


def _extract_prediction_objects(html: str) -> list[dict[str, Any]]:
    """Every {..., "outcomes": [...]} object in the page's embedded RSC
    payload, resolved or open alike."""
    results: list[dict[str, Any]] = []
    for chunk in _PUSH_RE.finditer(html):
        try:
            text = json.loads('"' + chunk.group(1) + '"')  # unescape JS string literal
        except json.JSONDecodeError:
            continue

        pos = 0
        while (key := _OUTCOMES_KEY_RE.search(text, pos)) is not None:
            start, end = _enclosing_object_span(text, key.start())
            try:
                results.append(json.loads(text[start : end + 1]))
            except json.JSONDecodeError:
                pass
            # Never rewind: on a truncated chunk the span can close before the
            # key it was searched from, and `pos = end` alone would re-find the
            # same match forever — an unlogged hang of the whole poll loop.
            pos = max(end, key.end())
    return results


def _is_valid_prediction(raw: dict[str, Any]) -> bool:
    """True only for complete prediction objects. Fragments the site embeds
    during a live prediction carry an "outcomes" array but lack the rest, and
    building a Prediction from one crashes the read loop on the first missing
    key (seen live: `Failed to read ... ('id')`)."""
    if not all(k in raw for k in _PREDICTION_KEYS):
        return False
    outcomes = raw.get("outcomes")
    if not isinstance(outcomes, list) or not outcomes:
        return False
    return all(isinstance(o, dict) and all(k in o for k in _OUTCOME_KEYS) for o in outcomes)


def _is_bettable(raw: dict[str, Any], now: datetime) -> bool:
    """A prediction accepts bets only while open AND before its lock time.
    A locked-but-unresolved prediction lingers in the payload with a frozen
    pool — that is what made the bot re-read a finished prediction ("Kills
    Jcobb ANUBIS") as live for 30+ minutes."""
    if raw.get("status") == "resolved":
        return False
    locks_at = raw.get("locks_at")
    if locks_at:
        try:
            return datetime.fromisoformat(locks_at) > now
        except (ValueError, TypeError):
            pass  # unparseable/naive timestamp -> fall back to the status check
    return True


def _build_prediction(raw: dict[str, Any], balance: int, now: datetime) -> Prediction:
    """Raises pydantic's ValidationError if a field has the wrong type."""
    return Prediction(
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
        captured_at=now,
    )


def _describe_validation(exc: ValidationError) -> str:
    """Field, problem and offending value for each error. The whole point of
    this log line: short_error() keeps only "1 validation error for
    Prediction", which says nothing about WHICH field. Market data only, no
    headers or cookies can be in here."""
    parts = []
    for err in exc.errors():
        where = ".".join(str(p) for p in err.get("loc", ()))
        value = repr(err.get("input"))[:80]
        parts.append(f"{where}: {err.get('msg')} (got {value})")
    return "; ".join(parts)


def _path_of(url: str) -> str:
    """Comparable path of a URL: no query, no fragment, no trailing slash."""
    return urlparse(url).path.rstrip("/")


class PredictionPageReader:
    """Turns the live predictions page into a Prediction domain object.
    All scraping lives here — strategies never see a Page."""

    def __init__(self, page: Page, url: str, screenshot_dir: Path = Path("logs")) -> None:
        self._page = page
        self._url = url
        self._screenshot_dir = screenshot_dir
        self._turnstile_clicks = 0
        self._turnstile_reset = False
        self._turnstile_seen = False
        self._play_gate_clicked_at: float | None = None
        # Payload objects already reported as unreadable, so a permanent one
        # warns once instead of every poll.
        self._unreadable_ids: set[str] = set()
        # How many markets the last read found open. place_bet only falls back
        # to an unscoped card match when this is 1.
        self._open_market_count = 0

    async def _pass_play_gate(self, after_reload: bool = False) -> bool:
        """Click forg1's "Jugar" screen if it is showing. True if clicked.

        Added 2026-10-31, when forg1 put that screen in front of the markets.
        Reads never needed it (they are HTTP), but the visible tab does: the
        balance header and the outcome cards that place_bet clicks live behind
        it. Nothing here touches a bet: it refuses while any dialog is open
        and while outcome cards are already on screen, and it clicks at most
        once a minute so an element that does not go away is not hammered;
        `after_reload` lifts that limit, because a reload legitimately brings
        the screen back and a bet must not wait out the minute. Best-effort: any Playwright error is logged and swallowed.
        """
        try:
            gate = self._page.locator(_PLAY_GATE_SELECTOR).filter(has_text=_PLAY_GATE_TEXT)
            if await gate.count() == 0:
                return False
            if await self._page.locator(_DIALOG_OVERLAY_SELECTOR).count() > 0:
                return False
            if await self._page.locator(_OUTCOME_SELECTOR).count() > 0:
                return False
            if not await gate.first.is_visible():
                return False
            now = _play_gate_clock()
            if (
                not after_reload
                and self._play_gate_clicked_at is not None
                and now - self._play_gate_clicked_at < _PLAY_GATE_COOLDOWN_SECONDS
            ):
                return False
            self._play_gate_clicked_at = now
            await gate.first.click(timeout=5000)
            logger.info("Clicked forg1's 'Jugar' screen to reach the predictions.")
            return True
        except PlaywrightError as exc:
            logger.warning("Could not get past the 'Jugar' screen: {}", short_error(exc))
            return False

    async def _wait_past_play_gate(self, target: Locator, timeout_ms: float) -> bool:
        """Wait for `target` to show, clicking "Jugar" first if it is in the
        way. Call right after a reload. If the wait fails, look for the screen
        once more (it may have rendered late) and wait again only if it was
        there, so a genuinely missing card costs no extra time."""
        await self._pass_play_gate(after_reload=True)
        try:
            await target.wait_for(state="visible", timeout=timeout_ms)
            return True
        except PlaywrightError:
            pass
        if not await self._pass_play_gate(after_reload=True):
            return False
        try:
            await target.wait_for(state="visible", timeout=timeout_ms)
            return True
        except PlaywrightError:
            return False

    async def _ensure_live_page(self) -> None:
        """Recover from a closed TAB: reopen it in the SAME context, where the
        cookies live, so the new page is still logged in. A dead browser/context
        makes new_page() raise instead — handled a level up, where main watches
        the context's close event and relaunches the whole session.

        Also recover from a DRIFTED tab. On 2026-08-16 the visible tab sat idle
        overnight, came back parked off /predicciones, and stayed there for 2h20m
        until the process died: every recovery path in this class calls
        `page.reload()`, which faithfully reloads whatever wrong URL the tab
        drifted to. The failure is silent because reads never touch the tab —
        `_fetch_html` requests `self._url` over HTTP — so predictions kept
        arriving while `refresh_balance` logged "Balance button missing" 61 times
        and `_select_outcome` would have burned all three of its reload attempts
        on a page with no outcome cards. Navigating back belongs here, not in
        each caller, because reload() is wrong for all of them.

        Every public method starts here; the reads are cheap HTTP calls that
        would otherwise fail silently for the rest of the process's life.
        """
        if self._page.is_closed():
            logger.warning("Prediction page was closed; reopening it in the same context.")
            self._page = await self._page.context.new_page()
            await self._page.goto(self._url, wait_until="domcontentloaded")
            logger.info("Reopened prediction page at {}.", self._url)
            await self._pass_play_gate(after_reload=True)
            return
        # ponytail: path only. The site appends query strings of its own, and an
        # exact-URL match would renavigate the tab on every single poll.
        if _path_of(self._page.url) != _path_of(self._url):
            logger.warning(
                "Prediction tab drifted to {}; navigating back to {}.", self._page.url, self._url
            )
            await self._page.goto(self._url, wait_until="domcontentloaded")
            await self._pass_play_gate(after_reload=True)
            return
        await self._pass_play_gate()

    async def _fetch_html(self) -> str:
        """Server-rendered HTML for the predictions page.

        Requested by configured URL, not page.url: a tab parked on a Cloudflare
        interstitial or a redirect would otherwise make every read return "no
        predictions" forever, with nothing in the log to say why.

        Raises CloudflareBlocked rather than returning an interstitial, because
        a challenge page parses to zero predictions and is otherwise
        indistinguishable from a quiet stream — the bot would look healthy while
        locked out. Not a PlaywrightError on purpose: `flaky_page_action` must
        not hammer a block with five retries and make it worse.
        """
        await self._ensure_live_page()
        response = await self._page.request.get(self._url)
        html = await response.text()
        reason = cloudflare_reason(response.status, dict(response.headers), html)
        if reason:
            raise CloudflareBlocked(reason)
        return html

    async def read_active_prediction(self, prediction_id: str | None = None) -> Prediction | None:
        """The first open market, or the open market with `prediction_id`."""
        predictions = await self.read_open_predictions()
        if prediction_id is None:
            return predictions[0] if predictions else None
        return next((p for p in predictions if p.id == prediction_id), None)

    @flaky_page_action
    async def read_open_predictions(self) -> list[Prediction]:
        """Every market currently open for bets, in payload order.

        forg1 can run several markets at the same time since 2026-10-31. The
        monitor used to see only the first one, so a second market open next
        to it was never bet.
        """
        html = await self._fetch_html()
        predictions = [p for p in _extract_prediction_objects(html) if _is_valid_prediction(p)]
        now = datetime.now(timezone.utc)
        open_predictions = [p for p in predictions if _is_bettable(p, now)]
        # On 2026-10-31 (the day forg1 added the "Jugar" screen) every read
        # died on "1 validation error for Prediction", stream on or off: one
        # open object in the payload no longer fits the model, and since it
        # came first it took the whole read down. Skip what does not fit and
        # say which field, once.
        readable = [r for r in open_predictions if self._readable(r, now)]
        self._open_market_count = len(readable)
        if not readable:
            return []

        balance = await self.read_balance()
        markets = [_build_prediction(raw, balance, now) for raw in readable]
        for prediction in markets:
            logger.info(
                "Prediction read: {} | leading={} ({:.1f}% of {} pool) | balance={}",
                prediction.title,
                prediction.leading_outcome.label,
                prediction.leading_share,
                prediction.total_pool,
                balance,
            )
        return markets

    def _readable(self, raw: dict[str, Any], now: datetime) -> bool:
        """True if `raw` builds a Prediction. Otherwise logs why, once per id."""
        try:
            _build_prediction(raw, 0, now)
            return True
        except ValidationError as exc:
            key = str(raw.get("id"))
            if key not in self._unreadable_ids:
                self._unreadable_ids.add(key)
                logger.warning(
                    "Skipping a payload object that is not a readable market: id={} "
                    "title={!r} status={!r} problems: {}",
                    key,
                    raw.get("title"),
                    raw.get("status"),
                    _describe_validation(exc),
                )
            return False

    @flaky_page_action
    async def read_resolution(self, prediction_id: str) -> str | None:
        """Winning outcome label for a now-resolved prediction, or None if it
        isn't resolved yet (or dropped off the recent list). Lets the monitor
        report the final result after a prediction closes."""
        html = await self._fetch_html()
        for raw in _extract_prediction_objects(html):
            if raw.get("id") != prediction_id or raw.get("status") != "resolved":
                continue
            winner_id = raw.get("winning_outcome_id")
            for outcome in raw.get("outcomes", []):
                if outcome.get("id") == winner_id:
                    return str(outcome["label"])
            return None
        return None

    async def read_final_pool(self, prediction_id: str) -> dict[str, int] | None:
        """Points per outcome label as the payload holds them now, or None if
        the prediction is not in it. Once a market has locked its pool is
        frozen there, so after resolution this is the pool forg1 paid out on.
        Observation only (results_skipped.jsonl). Deliberately not
        @flaky_page_action: retries would stall the monitoring loop for a log."""
        html = await self._fetch_html()
        for raw in _extract_prediction_objects(html):
            if raw.get("id") == prediction_id and _is_valid_prediction(raw):
                return {str(o["label"]): int(o["total_points"]) for o in raw["outcomes"]}
        return None

    async def is_logged_in(self) -> bool:
        """Server-authoritative login check (the same one startup uses). A
        logged-out session has no header balance/refresh button, so callers use
        this to tell 'you got logged out' apart from a flaky locator.

        Goes through _fetch_html so a Cloudflare block raises instead of being
        read as a healthy session: a challenge page carries neither the
        "isLoggedIn" flag nor the logged-out marker, which used to fall through
        to "logged in" and hide the block completely.
        """
        return not is_logged_out_html(await self._fetch_html())

    async def read_balance(self) -> int:
        """Reads "Tu balance" from the header — the account's own points, not
        the prediction pool total."""
        await self._ensure_live_page()
        span = self._page.locator(_BALANCE_VALUE_SELECTOR).filter(has_text=_BALANCE_PATTERN).first
        # Short timeout: the span is missing entirely when the session is logged
        # out, and the default 30s (x5 flaky retries) wedged the poll loop for
        # minutes per cycle.
        text = await span.inner_text(timeout=5000)
        return int(re.sub(r"[^\d]", "", text))

    async def refresh_balance(self) -> None:
        """Clicks the header's balance refresh button.

        The button only exists on the logged-in predicciones view. Over a long
        VPS run the single visible tab can drift off it (stale render, Cloudflare
        re-check, session degrade) while HTTP reads keep working — so reload once
        to re-render the header before giving up. A failure that survives the
        reload means the visible page is genuinely logged out or challenged,
        which betting needs to work too.
        """
        await self._ensure_live_page()
        button = self._page.locator(_BALANCE_REFRESH_SELECTOR).first
        try:
            await button.wait_for(state="visible", timeout=5000)
        except PlaywrightError:
            logger.warning("Balance button missing; reloading page to recover the header.")
            await self._page.reload(wait_until="domcontentloaded")
            await self._pass_play_gate(after_reload=True)
            await button.wait_for(state="visible", timeout=15_000)
        try:
            await button.click(timeout=5000)
        except PlaywrightError:
            # The button is there and enabled but something covers it — almost
            # always a modal overlay ("dialog-overlay intercepts pointer
            # events"). A reload is the one thing that clears any leftover
            # dialog regardless of which one it is.
            logger.warning("Balance button was covered; reloading to clear the overlay.")
            await self._page.reload(wait_until="domcontentloaded")
            await self._pass_play_gate(after_reload=True)
            await button.wait_for(state="visible", timeout=15_000)
            await button.click(timeout=5000)
        logger.info("Clicked balance refresh button.")

    async def refresh(self) -> None:
        logger.debug("Refreshing predictions page.")
        await self._ensure_live_page()
        await self._page.reload(wait_until="domcontentloaded")
        await self._pass_play_gate(after_reload=True)

    async def place_bet(
        self,
        side: str,
        amount: int,
        market_title: str | None = None,
        outcome_count: int | None = None,
    ) -> bool:
        """Executes the bet on the live UI: pick the outcome, fill the amount,
        confirm, then clear the Cloudflare-gated final "Confirmar".

        ONE attempt only once the outcome has been clicked. A retry loop lived
        here briefly on the theory that an unclickable Confirmar meant nothing
        had been submitted. The site's own bet history disproved it: bets this
        code logged as "Final confirmation did not appear" (3,675 and 4,275) are
        both on forg1 as real, settled bets. The button reads `disabled` BECAUSE
        the bet already went through, so retrying would stake the amount twice.

        Returns True only once the post-bet summary renders; False means "we do
        not know whether it landed" and a human should check the site.

        `market_title` and `outcome_count` pick the card inside the right
        market when several are open at once (see _card_in_market).
        """
        logger.info("place_bet start: side={} amount={}", side, amount)
        await self._ensure_live_page()
        self._turnstile_clicks = 0
        self._turnstile_reset = False
        self._turnstile_seen = False

        await self._select_outcome(side, market_title, outcome_count)
        try:
            await self._fill_amount(amount)
        except _AmountNotAccepted as exc:
            # Nothing has been confirmed yet, so refusing here costs only the
            # bet. Confirming would stake whatever the field actually holds.
            logger.error("{}", exc)
            await self._dismiss_dialog()
            return False

        if await self._confirm(side, amount) is _ConfirmResult.PLACED:
            logger.info("Bet placed: {} points on {}", amount, side)
            return True
        return False

    async def _select_outcome(
        self, side: str, market_title: str | None = None, outcome_count: int | None = None
    ) -> None:
        """Reloads until the outcome card renders, then clicks it.

        The monitor reads predictions over HTTP (page.request.get), which renders
        nothing — so the visible DOM still shows whatever was last loaded, usually
        a stale state without this prediction's cards. That is why the outcome
        locator used to time out. Retrying the reload is safe: nothing has been
        submitted yet, and a reload occasionally draws a Cloudflare re-check or
        slow hydration that leaves the cards unrendered (one bet was lost to it).
        """
        outcome = self._page.locator(_OUTCOME_SELECTOR).filter(has_text=side).first
        for attempt in range(1, 4):
            await self._page.reload(wait_until="domcontentloaded")
            # Since 2026-10-31 the cards sit behind a "Jugar" screen. Clicking
            # it is safe here: nothing has been selected or submitted yet.
            if await self._wait_past_play_gate(outcome, 15_000):
                break
            logger.warning(
                "Outcome card for {} not visible (attempt {}/3) — reloading again.",
                side,
                attempt,
            )
        else:
            raise PlaywrightError(
                f"Outcome card for {side!r} never became visible after 3 reloads."
            )

        cards = self._page.locator(_OUTCOME_SELECTOR).filter(has_text=side)
        target = await self._card_in_market(cards, market_title, outcome_count)
        if target is None:
            # has_text is a case-insensitive SUBSTRING match, so "OT" also
            # matches the "OT 2 o +" card. Nothing is submitted yet, so refusing
            # here is free; clicking .first could stake on the wrong outcome.
            matches = await cards.count()
            if matches > 1:
                raise _AmbiguousOutcome(
                    f"{matches} outcome cards contain {side!r} — refusing to guess which one"
                )
            # With several markets open, a lone match may belong to ANOTHER
            # market (this one's cards not rendered yet). Only trust it when
            # this is the only market open.
            if market_title is not None and self._open_market_count > 1:
                raise _AmbiguousOutcome(
                    f"{self._open_market_count} markets are open and the {side!r} card "
                    f"could not be tied to {market_title!r} — refusing to guess"
                )
            target = outcome

        logger.info("Selecting outcome card for {}", side)
        await target.click(timeout=5000)

    async def _card_in_market(
        self, cards: Locator, market_title: str | None, outcome_count: int | None
    ) -> Locator | None:
        """The one card in `cards` that belongs to the market titled
        `market_title`, or None if that cannot be decided.

        forg1 can show several markets at once since 2026-10-31, and two of them
        can share labels (the same two teams on "1er MAPA" and "la serie").
        Without the market's container markup, this works from structure: from
        each card, climb to the smallest ancestor whose text contains the
        title. For this market's card that is the market's own box, holding
        exactly `outcome_count` cards; for another market's card it is a shared
        ancestor holding both markets' cards, so the count gives it away.
        Text is compared case-insensitively on textContent (forg1 uppercases
        with CSS). Anything other than exactly one hit returns None, and the
        caller then refuses rather than guess.
        """
        if not market_title or not outcome_count:
            return None
        try:
            hits: list[int] = await cards.evaluate_all(
                _CARD_IN_MARKET_JS,
                {"title": market_title, "count": outcome_count, "card": _OUTCOME_SELECTOR},
            )
        except PlaywrightError as exc:
            logger.warning("Could not tie the card to its market: {}", short_error(exc))
            return None
        if len(hits) != 1:
            logger.info(
                "{} card(s) tie to market {!r}; falling back to the page-wide check.",
                len(hits),
                market_title,
            )
            return None
        return cards.nth(hits[0])

    async def _fill_amount(self, amount: int) -> None:
        """Sets the stake and VERIFIES the field actually holds it.

        `fill()` sets the value and fires one `input` event. React's controlled
        number inputs do not always pick that up, and when they miss it the
        component keeps its own state — the site's default, which is `min_bet`.
        The bot would then confirm a 500-point bet while the log said 6,331:
        the wrong amount, silently, on a money path.

        So read it back. If it disagrees, retype key-by-key (which fires the
        per-keystroke events React cannot miss) and read again. If it still
        disagrees, refuse — placing nothing beats placing the wrong stake.
        """
        amount_input = self._page.locator(_AMOUNT_INPUT_SELECTOR).first
        await amount_input.wait_for(state="visible", timeout=5000)
        logger.info("Filling bet amount {}", amount)
        await amount_input.fill(str(amount), timeout=5000)
        await amount_input.press("Tab")  # blur so the form validates the value

        if await self._amount_field_holds(amount_input, amount):
            return

        logger.warning("Amount field did not take {} from fill(); retyping it.", amount)
        await amount_input.click(timeout=5000)
        await amount_input.press("ControlOrMeta+a")
        await amount_input.press_sequentially(str(amount), delay=25)
        await amount_input.press("Tab")

        if not await self._amount_field_holds(amount_input, amount):
            actual = await amount_input.input_value(timeout=5000)
            raise _AmountNotAccepted(
                f"amount field holds {actual!r}, expected {amount} — refusing to confirm"
            )

    async def _amount_field_holds(self, amount_input: Locator, amount: int) -> bool:
        actual = (await amount_input.input_value(timeout=5000)).strip()
        # The field may format the number (thousands separators); compare digits.
        matches = re.sub(r"\D", "", actual) == str(amount)
        if matches:
            logger.info("Amount field verified at {}.", actual)
        return matches

    async def _confirm(self, side: str, amount: int) -> "_ConfirmResult":
        """The two-step confirmation.

        The return value distinguishes the two failures, because only one of
        them is safe to retry: if the final button never became clickable the
        site received nothing, whereas once that click lands the bet may exist
        even if the summary never renders.

        Both clicks rely on Playwright's own actionability wait rather than a
        hand-rolled is_enabled() poll. click() waits for visible + stable +
        enabled + hit-testable and reports precisely which check failed.
        """
        primary = self._page.get_by_role("button", name=_PRIMARY_CONFIRM_TEXT).first
        await primary.wait_for(state="visible", timeout=5000)
        logger.info("Clicking primary confirm button")
        await primary.click(timeout=_CONFIRM_CLICK_TIMEOUT_MS)

        final = self._page.get_by_role("button", name=_FINAL_CONFIRM_TEXT).first
        try:
            logger.info("Waiting for and clicking the final Confirmar button")
            await self._clear_turnstile_then_click(final)
        except PlaywrightError as exc:
            # Do NOT assume Cloudflare here. This button is Turnstile-gated, but
            # a slow render, a covering overlay and a stale locator all land in
            # this same branch, and claiming a block that did not happen is its
            # own bug. Dump what the DOM actually says so the next occurrence is
            # a diagnosis instead of a guess.
            await self._log_confirm_failure(final)
            logger.error("Final confirmation was not clickable: {}", short_error(exc))
            return _ConfirmResult.UNKNOWN

        try:
            logger.info("Waiting for post-bet confirmation text")
            await self._page.wait_for_function(_POST_BET_SUMMARY_JS, timeout=15_000)
        except PlaywrightError:
            logger.error(
                "Bet confirmation click completed for {} pts on {}, but the post-bet "
                "summary never appeared.",
                amount,
                side,
            )
            await self._dismiss_dialog()
            return _ConfirmResult.UNKNOWN
        return _ConfirmResult.PLACED

    async def _dismiss_dialog(self) -> None:
        """Close the bet modal after a failed confirmation.

        The flow opens a Radix dialog whose overlay covers the entire page. A
        failure used to leave it open, and every later interaction died on
        "<div data-slot=dialog-overlay> intercepts pointer events" — the header
        balance refresh first, then the next bet's outcome card. Escape is how
        Radix closes; a reload is the fallback because a page with a stuck modal
        is useless to us either way.
        """
        try:
            await self._page.keyboard.press("Escape")
            overlay = self._page.locator(_DIALOG_OVERLAY_SELECTOR)
            await overlay.first.wait_for(state="detached", timeout=3000)
            logger.info("Closed the leftover bet dialog.")
            return
        except PlaywrightError as exc:
            logger.warning("Escape did not close the bet dialog ({}); reloading.", short_error(exc))
        try:
            await self._page.reload(wait_until="domcontentloaded")
        except PlaywrightError as exc:
            logger.error("Could not clear the bet dialog: {}", short_error(exc))

    async def _clear_turnstile_then_click(self, final: Locator) -> None:
        """Get past the Turnstile widget, then click Confirmar.

        Cloudflare serves Turnstile in two modes. The non-interactive one solves
        itself in a second or two and the button enables — that is every bet
        that has ever worked. The MANAGED one renders a checkbox inside a
        cross-origin iframe and waits for a real click; nothing was clicking it,
        so `disabled` stayed true until the 25s timeout and the bet was lost.
        Both modes were happening at random, which is exactly the intermittent
        ~25% failure rate in the logs.

        So: poll. Each round ticks the checkbox if one is showing and then tries
        the button. Clicking an already-solved widget is harmless, and the
        button click is what ends the loop.
        """
        deadline = time.monotonic() + _FINAL_CONFIRM_CLICK_TIMEOUT_MS / 1000
        last: PlaywrightError | None = None
        while True:
            await self._tick_turnstile_checkbox()
            try:
                # Short per-attempt timeout: this is the poll interval, not the
                # budget. The deadline above is the budget.
                await final.click(timeout=2000)
                return
            except PlaywrightError as exc:
                last = exc
            if time.monotonic() >= deadline:
                raise last
            await asyncio.sleep(_CONFIRM_POLL_SECONDS)

    async def _tick_turnstile_checkbox(self) -> None:
        """Click the Turnstile widget if it is waiting on a human.

        Two routes, because the first one is not reliable. Turnstile puts its
        checkbox behind a shadow root inside a cross-origin iframe, so
        `frame_locator(...).locator('input[type=checkbox]')` often matches
        nothing at all — which is exactly what happened live: the widget was
        present, the token never arrived, and the original version of this
        method logged absolutely nothing because it swallowed every error.

        So: try the real checkbox, and if it cannot be reached, click the
        iframe element itself where the checkbox sits. That is what a person
        does, and it does not require piercing the shadow DOM.

        Clicks are capped. Hammering a challenge widget every 500ms for 25s is
        both useless and the kind of behaviour that gets a client flagged.
        """
        if self._turnstile_clicks >= _MAX_TURNSTILE_CLICKS:
            return

        frame = self._turnstile_frame()
        if frame is None:
            return  # non-interactive mode: nothing to click, it self-solves

        # The state BEFORE anything touches it. The 2026-08-12 failure only
        # showed "failure_retry" in the dump AFTER two clicks had landed, so it
        # could not say whether the widget arrived broken or our own click broke
        # it — and those two have opposite fixes.
        if not self._turnstile_seen:
            self._turnstile_seen = True
            logger.info("Turnstile widget appeared, untouched state: {}", frame.url)

        # A widget in "failure_retry" ERRORED — it is not waiting on a human, so
        # clicking does nothing (proven live: three clicks landed, token stayed
        # EMPTY). turnstile.reset() is the documented remedy: it tears the failed
        # challenge down and runs a fresh one. Once per bet — the site's own
        # script owns the widget, and resetting a healthy one would throw away a
        # good token.
        if _TURNSTILE_FAILED_MARK in frame.url and not self._turnstile_reset:
            self._turnstile_reset = True
            try:
                await self._page.evaluate("() => window.turnstile && window.turnstile.reset()")
                logger.warning("Turnstile reported a failure; asked the widget to reset.")
            except PlaywrightError as exc:
                logger.warning("Could not reset the Turnstile widget: {}", short_error(exc))
            return

        try:
            box = frame.locator(_TURNSTILE_CHECKBOX_SELECTOR).first
            if await box.count() > 0:
                await box.click(timeout=1500)
                self._turnstile_clicks += 1
                logger.info("Ticked the Turnstile checkbox inside the iframe.")
                return
            logger.warning("Turnstile iframe present but no reachable checkbox (shadow DOM).")
        except PlaywrightError as exc:
            logger.warning("Could not reach the Turnstile checkbox: {}", short_error(exc))

        try:
            # The checkbox sits at the left of the widget, vertically centred.
            widget = await frame.frame_element()
            await widget.click(position={"x": 30, "y": 32}, timeout=1500)
            self._turnstile_clicks += 1
            logger.info(
                "Clicked the Turnstile widget directly (attempt {}).", self._turnstile_clicks
            )
        except PlaywrightError as exc:
            logger.warning("Could not click the Turnstile widget: {}", short_error(exc))

    def _turnstile_frame(self) -> Frame | None:
        """The Turnstile frame, found by URL across the whole frame tree.

        NOT `page.locator('iframe[src*=...]')`, which is what this used to do:
        that only queries the MAIN frame's document, and the live failure of
        2026-08-12 proved the widget is not reachable there. The whole poll ran
        27s without a single log line — meaning count() was 0 every round and
        the checkbox was never clicked — while `page.frames` (used by
        _log_confirm_failure two lines later) listed the Turnstile URL and the
        token stayed EMPTY. The mitigation for the managed challenge was dead
        code in production. Frames are also how `_log_confirm_failure` sees it,
        so both paths now agree on what "the widget is present" means.
        """
        return next((f for f in self._page.frames if _TURNSTILE_URL_MARK in f.url), None)

    async def _log_confirm_failure(self, locator: Locator) -> None:
        """Records why the confirm button was refused: how many nodes matched,
        and what the first one actually looks like. Everything here is
        best-effort — diagnostics must never mask the original failure."""
        try:
            count = await locator.count()
            details = await locator.evaluate("""el => ({
                    tag: el.tagName,
                    disabled: el.disabled ?? null,
                    ariaDisabled: el.getAttribute('aria-disabled'),
                    cls: el.className,
                    text: (el.innerText || '').trim().slice(0, 60),
                    rect: el.getBoundingClientRect().width + 'x'
                          + el.getBoundingClientRect().height,
                })""")
            logger.error("Confirm button state: matches={} {}", count, details)
        except Exception as exc:
            logger.warning("Could not inspect the confirm button: {}", short_error(exc))
        try:
            # What is actually inside the modal. A Turnstile iframe here means
            # the managed widget was showing and the checkbox is the blocker;
            # no iframe means the gate is something else entirely and this
            # diagnosis is wrong.
            frames = [f.url for f in self._page.frames if f != self._page.main_frame]
            logger.error("Dialog iframes: {}", frames or "none")
            dialog = await self._page.locator(_DIALOG_CONTENT_SELECTOR).first.evaluate(
                """el => Array.from(el.querySelectorAll('button,input,iframe')).map(n => ({
                    tag: n.tagName,
                    type: n.getAttribute('type'),
                    disabled: n.disabled ?? null,
                    src: (n.getAttribute('src') || '').slice(0, 80),
                    text: (n.innerText || '').trim().slice(0, 30),
                }))"""
            )
            logger.error("Dialog controls: {}", dialog)
            token = await self._page.locator(_TURNSTILE_TOKEN_SELECTOR).first.input_value(
                timeout=2000
            )
            logger.error(
                "Turnstile token: {} (empty means the widget never solved, which is "
                "why CONFIRMAR stayed disabled)",
                f"{len(token)} chars" if token else "EMPTY",
            )
        except Exception as exc:
            logger.warning("Could not inspect the dialog contents: {}", short_error(exc))
        try:
            shot = self._screenshot_dir / f"confirm-failed-{int(time.time())}.png"
            shot.parent.mkdir(parents=True, exist_ok=True)
            # Short timeout: a page with an open modal can hang screenshotting,
            # and the last failure spent 30s here before giving up.
            await self._page.screenshot(path=str(shot), timeout=5000)
            logger.error("Saved a screenshot of the failed confirmation to {}", shot)
        except Exception as exc:
            logger.warning("Could not screenshot the failed confirmation: {}", short_error(exc))
