# Redeem — All Related Code

## Constants (`bot.py`)

```python
RESOLUTION_WAIT = 480    # seconds after window close before first redeem attempt (8 min — oracle typically resolves in 5–15 min)
REDEEM_POLL_INTERVAL = 600   # seconds between Data API polls for oracle readiness (10 min)
REDEEM_MAX_AGE = 86400       # give up on a pending redemption after 24h
```

---

## `executor.py` — On-chain redemption helpers

### NegRisk Adapter config

```python
# Polymarket 5-min BTC markets use the NegRisk system.
# After a market resolves, winning tokens must be redeemed on-chain
# via the NegRiskAdapter to return USDC.e to the proxy wallet.
_NEG_RISK_ADAPTER = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

_REDEEM_ABI = [
    {
        "inputs": [
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]
```

### `fetch_redeemable_positions(funder_address)`

```python
def fetch_redeemable_positions(funder_address: str) -> list[dict]:
    """
    Query the Polymarket Data API for all positions where the oracle has resolved
    and tokens are still held (i.e. ready to redeem).

    Returns a list of dicts with keys: condition_id, outcome_index, size, title.
    Returns [] on any error so callers can proceed safely.
    """
    import requests
    try:
        resp = requests.get(
            "https://data-api.polymarket.com/positions",
            params={"user": funder_address, "redeemable": "true", "sizeThreshold": 0},
            timeout=15,
        )
        if resp.status_code in (429, 1015):
            log.warning("fetch_redeemable_positions: Data API rate limited")
            return []
        positions = resp.json()
    except Exception as e:
        log.warning(f"fetch_redeemable_positions failed: {e}")
        return []

    result = []
    for p in positions:
        if float(p.get("size", 0)) <= 0:
            continue
        cid = p.get("conditionId", p.get("condition_id", ""))
        if not cid:
            continue
        if not cid.startswith("0x"):
            cid = "0x" + cid
        result.append({
            "condition_id": cid,
            "outcome_index": int(p.get("outcomeIndex", 0)),
            "size": float(p.get("size", 0)),
            "title": p.get("title", cid[:12]),
        })
    return result
```

### `redeem_positions(condition_id, outcome_index)`

```python
def redeem_positions(condition_id: str, outcome_index: int) -> str:
    """
    Redeem winning NegRisk CTF tokens for USDC.e on-chain. Single attempt.

    Call this only after confirming the oracle has resolved via
    fetch_redeemable_positions() — the Data API is the readiness signal.

    condition_id   : hex string (with or without 0x prefix) from Gamma API
    outcome_index  : 0 for the first outcome (e.g. "Up"), 1 for the second ("Down")
                     Maps to CTF indexSet: 0 → 1, 1 → 2

    Raises RuntimeError if the tx reverts on-chain.
    Returns the confirmed transaction hash on success.
    """
    from web3 import Web3

    rpc = os.getenv(
        "POLYGON_RPC",
        "https://polygon-mainnet.g.alchemy.com/v2/" + os.getenv("ALCHEMY_API_KEY", ""),
    )
    w3 = Web3(Web3.HTTPProvider(rpc))
    account = w3.eth.account.from_key(os.getenv("POLY_PRIVATE_KEY"))

    adapter = w3.eth.contract(
        address=Web3.to_checksum_address(_NEG_RISK_ADAPTER),
        abi=_REDEEM_ABI,
    )

    condition_bytes = bytes.fromhex(condition_id.removeprefix("0x"))
    index_set = 1 << outcome_index  # outcome 0 → 1, outcome 1 → 2

    nonce = w3.eth.get_transaction_count(account.address, "pending")
    gas_price = int(w3.eth.gas_price * 1.2)  # 20% above current base fee

    tx = adapter.functions.redeemPositions(
        condition_bytes, [index_set]
    ).build_transaction({
        "from": account.address,
        "nonce": nonce,
        "gas": 250_000,
        "gasPrice": gas_price,
    })
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    if receipt.status == 1:
        return tx_hash.hex()

    raise RuntimeError(
        f"redeemPositions reverted — "
        f"condition: {condition_id} indexSet: {index_set} tx: {tx_hash.hex()}"
    )
```

---

## `bot.py` — Redemption orchestration

### `_redeem_wins()` — spawns background redeem tasks after a window

