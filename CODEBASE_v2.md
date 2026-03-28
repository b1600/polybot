# CODEBASE_v2.md — Polymarket BTC 5-min Trading Bot

## Architecture Overview

```
bot.py              ← Main loop orchestrator
├── strategy_v2.py  ← Three trading strategies + combined orchestrator
├── executor.py     ← Polymarket CLOB API wrapper (orders, balance)
├── price_feed.py   ← Binance WebSocket real-time BTC price feed
└── market_discovery.py  ← Polymarket Gamma API market lookup

approve.py          ← One-time USDC approval script (on-chain)
setup_creds.py      ← One-time API credential generator
strategy.py         ← Legacy v1 strategy (superseded by strategy_v2.py)
```

---

## `bot.py` — Main Trading Loop

**Entry point:** `asyncio.run(main())`
**Run modes:** `python bot.py` (live) or `python bot.py --dry-run`

### Key Constants
| Constant | Value | Purpose |
|---|---|---|
| `EVAL_INTERVAL` | 5s | Strategy evaluation frequency within a window |
| `RESOLUTION_WAIT` | 6s | Wait after window close before checking outcome |
| `MAX_TRADES_PER_WINDOW` | 3 | Hard cap per window |
| `DAILY_LOSS_LIMIT_PCT` | 25% | Pause if down 25% from session start |

### Classes

**`TelegramHandler`** (logging.Handler)
- Sends log records to a Telegram chat via `POST /sendMessage`
- Runs each send in a daemon thread (non-blocking)
- `flush()` waits for all in-flight sends before exit
- Credentials from env: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`

**`WindowState`**
- Tracks state within one 5-minute trading window
- Stores: `window_start`, `slug`, `window_end`, `btc_open_price`, `btc_close_price`, `trades[]`
- Flags: `momentum_fired`, `scalp_fired`, `fade_fired` (each strategy fires at most once)
- Properties: `trade_count`, `committed_capital` (sum of open/pending bets)

**`TradingBot`**
- Owns: CLOB client, bankroll, strategy, price feed, trade log
- `run()` → starts price feed → calls `_window_loop()` repeatedly
- `_window_loop()`: waits for T-240, checks daily loss limit + price feed health, initialises `WindowState`, runs `_eval_loop()`, cleans up, resolves
- `_eval_loop()`: ticks every 5s, calls `strategy.evaluate_phase()`, routes to `_execute_directional()` based on phase
- `_execute_directional(trade, label)`: executes a FAK order with up to 2 retries (SCALP only), pre-checks order book depth before first attempt
- `_resolve_window()`: determines win/loss by comparing `btc_open_price` vs `btc_close_price` (avoids Polymarket settlement lag)
- `_refresh_bankroll()`: syncs `self.bankroll` with actual CLOB balance; logs drift
- `_is_daily_loss_limit_hit()`: drawdown check vs session start
- `save_log(path)`: writes `trade_log` to JSON
- `print_session_stats()`: logs win rate, P&L, ROI at end of session

### Window Lifecycle
```
T-240: enter window loop
T-180: fade strategy activates
T-120: momentum strategy activates
 T-30: momentum/fade deactivate, scalp activates
  T-3: eval loop exits
  T-0: window ends
 T+6: resolve using BTC close price
