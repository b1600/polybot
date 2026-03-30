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
# C) Mid-Window Scalp (T-90 to T-10):
#    Directional taker bet (FAK) when book has asks, or GTC maker
#    when book is empty. Fires at T-90 where liquidity still exists.
#
# WHY MM WAS REMOVED:
# ─────────────────────────────────────────────────────────
# In live (non dry-run) mode, Polymarket's CLOB filled maker bids
# instantly as taker orders, spending USDC with no way to cancel
# ("matched orders can't be canceled"). The cancel-after-fill
# approach that looked profitable in dry-run is not replicable
# live because the CLOB immediately matches aggressive bids.
# ─────────────────────────────────────────────────────────

import math
import logging

log = logging.getLogger("strategy_v2")


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
        kelly_fraction: float = 0.25,      # quarter-Kelly
        min_edge: float = 0.05,            # need 5% net edge after fees
        min_bet: float = 1.0,
        max_bet_pct: float = 0.10,         # max 10% of bankroll
        entry_start: int = 120,            # start evaluating at T-120
        entry_end: int = 90,               # stop at T-90 (scalp takes over)
    ):
        self.min_delta_pct = min_delta_pct
        self.kelly_fraction = kelly_fraction
        self.min_edge = min_edge
        self.min_bet = min_bet
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

        log.info(
            f"MOMENTUM | {side} @ ${market_price:.2f} | "
            f"Delta: {delta*100:+.3f}% | Vol: {vol*100:.4f}% | "
            f"P(win): {prob_win:.2f} | Edge: {net_edge:.3f} | "
            f"Bet: ${bet_amount:.2f}"
        )

        return {
            "side": side,
            "token_id": market[side]["token_id"],
            "price": market_price,
            "bet_amount": round(bet_amount, 2),
            "shares": round(bet_amount / market_price, 1),
            "edge": round(net_edge, 4),
            "kelly_pct": round(kelly_bet, 4),
            "estimated_prob": round(prob_win, 4),
            "use_maker": False,   # taker — avoids instant-match + cancel problem
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
        min_bet: float = 1.0,
        spike_vol_ratio: float = 3.0,      # current vol must be 3x avg
    ):
        self.extreme_threshold = extreme_threshold
        self.max_bet_pct = max_bet_pct
        self.min_bet = min_bet
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

        log.info(
            f"FADE | {cheap_side} @ ${cheap_price:.2f} | "
            f"Spike ratio: {spike_ratio:.1f}x | "
            f"Bet: ${bet_amount:.2f}"
        )

        return {
            "side": cheap_side,
            "token_id": market[cheap_side]["token_id"],
            "price": cheap_price,
            "bet_amount": round(bet_amount, 2),
            "shares": round(bet_amount / cheap_price, 1),
            "edge": round(1.0 / cheap_price * 0.10 - 1.0, 4),  # rough EV
            "kelly_pct": 0.0,
            "estimated_prob": round(1.0 - self.extreme_threshold + 0.05, 4),
            "use_maker": False,   # taker — avoids instant-match + cancel problem
            "strategy": "fade",
        }


# ═══════════════════════════════════════════════════════════
# STRATEGY C: Late-Window Scalp (secondary strategy)
# ═══════════════════════════════════════════════════════════
#
# At T-90 the order book still has resting asks from market makers.
# By T-30 those asks are gone. So we enter at T-90 and use:
#   - FAK if the book has asks (immediate fill)
#   - GTC maker if the book is empty (rests until filled or cancelled at T-3)
# Stops at T-10 to leave time for the order to process.

class LateScalpStrategy:
    """
    Bet direction in the last 90 seconds when delta is large.
    FAK if book has asks; GTC maker order if book is empty.
    """

    def __init__(
        self,
        min_delta_pct: float = 0.15,     # minimum 0.15% BTC move
        kelly_fraction: float = 0.25,     # quarter-Kelly (conservative)
        min_edge: float = 0.05,           # need 5% edge minimum
        min_bet: float = 1.0,
        max_bet_pct: float = 0.10,        # max 10% of bankroll
        entry_window_seconds: int = 90,   # act in last 90s — book still has depth
    ):
        self.min_delta_pct = min_delta_pct
        self.kelly_fraction = kelly_fraction
        self.min_edge = min_edge
        self.min_bet = min_bet
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

        log.info(
            f"SCALP | {side} @ ${market_price:.2f} | "
            f"Delta: {delta*100:+.3f}% | Vol: {vol*100:.4f}% | "
            f"P(win): {prob_win:.2f} | Edge: {net_edge:.3f} | "
            f"Bet: ${bet_amount:.2f}"
        )

        return {
            "side": side,
            "token_id": market[side]["token_id"],
            "price": market_price,
            "bet_amount": round(bet_amount, 2),
            "shares": round(bet_amount / market_price, 1),
            "edge": round(net_edge, 4),
            "kelly_pct": round(kelly_bet, 4),
            "estimated_prob": round(prob_win, 4),
            "use_maker": False,  # executor decides FAK vs GTC based on book depth
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

    def on_new_window(self):
        """Reset state for a new 5-min window."""
        self._momentum_fired = False

    def evaluate_phase(self, market, bankroll, price_feed, seconds_remaining):
        """
        Called multiple times per window. Returns:
        - ("momentum", trade)  — early directional bet (T-120 to T-90)
        - ("fade", trade)      — fade a spike (T-180 to T-90)
        - ("scalp", trade)     — directional bet (T-90 to T-10)
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

        # Phase 3: Mid-window scalp (T-90 to T-10) — book still has depth here
        if seconds_remaining <= 90:
            trade = self.scalp.evaluate(
                market, bankroll, price_feed, seconds_remaining
            )
            if trade:
                return ("scalp", trade)

        return ("skip", None)


# ═══════════════════════════════════════════════════════════
# Shared utility: probability from delta
# ═══════════════════════════════════════════════════════════

def _estimate_prob_from_delta(delta_pct, seconds_remaining, volatility):
    """
    Estimate P(Up wins) from current delta, time left, and volatility.

    Normalises delta by remaining expected volatility (vol * sqrt(T))
    to get a z-score, then converts via normal CDF.
    Clamped to [0.10, 0.90] — we're never that certain.
    """
    if volatility <= 0:
        volatility = 0.0001  # fallback: ~0.01% per second

    remaining_vol = volatility * math.sqrt(max(seconds_remaining, 1))

    if remaining_vol <= 0:
        return 0.50

    z = delta_pct / remaining_vol
    prob_up = _normal_cdf(z)
    return max(0.10, min(0.90, prob_up))


def _normal_cdf(x):
    """Abramowitz & Stegun approximation of Φ(x), max error ~1.5e-7."""
    sign = 1 if x >= 0 else -1
    x = abs(x)
    t = 1.0 / (1.0 + 0.2316419 * x)
    d = 0.3989422804014327  # 1/sqrt(2π)
    p = d * math.exp(-x * x / 2.0) * (
        t * (0.319381530 +
        t * (-0.356563782 +
        t * (1.781477937 +
        t * (-1.821255978 +
        t * 1.330274429))))
    )
    return 0.5 + sign * (0.5 - p)
