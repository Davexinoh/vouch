# State of the Agent Economy — Marketplace Sweep Summary

_Generated: 2026-07-23T07:39:57.474676+00:00_  
_Source cache: `marketplace_sweep_cache.json`_  
_Raw table: `sweep_raw.csv` (all aggregates traceable row-by-row)_  
_RPC: `https://rpc.xlayer.tech` — **reachable**_

## Sample

- **n agents swept:** 25
- Selection: top listed marketplace agents by `soldCount` (desc) via `onchainos agent search` merge + identity hydrate.

## Trust score distribution

- **min / median / max:** 60 / 100.0 / 100
- **mean:** 88.4
- Scored agents: 25 of 25

| Risk band | Count | % of scored |
| --- | ---: | ---: |
| low risk | 16 | 64% |
| moderate risk | 9 | 36% |
| elevated risk | 0 | 0% |
| high risk | 0 | 0% |

Bands: low ≥80 · moderate ≥55 · elevated ≥30 · high <30 (matches `app.scoring.score_band`).

## Signal frequency (ranked)

Percent of agents **where the signal was evaluated** that triggered it.

| Rank | Signal | Triggered | Evaluated | % of evaluated |
| ---: | --- | ---: | ---: | ---: |
| 1 | `sybil_review_pattern` | 7 | 18 (+6 not_evaluated) | 39% |
| 2 | `endpoint_wrong_status` | 3 | 20 | 15% |
| 3 | `review_data_inconsistent` | 3 | 25 | 12% |
| 4 | `security_rate_missing` | 1 | 25 | 4% |
| 5 | `offline_or_stale` | 1 | 25 | 4% |
| 6 | `listing_age_under_7d` | 0 | 25 | 0% |
| 7 | `zero_sales` | 0 | 25 | 0% |
| 8 | `zero_prior_payouts` | 0 | 25 | 0% |
| 9 | `self_review_detected` | 0 | 25 | 0% |
| 10 | `endpoint_dead` | 0 | 20 | 0% |
| 11 | `wallet_reused_across_listings` | 0 | 25 | 0% |

**Emitted only as not_evaluated (check did not run):** `wallet_age_under_7d` (25/25)

## Review-data inconsistencies

- **3** of 25 agents triggered `review_data_inconsistent` (12% of evaluated).

## Endpoints (dead / non-spec)

- Agents with a probeable endpoint URL: **20** of 25
- Agents with dead or non-spec-compliant endpoint signals and/or probe failures: **16** of 20 with endpoints

### Failure-type breakdown (extra live probe; may double-count agents)

| Failure type | Agent count |
| --- | ---: |
| `only_exact_scheme_advertised` | 10 |
| `405_on_GET` | 5 |
| `vet_signal_endpoint_wrong_status` | 3 |
| `GET_status_400` | 1 |
| `empty_or_missing_PAYMENT_REQUIRED_header` | 1 |
| `405_on_POST` | 1 |

Note: engine probe tries unpaid **GET**, then unpaid **POST** if GET is not 200/402. POST-only rails that return 402 on POST are scored clean. Extra failure-type table may still list raw GET 405s for diagnostics.

## Owner wallet reuse across listings

- **0** of 25 agents triggered `wallet_reused_across_listings` (map built from this sweep’s 25 unique owner addresses; 0 owners appear on ≥2 listings in-sample).

## Coverage honesty

- Wallet forensics (RPC nonce / age gap signals) ran for **25** of 25 agents.
- `sybil_review_pattern` evaluated for **24** of 25 (requires reviewer addresses + RPC).
- `wallet_reused_across_listings` evaluated for **25** of 25 (requires ownerAddress).
- Endpoint probe evaluated for **20** of 25 (requires services[].endpoint).

## Highest-scoring agents (top 5 by name)

| Rank | Name | Score | soldCount |
| ---: | --- | ---: | ---: |
| 1 | **Onchain Data Explorer** | 100 | 1602 |
| 2 | **CoinWM Open API** | 100 | 1566 |
| 3 | **CoinAnk OpenAPI** | 100 | 1560 |
| 4 | **Quiver** | 100 | 590 |
| 5 | **AgentFund** | 100 | 555 |

_Low-scoring agents are intentionally not named in this public summary._

## Surprises / notes

- Median trust score is relatively high (100) — many top-sold agents look clean on listing-meta + review signals alone.
- 3 agents show review distribution/list/total mismatches — marketplace review API shape is still noisy.
- 10 endpoints advertise **only `exact`** (no `aggr_deferred`) when a 402 body was parseable — agentic-wallet buyers may need the exact path.
- Sales are concentrated: max soldCount=10112 vs median=343 in this cohort.

---
_Read-only local sweep. No marketplace state changed._