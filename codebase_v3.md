# Poly5min01 — Codebase v3

## Project Overview

Automated trading bot for Polymarket's BTC 5-minute Up/Down prediction markets. Connects to Binance via WebSocket for real-time BTC prices, evaluates three time-phased strategies per window, and places orders via the Polymarket CLOB API.

---

## File Structure

```
Poly5min01/
├── bot.py               — Main async trading loop + orchestration
├── strategy_v2.py       — Three strategies + combined orchestrator (active)
├── strategy.py          — Legacy v1 strategy (unused, kept for reference)
├── executor.py          — Polymarket CLOB API wrapper
├── price_feed.py        — Binance WebSocket price feed
├── market_discovery.py  — Window timing + Gamma API market lookup
├── approve.py           — One-time USDC approval script (run once)
├── setup_creds.py       — One-time API credential generator (run once)
├── check_balance.py     — Utility to check CLOB USDC balance
└── requirements.txt     — Python dependencies
```

---

## `bot.py` — Main Trading Loop

**Entry point.** Runs `asyncio.run(main())`. Supports `--dry-run` flag.

### Classes

#### `TelegramHandler(logging.Handler)`
Custom log handler that sends log records to a Telegram chat via background daemon threads. Never blocks the bot loop. Silently drops messages if credentials are missing. Call `flush()` before exit to drain in-flight sends.

- `emit(record)` — spawns a daemon thread per message
- `flush()` — joins all pending threads (timeout 10s each)
- `_send(text)` — POST to `https://api.telegram.org/bot{token}/sendMessage`

#### `WindowState`
Tracks state for one 5-minute window. Reset at each boundary.

| Field | Type | Description |
|---|---|---|
| `window_start` | int | Unix timestamp of window open |
| `slug` | str | Polymarket slug e.g. `btc-updown-5m-{ts}` |
| `window_end` | int | `window_start + 300` |
| `btc_open_price` | float\|None | BTC price at window open |
| `btc_close_price` | float\|None | BTC price ~6s after window close |
| `trades` | list[dict] | All trade records this window |
| `momentum_fired` | bool | Whether momentum strategy fired |
| `scalp_fired` | bool | Whether scalp strategy fired |
| `fade_fired` | bool | Whether fade strategy fired |

Properties: `trade_count`, `committed_capital` (sum of open/pending bet amounts).

#### `TradingBot`
Main orchestrator class.

**`__init__(dry_run=False)`**
- Initializes CLOB client (or skips in dry-run)
- Reads starting bankroll from CLOB or env var
- Creates `CombinedStrategy`, `BinancePriceFeed`

**`run()`** — top-level async loop
1. Starts price feed, waits until ready (30s timeout)
2. Loops `_window_loop()` indefinitely

**`_window_loop()`** — one iteration per 5-min window
1. Gets current window; skips if >240s remain or <3s remain
2. Checks daily loss circuit breaker (25% drawdown → 30min pause)
3. Checks price feed health (stale / no open price → skip)
4. Syncs bankroll from CLOB
5. Initializes `WindowState`, calls `strategy.on_new_window()`
6. Runs `_eval_loop()` (inner 5s tick loop)
7. Runs `_cleanup_window()` (cancel_all sweep)
8. Sleeps until window end + 6s resolution wait
9. Captures BTC close price, calls `_resolve_window()`
10. Re-syncs bankroll, logs summary

**`_eval_loop()`** — inner loop, runs every `EVAL_INTERVAL=5s`
- Breaks when `seconds_remaining < 3` or trade cap hit (`MAX_TRADES_PER_WINDOW=3`)
- Fetches market data via `_fetch_market_safe()`
- Calls `strategy.evaluate_phase()` → dispatches to `_execute_directional()`
- Each strategy flag (`momentum_fired`, `fade_fired`, `scalp_fired`) fires at most once

**`_execute_directional(trade, label)`** — order placement

For `SCALP`:
- Checks ask depth via `get_ask_depth()`
- If asks exist → FAK order; logs partial fills; on failure → GTC maker fallback
- If book empty → GTC maker immediately

For `MOMENTUM` / `FADE`:
- Single FAK order, `price=0` (SDK sweeps best ask)

Records trade dict to `window.trades` and `trade_log` in both live and dry-run.

