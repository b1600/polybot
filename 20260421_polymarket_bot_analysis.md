# Polymarket BTC 5-min Up/Down — Bot Trade Analysis

**Log file:** `20260421_0907_bot.log`
**Log window:** 2026-04-18 04:52 → 2026-04-21 09:05 UTC
**Duration:** 76 hours (916 five-minute market windows observed)

---

## Headline Numbers

| Metric | Value |
|---|---|
| Signals fired | 110 (108 SCALP, 2 MOMENTUM, 0 FADE) |
| Orders posted | 45 |
| Orders filled | 14 (**fill rate 31%**) |
| Resolved trades | 14 — **6W / 8L**, win rate 43% |
| Cumulative realised P&L | **−$195.63** |
| Avg win | +$4.52 |
| Avg loss | **−$27.84** (~6× the avg win) |
| Biggest single loss | **−$68.38** (35% of total damage in one trade) |
| Circuit breaker trips | 2 |

---

## Key Finding #1 — FADE never fires, MOMENTUM barely fires

Out of 211 strategy mentions in the log:
- **SCALP**: 108 signal decisions
- **MOMENTUM**: 2 signal decisions
- **FADE**: 0

Effectively this is a single-strategy bot (`scalp_gtc`). Either FADE's conditions never hit, or they hit only during windows where SCALP already claimed the signal.

---

## Key Finding #2 — The −$68.38 trade is a model-vs-market disagreement

On 2026-04-19 20:21, this signal fired:

```
SCALP | Up @ $0.18 (max $0.76) | Δ: +0.219% | Vol: 0.0200% | P(win): 0.77 | Edge: 0.578
```

The bot's own log says **fair value for Up was $0.18** (market pricing Up at 18% probability), but its P(win) model said **77%**. It posted a bid at $0.76 for 68.4 shares, filled almost immediately (someone happy to sell Up at $0.76 when the market thought it was worth $0.18), and lost the full $68.38.

**This is not a fill-quality problem.** The signal model and the observed market price **structurally disagree by 40–60 cents on almost every trade**, and the bot systematically trusts the signal model over the market. The market has been right more often.

---

## Key Finding #3 — Far below breakeven at actual fill prices

Across the 14 resolved trades:

```
Avg fill price:              $0.831
Breakeven win rate required: 83%
Actual win rate:             43%
```

### Model P(win) calibration against reality

| Model said | Actual hit rate |
|---|---|
| P ≥ 0.90 | **3 / 7 (43%)** ← worst bucket, biggest bucket |
| P ≥ 0.79–0.85 | 3 / 3 (100%) |
| P ≤ 0.77 | 0 / 3 (0%) |

**The P(win) = 0.90 bucket is the core problem.** It's the biggest bucket (7 of 14 trades) and hits at 43%, not 90%. P(win) hits 0.90 whenever `Δ ≥ ~0.2%` with low volatility — the model interprets "BTC moved 0.2% in one direction in the first 4 minutes" as near-certain continuation for one more minute. It's not.

---

## Key Finding #4 — The `max_buy` cap leaks edge to zero

Pattern on every SCALP trade:

```
Best ask $0.99 exceeds max $0.89 — posting maker bid @ $0.89
```

When the book has a single $0.99 seller (a placeholder order — logs flag this as `book_anomaly` 5 times but let it through otherwise), the bot posts its *maximum willing bid* as a maker. `max_buy` is set at roughly `P(win) − 0.01`.

**Result:** when filled, the implied probability of the buy price is ~1 cent below the model's P(win) — an edge of ~1.2% on a $1 binary. Any calibration error in P(win) larger than 1% wipes it out. The calibration error on the P=0.90 bucket is **47 percentage points**.

---

## Key Finding #5 — Fill-rate selection bias

69% of posted orders were cancelled unfilled. A fill at the bot's max bid only happens when **someone on the other side actively wants to sell at that price**. In a thin CLOB this is an adversely-selected fill: counterparties who have good reason to believe the resolution will go the other way. This partly explains why the filled group underperforms the signal population.

---

## Trade Ledger

