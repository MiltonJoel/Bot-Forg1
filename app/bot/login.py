import asyncio
import json
from pathlib import Path

from loguru import logger
from playwright.async_api import BrowserContext, Page

# forg1's logged-OUT page renders the Kick connect CTA ("Conectar con Kick");
# the logged-IN page shows the user's balance/profile instead. Verified against
# the live site. We also trust the server-embedded "isLoggedIn" flag when present
# (authoritative), falling back to the text marker.
_LOGGED_OUT_MARKER = "conectar con kick"


async def _looks_logged_out(page: Page, url: str) -> bool:
    """Server-authoritative login check: reads forg1's own SSR response rather
    than the post-hydration DOM (which can momentarily show either state)."""
    resp = await page.request.get(url)
    body = await resp.text()
    flat = body.replace("\\", "")  # the flag is embedded inside an escaped RSC string
    if '"isLoggedIn":true' in flat:
        return False
    if '"isLoggedIn":false' in flat:
        return True
    return _LOGGED_OUT_MARKER in body.lower()


async def _restore_session(context: BrowserContext, storage_state_path: Path) -> None:
    """Re-inject saved cookies before navigating.

    forg1's session cookie is session-scoped — Chrome drops it from disk when the
    browser closes, so the persistent profile alone loses the login between runs.
    We snapshot every cookie (incl. session-scoped) after a confirmed login and
    replay it here, which is what actually keeps you logged in across runs.
    """
    if not storage_state_path.exists():
        return
    cookies = json.loads(storage_state_path.read_text()).get("cookies", [])
    if cookies:
        await context.add_cookies(cookies)
        logger.info("Restored {} saved cookies from {}.", len(cookies), storage_state_path)


async def ensure_logged_in(context: BrowserContext, url: str, storage_state_path: Path) -> Page:
    """Restores the saved session; if forg1 still shows logged out, blocks for one
    manual Kick login, then snapshots the session so future runs restore it."""
    await _restore_session(context, storage_state_path)
    page = await context.new_page()
    await page.goto(url, wait_until="domcontentloaded")

    if await _looks_logged_out(page, url):
        logger.warning("forg1 shows logged out. Complete the Kick login in the browser window.")
        print(
            "\n>>> Log into forg1.com (click 'Conectar con Kick' and finish the Kick "
            "login) in the opened browser window, then press Enter here <<<\n"
        )
        await asyncio.to_thread(input)
        await page.reload(wait_until="domcontentloaded")
        if await _looks_logged_out(page, url):
            raise RuntimeError("Still logged out after confirmation — aborting.")
        logger.info("Login confirmed.")
    else:
        logger.info("Already logged into forg1.")

    storage_state_path.parent.mkdir(parents=True, exist_ok=True)
    await context.storage_state(path=str(storage_state_path))
    logger.info(
        "Session snapshot saved to {} — future runs restore it automatically.", storage_state_path
    )
    return page
