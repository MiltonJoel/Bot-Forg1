import re

from loguru import logger
from playwright.async_api import Error as PlaywrightError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential


def short_error(exc: BaseException | None) -> str:
    """First line of an exception, safe to write to a log file.

    NEVER log a Playwright exception directly. Its "Call log" embeds the full
    request headers, and those headers carry `forg1_session` — the actual login
    cookie. `logger.error("... ({})", exc)` in the monitor's read handler wrote
    a live session cookie into logs/forg1_monitor.log in plaintext, where it sat
    until someone pasted the log somewhere. The first line carries the whole
    diagnosis ("getaddrinfo ENOTFOUND www.forg1.com") and none of the secret.
    """
    if exc is None:
        return repr(exc)
    first = str(exc).splitlines()[0] if str(exc) else repr(exc)
    # Belt and braces: if a future Playwright version ever puts headers on line
    # one, redact rather than leak.
    return _COOKIE_RE.sub("<redacted>", first)


_COOKIE_RE = re.compile(r"(forg1_session|cookie)\s*[=:]\s*\S+", re.IGNORECASE)


def _log_retry(retry_state: object) -> None:
    """Log a one-line retry reason (see short_error for why it must be short)."""
    outcome = getattr(retry_state, "outcome", None)
    exc = outcome.exception() if outcome else None
    reason = short_error(exc)
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