**`_resolve_window()`** — P&L calculation using BTC prices (not Polymarket settlement)
- `btc_close > btc_open` → winning side = "Up"
- Win: `profit = shares * (1.0 - price)`
- Loss: deducts `bet_amount` from bankroll
- Updates `trade["outcome"]`, `trade["pnl"]`, `trade["bankroll_after"]`

**`_refresh_bankroll()`** — syncs `self.bankroll` with actual CLOB balance; logs drift if >$0.01

**Configuration constants:**
```python
EVAL_INTERVAL = 5           # seconds between ticks
RESOLUTION_WAIT = 6         # seconds after window close before checking outcome
MAX_TRADES_PER_WINDOW = 3   # hard cap per window
DAILY_LOSS_LIMIT_PCT = 0.25 # 25% drawdown triggers 30-min pause
```

---

## `strategy_v2.py` — Three Strategies + Orchestrator

### Strategy A: `EarlyMomentumStrategy` (T-120 to T-90)

Fires a directional taker bet when BTC has moved ≥0.30% from window open.

**Parameters:**
```python
min_delta_pct = 0.30   # minimum BTC % move required
kelly_fraction = 0.25  # quarter-Kelly sizing
min_edge = 0.05        # 5% minimum net edge after fees
max_bet_pct = 0.10     # cap at 10% of bankroll
entry_start = 120      # evaluate from T-120
entry_end = 90         # stop at T-90 (scalp takes over)
```

**Logic:**
1. Check `90 ≤ seconds_remaining ≤ 120`
2. Get `delta = price_feed.get_window_delta()`, require `|delta| ≥ 0.30%`
3. Compute `prob_up` via `_estimate_prob_from_delta()` (z-score → normal CDF)
4. Side: "Up" if delta > 0, else "Down"
5. `net_edge = (prob_win - market_price) - taker_fee`; require `≥ 0.05`
6. Kelly sizing: `f* = (b*p - q) / b`, scaled by `kelly_fraction`
7. Returns trade dict with `strategy: "momentum"`

### Strategy B: `FadeExtremeStrategy` (T-180 to T-30)

Buys the cheap side when one outcome is at extreme odds (>0.85) AND the move is a spike (5s vol ≥ 3× 60s vol).

**Parameters:**
```python
extreme_threshold = 0.85  # market price > this = extreme
max_bet_pct = 0.03        # tiny bets — longshots
spike_vol_ratio = 3.0     # 5s vol must be 3× 60s vol
```

**Logic:**
1. Check `30 ≤ seconds_remaining ≤ 180`
2. Detect extreme: `up_price > 0.85` → fade Down; `down_price > 0.85` → fade Up
3. Compute `vol_5s / vol_60s`; require ratio ≥ 3.0 (confirms spike not drift)
4. Fixed small bet: `min(bankroll * 0.03, $3.00)`; guard: `≤ 10%` of bankroll
5. Returns trade dict with `strategy: "fade"`

### Strategy C: `LateScalpStrategy` (T-90 to T-10)

Directional taker bet when delta is ≥0.15% in the last 90 seconds.

**Parameters:**
```python
min_delta_pct = 0.15       # minimum 0.15% BTC move
kelly_fraction = 0.25      # quarter-Kelly
min_edge = 0.05            # 5% minimum edge
max_bet_pct = 0.10         # cap at 10% of bankroll
entry_window_seconds = 90  # act in last 90s
```

**Logic:** Same as Momentum but triggers in `10 ≤ seconds_remaining ≤ 90`. Returns `strategy: "scalp"`. The executor decides FAK vs GTC based on order book depth.

### `CombinedStrategy` — Orchestrator

Wraps all three strategies. Called by bot on every 5s tick via `evaluate_phase()`.

**Phase dispatch:**
```
T-120 to T-90  → Momentum (fires once)
T-180 to T-90  → Fade (opportunistic, can fire if extreme odds)
T-90 to T-10   → Scalp
```

Returns `(phase_name, trade_dict)` or `("skip", None)`.

### Shared Utility: `_estimate_prob_from_delta(delta, seconds_remaining, vol)`

Converts BTC delta to win probability using z-score + normal CDF:
```
remaining_vol = vol * sqrt(seconds_remaining)
z = delta / remaining_vol
prob_up = Φ(z)  # normal CDF, clamped to [0.10, 0.90]
```

Uses Abramowitz & Stegun polynomial approximation for `Φ(x)` (max error ~1.5e-7).

