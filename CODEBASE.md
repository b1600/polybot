# Poly5min01 — Codebase Summary

Polymarket BTC 5-minute prediction market trading bot. Connects to Binance for real-time BTC price and Polymarket's CLOB API to place taker orders when it detects edge.

---

## Architecture Overview

```
bot.py (main loop)
  ├── price_feed.py     — Binance WebSocket, BTC price signals
  ├── strategy_v2.py    — Three strategies: Momentum, Fade, Scalp
  ├── market_discovery.py — Polymarket Gamma API, market/token IDs
  └── executor.py       — Polymarket CLOB client, order placement

setup_creds.py          — One-time: generate API credentials
approve.py              — One-time: approve USDC on-chain (Polygon)
strategy.py             — (Legacy) original mispricing strategy
```

---

## Files

### `bot.py` — Main Trading Loop

Entry point. Runs an async loop that tracks 5-minute windows, evaluates strategies, executes orders, and resolves outcomes.

**Key classes:**

**`TelegramHandler`** — logging handler that sends log records to a Telegram chat via background threads. Never blocks the bot loop.

**`WindowState`** — tracks activity within one 5-minute window:
- `btc_open_price` / `btc_close_price` — for direct resolution
- `trades` — list of orders placed
- `momentum_fired`, `scalp_fired`, `fade_fired` — one-shot flags per window

**`TradingBot`**:
- `run()` — starts price feed, enters main loop
- `_window_loop()` — one iteration per window: waits for T-240, refreshes bankroll, runs eval loop, cleanup, resolution
- `_eval_loop()` — polls every 5s; calls `strategy.evaluate_phase()` and routes to `_execute_directional()`
- `_execute_directional(trade, label)` — places order; for SCALP retries up to 2× on `no match`
- `_resolve_window()` — determines win/loss by comparing BTC open vs close prices (avoids Polymarket settlement lag)
- `_refresh_bankroll()` — syncs bankroll from CLOB before and after each window

**Configuration constants:**
| Constant | Value | Description |
|---|---|---|
| `EVAL_INTERVAL` | 5s | Tick rate inside eval loop |
| `RESOLUTION_WAIT` | 6s | Wait after window close before reading close price |
| `MAX_TRADES_PER_WINDOW` | 3 | Hard cap per window |
| `DAILY_LOSS_LIMIT_PCT` | 25% | Circuit breaker |

---

### `strategy_v2.py` — Trading Strategies

Three strategies, each fires at most once per window.

#### Strategy A: `EarlyMomentumStrategy` (T-120 to T-30)
Directional taker bet when BTC moves >0.3% from window open early in the window.

- Uses volatility-normalised z-score → normal CDF to estimate P(win)
- Quarter-Kelly sizing, max 10% bankroll
- Minimum 5% net edge after taker fee
- Parameters: `min_delta_pct=0.30%`, `entry_start=120s`, `entry_end=30s`

#### Strategy B: `FadeExtremeStrategy` (T-180 to T-30)
Buy the cheap side when market odds are extreme (>0.85) AND the move looks like a spike.

- Spike detection: `vol_5s / vol_60s > 3.0×`
- Tiny fixed bet (3% bankroll max) — these are longshots
- Logic: Buy Down @ $0.08 → 12.5:1 payout; only needs ~10% reversion rate

#### Strategy C: `LateScalpStrategy` (last 30s)
Directional taker when delta >0.15% and very little time remains for reversal.

- Quarter-Kelly, max 10% bankroll, minimum $1.00
- Uses same z-score probability model as Momentum
- Always taker (FOK) — speed matters

#### `CombinedStrategy` — Orchestrator
Calls `evaluate_phase()` on every 5s tick. Priority order:
1. Momentum (T-30 to T-120)
2. Fade (T-30 to T-180)
3. Scalp (≤T-30)

**Shared utility:**

`_estimate_prob_from_delta(delta, seconds_remaining, volatility)`:
- Normalises delta by remaining expected volatility: `z = delta / (vol * sqrt(T))`
- Converts z-score to probability via normal CDF (Abramowitz & Stegun approximation)
- Clamped to [0.10, 0.90]

---

### `price_feed.py` — Binance WebSocket Price Feed

`BinancePriceFeed` — real-time BTC/USDT via Binance `@aggTrade` stream (10-50 msg/sec).

**Features:**
- Auto-reconnect with exponential backoff (1s → 30s cap)
- Readiness gating via `wait_until_ready(timeout=30)`
- Stale detection: `is_stale` → True if no update in 10s
- Downsampled 1-per-second price history (300-entry deque, ~5min)
- Auto-captures `window_open_price` at every 5-min UTC boundary

