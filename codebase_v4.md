# CODEBASE SUMMARY: Poly5min01 (v4)

## PROJECT OVERVIEW

**Poly5min01** is an automated trading bot for Polymarket's BTC 5-minute Up/Down binary prediction markets. It connects to Binance for real-time BTC prices via WebSocket, evaluates three time-phased trading strategies within each 5-minute window, and executes orders through the Polymarket CLOB (Batch Clearance Order Book) API.

---

## FILE STRUCTURE

```
Poly5min01/
├── bot.py                    — Main async trading loop + orchestration (713 lines)
├── strategy_v2.py            — Three strategies + orchestrator (429 lines) [ACTIVE]
├── strategy.py               — Legacy v1 strategy (162 lines) [REFERENCE ONLY]
├── executor.py               — Polymarket CLOB API wrapper (125 lines)
├── price_feed.py             — Binance WebSocket price feed (232 lines)
├── market_discovery.py       — Window timing + Gamma API lookup (63 lines)
├── approve.py                — One-time USDC approval (66 lines)
├── setup_creds.py            — One-time credential generator (21 lines)
├── check_balance.py          — Balance checker utility (42 lines)
├── requirements.txt          — Dependencies
└── trade_log.json            — Persistent trade history
```

---

## bot.py — Main Trading Loop & Orchestration

**Entry Point:** `asyncio.run(main())` with optional `--dry-run` flag

### Configuration Constants
```python
EVAL_INTERVAL = 5              # seconds between strategy evaluation ticks
RESOLUTION_WAIT = 6            # seconds after window close before outcome check
MAX_TRADES_PER_WINDOW = 3      # hard cap (one of each strategy max)
DAILY_LOSS_LIMIT_PCT = 0.25    # 25% drawdown triggers 30-min pause
```

### `TelegramHandler(logging.Handler)`
Custom async logging handler for Telegram notifications. Sends messages via background daemon threads to avoid blocking the bot loop.
- `emit(record)` — spawns daemon thread per log message
- `flush()` — joins all pending threads (10s timeout each) before exit
- `_send(text)` — POST to Telegram API, silently drops on failure

### `WindowState`
Tracks all activity within a single 5-minute trading window (reset at each boundary).
- Fields: `window_start`, `window_end`, `slug`, `btc_open_price`, `btc_close_price`, `trades[]`
- Flags: `momentum_fired`, `scalp_fired`, `fade_fired` (fire at most once per window)
- Properties: `trade_count`, `committed_capital` (sum of open/pending bet amounts)

### `TradingBot`
Main orchestrator class handling the full trading lifecycle.

**`__init__(dry_run=False)`**
- Initializes CLOB client (or skips in dry-run mode)
- Loads starting bankroll from CLOB balance or `STARTING_BANKROLL` env var
- Creates `CombinedStrategy()` and `BinancePriceFeed()`
- Initializes trade log and session statistics (wins, losses, total_windows)

**`async run()`** — Top-level event loop
1. Starts price feed, waits until ready (30s timeout)
2. Loops `_window_loop()` indefinitely
3. Catches KeyboardInterrupt/CancelledError for graceful shutdown

**`async _window_loop()`** — One iteration per 5-minute window
1. Calculates current window; skips if >240s remaining or <3s remaining
2. Checks daily loss circuit breaker (DAILY_LOSS_LIMIT_PCT = 0.25 → 30-min pause)
3. Validates price feed health (is_stale check, is_ready check)
4. Syncs bankroll from CLOB via `_refresh_bankroll()`
5. Initializes WindowState, resets strategy via `strategy.on_new_window()`
6. Increments total_windows counter
7. Runs `_eval_loop()` (inner 5s tick loop)
8. Runs `_cleanup_window()` (cancel_all safety sweep)
9. Sleeps until window end + RESOLUTION_WAIT=6s
10. Captures BTC close price from live feed
11. Calls `_resolve_window()` to determine P&L
12. Re-syncs bankroll to catch fills missed by model

**`async _eval_loop()`** — Inner loop, runs every EVAL_INTERVAL=5s
- Breaks when seconds_remaining < 3 or trade_count >= MAX_TRADES_PER_WINDOW (3)
- Fetches fresh market data via `_fetch_market_safe()`
- Calculates available bankroll (total - committed_capital in open orders)
- Calls `strategy.evaluate_phase()` → returns (phase_name, trade_dict)
- Dispatches to `_execute_directional()` or `_execute_scalp()` based on phase
- Uses flags to fire each strategy at most once per window
- Sleeps min(EVAL_INTERVAL, max(1, seconds_remaining - 3)) before next tick

