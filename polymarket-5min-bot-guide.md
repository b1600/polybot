# Building a Polymarket BTC 5-Minute Trading Bot

A step-by-step guide to building an automated trading bot for Polymarket's BTC Up/Down 5-minute binary markets. Updated for the **post-February 2026 rule changes** (dynamic taker fees, no 500ms delay, `feeRateBps` required in signed orders).

> **Disclaimer:** This is an educational guide. Trading bots can and do lose money. Never risk funds you can't afford to lose. You are responsible for compliance with all applicable laws in your jurisdiction.

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────┐
│                      BOT LOOP                            │
│                                                          │
│  1. DISCOVER  ──→  Calculate next 5-min window slug      │
│                    (deterministic from Unix timestamp)    │
│                                                          │
│  2. FETCH     ──→  Get token IDs from Gamma API          │
│                    Get orderbook from CLOB WebSocket      │
│                    Get BTC price from Binance WebSocket   │
│                                                          │
│  3. ANALYZE   ──→  Compare market odds vs estimated       │
│                    true probability (mispricing check)    │
│                    Calculate Kelly bet size               │
│                                                          │
│  4. EXECUTE   ──→  Sign order with feeRateBps            │
│                    Post GTC maker limit order             │
│                                                          │
│  5. MONITOR   ──→  Track fill, wait for resolution       │
│                    Log result, update bankroll            │
│                                                          │
│  6. REPEAT    ──→  Sleep until next window                │
└──────────────────────────────────────────────────────────┘
```

---

## Step 1: Prerequisites & Environment Setup

### 1.1 What You Need

- **Python 3.9+** (the official SDK requires this)
- **A Polygon wallet** with USDC funded (start with a small amount, e.g. $10–$100)
- **Your private key** exported from your wallet (MetaMask → Account Details → Export Private Key, or from reveal.polymarket.com)
- **Your Polymarket proxy/funder address** (this is the address you deposit USDC to on Polymarket)
- **A VPS or always-on machine** (the bot needs to run 24/7; a $5/mo VPS works fine)

### 1.2 Install Dependencies

```bash
mkdir polymarket-bot && cd polymarket-bot
python -m venv venv
source venv/bin/activate

pip install py-clob-client==0.34.6
pip install websockets requests python-dotenv
```

### 1.3 Create `.env` File

```env
# Wallet
POLY_PRIVATE_KEY=0x_your_private_key_here
POLY_FUNDER_ADDRESS=0x_your_polymarket_proxy_address

# Auth type: 0 = EOA (MetaMask), 1 = Email/Magic wallet
POLY_SIGNATURE_TYPE=0

# Strategy
STARTING_BANKROLL=100.0
KELLY_FRACTION=0.5
MIN_EDGE_THRESHOLD=0.03
MIN_BET=1.0

# Endpoints
CLOB_HOST=https://clob.polymarket.com
BINANCE_WS=wss://stream.binance.com:9443/ws/btcusdt@kline_1m
```

### 1.4 Generate API Credentials

```python
# setup_creds.py — run once
from py_clob_client.client import ClobClient
from dotenv import load_dotenv
import os

load_dotenv()

client = ClobClient(
    host=os.getenv("CLOB_HOST"),
    key=os.getenv("POLY_PRIVATE_KEY"),
    chain_id=137,
    signature_type=int(os.getenv("POLY_SIGNATURE_TYPE", "0")),
    funder=os.getenv("POLY_FUNDER_ADDRESS"),
)

creds = client.create_or_derive_api_creds()
print("Add these to your .env file:")
print(f"POLY_API_KEY={creds.api_key}")
print(f"POLY_API_SECRET={creds.api_secret}")
print(f"POLY_API_PASSPHRASE={creds.api_passphrase}")
```

Run it once: `python setup_creds.py`, then add the output to your `.env` file.

---

## Step 2: Market Discovery

Polymarket's 5-minute BTC markets follow a deterministic naming pattern. You don't search for them — you **calculate** them.

### 2.1 How Slugs Work

The slug format is: `btc-updown-5m-{unix_timestamp}`

The timestamp is the start of the 5-minute window, always divisible by 300 (seconds).

```python
# market_discovery.py
import time
import requests

GAMMA_API = "https://gamma-api.polymarket.com"

