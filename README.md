# Vouch

Due diligence for hiring agents on OKX.AI.

Vouch checks any agent before you hire: onchain identity, wallet history,
review authenticity, and endpoint health. Returns a sourced risk report
where every claim links to a transaction, timestamp, or live probe.

## Signals

Deterministic. The score is a pure function of triggered signals. The LLM
never touches the numbers.

- Listing age, security rating, sales history, online status
- Owner wallet age and reuse across listings (X Layer forensics)
- Self-review detection (reviewer wallet == owner wallet)
- Sybil review pattern (single-use reviewer wallets)
- Review data integrity (distribution vs list vs total)
- Live endpoint probe (valid x402 402 challenge, or dead/wrong-status)

## Run

    pip install -r requirements.txt
    uvicorn app.main:app --reload

## Payment

x402 exact scheme on X Layer (eip155:196), settled via the OKX facilitator.
Unpaid calls receive a 402 with payment requirements.
