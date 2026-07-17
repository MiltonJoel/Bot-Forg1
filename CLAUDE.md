# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An async Python bot that monitors forg1.com's predictions page, sizes a bet with a fractional-Kelly strategy, **places the bet on the live UI via Playwright**, and reports everything to Telegram. Note: the README and pyproject description still say "read-only / never places bets" — that is outdated; `PredictionPageReader.place_bet()` and `MonitoringService` really do execute bets.

Production runs on a Google Cloud free-tier VPS under `xvfb-run` as a systemd service (`HEADLESS=false` with a virtual display — real headful Chrome is what keeps Cloudflare happy). This desktop checkout is for development; fixes only take effect after deploying to the VPS.

## Commands

```bash
uv sync && uv run playwright install   # install deps + browsers
./run.sh                               # run the bot (uses .venv python; system python has broken loguru)
uv run pytest -q                       # all tests
uv run pytest tests/test_bet_execution.py -q            # one file
uv run pytest tests/test_bet_execution.py::test_name -q # one test
uv run ruff check . && uv run black . && uv run mypy app  # lint / format / typecheck (mypy is strict)
```

Requires Python 3.13 and a `.env` (copy `.env.example`; `TELEGRAM_BOT_TOKEN` is the only required value — `TELEGRAM_CHAT_ID` is auto-discovered on first run via getUpdates).

## Architecture

`main.py` wires everything and runs two independent asyncio tasks under `_run_forever` (crash → log → restart in 10s): `MonitoringService` and `KickLiveChecker`.

- `app/bot/browser.py` — `persistent_context()`: real Chrome (`channel="chrome"`) with an on-disk profile at `.auth/chrome-profile`. The persistent profile is what preserves the Cloudflare clearance across runs. It deliberately strips Playwright's profile-hostile default flags and refuses to launch if another Chrome holds the profile (`SingletonLock` check).
- `app/bot/login.py` — forg1 auth is via Kick OAuth and the session cookie is session-scoped, so the profile alone doesn't survive restarts: cookies are snapshotted to `.auth/forg1_state.json` after a confirmed login and re-injected on startup. First run blocks on manual login. Login state is detected from the server-embedded `"isLoggedIn"` flag, not the DOM.
- `app/bot/prediction_page.py` — all scraping. Predictions are NOT scraped from the rendered DOM: forg1 is a Next.js app and the data is parsed out of the RSC payload (`self.__next_f.push`) fetched over `page.request.get()`. Consequence: the visible DOM can be stale, so `place_bet()` reloads the page before clicking. Betting is a two-step UI flow whose final "Confirmar" button is Cloudflare-gated; everything after the outcome click is single-attempt (never retry a possibly-submitted bet). Balance reads/refresh DO use the rendered DOM (header selectors).
- `app/services/monitoring_service.py` — poll loop: idle/active poll intervals, a stabilization wait before betting (re-reads the prediction so the pool settles), dedupe via `.data/seen_predictions.json`, one in-memory pending slot for result reporting.
- `app/strategies/` — pure decision logic (`decide(Prediction) -> BetDecision`); strategies never see a Page. `AggressiveFractionalKellyStrategy` is the one in use; sizing tunables come from `.env`.
- `app/services/kick_service.py` — polls Kick's public channel API with a plain urllib request (browser UA to pass Cloudflare) and notifies live/offline.

### Kick tab (owner's decision — do not remove)

`KickLiveChecker` receives the shared browser `context` and opens a kick.com tab when the stream goes live. The owner requires this; it has been removed twice and he restored it both times. Known accepted trade-offs: the tab plays video (bandwidth) and Kick cookie rotation can log forg1 out. That logout case is handled, not prevented: the monitor's `is_logged_in` server check alerts on Telegram once and recovers after a manual re-login in the bot's Chrome window (VNC on the VPS); startup login likewise polls instead of waiting on stdin, so it works under systemd.

## Testing conventions

Tests are stdlib-style pytest with hand-rolled fakes (no pytest-asyncio; async paths are driven with `asyncio.run` inside tests). Strategy tests are pure; page/bet tests fake the Playwright objects.
