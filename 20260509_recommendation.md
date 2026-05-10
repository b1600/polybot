# Polybot Strategy Recommendation — 2026-05-09

## Executive Summary

The bot is currently losing 100% of resolved trades (0 wins / 14 losses, -$195.63 net P&L).
The P(win) model has no predictive power and the book_anomaly filter is blocking all new
trade attempts. The bot should be paused until the issues below are addressed.

---

## Findings

### 1. Bot has stopped trading entirely (Apr 23–24)

Every scalp attempt is blocked by the book_anomaly check:
```
SCALP | book_anomaly — best ask $0.99 ≥ $0.95, likely post-resolution book — skipping
```
Zero trades placed across the last two sessions. Bankroll frozen at $146.94.

**Root cause:** The Gamma API `outcomePrices` (used to calculate edge) is cached and lags
the live CLOB by minutes. By the time the scalp fires at T-240 to T-120, the CLOB has
already repriced to near-resolution levels ($0.99 asks). The book_anomaly guard correctly
catches this but the underlying data source problem means it fires every time.

### 2. P(win) model is systematically wrong

Observed calibration across 14 resolved trades:

| P(win) Predicted | Trades | Actual Wins | Expected |
|---|---|---|---|
| 0.90 | 3 | 0 | ~2.7 |
| 0.80 | 4 | 0 | ~3.2 |
| 0.72–0.77 | 2 | 0 | ~1.5 |
| **All** | **14** | **0** | **~7** |

The probability of 0 wins from 14 trades at P(win)=0.80 is ~1-in-600,000.
This is systematic failure, not variance.

**Root cause:** The model (`_estimate_prob_from_delta` in `strategy_v2.py`) uses a BTC
momentum z-score (Normal CDF of delta/remaining_vol). By T-240 to T-120, the market has
already fully priced in the BTC move. Buying the direction of a move that already happened
and is already reflected in the CLOB price produces a systematically losing strategy.
GTC maker fills compound this — a GTC bid at $0.79 only fills when the market moves
further in that direction, meaning you get filled right before an adverse reversal.

### 3. GTC fill rate is low even when trades execute (Apr 18–20)

When the bot did place orders, only ~25% of GTC maker bids filled (5 of ~20 orders).
Most orders sat for the full 2.5-minute lifetime and expired unfilled.

### 4. API reliability issues (Apr 20)

- **HTTP 404 flood:** After a market expires, the bot hammers the dead token with order
  book fetches every 15 seconds for ~30 minutes with no circuit breaker.
- **HTTP 425 cascade:** `service not ready` errors on all cancel/order calls for ~3 hours
  with no backoff or alerting. Bot kept retrying silently.

### 5. Redundant API calls

- Two consecutive `DELETE /cancel-all` calls fire within 1–2 seconds on every window
  transition (race condition between cleanup paths).
- Two identical `GET balance-allowance` calls fire milliseconds apart at the 3-minute
  mark each window (likely a race condition between async timers).

---

## Recommendations

### Priority 1 — Fix the data source (blocker)

Replace `outcomePrices` from the Gamma REST API with live CLOB mid prices.
The strategy edge calculation must use the same price source as the book_anomaly check.

```python
# In _fetch_market_safe(): after fetching token IDs from Gamma,
# fetch live mid from CLOB for each token instead of using outcomePrices
book = get_book(client, token_id)
live_mid = (float(book.bids[0].price) + float(book.asks[0].price)) / 2
```

If the live CLOB mid and Gamma price diverge by more than 5%, skip the window entirely
rather than computing false edge.

### Priority 2 — Pause live trading and calibrate P(win) from DuckDB

The 1.2B-row Polymarket trade history in `./db/polybot.duckdb` contains the ground truth
needed to validate (or replace) the current probability model.

**Step 1:** Reconstruct actual market outcomes from final trade prices:
```sql
CREATE TABLE market_outcomes AS
WITH last_trade AS (
    SELECT market_id, price, nonusdc_side,
           ROW_NUMBER() OVER (PARTITION BY market_id ORDER BY timestamp DESC) AS rn
    FROM trades
    WHERE price > 0.95 OR price < 0.05
)
SELECT market_id,
       CASE WHEN price > 0.95 THEN nonusdc_side
            ELSE (CASE nonusdc_side WHEN 'UP' THEN 'DOWN' ELSE 'UP' END)
       END AS winning_side
FROM last_trade WHERE rn = 1;
```

**Step 2:** Build a calibration table — empirical win rate by (price bucket, time bucket):
```sql
CREATE TABLE calibration AS
SELECT
    ROUND(price / 0.05) * 0.05       AS price_bucket,
    (seconds_remaining // 30) * 30   AS seconds_remaining_bucket,
    COUNT(*)                          AS trade_count,
    AVG(CASE WHEN taker_direction = winning_side THEN 1.0 ELSE 0.0 END) AS actual_win_rate
FROM window_trades t JOIN market_outcomes o ON t.market_id = o.market_id
WHERE seconds_remaining BETWEEN 10 AND 290
GROUP BY 1, 2 HAVING COUNT(*) >= 100;
```

**Step 3:** Identify where actual win rate diverges from market price by >5% with
statistical significance. That gap is the only real edge available.

**Step 4:** Replace `_estimate_prob_from_delta()` with a lookup into the calibration table.
Fall back to market price (no trade) if a bucket has insufficient history.

**Expected outcomes from this analysis:**
- If actual_win_rate ≈ market_price across all buckets → market is efficient, no edge exists; stop trading
- If actual_win_rate < market_price at high prices (>0.65) late in window → mean-reversion edge: fade favorites
- If actual_win_rate > market_price at moderate prices near T-60 → market underreacts late; current momentum direction is valid but entry timing is wrong

### Priority 3 — Fix API resilience

- Add exponential backoff (2s → 4s → 8s → 32s) on 404 and 425 errors with a max retry
  count. After 5 consecutive failures on the same token, mark it dead and stop fetching.
- Send a Telegram alert when 425 errors persist beyond 10 minutes.
- Fix the double `cancel-all` race condition — ensure only one cleanup path calls it per
  window transition.
- Fix the duplicate balance-allowance polling — consolidate into a single scheduled refresh.

### Priority 4 — Reconsider GTC maker strategy

GTC maker bids have an adverse selection problem: they only fill when the market moves
against the bid price direction. Consider:
- IOC taker orders exclusively (immediate fill or cancel) to avoid the selection bias
- Tighter GTC lifetime (30–60s instead of 2.5 minutes) to reduce adverse selection window
- Only use GTC if the calibration analysis shows a specific time-window edge that requires
  resting orders

---

## Suggested Next Steps (in order)

1. Run the DuckDB calibration queries — this takes hours to compute but costs nothing
2. Examine the calibration output to determine if any edge exists at all
3. If edge exists: rebuild P(win) as a calibration table lookup and backtest on held-out data
4. If no edge found: stop trading this market; the 5-min BTC CLOB is too efficient
5. Fix the Gamma API / live CLOB price divergence issue regardless of outcome
6. Fix the 404/425 retry logic and duplicate API calls
7. Re-enable trading only after (3) or (4) is resolved with backtest validation

---

## Current Bot State

| Metric | Value |
|---|---|
| Bankroll | $146.94 (unchanged since Apr 21) |
| Last trade executed | Apr 20, 2026 |
| Trades placed (all-time) | ~20 |
| Trades filled | 5 |
| Wins | 0 |
| Net P&L | -$195.63 |
| Bot status | Running but 100% blocked by book_anomaly |