---

## `executor.py` — Polymarket CLOB API Wrapper

### `init_client() → ClobClient`
Builds `ClobClient` from `.env` vars: `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_API_PASSPHRASE`, `CLOB_HOST`, `POLY_PRIVATE_KEY`, `POLY_SIGNATURE_TYPE`, `POLY_FUNDER_ADDRESS`. Chain ID hardcoded to 137 (Polygon).

### `get_usdc_balance(client) → float`
Fetches CLOB collateral balance. **Important:** does NOT call `update_balance_allowance` first — that overwrites the cache with raw on-chain base units (6 decimals), causing massive misreads (e.g. 0.22 USDC → "216791"). Detects raw format (large integer without ".") and divides by 1e6.

### `place_maker_order(client, token_id, price, size) → resp`
GTC limit order (maker). Uses `OrderArgs(price, size, BUY, token_id)` → `client.create_order()` → `client.post_order(GTC)`. Earns maker rebates.

### `place_market_order(client, token_id, amount, price=0) → resp`
FAK (Fill-and-Kill = IOC) market order. `amount` is in USDC. `price=0` → SDK sweeps best ask. `price>0` → worst-case price cap. Accepts partial fills.

### `get_ask_depth(client, token_id) → list`
Fetches ask side of order book. Returns `book.asks` or `[]` on failure. Used as pre-flight liquidity check before SCALP orders.

### `cancel_order(client, order_id) → resp`
Cancels a resting order. Raises `RuntimeError` if not confirmed in response.

### `cancel_all(client) → resp`
Cancels all open orders. Called as safety sweep at window close.

---

## `price_feed.py` — Binance WebSocket Feed

### `BinancePriceFeed`

Real-time BTC/USDT price via Binance `@aggTrade` WebSocket (10-50 msgs/sec).

**Constants:**
```python
RECONNECT_BASE_DELAY = 1.0   # seconds
RECONNECT_MAX_DELAY = 30.0   # seconds
STALE_THRESHOLD = 10.0       # seconds without update = stale
HISTORY_SIZE = 300           # ~5 min of 1-per-second samples
```

**State:**
- `current_price` — latest BTC price
- `last_update_time` — monotonic timestamp of last update
- `window_open_price` — BTC price at start of current 5-min window
- `price_history` — deque of 1-per-second downsampled prices (max 300)

**Lifecycle:**
- `start()` — creates `_connection_loop` asyncio task
- `stop()` — closes WS, cancels task

**Connection:** `_connection_loop()` outer loop with exponential backoff reconnect. `_listen()` inner loop parses `msg["p"]` from aggTrade messages.

**Window open capture:** `_maybe_capture_window_open()` detects 5-min boundary change (`now % 300`) and sets `window_open_price` to first price in new window.

**Properties:**
- `is_ready` — True after first price received
- `is_stale` — True if no update in 10s
- `is_connected` — True if WS is open

**Price signals:**
- `get_window_delta()` — `(current - open) / open` (fraction, not %)
- `get_momentum(lookback=10)` — price change over last N seconds from history
- `get_volatility(lookback=30)` — rolling std dev of 1-second returns

---

## `market_discovery.py` — Window Timing + Market Lookup

### `get_current_window() → (start, end)`
Returns current 5-min window Unix timestamps: `start = now - (now % 300)`, `end = start + 300`.

### `get_next_window() → (start, end)`
Returns the next 5-min window timestamps.

### `build_slug(window_start) → str`
Returns `f"btc-updown-5m-{window_start}"` — Polymarket market slug format.

### `fetch_market(slug) → dict | None`
Fetches from `https://gamma-api.polymarket.com/events?slug={slug}`.

Returns:
```python
{
  "Up":   {"token_id": str, "price": float},
  "Down": {"token_id": str, "price": float},
  "condition_id": str,
  "active": bool,
  "accepting_orders": bool,
}
```

---

## `strategy.py` — Legacy v1 Strategy (Unused)

### `MispricingStrategy`
Original strategy, superseded by `strategy_v2.py`. Kept for reference.

