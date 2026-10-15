import asyncio
import json
import os
import random
import socket
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from loguru import logger

from app.utils.retry import short_error

from app.services.notifier import TelegramNotifier
from app.utils import http as http_ipv4
from app.utils.http import CloudflareBlocked

# Kick sits behind Cloudflare, which 403s a bare "Mozilla/5.0". A full
# desktop-browser UA passes the bot check — no headless browser needed.
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# Tolerate a blip, escalate an outage. At the production 1800s interval this is
# ~1.5h of silence; at a 30s test interval, ~2 minutes.
_FAILURES_BEFORE_ALERT = 3


class KickLiveChecker:
    """Pings Kick's public channel API on an interval.

    When the stream goes live it notifies and launches the stream viewer (a
    separate plain Chrome process, see _open_stream). When the stream ends it
    notifies and closes the viewer. Its live/offline state also gates the
    forg1 poll loop (MonitoringService._stream_offline).
    """

    def __init__(
        self,
        channel: str,
        check_interval_seconds: int,
        notifier: TelegramNotifier,
        viewer_profile: Path = Path(".auth/kick-viewer"),
        open_delay_seconds: float = 2.0,
        chrome_binary: str = "google-chrome",
        viewer_extension: Path | None = None,
    ) -> None:
        self._viewer_extension = viewer_extension
        self._api_url = f"https://kick.com/api/v2/channels/{channel}"
        self._channel_url = f"https://kick.com/{channel}"
        self._interval = check_interval_seconds
        self._notifier = notifier
        self._viewer_profile = viewer_profile
        self._chrome_binary = chrome_binary
        self._open_delay_seconds = open_delay_seconds
        self._was_live: bool | None = None
        self._viewer: subprocess.Popen[bytes] | None = None
        self._blocked_alerted = False
        self._consecutive_failures = 0

    async def _sleep_interval(self) -> None:
        # ponytail: jitter, same reasoning as the forg1 poll loop — an exact
        # interval hitting kick.com for hours is a bot-timing signal too.
        await asyncio.sleep(random.uniform(self._interval * 0.85, self._interval * 1.15))

    @property
    def is_live(self) -> bool:
        """True only when the stream is confirmed live (None = unknown yet)."""
        return self._was_live is True

    @property
    def is_offline(self) -> bool:
        """True only when a check has CONFIRMED the stream is down. Unknown (no
        successful check yet, e.g. Kick blocked since startup) is not offline,
        so a Kick outage never stops the bot from watching forg1."""
        return self._was_live is False

    async def run(self) -> None:
        logger.info("Kick live-check started (every {}s).", self._interval)
        while True:
            try:
                is_live = await asyncio.to_thread(self._check_live)
            except CloudflareBlocked as exc:
                # The last known state is kept while blocked: if it was never
                # known the monitor keeps reading forg1; if it was "offline" a
                # stream that starts during the block goes unseen until it lifts.
                await self._alert_blocked(str(exc))
                await self._sleep_interval()
                continue
            except (urllib.error.URLError, socket.gaierror, TimeoutError) as exc:
                # A blip (machine just woke, DNS not up yet) is normal and stays
                # quiet. A sustained outage is not: this ran for an hour straight
                # writing warnings nobody reads while the bot was effectively
                # dead to Kick. Escalate once it stops looking transient.
                logger.warning(
                    "Kick live-check skipped (network unavailable): {}", short_error(exc)
                )
                self._consecutive_failures += 1
                if self._consecutive_failures == _FAILURES_BEFORE_ALERT:
                    await self._notifier.send(
                        f"⚠️ El chequeo de Kick falla hace {self._consecutive_failures} "
                        f"intentos seguidos — el bot no sabe si el stream está en vivo "
                        f"(y no refresca el balance). Las apuestas siguen "
                        f"funcionando.\n\nÚltimo error: {short_error(exc)}"
                    )
                await self._sleep_interval()
                continue
            except Exception as exc:
                logger.error("Kick live-check failed: {}", short_error(exc))
                await self._sleep_interval()
                continue

            if self._blocked_alerted:
                self._blocked_alerted = False
                logger.info("Kick API reachable again.")
                await self._notifier.send("✅ Cloudflare dejó pasar al chequeo de Kick.")
            if self._consecutive_failures >= _FAILURES_BEFORE_ALERT:
                await self._notifier.send("✅ El chequeo de Kick volvió a funcionar.")
            self._consecutive_failures = 0

            if self._was_live is None:
                logger.info("Kick stream is {} at startup.", "live" if is_live else "offline")
            if is_live and not self._was_live:
                await self._notifier.send(f"🟢 El stream está en vivo: {self._channel_url}")
                logger.info("Kick stream went live.")
                await self._open_stream()
            elif self._was_live and not is_live:
                await self._notifier.send("🔴 El stream de Kick se cayó o terminó.")
                logger.warning("Kick stream went offline.")
                await self._close_stream()
            elif is_live and self._viewer is not None and self._viewer.poll() is not None:
                # Chrome died on its own (crash, OOM, closed by hand). The live
                # edge will not fire again until the stream ends, so without this
                # the watch time stops for the rest of the stream.
                logger.warning("Stream viewer exited on its own — relaunching it.")
                await self._open_stream()

            self._was_live = is_live
            await self._sleep_interval()

    async def _alert_blocked(self, reason: str) -> None:
        """Alerts once per block, not once per interval."""
        logger.error("Cloudflare is blocking the Kick channel API: {}", reason)
        if self._blocked_alerted:
            return
        self._blocked_alerted = True
        await self._notifier.send(
            "🚫 Cloudflare está bloqueando el chequeo de Kick — el bot no puede "
            "saber si el stream está en vivo (y por eso tampoco refresca el "
            f"balance). Las apuestas siguen funcionando.\n\nDetalle: {reason}"
        )

    async def _open_stream(self) -> None:
        """Launch the viewer as a PLAIN Chrome process, not a Playwright tab.

        Kick's player fetches web.kick.com/api/v1/*, and Cloudflare now 403s
        those calls when Chrome is driven over CDP — the page renders, the chat
        says "connection failed" and the <video> never gets a src, so the tab
        was open but watching nothing. Confirmed 2026-08-12: identical profile
        and flags, the same URL plays in a normally-launched Chrome and spins
        forever under Playwright. Nothing here needs automation; the viewer only
        has to exist and play, so it gets its own process and its own profile
        copy (two Chromes cannot share a user-data-dir).

        ponytail: no raid detection anymore — reading the tab's URL needed CDP,
        which is the thing being avoided. A raid now plays the other channel
        until the stream ends. Re-add via the channel API if it matters.
        """
        # A viewer left over from a previous live edge would leak (and keep
        # playing video) if the offline edge was ever missed — network blips
        # make the checker skip ticks without touching state.
        await self._close_stream()
        try:
            if self._open_delay_seconds > 0:
                await asyncio.sleep(self._open_delay_seconds)
            await self._kill_stray_viewers()
            # Keep Chrome's stdout/stderr. Sending it to DEVNULL turned every
            # launch failure into a guess — Chrome states the reason plainly
            # ("Opening in existing browser session", a missing $DISPLAY, a
            # sandbox refusal) and that line IS the diagnosis.
            log_path = Path("logs/kick-viewer.log")
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("wb") as log:
                viewer = subprocess.Popen(  # noqa: S603
                    self._viewer_command(), stdout=log, stderr=subprocess.STDOUT
                )
            # Chrome hands the URL to an instance that already owns the profile
            # and exits ~instantly. That reads as a dead viewer, and the relaunch
            # branch below then spawns one every tick forever. Catch it here.
            await asyncio.sleep(2)
            if viewer.poll() is not None:
                said = log_path.read_text("utf-8", "replace").strip().splitlines()
                logger.error(
                    "Stream viewer exited immediately (code {}). Chrome said: {}",
                    viewer.returncode,
                    " | ".join(said[-4:]) or "(nothing on stdout/stderr)",
                )
                return
            self._viewer = viewer
            logger.info("Opened stream viewer (pid {}): {}", viewer.pid, self._channel_url)
        except Exception as exc:  # a failed viewer must never kill the checker
            logger.error("Could not open stream viewer: {}", short_error(exc))
            self._viewer = None

    async def _kill_stray_viewers(self) -> None:
        """Kill any Chrome already holding the viewer profile.

        The profile is ours alone, so anything on it is a leftover — a manual
        window opened over VNC, or a viewer that outlived a bot restart. Chrome
        refuses to start a second process on a profile, so a stray one blocks
        every launch from here on.

        ponytail: pkill by command line, which is Linux-only. Fine — the bot only
        ever runs on Linux (systemd + xvfb-run).
        """
        # Match the profile DIRECTORY NAME, not the full path: strays launched by
        # an older build (or by hand) carry a relative --user-data-dir, so an
        # absolute pattern misses them and every launch stays blocked.
        pattern = f"user-data-dir=.*{self._viewer_profile.name}"
        try:
            killed = await asyncio.to_thread(subprocess.call, ["pkill", "-f", pattern])
            if killed == 0:
                logger.info("Killed a stray Chrome holding the viewer profile.")
                await asyncio.sleep(1)  # let it release the profile lock
        except Exception as exc:
            logger.warning("Could not check for stray viewers: {}", short_error(exc))

        # Then clear the profile lock. Anything using this profile was just
        # killed, so a surviving lock is stale by construction. It also arrives
        # stale: copying the profile between machines brings a SingletonLock
        # naming the OTHER host, and Chrome cannot verify a pid it does not own,
        # so it refuses the profile outright and exits with code 21 — every
        # launch, forever (seen on the VPS 2026-08-12, lock from milton-desktop).
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                (self._viewer_profile / name).unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Could not clear {}: {}", name, short_error(exc))

    def _viewer_command(self) -> list[str]:
        """Chrome, its own profile, the channel. Deliberately no automation flags.

        --no-sandbox only as root, which is how it runs under systemd on the VPS;
        Chrome refuses to start as root without it.
        """
        # ABSOLUTE, always. A relative --user-data-dir resolves against the
        # process CWD, and when that is not the directory the profile was set up
        # in, Chrome silently creates a fresh blank one instead of failing:
        # logged out of Kick, no extension, and nothing in the log to say so.
        profile = self._viewer_profile.expanduser().resolve()
        command = [
            self._chrome_binary,
            f"--user-data-dir={profile}",
            self._channel_url,
        ]
        if os.geteuid() == 0:
            command.insert(1, "--no-sandbox")
        # The viewer is a separate browser and inherits none of the bot profile's
        # extensions. --load-extension is IGNORED by current stable Chrome (tried
        # 2026-08-12, with and without the DisableLoadExtensionCommandLineSwitch
        # escape), so the extension has to be installed into the viewer profile
        # by hand once; this flag is here for builds that still honour it.
        # Deliberately NOT --disable-extensions-except: that would switch off the
        # hand-installed copy, which is the one actually doing the work.
        if self._viewer_extension is not None:
            command.insert(1, f"--load-extension={self._viewer_extension.expanduser().resolve()}")
        return command

    async def _close_stream(self) -> None:
        viewer, self._viewer = self._viewer, None
        if viewer is None:
            return
        try:
            viewer.terminate()
            await asyncio.to_thread(viewer.wait, 10)
            logger.info("Closed the stream viewer.")
        except subprocess.TimeoutExpired:
            viewer.kill()
        except Exception as exc:
            logger.error("Could not close the stream viewer: {}", short_error(exc))

    def _check_live(self) -> bool:
        request = urllib.request.Request(self._api_url, headers={"User-Agent": _BROWSER_UA})
        # IPv4-first: a dual-stack connect stalls for the full timeout on this
        # network before falling back (see app/utils/http.py).
        try:
            with http_ipv4.urlopen(request, timeout=15) as response:
                body = response.read().decode("utf-8", "replace")
                reason = http_ipv4.cloudflare_reason(response.status, dict(response.headers), body)
        except urllib.error.HTTPError as exc:
            # Cloudflare blocks arrive as 403/503, which urllib raises.
            body = exc.read().decode("utf-8", "replace")
            reason = http_ipv4.cloudflare_reason(exc.code, dict(exc.headers), body)
            if reason is None:
                raise
        if reason:
            raise CloudflareBlocked(reason)
        data = json.loads(body)
        return data.get("livestream") is not None
