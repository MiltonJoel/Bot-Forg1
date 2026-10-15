import asyncio
import json
from pathlib import Path

from loguru import logger
from playwright.async_api import BrowserContext, Page

from app.services.notifier import TelegramNotifier

# forg1's logged-OUT page renders the Kick connect CTA ("Conectar con Kick");
# the logged-IN page shows the user's balance/profile instead. Verified against
# the live site. We also trust the server-embedded "isLoggedIn" flag when present
# (authoritative), falling back to the text marker.
_LOGGED_OUT_MARKER = "conectar con kick"


def is_logged_out_html(body: str) -> bool:
    """Login state from forg1's own SSR response body.

    Caller beware: a Cloudflare challenge page carries neither the flag nor the
    marker and therefore reads as LOGGED IN here. Callers that can be blocked
    must check for that first (PredictionPageReader does, via _fetch_html).
    """
    flat = body.replace("\\", "")  # the flag is embedded inside an escaped RSC string
    if '"isLoggedIn":true' in flat:
        return False
    if '"isLoggedIn":false' in flat:
        return True
    return _LOGGED_OUT_MARKER in body.lower()


async def looks_logged_out(page: Page, url: str) -> bool:
    """Server-authoritative login check: reads forg1's own SSR response rather
    than the post-hydration DOM (which can momentarily show either state)."""
    response = await page.request.get(url)
    return is_logged_out_html(await response.text())


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


async def ensure_logged_in(
    context: BrowserContext,
    url: str,
    storage_state_path: Path,
    notifier: TelegramNotifier | None = None,
) -> Page:
    """Restores the saved session; if forg1 still shows logged out, alerts on
    Telegram and polls until a manual Kick login happens in the open browser
    window, then snapshots the session so future runs restore it.

    Polling (instead of waiting for Enter on stdin) is what makes this work
    under systemd on the VPS: there is no stdin there, and input() crash-looped
    the whole service (browser opened, died at the prompt, systemd restarted it).
    """
    await _restore_session(context, storage_state_path)
    # Open forg1 in a fresh page. (Reusing context.pages[0] looked tidier but that
    # index isn't reliably the visible tab with a real profile — it left the
    # visible window on about:blank while forg1 loaded into an off-screen page.)
    page = await context.new_page()
    await page.goto(url, wait_until="domcontentloaded")

    if await looks_logged_out(page, url):
        logger.warning(
            "forg1 shows logged out. Complete the Kick login in the browser window; "
            "the bot resumes on its own (checks every 30s)."
        )
        if notifier is not None:
            await notifier.send(
                "⚠️ Sesión de forg1 expirada — entrá a la ventana de Chrome del bot "
                "(VNC en el VPS) y logueate con Kick. El bot sigue solo al detectarlo."
            )
        while await looks_logged_out(page, url):
            await asyncio.sleep(30)
        await page.goto(url, wait_until="domcontentloaded")  # show the logged-in page
        logger.info("Login confirmed.")
        if notifier is not None:
            await notifier.send("✅ Sesión de forg1 recuperada — el bot sigue normal.")
    else:
        logger.info("Already logged into forg1.")

    storage_state_path.parent.mkdir(parents=True, exist_ok=True)
    await context.storage_state(path=str(storage_state_path))
    logger.info(
        "Session snapshot saved to {} — future runs restore it automatically.", storage_state_path
    )
    return page