```python
async def _redeem_wins(self):
    """
    For each winning trade this window, spawn a background task that polls
    the Polymarket Data API until the oracle resolves, then redeems on-chain.

    Returns immediately — the window loop is not blocked. Each background
    task retries every REDEEM_POLL_INTERVAL seconds for up to REDEEM_MAX_AGE.
    """
    if self.dry_run or not self.window:
        return

    condition_id = self.window.condition_id
    if not condition_id:
        log.warning("REDEEM | No condition_id cached — skipping redemption sweep")
        return

    winning_trades = [
        t for t in self.window.trades
        if t.get("outcome") == "win" and t.get("outcome_index") is not None
    ]

    if not winning_trades:
        return

    seen: set[int] = set()
    deadline = time.time() + REDEEM_MAX_AGE
    for trade in winning_trades:
        outcome_index = trade["outcome_index"]
        if outcome_index in seen:
            continue
        seen.add(outcome_index)
        label = f"{trade.get('strategy', '?')} {trade['side']}"
        asyncio.create_task(
            self._redeem_background(condition_id, outcome_index, label, deadline)
        )
```

### `_redeem_background()` — polls oracle and executes on-chain redemption

```python
async def _redeem_background(
    self, condition_id: str, outcome_index: int, label: str, deadline: float
):
    """
    Background task: polls Data API every REDEEM_POLL_INTERVAL seconds until
    the position appears as redeemable, then executes on-chain redemption.
    Gives up when deadline (REDEEM_MAX_AGE from trade time) is reached.
    """
    funder = os.getenv("POLY_FUNDER_ADDRESS", "")
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            positions = await asyncio.to_thread(fetch_redeemable_positions, funder)
            match = next(
                (p for p in positions
                 if p["condition_id"].lower() == condition_id.lower()),
                None,
            )
            if match is None:
                log.warning(
                    f"REDEEM | {label} | attempt {attempt} | "
                    f"oracle not yet resolved, retry in {REDEEM_POLL_INTERVAL // 60}m"
                )
                await asyncio.sleep(REDEEM_POLL_INTERVAL)
                continue

            tx_hash = await asyncio.to_thread(
                redeem_positions, condition_id, outcome_index
            )
            log.info(
                f"REDEEM | {label} | attempt {attempt} | "
                f"Condition: …{condition_id[-8:]} | Tx: {tx_hash}"
            )
            return
        except Exception as e:
            log.error(f"REDEEM FAILED | {label} | Condition: {condition_id} | {e}")
            return  # unexpected error — don't retry

    log.error(
        f"REDEEM FAILED | {label} | oracle not resolved after 24h | "
        f"Condition: {condition_id}"
    )
```

### `_startup_redeem_sweep()` — recovers stranded positions on bot startup

```python
async def _startup_redeem_sweep(self):
    """
    On bot startup, redeem any positions the Data API reports as already
    redeemable. Recovers positions stranded by exhausted retries, crashes,
    or restarts in previous sessions.
    """
    if self.dry_run:
        return
    funder = os.getenv("POLY_FUNDER_ADDRESS", "")
    if not funder:
        return
    try:
        positions = await asyncio.to_thread(fetch_redeemable_positions, funder)
        if not positions:
            return
        log.info(f"STARTUP SWEEP | Found {len(positions)} redeemable position(s)")
        for p in positions:
            try:
                tx_hash = await asyncio.to_thread(
                    redeem_positions, p["condition_id"], p["outcome_index"]
                )
                log.info(
                    f"STARTUP SWEEP | Redeemed {p['title']} | Tx: {tx_hash}"
                )
            except Exception as e:
                log.error(
                    f"STARTUP SWEEP | Failed to redeem {p['title']}: {e}"
                )
    except Exception as e:
        log.error(f"STARTUP SWEEP | Error: {e}")
```

---

## Flow Summary

```
bot startup
  └─ _startup_redeem_sweep()          ← redeem any already-resolved positions

after each window resolves
  └─ _redeem_wins()                   ← for each winning trade outcome_index
       └─ _redeem_background()        ← background task, polls every 10 min
            ├─ fetch_redeemable_positions()   ← Data API check
            └─ redeem_positions()             ← on-chain tx via NegRiskAdapter
```
