# strategy_v2.py — Polymarket BTC 5-min strategy (MM removed)
#
# STRATEGIES:
# ─────────────────────────────────────────────────────────
# A) Early Momentum (T-120 to T-30):
#    Directional taker bet when BTC has moved >0.3% from the
#    window open. Uses volatility-normalised z-score to estimate
#    P(win) and quarter-Kelly sizing. Fires at most once per window.
#
# B) Fade Extreme Odds (T-180 to T-30):
#    Buy the cheap side when market price is extreme (>0.85) AND
#    the move looks like a spike (5s vol >> 60s vol). Taker order.
#
# C) Mid-Window Scalp (T-240 to T-10):
#    Single-shot IOC taker bet after checking order book depth.
#    Enters at T-240 before counter-parties reprice away from the current ask.
#    Caps the fill price at prob_win - min_edge to preserve positive EV.
#    Skips if the book has no asks (illiquid market).
#
# WHY MM WAS REMOVED:
# ─────────────────────────────────────────────────────────
# In live (non dry-run) mode, Polymarket's CLOB filled maker bids
# instantly as taker orders, spending USDC with no way to cancel
# ("matched orders can't be canceled"). The cancel-after-fill
# approach that looked profitable in dry-run is not replicable
# live because the CLOB immediately matches aggressive bids.
# ─────────────────────────────────────────────────────────

import logging

log = logging.getLogger("strategy_v2")

MAX_BUY_PRICE = 0.80  # Rec 2: skip entries above this price — adverse payoff math


# ═══════════════════════════════════════════════════════════
# STRATEGY A: Early Momentum (replaces Market Making)
# ═══════════════════════════════════════════════════════════
#
# When BTC moves strongly early in the window (T-120 to T-30),
# the Polymarket price often lags. We take a taker order in the
# direction of the move once delta clears a 0.3% threshold.
# The probability model normalises the delta by remaining
# volatility (z-score → normal CDF), so it automatically
# becomes less aggressive when there is still a lot of time left.

class EarlyMomentumStrategy:
    """
    Directional taker bet when delta is large and time is T-120 to T-30.
    """

    def __init__(
        self,
        min_delta_pct: float = 0.30,      # need ≥0.30% BTC move
        kelly_fraction: float = 0.125,     # eighth-Kelly (Rec 4B: halved from quarter)
        min_edge: float = 0.05,            # need 5% net edge after fees
        min_bet: float = 2.50,             # GTC min: 5 shares × $0.50 = $2.50
        min_shares: int = 5,               # Polymarket CLOB GTC minimum
        max_bet_pct: float = 0.10,         # max 10% of bankroll
        entry_start: int = 120,            # start evaluating at T-120
        entry_end: int = 90,               # stop at T-90 (scalp takes over)
    ):
        self.min_delta_pct = min_delta_pct
        self.kelly_fraction = kelly_fraction
        self.min_edge = min_edge
        self.min_bet = min_bet
        self.min_shares = min_shares
        self.max_bet_pct = max_bet_pct
        self.entry_start = entry_start
        self.entry_end = entry_end

    def evaluate(self, market, bankroll, price_feed, seconds_remaining):
        """Returns a trade dict or None."""
        if seconds_remaining > self.entry_start or seconds_remaining < self.entry_end:
            return None

        delta = price_feed.get_window_delta()
        delta_pct = abs(delta) * 100

        if delta_pct < self.min_delta_pct:
            return None

        vol = price_feed.get_volatility(lookback=30)
        prob_up = _estimate_prob_from_delta(delta, seconds_remaining, vol)

        if delta > 0:
            side = "Up"
            prob_win = prob_up
            market_price = market["Up"]["price"]
        else:
            side = "Down"
            prob_win = 1.0 - prob_up
            market_price = market["Down"]["price"]

        if market_price > MAX_BUY_PRICE:
            return None  # Rec 2: adverse payoff math above $0.80

        edge = prob_win - market_price
        taker_fee = 4 * market_price * (1 - market_price) * 0.0156
        net_edge = edge - taker_fee

        if net_edge < self.min_edge:
            return None

        b = (1.0 - market_price) / market_price
        p = prob_win
        q = 1.0 - p
        f_star = (b * p - q) / b
        if f_star <= 0:
            return None

        kelly_bet = f_star * self.kelly_fraction
        bet_amount = kelly_bet * bankroll
        bet_amount = min(bet_amount, bankroll * self.max_bet_pct)
        bet_amount = max(self.min_bet, bet_amount)

        # Enforce 5-share minimum for GTC orders
        shares = max(self.min_shares, round(bet_amount / market_price, 1))
        bet_amount = round(shares * market_price, 2)

        # Max price we'll accept — keeps at least min_edge positive EV
        price_buffer = 0.04 if edge >= self.min_edge * 2 else 0.03
        max_price = round(min(prob_win - self.min_edge + price_buffer, 0.95), 2)

        log.info(
            f"MOMENTUM | {side} @ ${market_price:.2f} | "
            f"Delta: {delta*100:+.3f}% | Vol: {vol*100:.4f}% | "
            f"P(win): {prob_win:.2f} | Edge: {net_edge:.3f} | "
            f"Shares: {shares} | Bet: ${bet_amount:.2f}"
        )

        return {
            "side": side,
            "token_id": market[side]["token_id"],
            "outcome_index": market[side]["outcome_index"],
            "price": market_price,
            "maker_price": market_price,   # GTC limit bid at current ask
            "max_price": max_price,
            "bet_amount": bet_amount,
            "shares": shares,
            "edge": round(net_edge, 4),
            "kelly_pct": round(kelly_bet, 4),
            "estimated_prob": round(prob_win, 4),
            "use_maker": True,
            "strategy": "momentum",
        }


