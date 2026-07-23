# Vouch

Due diligence for hiring agents on OKX.AI.

Vouch checks any agent before you hire: onchain identity, wallet history,
review authenticity, and endpoint health. Returns a sourced risk report
where every claim links to a transaction, timestamp, or live probe.

## Signals

Deterministic. The score is a pure function of **evaluated + triggered**
signals. Checks that could not run are `not_evaluated` (never a silent pass).
The LLM never touches the numbers.

Active collectors (12):

- Listing age, security rating, sales history, online status
- Owner wallet outbound activity + reuse across listings (X Layer RPC)
- Wallet age under 7d (only when explorer first-seen is available; else not_evaluated)
- Self-review detection (reviewer wallet == owner wallet)
- Coordinated review timing (internal id `sybil_review_pattern`: ≥5 low-activity reviewers in a 2h window — pattern only, not intent)
- Review data integrity (distribution vs list vs total)
- Live endpoint probe: unpaid GET, then POST if needed (POST-only 402 is healthy)

## Run

    pip install -r requirements.txt
    uvicorn app.main:app --reload

## Payment

x402 exact + aggr_deferred on X Layer (eip155:196), settled via the OKX
facilitator. Unpaid calls receive a 402 with payment requirements.

### Request body (paid POST)

```json
{ "agent_id": "6086" }
```

Server resolves marketplace identity when a wallet session is available.

**Recommended client shape** (always works — use this from user agents so a
missing server JWT cannot brick paid calls):

```json
{
  "agent_id": "6086",
  "agent": { "...get-agents snapshot..." },
  "reviews": { "...feedback-list snapshot..." }
}
```

If `agent_id` resolve fails (marketplace code 10008), Vouch falls back to the
`agent` / `reviews` objects in the body and still settles after a successful
report.

### Env

See `.env.example`. Facilitator needs `OKX_API_KEY` / `OKX_SECRET_KEY` /
`OKX_PASSPHRASE`. Optional `OKX_ACCESS_TOKEN` for server-side agent-list.
`GET /health` → `marketplace_resolve` shows whether agent_id-only resolve works.
