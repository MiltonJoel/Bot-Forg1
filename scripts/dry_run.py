"""Read-only live check: no bets, no Telegram.

Exercises the whole production read path against the real site — Chrome profile,
cookie restore, login detection, RSC payload parsing, balance read — and prints
what the strategy WOULD have staked. `place_bet` is never called.

    .venv/bin/python scripts/dry_run.py

Use it after touching the scraping or the strategy, and to watch the new sizing
for a stream before trusting it with points. Ctrl+C to stop.
"""

import asyncio
import sys

from app.bot.browser import persistent_context
from app.bot.login import ensure_logged_in
from app.bot.prediction_page import PredictionPageReader
from app.config.settings import Settings
from app.strategies.factory import build_strategy


async def main() -> int:
    settings = Settings()  # type: ignore[call-arg]
    strategy = build_strategy(settings)  # exactly what main.py runs

    async with persistent_context(settings.chrome_profile_dir, settings.headless) as context:
        print("Chrome up. Checking the forg1 session (may ask you to log in)…")
        page = await ensure_logged_in(context, settings.forg1_url, settings.storage_state_path)
        reader = PredictionPageReader(page, settings.forg1_url)

        print(f"Logged in: {await reader.is_logged_in()}")
        print(f"Balance:   {await reader.read_balance()} pts")

        prediction = await reader.read_active_prediction()
        if prediction is None:
            print("\nNo open prediction right now — read path works, nothing to price.")
            return 0

        print(f"\nMarket: {prediction.title}")
        for outcome in prediction.outcomes:
            share = outcome.total_points / prediction.total_pool if prediction.total_pool else 0
            pays = (1 / share - 1) if share else float("inf")
            print(
                f"  {outcome.label:20s} {outcome.total_points:>12,} pts "
                f"({share * 100:5.1f}%)  pays {pays:7.2f}x"
            )

        decision = strategy.decide(prediction)
        verdict = (
            f"WOULD BET {decision.amount} on {decision.side}"
            if decision.should_bet
            else "WOULD NOT BET"
        )
        print(f"\n{verdict}\n  {decision.reason}\n\n(dry run — nothing was placed)")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