```

---

## `strategy_v2.py` — Trading Strategies

### Strategy A: `EarlyMomentumStrategy` (T-120 to T-30)
- **Signal:** BTC window delta ≥ 0.3%
- **Model:** z-score = delta / (vol × √T) → normal CDF → P(win), clamped [0.10, 0.90]
- **Sizing:** Quarter-Kelly, max 10% of bankroll, min $1
- **Fee model:** `taker_fee = 4 × price × (1 - price) × 0.0156`
- **Min net edge:** 5% after fees
- **Returns:** `strategy="momentum"`, `use_maker=False`

### Strategy B: `FadeExtremeStrategy` (T-30 to T-180)
- **Signal:** One side priced > 0.85 AND 5-second vol ≥ 3× 60-second vol (spike filter)
- **Bet direction:** Opposite (cheap) side
- **Sizing:** Fixed small bet: min(bankroll × 3%, $3.00), hard cap at 10% of bankroll
- **Rationale:** Spike likely to mean-revert; buys cheap side at longshot odds
- **Returns:** `strategy="fade"`, `use_maker=False`

### Strategy C: `LateScalpStrategy` (last 30s, configurable `entry_window_seconds=60`)
- **Signal:** BTC window delta ≥ 0.15% with ≤30s remaining
- **Model:** Same z-score / normal CDF as Momentum
- **Sizing:** Quarter-Kelly, max 10% of bankroll, min $1
- **Min net edge:** 5% after fees
- **Returns:** `strategy="scalp"`, `use_maker=False`

### `CombinedStrategy` (orchestrator)
- `on_new_window()`: resets `_momentum_fired` flag
- `evaluate_phase(market, bankroll, price_feed, seconds_remaining)` returns:
  - `("momentum", trade)` — T-30 to T-120
  - `("fade", trade)` — T-30 to T-180
  - `("scalp", trade)` — ≤T-30
  - `("skip", None)` — no signal

### Shared Utility
- `_estimate_prob_from_delta(delta_pct, seconds_remaining, volatility)`: z-score → Φ(z), clamped [0.10, 0.90]
- `_normal_cdf(x)`: Abramowitz & Stegun approximation, max error ~1.5e-7

### Why Market Making Was Removed
Polymarket CLOB fills aggressive maker bids instantly as taker orders; cancel-after-fill logic from dry-run isn't replicable live.

---

## `executor.py` — CLOB API Wrapper

### Functions

**`init_client() → ClobClient`**
- Builds `ClobClient` from env vars: `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_API_PASSPHRASE`, `CLOB_HOST`, `POLY_PRIVATE_KEY`, `POLY_SIGNATURE_TYPE`, `POLY_FUNDER_ADDRESS`
- Chain ID: 137 (Polygon)

**`get_usdc_balance(client) → float`**
- Calls `get_balance_allowance(AssetType.COLLATERAL)`
- Detects raw base-unit format (integer > 1000 with no decimal) and divides by 1e6
- **Do NOT** call `update_balance_allowance` before this — it corrupts the cached balance

**`place_market_order(client, token_id, amount, price=0) → dict`**
- Places FAK (Fill-and-Kill = IOC) order; accepts partial fills
- `price=0`: SDK auto-selects from order book (sweeps best ask)
- `price>0`: worst-case price cap for retry walk-up
- `amount` is in USDC

**`place_maker_order(client, token_id, price, size) → dict`**
- Places GTC limit order; earns rebates instead of paying taker fees

**`get_ask_depth(client, token_id) → list`**
- Fetches ask side of order book; returns `[]` if empty or unavailable
- Used to pre-check liquidity before SCALP to avoid wasted round-trips

**`cancel_order(client, order_id) → dict`**
- Cancels a resting maker order; raises if not confirmed

**`cancel_all(client) → dict`**
- Cancels all open orders (called at end of every window as safety sweep)

---

## `price_feed.py` — Binance WebSocket Price Feed

### `BinancePriceFeed`

**Source:** Binance `@aggTrade` stream — 10-50 messages/second (sub-second resolution)
**Default URL:** `wss://stream.binance.com:9443/ws/btcusdt@aggTrade` (overridable via `BINANCE_WS` env)

**State:**
- `current_price`: latest trade price
- `window_open_price`: first price of current 5-min window (auto-captured)
- `price_history`: deque of 1-per-second downsampled prices (max 300 = 5 min)

**Auto-reconnect:** exponential backoff 1s → 30s max on any disconnect

**Properties:**
- `is_ready`: True after first price received
- `is_stale`: True if no update in 10s
- `is_connected`: True if WebSocket is open

**Methods:**
- `start()` / `stop()`: lifecycle
- `wait_until_ready(timeout=30)`: blocks until first price or raises `TimeoutError`
- `get_window_delta() → float`: `(current - open) / open`, returns 0.0 if unavailable
- `get_momentum(lookback=10) → float`: price change over last N seconds
- `get_volatility(lookback=30) → float`: rolling std dev of 1s returns

---

## `market_discovery.py` — Polymarket Market Lookup

**API:** Gamma API at `https://gamma-api.polymarket.com`

**Functions:**
- `get_current_window() → (start, end)`: current 5-min boundary from Unix time
- `get_next_window() → (start, end)`: next 5-min window
- `build_slug(window_start) → str`: e.g. `"btc-updown-5m-1711641600"`
- `fetch_market(slug) → dict | None`: fetches event data, returns:
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

