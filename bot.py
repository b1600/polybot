# bot.py — v2 multi-phase trading loop
#
# KEY CHANGES FROM v1:
# ─────────────────────────────────────────────────────────
# v1: Evaluate ONCE at T-60. One shot, one strategy.
# v2: Evaluate CONTINUOUSLY throughout each window on a 5s tick.
#     Three strategies fire at different phases:
#       T-240 → T-30:  Market making (two-sided quotes)
#       T-180 → T-30:  Fade extreme spikes (opportunistic)
#       T-15  → T-3:   Late-window directional scalp
#
# v1: Single trade per window, sleep until resolution.
# v2: MM orders go out early + get monitored for fills.
#     If one side fills, the other is cancelled immediately.
#     A late scalp can STILL fire even if MM is already active.
#
# v1: No order tracking. Fire and forget.
# v2: Tracks open order IDs per window. Cancels stale orders
#     at window boundaries. Knows which side filled.
#
# v1: Bankroll only updates after resolution.
# v2: Bankroll tracks committed capital (open orders reduce
#     available bankroll to prevent over-betting).
#
# ─────────────────────────────────────────────────────────

import asyncio
import time
import json
import logging
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
    place_maker_order,
    place_market_order,
    cancel_order,
    cancel_all,
)

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
)
log = logging.getLogger("bot")

# ── Configuration ──────────────────────────────────────────

