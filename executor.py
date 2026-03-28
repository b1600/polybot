# executor.py
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, MarketOrderArgs, OrderType, BalanceAllowanceParams, AssetType
from py_clob_client.order_builder.constants import BUY
from dotenv import load_dotenv
import logging
import os

load_dotenv()

log = logging.getLogger("executor")


def get_usdc_balance(client) -> float:
    """Return the Polymarket CLOB trading balance in USDC.

    Do NOT call update_balance_allowance before this. That endpoint causes
    the CLOB server to overwrite its cached deposit balance with the raw
    on-chain USDC value in base units (6 decimals), e.g. 0.22 USDC becomes
    216791 raw units which the code then misreads as $216,791.

    Polymarket normally returns the balance as a decimal dollar string like
    "5.170000". If update_balance_allowance has previously been called and
    the cache is stale, the value may come back as a bare integer string
    ("216791"). We detect this case and convert.
    """
    resp = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    raw = resp.get("balance", 0)
    value = float(raw)
    # Detect raw USDC base-unit format: no decimal point and suspiciously large.
    # "5.170000"  → has '.'  → already in dollars, no conversion needed.
    # "216791"    → no '.'   → base units with 6 decimals → divide by 1e6.
    if value > 1000 and "." not in str(raw):
        converted = value / 1_000_000
        log.warning(
            f"Balance field '{raw}' looks like raw USDC base units "
            f"(caused by a prior update_balance_allowance call) — "
            f"converting to ${converted:.6f}"
        )
        return converted
    return value

def init_client():
    creds = ApiCreds(
        api_key=os.getenv("POLY_API_KEY"),
        api_secret=os.getenv("POLY_API_SECRET"),
        api_passphrase=os.getenv("POLY_API_PASSPHRASE"),
    )
    client = ClobClient(
        host=os.getenv("CLOB_HOST"),
        key=os.getenv("POLY_PRIVATE_KEY"),
        chain_id=137,
        signature_type=int(os.getenv("POLY_SIGNATURE_TYPE", "0")),
        funder=os.getenv("POLY_FUNDER_ADDRESS"),
        creds=creds,
    )
    return client

def place_maker_order(client, token_id, price, size):
    """
    Place a GTC limit (maker) order.
    Maker orders earn rebates instead of paying taker fees.
    The SDK handles feeRateBps automatically.
    """
    order_args = OrderArgs(
        price=price,
        size=size,        # number of shares
        side=BUY,
        token_id=token_id,
    )
    signed_order = client.create_order(order_args)
    resp = client.post_order(signed_order, OrderType.GTC)
    return resp

def get_ask_depth(client, token_id) -> list:
    """
    Fetch the current ask side of the order book.
    Returns a list of OrderSummary(price, size) or [] if empty/unavailable.
    Used to pre-check liquidity before placing a SCALP order.
    """
    try:
        book = client.get_order_book(token_id)
        if book and book.asks:
            return book.asks
    except Exception as e:
        log.warning(f"Order book fetch failed: {e}")
    return []


def place_market_order(client, token_id, amount, price=0):
    """
    Place a FAK (Fill-and-Kill = IOC) market order.
    Fills as much as possible at or below `price`, cancels the rest.
    Accepts partial fills — better than FOK when liquidity is thin.

    price=0  → SDK auto-calculates from the live order book (sweeps best ask).
    price>0  → acts as a worst-case price cap (for walk-up retries).
    amount is in USD (USDC).
    """
    market_args = MarketOrderArgs(
        token_id=token_id,
        amount=amount,
        side=BUY,
        order_type=OrderType.FAK,
        price=price,
    )
    signed_order = client.create_market_order(market_args)
    resp = client.post_order(signed_order, OrderType.FAK)
    return resp

def cancel_order(client, order_id):
    """Cancel a resting maker order. Raises if the API did not confirm cancellation."""
    resp = client.cancel(order_id)
    if not resp or order_id not in resp.get("canceled", []):
        raise RuntimeError(f"Cancel may have failed for order {order_id}: {resp}")
    return resp

def cancel_all(client):
    """Cancel all open orders."""
    return client.cancel_all()
