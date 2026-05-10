"""
calibrate_model.py — Does _estimate_prob_from_delta actually predict BTC 5-min outcomes?

Fetches N days of BTC 1m klines from Binance, reconstructs each 5-min window,
runs the strategy's probability model at key entry points, then compares estimated
P(win) to actual win rate by bucket.

Usage:
    venv/bin/python3 calibrate_model.py            # last 30 days
    venv/bin/python3 calibrate_model.py --days 7
    venv/bin/python3 calibrate_model.py --days 90 --plot
"""

import argparse
import math
import time
import requests
from collections import defaultdict

# ── Replicated from strategy_v2 (no import needed) ─────────────────────────

def _normal_cdf(x):
    sign = 1 if x >= 0 else -1
    x = abs(x)
    t = 1.0 / (1.0 + 0.2316419 * x)
    d = 0.3989422804014327
    p = d * math.exp(-x * x / 2.0) * (
        t * (0.319381530 +
        t * (-0.356563782 +
        t * (1.781477937 +
        t * (-1.821255978 +
        t * 1.330274429))))
    )
    return 0.5 + sign * (0.5 - p)


def _estimate_prob_from_delta(delta, seconds_remaining, volatility):
    """Exact copy of strategy_v2._estimate_prob_from_delta."""
    if volatility <= 0:
        volatility = 0.0001
    remaining_vol = volatility * math.sqrt(max(seconds_remaining, 1))
    if remaining_vol <= 0:
        return 0.50
    z = delta / remaining_vol
    prob_up = _normal_cdf(z)
    return max(0.10, min(0.80, prob_up))


# ── Binance kline fetch ─────────────────────────────────────────────────────

BINANCE_REST = "https://data-api.binance.vision/api/v3/klines"


