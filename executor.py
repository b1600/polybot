# executor.py
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY
from dotenv import load_dotenv
import os

load_dotenv()

def create_client():
    return ClobClient(
        host=os.getenv("CLOB_HOST"),
        key=os.getenv("POLY_PRIVATE_KEY"),
        chain_id=137,
        signature_type=int(os.getenv("POLY_SIGNATURE_TYPE", "0")),
        funder=os.getenv("POLY_FUNDER_ADDRESS"),
    )

def init_client():
    client = create_client()
    client.set_api_creds(client.create_or_derive_api_creds())
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

def place_market_order(client, token_id, amount):
    """
    Place a FOK (Fill-or-Kill) market order.
    This pays taker fees but guarantees immediate execution.
    Use when you need speed (e.g., last few seconds of window).
    amount is in USD (USDC).
    """
    market_args = MarketOrderArgs(
        token_id=token_id,
        amount=amount,
        side=BUY,
        order_type=OrderType.FOK,
    )
    signed_order = client.create_market_order(market_args)
    resp = client.post_order(signed_order, OrderType.FOK)
    return resp

def cancel_order(client, order_id):
    """Cancel a resting maker order."""
    return client.cancel(order_id)

def cancel_all(client):
    """Cancel all open orders."""
    return client.cancel_all()
