"""Review forensics and endpoint probing. This is the Vouch edge.

Surface scanners read the star count. Vouch reads WHO left the stars and
whether the endpoint actually works. All signals derive from real fields
in the OKX.AI schema (verified live 2026-07-10).
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from .models import Evidence, Severity
from .scoring import build_signal


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Review forensics — reviewerAddress is public in feedback-list
# ---------------------------------------------------------------------------

def analyze_reviews(owner_address: str, reviews: list[dict],
                    distribution: dict, total_claimed: int) -> list:
    """Detect self-reviews, data inconsistency. Sybil funding trace is a
    separate onchain step (analyze_review_funding).
    """
    signals = []
    fetched = _now_iso()
    owner = (owner_address or "").lower()

    # self-review: any reviewer == owner
    self_reviews = [r for r in reviews
                    if (r.get("reviewerAddress") or "").lower() == owner and owner]
    signals.append(build_signal(
        name="self_review_detected",
        triggered=len(self_reviews) > 0,
        severity=Severity.critical,
        evidence=[Evidence(
            claim=f"{len(self_reviews)} review(s) left by the owner wallet itself",
            source_type="agent_id",
            source_ref=owner_address or "unknown",
            fetched_at=fetched,
        )] if self_reviews else [Evidence(
            claim="No reviews traced to the owner wallet",
            source_type="agent_id",
            source_ref=owner_address or "unknown",
            fetched_at=fetched,
        )],
    ))

    # data inconsistency: distribution total vs list length vs claimed total
    dist_total = sum(distribution.values()) if distribution else 0
    list_len = len(reviews)
    inconsistent = not (dist_total == list_len == total_claimed) and (
        dist_total > 0 or total_claimed > 0)
    signals.append(build_signal(
        name="review_data_inconsistent",
        triggered=inconsistent,
        severity=Severity.medium,
        evidence=[Evidence(
            claim=(f"Review counts disagree: distribution sums to {dist_total}, "
                   f"list has {list_len}, total claimed {total_claimed}"),
            source_type="feedback_list",
            source_ref=owner_address or "unknown",
            fetched_at=fetched,
        )],
    ))
    return signals


async def analyze_review_funding(client: httpx.AsyncClient, owner_address: str,
                                 reviewer_addresses: list[str], rpc_url: str) -> list:
    """Sybil pattern: reviewer wallets funded by the owner, or all created same day.
    Uses X Layer RPC. Conservative — only flags on strong signal.
    """
    signals = []
    fetched = _now_iso()

    # Minimal version: check how many reviewers have zero other activity
    # (fresh wallets that only exist to review). Deep funding-trace is v2.
    fresh = 0
    for addr in reviewer_addresses[:20]:  # cap RPC calls
        try:
            payload = {"jsonrpc": "2.0", "method": "eth_getTransactionCount",
                       "params": [addr, "latest"], "id": 1}
            r = await client.post(rpc_url, json=payload, timeout=10)
            nonce = int(r.json().get("result", "0x0"), 16)
            if nonce <= 1:  # only the review tx, nothing else
                fresh += 1
        except Exception:
            continue

    n = len(reviewer_addresses[:20])
    sybil = n >= 3 and fresh / max(n, 1) >= 0.7  # 70%+ single-use wallets
    signals.append(build_signal(
        name="sybil_review_pattern",
        triggered=sybil,
        severity=Severity.high,
        evidence=[Evidence(
            claim=f"{fresh} of {n} sampled reviewer wallets have near-zero activity "
                  f"(single-use pattern)",
            source_type="rpc",
            source_ref=owner_address or "unknown",
            fetched_at=fetched,
        )],
    ))
    return signals


# ---------------------------------------------------------------------------
# Endpoint probing — does the service actually work per x402 spec
# ---------------------------------------------------------------------------

async def probe_endpoint(client: httpx.AsyncClient, endpoint_url: str) -> list:
    """A live A2MCP service should answer an unpaid GET with 402 + payment terms.
    405 or dead means the listing sells something that does not respond.
    """
    signals = []
    fetched = _now_iso()

    status = None
    err = None
    try:
        r = await client.get(endpoint_url, timeout=15)
        status = r.status_code
    except Exception as e:
        err = str(e)

    if err is not None or status is None:
        signals.append(build_signal(
            name="endpoint_dead",
            triggered=True,
            severity=Severity.high,
            evidence=[Evidence(
                claim=f"Endpoint did not respond: {err or 'no status'}",
                source_type="live_probe",
                source_ref=endpoint_url,
                fetched_at=fetched,
            )],
        ))
        return signals

    # 402 is the healthy answer for a paid endpoint. 200 also acceptable (free preview).
    healthy = status in (200, 402)
    signals.append(build_signal(
        name="endpoint_wrong_status",
        triggered=not healthy,
        severity=Severity.medium,
        evidence=[Evidence(
            claim=f"Endpoint returned HTTP {status} to an unpaid probe "
                  f"({'healthy' if healthy else 'expected 402 payment challenge'})",
            source_type="live_probe",
            source_ref=endpoint_url,
            fetched_at=fetched,
        )],
    ))
    return signals