def get_current_window():
    """Calculate the current 5-minute window timestamps."""
    now = int(time.time())
    window_start = now - (now % 300)
    window_end = window_start + 300
    return window_start, window_end

def get_next_window():
    """Calculate the NEXT 5-minute window (the one to trade)."""
    now = int(time.time())
    current_start = now - (now % 300)
    next_start = current_start + 300
    next_end = next_start + 300
    return next_start, next_end

def build_slug(window_start):
    """Build the Polymarket slug for a 5-min BTC market."""
    return f"btc-updown-5m-{window_start}"

def fetch_market(slug):
    """Fetch market data from the Gamma API. Returns token IDs and prices."""
    url = f"{GAMMA_API}/events"
    params = {"slug": slug}
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    events = resp.json()

    if not events:
        return None

    event = events[0]
    markets = event.get("markets", [])

    result = {}
    for market in markets:
        # Each market has outcomes like ["Up", "Down"]
        # and clobTokenIds as a JSON string: '["token_up", "token_down"]'
        import json
        outcomes = json.loads(market.get("outcomes", "[]"))
        token_ids = json.loads(market.get("clobTokenIds", "[]"))
        prices = json.loads(market.get("outcomePrices", "[]"))

        condition_id = market.get("conditionId")
        active = market.get("active", False)
        accepting = market.get("acceptingOrders", False)

        for i, outcome in enumerate(outcomes):
            result[outcome] = {
                "token_id": token_ids[i] if i < len(token_ids) else None,
                "price": float(prices[i]) if i < len(prices) else None,
            }
        result["condition_id"] = condition_id
        result["active"] = active
        result["accepting_orders"] = accepting

    return result
```

### 2.2 Usage

```python
window_start, window_end = get_next_window()
slug = build_slug(window_start)
market = fetch_market(slug)

# market = {
#   "Up":   {"token_id": "12345...", "price": 0.52},
#   "Down": {"token_id": "67890...", "price": 0.48},
#   "condition_id": "0xabc...",
#   "active": True,
#   "accepting_orders": True,
# }
```

---

## Step 3: Real-Time Price Feed (Binance WebSocket)

You need live BTC price data to estimate the true probability and detect mispricings.

```python
# price_feed.py
import json
import asyncio
import websockets
from collections import deque

class BinancePriceFeed:
    def __init__(self):
        self.current_price = None
        self.window_open_price = None
        self.price_history = deque(maxlen=60)  # last 60 ticks
        self._ws = None

    async def connect(self):
        uri = "wss://stream.binance.com:9443/ws/btcusdt@kline_1m"
        self._ws = await websockets.connect(uri)
        asyncio.create_task(self._listen())

    async def _listen(self):
        async for msg in self._ws:
            data = json.loads(msg)
            kline = data.get("k", {})
            self.current_price = float(kline.get("c", 0))  # close price
            self.price_history.append(self.current_price)

    def set_window_open(self, price):
        """Call this at the start of each 5-min window."""
        self.window_open_price = price

    def get_window_delta(self):
        """How much BTC has moved since the window opened."""
        if self.window_open_price and self.current_price:
            return (self.current_price - self.window_open_price) / self.window_open_price
        return 0.0

    def get_momentum(self, lookback=10):
        """Simple momentum: avg price change over last N ticks."""
        if len(self.price_history) < lookback + 1:
            return 0.0
        recent = list(self.price_history)[-lookback:]
        return (recent[-1] - recent[0]) / recent[0]
```

---

## Step 4: Strategy — Mispricing Detection + Kelly Criterion

This is the core decision engine. It compares the market's implied probability against your estimated true probability, then sizes the bet with Kelly.

```python
# strategy.py
import math

