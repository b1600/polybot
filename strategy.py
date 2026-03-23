# strategy.py
import math

class MispricingStrategy:
    def __init__(self, kelly_fraction=0.5, min_edge=0.03, max_edge=0.08, min_bet=1.0):
        self.kelly_fraction = kelly_fraction  # half-Kelly recommended
        self.min_edge = min_edge              # minimum edge to trade (3%)
        self.max_edge = max_edge              # maximum edge to trade (8%) — above this likely a data error
        self.min_bet = min_bet

    def estimate_true_probability(self, window_delta, momentum, seconds_remaining):
        """
        Estimate the true probability that BTC finishes "Up" for this window.

        The dominant signal is window_delta: if BTC is already up vs. the
        window open price, it's more likely to finish up (and vice versa).

        As time remaining decreases, the signal gets stronger because
        there's less time for reversal.
        """
        base_prob = 0.50  # Baseline: 50/50

        # Window delta is the strongest signal
        # Scale: 0.1% move ≈ +/- 5% probability shift
        delta_weight = min(abs(window_delta) / 0.001, 1.0) * 0.08
        if window_delta > 0:
            base_prob += delta_weight
        else:
            base_prob -= delta_weight

        # Time decay: signal is stronger with less time remaining
        # In last 30 seconds, delta signal gets a 50% boost
        if seconds_remaining < 30:
            time_boost = 1.5
        elif seconds_remaining < 60:
            time_boost = 1.2
        else:
            time_boost = 1.0

        # Recompute with time boost
        adjusted_delta = delta_weight * time_boost
        prob_up = 0.50 + (adjusted_delta if window_delta > 0 else -adjusted_delta)

        # Light momentum factor
        momentum_shift = momentum * 500  # scale to ±0.02 range
        momentum_shift = max(-0.02, min(0.02, momentum_shift))
        prob_up += momentum_shift

        # Clamp to reasonable range
        prob_up = max(0.35, min(0.65, prob_up))

        return prob_up

    def calculate_kelly(self, prob_win, share_price):
        """
        Kelly Criterion for binary bet.
        f* = (b*p - q) / b
        where b = (1 - price) / price, p = prob_win, q = 1 - p
        """
        if share_price <= 0 or share_price >= 1:
            return 0.0

        b = (1.0 - share_price) / share_price  # payout ratio
        p = prob_win
        q = 1.0 - p
        f_star = (b * p - q) / b

        if f_star <= 0:
            return 0.0  # No edge, don't bet

        return f_star * self.kelly_fraction  # Apply fractional Kelly

    def calculate_taker_fee(self, share_price):
        """
        Polymarket 5-min market taker fee formula.
        Fee is highest at 50% probability (~1.56%), lowest at extremes.
        fee = 4 * price * (1 - price) * base_rate
        """
        base_rate = 0.0156  # 1.56% max at 50%
        return 4 * share_price * (1 - share_price) * base_rate

    def evaluate(self, market, bankroll, price_feed, seconds_remaining):
        """
        Main decision function. Returns a trade dict or None.

        Returns: {
            "side": "Up" or "Down",
            "token_id": str,
            "price": float,
            "bet_amount": float,
            "shares": float,
            "edge": float,
            "kelly_fraction": float,
        } or None
        """
        window_delta = price_feed.get_window_delta()
        momentum = price_feed.get_momentum(lookback=10)

        prob_up = self.estimate_true_probability(
            window_delta, momentum, seconds_remaining
        )
        prob_down = 1.0 - prob_up

        # Check both sides for mispricing
        candidates = []

        # Check "Up" side
        up_price = market["Up"]["price"]
        up_edge = prob_up - up_price
        if self.min_edge < up_edge <= self.max_edge:
            kelly = self.calculate_kelly(prob_up, up_price)
            candidates.append({
                "side": "Up",
                "token_id": market["Up"]["token_id"],
                "price": up_price,
                "edge": up_edge,
                "kelly_pct": kelly,
                "prob": prob_up,
            })

        # Check "Down" side
        down_price = market["Down"]["price"]
        down_edge = prob_down - down_price
        if self.min_edge < down_edge <= self.max_edge:
            kelly = self.calculate_kelly(prob_down, down_price)
            candidates.append({
                "side": "Down",
                "token_id": market["Down"]["token_id"],
                "price": down_price,
                "edge": down_edge,
                "kelly_pct": kelly,
                "prob": prob_down,
            })

        if not candidates:
            return None  # No mispricing found — skip this window

        # Pick the side with the largest edge
        best = max(candidates, key=lambda c: c["edge"])

        bet_amount = best["kelly_pct"] * bankroll
        bet_amount = max(self.min_bet, bet_amount)
        bet_amount = min(bet_amount, bankroll * 0.20)  # hard cap: 20% of bankroll
        shares = bet_amount / best["price"]

        # Check if edge survives taker fee (if not using maker orders)
        taker_fee = self.calculate_taker_fee(best["price"])
        net_edge = best["edge"] - taker_fee

        return {
            "side": best["side"],
            "token_id": best["token_id"],
            "price": best["price"],
            "bet_amount": round(bet_amount, 2),
            "shares": round(shares, 1),
            "edge": round(best["edge"], 4),
            "net_edge_after_taker_fee": round(net_edge, 4),
            "kelly_pct": round(best["kelly_pct"], 4),
            "estimated_prob": round(best["prob"], 4),
            "use_maker": net_edge < self.min_edge,  # prefer maker if edge is thin
        }
