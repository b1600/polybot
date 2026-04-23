# bot.py — v2 multi-phase trading loop
#
# Three strategies fire at different phases:
#   T-120 → T-90:  Early momentum (directional, taker FAK)
#   T-180 → T-90:  Fade extreme spikes (opportunistic, taker FAK)
#   T-240 → T-10:  Late-window scalp — single-shot taker execution:
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
    cancel_order,
    get_usdc_balance,
    get_ask_depth,
    get_book,
    get_clob_mid,
    get_order_status,
    redeem_positions,
    fetch_redeemable_positions,
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

EVAL_INTERVAL = 5        # seconds between strategy evaluations within a window
RESOLUTION_WAIT = 480    # seconds after window close before first redeem attempt (8 min — oracle typically resolves in 5–15 min)
MAX_TRADES_PER_WINDOW = 3  # hard cap: at most one of each strategy per window
DAILY_LOSS_LIMIT_PCT = 0.25  # stop trading if down 25% from session start

REDEEM_POLL_INTERVAL = 600   # seconds between Data API polls for oracle readiness (10 min)
REDEEM_MAX_AGE = 86400       # give up on a pending redemption after 24h



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
        self._scalp_cooldown_until: float = 0.0

        # Rec 4C: rolling calibration state (last 20 resolved trades)
        self._wr_history: list[int] = []       # 1=win, 0=loss
        self._p_win_history: list[float] = []  # model P(win) at trade time
        self._kelly_throttle: float = 1.0      # halved when model is miscalibrated

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

        # asyncio.create_task(self._startup_redeem_sweep())  # disabled: another bot handles redeem

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

        if seconds_remaining > 290:
            # Too early in the window for any strategy — sleep
            wait = seconds_remaining - 290
            log.info(
                f"Window {window_start} | Waiting {wait}s until T-290"
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

        # ── Spawn background resolution — don't block next window entry ──
        # MOMENTUM/FADE GTC orders survive cleanup and get extra lifetime
        # equal to RESOLUTION_WAIT before background task cancels them.
        asyncio.create_task(self._resolve_window_background(self.window))

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

            # Rec 3: T-180 entry cutoff — no new signals after 180s into window
            if seconds_remaining < 120:
                await asyncio.sleep(max(1, seconds_remaining - 3))
                break

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
                if time.time() < self._scalp_cooldown_until:
                    pass  # cooldown active — skip this tick
                else:
                    await self._execute_scalp(result)

            # Sleep until next tick
            sleep_time = min(EVAL_INTERVAL, max(1, seconds_remaining - 3))
            await asyncio.sleep(sleep_time)

    # ── Kelly throttle (Rec 4C) ────────────────────────────

    def _apply_kelly_throttle(self, trade: dict) -> bool:
        """Scale bet by _kelly_throttle. Returns False if result falls below $2.50 minimum."""
        if self._kelly_throttle >= 1.0:
            return True
        trade["bet_amount"] = round(trade["bet_amount"] * self._kelly_throttle, 2)
        trade["shares"] = round(trade["bet_amount"] / trade["price"], 1)
        trade["bet_amount"] = round(trade["shares"] * trade["price"], 2)
        if trade["bet_amount"] < 2.50:
            log.info(
                f"Kelly throttle | Scaled bet ${trade['bet_amount']:.2f} < $2.50 minimum — skipping"
            )
            return False
        return True

    # ── Execution: Directional (Momentum / Fade) ──────────────────────

    async def _execute_directional(self, trade: dict, label: str):
        """GTC maker order for MOMENTUM and FADE strategies.
        Posts a resting limit bid at maker_price; cancelled by cleanup sweep if unfilled.
        """
        if not self._apply_kelly_throttle(trade):
            return

        order_price = trade.get("max_price", trade["maker_price"])
        log.info(
            f"{label} | {trade['side']} @ ${trade['price']:.2f} "
            f"(bid ${order_price:.2f}) | "
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
                    price=trade.get("max_price", trade["maker_price"]),
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

        # Rec 5: spawn adverse-fill monitor for GTC orders
        if order_id and not self.dry_run:
            asyncio.create_task(
                self._monitor_gtc(order_id, order_price, trade["side"], trade["token_id"], trade_record)
            )

    # ── Execution: Scalp (single-shot IOC taker) ──────────────────────

    async def _execute_scalp(self, trade: dict):
        """
        Single-shot taker scalp:
        1. Check order book depth — skip if no asks (illiquid).
        2. Place one IOC order, capped at max_price to keep positive EV.
        No GTC, no polling, no retry chain.
        """
        if not self._apply_kelly_throttle(trade):
            return

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
                    log.info("SCALP | No asks in book — skipping")
                    return
                best_ask = float(asks[0].price)
                # Best ask at near-certainty price = post-resolution book
                if best_ask >= 0.95:
                    log.info(
                        f"SCALP | book_anomaly — best ask ${best_ask:.2f} ≥ $0.95, "
                        f"likely post-resolution book — skipping"
                    )
                    self._scalp_cooldown_until = time.time() + 30
                    return
                if best_ask > max_price:
                    log.info(
                        f"SCALP | Best ask ${best_ask:.2f} exceeds max ${max_price:.2f} — skipping"
                    )
                    self._scalp_cooldown_until = time.time() + 30
                    return
                log.info(
                    f"SCALP | Book has {len(asks)} ask level(s), "
                    f"best ${best_ask:.2f} — proceeding with IOC"
                )
            except Exception as e:
                log.warning(f"SCALP | Book depth check failed: {e} — proceeding anyway")

            # Book check passed — lock the scalp slot for this window
            self.window.scalp_fired = True

            # ── Single IOC order ─────────────────────────────────
            ioc_filled = False
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
                    ioc_filled = True
                    log.info(
                        f"SCALP | IOC filled ${size_matched:.2f} | Order: {order_id}"
                    )
                else:
                    log.info(
                        f"SCALP | IOC no fill at max ${max_price:.2f} — skipping"
                    )
                    order_id = None
            except Exception as e:
                log.error(f"SCALP | IOC placement failed: {e}")

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

    # ── Rec 5: GTC adverse-fill monitor ───────────────────

    async def _monitor_gtc(
        self,
        order_id: str,
        posted_price: float,
        direction: str,
        token_id: str,
        trade_record: dict,
    ):
        """
        Poll the book every 15s after posting a GTC bid.
        Cancel if adverse conditions appear:
          1. best_ask < posted_price - 0.02 — signal reversed
          2. best_bid > posted_price       — stronger buyer appeared (adverse selection)
          3. single ask ≥ $0.95            — post-resolution book anomaly
        """
        await asyncio.sleep(15)  # initial wait before first check
        while True:
            if trade_record.get("status") != "pending":
                return  # already filled or cancelled by cleanup

            # Confirm order is still open
            try:
                info = get_order_status(self.client, order_id)
                size_matched = float(info.get("size_matched") or info.get("filled") or 0)
                if size_matched > 0 or info.get("status") in ("MATCHED", "FILLED"):
                    return  # filled — nothing to cancel
            except Exception as e:
                log.warning(f"GTC monitor | Status check failed for {order_id}: {e}")
                await asyncio.sleep(15)
                continue

            # Check book for adverse conditions
            try:
                book = get_book(self.client, token_id)
                cancel_reason = None

                if book and book.asks:
                    best_ask = float(book.asks[0].price)
                    if best_ask < posted_price - 0.02:
                        cancel_reason = (
                            f"ask ${best_ask:.2f} < bid ${posted_price:.2f} − 2¢ (signal reversed)"
                        )
                    elif len(book.asks) == 1 and best_ask >= 0.95:
                        cancel_reason = f"single ask ${best_ask:.2f} ≥ $0.95 (post-resolution book)"

                if cancel_reason is None and book and book.bids:
                    best_bid = float(book.bids[0].price)
                    if best_bid > posted_price:
                        cancel_reason = (
                            f"best_bid ${best_bid:.2f} > our bid ${posted_price:.2f} (adverse selection)"
                        )

                if cancel_reason:
                    try:
                        cancel_order(self.client, order_id)
                        trade_record["status"] = "cancelled"
                        log.info(f"GTC monitor | Cancelled {order_id} — {cancel_reason}")
                    except Exception as e:
                        log.warning(f"GTC monitor | Cancel failed for {order_id}: {e}")
                    return

            except Exception as e:
                log.warning(f"GTC monitor | Book fetch failed for {token_id}: {e}")

            await asyncio.sleep(15)

    # ── End-of-window cleanup ──────────────────────────────

    async def _cleanup_window(self):
        """Cancel resting GTC orders at window close.

        MOMENTUM and FADE GTC orders are intentionally left open — they get
        extra fill time equal to RESOLUTION_WAIT before the background task
        cancels them. All other orders (scalp_gtc, etc.) are cancelled now.
        """
        if not self.dry_run and self.client:
            # Determine if any MOMENTUM/FADE orders need to survive
            surviving = [
                t for t in self.window.trades
                if t.get("status") == "pending"
                and t.get("order_id")
                and t.get("strategy") in ("momentum", "fade")
            ]

            if surviving:
                # Cancel non-MOMENTUM/FADE orders individually; leave survivors open
                for trade in self.window.trades:
                    if trade.get("status") != "pending" or not trade.get("order_id"):
                        continue
                    if trade.get("strategy") in ("momentum", "fade"):
                        log.info(
                            f"CLEANUP | {trade.get('strategy','?')} GTC left open "
                            f"(+{RESOLUTION_WAIT}s extra lifetime) | "
                            f"Order: {trade['order_id']}"
                        )
                        continue  # let it ride through resolution wait
                    # Cancel all other resting orders
                    try:
                        cancel_order(self.client, trade["order_id"])
                        trade["status"] = "cancelled"
                        log.info(
                            f"CLEANUP | Cancelled {trade.get('strategy','?')} "
                            f"Order: {trade['order_id']}"
                        )
                    except Exception as e:
                        log.warning(
                            f"CLEANUP | Could not cancel {trade['order_id']}: {e}"
                        )
            else:
                # No survivors — safe to cancel everything at once
                try:
                    cancel_all(self.client)
                except Exception as e:
                    log.error(f"cancel_all safety sweep failed: {e}")

                # Mark non-MOMENTUM/FADE GTC orders as cancelled
                for trade in self.window.trades:
                    if trade.get("status") != "pending" or not trade.get("order_id"):
                        continue
                    if not trade.get("use_maker"):
                        continue  # IOC orders self-cancel
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

    async def _resolve_window_background(self, window: "WindowState"):
        """
        Background task: waits RESOLUTION_WAIT seconds, then cancels any
        still-open MOMENTUM/FADE GTC orders, resolves P&L, and refreshes bankroll.

        Runs concurrently with the next window's trading so the main loop
        re-enters at T=0 instead of T-120.
        """
        now = int(time.time())
        remaining = window.window_end - now
        await asyncio.sleep(max(0, remaining) + RESOLUTION_WAIT)

        window.btc_close_price = self.price_feed.current_price

        # Check and cancel any MOMENTUM/FADE orders that survived cleanup
        if not self.dry_run and self.client:
            for trade in window.trades:
                if trade.get("status") != "pending" or not trade.get("order_id"):
                    continue
                if trade.get("strategy") not in ("momentum", "fade"):
                    continue
                try:
                    info = get_order_status(self.client, trade["order_id"])
                    size_matched = float(
                        info.get("size_matched") or info.get("filled") or 0
                    )
                    if size_matched == 0:
                        try:
                            cancel_order(self.client, trade["order_id"])
                        except Exception:
                            pass
                        trade["status"] = "cancelled"
                        log.info(
                            f"GTC unfilled — cancelled | "
                            f"{trade.get('strategy','?')} {trade.get('side','?')} "
                            f"Order: {trade['order_id']}"
                        )
                    else:
                        trade["size_matched"] = size_matched
                        log.info(
                            f"GTC filled ${size_matched:.2f} | "
                            f"{trade.get('strategy','?')} {trade.get('side','?')} "
                            f"Order: {trade['order_id']}"
                        )
                except Exception as e:
                    log.warning(
                        f"Could not check/cancel order {trade['order_id']}: {e}"
                    )

        await self._resolve_window(window)
        # await self._redeem_wins()  # disabled: another bot handles redeem
        await self._refresh_bankroll()
        self._log_window_summary(window)

    # ── Resolution ─────────────────────────────────────────

    async def _resolve_window(self, window: "WindowState | None" = None):
        """
        Determine win/loss for each trade using the actual BTC price change.
        Compares btc_open_price (captured at window start) against
        btc_close_price (captured ~6s after window end from the live feed).
        This avoids Polymarket token settlement lag which caused
        "Resolution unclear" false-negatives.
        """
        w = window if window is not None else self.window
        if not w or not w.trades:
            return

        btc_open = w.btc_open_price
        btc_close = w.btc_close_price

        if not btc_open or not btc_close:
            log.warning(
                f"Window {w.window_start} | "
                f"BTC prices unavailable for resolution. Skipping P&L."
            )
            return

        if btc_close > btc_open:
            winning_side = "Up"
        elif btc_close < btc_open:
            winning_side = "Down"
        else:
            log.warning(
                f"Window {w.window_start} | "
                f"BTC open == close (${btc_open:,.2f}). No clear winner."
            )
            return

        log.info(
            f"Window {w.window_start} | "
            f"BTC: ${btc_open:,.2f} → ${btc_close:,.2f} | "
            f"Winner: {winning_side}"
        )

        for trade in w.trades:
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

            # Rec 4C: update rolling calibration state
            self._wr_history.append(1 if side == winning_side else 0)
            self._p_win_history.append(trade.get("estimated_prob", 0.75))
            if len(self._wr_history) > 20:
                self._wr_history.pop(0)
                self._p_win_history.pop(0)

        # Rec 4C: recompute kelly throttle after resolving this window
        if len(self._wr_history) >= 10:
            rolling_wr = sum(self._wr_history) / len(self._wr_history)
            implied_wr = sum(self._p_win_history) / len(self._p_win_history)
            if rolling_wr < implied_wr - 0.10:
                if self._kelly_throttle > 0.5:
                    self._kelly_throttle = 0.5
                    log.warning(
                        f"Kelly throttle | Activated — realized WR {rolling_wr:.1%} "
                        f"vs model {implied_wr:.1%} (gap {implied_wr - rolling_wr:.1%}) "
                        f"— halving bet sizes"
                    )
            else:
                if self._kelly_throttle < 1.0:
                    self._kelly_throttle = 1.0
                    log.info(
                        f"Kelly throttle | Restored — realized WR {rolling_wr:.1%} "
                        f"vs model {implied_wr:.1%} gap within tolerance"
                    )

    # ── Redemption sweep ───────────────────────────────────

    async def _redeem_wins(self):
        """
        For each winning trade this window, spawn a background task that polls
        the Polymarket Data API until the oracle resolves, then redeems on-chain.

        Returns immediately — the window loop is not blocked. Each background
        task retries every REDEEM_POLL_INTERVAL seconds for up to REDEEM_MAX_AGE.
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

        seen: set[int] = set()
        deadline = time.time() + REDEEM_MAX_AGE
        for trade in winning_trades:
            outcome_index = trade["outcome_index"]
            if outcome_index in seen:
                continue
            seen.add(outcome_index)
            label = f"{trade.get('strategy', '?')} {trade['side']}"
            asyncio.create_task(
                self._redeem_background(condition_id, outcome_index, label, deadline)
            )

    async def _redeem_background(
        self, condition_id: str, outcome_index: int, label: str, deadline: float
    ):
        """
        Background task: polls Data API every REDEEM_POLL_INTERVAL seconds until
        the position appears as redeemable, then executes on-chain redemption.
        """
        # === CRITICAL FIX: Normalize condition_id exactly like fetch_redeemable_positions does ===
        if condition_id:
            condition_id = condition_id.removeprefix("0x").removeprefix("0X").lower()
            condition_id = f"0x{condition_id}"

        funder = os.getenv("POLY_FUNDER_ADDRESS", "")
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            try:
                positions = await asyncio.to_thread(fetch_redeemable_positions, funder)
                
                # Debug log (remove after verification)
                log.debug(f"REDEEM | comparing: window={condition_id} | API={[p['condition_id'] for p in positions]}")

                match = next(
                    (p for p in positions
                    if p["condition_id"].lower() == condition_id.lower()), None
                )

                if match is None:
                    log.warning(
                        f"REDEEM | {label} | attempt {attempt} | "
                        f"oracle not yet resolved, retry in {REDEEM_POLL_INTERVAL // 60}m"
                    )
                    await asyncio.sleep(REDEEM_POLL_INTERVAL)
                    continue

                tx_hash = await asyncio.to_thread(
                    redeem_positions, condition_id, outcome_index
                )
                log.info(
                    f"REDEEM | {label} | SUCCESS | Condition: …{condition_id[-8:]} | Tx: {tx_hash}"
                )
                return
            except Exception as e:
                log.error(f"REDEEM FAILED | {label} | Condition: {condition_id} | {e}")
                return  # unexpected error — don't retry

        log.error(
            f"REDEEM FAILED | {label} | oracle not resolved after 24h | "
            f"Condition: {condition_id}"
        )

    async def _startup_redeem_sweep(self):
        """
        On bot startup, redeem any positions the Data API reports as already
        redeemable. Recovers positions stranded by exhausted retries, crashes,
        or restarts in previous sessions.
        """
        if self.dry_run:
            return
        funder = os.getenv("POLY_FUNDER_ADDRESS", "")
        if not funder:
            return
        try:
            positions = await asyncio.to_thread(fetch_redeemable_positions, funder)
            if not positions:
                return
            log.info(f"STARTUP SWEEP | Found {len(positions)} redeemable position(s)")
            for p in positions:
                try:
                    tx_hash = await asyncio.to_thread(
                        redeem_positions, p["condition_id"], p["outcome_index"]
                    )
                    log.info(
                        f"STARTUP SWEEP | Redeemed {p['title']} | Tx: {tx_hash}"
                    )
                except Exception as e:
                    log.error(
                        f"STARTUP SWEEP | Failed to redeem {p['title']}: {e}"
                    )
        except Exception as e:
            log.error(f"STARTUP SWEEP | Error: {e}")

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

    # Reject markets within this many seconds of their close (fix #1)
    _END_TIME_GUARD_SECS = 90

    def _fetch_market_safe(self) -> dict | None:
        """
        Fetch market data with two liveness checks:

        Fix #1 — End-time guard: skip if we're within 90s of window close.
          Gamma's accepting_orders flag can lag reality; markets near expiry
          are too risky regardless.

        Fix #2 — CLOB mid-price: replace Gamma's cached outcomePrices with
          live bid/ask mids from the CLOB. Rejects books with no bids, no asks,
          or asks already at the post-resolution sentinel (>= $0.95).
          Also rejects if the two token mids don't sum to ~$1.00 (stale/broken).
        """
        # Fix #1: end-time guard
        seconds_remaining = self.window.window_end - time.time()
        if seconds_remaining < self._END_TIME_GUARD_SECS:
            log.info(
                f"Market fetch skipped — only {seconds_remaining:.0f}s to window close "
                f"(guard: {self._END_TIME_GUARD_SECS}s)"
            )
            return None

        try:
            market = fetch_market(self.window.slug)
            if not market or not market.get("accepting_orders", False):
                return None
        except Exception as e:
            log.error(f"Market fetch failed: {e}")
            return None

        # Fix #2: CLOB mid-price validation (skipped in dry run — no live client)
        if not self.dry_run:
            up_token = market.get("Up", {}).get("token_id")
            down_token = market.get("Down", {}).get("token_id")

            if not up_token or not down_token:
                log.warning("Market missing token IDs — skipping")
                return None

            up_mid = get_clob_mid(self.client, up_token)
            down_mid = get_clob_mid(self.client, down_token)

            if up_mid is None or down_mid is None:
                log.info(
                    f"Market rejected — dead CLOB book "
                    f"(up_mid={up_mid}, down_mid={down_mid})"
                )
                return None

            mid_sum = up_mid + down_mid
            if not (0.90 <= mid_sum <= 1.10):
                log.info(
                    f"Market rejected — mid prices don't sum to ~$1.00 "
                    f"(up={up_mid:.3f} + down={down_mid:.3f} = {mid_sum:.3f})"
                )
                return None

            # Replace Gamma's stale cached prices with live CLOB mids
            market["Up"]["price"] = round(up_mid, 4)
            market["Down"]["price"] = round(down_mid, 4)
            log.info(
                f"CLOB mid-prices — Up: ${up_mid:.3f} | Down: ${down_mid:.3f} "
                f"(sum: ${mid_sum:.3f})"
            )

        return market

    def _is_daily_loss_limit_hit(self) -> bool:
        """Check if we've lost more than the daily limit."""
        if self.session_start_bankroll <= 0:
            return True
        drawdown = (
            (self.session_start_bankroll - self.bankroll)
            / self.session_start_bankroll
        )
        return drawdown >= DAILY_LOSS_LIMIT_PCT

    def _log_window_summary(self, window: "WindowState | None" = None):
        """Print a summary line after each window resolves."""
        w = window if window is not None else self.window
        if not w:
            return

        active_trades = [
            t for t in w.trades
            if t.get("status") == "resolved"
        ]
        if not active_trades:
            return

        window_pnl = sum(t.get("pnl", 0) for t in active_trades)
        strategies_used = set(t.get("strategy", "?") for t in active_trades)

        log.info(
            f"WINDOW SUMMARY | {w.window_start} | "
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