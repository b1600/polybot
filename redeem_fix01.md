# Recommendations — Fix Polymarket Redemption Failures (grok 8 Apr 2026)

**Status**: All winning redemptions are failing with the 24-hour timeout message  
**Root cause**: Condition ID string mismatch between Gamma API (no `0x`) and Data API (always has `0x`)

---

## 1. Immediate Fix (one-line change)

Replace the `_redeem_background` function with this updated version:

```python
async def _redeem_background(
    self, condition_id: str, outcome_index: int, label: str, deadline: float
):
    """
    Background task: polls Data API every REDEEM_POLL_INTERVAL seconds until
    the position appears as redeemable, then executes on-chain redemption.
    """
    # === CRITICAL FIX: Normalize condition_id exactly like fetch_redeemable_positions does ===
    if condition_id:
        condition_id = condition_id.removeprefix("0x").removeprefix("0X").lower()
        condition_id = f"0x{condition_id}"

    funder = os.getenv("POLY_FUNDER_ADDRESS", "")
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            positions = await asyncio.to_thread(fetch_redeemable_positions, funder)
            
            # Debug log (remove after verification)
            log.debug(f"REDEEM | comparing: window={condition_id} | API={[p['condition_id'] for p in positions]}")

            match = next(
                (p for p in positions
                 if p["condition_id"].lower() == condition_id.lower()), None
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
                f"REDEEM | {label} | SUCCESS | Condition: …{condition_id[-8:]} | Tx: {tx_hash}"
            )
            return
        except Exception as e:
            log.error(f"REDEEM FAILED | {label} | Condition: {condition_id} | {e}")
            return  # unexpected error — don't retry

    log.error(
        f"REDEEM FAILED | {label} | oracle not resolved after 24h | "
        f"Condition: {condition_id}"
    )