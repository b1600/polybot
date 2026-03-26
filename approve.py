# approve.py — run once to approve USDC spending (EOA wallets only)
from web3 import Web3
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
import os

load_dotenv()

POLYGON_RPC = os.getenv("POLYGON_RPC", "https://polygon-mainnet.g.alchemy.com/v2/" + os.getenv("ALCHEMY_API_KEY", ""))
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # Bridged USDC (USDC.e) — what py_clob_client uses
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_ADAPTER = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
account = w3.eth.account.from_key(os.getenv("POLY_PRIVATE_KEY"))

USDC_ABI = [
    {"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"type":"bool"}],"type":"function"},
    {"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
]
usdc = w3.eth.contract(address=USDC_ADDRESS, abi=USDC_ABI)

print(f"Wallet address: {account.address}")
matic_balance = w3.eth.get_balance(account.address)
print(f"MATIC balance: {w3.from_wei(matic_balance, 'ether'):.4f} MATIC")

balance = usdc.functions.balanceOf(account.address).call()
print(f"USDC.e balance: {balance / 1e6:.2f} USDC")
for spender_name, spender in [("CTF_EXCHANGE", CTF_EXCHANGE), ("NEG_RISK_ADAPTER", NEG_RISK_ADAPTER)]:
    allow = usdc.functions.allowance(account.address, spender).call()
    print(f"Allowance for {spender_name}: {allow / 1e6:.2f} USDC")

MAX_UINT = 2**256 - 1
nonce = w3.eth.get_transaction_count(account.address, "latest")
print(f"Using confirmed nonce: {nonce}")
for spender in [CTF_EXCHANGE, NEG_RISK_ADAPTER]:
    tx = usdc.functions.approve(spender, MAX_UINT).build_transaction({
        "from": account.address,
        "nonce": nonce,
        "gas": 100000,
        "gasPrice": w3.to_wei("500", "gwei"),
    })
    nonce += 1
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"Approval tx: https://polygonscan.com/tx/{tx_hash.hex()} — waiting...")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
    status = "SUCCESS" if receipt.status == 1 else "FAILED (reverted)"
    print(f"  → {status} (block {receipt.blockNumber})")

print("\nFinal allowances after confirmation:")
for spender_name, spender in [("CTF_EXCHANGE", CTF_EXCHANGE), ("NEG_RISK_ADAPTER", NEG_RISK_ADAPTER)]:
    allow = usdc.functions.allowance(account.address, spender).call()
    print(f"  {spender_name}: {'unlimited' if allow > 2**200 else f'{allow/1e6:.2f} USDC'}")

# Notify Polymarket's backend to re-read on-chain balance/allowance
print("\nSyncing balance/allowance with Polymarket...")
from executor import init_client
clob = init_client()
resp = clob.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
print(f"  Polymarket USDC balance/allowance: {resp}")
resp2 = clob.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
print(f"  Confirmed state: {resp2}")