- `estimate_true_probability(window_delta, momentum, seconds_remaining)` — heuristic model: base 50%, adjusts by delta weight (±8%), applies time boost (<30s: 1.5×, <60s: 1.2×), adds light momentum factor (±2%). Clamps to [0.35, 0.65].
- `calculate_kelly(prob_win, share_price)` — standard binary Kelly: `f* = (b*p - q) / b`
- `calculate_taker_fee(share_price)` — `4 * price * (1-price) * 0.0156`
- `evaluate(market, bankroll, price_feed, seconds_remaining)` — checks both Up and Down sides for edge in `(0.03, 0.08]` range; picks best; uses maker order if net edge after fee < `min_edge`

---

## `approve.py` — One-Time USDC Approval

Run once per wallet to grant unlimited USDC allowance to Polymarket contracts on Polygon.

**Approves both:**
- `CTF_EXCHANGE` (`0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E`)
- `NEG_RISK_ADAPTER` (`0xC5d563A36AE78145C45a50134d48A1215220f80a`)

Uses `USDC.e` (`0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`). Sends `approve(spender, MAX_UINT256)` on-chain via Web3.py. Then calls `clob.update_balance_allowance()` to notify Polymarket backend.

---

## `setup_creds.py` — One-Time Credential Generator

Run once to derive API credentials from private key:
```python
creds = client.create_or_derive_api_creds()
# prints POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE
```

---

## `check_balance.py` — Balance Checker Utility

Standalone script: initializes CLOB client, prints wallet address and CLOB USDC balance. Handles 401 errors with instructions to re-run `setup_creds.py`.

---

## `requirements.txt`

```
py-clob-client==0.34.6
websockets
requests
python-dotenv
web3
boto3
```

---

## Environment Variables (`.env`)

| Variable | Used In | Description |
|---|---|---|
| `POLY_PRIVATE_KEY` | executor, approve, setup_creds | Ethereum private key |
| `POLY_API_KEY` | executor | CLOB API key |
| `POLY_API_SECRET` | executor | CLOB API secret |
| `POLY_API_PASSPHRASE` | executor | CLOB API passphrase |
| `POLY_SIGNATURE_TYPE` | executor, setup_creds | Signature type (0=EOA) |
| `POLY_FUNDER_ADDRESS` | executor, setup_creds | Proxy/funder address |
| `CLOB_HOST` | executor, setup_creds | CLOB endpoint URL |
| `BINANCE_WS` | price_feed | WebSocket URL (default: btcusdt@aggTrade) |
| `TELEGRAM_BOT_TOKEN` | bot | Telegram bot token |
| `TELEGRAM_CHAT_ID` | bot | Telegram chat ID |
| `STARTING_BANKROLL` | bot | Starting capital for dry-run mode |
| `POLYGON_RPC` | approve | Polygon RPC URL |
| `ALCHEMY_API_KEY` | approve | Alchemy API key for Polygon RPC |

---

## Trading Flow (Per Window)

```
Window starts (5-min boundary)
│
├─ T-240: Bot wakes up, syncs bankroll
│
├─ T-180 to T-90: FADE check (every 5s)
│   └─ If one side > 0.85 AND spike vol ratio ≥ 3x → taker FAK order
│
├─ T-120 to T-90: MOMENTUM check (every 5s, fires once)
│   └─ If |BTC delta| ≥ 0.30% AND net_edge ≥ 5% → taker FAK order
│
├─ T-90 to T-10: SCALP check (every 5s, fires once)
│   ├─ If asks in book → FAK order; on failure → GTC maker fallback
│   └─ If book empty → GTC maker immediately
│
├─ T-3: eval loop exits
│
├─ Window close: cancel_all() safety sweep
│
├─ T+6s: capture BTC close price from live feed
│
└─ Resolution: compare BTC open vs close → assign win/loss P&L
```

---

## Key Design Decisions

1. **FAK over FOK** — accepts partial fills; avoids complete rejection on thin liquidity
2. **GTC maker fallback** — when FAK fails (no match), rests a limit order that may still fill
3. **BTC-based resolution** — uses live Binance price instead of Polymarket token settlement (avoids settlement lag false-negatives)
4. **No `update_balance_allowance`** — calling it corrupts the CLOB's cached balance (raw base units vs decimals bug)
5. **Market Making removed** — CLOB instantly matched aggressive bids, making cancel-after-fill unreliable in live mode
6. **Quarter-Kelly sizing** — conservative; capped at 10% of bankroll per trade
7. **Daily loss circuit breaker** — 25% drawdown triggers 30-minute pause