**`async _execute_directional(trade, label)`** — For MOMENTUM and FADE
1. Logs trade details (side, price, edge, bet amount, shares)
2. Places FAK market order if not dry-run (price=0 sweeps best ask)
3. Records trade to window.trades and trade_log; status set to "pending"

**`async _execute_scalp(trade)`** — Single-shot FAK taker
1. Pre-flight book depth check via `get_ask_depth()`
2. If no asks → skip (illiquid market)
3. If asks exist → place FAK order at max_price cap (prob_win - min_edge)
4. Logs size_matched, order_id; records trade

**`async _cleanup_window()`** — Safety sweep at window close
- Calls `cancel_all()` if not dry-run to ensure no orders persist past window

**`async _resolve_window()`** — P&L calculation (BTC-based, not Polymarket settlement)
- Compares btc_open_price vs btc_close_price to determine winning side
- Win: `profit = shares * (1.0 - price)`, adds to bankroll, increments wins counter
- Loss: deducts `bet_amount` from bankroll, increments losses counter
- Updates `trade["outcome"]`, `trade["pnl"]`, `trade["status"]="resolved"`, `trade["bankroll_after"]`

**`async _refresh_bankroll()`** — Syncs self.bankroll with actual CLOB balance
- Skipped in dry-run mode; logs drift if |actual - model| > $0.01

**`_fetch_market_safe()`** — Fetches market data with error handling
- Returns None on error or if market not accepting orders

**`_is_daily_loss_limit_hit()`** — Circuit breaker check
- Returns True if drawdown >= DAILY_LOSS_LIMIT_PCT (0.25 = 25%)

**`save_log(path="trade_log.json")`** — Persists full trade log to disk as JSON

**`print_session_stats()`** — End-of-session summary with win_rate, net_pnl, ROI

---

## strategy_v2.py — Three Time-Phased Strategies [ACTIVE]

Three independent strategies fire at different phases of each 5-minute window. Orchestrated by `CombinedStrategy` class.

### Strategy A: `EarlyMomentumStrategy` (T-120 to T-90)

Fires a directional taker bet when BTC has moved ≥0.30% from window open.

**Parameters:**
```python
min_delta_pct = 0.30          # minimum 0.30% BTC move required
kelly_fraction = 0.25         # quarter-Kelly sizing (conservative)
min_edge = 0.05               # 5% minimum net edge after taker fees
min_bet = 1.0
max_bet_pct = 0.10            # cap at 10% of bankroll
entry_start = 120             # evaluate from T-120
entry_end = 90                # stop at T-90
```

**Logic:**
1. Checks if `90 ≤ seconds_remaining ≤ 120`
2. Gets `delta = price_feed.get_window_delta()`, requires `|delta| ≥ 0.30%`
3. Computes volatility over lookback=30 seconds
4. Calls `_estimate_prob_from_delta(delta, seconds_remaining, vol)` to estimate P(Up) via z-score + normal CDF
5. Determines side: "Up" if delta > 0, else "Down"
6. Calculates net_edge = (prob_win - market_price) - taker_fee, requires ≥ 0.05
7. Applies Kelly sizing: `f* = (b*p - q) / b` where `b = (1-price)/price`
8. Scales by kelly_fraction, caps at max_bet_pct, enforces min_bet

**Return Dict:**
```python
{
  "side": "Up" or "Down", "token_id": str, "price": float,
  "bet_amount": float, "shares": float, "edge": float,
  "kelly_pct": float, "estimated_prob": float,
  "use_maker": False, "strategy": "momentum"
}
```

### Strategy B: `FadeExtremeStrategy` (T-180 to T-30)

Buys the cheap side when market odds are extreme (>0.85) AND the move is a spike (not drift).

**Parameters:**
```python
extreme_threshold = 0.85      # market price > this = extreme
max_bet_pct = 0.03            # tiny bets (longshots)
min_bet = 1.0
spike_vol_ratio = 3.0         # 5s vol must be 3× 60s vol to confirm spike
```

**Logic:**
1. Checks if `30 ≤ seconds_remaining ≤ 180`
2. Detects extreme: if `up_price > 0.85` → fade "Down"; if `down_price > 0.85` → fade "Up"
3. Computes `vol_5s / vol_60s` ratio, requires ratio ≥ 3.0 (confirms spike, not drift)
4. Fixed small bet: `min(bankroll * 0.03, $3.00)`