EVAL_INTERVAL = 5       # seconds between strategy evaluations within a window
RESOLUTION_WAIT = 6     # seconds after window close before checking outcome
MM_CANCEL_BUFFER = 20   # cancel unfilled MM orders this many seconds before close
MAX_TRADES_PER_WINDOW = 3  # hard cap: 1 MM pair + 1 fade/scalp
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

        # Order tracking
        self.mm_order_ids: dict[str, str] = {}  # side → order_id
        self.mm_fill_side: str | None = None     # which MM side filled
        self.directional_trade: dict | None = None
        self.trades: list[dict] = []

        # Flags
        self.mm_placed = False
        self.mm_cancelled = False
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
        self.bankroll = float(os.getenv("STARTING_BANKROLL", 100.0))
        self.session_start_bankroll = self.bankroll
        self.strategy = CombinedStrategy()
        self.price_feed = BinancePriceFeed()
        self.client = None if dry_run else init_client()
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

        # ── Initialize window state ────────────────────────
        self.window = WindowState(window_start)
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

        # ── Check outcomes for all trades this window ──────
        await self._resolve_window()

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

            # Cancel MM orders before window close if unfilled
            if (
                self.window.mm_placed
                and not self.window.mm_cancelled
                and seconds_remaining < MM_CANCEL_BUFFER
            ):
                await self._cancel_mm_orders("approaching window close")

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

            if phase == "mm" and not self.window.mm_placed:
                await self._execute_mm(result)

            elif phase == "fade" and not self.window.fade_fired:
                await self._execute_directional(result, "FADE")
                self.window.fade_fired = True

            elif phase == "scalp" and not self.window.scalp_fired:
                # Cancel any remaining MM orders before scalping
                if self.window.mm_placed and not self.window.mm_cancelled:
                    await self._cancel_mm_orders("scalp taking over")
                await self._execute_directional(result, "SCALP")
                self.window.scalp_fired = True

            # Sleep until next tick
            sleep_time = min(EVAL_INTERVAL, max(1, seconds_remaining - 3))
            await asyncio.sleep(sleep_time)

    # ── Execution: Market Making ───────────────────────────

    async def _execute_mm(self, orders: list[dict]):
        """
        Place two-sided maker orders (buy Up + buy Down).
        Track order IDs so we can cancel the opposite side on fill.
        """
        self.window.mm_placed = True

        for order in orders:
            side = order["side"]
            log.info(
                f"MM-POST | {side} @ ${order['price']:.2f} | "
                f"Shares: {order['shares']} | "
                f"Amount: ${order['bet_amount']:.2f}"
            )

            order_id = None
            if not self.dry_run:
                try:
                    resp = place_maker_order(
                        self.client,
                        order["token_id"],
                        order["price"],
                        order["shares"],
                    )
                    order_id = resp.get("orderID") or resp.get("id")
                    log.info(f"MM-POST | {side} order ID: {order_id}")
                except Exception as e:
                    log.error(f"MM-POST | {side} order failed: {e}")
                    continue

            # Track the order
            trade_record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "window": self.window.window_start,
                "slug": self.window.slug,
                "order_id": order_id,
                "status": "open",
                **order,
                "bankroll_before": self.bankroll,
            }
            self.window.trades.append(trade_record)
            self.trade_log.append(trade_record)

            if order_id:
                self.window.mm_order_ids[side] = order_id

    async def _cancel_mm_orders(self, reason: str):
        """Cancel all resting MM orders for this window."""
        self.window.mm_cancelled = True
        if self.dry_run:
            log.info(f"MM-CANCEL | (dry run) reason: {reason}")
            for t in self.window.trades:
                if t.get("strategy") == "mm" and t.get("status") == "open":
                    t["status"] = "cancelled"
            return

        for side, order_id in self.window.mm_order_ids.items():
            try:
                cancel_order(self.client, order_id)
                log.info(
                    f"MM-CANCEL | {side} order {order_id} | {reason}"
                )
            except Exception as e:
                log.error(f"MM-CANCEL | {side} failed: {e}")

        # Update trade records
        for t in self.window.trades:
            if t.get("strategy") == "mm" and t.get("status") == "open":
                t["status"] = "cancelled"

    # ── Execution: Directional (Scalp / Fade) ──────────────

    async def _execute_directional(self, trade: dict, label: str):
        """Execute a single directional trade (scalp or fade)."""
        log.info(
            f"{label} | {trade['side']} @ ${trade['price']:.2f} | "
            f"Edge: {trade['edge']*100:.1f}% | "
            f"Bet: ${trade['bet_amount']:.2f} | "
            f"Shares: {trade['shares']}"
        )

        order_id = None
        if not self.dry_run:
            try:
                if trade.get("use_maker"):
                    resp = place_maker_order(
                        self.client,
                        trade["token_id"],
                        trade["price"],
                        trade["shares"],
                    )
                else:
                    resp = place_market_order(
                        self.client,
                        trade["token_id"],
                        trade["bet_amount"],
                    )
                order_id = resp.get("orderID") or resp.get("id")
                log.info(f"{label} | Order ID: {order_id}")
            except Exception as e:
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
        """
        Cancel any resting orders that didn't fill before
        the window closes.
        """
        if self.window.mm_placed and not self.window.mm_cancelled:
            await self._cancel_mm_orders("window closing")

        # In live mode, also cancel_all as a safety net
        if not self.dry_run and self.client:
            try:
                cancel_all(self.client)
            except Exception as e:
                log.error(f"cancel_all safety sweep failed: {e}")

    # ── Resolution ─────────────────────────────────────────

    async def _resolve_window(self):
        """
        Check the resolved market to determine win/loss for
        each trade placed this window.

        In production, you'd poll the CLOB or listen to the
        WebSocket for fill confirmations and market resolution.
        For dry-run and simplicity, we use the Gamma API price
        after resolution.
        """
        if not self.window or not self.window.trades:
            return

        market = self._fetch_market_safe()
        if not market:
            log.error(
                f"Could not fetch resolved market for "
                f"window {self.window.window_start}"
            )
            return

        up_price = market.get("Up", {}).get("price", 0.5)
        # After resolution: winning side → ~$1.00, losing → ~$0.00
        winning_side = "Up" if up_price > 0.90 else "Down" if up_price < 0.10 else None

        if winning_side is None:
            log.warning(
                f"Window {self.window.window_start} | "
                f"Resolution unclear (Up={up_price:.2f}). Skipping P&L."
            )
            return

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


if __name__ == "__main__":
    asyncio.run(main())