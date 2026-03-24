# strategy_v2.py — Improved Polymarket BTC 5-min strategy
#
# PROBLEMS WITH v1 (strategy.py):
# ─────────────────────────────────────────────────────────
# 1. estimate_true_probability() is fundamentally broken:
#    - A 0.1% BTC move gives ±8% probability shift, but the market
#      ALREADY prices in the current delta. You're not finding edge —
#      you're just agreeing with a stale version of what the market
#      already knows, then getting surprised when it moves against you.
#    - The "time_boost" makes this WORSE: boosting confidence in the
#      last 60s on a signal the market priced in 3 minutes ago.
#    - Momentum (10s lookback) is pure noise at BTC tick scale.
#
# 2. Evaluates at T-60 only (one shot). If you're wrong, you're stuck.
#
# 3. Treats Polymarket prices as slow/dumb. They're not — market makers
#    on Polymarket watch the SAME Binance feed and update in <1 second.
#    By T-60, the Polymarket price already reflects the current delta.
#
# 4. No concept of market microstructure — bid/ask spread, book depth,
#    or how your own order moves the price.
#
# WHAT ACTUALLY WORKS IN 5-MIN BINARY MARKETS:
# ─────────────────────────────────────────────────────────
# The guide's own Key Takeaway #6 says it: "The latency game is over
# for retail." You cannot out-predict the market on direction. The edge
# for retail is MARKET MAKING — posting limit orders on both sides,
# capturing the bid-ask spread, and earning maker rebates.
#
# Strategy v2 implements a dual approach:
#   A) Market-making: post two-sided liquidity, earn spread + rebates
#   B) Late-window scalping: only bet direction in the LAST 15 seconds
#      when the delta is so large the market can't revert
#
# ─────────────────────────────────────────────────────────

import math
import time
import logging

log = logging.getLogger("strategy_v2")


# ═══════════════════════════════════════════════════════════
# STRATEGY A: Market Making (primary strategy)
# ═══════════════════════════════════════════════════════════
#
# Instead of predicting direction, post limit orders on BOTH
# sides of the book at prices that give you a built-in edge
# from the spread. You profit when one side fills and the
# market resolves, as long as your fill rate and spread math
# are correct.
#
# Example:
#   True prob ≈ 50/50. Market mid = 0.50.
#   You post: Buy Up @ 0.47, Buy Down @ 0.47
#   If Up fills and wins:  profit = (1.00 - 0.47) = $0.53/share
#   If Up fills and loses: loss   = $0.47/share
#   Expected: 0.50 * 0.53 - 0.50 * 0.47 = +$0.03/share (6% ROI)
#   Plus maker rebate on top.
#
# The risk: both sides fill in a choppy market, guaranteeing
# a loss on one side. Mitigation: cancel the unfilled side
# immediately when one side fills.