# ═══════════════════════════════════════════════════════════
# STRATEGY B: Fade Extreme Odds (opportunistic)
# ═══════════════════════════════════════════════════════════
#
# Sometimes the market overshoots — a big BTC spike pushes
# Up to 0.90+ but the spike is from a single large trade
# that's likely to mean-revert. Betting the opposite side at
# extreme odds has favorable risk/reward:
#   Buy Down @ $0.08 → 12.5:1 payout if it reverts
#   Need only ~10% reversion rate to break even
#
# Filter: only fade when the delta is driven by a spike (high
# instantaneous volatility) rather than a steady drift.
# Uses taker orders to guarantee fill without cancel risk.

class FadeExtremeStrategy:
    """
    Buy the cheap side when market odds are extreme (>0.85)
    and the move looks like a spike rather than a drift.
    """

    def __init__(
        self,
        extreme_threshold: float = 0.85,  # market price > this = extreme
        max_bet_pct: float = 0.03,         # tiny bets — these are longshots
        min_bet: float = 2.50,             # GTC min: 5 shares × $0.50 = $2.50
        min_shares: int = 5,               # Polymarket CLOB GTC minimum
        spike_vol_ratio: float = 3.0,      # current vol must be 3x avg
    ):
        self.extreme_threshold = extreme_threshold
        self.max_bet_pct = max_bet_pct
        self.min_bet = min_bet
        self.min_shares = min_shares
        self.spike_vol_ratio = spike_vol_ratio

    def evaluate(self, market, bankroll, price_feed, seconds_remaining):
        """Bet against spikes when odds are extreme."""
        if seconds_remaining < 30 or seconds_remaining > 180:
            return None

        up_price = market["Up"]["price"]
        down_price = market["Down"]["price"]

        # Detect extreme
        if up_price > self.extreme_threshold:
            cheap_side = "Down"
            cheap_price = down_price
        elif down_price > self.extreme_threshold:
            cheap_side = "Up"
            cheap_price = up_price
        else:
            return None

        # Check if this is a spike (high recent vol vs avg)
        vol_5s = price_feed.get_volatility(lookback=5)
        vol_60s = price_feed.get_volatility(lookback=60)

        if vol_60s <= 0:
            return None

        spike_ratio = vol_5s / vol_60s
        if spike_ratio < self.spike_vol_ratio:
            # Not a spike — steady drift, don't fade it
            return None

        # Small fixed-size bet (not Kelly — this is a speculative play)
        bet_amount = min(bankroll * self.max_bet_pct, 3.0)
        bet_amount = max(self.min_bet, bet_amount)

        if bet_amount > bankroll * 0.10:
            return None  # don't risk too much on longshots

        # Enforce 5-share minimum for GTC orders
        shares = max(self.min_shares, round(bet_amount / cheap_price, 1))
        bet_amount = round(shares * cheap_price, 2)

        log.info(
            f"FADE | {cheap_side} @ ${cheap_price:.2f} | "
            f"Spike ratio: {spike_ratio:.1f}x | "
            f"Shares: {shares} | Bet: ${bet_amount:.2f}"
        )

        return {
            "side": cheap_side,
            "token_id": market[cheap_side]["token_id"],
            "outcome_index": market[cheap_side]["outcome_index"],
            "price": cheap_price,
            "maker_price": cheap_price,    # GTC limit bid at current ask
            "bet_amount": bet_amount,
            "shares": shares,
            "edge": round(1.0 / cheap_price * 0.10 - 1.0, 4),  # rough EV
            "kelly_pct": 0.0,
            "estimated_prob": round(1.0 - self.extreme_threshold + 0.05, 4),
            "use_maker": True,
            "strategy": "fade",
        }


