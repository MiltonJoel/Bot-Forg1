from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All tunables live here and in .env — nothing hardcoded in business logic."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Browser / session
    headless: bool = True
    forg1_url: str = "https://www.forg1.com/predicciones"
    storage_state_path: Path = Path(".auth/forg1_state.json")
    # Drop boxes. Opened automatically the moment their ~24h cooldown expires;
    # the page's own countdown sets the poll interval, so this costs a handful
    # of requests a day. Empty disables the watcher.
    boxes_url: str = "https://www.forg1.com/cajas"
    # Persistent on-disk Chrome profile: keeps login + Cloudflare clearance
    # across runs, so you log in once and it's never forgotten.
    chrome_profile_dir: Path = Path(".auth/chrome-profile")
    # Separate copy of the profile for the Kick viewer: it runs as its own
    # Chrome process (Cloudflare 403s the player's API under CDP), and two
    # Chromes cannot share one user-data-dir.
    kick_viewer_profile_dir: Path = Path(".auth/kick-viewer")
    # Unpacked extension to load into the viewer (it is a separate browser and
    # inherits nothing from the bot's profile). None = no extension.
    kick_viewer_extension_dir: Path | None = None

    # Polling
    poll_interval_idle_seconds: int = 45
    poll_interval_active_seconds: int = 5
    page_refresh_after_idle_seconds: int = 1200
    prediction_stabilization_seconds: int = 50
    balance_refresh_interval_seconds: int = 120

    # --- Strategy -----------------------------------------------------------
    # How much of the crowd's pool share to believe, after shrinking toward a
    # uniform prior. 1.0 = treat the pool as a fair price (no edge from the crowd
    # alone, bot rarely bets). 0.0 = pure noise. NOTE any value below 1.0
    # mathematically favours the underdog; it only sets how good a price is
    # demanded before staking.
    #
    # 0.12 is the measured value: over 19 of this bot's own binary markets the
    # crowd implied 71.6% for the favourite, which won 52.6%. On a replay of
    # those markets it returns +152% against +27% for a cautious 1.0/0.85, with
    # a worst dip to -12%. OWNER'S DECISION: run it aggressive — a cautious
    # setting takes too long to recover the bankroll. The risk is understood and
    # accepted, and BANKROLL_STOP_PCT is the backstop. Do not quietly lower this.
    #
    # Caveat that stays true regardless: n=19, ~1.8 sigma, and measured only on
    # CS2 map matches. Re-measure with scripts/analyze_results.py as
    # .data/results.jsonl fills up.
    strategy_crowd_weight: float = 0.12
    # Same weight for markets with no official rating (kill counts, player
    # names, one-off questions). Strictly an assumption — the calibration was
    # never measured there — but a Kick audience betting free points is at least
    # as noisy off-game as on it, and the owner wants those markets bet too.
    # Raise this alone to keep team bets aggressive while calming the rest.
    strategy_unrated_crowd_weight: float = 0.12
    # Fraction of the full Kelly stake. 1.0 IS the growth-maximising fraction by
    # definition, and a grid search over the logged markets confirms it: +228%
    # against +152% at 0.5. The cost is variance — bootstrapping those markets,
    # ~25% of runs end below the starting bankroll and the 5th percentile is a
    # 77% loss. Chosen deliberately for maximum growth.
    strategy_kelly_fraction: float = 1.0
    # Hard ceiling on any single stake, as % of balance. NOT a throttle to
    # loosen: raising it LOWERS returns. Same markets at full Kelly, 25% -> +228%
    # but 40% -> +133%, with a deeper drawdown too (13,116 vs 9,511). Past the
    # Kelly optimum extra leverage costs growth — that is overbetting, not
    # caution. 25 was the best value found across the whole grid.
    strategy_max_pct: float = 25.0
    # Minimum expected value per unit staked before betting at all.
    strategy_min_edge: float = 0.05
    # Weight on Valve's official standings vs the pool price, for head-to-head
    # markets where both teams are ranked — the only genuine outside information
    # the bot has. Kept moderate so it informs the price without overriding the
    # crowd calibration above. 0 disables the prior.
    strategy_rating_weight: float = 0.35
    # Ratings gap that makes a team a 10:1 favourite. MEASURED, not guessed:
    # fitted by minimising log loss over 385 real CS2 matches played AFTER the
    # rating snapshot used to predict them (Valve's 2026-06-01 standings vs
    # results in the 2026-07-06 detail files), so it is genuinely out of sample.
    # Log loss 0.664 against 0.693 for a coin flip; the higher-rated team won
    # 63.9%; calibration checks out (predicted 20-40% -> actual 32%, 40-60% ->
    # 45%, 60-80% -> 64%). The earlier 800 was a guess and far too confident:
    # it turned a 737-point gap into p=0.89 where the real figure is p=0.74.
    strategy_rating_scale: float = 1400.0
    # --- MapForm: team strength from bo3.gg match results ---------------------
    # Weight on the Elo model in app/services/match_history.py, blended the same
    # way as the Valve rating above; the crowd keeps whatever the two leave.
    #
    # SHIPS AT 0 — OFF — on purpose. It is a genuine second outside opinion and
    # it covers thousands of teams Valve never ranks, but "better data" is not
    # the same as "an edge over the pool", and this bot has been wrong about
    # that before (the Valve ratings beat a coin flip and still do not beat the
    # pool). Turn it on only when scripts/analyze_results.py prints a bootstrap
    # CI on `form vs pool` that clears zero. Until then it costs nothing to
    # collect the measurement with the weight at 0.
    strategy_form_weight: float = 0.0
    # Elo constants, FITTED by scripts/fit_map_form.py against held-out maps —
    # never guess these, the same way strategy_rating_scale was never guessed.
    # scale: rating gap that makes a team a ~10:1 favourite (logistic, so 400 is
    # the classic Elo decade). k_global: how fast a team's overall strength
    # moves. k_map: how fast its per-map deviation moves; 0 disables the
    # map-specific half and leaves plain global Elo.
    # Fitted 2026-08-28 over 54,624 bo3.gg maps (2024-08-28 .. 2026-08-28),
    # scored prequentially on the 10,716 played after 2026-03-08: log loss
    # 0.6619 against 0.6931 for a coin flip, 59.7% accuracy, and calibration
    # that holds across every bucket (20%->20%, 34%->34%, 50%->50%, 66%->66%,
    # 79%->81%). `scale` is pinned rather than fitted — in Elo it is redundant
    # with K, so searching both walks a ridge and lands on the grid edge.
    strategy_form_scale: float = 400.0
    strategy_form_k_global: float = 52.0
    # k_map buys +0.0020 log loss over k_map=0. That is real but tiny, and it
    # matches what the reference project found (map identity: at most +0.01 AUC
    # on top of round state). It is kept for the shrinkage it gives thin maps.
    # Do NOT claim a map-specific edge on the strength of it.
    strategy_form_k_map: float = 16.0
    # Ignore teams with fewer than this many maps on record — an Elo built from
    # three games is noise wearing a number.
    strategy_form_min_games: int = 10
    # Only bet when the form model ALONE rates the chosen side at least this
    # much above its share of the two teams' pool (0.05 = 5 points). The blend
    # still picks the side and the stake; this only drops bets the model would
    # not make, including every market it has no opinion on. Unset = off.
    # 0.05 from a 2026-10-08 replay of the 135 logged markets with
    # CounterPredictor v1 at form weight 0.3: it kept 68 of 85 bets and tied or
    # beat betting every edge in every 40-day simulation. Requires
    # strategy_form_weight > 0, or the bot refuses to start.
    strategy_min_form_edge: float | None = None
    # NOT a tier artifact — that hypothesis was tested and killed (2026-08-28).
    # The worry was that fitting on all of bo3.gg (mostly tier-3) mistunes the
    # model for the tier-1/2 forg1 streams. Re-scored on 4,612 held-out maps
    # played between forg1's own 29 teams and their direct opponents, the
    # population makes no difference at all: 0.6658 log loss / 59.8% accuracy
    # against 0.6634 / 60% on all of bo3, and the best k_global there is 32
    # versus 52 here, worth 0.0005 log loss. MapForm predicts CS2 maps at ~60%
    # whoever is playing. Do not re-open this.
    match_history_path: Path = Path(".data/match_history.json")
    # The owner's CounterPredictor model (~/Desktop/CounterPredictor), exported
    # by scripts/export_counterpredictor.py. When this file exists it REPLACES
    # MapForm as the form model; strategy_form_weight still sets its say.
    counterpredictor_path: Path = Path(".data/counterpredictor.json")
    # Where Telegram /actualizarmodelo runs update.py, and which model it
    # re-exports (v1 is the live one since 2026-10-08; false = v2).
    counterpredictor_source_dir: Path = Path("~/Desktop/CounterPredictor")
    counterpredictor_v1: bool = True
    # Ask bo3.gg which map a "2do MAPA" market is actually on. One extra HTTP
    # call per new market, and the whole thing degrades to "no map" (still a
    # usable overall-strength prediction) whenever it fails. Irrelevant while
    # strategy_form_weight is 0, since nothing reads the map then.
    resolve_map_names: bool = True
    # Look up Polymarket's price for the same map ("Map 2 Winner") and use it in
    # place of the form model's probability when both teams match exactly
    # (app/services/polymarket.py). Takes the form model's slot, so
    # strategy_form_weight and strategy_min_form_edge still govern it.
    polymarket_lookup: bool = True
    # Only bet markets with at most this many outcomes. The crowd calibration
    # was measured head-to-head; all four multi-way bets in the logs lost.
    strategy_max_outcomes: int = 2
    # Exempt from that limit: a Team/Team/OT map market, where "OT" wins
    # outright and both team bets lose. This weight prices the OT side alone.
    # At 1.0 it is the pool's own price — exactly zero edge, so OT is never
    # backed and only the two teams are. Lower it to make OT bettable; that is
    # a bet that CS2 maps go to overtime more often than the ~18% the crowd
    # pays for, which nothing here has measured.
    strategy_ot_crowd_weight: float = 1.0
    # Owner's standing order: always bet on these teams when they appear,
    # whatever the price (comma-separated, e.g. "9z"). This SKIPS the edge
    # check — backing a favourite at 85% of the pool pays 0.18x and needs them
    # to win 85% of the time to break even. Stake is the Kelly amount when the
    # price is good and min_bet when it is not, so a bad price costs 500.
    strategy_always_back: str = ""
    # When the strategy sits out, back the pool favourite with this fraction of
    # the balance (capped by the site's max_bet). 0 = off. Ties are skipped.
    strategy_fav_fallback_pct: float = 0.0
    # ...but not when the favourite already holds this share of the pool or
    # more (0.75 = skip 75%+ favourites, which pay 0.33x or less). 1.0 = off.
    strategy_fav_fallback_max_share: float = 1.0
    team_ratings_path: Path = Path(".data/team_ratings.json")

    # Kill switch: stop betting once the balance falls this far below the
    # session's starting balance. Alerts on Telegram and keeps monitoring.
    bankroll_stop_pct: float = 40.0

    # Append every resolved market (final shares + winner + our bet) here, so
    # the calibration above can be re-measured out of sample.
    results_log_path: Path = Path(".data/results.jsonl")
    # Observation only: markets skipped for their shape (3/4-way), with every
    # pool read while open and the closing pool. Never read by the strategy.
    skipped_log_path: Path = Path(".data/results_skipped.jsonl")

    # Telegram (chat_id may be empty: it's auto-discovered on startup via getUpdates)
    telegram_bot_token: str
    telegram_chat_id: str = ""
    # api.telegram.org is slow from this network. Measured: TLS handshake 0.25s,
    # but a full response 10.8-16s, and some attempts stall past 30s — while a
    # fresh retry after a stall succeeds in ~0.9s. The old 10s timeout sat below
    # normal latency, so sends failed at random ("handshake operation timed
    # out"). 20s clears the real latency; a stall gets cut early and retried,
    # which is what actually recovers the message.
    telegram_timeout_seconds: float = 20.0
    telegram_attempts: int = 3

    # Kick (alert-only)
    kick_channel: str = "forg1"
    kick_check_interval_seconds: int = 1800

    # Logging
    log_dir: Path = Path("logs")
    log_level: str = "INFO"

    # State
    seen_predictions_path: Path = Path(".data/seen_predictions.json")
