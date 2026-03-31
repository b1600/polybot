# market_discovery.py
import time
import json
import requests

GAMMA_API = "https://gamma-api.polymarket.com"

def get_current_window():
    """Calculate the current 5-minute window timestamps."""
    now = int(time.time())
    window_start = now - (now % 300)
    window_end = window_start + 300
    return window_start, window_end

def get_next_window():
    """Calculate the NEXT 5-minute window (the one to trade)."""
    now = int(time.time())
    current_start = now - (now % 300)
    next_start = current_start + 300
    next_end = next_start + 300
    return next_start, next_end

def build_slug(window_start):
    """Build the Polymarket slug for a 5-min BTC market."""
    return f"btc-updown-5m-{window_start}"

def fetch_market(slug):
    """Fetch market data from the Gamma API. Returns token IDs and prices."""
    url = f"{GAMMA_API}/events"
    params = {"slug": slug}
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    events = resp.json()

    if not events:
        return None

    event = events[0]
    markets = event.get("markets", [])

    result = {}
    for market in markets:
        # Each market has outcomes like ["Up", "Down"]
        # and clobTokenIds as a JSON string: '["token_up", "token_down"]'
        outcomes = json.loads(market.get("outcomes", "[]"))
        token_ids = json.loads(market.get("clobTokenIds", "[]"))
        prices = json.loads(market.get("outcomePrices", "[]"))

        condition_id = market.get("conditionId")
        active = market.get("active", False)
        accepting = market.get("acceptingOrders", False)

        for i, outcome in enumerate(outcomes):
            result[outcome] = {
                "token_id": token_ids[i] if i < len(token_ids) else None,
                "price": float(prices[i]) if i < len(prices) else None,
                "outcome_index": i,  # 0 → indexSet 1, 1 → indexSet 2 for CTF redemption
            }
        result["condition_id"] = condition_id
        result["active"] = active
        result["accepting_orders"] = accepting

    return result
