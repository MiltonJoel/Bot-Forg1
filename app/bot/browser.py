import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from loguru import logger
from playwright.async_api import BrowserContext, async_playwright

# Playwright's default Chromium flags put Chrome into a "testing" mode that is
# hostile to a REAL, personal profile — the direct cause of losing extensions
# and getting signed out of Google/Gmail on every run. We strip the destructive
# ones (exact-string match, per Playwright's ignoreDefaultArgs filter) so the
# automated launch leaves the profile as intact as a normal `google-chrome`
# launch would:
#   --disable-extensions / --disable-component-extensions-with-background-pages
#       -> disables (and lets Chrome prune) your installed extensions
#   --disable-sync
#       -> signs the profile out of Google sync, dropping the Gmail/Google session
#   --disable-default-apps / --disable-component-update
#       -> minor, but keep the profile behaving like a normal one
# NOTE 1: the --disable-features=<bundle> default is deliberately NOT stripped —
# it can't be (ignoreDefaultArgs is exact-match and the token is version-
# specific), and testing showed the forg1 session survives it fine.
# NOTE 2: we intentionally do NOT strip --enable-automation. It shows the
# "controlled by automated software" banner, but it also SUPPRESSES Chrome's
# security warning infobars (e.g. the "--no-sandbox" one). Stripping it made
# those warnings appear. Harmless banner < broken-looking warnings.
_STRIP_DEFAULT_ARGS = [
    "--disable-extensions",
    "--disable-component-extensions-with-background-pages",
    "--disable-sync",
    "--disable-default-apps",
    "--disable-component-update",
]


def _assert_profile_not_in_use(user_data_dir: Path) -> None:
    """Refuse to launch if another Chrome already holds this profile.

    Two Chrome processes on one user-data-dir is the classic way a profile gets
    corrupted/reset — lost cookies, wiped extensions, signed-out sessions. On
    Linux, a running Chrome leaves a `SingletonLock` symlink -> "<host>-<pid>".
    If that pid is alive, bail with a clear message instead of clobbering the
    profile. A stale lock (dead pid) is left for Chrome/Playwright to clean up.
    """
    lock = user_data_dir / "SingletonLock"
    if not lock.is_symlink():
        return
    try:
        pid = int(os.readlink(lock).rsplit("-", 1)[-1])
    except (OSError, ValueError):
        return  # unreadable/odd format — let Playwright deal with it
    try:
        os.kill(pid, 0)  # signal 0 = liveness probe, doesn't actually signal
    except OSError:
        return  # process is gone; lock is stale and harmless
    raise RuntimeError(
        f"Chrome is already running on this profile ({user_data_dir}, pid {pid}). "
        "Close that Chrome window completely before starting the app — running "
        "both at once corrupts the profile (lost logins, wiped extensions)."
    )


@asynccontextmanager
async def persistent_context(user_data_dir: Path, headless: bool) -> AsyncIterator[BrowserContext]:
    """One dedicated, on-disk Chrome profile driven by Playwright.

    Uses real Chrome (channel="chrome") with a persistent user_data_dir, so
    cookies, the logged-in session AND Cloudflare's clearance cookie all
    survive across runs — you log in once and it's remembered. That persisted
    real-browser session is what keeps Cloudflare from flagging it as a bot,
    unlike a fresh throwaway Chromium context every run.
    """
    user_data_dir.mkdir(parents=True, exist_ok=True)
    _assert_profile_not_in_use(user_data_dir)
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            str(user_data_dir),
            channel="chrome",
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=_STRIP_DEFAULT_ARGS,
        )
        logger.info("Chrome profile ready at {} (headless={}).", user_data_dir, headless)
        try:
            yield context
        finally:
            # On Ctrl+C, SIGINT also reaches the Playwright driver subprocess and
            # tears it down before we get here, so close() hits an already-dead
            # connection. The browser is gone either way — don't crash the exit.
            try:
                await context.close()
            except Exception as exc:
                logger.debug("Ignoring browser close error during shutdown: {}", exc)