# ═══════════════════════════════════════════════════════════
# STRATEGY C: Late-Window Scalp (secondary strategy)
# ═══════════════════════════════════════════════════════════
#
# At T-90 the order book still has resting asks from market makers.
# By T-30 those asks are gone. So we enter at T-220 with a single IOC order.
# Stops at T-10 to leave time for the order to process.

class LateScalpStrategy:
    """
    Directional taker bet in the last 150 seconds when delta is large.
    Fires a single IOC order at the current market ask, capped at max_price
    (prob_win - min_edge) to guarantee positive EV even after spread crossing.
    Skips if the order book has no asks (illiquid).
    """

    def __init__(
        self,
        min_delta_pct: float = 0.20,     # Rec 1: raised from 0.15% — weak signals below this lack edge
        kelly_fraction: float = 0.125,   # Rec 4B: eighth-Kelly (halved from quarter)
        min_edge: float = 0.05,           # need 5% edge minimum
        high_edge_threshold: float = 0.10, # edge above this → 2% max_price buffer
        min_bet: float = 2.50,            # IOC: match GTC floor for consistency
        min_shares: int = 5,              # Polymarket CLOB minimum
        max_bet_pct: float = 0.10,        # max 10% of bankroll
        entry_window_seconds: int = 270,  # act in last 270s — enter before market reprices
    ):
        self.min_delta_pct = min_delta_pct
        self.kelly_fraction = kelly_fraction
        self.min_edge = min_edge
        self.high_edge_threshold = high_edge_threshold
        self.min_bet = min_bet
        self.min_shares = min_shares
        self.max_bet_pct = max_bet_pct
        self.entry_window_seconds = entry_window_seconds

    def evaluate(self, market, bankroll, price_feed, seconds_remaining):
        """
        Returns a trade dict or None.
        Only triggers in the last `entry_window_seconds` seconds.
        """
        if seconds_remaining > self.entry_window_seconds:
            return None

        if seconds_remaining < 10:
            return None  # too late, order won't fill

        delta = price_feed.get_window_delta()
        delta_pct = abs(delta) * 100

        if delta_pct < self.min_delta_pct:
            return None

        vol = price_feed.get_volatility(lookback=30)
        prob_up = _estimate_prob_from_delta(delta, seconds_remaining, vol)

        if delta > 0:
            side = "Up"
            prob_win = prob_up
            market_price = market["Up"]["price"]
        else:
            side = "Down"
            prob_win = 1.0 - prob_up
            market_price = market["Down"]["price"]

        if market_price > MAX_BUY_PRICE:
            return None  # Rec 2: adverse payoff math above $0.80

        edge = prob_win - market_price
        taker_fee = 4 * market_price * (1 - market_price) * 0.0156
        net_edge = edge - taker_fee

        if net_edge < self.min_edge:
            return None

        b = (1.0 - market_price) / market_price
        p = prob_win
        q = 1.0 - p
        f_star = (b * p - q) / b
        if f_star <= 0:
            return None

        kelly_bet = f_star * self.kelly_fraction
        bet_amount = kelly_bet * bankroll
        bet_amount = min(bet_amount, bankroll * self.max_bet_pct)
        bet_amount = max(self.min_bet, bet_amount)

        # Enforce 5-share minimum for IOC orders
        shares = max(self.min_shares, round(bet_amount / market_price, 1))
        bet_amount = round(shares * market_price, 2)

        # Max price we'll pay as a taker — keeps at least min_edge positive EV.
        # Buffer absorbs the ~1s submission latency where the best ask can move
        # 1-3 ticks above the cap before the FAK reaches the matching engine.
        # High-edge trades get a slightly larger buffer since there's more room.
        price_buffer = 0.04 if edge >= self.high_edge_threshold else 0.03
        max_price = round(min(prob_win - self.min_edge + price_buffer, 0.95), 2)

        eval_token_id = market[side]["token_id"]
        log.info(
            f"SCALP | {side} @ ${market_price:.2f} (max ${max_price:.2f}) | "
            f"Delta: {delta*100:+.3f}% | Vol: {vol*100:.4f}% | "
            f"P(win): {prob_win:.2f} | Edge: {net_edge:.3f} | "
            f"Buffer: +{int(price_buffer*100)}% | "
            f"Shares: {shares} | Bet: ${bet_amount:.2f} | "
            f"token={eval_token_id}"
        )

        return {
            "side": side,
            "token_id": market[side]["token_id"],
            "outcome_index": market[side]["outcome_index"],
            "price": market_price,
            "max_price": max_price,
            "bet_amount": bet_amount,
            "shares": shares,
            "edge": round(net_edge, 4),
            "kelly_pct": round(kelly_bet, 4),
            "estimated_prob": round(prob_win, 4),
            "use_maker": False,
            "strategy": "scalp",
        }


