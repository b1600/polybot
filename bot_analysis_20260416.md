# Polymarket BTC 5-Min Bot — Analysis (16 April 2026)

## Headline

Over ~8.5 hours of runtime and **100 five-minute windows**, the bot placed **0 successful trades**. Every signal was blocked at the execution layer. Bankroll unchanged at **$163.76**.

## Sessions

- **Session 1:** 04:50–04:55 UTC (clean shutdown, 1 window, 0 trades)
- **Session 2:** 05:08–13:10+ UTC (still running at log end, 99 windows, 0 trades)

## Signals Generated (8 total — all SCALP)

| # | Time (UTC) | Direction | Price | Edge | P(win) | Bet |
|---|---|---|---|---|---|---|
| 1 | 05:19 | Down | $0.54 | 34.9% | 0.90 | $16.37 |
| 2 | 05:39 | Up   | $0.60 | 27.7% | 0.90 | $16.40 |
| 3 | 05:58 | Down | $0.49 | 38.9% | 0.90 | $16.38 |
| 4 | 06:49 | Up   | $0.53 | 35.9% | 0.90 | $16.38 |
| 5 | 08:08 | Down | $0.49 | 38.9% | 0.90 | $16.38 |
| 6 | 09:48 | Down | $0.54 | 34.9% | 0.90 | $16.37 |
| 7 | 11:08 | Up   | $0.54 | 34.9% | 0.90 | $16.37 |
| 8 | 11:48 | Down | $0.49 | 38.8% | 0.90 | $16.38 |

No MOMENTUM or FADE signals fired. Bet sizing ~$16.38 = quarter-Kelly, 10% of bankroll (configured cap).

## Core Failure Mode

Every signal hit the **same two-stage wall**:

1. **IOC/FAK fails** — `"no orders found to match with FAK order"`. No marketable asks at target price.
2. **GTC fallback refuses** — 7 of 8 cases: best ask was **$0.99** (exceeds `max_price` of $0.89). 1 case: **no asks in book at all**.

**Diagnosis:** At T-240 (4 min before window close), the market is already 80%+ decided. Liquidity providers have pulled all reasonable asks, leaving only a lone $0.99 "catch-a-falling-knife" quote. The bot correctly refuses it, but has no alternative.

## Key Observations

- **4% price buffer can't fix a structural book-shape problem.** The lone $0.99 ask is not a liquidity gap — it's a post-resolution order book.
- **"Book has 1 ask level" log is misleading.** Bot proceeds as if tradeable; that single level is always the $0.99 outlier.
- **6 FAK orders wasted** — signed, submitted, killed. Each burns nonce, signature overhead, and rate-limit budget with zero fill chance.
- **P(win) = 0.90 on every signal regardless of Δ** (range: -0.15% to -0.35%) → win-probability model is a threshold classifier, not calibrated.
- **Redemption/bankroll bugs untested today** — no fills means no exposure to known `redeemPositions` or `WindowState` desync issues.

## Recommended Fixes (Priority Order)

1. **Pre-sign ask-price gate.** Check `best_ask ≤ max_price` before signing the FAK order (book is already fetched at that point). Would have saved 6 wasted submissions today.
2. **Shift entry earlier or widen buffer.** T-240 is too late for SCALP. Try T-270 or T-300 when passive makers still quote.
3. **Ask-wall detector.** If only ask is at price > Nx second-best-bid, log `book_anomaly` and skip.
4. **Calibrate P(win)** as a function of |Δ|, volatility, and time-to-close. Hardcoded 0.90 is the smoking gun.
5. **Per-signal cooldown.** After a failed order, pause 30s before re-evaluating to cut log spam and API load.

## Bottom Line

Strategy layer is finding opportunities — 8 clean signals with positive theoretical edge. Execution layer is rejecting all of them for correct-but-unhelpful reasons. **Today's problem isn't a bad bot; it's a bot being asked to trade against a non-existent order book.** Signals #3, #5, and #8 (Δ -0.17% to -0.21%, Down @ $0.49) look like exactly the trades you want it taking once the entry gate and timing are fixed.
