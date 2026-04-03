# bot.py — v2 multi-phase trading loop
#
# Three strategies fire at different phases:
#   T-120 → T-90:  Early momentum (directional, taker FAK)
#   T-180 → T-90:  Fade extreme spikes (opportunistic, taker FAK)
#   T-220 → T-10:  Late-window scalp — single-shot taker execution:
#                    1. Check order book depth — skip if no asks (illiquid)
#                    2. Place one IOC order, capped at max_price
#                       (prob_win - min_edge) to keep positive EV
#                    No GTC maker, no polling loop, no retry chain.
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
    place_maker_order,
    place_market_order,
    place_ioc_order,
    cancel_all,
    get_usdc_balance,
    get_ask_depth,
    get_order_status,
    redeem_positions,
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
RESOLUTION_WAIT = 120   # seconds after window close before checking outcome (allows oracle to post resolution on-chain)
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

        # Cached from Gamma API — needed for on-chain redemption
        self.condition_id: str | None = None

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

        # ── Redeem winning tokens on-chain → USDC.e back to proxy wallet ──
        await self._redeem_wins()

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

            # Cache condition_id on first successful fetch for post-window redemption
            if not self.window.condition_id and market.get("condition_id"):
                self.window.condition_id = market["condition_id"]

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
                self.window.scalp_fired = True
                await self._execute_scalp(result)

            # Sleep until next tick
            sleep_time = min(EVAL_INTERVAL, max(1, seconds_remaining - 3))
            await asyncio.sleep(sleep_time)

    # ── Execution: Directional (Momentum / Fade) ──────────────────────

    async def _execute_directional(self, trade: dict, label: str):
        """GTC maker order for MOMENTUM and FADE strategies.
        Posts a resting limit bid at maker_price; cancelled by cleanup sweep if unfilled.
        """
        log.info(
            f"{label} | {trade['side']} @ ${trade['price']:.2f} | "
            f"Edge: {trade['edge']*100:.1f}% | "
            f"Bet: ${trade['bet_amount']:.2f} | "
            f"Shares: {trade['shares']}"
        )

        order_id = None
        if not self.dry_run:
            try:
                resp = place_maker_order(
                    self.client,
                    trade["token_id"],
                    price=trade["maker_price"],
                    size=trade["shares"],
                )
                order_id = resp.get("orderID") or resp.get("id")
                log.info(f"{label} | GTC resting | Order ID: {order_id}")
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

    # ── Execution: Scalp (single-shot IOC taker) ──────────────────────

    async def _execute_scalp(self, trade: dict):
        """
        Single-shot taker scalp:
        1. Check order book depth — skip if no asks (illiquid).
        2. Place one IOC order, capped at max_price to keep positive EV.
        No GTC, no polling, no retry chain.
        """
        token_id = trade["token_id"]
        bet_amount = trade["bet_amount"]
        max_price = trade.get("max_price", 0)

        log.info(
            f"SCALP | {trade['side']} @ ${trade['price']:.2f} "
            f"(max ${max_price:.2f}) | "
            f"Edge: {trade['edge']*100:.1f}% | Bet: ${bet_amount:.2f}"
        )

        order_id = None

        if not self.dry_run:
            # ── Book depth check ─────────────────────────────────
            try:
                asks = get_ask_depth(self.client, token_id)
                if not asks:
                    log.info("SCALP | No asks in book — skipping (illiquid)")
                    return
                log.info(f"SCALP | Book has {len(asks)} ask level(s) — proceeding")
            except Exception as e:
                log.warning(f"SCALP | Book depth check failed: {e} — proceeding anyway")

            # ── Single IOC order ─────────────────────────────────
            try:
                resp = place_ioc_order(
                    self.client, token_id, bet_amount, price=max_price
                )
                order_id = resp.get("orderID") or resp.get("id")
                size_matched = float(
                    resp.get("size_matched") or resp.get("filled") or 0
                )

                # The CLOB sometimes returns size_matched=0 in the immediate
                # POST response even when the IOC filled (match is async).
                # If size_matched is ambiguous and we have an order_id, do one
                # follow-up status fetch to get the confirmed fill amount.
                if size_matched == 0 and order_id:
                    try:
                        status = get_order_status(self.client, order_id)
                        size_matched = float(
                            status.get("size_matched") or status.get("filled") or 0
                        )
                    except Exception as e:
                        log.warning(f"SCALP | Order status fetch failed: {e}")

                if size_matched > 0:
                    log.info(
                        f"SCALP | IOC filled ${size_matched:.2f} | Order: {order_id}"
                    )
                else:
                    log.info(
                        f"SCALP | IOC no fill at max ${max_price:.2f} — attempting GTC fallback"
                    )
                    order_id = None
                    # Place a resting GTC limit at the current best ask so we become
                    # the liquidity rather than chasing it. Any taker entering the
                    # market before window close will cross us.
                    try:
                        current_asks = get_ask_depth(self.client, token_id)
                        if current_asks:
                            best_ask = float(current_asks[0].price)
                            gtc_shares = trade["shares"]
                            gtc_resp = place_maker_order(
                                self.client, token_id, price=best_ask, size=gtc_shares
                            )
                            gtc_order_id = gtc_resp.get("orderID") or gtc_resp.get("id")
                            log.info(
                                f"SCALP | GTC fallback resting @ ${best_ask:.2f} | "
                                f"{gtc_shares} shares | Order: {gtc_order_id}"
                            )
                            gtc_record = {
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "window": self.window.window_start,
                                "slug": self.window.slug,
                                "order_id": gtc_order_id,
                                "status": "pending",
                                **trade,
                                "price": best_ask,
                                "maker_price": best_ask,
                                "bet_amount": round(gtc_shares * best_ask, 2),
                                "use_maker": True,
                                "strategy": "scalp_gtc",
                                "bankroll_before": self.bankroll,
                            }
                            self.window.trades.append(gtc_record)
                            self.trade_log.append(gtc_record)
                            return  # GTC placed — IOC miss not worth recording
                        else:
                            log.info("SCALP | GTC fallback skipped — no asks in book")
                    except Exception as e:
                        log.error(f"SCALP | GTC fallback failed: {e}")
            except Exception as e:
                log.error(f"SCALP | IOC placement failed: {e}")
                return

        trade_record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "window": self.window.window_start,
            "slug": self.window.slug,
            "order_id": order_id,
            "status": "pending",
            **trade,
            "bet_amount": bet_amount,
            "bankroll_before": self.bankroll,
        }
        self.window.trades.append(trade_record)
        self.trade_log.append(trade_record)

    # ── End-of-window cleanup ──────────────────────────────

    async def _cleanup_window(self):
        """Cancel all resting GTC orders at window close, then mark unfilled trades."""
        if not self.dry_run and self.client:
            try:
                cancel_all(self.client)
            except Exception as e:
                log.error(f"cancel_all safety sweep failed: {e}")

            # Mark any still-pending GTC orders as cancelled so resolution skips them
            for trade in self.window.trades:
                if trade.get("status") != "pending" or not trade.get("order_id"):
                    continue
                if not trade.get("use_maker"):
                    continue  # IOC orders self-cancel; don't need to check
                try:
                    info = get_order_status(self.client, trade["order_id"])
                    size_matched = float(
                        info.get("size_matched") or info.get("filled") or 0
                    )
                    if size_matched == 0:
                        trade["status"] = "cancelled"
                        log.info(
                            f"GTC unfilled — cancelled | "
                            f"{trade.get('strategy','?')} {trade.get('side','?')} "
                            f"Order: {trade['order_id']}"
                        )
                    else:
                        # Partial or full fill — record actual fill size for resolution
                        trade["size_matched"] = size_matched
                        log.info(
                            f"GTC filled ${size_matched:.2f} | "
                            f"{trade.get('strategy','?')} {trade.get('side','?')} "
                            f"Order: {trade['order_id']}"
                        )
                except Exception as e:
                    log.warning(
                        f"Could not fetch GTC order status {trade['order_id']}: {e}"
                    )

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

            # Skip trades where no order was placed (e.g. dry_run or pre-flight abort)
            if trade.get("order_id") is None and not self.dry_run:
                log.info(
                    f"SKIP | {trade.get('strategy','?')} {trade.get('side','?')} "
                    f"— no order placed (order_id is None)"
                )
                trade["status"] = "skipped"
                continue

            side = trade["side"]
            price = trade["price"]
            # For GTC orders, use actual filled size if available (may be partial fill)
            if trade.get("use_maker") and "size_matched" in trade:
                bet = trade["size_matched"]
                shares = bet / price if price > 0 else 0
            else:
                bet = trade["bet_amount"]
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

    # ── Redemption sweep ───────────────────────────────────

    async def _redeem_wins(self):
        """
        For each winning trade this window, call redeemPositions on-chain so
        the USDC.e flows back to the proxy wallet and is available next window.

        Runs only in live mode. Skips silently if condition_id is unavailable
        (e.g. market fetch failed all window) or if there were no wins.

        The market must have resolved on-chain before redemption succeeds.
        We already wait RESOLUTION_WAIT seconds after window close before
        reaching this point, which is normally sufficient. If the chain hasn't
        settled yet the call will revert and we log an error — the tokens stay
        in the wallet and Polymarket's own sweeper will eventually redeem them.
        """
        if self.dry_run or not self.window:
            return

        condition_id = self.window.condition_id
        if not condition_id:
            log.warning("REDEEM | No condition_id cached — skipping redemption sweep")
            return

        winning_trades = [
            t for t in self.window.trades
            if t.get("outcome") == "win" and t.get("outcome_index") is not None
        ]

        if not winning_trades:
            return

        # De-duplicate: only one redemption call per outcome_index is needed
        # even if somehow two trades landed on the same side.
        seen: set[int] = set()
        for trade in winning_trades:
            outcome_index = trade["outcome_index"]
            if outcome_index in seen:
                continue
            seen.add(outcome_index)

            strategy = trade.get("strategy", "?")
            try:
                tx_hash = redeem_positions(condition_id, outcome_index)
                log.info(
                    f"REDEEM | {strategy} {trade['side']} | "
                    f"Condition: …{condition_id[-8:]} | "
                    f"Tx: {tx_hash}"
                )
            except Exception as e:
                log.error(
                    f"REDEEM FAILED | {strategy} {trade['side']} | "
                    f"Condition: {condition_id} | {e}"
                )

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