**Price signals:**
| Method | Description |
|---|---|
| `get_window_delta()` | `(current - open) / open` — fractional price change from window open |
| `get_momentum(lookback=10)` | Price change over last N seconds from 1s history |
| `get_volatility(lookback=30)` | Rolling std dev of 1s returns |

---

### `executor.py` — CLOB Order Execution

Wraps `py_clob_client` for Polymarket's Central Limit Order Book.

| Function | Description |
|---|---|
| `init_client()` | Initialises `ClobClient` from `.env` credentials |
| `get_usdc_balance(client)` | Reads USDC balance; detects and converts raw base-unit format (divide by 1e6) |
| `place_market_order(client, token_id, amount)` | FOK market order; pays taker fee, guarantees immediate execution |
| `place_maker_order(client, token_id, price, size)` | GTC limit order; earns maker rebate (not actively used) |
| `cancel_order(client, order_id)` | Cancel a single resting order |
| `cancel_all(client)` | Cancel all open orders (called at every window close) |

**Balance bug note:** Calling `update_balance_allowance` corrupts the CLOB balance cache with raw on-chain base units (e.g. $8.41 → `8411564`). `get_usdc_balance` detects `value > 1000` with no decimal point and divides by 1e6.

---

### `market_discovery.py` — Market Lookup

Fetches Polymarket market data from the Gamma API.

| Function | Description |
|---|---|
| `get_current_window()` | Returns `(window_start, window_end)` for the current 5-min window |
| `get_next_window()` | Returns timestamps for the next window |
| `build_slug(window_start)` | Builds slug: `btc-updown-5m-{window_start}` |
| `fetch_market(slug)` | Fetches Up/Down token IDs and current prices from Gamma API |

Returns structure:
```python
{
  "Up":   {"token_id": "...", "price": 0.52},
  "Down": {"token_id": "...", "price": 0.48},
  "condition_id": "...",
  "active": True,
  "accepting_orders": True,
}
```

---

### `approve.py` — One-time USDC Approval (run once)

Approves `MAX_UINT` USDC spending for Polymarket contracts on Polygon:
- `CTF_EXCHANGE`: `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E`
- `NEG_RISK_ADAPTER`: `0xC5d563A36AE78145C45a50134d48A1215220f80a`
- Token: Bridged USDC.e — `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`

Sends two approval txs on Polygon (500 gwei gas), waits for receipts, then syncs Polymarket backend via `update_balance_allowance`.

---

### `setup_creds.py` — One-time Credential Generation (run once)

Derives Polymarket CLOB API credentials from the private key and prints them for the `.env` file.

```
POLY_API_KEY=...
POLY_API_SECRET=...
POLY_API_PASSPHRASE=...
```

---

### `strategy.py` — Legacy Strategy (unused)

Original `MispricingStrategy` — estimates P(Up) from window delta + momentum, uses Kelly criterion, checks both sides for mispricing. Superseded by `strategy_v2.py`.

---

## Environment Variables (`.env`)

| Variable | Description |
|---|---|
| `POLY_PRIVATE_KEY` | Ethereum private key for signing orders |
| `POLY_API_KEY` | Polymarket CLOB API key |
| `POLY_API_SECRET` | Polymarket CLOB API secret |
| `POLY_API_PASSPHRASE` | Polymarket CLOB API passphrase |
| `POLY_SIGNATURE_TYPE` | Signature type (`0` = EOA) |
| `POLY_FUNDER_ADDRESS` | Funder wallet address |
| `CLOB_HOST` | Polymarket CLOB host URL |
| `ALCHEMY_API_KEY` | Polygon RPC via Alchemy |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token for log alerts |
| `TELEGRAM_CHAT_ID` | Telegram chat ID |
| `STARTING_BANKROLL` | Bankroll for dry-run mode (default: 100) |
| `BINANCE_WS` | Binance WS URL (default: btcusdt@aggTrade) |

---

## Dependencies (`requirements.txt`)

```
py-clob-client==0.34.6
websockets
requests
python-dotenv
web3
boto3
```

---

## Running the Bot

```bash
# Setup (run once)
python setup_creds.py    # generate API creds → add to .env
python approve.py        # approve USDC on-chain

# Live trading
python bot.py

# Dry run (simulated, no real orders)
python bot.py --dry-run
```

---

## Window Timing

Each 5-minute window = 300 seconds. The bot uses countdown from window end:

| Time Remaining | Phase | Action |
|---|---|---|
| >240s | Too early | Sleep until T-240 |
| T-180 to T-30 | Fade window | `FadeExtremeStrategy` evaluates |
| T-120 to T-30 | Momentum window | `EarlyMomentumStrategy` evaluates |
| T-30 to T-3 | Scalp window | `LateScalpStrategy` evaluates |
| <3s | Window closing | Break eval loop |
| +6s after close | Resolution | Compare BTC open vs close → win/loss |
