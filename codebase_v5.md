# Codebase Summary — v5
_Generated: 2026-03-31_

---

## Architecture Overview

Polymarket BTC 5-minute prediction market trading bot. Places directional bets on whether BTC will be Up or Down in each 5-minute window.

```
bot.py (main loop + orchestration)
  ├── strategy_v2.py   (3 strategies: Momentum, Fade, Scalp)
  ├── price_feed.py    (Binance aggTrade WebSocket)
  ├── executor.py      (Polymarket CLOB order placement)
  └── market_discovery.py (Polymarket Gamma API)

setup_creds.py    (one-time: generate API keys)
approve.py        (one-time: approve USDC on Polygon)
check_balance.py  (utility: print CLOB balance)
```

---

## `bot.py` — Main Trading Loop

**Entry point.** `asyncio.run(main())` with optional `--dry-run` flag.

### Key Constants
| Name | Value | Purpose |
|------|-------|---------|
| `EVAL_INTERVAL` | 5s | tick rate inside a window |
| `RESOLUTION_WAIT` | 6s | wait after window close before checking BTC close |
| `MAX_TRADES_PER_WINDOW` | 3 | hard cap: one per strategy per window |
| `DAILY_LOSS_LIMIT_PCT` | 25% | circuit breaker; pauses 30 min if hit |

### `TelegramHandler`
Custom `logging.Handler`. Sends every log line to Telegram via daemon threads. `flush()` waits for in-flight sends before exit.

### `WindowState`
Tracks one 5-min window: `window_start`, `slug`, `window_end`, `btc_open_price`, `btc_close_price`, list of `trades`, and three fired-flags (`momentum_fired`, `scalp_fired`, `fade_fired`).

### `TradingBot`
Core class.

**`run()`** — starts price feed, then loops `_window_loop()` forever.

**`_window_loop()`** — one iteration per window:
1. Wait until T-240 (4 min remaining)
2. Check daily loss circuit breaker
3. Check price feed health
4. `_refresh_bankroll()` — sync from CLOB
5. Init `WindowState`, call `strategy.on_new_window()`
6. `_eval_loop()` — 5s ticks, calls strategies
7. `_cleanup_window()` — `cancel_all()` safety sweep
8. Wait for resolution (`window_end + RESOLUTION_WAIT`)
9. Capture BTC close price, `_resolve_window()`
10. `_refresh_bankroll()` again, log summary

**`_eval_loop()`** — inner loop:
- Fetches market data every 5s
- Calls `strategy.evaluate_phase()` → returns `("momentum"|"fade"|"scalp"|"skip", trade_dict)`
- Routes to `_execute_directional()` or `_execute_scalp()`
- Each strategy fires at most once per window (fired-flags)

**`_execute_directional(trade, label)`** — places FAK market order for MOMENTUM and FADE strategies.

**`_execute_scalp(trade)`** — single-shot scalp:
1. `get_ask_depth()` — skip if no asks
2. `place_ioc_order()` capped at `max_price` (prob_win − min_edge)
3. Logs fill size; records order_id=None if no fill

**`_resolve_window()`** — P&L from BTC price direction (not Polymarket settlement):
- `btc_close > btc_open` → winning_side = "Up"
- Win: `profit = shares × (1.0 − price)`; Loss: `bankroll -= bet_amount`
- Skips cancelled/unplaced orders

**`_refresh_bankroll()`** — calls `get_usdc_balance()`, logs drift if >$0.01.

---

## `strategy_v2.py` — Three Strategies

### Strategy A: `EarlyMomentumStrategy` (T-120 to T-90)
Directional taker bet when BTC has moved ≥0.30% from window open.

**Parameters:** `min_delta_pct=0.30%`, `kelly_fraction=0.25`, `min_edge=0.05`, `max_bet_pct=10%`

**Logic:**
1. Active window: `90 ≤ seconds_remaining ≤ 120`
2. Compute `delta_pct` from price feed
3. Estimate `prob_win` via `_estimate_prob_from_delta()` (z-score → normal CDF)
4. Compute `net_edge = edge − taker_fee`; skip if < `min_edge`
5. Size via quarter-Kelly, capped at 10% bankroll

### Strategy B: `FadeExtremeStrategy` (T-180 to T-30)
Buys the cheap side when market price is extreme (>0.85) AND it looks like a spike.

**Parameters:** `extreme_threshold=0.85`, `max_bet_pct=3%` (max $3), `spike_vol_ratio=3.0x`

**Logic:**
1. Active window: `30 ≤ seconds_remaining ≤ 180`
2. Check if either side >0.85
3. Confirm spike: `vol_5s / vol_60s ≥ 3.0`
4. Fixed small bet (not Kelly) — longshot play

### Strategy C: `LateScalpStrategy` (T-150 to T-10)
Single-shot IOC taker bet before market makers reprice.

**Parameters:** `min_delta_pct=0.15%`, `kelly_fraction=0.25`, `min_edge=0.05`, `high_edge_threshold=0.10`, `max_bet_pct=10%`, `entry_window_seconds=150`

**Logic:**
1. Active window: `10 ≤ seconds_remaining ≤ 150`
2. Same delta + prob_win model as Momentum
3. `max_price = prob_win − min_edge` (+ 2% buffer if edge ≥ 10%)
4. Returns `max_price` in trade dict; bot uses it as IOC price cap

### `CombinedStrategy` (Orchestrator)
Wraps all three. `evaluate_phase()` is called on every 5s tick:
- T-90 to T-120 → try Momentum (once per window)
- T-90 to T-180 → try Fade (opportunistic)
- T-10 to T-150 → try Scalp