class MispricingStrategy:
    def __init__(self, kelly_fraction=0.5, min_edge=0.03, min_bet=1.0):
        self.kelly_fraction = kelly_fraction  # half-Kelly recommended
        self.min_edge = min_edge              # minimum edge to trade (3%)
        self.min_bet = min_bet

    def estimate_true_probability(self, window_delta, momentum, seconds_remaining):
        """
        Estimate the true probability that BTC finishes "Up" for this window.

        The dominant signal is window_delta: if BTC is already up vs. the
        window open price, it's more likely to finish up (and vice versa).

        As time remaining decreases, the signal gets stronger because
        there's less time for reversal.
        """
        base_prob = 0.50  # Baseline: 50/50

        # Window delta is the strongest signal
        # Scale: 0.1% move ≈ +/- 5% probability shift
        delta_weight = min(abs(window_delta) / 0.001, 1.0) * 0.08
        if window_delta > 0:
            base_prob += delta_weight
        else:
            base_prob -= delta_weight

        # Time decay: signal is stronger with less time remaining
        # In last 30 seconds, delta signal gets a 50% boost
        if seconds_remaining < 30:
            time_boost = 1.5
        elif seconds_remaining < 60:
            time_boost = 1.2
        else:
            time_boost = 1.0

        # Recompute with time boost
        adjusted_delta = delta_weight * time_boost
        prob_up = 0.50 + (adjusted_delta if window_delta > 0 else -adjusted_delta)

        # Light momentum factor
        momentum_shift = momentum * 500  # scale to ±0.02 range
        momentum_shift = max(-0.02, min(0.02, momentum_shift))
        prob_up += momentum_shift

        # Clamp to reasonable range
        prob_up = max(0.35, min(0.65, prob_up))

        return prob_up

    def calculate_kelly(self, prob_win, share_price):
        """
        Kelly Criterion for binary bet.
        f* = (b*p - q) / b
        where b = (1 - price) / price, p = prob_win, q = 1 - p
        """
        if share_price <= 0 or share_price >= 1:
            return 0.0

        b = (1.0 - share_price) / share_price  # payout ratio
        p = prob_win
        q = 1.0 - p
        f_star = (b * p - q) / b

        if f_star <= 0:
            return 0.0  # No edge, don't bet

        return f_star * self.kelly_fraction  # Apply fractional Kelly

    def calculate_taker_fee(self, share_price):
        """
        Polymarket 5-min market taker fee formula.
        Fee is highest at 50% probability (~1.56%), lowest at extremes.
        fee = 4 * price * (1 - price) * base_rate
        """
        base_rate = 0.0156  # 1.56% max at 50%
        return 4 * share_price * (1 - share_price) * base_rate

    def evaluate(self, market, bankroll, price_feed, seconds_remaining):
        """
        Main decision function. Returns a trade dict or None.

        Returns: {
            "side": "Up" or "Down",
            "token_id": str,
            "price": float,
            "bet_amount": float,
            "shares": float,
            "edge": float,
            "kelly_fraction": float,
        } or None
        """
        window_delta = price_feed.get_window_delta()
        momentum = price_feed.get_momentum(lookback=10)

        prob_up = self.estimate_true_probability(
            window_delta, momentum, seconds_remaining
        )
        prob_down = 1.0 - prob_up

        # Check both sides for mispricing
        candidates = []

        # Check "Up" side
        up_price = market["Up"]["price"]
        up_edge = prob_up - up_price
        if up_edge > self.min_edge:
            kelly = self.calculate_kelly(prob_up, up_price)
            candidates.append({
                "side": "Up",
                "token_id": market["Up"]["token_id"],
                "price": up_price,
                "edge": up_edge,
                "kelly_pct": kelly,
                "prob": prob_up,
            })

        # Check "Down" side
        down_price = market["Down"]["price"]
        down_edge = prob_down - down_price
        if down_edge > self.min_edge:
            kelly = self.calculate_kelly(prob_down, down_price)
            candidates.append({
                "side": "Down",
                "token_id": market["Down"]["token_id"],
                "price": down_price,
                "edge": down_edge,
                "kelly_pct": kelly,
                "prob": prob_down,
            })

        if not candidates:
            return None  # No mispricing found — skip this window

        # Pick the side with the largest edge
        best = max(candidates, key=lambda c: c["edge"])

        bet_amount = best["kelly_pct"] * bankroll
        bet_amount = max(self.min_bet, bet_amount)
        bet_amount = min(bet_amount, bankroll * 0.20)  # hard cap: 20% of bankroll
        shares = bet_amount / best["price"]

        # Check if edge survives taker fee (if not using maker orders)
        taker_fee = self.calculate_taker_fee(best["price"])
        net_edge = best["edge"] - taker_fee

        return {
            "side": best["side"],
            "token_id": best["token_id"],
            "price": best["price"],
            "bet_amount": round(bet_amount, 2),
            "shares": round(shares, 1),
            "edge": round(best["edge"], 4),
            "net_edge_after_taker_fee": round(net_edge, 4),
            "kelly_pct": round(best["kelly_pct"], 4),
            "estimated_prob": round(best["prob"], 4),
            "use_maker": net_edge < self.min_edge,  # prefer maker if edge is thin
        }
