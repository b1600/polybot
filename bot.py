# bot.py — v2 multi-phase trading loop
#
# Three strategies fire at different phases:
#   T-120 → T-30:  Early momentum (directional, taker)
#   T-180 → T-30:  Fade extreme spikes (opportunistic, taker)
#   T-30  → T-3:   Late-window directional scalp (taker, with maker fallback)
#
# SCALP uses FOK; on "no match" retries up to 2x with FOK again.
# ─────────────────────────────────────────────────────────

import asyncio
import time
import json
import logging
import threading
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
import os

from market_discovery import (
    get_current_window,
    build_slug,
    fetch_market,
)
from price_feed import BinancePriceFeed
from strategy_v2 import CombinedStrategy
from executor import (
    init_client,
    place_market_order,
    get_ask_depth,
    cancel_all,
    get_usdc_balance,
)

load_dotenv()


class TelegramHandler(logging.Handler):
    """
    Logging handler that sends log records to a Telegram chat.
    Runs each send in a daemon thread so it never blocks the bot loop.
    Call flush() before process exit to wait for any in-flight sends.
    Silently drops messages if credentials are missing or the API call fails.
    """

    def __init__(self):
        super().__init__()
        self.token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self._url = (
            f"https://api.telegram.org/bot{self.token}/sendMessage"
            if self.token else None
        )
        self._pending: list[threading.Thread] = []
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord):
        if not self._url or not self.chat_id:
            return
        text = self.format(record)
        t = threading.Thread(target=self._send, args=(text,), daemon=True)
        with self._lock:
            self._pending.append(t)
        t.start()

    def flush(self):
        """Block until all queued Telegram sends have completed."""
        with self._lock:
            threads, self._pending = list(self._pending), []
        for t in threads:
            t.join(timeout=10)

    def _send(self, text: str):
        try:
            requests.post(
                self._url,
                json={"chat_id": self.chat_id, "text": text},
                timeout=5,
            )
        except Exception:
            pass  # never let Telegram errors crash the bot


_formatter = logging.Formatter("%(asctime)s | %(message)s")
_file_handler = logging.FileHandler("bot.log")
_file_handler.setFormatter(_formatter)
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_formatter)
_telegram_handler = TelegramHandler()
_telegram_handler.setFormatter(_formatter)

logging.basicConfig(
    level=logging.INFO,
    handlers=[_file_handler, _console_handler, _telegram_handler],
)
log = logging.getLogger("bot")

# ── Configuration ──────────────────────────────────────────

EVAL_INTERVAL = 5       # seconds between strategy evaluations within a window
RESOLUTION_WAIT = 6     # seconds after window close before checking outcome
MAX_TRADES_PER_WINDOW = 3  # hard cap: at most one of each strategy per window
DAILY_LOSS_LIMIT_PCT = 0.25  # stop trading if down 25% from session start


class WindowState:
    """
    Tracks all activity within a single 5-minute window.
    Reset at each window boundary.
    """

    def __init__(self, window_start: int):
        self.window_start = window_start
        self.slug = build_slug(window_start)
        self.window_end = window_start + 300

        # BTC prices for direct resolution (avoids Polymarket settlement lag)
        self.btc_open_price: float | None = None
        self.btc_close_price: float | None = None

        # Order tracking
        self.trades: list[dict] = []

        # Flags
        self.momentum_fired = False
        self.scalp_fired = False
        self.fade_fired = False

    @property
    def trade_count(self) -> int:
        return len(self.trades)

    @property
    def committed_capital(self) -> float:
        """Total USD tied up in open/pending orders this window."""
        total = 0.0
        for t in self.trades:
            if t.get("status") in ("open", "pending"):
                total += t.get("bet_amount", 0.0)
        return total