`on_new_window()` resets `_momentum_fired` flag.

### `_estimate_prob_from_delta(delta, seconds_remaining, volatility)`
Shared utility. Normalises delta by remaining volatility (`vol × √T`) → z-score → `_normal_cdf(z)`. Clamped to [0.10, 0.90].

### `_normal_cdf(x)`
Abramowitz & Stegun approximation, max error ~1.5e−7.

---

## `price_feed.py` — `BinancePriceFeed`

Real-time BTC/USDT via Binance `@aggTrade` WebSocket (10–50 msg/s).

**Key state:**
- `current_price` — latest trade price
- `window_open_price` — auto-captured at each 5-min boundary
- `price_history` — deque of 300 downsampled 1-per-second prices

**Connection:** `_connection_loop()` with exponential backoff (1s → 30s max).

**Readiness:**
- `is_ready` — True once first price received
- `is_stale` — True if no update in 10s
- `wait_until_ready(timeout=30)` — async blocking wait

**Price signals:**
| Method | Description |
|--------|-------------|
| `get_window_delta()` | `(current − open) / open` |
| `get_momentum(lookback=10)` | price change over last N seconds |
| `get_volatility(lookback=30)` | rolling std-dev of 1s returns |

---

## `executor.py` — CLOB Order Placement

Wraps `py_clob_client`. All orders are BUY-side (betting on Up or Down token).

| Function | Description |
|----------|-------------|
| `init_client()` | Build `ClobClient` from `.env` creds |
| `get_usdc_balance(client)` | Reads CLOB COLLATERAL balance; handles raw base-unit bug (divides by 1e6 if no decimal and >1000) |
| `place_maker_order(client, token_id, price, size)` | GTC limit order (unused in current strategies) |
| `place_market_order(client, token_id, amount, price=0)` | FAK market order for Momentum/Fade |
| `place_ioc_order(client, token_id, amount, price=0)` | IOC (FAK internally) for Scalp with price cap |
| `get_ask_depth(client, token_id)` | Returns ask-side order book list |
| `cancel_order(client, order_id)` | Cancel single order |
| `cancel_all(client)` | Cancel all open orders |
| `get_order_status(client, order_id)` | Fetch order details |

**Important note on `get_usdc_balance`:** Do NOT call `update_balance_allowance` before this — it overwrites the CLOB cached balance with raw on-chain base units (6 decimals), causing ~$216K misread. Detection: if value >1000 and no decimal point → divide by 1e6.

---

## `market_discovery.py` — Polymarket Gamma API

| Function | Description |
|----------|-------------|
| `get_current_window()` | Returns `(window_start, window_end)` for now |
| `get_next_window()` | Returns next 5-min window |
| `build_slug(window_start)` | `"btc-updown-5m-{window_start}"` |
| `fetch_market(slug)` | Fetches event from Gamma API, returns `{"Up": {token_id, price}, "Down": {...}, condition_id, active, accepting_orders}` |

---

## `strategy.py` — Legacy Strategy (unused)

Original `MispricingStrategy` — single strategy without the three-phase structure. Uses window delta + momentum + time-decay to estimate P(Up). Not called by `bot.py` (replaced by `strategy_v2.py`).

---

## Utility Scripts

### `setup_creds.py`
Run once. Creates/derives API credentials from private key, prints `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_API_PASSPHRASE` to add to `.env`.

### `approve.py`
Run once (EOA wallets only). Approves unlimited USDC.e spending for `CTF_EXCHANGE` and `NEG_RISK_ADAPTER` on Polygon mainnet via `web3.py`. Then calls `update_balance_allowance` to sync Polymarket backend.

### `check_balance.py`
Quick utility. Prints wallet address, CLOB trading balance, proxy address.

---

## Environment Variables (`.env.example`)

```
# Wallet
POLY_PRIVATE_KEY        # 0x-prefixed EOA private key
POLY_FUNDER_ADDRESS     # Polymarket proxy wallet address
POLY_SIGNATURE_TYPE     # 0=EOA, 1=Email/Magic

# API (from setup_creds.py)
POLY_API_KEY
POLY_API_SECRET
POLY_API_PASSPHRASE

# Strategy
STARTING_BANKROLL       # used in dry-run mode
KELLY_FRACTION          # legacy (strategy.py)
MIN_EDGE_THRESHOLD      # legacy (strategy.py)
MAX_EDGE_THRESHOLD      # legacy (strategy.py)
MIN_BET                 # legacy (strategy.py)

# Notifications
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID

# Endpoints
CLOB_HOST               # https://clob.polymarket.com
BINANCE_WS              # wss://stream.binance.com:9443/ws/btcusdt@aggTrade

# AWS S3 (trade log backup)
AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY
AWS_REGION
S3_BUCKET

# Alchemy (for approve.py)
ALCHEMY_API_KEY
```

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

## Trade Flow Summary

```
Window T-300 → T-240   Bot waits (too early)
Window T-240 → T-180   Eval loop starts; Fade may fire (spike detection)
Window T-180 → T-120   Fade window (T-180 to T-90)
Window T-120 → T-90    Momentum evaluates (if BTC moved ≥0.30%)
Window T-150 → T-10    Scalp evaluates on every tick (IOC order)
Window T-0             cleanup: cancel_all()
T+6s                   BTC close price captured
Resolution             Compare BTC open vs close → update P&L
```

Each strategy fires **at most once per window**. Max 3 trades per window total.