```

---

## Step 5: Order Execution

### 5.1 Key 2026 Changes

Three critical changes since February 2026 that your bot MUST handle:

1. **`feeRateBps` is required** in signed order payloads on fee-enabled markets (5-min, 15-min crypto). If you use the official `py-clob-client` SDK, this is handled automatically. If building custom signing, you must include it.

2. **No more 500ms taker delay** — orders execute immediately. This means taker bots compete on pure latency now, which is why **maker (limit) orders are the recommended approach** for non-HFT traders.

3. **Dynamic fees** — fees scale with probability. At 50% odds, taker fee is ~1.56%. At extreme odds (<10% or >90%), fees approach zero.

### 5.2 Placing Orders

```python
# executor.py
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY
from dotenv import load_dotenv
import os

load_dotenv()

def create_client():
    return ClobClient(
        host=os.getenv("CLOB_HOST"),
        key=os.getenv("POLY_PRIVATE_KEY"),
        chain_id=137,
        signature_type=int(os.getenv("POLY_SIGNATURE_TYPE", "0")),
        funder=os.getenv("POLY_FUNDER_ADDRESS"),
    )

def init_client():
    client = create_client()
    client.set_api_creds(client.create_or_derive_api_creds())
    return client

def place_maker_order(client, token_id, price, size):
    """
    Place a GTC limit (maker) order.
    Maker orders earn rebates instead of paying taker fees.
    The SDK handles feeRateBps automatically.
    """
    order_args = OrderArgs(
        price=price,
        size=size,        # number of shares
        side=BUY,
        token_id=token_id,
    )
    signed_order = client.create_order(order_args)
    resp = client.post_order(signed_order, OrderType.GTC)
    return resp

def place_market_order(client, token_id, amount):
    """
    Place a FOK (Fill-or-Kill) market order.
    This pays taker fees but guarantees immediate execution.
    Use when you need speed (e.g., last few seconds of window).
    amount is in USD (USDC).
    """
    market_args = MarketOrderArgs(
        token_id=token_id,
        amount=amount,
        side=BUY,
        order_type=OrderType.FOK,
    )
    signed_order = client.create_market_order(market_args)
    resp = client.post_order(signed_order, OrderType.FOK)
    return resp

def cancel_order(client, order_id):
    """Cancel a resting maker order."""
    return client.cancel(order_id)

def cancel_all(client):
    """Cancel all open orders."""
    return client.cancel_all()
```

---

## Step 6: Main Bot Loop

```python
# bot.py
import asyncio
import time
import json
import logging
from datetime import datetime
from dotenv import load_dotenv
import os

from market_discovery import get_current_window, get_next_window, build_slug, fetch_market
from price_feed import BinancePriceFeed
from strategy import MispricingStrategy
from executor import init_client, place_maker_order, place_market_order, cancel_all

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger("bot")

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
        self.trade_log = []

    async def run(self):
        log.info(f"Bot started | Bankroll: ${self.bankroll:.2f} | Dry run: {self.dry_run}")
        await self.price_feed.connect()
        await asyncio.sleep(3)  # Let price feed warm up

        while True:
            try:
                await self.trade_cycle()
            except KeyboardInterrupt:
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
            "timestamp": datetime.utcnow().isoformat(),
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
                    log.info(f"WIN  | +${profit:.2f} | Bankroll: ${self.bankroll:.2f}")
                else:
                    loss = trade["bet_amount"]
                    self.bankroll -= loss
                    log.info(f"LOSS | -${loss:.2f} | Bankroll: ${self.bankroll:.2f}")
        except Exception as e:
            log.error(f"Could not check outcome: {e}")

    def save_log(self, path="trade_log.json"):
        with open(path, "w") as f:
            json.dump(self.trade_log, f, indent=2)
        log.info(f"Trade log saved to {path}")