class MarketMakingStrategy:
    """
    Post two-sided liquidity at a spread around the mid price.
    Cancel the opposite side as soon as one fills.
    """

    def __init__(
        self,
        spread_bps: int = 600,       # 6% total spread (3% each side)
        max_position_pct: float = 0.05,  # max 5% of bankroll per side
        min_spread_bps: int = 400,    # don't MM if spread < 4%
        skew_factor: float = 0.3,     # how much to skew quotes toward likely winner
    ):
        self.spread_bps = spread_bps
        self.max_position_pct = max_position_pct
        self.min_spread_bps = min_spread_bps
        self.skew_factor = skew_factor

    def generate_quotes(self, market, bankroll, price_feed, seconds_remaining):
        """
        Generate two-sided quotes (buy Up + buy Down).

        Returns list of order dicts, or empty list if no opportunity.
        Only quote when:
          - We're between T-240 and T-30 (enough time for fill + cancel)
          - The existing spread is wide enough to be profitable
          - Market is near 50/50 (spread capturing works best here)
        """
        if seconds_remaining < 30 or seconds_remaining > 240:
            return []

        up_price = market["Up"]["price"]
        down_price = market["Down"]["price"]
        mid = (up_price + (1.0 - down_price)) / 2.0  # synthetic mid

        # Only MM when market is near 50/50 (0.35 to 0.65)
        if mid < 0.35 or mid > 0.65:
            return []

        # Check if existing spread is wide enough
        existing_spread = up_price + down_price - 1.0  # overround
        # In a two-outcome market, if Up=0.52 and Down=0.51,
        # the "vig" is 0.52 + 0.51 - 1.0 = 0.03 (3%)
        # We need room to post inside this spread

        half_spread = self.spread_bps / 10000 / 2

        # Optional: skew toward the likely direction
        delta = price_feed.get_window_delta()
        skew = 0.0
        if abs(delta) > 0.0005:  # only skew on meaningful moves
            # If BTC is up, we're slightly more willing to buy Up (higher bid)
            # and slightly less willing to buy Down (lower bid)
            skew = min(abs(delta) * self.skew_factor * 100, half_spread * 0.5)
            if delta < 0:
                skew = -skew

        # Our quote prices
        bid_up = round(mid - half_spread + skew, 2)
        bid_down = round((1.0 - mid) - half_spread - skew, 2)

        # Sanity: prices must be 0.01–0.99
        bid_up = max(0.01, min(0.99, bid_up))
        bid_down = max(0.01, min(0.99, bid_down))

        # Position sizing: small and symmetric
        size_usd = bankroll * self.max_position_pct
        size_usd = max(1.0, min(size_usd, 10.0))  # $1–$10 per side

        orders = []
        orders.append({
            "side": "Up",
            "token_id": market["Up"]["token_id"],
            "price": bid_up,
            "bet_amount": round(size_usd, 2),
            "shares": round(size_usd / bid_up, 1),
            "order_type": "maker",
            "strategy": "mm",
        })
        orders.append({
            "side": "Down",
            "token_id": market["Down"]["token_id"],
            "price": bid_down,
            "bet_amount": round(size_usd, 2),
            "shares": round(size_usd / bid_down, 1),
            "order_type": "maker",
            "strategy": "mm",
        })

        log.info(
            f"MM quotes | Up bid: ${bid_up:.2f} | Down bid: ${bid_down:.2f} | "
            f"Size: ${size_usd:.2f}/side | Spread: {self.spread_bps}bps | "
            f"Skew: {skew:+.3f}"
        )
        return orders


# ═══════════════════════════════════════════════════════════
# STRATEGY B: Late-Window Scalp (secondary strategy)
# ═══════════════════════════════════════════════════════════
#
# The ONLY time retail can reliably predict direction is in the
# very last seconds of a window, when:
#   1. BTC delta is large (>0.15%) AND
#   2. Time remaining is tiny (<15s) AND
#   3. The Polymarket price hasn't fully caught up
#
# This is rare. The bot should trigger on maybe 5-10% of windows.
# When it does, use a market (taker) order for guaranteed fill.
#
# Key differences from v1:
#   - Much higher delta threshold (0.15% vs 0.001%)
#   - Much later entry (T-15 vs T-60)
#   - No momentum — only raw delta matters this late
#   - Smaller bet sizing (quarter-Kelly, not half-Kelly)

