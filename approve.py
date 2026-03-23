# approve.py — run once to approve USDC spending (EOA wallets only)
from web3 import Web3
from dotenv import load_dotenv
import os

load_dotenv()

POLYGON_RPC = "https://polygon-rpc.com"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_ADAPTER = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
account = w3.eth.account.from_key(os.getenv("POLY_PRIVATE_KEY"))

USDC_ABI = [{"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"type":"bool"}],"type":"function"}]
usdc = w3.eth.contract(address=USDC_ADDRESS, abi=USDC_ABI)

MAX_UINT = 2**256 - 1
for spender in [CTF_EXCHANGE, NEG_RISK_ADAPTER]:
    tx = usdc.functions.approve(spender, MAX_UINT).build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address),
        "gas": 100000,
        "gasPrice": w3.to_wei("50", "gwei"),
    })
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"Approval tx: {tx_hash.hex()}")