### Strategy C: `LateScalpStrategy` (T-150 to T-10)

Directional taker bet when delta is ≥0.15% in the last 150 seconds.

**Parameters:**
```python
min_delta_pct = 0.15          # minimum 0.15% BTC move
kelly_fraction = 0.25         # quarter-Kelly
min_edge = 0.05               # 5% minimum edge
min_bet = 1.0
max_bet_pct = 0.10            # cap at 10% of bankroll
entry_window_seconds = 150    # act in last 150s (T-150 to T-10)
```

**Logic:** Same as Momentum but triggers in `10 ≤ seconds_remaining ≤ 150`. Key addition: `max_price` cap at `prob_win - min_edge` guarantees positive EV.

**Additional Field in Return Dict:**
```python
"max_price": round(min(prob_win - self.min_edge, 0.95), 2)
```

### `CombinedStrategy` Orchestrator

Wraps all three strategies. Called by bot on every 5s tick.

**Methods:**
- `__init__(dry_run=True)` — initializes all three strategy instances
- `on_new_window()` — resets `_momentum_fired` flag for new window
- `evaluate_phase(market, bankroll, price_feed, seconds_remaining)` — main dispatch method

**Phase Dispatch Logic:**
```
T-120 to T-90  → Momentum (fires once if conditions met)
T-180 to T-90  → Fade (fires opportunistically)
T-150 to T-10  → Scalp (fires once if conditions met)
```

Returns: `("momentum"|"fade"|"scalp"|"skip", trade_dict|None)`

### Shared Utility: `_estimate_prob_from_delta(delta_pct, seconds_remaining, volatility)`

Estimates P(Up wins) using statistical modeling:
1. `remaining_vol = vol * sqrt(seconds_remaining)`
2. `z = delta_pct / remaining_vol`
3. `prob_up = Φ(z)` (normal CDF via Abramowitz & Stegun polynomial approximation)
4. Clamps to [0.10, 0.90]

**Key Insight:** As time remaining decreases, the volatility term shrinks, making delta a stronger signal.

---

## executor.py — Polymarket CLOB API Wrapper

### `init_client() → ClobClient`
Builds ClobClient from env vars: `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_API_PASSPHRASE`, `CLOB_HOST`, `POLY_PRIVATE_KEY`, `POLY_SIGNATURE_TYPE`, `POLY_FUNDER_ADDRESS`. Chain ID hardcoded to 137 (Polygon mainnet).

### `get_usdc_balance(client) → float`
Fetches CLOB collateral balance in USDC.

**CRITICAL:** Does NOT call `update_balance_allowance()` first. That endpoint overwrites cached balance with raw on-chain base units (6 decimals), causing 1,000,000× misreads (0.22 USDC → 216791 raw units → reads as $216,791).

**Detection Logic:** If value > 1000 AND "." not in string → raw base units → divide by 1,000,000.

### `place_maker_order(client, token_id, price, size) → resp`
Places a GTC (Good-Till-Cancelled) limit maker order. Earns maker rebates.

### `place_market_order(client, token_id, amount, price=0) → resp`
Places a FAK (Fill-and-Kill = IOC) market order.
- `price=0` → SDK sweeps best ask
- `price>0` → acts as worst-case price cap
- Accepts partial fills

### `get_ask_depth(client, token_id) → list`
Fetches ask side of order book. Returns `book.asks` or `[]`. Used as pre-flight liquidity check before SCALP orders.

### `cancel_order(client, order_id) → resp`
Cancels a resting maker order. Raises RuntimeError if order_id not in response's "canceled" list.

### `cancel_all(client) → resp`
Cancels all open orders. Called as safety sweep at window close.

### `get_order_status(client, order_id) → dict`
Fetches current order info (status, size_matched, original_size, etc.)

---

## price_feed.py — Binance WebSocket Price Feed

### `BinancePriceFeed` Class

**Constants:**
```python
RECONNECT_BASE_DELAY = 1.0      # seconds
RECONNECT_MAX_DELAY = 30.0      # seconds
STALE_THRESHOLD = 10.0          # seconds without update = stale
HISTORY_SIZE = 300              # ~5 min of 1-per-second samples
```

**State:**
- `current_price` — latest BTC price (float or None)
- `last_update_time` — monotonic timestamp of last update
- `window_open_price` — BTC price at start of current 5-min window
- `price_history` — deque[float], max 300 entries, 1-per-second downsampled
- `_ws` — WebSocket connection
- `_reconnect_delay` — exponential backoff state