def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> list:
    """
    Fetches all 1m klines between start_ms and end_ms (epoch milliseconds).
    Paginates automatically (Binance limit=1000 per call).
    Returns list of [open_time_ms, open, high, low, close, volume, ...].
    """
    klines = []
    cursor = start_ms
    while cursor < end_ms:
        resp = requests.get(
            BINANCE_REST,
            params={
                "symbol": symbol,
                "interval": interval,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": 1000,
            },
            timeout=15,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        klines.extend(batch)
        cursor = batch[-1][0] + 60_000  # next minute after last bar
        if len(batch) < 1000:
            break
        time.sleep(0.1)  # gentle rate-limit
    return klines


# ── Window reconstruction ───────────────────────────────────────────────────

def build_windows(klines: list) -> list:
    """
    Groups 1m klines into 5-min windows. Returns list of window dicts:
      {
        window_start: int (unix seconds),
        minutes: [open_price_t0, t1, t2, t3, t4],  # open price of each minute
        close: float,  # close of minute t4 (= window close)
        outcome: "Up" | "Down" | "Flat",
      }
    """
    # Build minute_open lookup: unix_minute_start → open price
    minute_open: dict[int, float] = {}
    minute_close: dict[int, float] = {}
    for bar in klines:
        t = bar[0] // 1000  # ms → seconds
        minute_open[t] = float(bar[1])
        minute_close[t] = float(bar[4])

    windows = []
    if not minute_open:
        return windows

    min_t = min(minute_open)
    max_t = max(minute_open)

    # Snap to 5-min grid
    start = min_t - (min_t % 300)
    end = max_t - (max_t % 300)

    t = start
    while t < end:
        minutes = [minute_open.get(t + i * 60) for i in range(5)]
        if any(v is None for v in minutes):
            t += 300
            continue

        window_open = minutes[0]
        window_close = minute_close.get(t + 4 * 60)
        if window_close is None:
            t += 300
            continue

        if window_close > window_open:
            outcome = "Up"
        elif window_close < window_open:
            outcome = "Down"
        else:
            outcome = "Flat"

        windows.append({
            "window_start": t,
            "minutes": minutes,     # open price at minutes 0-4
            "close": window_close,
            "outcome": outcome,
        })
        t += 300

    return windows


# ── Model evaluation ────────────────────────────────────────────────────────

def compute_model_signals(window: dict) -> list[dict]:
    """
    For each key entry point in a window, compute:
      - seconds_remaining
      - window_delta (% move from open to current)
      - rolling_vol (std of 1-min returns so far)
      - prob_up from model
      - outcome_is_up (ground truth)

    Entry points: T-270, T-210, T-150, T-120, T-90, T-30
    (seconds remaining until window closes)
    """
    minutes = window["minutes"]  # [open@0min, open@1min, open@2min, open@3min, open@4min]
    outcome_is_up = window["outcome"] == "Up"
    window_open = minutes[0]

    # Map seconds_remaining → price available at that moment
    # T=300s remaining = window just started (second 0), price = minutes[0]
    # T=240s remaining = 1 minute elapsed, price = minutes[1]
    # T=180s remaining = 2 minutes elapsed, price = minutes[2]
    # T=120s remaining = 3 minutes elapsed, price = minutes[3]
    # T=60s  remaining = 4 minutes elapsed, price = minutes[4]
    available = {
        300: minutes[0],
        240: minutes[1],
        180: minutes[2],
        120: minutes[3],
        60:  minutes[4],
    }

    target_seconds = [270, 240, 210, 180, 150, 120, 90, 60]

    signals = []
    for secs in target_seconds:
        # Use the last available price at or before this point
        elapsed = 300 - secs
        if elapsed < 0:
            continue
        # find latest minute whose open we've seen
        price = None
        for minute_elapsed in [240, 180, 120, 60, 0]:
            if elapsed >= minute_elapsed:
                price = available.get(300 - minute_elapsed)
                break
        if price is None:
            continue

        delta = (price - window_open) / window_open if window_open != 0 else 0.0

        # Rolling volatility: std of returns for minutes elapsed so far
        n_mins = max(0, elapsed // 60)
        if n_mins >= 2:
            prices_so_far = minutes[:n_mins + 1]
            returns = [
                (prices_so_far[i] - prices_so_far[i - 1]) / prices_so_far[i - 1]
                for i in range(1, len(prices_so_far))
                if prices_so_far[i - 1] != 0
            ]
            if returns:
                mean_r = sum(returns) / len(returns)
                variance = sum((r - mean_r) ** 2 for r in returns) / len(returns)
                vol = variance ** 0.5
            else:
                vol = 0.0001
        else:
            vol = 0.0002  # fallback for very early in window

        prob_up = _estimate_prob_from_delta(delta, secs, vol)

        signals.append({
            "seconds_remaining": secs,
            "delta_pct": delta * 100,
            "vol": vol,
            "prob_up": prob_up,
            "outcome_is_up": outcome_is_up,
        })

    return signals


# ── Calibration analysis ────────────────────────────────────────────────────

def bucket(prob: float, width: float = 0.05) -> float:
    return round(round(prob / width) * width, 2)


def run_calibration(windows: list) -> dict:
    """
    Returns nested dict: seconds_remaining → prob_bucket → {count, wins, losses}.
    Excludes "Flat" windows (ambiguous outcome).
    """
    stats: dict[int, dict[float, dict]] = defaultdict(lambda: defaultdict(lambda: {"n": 0, "wins": 0}))

    for w in windows:
        if w["outcome"] == "Flat":
            continue
        for sig in compute_model_signals(w):
            secs = sig["seconds_remaining"]
            b = bucket(sig["prob_up"])
            outcome_is_up = sig["outcome_is_up"]
            # If prob_up > 0.5, we'd bet Up. If prob_up < 0.5, we'd bet Down.
            if sig["prob_up"] >= 0.5:
                bet_side_up = True
                estimated_p = sig["prob_up"]
            else:
                bet_side_up = False
                estimated_p = 1.0 - sig["prob_up"]

            won = (bet_side_up and outcome_is_up) or (not bet_side_up and not outcome_is_up)

            stats[secs][b]["n"] += 1
            if won:
                stats[secs][b]["wins"] += 1

    return stats


def print_calibration_table(stats: dict, min_n: int = 30):
    """Prints calibration table grouped by seconds_remaining."""
    print("\n" + "=" * 72)
    print("MODEL CALIBRATION: estimated P(win) vs actual win rate")
    print("(bet direction = whichever side the model favors)")
    print("=" * 72)

    all_secs = sorted(stats.keys(), reverse=True)
    for secs in all_secs:
        buckets = stats[secs]
        rows = []
        for b in sorted(buckets.keys()):
            d = buckets[b]
            if d["n"] < min_n:
                continue
            actual = d["wins"] / d["n"]
            stderr = math.sqrt(actual * (1 - actual) / d["n"])
            bias = actual - b  # positive = model underestimates, actual > estimate
            rows.append((b, d["n"], actual, stderr, bias))

        if not rows:
            continue

        print(f"\n  T-{300 - secs:3d}s elapsed (T-{secs:3d}s remaining):")
        print(f"  {'Est P':>7}  {'N':>6}  {'Actual%':>8}  {'±StdErr':>8}  {'Bias':>7}  {'Status'}")
        print(f"  {'-'*7}  {'-'*6}  {'-'*8}  {'-'*8}  {'-'*7}  {'-'*20}")
        for b, n, actual, stderr, bias in rows:
            flag = ""
            if abs(bias) > 2 * stderr and n >= 50:
                flag = "<-- MISCALIBRATED" if abs(bias) > 0.05 else "<-- slight bias"
            print(
                f"  {b:7.2f}  {n:6d}  {actual:8.3f}  {stderr:8.4f}  {bias:+7.3f}  {flag}"
            )


def print_summary(stats: dict, min_n: int = 30):
    """Overall win rate by seconds_remaining bucket."""
    print("\n" + "=" * 72)
    print("OVERALL WIN RATE BY ENTRY TIME (all prob buckets combined)")
    print("=" * 72)
    print(f"  {'Secs left':>9}  {'Trades':>8}  {'Win%':>7}  {'Edge vs 50%':>12}")
    print(f"  {'-'*9}  {'-'*8}  {'-'*7}  {'-'*12}")

    for secs in sorted(stats.keys(), reverse=True):
        total_n = sum(d["n"] for d in stats[secs].values())
        total_wins = sum(d["wins"] for d in stats[secs].values())
        if total_n < min_n:
            continue
        win_rate = total_wins / total_n
        edge = win_rate - 0.50
        flag = " <-- has edge" if win_rate > 0.52 else ""
        print(f"  {secs:9d}  {total_n:8d}  {win_rate:7.3f}  {edge:+12.3f}{flag}")


def print_delta_buckets(windows: list, min_n: int = 30):
    """
    Show: given BTC delta at T-120 or T-90, what % win?
    This is the raw signal strength independent of the model.
    """
    print("\n" + "=" * 72)
    print("RAW SIGNAL: BTC delta (%) at T-120s → actual Up win rate")
    print("(positive delta = BTC moved up; shows if momentum persists)")
    print("=" * 72)

    delta_stats: dict[float, dict] = defaultdict(lambda: {"n": 0, "ups": 0})

    for w in windows:
        if w["outcome"] == "Flat":
            continue
        for sig in compute_model_signals(w):
            if sig["seconds_remaining"] != 120:
                continue
            # bucket delta_pct into 0.1% bins
            db = round(round(sig["delta_pct"] / 0.1) * 0.1, 2)
            delta_stats[db]["n"] += 1
            if w["outcome"] == "Up":
                delta_stats[db]["ups"] += 1

    print(f"  {'Delta%':>8}  {'N':>6}  {'Actual Up%':>11}  {'Edge':>7}")
    print(f"  {'-'*8}  {'-'*6}  {'-'*11}  {'-'*7}")
    for db in sorted(delta_stats.keys()):
        d = delta_stats[db]
        if d["n"] < min_n:
            continue
        up_rate = d["ups"] / d["n"]
        print(f"  {db:+8.2f}  {d['n']:6d}  {up_rate:11.3f}  {up_rate - 0.50:+7.3f}")


def try_plot(stats: dict, windows: list):
    try:
        import matplotlib.pyplot as plt
        import numpy as np

        fig, axes = plt.subplots(1, 2, figsize=(14, 6))
        fig.suptitle("BTC 5-min Strategy: Calibration Analysis", fontsize=14)

        # Left: calibration curve (all time buckets combined)
        ax = axes[0]
        all_buckets: dict[float, dict] = defaultdict(lambda: {"n": 0, "wins": 0})
        for secs_data in stats.values():
            for b, d in secs_data.items():
                all_buckets[b]["n"] += d["n"]
                all_buckets[b]["wins"] += d["wins"]

        xs, ys, errs = [], [], []
        for b in sorted(all_buckets.keys()):
            d = all_buckets[b]
            if d["n"] < 30:
                continue
            actual = d["wins"] / d["n"]
            stderr = math.sqrt(actual * (1 - actual) / d["n"])
            xs.append(b)
            ys.append(actual)
            errs.append(stderr)

        ax.plot([0.4, 0.9], [0.4, 0.9], "k--", alpha=0.4, label="Perfect calibration")
        ax.errorbar(xs, ys, yerr=errs, fmt="o-", capsize=4, color="steelblue", label="Actual")
        ax.set_xlabel("Model estimated P(win)")
        ax.set_ylabel("Actual win rate")
        ax.set_title("Calibration Curve")
        ax.legend()
        ax.grid(True, alpha=0.3)
        ax.set_xlim(0.38, 0.92)
        ax.set_ylim(0.38, 0.92)

        # Right: win rate by seconds remaining
        ax2 = axes[1]
        secs_list, win_rates = [], []
        for secs in sorted(stats.keys(), reverse=True):
            total_n = sum(d["n"] for d in stats[secs].values())
            total_wins = sum(d["wins"] for d in stats[secs].values())
            if total_n < 30:
                continue
            secs_list.append(secs)
            win_rates.append(total_wins / total_n)

        ax2.bar(range(len(secs_list)), [w - 0.5 for w in win_rates], color=[
            "green" if w > 0.5 else "red" for w in win_rates
        ], alpha=0.7)
        ax2.axhline(0, color="black", linewidth=0.8)
        ax2.set_xticks(range(len(secs_list)))
        ax2.set_xticklabels([str(s) for s in secs_list], rotation=45)
        ax2.set_xlabel("Seconds remaining")
        ax2.set_ylabel("Win rate − 50%")
        ax2.set_title("Edge by Entry Time")
        ax2.grid(True, alpha=0.3)

        plt.tight_layout()
        out = "calibration_plot.png"
        plt.savefig(out, dpi=150)
        print(f"\n  Plot saved → {out}")
        plt.show()
    except ImportError:
        print("\n  (matplotlib not installed — skipping plot)")
    except Exception as e:
        print(f"\n  (plot failed: {e})")


# ── Main ────────────────────────────────────────────────────────────────────

def build_2d_table(windows: list, min_n: int = 20) -> dict:
    """
    Builds the empirical lookup table used in strategy_v2._estimate_prob_empirical.
    Returns: {seconds_remaining: {delta_bucket: prob_up}}
    delta_bucket is rounded to nearest 0.05% (e.g. -0.20, -0.15, ..., +0.20)
    """
    TARGET_SECS = [270, 240, 210, 180, 150, 120, 90, 60, 30]
    DELTA_STEP = 0.05  # % bucket width

    # secs → delta_bucket → {n, ups}
    raw: dict = {s: defaultdict(lambda: {"n": 0, "ups": 0}) for s in TARGET_SECS}

    for w in windows:
        if w["outcome"] == "Flat":
            continue
        is_up = w["outcome"] == "Up"
        for sig in compute_model_signals(w):
            s = sig["seconds_remaining"]
            if s not in raw:
                continue
            db = round(round(sig["delta_pct"] / DELTA_STEP) * DELTA_STEP, 2)
            raw[s][db]["n"] += 1
            if is_up:
                raw[s][db]["ups"] += 1

    # Convert to prob_up, filtering low-sample cells
    table = {}
    for s in TARGET_SECS:
        table[s] = {}
        for db, d in raw[s].items():
            if d["n"] >= min_n:
                table[s][db] = round(d["ups"] / d["n"], 4)

    return table


def print_2d_table(table: dict):
    """Print the 2D lookup table in copy-paste format for strategy_v2.py."""
    all_deltas = sorted({db for s_data in table.values() for db in s_data.keys()})
    secs_list = sorted(table.keys(), reverse=True)

    print("\n" + "=" * 72)
    print("2D EMPIRICAL TABLE  (seconds_remaining × delta% → P(Up wins))")
    print("=" * 72)
    header = f"  {'delta%':>8}" + "".join(f"  {s:>4}s" for s in secs_list)
    print(header)
    print("  " + "-" * (len(header) - 2))
    for db in all_deltas:
        row = f"  {db:+8.2f}"
        for s in secs_list:
            val = table[s].get(db)
            row += f"  {val:5.3f}" if val is not None else "      -"
        print(row)

    print("\n# ── Python dict for strategy_v2.py ─────────────────────────────────")
    print("_EMPIRICAL_TABLE = {")
    for s in secs_list:
        items = ", ".join(
            f"{db:.2f}: {p:.4f}"
            for db, p in sorted(table[s].items())
        )
        print(f"    {s}: {{{items}}},")
    print("}")


def main():
    parser = argparse.ArgumentParser(description="Calibrate BTC 5-min probability model")
    parser.add_argument("--days", type=int, default=30, help="Days of history (default 30)")
    parser.add_argument("--plot", action="store_true", help="Generate matplotlib plot")
    parser.add_argument("--min-n", type=int, default=30, help="Min samples per bucket (default 30)")
    parser.add_argument("--table", action="store_true", help="Output 2D delta×secs lookup table")
    args = parser.parse_args()

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - args.days * 24 * 3600 * 1000

    print(f"Fetching {args.days} days of BTC/USDT 1m klines from Binance...")
    klines = fetch_klines("BTCUSDT", "1m", start_ms, now_ms)
    print(f"  Got {len(klines):,} 1-minute bars")

    windows = build_windows(klines)
    non_flat = [w for w in windows if w["outcome"] != "Flat"]
    up_count = sum(1 for w in non_flat if w["outcome"] == "Up")
    print(f"  {len(windows):,} 5-min windows  ({len(non_flat):,} non-flat)")
    print(f"  Base rate: {up_count}/{len(non_flat)} = {up_count/len(non_flat):.3f} Up")

    if args.table:
        table = build_2d_table(windows, min_n=20)
        print_2d_table(table)
        print("\nDone.")
        return

    stats = run_calibration(windows)

    print_calibration_table(stats, min_n=args.min_n)
    print_summary(stats, min_n=args.min_n)
    print_delta_buckets(windows, min_n=args.min_n)

    if args.plot:
        try_plot(stats, windows)

    print("\nDone.")


if __name__ == "__main__":
    main()
