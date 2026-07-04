from loguru import logger
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from playwright.async_api import Error as PlaywrightError


def _log_retry(retry_state: object) -> None:
    """Log a one-line retry reason.

    Playwright errors embed a full "Call log" that includes the request headers —
    and those headers carry the forg1 session cookie. Dumping the whole exception
    (as tenacity's before_sleep_log does) writes that cookie to the log file in
    plaintext. Log only the first line so credentials never land on disk.
    """
    outcome = getattr(retry_state, "outcome", None)
    exc = outcome.exception() if outcome else None
    reason = str(exc).splitlines()[0] if exc else repr(exc)
    fn = getattr(retry_state, "fn", None)
    logger.warning(
        "Retrying {} (attempt {}) after: {}",
        getattr(fn, "__name__", "?"),
        getattr(retry_state, "attempt_number", "?"),
        reason,
    )


# Retries anything Playwright throws (timeouts, detached elements, navigation
# races, dropped connections, DNS blips on sleep/wake) with exponential backoff.
flaky_page_action = retry(
    retry=retry_if_exception_type(PlaywrightError),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(5),
    before_sleep=_log_retry,
    reraise=True,
)