**Lifecycle:**
- `async start()` — creates and starts `_connection_loop()` task
- `async stop()` — sets `_running=False`, closes WS, cancels task

**Connection Management:**
- `async _connection_loop()` — outer loop with exponential backoff reconnect (base 1s, max 30s). Uses ping/pong (Binance requires 20s ping interval).
- `async _listen(ws)` — inner loop. Parses `msg["p"]` (price string). Downsamples to 1-per-second. Calls `_maybe_capture_window_open()`.
- `_maybe_capture_window_open(price)` — detects 5-min boundary (`now % 300`), auto-captures open price for new windows.

**Readiness & Health:**
- `is_ready` (property) — True once first price received
- `is_stale` (property) — True if no update in STALE_THRESHOLD (10s)
- `is_connected` (property) — True if WS is open
- `async wait_until_ready(timeout=30.0)` — blocks until ready or raises TimeoutError

**Price Signal Methods:**
- `get_window_delta() → float` — `(current - open) / open` (fraction)
- `get_momentum(lookback=10) → float` — price change over last N seconds from downsampled history
- `get_volatility(lookback=30) → float` — rolling std dev of 1-second log returns

**Design Notes:** Uses `@aggTrade` (not `@kline_1m`) for trade-level updates → 10-50 msgs/sec vs 1/minute. Sub-second price resolution enables accurate delta calculations within 5-min windows.

---

## market_discovery.py — Window Timing & Market Lookup

### `get_current_window() → (start, end)`
```python
start = now - (now % 300)      # round down to nearest 5-min boundary
end = start + 300
```

### `get_next_window() → (start, end)`
Returns next 5-minute window timestamps.

### `build_slug(window_start) → str`
```python
f"btc-updown-5m-{window_start}"
```
Example: `"btc-updown-5m-1712345200"`

### `fetch_market(slug) → dict | None`
Fetches market data from Gamma API: `GET https://gamma-api.polymarket.com/events?slug={slug}`

**Returns:**
```python
{
  "Up":   {"token_id": str, "price": float},
  "Down": {"token_id": str, "price": float},
  "condition_id": str,
  "active": bool,
  "accepting_orders": bool,
}
```

Returns None on HTTP error, empty events list, or network error.

---

## strategy.py — Legacy v1 Strategy [REFERENCE ONLY]

### `MispricingStrategy` Class

**Parameters:**
```python
kelly_fraction = 0.5            # half-Kelly
min_edge = 0.03                 # 3% minimum edge
max_edge = 0.08                 # 8% maximum edge (above = likely data error)
min_bet = 1.0
```

**Methods:**
- `estimate_true_probability(window_delta, momentum, seconds_remaining) → float`
  - Heuristic-based: base 0.50 + delta weight + time boost + momentum shift; clamped to [0.35, 0.65]
- `calculate_kelly(prob_win, share_price) → float` — binary Kelly formula
- `calculate_taker_fee(share_price) → float` — `4 * price * (1-price) * 0.0156`
- `evaluate(market, bankroll, price_feed, seconds_remaining) → dict | None`
  - Checks both sides for mispricing in (0.03, 0.08] range
  - Recommends maker order if net_edge < min_edge

**Why Superseded:** Simpler heuristic probability model; fixed edge thresholds less responsive to volatility than v2's z-score approach.

---

## approve.py — One-Time USDC Approval

Run once per wallet to grant unlimited USDC allowance to Polymarket smart contracts on Polygon.

**Smart Contract Addresses:**
- USDC.e: `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` (Bridged USDC)
- CTF_EXCHANGE: `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E`
- NEG_RISK_ADAPTER: `0xC5d563A36AE78145C45a50134d48A1215220f80a`

Signs and sends MAX_UINT256 approve() transactions at 500 gwei gas, then calls `clob.update_balance_allowance()` to notify Polymarket backend.

---

## setup_creds.py — One-Time Credential Generator

Calls `client.create_or_derive_api_creds()` and prints the three API credentials. User adds them to `.env`.

---

## check_balance.py — Balance Checker Utility

Prints wallet address and CLOB USDC balance. Handles 401 errors with a message directing to `setup_creds.py`.

---

## ENVIRONMENT VARIABLES (.env)

