import asyncio
import json
import random
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from app.utils.retry import short_error

from app.bot.prediction_page import PredictionPageReader
from app.models.prediction import BetDecision, Prediction
from app.services import polymarket
from app.services.map_resolver import map_number_in_title, resolve_live_map
from app.services.notifier import TelegramNotifier
from app.strategies.base_strategy import BettingStrategy
from app.strategies.contrarian_pari_mutuel import _OVERTIME_RE
from app.utils.http import CloudflareBlocked

# Tolerate a blip, escalate an outage. At the 45s idle poll that is ~4 minutes
# of failed reads before Telegram hears about it.
_READ_FAILURES_BEFORE_ALERT = 5

# Market ids remembered as already handled. forg1 can run several markets at
# once since 2026-10-31, so one "last id" is not enough; this bounds the file.
_SEEN_IDS_KEPT = 200

# Markets the strategy refuses by shape ("4-way market (limit 2); ..."), the
# ones results_skipped.jsonl observes. ponytail: keyed on the reason text from
# ContrarianPariMutuelStrategy._unbettable_reason; if that wording changes the
# log just goes quiet, it cannot touch a bet. A favourite-fallback bet on such
# a market keeps that reason as its prefix, so it is logged here too.
_SHAPE_SKIP_RE = re.compile(r"^\d+-way market")