# ═══════════════════════════════════════════════════════════
# Combined Orchestrator
# ═══════════════════════════════════════════════════════════

class CombinedStrategy:
    """
    Runs all three strategies and picks the best action per window.
    Priority: Momentum (early) → Fade (opportunistic) → Scalp (late)

    The bot loop calls `evaluate_phase()` on every 5s tick.
    Each strategy fires at most once per window.
    """

    def __init__(self, dry_run: bool = True):  # dry_run kept for API compatibility
        self.momentum = EarlyMomentumStrategy()
        self.fade = FadeExtremeStrategy()
        self.scalp = LateScalpStrategy()
        self._momentum_fired = False
        self._scalp_fired = False

    def on_new_window(self):
        """Reset state for a new 5-min window."""
        self._momentum_fired = False
        self._scalp_fired = False

    def evaluate_phase(self, market, bankroll, price_feed, seconds_remaining):
        """
        Called multiple times per window. Returns:
        - ("momentum", trade)  — early directional bet (T-120 to T-90)
        - ("fade", trade)      — fade a spike (T-180 to T-90)
        - ("scalp", trade)     — directional bet (T-240 to T-10)
        - ("skip", None)       — do nothing this phase
        """
        # Phase 1: Early momentum (T-120 to T-90)
        if not self._momentum_fired and 90 <= seconds_remaining <= 120:
            trade = self.momentum.evaluate(
                market, bankroll, price_feed, seconds_remaining
            )
            if trade:
                self._momentum_fired = True
                return ("momentum", trade)

        # Phase 2: Fade extreme odds (T-180 to T-90)
        if 90 < seconds_remaining <= 180:
            trade = self.fade.evaluate(
                market, bankroll, price_feed, seconds_remaining
            )
            if trade:
                return ("fade", trade)

        # Phase 3: Mid-window scalp (T-240 to T-10) — enter before market reprices
        if seconds_remaining <= 240 and not self._scalp_fired:
            trade = self.scalp.evaluate(
                market, bankroll, price_feed, seconds_remaining
            )
            if trade:
                self._scalp_fired = True
                return ("scalp", trade)

        return ("skip", None)


# ═══════════════════════════════════════════════════════════
# Shared utility: probability from delta
# ═══════════════════════════════════════════════════════════

