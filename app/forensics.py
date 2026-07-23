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
    """Detect self-reviews, data inconsistency. Coordinated review timing
    is a separate onchain step (analyze_review_funding).
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


def _review_time_ms(r: dict) -> int | None:
    """Normalize review timestamp to ms. feedback-list uses `time` in ms."""
    t = r.get("time") or r.get("createdAt") or r.get("timestamp")
    if t is None:
        return None
    try:
        t = int(t)
    except (TypeError, ValueError):
        return None
    # seconds vs ms
    if t < 10_000_000_000:
        t *= 1000
    return t


async def analyze_review_funding(client: httpx.AsyncClient, owner_address: str,
                                 reviews: list[dict], rpc_url: str) -> list:
    """Coordinated review timing via **clustering**, not single-use wallets alone.

    Internal signal id remains `sybil_review_pattern` for scoring continuity.
    Public evidence claims describe the pattern only — no fraud/intent language.

    X Layer is young — nearly every wallet is low-nonce. A single-use
    reviewer is normal. The check fires only on structure:

    - ≥5 distinct low-activity reviewers (nonce ≤ 1) inside a 2-hour window.

    Without enough reviewers, timestamps, or RPC nonces → not_evaluated
    (never a silent clean pass on incomplete data).
    """
    fetched = _now_iso()
    owner = (owner_address or "").lower()

    # Deduplicate by reviewer, keep earliest review time per address
    by_addr: dict[str, dict] = {}
    for r in reviews:
        addr = (r.get("reviewerAddress") or "").lower().strip()
        if not addr or addr == owner:
            continue
        t = _review_time_ms(r)
        prev = by_addr.get(addr)
        if prev is None or (t is not None and (prev.get("time") is None or t < prev["time"])):
            by_addr[addr] = {"address": addr, "time": t, "raw": r}

    addrs = list(by_addr.values())
    if len(addrs) < 3:
        return [build_signal(
            name="sybil_review_pattern",
            triggered=False,
            severity=Severity.high,
            evaluated=False,
            evidence=[Evidence(
                claim=(
                    "Coordinated review timing not checked: need ≥3 distinct "
                    f"non-owner reviewers, have {len(addrs)}"
                ),
                source_type="gap",
                source_ref=owner_address or "unknown",
                fetched_at=fetched,
            )],
        )]

    times_present = sum(1 for a in addrs if a["time"] is not None)
    if times_present < 3:
        return [build_signal(
            name="sybil_review_pattern",
            triggered=False,
            severity=Severity.high,
            evaluated=False,
            evidence=[Evidence(
                claim=(
                    "Coordinated review timing not checked: need ≥3 review "
                    f"timestamps for clustering, have {times_present}"
                ),
                source_type="gap",
                source_ref=owner_address or "unknown",
                fetched_at=fetched,
            )],
        )]

    # Nonces for up to 20 reviewers
    sample = addrs[:20]
    low_activity: list[dict] = []
    sampled = 0
    for item in sample:
        try:
            payload = {"jsonrpc": "2.0", "method": "eth_getTransactionCount",
                       "params": [item["address"], "latest"], "id": 1}
            r = await client.post(rpc_url, json=payload, timeout=10)
            nonce = int(r.json().get("result", "0x0"), 16)
            sampled += 1
            item["nonce"] = nonce
            if nonce <= 1:
                low_activity.append(item)
        except Exception:
            continue

    if sampled < 3:
        return [build_signal(
            name="sybil_review_pattern",
            triggered=False,
            severity=Severity.high,
            evaluated=False,
            evidence=[Evidence(
                claim=(
                    "Coordinated review timing not checked: RPC nonce fetch "
                    f"failed (sampled {sampled})"
                ),
                source_type="gap",
                source_ref=owner_address or "unknown",
                fetched_at=fetched,
            )],
        )]

    # Tight time cluster among low-activity reviewers (2h = 7_200_000 ms)
    WINDOW_MS = 2 * 60 * 60 * 1000
    timed_low = sorted(
        [x for x in low_activity if x.get("time") is not None],
        key=lambda x: x["time"],
    )
    best_cluster = 0
    cluster_span_h = None
    for i in range(len(timed_low)):
        j = i
        while j < len(timed_low) and timed_low[j]["time"] - timed_low[i]["time"] <= WINDOW_MS:
            j += 1
        size = j - i
        if size > best_cluster:
            best_cluster = size
            if size >= 2:
                cluster_span_h = (timed_low[j - 1]["time"] - timed_low[i]["time"]) / 3_600_000

    n_reviews = len(addrs)
    n_low = len(low_activity)
    # Dense burst only: ≥5 distinct low-activity reviewers inside 2h.
    share = best_cluster / max(n_reviews, 1)
    burst = best_cluster >= 5 and n_low >= 5

    if burst:
        claim = (
            f"Coordinated review burst — {best_cluster} reviewers with minimal "
            f"wallet activity (nonce≤1) posted within a 2h window"
            f"{f' (span {cluster_span_h:.2f}h)' if cluster_span_h is not None else ''}. "
            f"{n_low}/{sampled} sampled reviewers are low-activity; "
            f"{n_reviews} distinct non-owner reviewers total "
            f"(cluster share {share:.0%}). Pattern only — not an assertion of intent."
        )
    else:
        claim = (
            f"No coordinated review timing pattern: {n_low}/{sampled} low-activity "
            f"reviewers, largest 2h low-activity cluster size={best_cluster} "
            f"(threshold: ≥5 distinct low-activity reviewers in a 2h window). "
            f"Single-use wallets alone are not treated as coordinated timing."
        )

    return [build_signal(
        name="sybil_review_pattern",
        triggered=burst,
        severity=Severity.high,
        evidence=[Evidence(
            claim=claim,
            source_type="rpc",
            source_ref=owner_address or "unknown",
            fetched_at=fetched,
        )],
    )]


# ---------------------------------------------------------------------------
# Endpoint probing — unpaid GET, then POST if needed
# ---------------------------------------------------------------------------

def _payment_header(r: httpx.Response) -> bool:
    return bool(
        r.headers.get("payment-required")
        or r.headers.get("PAYMENT-REQUIRED")
        or r.headers.get("x-payment-required")
    )


def _is_healthy_status(status: int) -> bool:
    # 402 = paid resource challenge; 200 = free/preview ok
    return status in (200, 402)


async def probe_endpoint(client: httpx.AsyncClient, endpoint_url: str) -> list:
    """Probe unpaid GET, then unpaid POST if GET is not a valid challenge.

    Only flag:
    - endpoint_dead: neither method produces an HTTP response
    - endpoint_wrong_status: responses received but neither is 200/402

    POST-only A2MCP routes that 405 on GET and 402 on POST are healthy.
    """
    fetched = _now_iso()
    results: list[tuple[str, int | None, str | None, bool]] = []
    # (method, status, error, has_payment_header)

    async def _try(method: str) -> tuple[int | None, str | None, bool]:
        try:
            if method == "GET":
                r = await client.get(endpoint_url, timeout=15)
            else:
                r = await client.post(
                    endpoint_url,
                    json={},
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                    timeout=15,
                )
            return r.status_code, None, _payment_header(r)
        except Exception as e:
            return None, str(e)[:180], False

    get_status, get_err, get_pr = await _try("GET")
    results.append(("GET", get_status, get_err, get_pr))

    need_post = True
    if get_status is not None and _is_healthy_status(get_status):
        need_post = False  # healthy on GET; still optional to skip POST

    post_status, post_err, post_pr = None, None, False
    if need_post:
        post_status, post_err, post_pr = await _try("POST")
        results.append(("POST", post_status, post_err, post_pr))

    any_response = any(s is not None for _, s, _, _ in results)
    healthy_methods = [
        (m, s, pr) for m, s, err, pr in results
        if s is not None and _is_healthy_status(s)
    ]
    parts = []
    for m, s, err, pr in results:
        if err:
            parts.append(f"{m}: error ({err})")
        else:
            pr_note = ", payment-required header present" if pr else ""
            parts.append(f"{m}: HTTP {s}{pr_note}")
    detail = "; ".join(parts)

    if not any_response:
        return [
            build_signal(
                name="endpoint_dead",
                triggered=True,
                severity=Severity.high,
                evidence=[Evidence(
                    claim=f"Endpoint did not respond on GET or POST: {detail}",
                    source_type="live_probe",
                    source_ref=endpoint_url,
                    fetched_at=fetched,
                )],
            ),
            build_signal(
                name="endpoint_wrong_status",
                triggered=False,
                severity=Severity.medium,
                evaluated=False,
                evidence=[Evidence(
                    claim="Status check not applicable: endpoint unreachable",
                    source_type="gap",
                    source_ref=endpoint_url,
                    fetched_at=fetched,
                )],
            ),
        ]

    if healthy_methods:
        hm = ", ".join(f"{m}→{s}" for m, s, _ in healthy_methods)
        claim = f"Endpoint healthy via {hm}. Probe detail: {detail}"
        return [
            build_signal(
                name="endpoint_dead",
                triggered=False,
                severity=Severity.high,
                evidence=[Evidence(
                    claim=claim,
                    source_type="live_probe",
                    source_ref=endpoint_url,
                    fetched_at=fetched,
                )],
            ),
            build_signal(
                name="endpoint_wrong_status",
                triggered=False,
                severity=Severity.medium,
                evidence=[Evidence(
                    claim=claim,
                    source_type="live_probe",
                    source_ref=endpoint_url,
                    fetched_at=fetched,
                )],
            ),
        ]

    # Responses but no valid challenge
    claim = (
        f"No valid unpaid challenge (need HTTP 200 or 402 on GET or POST). "
        f"Probe detail: {detail}"
    )
    return [
        build_signal(
            name="endpoint_dead",
            triggered=False,
            severity=Severity.high,
            evidence=[Evidence(
                claim=f"Endpoint responded but challenge invalid. {detail}",
                source_type="live_probe",
                source_ref=endpoint_url,
                fetched_at=fetched,
            )],
        ),
        build_signal(
            name="endpoint_wrong_status",
            triggered=True,
            severity=Severity.medium,
            evidence=[Evidence(
                claim=claim,
                source_type="live_probe",
                source_ref=endpoint_url,
                fetched_at=fetched,
            )],
        ),
    ]