| # | Timestamp (UTC) | Result | Side | P&L | Cumulative |
|---|---|---|---|---|---|
| 1 | 2026-04-19 20:33 | LOSS | Up | −$68.38 | −$68.38 |
| 2 | 2026-04-19 22:48 | LOSS | Down | −$31.70 | −$100.08 |
| 3 | 2026-04-20 05:13 | WIN | Down | +$6.20 | −$93.88 |
| 4 | 2026-04-20 05:23 | WIN | Down | +$4.72 | −$89.16 |
| 5 | 2026-04-20 05:28 | WIN | Down | +$6.12 | −$83.04 |
| 6 | 2026-04-20 05:33 | LOSS | Down | −$25.90 | −$108.94 |
| 7 | 2026-04-20 14:43 | WIN | Down | +$3.88 | −$105.06 |
| 8 | 2026-04-20 19:33 | LOSS | Up | −$19.79 | −$124.85 |
| 9 | 2026-04-20 20:43 | LOSS | Down | −$32.49 | −$157.34 |
| 10 | 2026-04-20 21:13 | WIN | Down | +$3.45 | −$153.89 |
| 11 | 2026-04-20 21:23 | LOSS | Down | −$22.30 | −$176.19 |
| 12 | 2026-04-21 01:18 | WIN | Up | +$2.76 | −$173.43 |
| 13 | 2026-04-21 03:13 | LOSS | Down | −$14.50 | −$187.93 |
| 14 | 2026-04-21 03:33 | LOSS | Up | −$7.70 | −$195.63 |

---

## Day-by-Day P&L

| Date | Signals | Fills | Resolved | W/L | P&L |
|---|---|---|---|---|---|
| 2026-04-18 | 8 | 0 | 0 | — | $0.00 |
| 2026-04-19 | 16 | 2 | 2 | 0/2 | **−$100.08** ← both big losses, circuit breaker #1 |
| 2026-04-20 | 64 | 9 | 9 | 5/4 | −$76.11 ← circuit breaker #2 |
| 2026-04-21 | 22 | 3 | 3 | 1/2 | −$19.44 |

April 19 accounts for 51% of total losses from just 2 trades. April 20 won more trades than it lost (5W/4L), but asymmetric payoffs (avg win $4.52, avg loss $27.84) still produced a net loss.

---

## Circuit Breaker Trips

1. **2026-04-19 20:29:57** — lost $88.44 / $138.83 limit, triggered right after the −$68.38 Up trade. Paused 30 min.
2. **2026-04-20 21:15:10** — lost $90.74 / $138.83, triggered after consecutive −$32.49 and −$22.30 Down losses. Paused 30 min.

Both times the breaker did its job. Without it, the next trade would likely have been another P=0.90 bucket signal and continued the bleed.

---

## P&L by Direction

| Side | Trades | W/L | P&L |
|---|---|---|---|
| Up | 4 | 1/3 | −$93.11 |
| Down | 10 | 5/5 | −$102.52 |

Up side lost more per trade (fewer, bigger losses including the −$68.38); Down side was more balanced but still negative overall.

---

## Priority Fixes

1. **Recalibrate the P(win) = 0.90 rule.** It's the dominant signal and wrong more than half the time. Either the `Δ` threshold needs to be much larger, or volatility needs higher weight, or the 5-minute continuation assumption is fundamentally wrong for BTC and should be replaced with logistic regression fit on historical windows.

2. **Resolve the fair-value / P(win) contradiction.** When the log says `Up @ $0.18 | P(win): 0.77`, one of those two numbers is lying. If $0.18 is a reliable market midprice, trust it and pass. If it's a stale last-trade price, stop labeling it as "fair".

3. **Widen the `book_anomaly` filter.** The 5 skipped signals were correctly identified — but every other filled trade also had `Best ask $0.99`, which is the same pathology. The current filter only trips on a *single* ask at $0.99. Extend it: if book depth inside the model's fair-value range is below $X, skip.

4. **Audit MOMENTUM and FADE.** Firing 2 and 0 times over 916 windows means either the thresholds are unreachable or they're shadowed by SCALP's evaluation order.

5. **Revisit bet sizing.** Quarter-Kelly from a miscalibrated P(win) is just "lose smaller than full Kelly". If P(win)=0.90 really means 0.43, even fractional Kelly is destructive. Fix calibration before tuning the Kelly fraction.

---

## Open Question to Investigate Next

Do the 45 unfilled posted orders show a different fair-value / max_buy gap pattern than the 14 filled ones? If the filled group has systematically wider gaps, that confirms the adverse-selection hypothesis and argues for tightening `max_buy` below `P(win) − 0.01`.