# Empirical P(Up wins) by (seconds_remaining, delta%).
# Built from 30 days of Binance 1m BTC/USDT klines via calibrate_model.py --days 30 --table.
# Four distinct time rows (60s elapsed, 120s, 180s, 240s into the window).
# Refresh by re-running: venv/bin/python3 calibrate_model.py --days 60 --table
_EMPIRICAL_TABLE: dict[int, dict[float, float]] = {
    240: {-0.20: 0.0333, -0.15: 0.1566, -0.10: 0.2581, -0.05: 0.2983,
           0.00: 0.5003,  0.05: 0.7199,  0.10: 0.8106,  0.15: 0.8750,  0.20: 0.8857},
    180: {-0.25: 0.0333, -0.20: 0.0816, -0.15: 0.0778, -0.10: 0.1725, -0.05: 0.2603,
           0.00: 0.4970,  0.05: 0.7444,  0.10: 0.8610,  0.15: 0.8889,  0.20: 0.9375,  0.25: 1.0000},
    120: {-0.25: 0.0000, -0.20: 0.0306, -0.15: 0.0714, -0.10: 0.0823, -0.05: 0.2180,
           0.00: 0.4947,  0.05: 0.8045,  0.10: 0.9029,  0.15: 0.9395,  0.20: 0.9667,
           0.25: 0.9722,  0.30: 1.0000},
    60:  {-0.35: 0.0000, -0.30: 0.0000, -0.25: 0.0000, -0.20: 0.0000, -0.15: 0.0154,
          -0.10: 0.0534, -0.05: 0.1507,  0.00: 0.4936,  0.05: 0.8515,  0.10: 0.9614,
           0.15: 0.9803,  0.20: 0.9924,  0.25: 1.0000,  0.30: 1.0000},
}
_EMPIRICAL_SECS = sorted(_EMPIRICAL_TABLE.keys(), reverse=True)  # [240, 180, 120, 60]


def _interp_delta(row: dict[float, float], delta_pct: float) -> float:
    """Linear interpolation across delta buckets in one time row."""
    deltas = sorted(row.keys())
    if delta_pct <= deltas[0]:
        return row[deltas[0]]
    if delta_pct >= deltas[-1]:
        return row[deltas[-1]]
    for i in range(len(deltas) - 1):
        lo, hi = deltas[i], deltas[i + 1]
        if lo <= delta_pct <= hi:
            t = (delta_pct - lo) / (hi - lo)
            return row[lo] * (1.0 - t) + row[hi] * t
    return 0.50


def _estimate_prob_from_delta(delta, seconds_remaining, volatility):
    """
    Estimate P(Up wins) via bilinear interpolation on the empirical table.

    `delta` is a fraction (e.g. 0.001 for a 0.1% BTC move).
    `volatility` is accepted for API compatibility but not used — the empirical
    table already encodes regime-average volatility scaling.
    Clamped to [0.05, 0.95].
    """
    delta_pct = delta * 100.0

    secs = _EMPIRICAL_SECS  # [240, 180, 120, 60]

    if seconds_remaining >= secs[0]:
        # Earlier than our earliest row — very weak signal, use 240s row
        return max(0.05, min(0.95, _interp_delta(_EMPIRICAL_TABLE[secs[0]], delta_pct)))

    if seconds_remaining <= secs[-1]:
        # Later than our latest row — signal is even stronger, use 60s row
        return max(0.05, min(0.95, _interp_delta(_EMPIRICAL_TABLE[secs[-1]], delta_pct)))

    # Interpolate between the two bracketing time rows
    for i in range(len(secs) - 1):
        upper, lower = secs[i], secs[i + 1]
        if lower <= seconds_remaining <= upper:
            t = (seconds_remaining - lower) / (upper - lower)  # 1 = upper, 0 = lower
            p_upper = _interp_delta(_EMPIRICAL_TABLE[upper], delta_pct)
            p_lower = _interp_delta(_EMPIRICAL_TABLE[lower], delta_pct)
            prob_up = t * p_upper + (1.0 - t) * p_lower
            return max(0.05, min(0.95, prob_up))

    return 0.50  # unreachable
