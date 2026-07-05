from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All tunables live here and in .env — nothing hardcoded in business logic."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Browser / session
    headless: bool = True
    forg1_url: str = "https://www.forg1.com/predicciones"
    storage_state_path: Path = Path(".auth/forg1_state.json")
    # Persistent on-disk Chrome profile: keeps login + Cloudflare clearance
    # across runs, so you log in once and it's never forgotten.
    chrome_profile_dir: Path = Path(".auth/chrome-profile")

    # Polling
    poll_interval_idle_seconds: int = 45
    poll_interval_active_seconds: int = 5
    page_refresh_after_idle_seconds: int = 1200
    prediction_stabilization_seconds: int = 50
    balance_refresh_interval_seconds: int = 120

    # Strategy sizing — % of balance staked per bet. min_pct is the floor (weak
    # favorite), max_pct the ceiling (strong favorite); the site's own per-bet
    # max_bet still caps the final amount. Set via STRATEGY_MIN_PCT / STRATEGY_MAX_PCT.
    strategy_min_pct: float = 13.0
    strategy_max_pct: float = 32.0

    # Telegram (chat_id may be empty: it's auto-discovered on startup via getUpdates)
    telegram_bot_token: str
    telegram_chat_id: str = ""

    # Kick (alert-only)
    kick_channel: str = "forg1"
    kick_check_interval_seconds: int = 1800

    # Logging
    log_dir: Path = Path("logs")
    log_level: str = "INFO"

    # State
    seen_predictions_path: Path = Path(".data/seen_predictions.json")