## `approve.py` — One-Time USDC Approval (run once)

- Approves `CTF_EXCHANGE` and `NEG_RISK_ADAPTER` to spend unlimited USDC.e on Polygon
- Uses `USDC_ADDRESS = 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` (bridged USDC.e)
- Signs and broadcasts two `approve(spender, MAX_UINT)` txs; waits for receipts
- Then calls `clob.update_balance_allowance()` to sync Polymarket's backend cache
- Requires env: `POLY_PRIVATE_KEY`, `POLYGON_RPC` or `ALCHEMY_API_KEY`
- Gas price: 500 gwei (hardcoded)

---

## `setup_creds.py` — One-Time Credential Generation (run once)

- Creates/derives API key, secret, and passphrase from the private key
- Prints values to add to `.env`
- Requires env: `CLOB_HOST`, `POLY_PRIVATE_KEY`, `POLY_SIGNATURE_TYPE`, `POLY_FUNDER_ADDRESS`

---

## `strategy.py` — Legacy v1 Strategy (superseded)

`MispricingStrategy` — single-strategy evaluator:
- `estimate_true_probability(window_delta, momentum, seconds_remaining)`: linear delta weight + time boost + momentum; clamped [0.35, 0.65]
- `calculate_kelly(prob_win, share_price)`: half-Kelly by default
- `calculate_taker_fee(share_price)`: `4 × p × (1-p) × 0.0156`
- `evaluate(market, bankroll, price_feed, seconds_remaining)`: checks both sides for mispricing in `(min_edge=0.03, max_edge=0.08]`; returns best candidate or None
- `use_maker=True` when net edge after taker fee < `min_edge`

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `POLY_PRIVATE_KEY` | Yes | Ethereum private key |
| `POLY_API_KEY` | Yes (live) | Polymarket CLOB API key |
| `POLY_API_SECRET` | Yes (live) | API secret |
| `POLY_API_PASSPHRASE` | Yes (live) | API passphrase |
| `CLOB_HOST` | Yes | CLOB API endpoint |
| `POLY_SIGNATURE_TYPE` | No | 0 (default) or 1 (Gnosis Safe) |
| `POLY_FUNDER_ADDRESS` | No | Gnosis Safe funder address |
| `ALCHEMY_API_KEY` | No | For Polygon RPC in approve.py |
| `POLYGON_RPC` | No | Custom Polygon RPC URL |
| `BINANCE_WS` | No | Custom Binance WebSocket URL |
| `STARTING_BANKROLL` | No | Dry-run initial bankroll (default: 100) |
| `TELEGRAM_BOT_TOKEN` | No | Telegram bot token for log alerts |
| `TELEGRAM_CHAT_ID` | No | Telegram chat ID for log alerts |

---

## Data Flow

```
Binance WS ──aggTrade──► BinancePriceFeed
                              │
                    get_window_delta()
                    get_volatility()
                              │
                              ▼
Gamma API ──fetch_market──► market dict
                              │
                              ▼
                    CombinedStrategy.evaluate_phase()
                              │
              ┌───────────────┼────────────────┐
         momentum           fade            scalp
              │               │               │
              └───────────────▼───────────────┘
                    TradingBot._execute_directional()
                              │
                    executor.place_market_order()
                              │
                    Polymarket CLOB API
```

---

## Order Execution Logic (SCALP)

1. Pre-check ask depth — skip if empty (avoid wasted round-trips)
2. Attempt 0: FAK @ base price
3. On `"no match"`: wait 1s, retry with price + $0.01
4. Attempt 1: FAK @ base + $0.01
5. On `"no match"`: wait 1s, retry
6. Attempt 2: FAK @ base + $0.02
7. After 3 attempts, log error and return

Momentum and Fade use single FAK attempt with `price=0` (auto-book).

---

## Resolution Logic

- `btc_open_price`: captured by `BinancePriceFeed` at window boundary
- `btc_close_price`: sampled from live feed ~6s after window end
- `btc_close > btc_open` → "Up" wins; `btc_close < btc_open` → "Down" wins
- Avoids Polymarket token settlement lag / "Resolution unclear" errors
- Win P&L: `shares × (1.0 - price)`; Loss P&L: `-bet_amount`