| Variable | Module | Description |
|----------|--------|-------------|
| `POLY_PRIVATE_KEY` | executor, approve, setup_creds | Ethereum private key (0x-prefixed) |
| `POLY_API_KEY` | executor | CLOB API key |
| `POLY_API_SECRET` | executor | CLOB API secret |
| `POLY_API_PASSPHRASE` | executor | CLOB API passphrase |
| `POLY_SIGNATURE_TYPE` | executor | 0=EOA, 1=Email/Magic wallet |
| `POLY_FUNDER_ADDRESS` | executor | Proxy/funder address (0x-prefixed) |
| `CLOB_HOST` | executor | CLOB endpoint URL |
| `BINANCE_WS` | price_feed | Binance WebSocket URL |
| `TELEGRAM_BOT_TOKEN` | bot | Telegram bot token |
| `TELEGRAM_CHAT_ID` | bot | Telegram chat ID |
| `STARTING_BANKROLL` | bot | Initial capital for dry-run mode |
| `POLYGON_RPC` | approve | Polygon mainnet RPC URL |
| `ALCHEMY_API_KEY` | approve | Alchemy API key |

---

## TRADING FLOW (Per 5-Minute Window)

```
Window Start (5-min boundary, Unix timestamp % 300 = 0)
│
├─ T-240: Bot becomes active
│
├─ T-180 to T-90: FADE STRATEGY (every 5s)
│   ├─ Check if market odds extreme (>0.85)
│   ├─ Verify spike: 5s vol ≥ 3× 60s vol
│   └─ If both → FAK taker on cheap side
│
├─ T-120 to T-90: MOMENTUM STRATEGY (fires once)
│   ├─ Check if |BTC delta| ≥ 0.30%
│   ├─ Estimate P(win) from z-score
│   ├─ net_edge ≥ 5% after taker fee?
│   └─ If yes → FAK taker directionally
│
├─ T-150 to T-10: SCALP STRATEGY (fires once)
│   ├─ Check if |BTC delta| ≥ 0.15%
│   ├─ Estimate P(win) from z-score
│   ├─ Check order book depth
│   └─ If asks exist → FAK at max_price cap
│
├─ T-3: Inner eval loop exits
├─ T-0: Window close → cancel_all() safety sweep
├─ T+6s: Capture BTC close price from live feed
│
└─ Resolution:
   ├─ Compare BTC open vs close
   ├─ Determine winning side
   ├─ Win: profit = shares * (1.0 - price)
   ├─ Loss: loss = bet_amount
   └─ Update bankroll, log results, save trade_log.json
```

---

## KEY DESIGN DECISIONS

1. **FAK over FOK** — Accepts partial fills. Better when liquidity is thin. Avoids complete rejection on thick spreads.

2. **BTC-based resolution (not Polymarket settlement)** — Uses Binance BTC price. Avoids settlement lag causing false-negative "unclear" resolutions.

3. **No `update_balance_allowance` calls** — That endpoint corrupts CLOB's cached balance (overwrites with raw on-chain base units). Causes 1,000,000× misreads.

4. **Market Making removed** — In live mode, CLOB instantly matched aggressive maker bids with no cancel option ("matched orders can't be canceled").

5. **Quarter-Kelly sizing** — 0.25× Kelly, capped at 10% of bankroll per trade. Protects against variance and volatility edge underestimation.

6. **Daily loss circuit breaker** — 25% drawdown triggers 30-minute trading pause. Prevents death spiral in losing streaks.

7. **Three strategies, three windows** — Time-phased approach captures different market micro-structure: early mispricing (momentum), spike reversions (fade), late liquidity exhaustion (scalp).

8. **Offline backfill resolution** — Trade log accurate even if Polymarket settlement API lags or fails.

---

## trade_log.json Structure (per trade)

```json
{
  "timestamp": "2024-04-01T12:34:56.789Z",
  "window": 1712000400,
  "slug": "btc-updown-5m-1712000400",
  "order_id": "0x...",
  "status": "resolved",
  "side": "Up",
  "strategy": "momentum",
  "price": 0.58,
  "bet_amount": 10.00,
  "shares": 17.2,
  "edge": 0.0523,
  "kelly_pct": 0.1234,
  "estimated_prob": 0.6400,
  "token_id": "0x...",
  "outcome": "win",
  "pnl": 7.24,
  "bankroll_before": 100.00,
  "bankroll_after": 107.24
}
```

---

## Dependencies (requirements.txt)

```
py-clob-client==0.34.6    # Official Polymarket Python SDK
websockets                # WebSocket client for Binance
requests                  # HTTP client for Gamma API
python-dotenv             # .env file loading
web3                      # Ethereum/Polygon interactions
boto3                     # AWS SDK (unused, kept for future use)
```