async def main():
    import sys
    dry_run = "--dry-run" in sys.argv
    bot = TradingBot(dry_run=dry_run)
    try:
        await bot.run()
    finally:
        bot.save_log()

if __name__ == "__main__":
    asyncio.run(main())
```

---

## Step 7: Run the Bot

```bash
# Dry run (real data, no real trades)
python bot.py --dry-run

# Live trading
python bot.py
```

---

## Step 8: Production Hardening

### 8.1 Token Allowances (Required for EOA Wallets)

If using MetaMask/EOA (signature_type=0), you must approve USDC spending before your first trade:

```python
# approve.py — run once
from web3 import Web3

POLYGON_RPC = "https://polygon-rpc.com"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_ADAPTER = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
account = w3.eth.account.from_key(os.getenv("POLY_PRIVATE_KEY"))

USDC_ABI = [{"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"type":"bool"}],"type":"function"}]
usdc = w3.eth.contract(address=USDC_ADDRESS, abi=USDC_ABI)

MAX_UINT = 2**256 - 1
for spender in [CTF_EXCHANGE, NEG_RISK_ADAPTER]:
    tx = usdc.functions.approve(spender, MAX_UINT).build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address),
        "gas": 100000,
        "gasPrice": w3.to_wei("50", "gwei"),
    })
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"Approval tx: {tx_hash.hex()}")
```

### 8.2 WebSocket Orderbook (Replace REST Polling)

For production, connect to Polymarket's WebSocket for real-time orderbook data instead of polling the REST API:

```python
# Connect to CLOB WebSocket
# wss://ws-subscriptions-clob.polymarket.com/ws/market
# Subscribe with: {"type": "market", "assets_ids": ["<token_id>"]}
```

### 8.3 Error Handling Checklist

- **Rate limits:** Polymarket rate-limits API calls. Use exponential backoff.
- **WebSocket disconnects:** Auto-reconnect with a 5-second delay.
- **Order rejections:** If `feeRateBps` is wrong or missing, the order is silently rejected. The SDK handles this, but verify your SDK version is ≥ 0.34.5.
- **Insufficient balance:** Check USDC balance before placing orders.
- **Stale prices:** If Binance WS disconnects, pause trading.

### 8.4 Logging & Monitoring

Track every trade in a JSON or CSV log:

```
timestamp, window, side, price, bet, shares, edge, outcome, pnl, bankroll
```

After 100+ trades, calculate:
- **Win rate** — if < 51%, you have no edge
- **Actual ROI** vs. theoretical Kelly ROI
- **Max drawdown** — if > 40% of bankroll, reduce Kelly fraction

---

## File Structure

```
polymarket-bot/
├── .env                  # Secrets (NEVER commit this)
├── bot.py                # Main loop
├── market_discovery.py   # Slug calculation + Gamma API
├── price_feed.py         # Binance WebSocket price data
├── strategy.py           # Mispricing detection + Kelly
├── executor.py           # CLOB order placement
├── setup_creds.py        # One-time credential generation
├── approve.py            # One-time token approval (EOA only)
├── trade_log.json        # Auto-generated trade history
└── requirements.txt      # py-clob-client, websockets, requests, python-dotenv
```

---

## Key Takeaways

1. **Be a maker, not a taker.** Taker fees at 50% probability are ~1.56%, which eats most thin edges. Maker orders earn rebates.

2. **Most windows have no trade.** The bot should skip 70–90% of windows where no mispricing exists. If you're trading every window, you have no edge.

3. **Half-Kelly protects you.** Full Kelly is theoretically optimal but assumes perfect probability estimates. Half-Kelly sacrifices ~25% of growth rate but dramatically reduces drawdowns.

4. **Window delta is king.** At the 5-minute scale, the only reliable signal is whether BTC is already above or below the window open price. All other TA indicators are noise.

5. **Start with dry-run.** Run `--dry-run` for at least 200 windows before going live with real money. If your simulated win rate isn't meaningfully above 50% + fees, there's no edge to exploit.

6. **The latency game is over for retail.** The removal of the 500ms delay and introduction of fees killed the simple latency arbitrage strategy. The new meta is market-making (providing liquidity and earning rebates), not taking liquidity.
