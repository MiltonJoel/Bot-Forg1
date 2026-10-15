import os
import signal
import socket
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from loguru import logger

from app.utils.retry import short_error
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


class ProfileInUse(RuntimeError):
    """Another live Chrome holds the profile. Relaunching can never fix this, so
    main.py exits instead of looping — the 2026-08-19 wedge was a manual run
    fighting the systemd service for 171 relaunches, one Telegram alert each."""


_SINGLETON_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket")


def _clear_singleton(user_data_dir: Path, why: str) -> None:
    """Drop the singleton files so Chrome stops treating the profile as taken."""
    for name in _SINGLETON_FILES:
        path = user_data_dir / name
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not remove {}: {}", path, short_error(exc))
    logger.info("Cleared stale Chrome profile lock in {} ({}).", user_data_dir, why)


def _cmdline(pid: int) -> str:
    """The process's argv as one string; "" if it's gone, a zombie, or no /proc."""
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").replace("\0", " ")
    except OSError:
        return ""


def _launcher_is_alive(pid: int) -> bool:
    """True while this Chrome is still attached to a live Playwright driver.

    The difference between "another bot instance owns the profile" (refuse) and
    "a session died without closing its browser" (kill it): an orphan is
    reparented to init/systemd, so its parent stops being a playwright process.
    """
    try:
        # /proc/<pid>/stat: `pid (comm) state ppid ...` — comm can contain spaces
        # and parens, so split after the LAST ')'.
        ppid = int(Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[1])
    except (OSError, IndexError, ValueError):
        return True  # can't tell -> assume it's in use and keep the refusal
    return "playwright" in _cmdline(ppid)


def _kill_orphaned_chrome(pid: int) -> None:
    """SIGTERM, then SIGKILL, a Chrome of ours that outlived its session."""
    logger.warning(
        "Chrome pid {} outlived its session and still holds the profile — killing it.", pid
    )
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except OSError:
            return  # already gone
        for _ in range(50):  # up to 5s per signal
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except OSError:
                return
    logger.error("Chrome pid {} survived SIGKILL — launching anyway.", pid)


def _assert_profile_not_in_use(user_data_dir: Path) -> None:
    """Refuse to launch only if another Chrome *on this machine* holds the profile.

    Two Chrome processes on one user-data-dir is the classic way a profile gets
    corrupted/reset — lost cookies, wiped extensions, signed-out sessions. On
    Linux a running Chrome leaves `SingletonLock` -> "<host>-<pid>".

    The host half matters as much as the pid. Copying this profile to another
    machine (deploy, rsync, restore) carries the lock along, and Chrome then
    refuses to start forever: it cannot probe a pid that belongs to a different
    computer, so it assumes the profile is in use and exits 21. Comparing only
    the pid — as this used to — misses that case entirely, and can also collide
    with an unrelated local process that happens to reuse the number.

    A live pid is not evidence either. On 2026-08-19 the bot spun 170 relaunches
    against `pid 4426`: a Chrome *we* had launched outlived its session (the
    context.close() in persistent_context is allowed to fail quietly), kept the
    lock, and the guard told an empty room to "close that Chrome window". A
    recycled pid or an unreaped zombie wedges it the same way. So the pid must
    also *be* Chrome running THIS profile, read off /proc/<pid>/cmdline — and if
    it is one of ours AND its Playwright driver is dead (i.e. orphaned by a dead
    session, never a live sibling instance), kill it instead of giving up.

    So: same host, and the pid is a live Chrome on this profile that a human
    started -> refuse. Anything else is cleared (or killed) and we carry on.
    """
    lock = user_data_dir / "SingletonLock"
    if not lock.is_symlink():
        return
    try:
        target = os.readlink(lock)
        host, _, raw_pid = target.rpartition("-")
        pid = int(raw_pid)
    except (OSError, ValueError):
        return  # unreadable/odd format — let Playwright deal with it

    if host != socket.gethostname():
        # Chrome would loop here forever; it has no way to check a remote pid.
        _clear_singleton(user_data_dir, f"written by {host!r}, we are {socket.gethostname()!r}")
        return
    try:
        os.kill(pid, 0)  # signal 0 = liveness probe, doesn't actually signal
    except OSError:
        _clear_singleton(user_data_dir, f"pid {pid} is gone")
        return

    cmdline = _cmdline(pid)
    if not any(str(d) in cmdline for d in (user_data_dir, user_data_dir.resolve())):
        # Empty (zombie / no /proc) or some unrelated process that inherited the
        # number. Either way this lock names nothing that can be holding the
        # profile. Chrome's own singleton check is the backstop if we're wrong.
        _clear_singleton(user_data_dir, f"pid {pid} is not Chrome on this profile")
        return

    ours = "--enable-automation" in cmdline or "--remote-debugging-pipe" in cmdline
    if ours and not _launcher_is_alive(pid):
        _kill_orphaned_chrome(pid)
        _clear_singleton(user_data_dir, f"killed our own orphaned Chrome (pid {pid})")
        return

    whose = (
        "Another instance of this bot already has it — stop that one first "
        "(`systemctl --user status forg1.service`)."
        if ours
        else "Close that Chrome window completely before starting the app."
    )
    raise ProfileInUse(
        f"Chrome is already running on this profile ({user_data_dir}, pid {pid}). "
        f"{whose} Running both at once corrupts the profile (lost logins, wiped extensions)."
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
            args=[
                "--disable-blink-features=AutomationControlled",
                # Chrome auto-selects the native Wayland ozone backend on this
                # (Wayland/GNOME) dev machine. Confirmed via journalctl that
                # every "Chrome closed itself" event lines up exactly with
                # gnome-shell logging "Invalid window geometry for
                # xdg_surface@41 ... this will result in client termination"
                # followed shortly after by "WL: error in client
                # communication (pid ...)" — GNOME Shell drops the Wayland
                # client after repeated invalid-geometry protocol violations
                # from the CDP-driven window, killing the whole browser with
                # no crash dump (clean disconnect, not a Chrome crash). XWayland
                # doesn't hit this code path. Harmless/no-op on the VPS, which
                # runs under xvfb-run (X11, no Wayland involved).
                "--ozone-platform=x11",
                # Kept as a separate defensive measure: shared-memory pressure
                # from a long-lived video tab is a real risk on the memory-
                # constrained free-tier VPS, independent of the Wayland issue
                # above. Spills Chrome's video/render shared-memory buffers to
                # disk instead of /dev/shm.
                "--disable-dev-shm-usage",
            ],
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
                logger.debug("Ignoring browser close error during shutdown: {}", short_error(exc))
