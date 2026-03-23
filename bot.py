# bot.py
import asyncio
import time
import json
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv
import os

from market_discovery import get_current_window, get_next_window, build_slug, fetch_market
from price_feed import BinancePriceFeed
from strategy import MispricingStrategy
from executor import init_client, place_maker_order, place_market_order, cancel_all

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger("bot")

file_handler = logging.FileHandler("bot.log")
file_handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
log.addHandler(file_handler)

class TradingBot:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.bankroll = float(os.getenv("STARTING_BANKROLL", 100.0))
        self.strategy = MispricingStrategy(
            kelly_fraction=float(os.getenv("KELLY_FRACTION", 0.5)),
            min_edge=float(os.getenv("MIN_EDGE_THRESHOLD", 0.03)),
            min_bet=float(os.getenv("MIN_BET", 1.0)),
        )
        self.price_feed = BinancePriceFeed()
        self.client = None if dry_run else init_client()
        self.log_path = "trade_log.json"
        self.trade_log = self._load_existing_log()

    async def run(self):
        log.info(f"Bot started | Bankroll: ${self.bankroll:.2f} | Dry run: {self.dry_run}")
        await self.price_feed.connect()
        await asyncio.sleep(3)  # Let price feed warm up

        while True:
            try:
                await self.trade_cycle()
            except (KeyboardInterrupt, asyncio.CancelledError):
                log.info("Shutting down...")
                break
            except Exception as e:
                log.error(f"Error in trade cycle: {e}")
                await asyncio.sleep(10)

    async def trade_cycle(self):
        # Calculate the CURRENT window (the one that's already open)
        window_start, window_end = get_current_window()
        now = int(time.time())
        seconds_remaining = window_end - now

        # Only trade when we're in the window with enough time to analyze
        # but late enough to have a signal
        if seconds_remaining > 60:
            # Too early — wait
            wait_time = seconds_remaining - 60
            log.info(f"Waiting {wait_time}s until T-60 for window {window_start}")
            await asyncio.sleep(wait_time)
            return

        if seconds_remaining < 5:
            # Too late — wait for next window
            log.info(f"Window {window_start} closing in {seconds_remaining}s, skipping")
            await asyncio.sleep(seconds_remaining + 1)
            return

        # Set window open price from Binance
        if self.price_feed.window_open_price is None:
            self.price_feed.set_window_open(self.price_feed.current_price)

        # Discover the market
        slug = build_slug(window_start)
        market = fetch_market(slug)

        if not market or not market.get("accepting_orders"):
            log.info(f"Market {slug} not available or not accepting orders")
            await asyncio.sleep(5)
            return

        # Evaluate mispricing
        trade = self.strategy.evaluate(
            market, self.bankroll, self.price_feed, seconds_remaining
        )

        if trade is None:
            log.info(
                f"Window {window_start} | No mispricing found | "
                f"Up={market['Up']['price']:.2f} Down={market['Down']['price']:.2f} | "
                f"BTC delta={self.price_feed.get_window_delta()*100:.3f}%"
            )
            await asyncio.sleep(seconds_remaining + 1)
            self.price_feed.window_open_price = None
            return

        # Execute
        log.info(
            f"TRADE | {trade['side']} @ ${trade['price']:.2f} | "
            f"Edge: {trade['edge']*100:.1f}% | "
            f"Kelly: {trade['kelly_pct']*100:.1f}% | "
            f"Bet: ${trade['bet_amount']:.2f} | "
            f"Shares: {trade['shares']}"
        )

        if not self.dry_run:
            try:
                if trade["use_maker"] and seconds_remaining > 15:
                    # Use maker order if we have time and edge is thin
                    resp = place_maker_order(
                        self.client,
                        trade["token_id"],
                        trade["price"],
                        trade["shares"],
                    )
                else:
                    # Use market order if time is short
                    resp = place_market_order(
                        self.client,
                        trade["token_id"],
                        trade["bet_amount"],
                    )
                log.info(f"Order response: {resp}")
            except Exception as e:
                log.error(f"Order failed: {e}")

        # Log the trade
        self.trade_log.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "window": window_start,
            "slug": slug,
            **trade,
            "bankroll_before": self.bankroll,
        })

        # Wait for resolution
        await asyncio.sleep(seconds_remaining + 5)
        self.price_feed.window_open_price = None

        # Check outcome (simplified — in production, poll the market result)
        await self.check_outcome(window_start, trade)

    async def check_outcome(self, window_start, trade):
        """Check if the trade won or lost by fetching resolved market data."""
        slug = build_slug(window_start)
        try:
            market = fetch_market(slug)
            if market:
                # After resolution, the winning side's price goes to ~$1.00
                up_price = market.get("Up", {}).get("price", 0.5)
                won = (
                    (trade["side"] == "Up" and up_price > 0.90) or
                    (trade["side"] == "Down" and up_price < 0.10)
                )

                if won:
                    profit = trade["shares"] * (1.0 - trade["price"])
                    self.bankroll += profit
                    pnl = round(profit, 2)
                    outcome = "win"
                    log.info(f"WIN  | +${profit:.2f} | Bankroll: ${self.bankroll:.2f}")
                else:
                    loss = trade["bet_amount"]
                    self.bankroll -= loss
                    pnl = round(-loss, 2)
                    outcome = "loss"
                    log.info(f"LOSS | -${loss:.2f} | Bankroll: ${self.bankroll:.2f}")

                # Update the trade log entry for this window
                for entry in self.trade_log:
                    if entry["window"] == window_start:
                        entry["outcome"] = outcome
                        entry["pnl"] = pnl
                        entry["bankroll_after"] = round(self.bankroll, 2)
                        break
        except Exception as e:
            log.error(f"Could not check outcome: {e}")

    def _load_existing_log(self):
        if os.path.exists(self.log_path):
            try:
                with open(self.log_path, "r") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return []

    def save_log(self):
        with open(self.log_path, "w") as f:
            json.dump(self.trade_log, f, indent=2)
        log.info(f"Trade log saved to {self.log_path}")


async def main():
    import sys
    dry_run = "--dry-run" in sys.argv
    bot = TradingBot(dry_run=dry_run)
    try:
        await bot.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        bot.save_log()

if __name__ == "__main__":
    asyncio.run(main())