class TradingBot:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.client = None if dry_run else init_client()
        if dry_run:
            self.bankroll = float(os.getenv("STARTING_BANKROLL", 100.0))
        else:
            self.bankroll = get_usdc_balance(self.client)
        self.session_start_bankroll = self.bankroll
        self.strategy = CombinedStrategy(dry_run=dry_run)
        self.price_feed = BinancePriceFeed()
        self.trade_log: list[dict] = []
        self.window: WindowState | None = None

        # Stats
        self.total_windows = 0
        self.total_trades = 0
        self.wins = 0
        self.losses = 0

    # ── Main loop ──────────────────────────────────────────

    async def run(self):
        log.info(
            f"Bot started | Bankroll: ${self.bankroll:.2f} | "
            f"Dry run: {self.dry_run}"
        )

        await self.price_feed.start()
        try:
            await self.price_feed.wait_until_ready(timeout=30)
        except TimeoutError:
            log.error("Binance price feed failed to connect. Exiting.")
            await self.price_feed.stop()
            return

        while True:
            try:
                await self._window_loop()
            except (KeyboardInterrupt, asyncio.CancelledError):
                log.info("Shutting down...")
                break
            except Exception as e:
                log.error(f"Error in window loop: {e}", exc_info=True)
                await asyncio.sleep(10)

    async def _window_loop(self):
        """
        Outer loop: one iteration per 5-minute window.
        Handles setup, the inner eval loop, cleanup, and resolution.
        """
        # ── Wait for a window with enough time left ────────
        window_start, window_end = get_current_window()
        now = int(time.time())
        seconds_remaining = window_end - now

        if seconds_remaining > 240:
            # Too early in the window for any strategy — sleep
            wait = seconds_remaining - 240
            log.info(
                f"Window {window_start} | Waiting {wait}s until T-240"
            )
            await asyncio.sleep(wait)
            return

        if seconds_remaining < 3:
            # Window is closing — skip to next
            await asyncio.sleep(seconds_remaining + 1)
            return

        # ── Daily loss circuit breaker ─────────────────────
        if self._is_daily_loss_limit_hit():
            log.warning(
                f"Daily loss limit hit "
                f"(${self.bankroll:.2f} / ${self.session_start_bankroll:.2f}). "
                f"Pausing for 30 minutes."
            )
            await asyncio.sleep(1800)
            return

        # ── Price feed health check ────────────────────────
        if self.price_feed.is_stale:
            log.warning("Price feed stale — skipping window")
            await asyncio.sleep(5)
            return

        if self.price_feed.window_open_price is None:
            log.warning("No window open price — skipping")
            await asyncio.sleep(5)
            return

        # ── Sync bankroll from CLOB before placing any orders ─
        await self._refresh_bankroll()

        # ── Initialize window state ────────────────────────
        self.window = WindowState(window_start)
        self.window.btc_open_price = self.price_feed.window_open_price
        self.strategy.on_new_window()
        self.total_windows += 1

        log.info(
            f"{'─'*50}\n"
            f"Window {window_start} | "
            f"BTC open: ${self.price_feed.window_open_price:,.2f} | "
            f"Bankroll: ${self.bankroll:.2f}"
        )

        # ── Inner evaluation loop (5s ticks) ───────────────
        await self._eval_loop()

        # ── End-of-window cleanup ──────────────────────────
        await self._cleanup_window()

        # ── Wait for resolution ────────────────────────────
        now = int(time.time())
        remaining = self.window.window_end - now
        if remaining > 0:
            await asyncio.sleep(remaining + RESOLUTION_WAIT)
        else:
            await asyncio.sleep(RESOLUTION_WAIT)

        # Capture BTC close price (current price ~6s after window end)
        self.window.btc_close_price = self.price_feed.current_price

        # ── Check outcomes for all trades this window ──────
        await self._resolve_window()

        # ── Re-sync bankroll: catches fills missed by model ─
        await self._refresh_bankroll()

        # ── Log summary ────────────────────────────────────
        self._log_window_summary()

    # ── Inner evaluation loop ──────────────────────────────

    async def _eval_loop(self):
        """
        Runs every EVAL_INTERVAL seconds within a window.
        Calls strategy.evaluate_phase() which returns different
        actions at different time phases.
        """
        while True:
            now = int(time.time())
            seconds_remaining = self.window.window_end - now

            if seconds_remaining < 3:
                break  # window is over

            # Don't over-trade
            if self.window.trade_count >= MAX_TRADES_PER_WINDOW:
                await asyncio.sleep(seconds_remaining)
                break

            # Fetch fresh market data
            market = self._fetch_market_safe()
            if market is None:
                await asyncio.sleep(EVAL_INTERVAL)
                continue

            # Available bankroll = total - committed in open orders
            available = self.bankroll - self.window.committed_capital
            if available < 1.0:
                await asyncio.sleep(EVAL_INTERVAL)
                continue

            # ── Evaluate strategy ──────────────────────────
            phase, result = self.strategy.evaluate_phase(
                market, available, self.price_feed, seconds_remaining
            )

            if phase == "momentum" and not self.window.momentum_fired:
                await self._execute_directional(result, "MOMENTUM")
                self.window.momentum_fired = True

            elif phase == "fade" and not self.window.fade_fired:
                await self._execute_directional(result, "FADE")
                self.window.fade_fired = True

            elif phase == "scalp" and not self.window.scalp_fired:
                await self._execute_directional(result, "SCALP")
                self.window.scalp_fired = True

            # Sleep until next tick
            sleep_time = min(EVAL_INTERVAL, max(1, seconds_remaining - 3))
            await asyncio.sleep(sleep_time)

    # ── Execution: Directional (Momentum / Fade / Scalp) ──────────────

    async def _execute_directional(self, trade: dict, label: str):
        """Execute a single directional trade (scalp or fade).

        For SCALP:
        - Pre-checks order book; aborts immediately if asks are empty (suggestion 1).
        - Uses FAK (Fill-and-Kill = IOC) to accept partial fills (suggestion 4).
        - Retries up to SCALP_MAX_RETRIES times, walking up the price cap by
          SCALP_PRICE_STEP each attempt so a higher ask can be hit (suggestion 2).
        - Waits 1s between retries to let the book refill (suggestion 6).
        Non-SCALP labels use a single FAK attempt with no price cap (price=0).
        """
        log.info(
            f"{label} | {trade['side']} @ ${trade['price']:.2f} | "
            f"Edge: {trade['edge']*100:.1f}% | "
            f"Bet: ${trade['bet_amount']:.2f} | "
            f"Shares: {trade['shares']}"
        )

        SCALP_MAX_RETRIES = 2
        SCALP_PRICE_STEP = 0.01  # walk up 1 tick per retry

        order_id = None
        if not self.dry_run:
            max_attempts = (SCALP_MAX_RETRIES + 1) if label == "SCALP" else 1

            # Suggestion 1: pre-check order book depth before any attempt.
            # If asks are empty the SDK will raise "no match" every time —
            # skip all retries and save ~0.9s of wasted round-trips.
            if label == "SCALP":
                asks = get_ask_depth(self.client, trade["token_id"])
                if not asks:
                    log.warning(f"{label} | Order book empty — skipping")
                    return

            for attempt in range(max_attempts):
                # Suggestion 2: walk up price cap each retry.
                # attempt 0 → base price, attempt 1 → +0.01, attempt 2 → +0.02
                price_cap = (
                    round(trade["price"] + attempt * SCALP_PRICE_STEP, 2)
                    if label == "SCALP" else 0
                )

                if attempt > 0:
                    log.warning(
                        f"{label} | no match — retry {attempt} (FAK @ ${price_cap:.2f})"
                    )
                    # Suggestion 6: wait 1s between retries so market makers
                    # have time to repost asks before the next attempt.
                    await asyncio.sleep(1)

                try:
                    resp = place_market_order(
                        self.client,
                        trade["token_id"],
                        trade["bet_amount"],
                        price=price_cap,
                    )
                    order_id = resp.get("orderID") or resp.get("id")

                    # Suggestion 4/5: log partial fills from FAK orders.
                    size_matched = resp.get("size_matched") or resp.get("filled")
                    if size_matched and float(size_matched) < trade["bet_amount"]:
                        log.info(
                            f"{label} | Partial fill: "
                            f"${float(size_matched):.2f} of ${trade['bet_amount']:.2f} "
                            f"| Order ID: {order_id}"
                        )
                    else:
                        log.info(f"{label} | Order ID: {order_id}")
                    break

                except Exception as e:
                    if "no match" in str(e).lower() and attempt < max_attempts - 1:
                        continue
                    log.error(f"{label} | Order failed: {e}")
                    return

        trade_record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "window": self.window.window_start,
            "slug": self.window.slug,
            "order_id": order_id,
            "status": "pending",
            **trade,
            "bankroll_before": self.bankroll,
        }
        self.window.trades.append(trade_record)
        self.trade_log.append(trade_record)

    # ── End-of-window cleanup ──────────────────────────────

    async def _cleanup_window(self):
        """Safety cancel_all sweep at window close (live mode only)."""
        if not self.dry_run and self.client:
            try:
                cancel_all(self.client)
            except Exception as e:
                log.error(f"cancel_all safety sweep failed: {e}")

    # ── Resolution ─────────────────────────────────────────

    async def _resolve_window(self):
        """
        Determine win/loss for each trade using the actual BTC price change.
        Compares btc_open_price (captured at window start) against
        btc_close_price (captured ~6s after window end from the live feed).
        This avoids Polymarket token settlement lag which caused
        "Resolution unclear" false-negatives.
        """
        if not self.window or not self.window.trades:
            return

        btc_open = self.window.btc_open_price
        btc_close = self.window.btc_close_price

        if not btc_open or not btc_close:
            log.warning(
                f"Window {self.window.window_start} | "
                f"BTC prices unavailable for resolution. Skipping P&L."
            )
            return

        if btc_close > btc_open:
            winning_side = "Up"
        elif btc_close < btc_open:
            winning_side = "Down"
        else:
            log.warning(
                f"Window {self.window.window_start} | "
                f"BTC open == close (${btc_open:,.2f}). No clear winner."
            )
            return

        log.info(
            f"Window {self.window.window_start} | "
            f"BTC: ${btc_open:,.2f} → ${btc_close:,.2f} | "
            f"Winner: {winning_side}"
        )

        for trade in self.window.trades:
            # Skip cancelled orders — no P&L
            if trade.get("status") == "cancelled":
                continue

            side = trade["side"]
            bet = trade["bet_amount"]
            price = trade["price"]
            shares = trade.get("shares", bet / price if price > 0 else 0)

            if side == winning_side:
                # Win: each share pays $1.00, we paid $price per share
                profit = shares * (1.0 - price)
                self.bankroll += profit
                self.wins += 1
                trade["outcome"] = "win"
                trade["pnl"] = round(profit, 2)
                log.info(
                    f"WIN  | {trade.get('strategy','?')} {side} | "
                    f"+${profit:.2f} | Bankroll: ${self.bankroll:.2f}"
                )
            else:
                # Loss: we lose the bet amount
                self.bankroll -= bet
                self.losses += 1
                trade["outcome"] = "loss"
                trade["pnl"] = round(-bet, 2)
                log.info(
                    f"LOSS | {trade.get('strategy','?')} {side} | "
                    f"-${bet:.2f} | Bankroll: ${self.bankroll:.2f}"
                )

            trade["status"] = "resolved"
            trade["bankroll_after"] = round(self.bankroll, 2)
            self.total_trades += 1

    # ── Bankroll sync ──────────────────────────────────────

    async def _refresh_bankroll(self):
        """Sync self.bankroll with the actual CLOB balance.

        Called at the start of every window and after resolution so that
        any orders filled before cancellation (or positions settled by
        Polymarket between sessions) are reflected in our capital tracking.
        Skipped in dry-run mode.
        """
        if self.dry_run:
            return
        try:
            actual = get_usdc_balance(self.client)
            if abs(actual - self.bankroll) > 0.01:
                log.info(
                    f"Bankroll sync: model=${self.bankroll:.2f} → "
                    f"actual=${actual:.2f} "
                    f"(drift=${actual - self.bankroll:+.2f})"
                )
            self.bankroll = actual
        except Exception as e:
            log.error(f"Bankroll refresh failed: {e}")

    # ── Helpers ────────────────────────────────────────────

    def _fetch_market_safe(self) -> dict | None:
        """Fetch market data with error handling."""
        try:
            market = fetch_market(self.window.slug)
            if market and market.get("accepting_orders", True):
                return market
        except Exception as e:
            log.error(f"Market fetch failed: {e}")
        return None

    def _is_daily_loss_limit_hit(self) -> bool:
        """Check if we've lost more than the daily limit."""
        if self.session_start_bankroll <= 0:
            return True
        drawdown = (
            (self.session_start_bankroll - self.bankroll)
            / self.session_start_bankroll
        )
        return drawdown >= DAILY_LOSS_LIMIT_PCT

    def _log_window_summary(self):
        """Print a summary line after each window resolves."""
        if not self.window:
            return

        active_trades = [
            t for t in self.window.trades
            if t.get("status") == "resolved"
        ]
        if not active_trades:
            return

        window_pnl = sum(t.get("pnl", 0) for t in active_trades)
        strategies_used = set(t.get("strategy", "?") for t in active_trades)

        log.info(
            f"WINDOW SUMMARY | {self.window.window_start} | "
            f"Trades: {len(active_trades)} | "
            f"Strategies: {','.join(strategies_used)} | "
            f"P&L: ${window_pnl:+.2f} | "
            f"Bankroll: ${self.bankroll:.2f} | "
            f"Session W/L: {self.wins}/{self.losses}"
        )

    def save_log(self, path: str = "trade_log.json"):
        """Persist the full trade log to disk."""
        with open(path, "w") as f:
            json.dump(self.trade_log, f, indent=2, default=str)
        log.info(f"Trade log saved to {path}")

    def print_session_stats(self):
        """Print end-of-session performance summary."""
        total = self.wins + self.losses
        win_rate = (self.wins / total * 100) if total > 0 else 0
        net_pnl = self.bankroll - self.session_start_bankroll
        roi = (net_pnl / self.session_start_bankroll * 100) if self.session_start_bankroll > 0 else 0

        log.info(
            f"\n{'═'*50}\n"
            f"SESSION STATS\n"
            f"{'─'*50}\n"
            f"  Windows observed : {self.total_windows}\n"
            f"  Trades placed    : {total}\n"
            f"  Wins / Losses    : {self.wins} / {self.losses}\n"
            f"  Win rate         : {win_rate:.1f}%\n"
            f"  Net P&L          : ${net_pnl:+.2f}\n"
            f"  ROI              : {roi:+.1f}%\n"
            f"  Final bankroll   : ${self.bankroll:.2f}\n"
            f"{'═'*50}"
        )


# ── Entry point ────────────────────────────────────────────

async def main():
    import sys
    dry_run = "--dry-run" in sys.argv
    bot = TradingBot(dry_run=dry_run)
    try:
        await bot.run()
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        pass
    finally:
        await bot.price_feed.stop()
        bot.print_session_stats()
        bot.save_log()
        _telegram_handler.flush()  # ensure session stats reach Telegram before exit


if __name__ == "__main__":
    asyncio.run(main())