class LateScalpStrategy:
    """
    Bet direction only in the last 15 seconds when delta is extreme.
    """

    def __init__(
        self,
        min_delta_pct: float = 0.15,     # minimum 0.15% BTC move
        kelly_fraction: float = 0.25,     # quarter-Kelly (conservative)
        min_edge: float = 0.05,           # need 5% edge minimum
        min_bet: float = 1.0,
        max_bet_pct: float = 0.10,        # max 10% of bankroll
        entry_window_seconds: int = 15,   # only act in last 15s
    ):
        self.min_delta_pct = min_delta_pct
        self.kelly_fraction = kelly_fraction
        self.min_edge = min_edge
        self.min_bet = min_bet
        self.max_bet_pct = max_bet_pct
        self.entry_window_seconds = entry_window_seconds

    def estimate_prob_from_delta(self, delta_pct, seconds_remaining, volatility):
        """
        Estimate P(Up wins) from current delta, time left, and volatility.

        Uses a simplified model: if BTC is X% above the open with T seconds
        left, what's the probability it stays above?

        For small T and large X relative to volatility, this approaches 1.0.
        Key insight: we use VOLATILITY to normalize the delta, not an
        arbitrary fixed scale like v1 did.
        """
        if volatility <= 0:
            volatility = 0.0001  # fallback: ~0.01% per second

        # Normalize delta by remaining volatility
        # Expected remaining move ≈ volatility * sqrt(seconds_remaining)
        remaining_vol = volatility * math.sqrt(max(seconds_remaining, 1))

        if remaining_vol <= 0:
            return 0.50

        # z-score: how many remaining-volatility units is the current delta?
        z = delta_pct / remaining_vol

        # Convert to probability using normal CDF approximation
        # P(finish Up) ≈ Φ(z) when delta > 0
        prob_up = _normal_cdf(z)

        # Clamp to [0.10, 0.90] — we're never that certain
        return max(0.10, min(0.90, prob_up))

    def evaluate(self, market, bankroll, price_feed, seconds_remaining):
        """
        Returns a trade dict or None.
        Only triggers in the last `entry_window_seconds` seconds.
        """
        if seconds_remaining > self.entry_window_seconds:
            return None

        if seconds_remaining < 3:
            return None  # too late, order won't fill

        delta = price_feed.get_window_delta()
        delta_pct = abs(delta) * 100

        # Hard filter: need a big enough move
        if delta_pct < self.min_delta_pct:
            return None

        # Use actual volatility, not an arbitrary scale
        vol = price_feed.get_volatility(lookback=30)
        prob_up = self.estimate_prob_from_delta(
            delta, seconds_remaining, vol
        )

        # Determine side
        if delta > 0:
            side = "Up"
            prob_win = prob_up
            market_price = market["Up"]["price"]
        else:
            side = "Down"
            prob_win = 1.0 - prob_up
            market_price = market["Down"]["price"]

        # Edge = our estimated prob - market price
        edge = prob_win - market_price

        # Subtract taker fee (we need market orders this late)
        taker_fee = 4 * market_price * (1 - market_price) * 0.0156
        net_edge = edge - taker_fee

        if net_edge < self.min_edge:
            return None

        # Quarter-Kelly sizing
        b = (1.0 - market_price) / market_price
        p = prob_win
        q = 1.0 - p
        f_star = (b * p - q) / b
        if f_star <= 0:
            return None

        kelly_bet = f_star * self.kelly_fraction
        bet_amount = kelly_bet * bankroll
        bet_amount = max(self.min_bet, bet_amount)
        bet_amount = min(bet_amount, bankroll * self.max_bet_pct)

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
            "use_maker": False,  # always taker this late
            "strategy": "scalp",
        }


# ═══════════════════════════════════════════════════════════
# STRATEGY C: Fade Extreme Odds (tertiary, opportunistic)
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
            "use_maker": True,  # use maker — we have time
            "strategy": "fade",
        }


# ═══════════════════════════════════════════════════════════
# Combined Orchestrator
# ═══════════════════════════════════════════════════════════

class CombinedStrategy:
    """
    Runs all three strategies and picks the best action per window.
    Priority: MM (passive) → Fade (opportunistic) → Scalp (late)

    The bot loop should call `evaluate_phase()` multiple times per
    window as time progresses, not just once at T-60.
    """

    def __init__(self):
        self.mm = MarketMakingStrategy()
        self.scalp = LateScalpStrategy()
        self.fade = FadeExtremeStrategy()
        self._mm_orders_placed = False
        self._current_window = None

    def on_new_window(self):
        """Reset state for a new 5-min window."""
        self._mm_orders_placed = False
        self._current_window = None

    def evaluate_phase(self, market, bankroll, price_feed, seconds_remaining):
        """
        Called multiple times per window. Returns:
        - ("mm", [orders])     — post two-sided quotes (early)
        - ("fade", trade)      — fade a spike (mid-window)
        - ("scalp", trade)     — directional bet (last 15s)
        - ("skip", None)       — do nothing this phase
        """
        # Phase 1: Market making (T-240 to T-30)
        if not self._mm_orders_placed and 30 < seconds_remaining <= 240:
            orders = self.mm.generate_quotes(
                market, bankroll, price_feed, seconds_remaining
            )
            if orders:
                self._mm_orders_placed = True
                return ("mm", orders)

        # Phase 2: Fade extreme odds (T-180 to T-30)
        if 30 < seconds_remaining <= 180:
            trade = self.fade.evaluate(
                market, bankroll, price_feed, seconds_remaining
            )
            if trade:
                return ("fade", trade)

        # Phase 3: Late scalp (last 15s)
        if seconds_remaining <= 15:
            trade = self.scalp.evaluate(
                market, bankroll, price_feed, seconds_remaining
            )
            if trade:
                return ("scalp", trade)

        return ("skip", None)


# ═══════════════════════════════════════════════════════════
# Utility: Normal CDF approximation (no scipy needed)
# ═══════════════════════════════════════════════════════════

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