class MonitoringService:
    """Detects new predictions, runs the strategy, executes the bet on the
    page, and reports the outcome on Telegram."""

    def __init__(
        self,
        reader: PredictionPageReader,
        strategy: BettingStrategy,
        notifier: TelegramNotifier,
        *,
        seen_predictions_path: Path,
        poll_idle_seconds: int,
        poll_active_seconds: int,
        refresh_after_idle_seconds: int,
        stabilization_seconds: int,
        balance_refresh_interval_seconds: int,
        bankroll_stop_pct: float = 100.0,
        results_log_path: Path | None = None,
        skipped_log_path: Path | None = None,
        resolve_maps: bool = False,
        polymarket_lookup: bool = False,
        stream_is_live: Callable[[], bool] | None = None,
    ) -> None:
        self._reader = reader
        self._strategy = strategy
        self._notifier = notifier
        self._seen_path = seen_predictions_path
        self._poll_idle = poll_idle_seconds
        self._poll_active = poll_active_seconds
        self._refresh_after_idle = refresh_after_idle_seconds
        self._stabilization_seconds = stabilization_seconds
        self._balance_refresh_interval_seconds = balance_refresh_interval_seconds
        self._bankroll_stop_pct = bankroll_stop_pct
        self._results_log_path = results_log_path
        self._skipped_log_path = skipped_log_path
        # Every distinct pool each pending market showed while open, for
        # results_skipped.jsonl, by market id. Only filled for shape-skipped markets.
        self._skipped_reads: dict[str, list[dict[str, object]]] = {}
        # Off by default so tests and any embedder never reach the network
        # implicitly; main.py turns it on from settings.resolve_map_names.
        self._resolve_maps = resolve_maps
        self._polymarket_lookup = polymarket_lookup
        self._stream_is_live = stream_is_live
        # Kill switch state: the balance this session opened with, and whether
        # we have already stopped betting because of a drawdown.
        self._session_start_balance: int | None = None
        self._bankroll_stopped = False
        self._blocked_alerted = False
        self._read_failures = 0
        # Every market already handled, oldest first. A set of ids, not one
        # "last id": with two markets open at once, a single id let the bot see
        # A, then B, then A again as "new" if the order changed, and bet A twice.
        self._seen_ids: list[str] = self._load_seen()
        # Markets awaiting their result, by id. forg1 can run several at once
        # since 2026-10-31. In memory only: lost on restart, so a result
        # mid-flight won't be reported; fine.
        self._pending: dict[str, tuple[Prediction, BetDecision]] = {}
        self._last_balance_refresh = time.monotonic()
        self._logout_alerted = False
        self._was_offline: bool | None = None

    # --- polling loop --------------------------------------------------------

    async def run(self) -> None:
        logger.info("Monitoring started. Waiting for predictions.")
        idle_elapsed = 0
        while True:
            # Reads go over HTTP, so nothing else re-renders the visible tab: it
            # sat on forg1's offline view for up to 20 min into a live stream
            # (and kept the live view all night). Reload once on each Kick edge;
            # the first observation is not an edge.
            offline = self._stream_offline()
            if self._was_offline is not None and offline != self._was_offline:
                logger.info(
                    "Stream went {}; reloading the forg1 tab.", "offline" if offline else "live"
                )
                await self._refresh_page()
            self._was_offline = offline

            # Off-stream there is nothing to find: forg1 only opens markets while
            # the stream runs, so reading the page around the clock is pure
            # Cloudflare/Vercel exposure for zero predictions. A bet placed just
            # before the stream ended still needs its result, so a pending slot
            # keeps the loop awake until it resolves.
            if self._stream_offline() and not self._pending:
                idle_elapsed = 0
                await self._sleep(self._poll_idle)
                continue

            try:
                open_predictions = await self._reader.read_open_predictions()
            except CloudflareBlocked as exc:
                # Not a read error: the bot is locked out and would otherwise
                # sit here reporting "no predictions" indefinitely.
                await self._alert_blocked(str(exc))
                await self._sleep(self._poll_idle)
                continue
            except Exception as exc:
                logger.error("Failed to read prediction page ({}). Backing off.", short_error(exc))
                await self._alert_read_failures(exc)
                await self._sleep(self._poll_idle)
                continue
            await self._clear_blocked_alert()
            await self._clear_read_failures()
            for open_prediction in open_predictions:
                self._observe_skipped(open_prediction)

            # A pending prediction may have resolved since the last poll.
            await self._check_pending_resolution()
            await self._maybe_refresh_balance()

            if not open_predictions:
                idle_elapsed += self._poll_idle
                if idle_elapsed >= self._refresh_after_idle:
                    idle_elapsed = 0
                    await self._refresh_page()
                await self._sleep(self._poll_idle)
                continue

            idle_elapsed = 0
            prediction = next((p for p in open_predictions if p.id not in self._seen_ids), None)
            if prediction is None:
                await self._sleep(self._poll_active)
                continue

            logger.info("New prediction detected: {}", prediction.id)
            await self._handle_prediction(prediction)
            await self._sleep(self._poll_active)

    async def _refresh_page(self) -> None:
        # A reload can time out (Cloudflare blocking the tab, say). Reads run
        # over HTTP anyway, so log it and keep polling rather than killing the loop.
        try:
            await self._reader.refresh()
        except Exception as exc:
            logger.warning("Page refresh failed ({}); continuing.", short_error(exc))

    async def _sleep(self, seconds: float) -> None:
        # ponytail: +-15% jitter so polling isn't a metronome — a request landing
        # at the exact same interval for hours is a classic bot-timing signal for
        # Cloudflare. Upgrade to a real backoff if that ever stops being enough.
        await asyncio.sleep(random.uniform(seconds * 0.85, seconds * 1.15))

    # --- dedupe state --------------------------------------------------------

    @property
    def _last_seen_id(self) -> str | None:
        return self._seen_ids[-1] if self._seen_ids else None

    def _load_seen(self) -> list[str]:
        if not self._seen_path.exists():
            return []
        try:
            data: dict[str, object] = json.loads(self._seen_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            # A half-written file (killed mid-write) would otherwise crash the
            # bot at startup, every startup. Losing the id only risks re-betting
            # a prediction that is almost certainly already locked.
            logger.warning(
                "Unreadable seen-predictions file ({}); starting fresh.", short_error(exc)
            )
            return []
        # Files written before 2026-10-31 hold only "last_id".
        seen = data.get("seen_ids")
        ids = [str(i) for i in seen] if isinstance(seen, list) else []
        last = data.get("last_id")
        if isinstance(last, str) and last not in ids:
            ids.append(last)
        return ids[-_SEEN_IDS_KEPT:]

    def _save_last_seen(self, prediction_id: str) -> None:
        if prediction_id not in self._seen_ids:
            self._seen_ids.append(prediction_id)
        self._seen_ids = self._seen_ids[-_SEEN_IDS_KEPT:]
        self._seen_path.parent.mkdir(parents=True, exist_ok=True)
        self._seen_path.write_text(
            json.dumps({"last_id": prediction_id, "seen_ids": self._seen_ids})
        )

    # --- betting -------------------------------------------------------------

    async def _with_map_name(self, prediction: Prediction) -> Prediction:
        """Attach which CS2 map this market is about, if bo3.gg can tell us.

        forg1 titles say "2do MAPA" and never which map that is, so MapForm
        would otherwise only ever see the teams' overall strength. This costs
        one HTTP call per new market and is best-effort in every direction:
        blocked, ambiguous, offline, or not a two-team market all leave
        `map_name` as None, which MapForm handles as "no map information".

        Runs in a thread because bo3.gg is plain blocking urllib, and this sits
        directly in front of a bet — stalling the event loop here would stall
        the page reader and the balance refresh with it.
        """
        if not self._resolve_maps:
            return prediction
        labels = tuple(o.label for o in prediction.outcomes if not _OVERTIME_RE.match(o.label))
        if len(labels) != 2:
            return prediction
        try:
            map_name = await asyncio.to_thread(resolve_live_map, prediction.title, labels)
        except Exception as exc:  # never let a stats lookup stop a bet
            logger.debug("Map lookup failed for {}: {}", prediction.id, short_error(exc))
            return prediction
        if map_name:
            logger.info("{} is being played on {}.", prediction.title, map_name)
        return prediction.model_copy(update={"map_name": map_name})

    async def _with_polymarket(self, prediction: Prediction) -> Prediction:
        """Attach Polymarket's price for this exact map, when the match is
        certain (see app/services/polymarket.py). Best-effort like the map
        lookup: any failure or doubt leaves `polymarket` as None and the
        strategy prices exactly as it did before."""
        if not self._polymarket_lookup:
            return prediction
        labels = tuple(o.label for o in prediction.outcomes if not _OVERTIME_RE.match(o.label))
        number = map_number_in_title(prediction.title)
        if len(labels) != 2 or number is None:
            return prediction
        try:
            found = await asyncio.to_thread(
                polymarket.map_probability, (labels[0], labels[1]), number
            )
        except Exception as exc:  # never let a price lookup stop a bet
            logger.debug("Polymarket lookup failed for {}: {}", prediction.id, short_error(exc))
            return prediction
        logger.info(
            "Polymarket for {} map {}: {}", " vs ".join(labels), number, found or "no exact match"
        )
        return prediction.model_copy(update={"polymarket": found})

    async def _handle_prediction(self, prediction: Prediction) -> None:
        # Mark it immediately so the same snapshot is not re-stabilized on the
        # next polling cycle while we wait for the pool to settle.
        self._save_last_seen(prediction.id)
        detected = prediction

        stabilized = await self._stabilize(prediction)
        if stabilized is None:
            return
        prediction = stabilized

        prediction = await self._with_map_name(prediction)
        prediction = await self._with_polymarket(prediction)

        decision = self._strategy.decide(prediction)
        logger.info(
            "Bet decision for {}: should_bet={} side={} amount={} reason={}",
            prediction.id,
            decision.should_bet,
            decision.side,
            decision.amount,
            decision.reason,
        )

        placed = False
        if await self._bankroll_exhausted(prediction.available_points):
            decision = decision.model_copy(
                update={"should_bet": False, "amount": 0, "reason": "kill switch: drawdown limit"}
            )
        elif decision.should_bet and decision.side and decision.amount > 0:
            try:
                logger.info(
                    "Placing bet now for {}: {} points on {}",
                    prediction.id,
                    decision.amount,
                    decision.side,
                )
                # Title and outcome count let the reader click the card inside
                # THIS market when another one is open with the same labels.
                placed = await self._reader.place_bet(
                    decision.side,
                    decision.amount,
                    market_title=prediction.title,
                    outcome_count=len(prediction.outcomes),
                )
            except Exception as exc:
                logger.error("Bet execution failed: {}", short_error(exc))
        else:
            logger.info(
                "Skipping bet for {} because the strategy did not return a placeable amount.",
                prediction.id,
            )

        remaining = await self._post_bet_balance(placed)
        await self._notify(prediction, decision, placed, remaining)
        # Remember it so the result gets reported once it resolves.
        self._pending[prediction.id] = (prediction, decision)
        self._skipped_reads[prediction.id] = []
        self._observe_skipped(detected)
        self._observe_skipped(prediction)

    async def _stabilize(self, prediction: Prediction) -> Prediction | None:
        """Waits for the pool to settle, then re-reads the prediction. Returns
        the refreshed snapshot, or None if the window should be skipped (the
        prediction changed underneath us, or the re-read failed)."""
        if self._stabilization_seconds <= 0:
            return prediction

        logger.info(
            "Stabilizing pool for {}s before betting on {}",
            self._stabilization_seconds,
            prediction.id,
        )
        await asyncio.sleep(self._stabilization_seconds)
        try:
            # By id: another market may have opened meanwhile, and "the first
            # open one" would then be a different market.
            refreshed = await self._reader.read_active_prediction(prediction_id=prediction.id)
        except Exception as exc:
            logger.error(
                "Failed to re-read stabilized prediction {}: {}", prediction.id, short_error(exc)
            )
            return None
        if refreshed is None or refreshed.id != prediction.id:
            logger.info(
                "Prediction {} closed during stabilization; skipping stale snapshot.",
                prediction.id,
            )
            return None
        logger.info(
            "Stabilization window elapsed for {}; proceeding with refreshed snapshot.",
            prediction.id,
        )
        return refreshed

    async def _bankroll_exhausted(self, balance: int) -> bool:
        """True once the balance has fallen `bankroll_stop_pct` below where this
        session started. Betting stops, monitoring continues — a losing run
        should cost a drawdown, not the whole bankroll. Restarting the bot
        resets the reference point, which is the intended manual override.
        """
        if self._session_start_balance is None:
            self._session_start_balance = balance
            logger.info("Session bankroll reference set at {} pts.", balance)
        if self._bankroll_stopped:
            return True

        floor = self._session_start_balance * (1 - self._bankroll_stop_pct / 100)
        if balance >= floor:
            return False

        self._bankroll_stopped = True
        logger.error(
            "Kill switch: balance {} fell below {:.0f} ({}% drawdown limit). No more bets.",
            balance,
            floor,
            self._bankroll_stop_pct,
        )
        await self._notifier.send(
            f"🛑 Freno automático: el balance bajó de {self._session_start_balance} a {balance} pts "
            f"(límite {self._bankroll_stop_pct:.0f}%). El bot deja de apostar pero sigue "
            "monitoreando. Reiniciá el bot para volver a habilitarlo."
        )
        return True

    def _record_result(self, prediction: Prediction, decision: BetDecision, winner: str) -> None:
        """Appends the finished market to a JSONL file: the pool shares, the
        winner, and what we staked. This is the out-of-sample data the crowd
        calibration needs — the old text logs never recorded the losing
        outcome's label, so no rating model could be fitted from them.

        The shares are the ones from `_pending`, i.e. the pool as it stood when
        the strategy DECIDED, not as it stood at lock. That is deliberate and is
        what makes the calibration meaningful: it measures the prices the bot
        actually had to beat. Do not "improve" this by re-reading the prediction
        at resolution — that would score the strategy against prices it never
        saw, and quietly flatter it, because late money moves toward the winner.
        """
        if self._results_log_path is None:
            return
        record = {
            "id": prediction.id,
            "title": prediction.title,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
            "pool": prediction.total_pool,
            "outcomes": [
                {"label": o.label, "points": o.total_points, "bettors": o.total_bettors}
                for o in prediction.outcomes
            ],
            "winner": winner,
            # Which map it was, when we could tell — this is what lets
            # scripts/analyze_results.py score MapForm per map after the fact.
            "map": prediction.map_name,
            "polymarket": prediction.polymarket,
            "bet_side": decision.side if decision.should_bet else None,
            "bet_amount": decision.amount if decision.should_bet else 0,
            "fallback": decision.fallback,
            "reason": decision.reason,
        }
        try:
            self._results_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._results_log_path.open("a") as handle:
                handle.write(json.dumps(record) + "\n")
        except OSError as exc:  # bookkeeping must never break the bot
            logger.warning("Could not append to results log: {}", short_error(exc))

    def _observe_skipped(self, prediction: Prediction | None) -> None:
        """Remembers this read's pool if it belongs to the pending market and
        that market was skipped for its shape. Observation only."""
        if self._skipped_log_path is None or prediction is None:
            return
        entry = self._pending.get(prediction.id)
        if entry is None or not _SHAPE_SKIP_RE.match(entry[1].reason):
            return
        try:
            points = {o.label: o.total_points for o in prediction.outcomes}
            reads = self._skipped_reads.setdefault(prediction.id, [])
            # Unchanged pools add nothing; the 5s active poll would repeat them.
            if not reads or reads[-1]["points"] != points:
                reads.append({"at": prediction.captured_at.isoformat(), "points": points})
        except Exception as exc:  # bookkeeping must never break the bot
            logger.warning("Could not record skipped-market read: {}", short_error(exc))

    async def _record_skipped(
        self, prediction: Prediction, decision: BetDecision, winner: str
    ) -> None:
        """Appends a shape-skipped market (3/4-way kill ranges, a draw, ...) to
        results_skipped.jsonl with every pool it showed while open and the pool
        forg1 actually paid on, so whether these markets are mispriced can be
        checked later. Observation only: nothing here feeds a decision.

        `final_points` is re-read from the resolved payload, where the pool
        sits frozen at lock — that is the closing pool forg1 pays on. The last
        entry of `reads` is the last open read (up to one active poll before
        lock) and is the fallback when that re-read fails.
        """
        if self._skipped_log_path is None or not _SHAPE_SKIP_RE.match(decision.reason):
            return
        try:
            try:
                final: dict[str, int] | None = await self._reader.read_final_pool(prediction.id)
            except Exception as exc:
                logger.warning(
                    "Could not read final pool for {}: {}", prediction.id, short_error(exc)
                )
                final = None
            record = {
                "id": prediction.id,
                "title": prediction.title,
                "options": [o.label for o in prediction.outcomes],
                "locks_at": prediction.locks_at.isoformat(),
                "resolved_at": datetime.now(timezone.utc).isoformat(),
                "reads": self._skipped_reads.get(prediction.id, []),
                "final_points": final,
                "winner": winner,
                "reason": decision.reason,
            }
            self._skipped_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._skipped_log_path.open("a") as handle:
                handle.write(json.dumps(record) + "\n")
        except Exception as exc:  # bookkeeping must never break the bot
            logger.warning("Could not append to skipped-markets log: {}", short_error(exc))
        self._skipped_reads.pop(prediction.id, None)

    async def _post_bet_balance(self, placed: bool) -> int | None:
        """The REAL balance after a bet, or None if unavailable.

        Never computed as available_points - amount: the header snapshot behind
        that arithmetic can be stale, which produced wrong "quedan X pts" numbers
        on Telegram. Click the header refresh first so the read reflects what the
        site actually deducted.
        """
        if not placed:
            return None
        try:
            await self._reader.refresh_balance()
            await asyncio.sleep(1)  # let the header update after the click
            return await self._reader.read_balance()
        except Exception as exc:
            logger.warning("Could not read post-bet balance ({}); omitting it.", short_error(exc))
            return None

    # --- balance / session upkeep --------------------------------------------

    def _stream_offline(self) -> bool:
        """True only when the Kick check CONFIRMS the stream is down.

        Deliberately not "not live": KickLiveChecker keeps its last known state
        when a check fails or Cloudflare blocks it, so an outage on Kick's side
        never reads as offline here and never blinds the bot to a live market.
        No checker wired in (tests, dry runs) also means never offline.
        """
        return self._stream_is_live is not None and not self._stream_is_live()

    async def _maybe_refresh_balance(self) -> None:
        if self._balance_refresh_interval_seconds <= 0:
            return

        # Only click while the Kick stream is live: predictions only happen during
        # the stream, so clicking around the clock is pure risk (steady automated
        # traffic for Cloudflare/Vercel to flag) with zero benefit. Resumes on its
        # own when the stream comes back.
        if self._stream_offline():
            return

        now = time.monotonic()
        if now - self._last_balance_refresh < self._balance_refresh_interval_seconds:
            return
        # Advance even on failure: a broken page gets probed once per interval,
        # not on every poll cycle (that was the every-45s error spam).
        self._last_balance_refresh = now

        try:
            logged_in = await self._reader.is_logged_in()
        except Exception as exc:
            logger.error("Could not check forg1 login state: {}", short_error(exc))
            return

        if not logged_in:
            await self._alert_logged_out()
            return
        await self._clear_logout_alert()

        try:
            await self._reader.refresh_balance()
        except Exception as exc:
            logger.error("Failed to refresh balance via header button: {}", short_error(exc))

    async def _alert_read_failures(self, exc: Exception) -> None:
        """Escalates a sustained read outage to Telegram.

        A single failure is a blip and stays in the log. A run of them means the
        bot is blind — it cannot see predictions and therefore cannot bet — and
        that used to produce nothing but log warnings nobody was watching.
        """
        self._read_failures += 1
        if self._read_failures != _READ_FAILURES_BEFORE_ALERT:
            return
        await self._notifier.send(
            f"⚠️ El bot no puede leer la página de predicciones hace "
            f"{self._read_failures} intentos seguidos — no está viendo ni "
            f"apostando nada. Revisá la conexión o la ventana de Chrome."
            f"\n\nÚltimo error: {short_error(exc)}"
        )

    async def _clear_read_failures(self) -> None:
        if self._read_failures >= _READ_FAILURES_BEFORE_ALERT:
            await self._notifier.send("✅ El bot volvió a leer las predicciones normalmente.")
        self._read_failures = 0

    async def _alert_blocked(self, reason: str) -> None:
        """Alerts once per block, not once per poll (that would be every 45s).

        Only reached from the READ path, where a Cloudflare interstitial is
        positively identified. A confirm button that won't click is reported as
        a failed bet instead — it has many causes, and asserting a block that
        did not happen is its own bug.
        """
        logger.error("Cloudflare is blocking the prediction reads: {}", reason)
        if self._blocked_alerted:
            return
        self._blocked_alerted = True
        await self._notifier.send(
            "🚫 Cloudflare está bloqueando al bot — no puede leer las predicciones "
            "ni apostar. Entrá a la ventana de Chrome del bot (VNC en el VPS) y "
            f"resolvé el desafío a mano; se recupera solo al detectarlo.\n\nDetalle: {reason}"
        )

    async def _clear_blocked_alert(self) -> None:
        if not self._blocked_alerted:
            return
        self._blocked_alerted = False
        logger.info("Cloudflare block cleared; reads are working again.")
        await self._notifier.send("✅ Cloudflare dejó pasar al bot de nuevo — sigue normal.")

    async def _alert_logged_out(self) -> None:
        """Alerts once per logout, not once per check."""
        if self._logout_alerted:
            return
        self._logout_alerted = True
        logger.warning("forg1 session is logged out; waiting for a manual re-login.")
        await self._notifier.send(
            "⚠️ La sesión de forg1 expiró — el bot no puede leer el balance ni "
            "apostar. Logueate de nuevo en la ventana de Chrome del bot (VNC en "
            "el VPS); se recupera solo al detectarlo."
        )

    async def _clear_logout_alert(self) -> None:
        if not self._logout_alerted:
            return
        self._logout_alerted = False
        logger.info("forg1 session is back after re-login.")
        await self._notifier.send("✅ Sesión de forg1 recuperada — el bot sigue normal.")

    # --- reporting -----------------------------------------------------------

    async def _notify(
        self,
        prediction: Prediction,
        decision: BetDecision,
        placed: bool,
        remaining: int | None = None,
    ) -> None:
        if not decision.should_bet:
            result = "⏭️ Sin apuesta"
        elif placed:
            result = f"✅ Apuesta ejecutada: {decision.amount} puntos a {decision.side}"
            if decision.fallback:
                result += " (respaldo al favorito)"
        else:
            # NOT "no se pudo apostar". The site's bet history showed bets
            # landing that the bot had reported as failures — an unclickable
            # Confirmar does not mean nothing was submitted. Say what is
            # actually known, and send the owner to check rather than to
            # blind-fire a second bet on top of one that may already exist.
            result = (
                f"❓ No pude confirmar la apuesta de {decision.amount} puntos a "
                f"{decision.side}. PUEDE que igual haya entrado — revisá tus "
                f"jugadas en forg1 ANTES de apostar a mano, para no duplicarla."
            )

        # available_points is the PRE-bet balance (it still counts the stake), so
        # when a bet lands also show what the page actually reports afterwards —
        # never a computed guess, which was wrong whenever the header was stale.
        balance_line = f"Tu balance: {prediction.available_points} pts"
        if placed and remaining is not None:
            balance_line += f" (quedan {remaining} pts tras apostar {decision.amount})"

        await self._notifier.send(
            "🔔 Nueva predicción\n"
            f"{prediction.title}\n"
            f"Líder: {prediction.leading_outcome.label} "
            f"({prediction.leading_share:.1f}% de {prediction.total_pool} pts en juego)\n"
            f"{balance_line}\n\n"
            f"{result}\n({decision.reason})"
        )
        logger.info("Notification sent for prediction {}", prediction.id)

    async def _check_pending_resolution(self) -> None:
        for prediction_id, (prediction, decision) in list(self._pending.items()):
            try:
                winner = await self._reader.read_resolution(prediction_id)
            except Exception as exc:
                logger.error(
                    "Failed to read resolution for {}: {}", prediction_id, short_error(exc)
                )
                continue
            if winner is None:
                continue  # not resolved yet
            self._record_result(prediction, decision, winner)
            await self._record_skipped(prediction, decision, winner)
            await self._notify_result(prediction, decision, winner)
            self._pending.pop(prediction_id, None)

    async def _notify_result(
        self, prediction: Prediction, decision: BetDecision, winning_label: str
    ) -> None:
        # Reload so "Tu balance" reflects the payout the site just applied.
        try:
            await self._reader.refresh()
            balance: int | None = await self._reader.read_balance()
        except Exception as exc:
            logger.error("Could not read updated balance after resolution: {}", short_error(exc))
            balance = None

        if not decision.should_bet:
            # Say why it sat out (edge, form filter, 3+ way market, kill switch),
            # not a bare "no bet" that reads like a lost suggestion.
            verdict = f"Ganó: {winning_label} — no aposté ({decision.reason})"
        elif decision.side == winning_label:
            verdict = f"✅ Ganó: {winning_label} — el lado sugerido acertó"
        else:
            verdict = f"❌ Ganó: {winning_label} — el lado sugerido ({decision.side}) no acertó"
        if decision.should_bet and decision.fallback:
            verdict += " (respaldo al favorito)"

        balance_line = f"Balance actualizado: {balance} pts" if balance is not None else ""
        await self._notifier.send(
            "\n".join(
                filter(None, ["🏁 Predicción finalizada", prediction.title, verdict, balance_line])
            )
        )
        logger.info(
            "Result notification sent for prediction {} (winner={})", prediction.id, winning_label
        )
