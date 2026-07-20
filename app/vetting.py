"""The vet_agent orchestrator. Pulls facts, runs all analyzers, scores."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from .collectors import fetch_wallet_facts, analyze_wallet
from .forensics import (analyze_reviews, analyze_review_funding,
                        probe_endpoint)
from .models import VetReport
from .scoring import compute_trust_score, score_band, build_signal
from .models import Evidence, Severity


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Minimum fields an agent object must carry to be a real vetting subject.
# Without them the listing-meta signals fire on absent defaults (security
# missing, zero sales, offline) and produce a score computed from nothing --
# which is worse than no score. See _has_real_data / vet_agent.
REQUIRED_AGENT_FIELDS = ("agentId", "ownerAddress", "createdAt")


def _has_real_data(agent: dict) -> bool:
    """True only if the agent object carries real identifying data to score."""
    return all(agent.get(f) not in (None, "") for f in REQUIRED_AGENT_FIELDS)


def analyze_listing_meta(agent: dict) -> list:
    """Signals from the identity record itself. All fields verified real."""
    signals = []
    fetched = _now_iso()
    now_ms = datetime.now(timezone.utc).timestamp() * 1000

    created = agent.get("createdAt")
    if created:
        age_days = (now_ms - created) / 86_400_000
        signals.append(build_signal(
            name="listing_age_under_7d",
            triggered=age_days < 7,
            severity=Severity.low,
            evidence=[Evidence(
                claim=f"Listing created {age_days:.1f} days ago",
                source_type="timestamp", source_ref=str(created), fetched_at=fetched)],
        ))

    sec = agent.get("securityRate", "")
    signals.append(build_signal(
        name="security_rate_missing",
        triggered=(sec == "" or sec is None),
        severity=Severity.low,
        evidence=[Evidence(
            claim="No security rating present" if sec in ("", None)
                  else f"Security rating {sec}",
            source_type="agent_id", source_ref=agent.get("agentId", "?"),
            fetched_at=fetched)],
    ))

    sold = agent.get("soldCount", 0)
    signals.append(build_signal(
        name="zero_sales",
        triggered=sold == 0,
        severity=Severity.low,
        evidence=[Evidence(
            claim=f"{sold} completed sales", source_type="agent_id",
            source_ref=agent.get("agentId", "?"), fetched_at=fetched)],
    ))

    online = agent.get("onlineStatus")
    signals.append(build_signal(
        name="offline_or_stale",
        triggered=online != 1,
        severity=Severity.medium,
        evidence=[Evidence(
            claim=f"onlineStatus={online}", source_type="agent_id",
            source_ref=agent.get("agentId", "?"), fetched_at=fetched)],
    ))
    return signals


async def vet_agent(agent: dict, reviews_block: dict, rpc_url: str,
                    known_owner_listings: dict | None = None) -> VetReport:
    """Full vet. agent = identity dict, reviews_block = feedback-list dict."""
    signals = []
    owner = agent.get("ownerAddress", "")

    # 1. listing meta
    signals += analyze_listing_meta(agent)

    async with httpx.AsyncClient() as client:
        # 2. wallet forensics
        if rpc_url and owner:
            wf = await fetch_wallet_facts(client, owner, rpc_url)
            signals += analyze_wallet(wf)

        # 3. review forensics
        reviews = reviews_block.get("list", []) or []
        dist = reviews_block.get("distribution", {}) or {}
        total = reviews_block.get("total", 0) or 0
        signals += analyze_reviews(owner, reviews, dist, total)

        reviewer_addrs = [r.get("reviewerAddress", "") for r in reviews
                          if r.get("reviewerAddress")]
        if rpc_url and reviewer_addrs:
            signals += await analyze_review_funding(client, owner, reviewer_addrs, rpc_url)

        # 4. endpoint probe (first service endpoint)
        services = agent.get("services", []) or []
        endpoint = None
        for s in services:
            endpoint = s.get("endpoint")
            if endpoint:
                break
        if endpoint:
            signals += await probe_endpoint(client, endpoint)

    # 5. wallet reuse across listings (needs a cross-listing index)
    if known_owner_listings and owner:
        others = known_owner_listings.get(owner.lower(), [])
        reused = len(others) > 1
        signals.append(build_signal(
            name="wallet_reused_across_listings",
            triggered=reused,
            severity=Severity.high,
            evidence=[Evidence(
                claim=f"Owner wallet appears on {len(others)} listings",
                source_type="agent_id", source_ref=owner, fetched_at=_now_iso())],
        ))

    if not _has_real_data(agent):
        # Nothing real was scored -- every listing-meta signal fired on an
        # absent default. Emit the (empty/degenerate) signals for transparency
        # but withhold the number rather than report a fabricated score.
        return VetReport(
            agent_id=agent.get("agentId", "?"),
            trust_score=None,
            signals=signals,
            summary="No trust score: agent object lacked the minimum data to "
                    "vet (agentId, ownerAddress, createdAt). Nothing was scored.",
            fetched_at=_now_iso(),
        )

    score = compute_trust_score(signals)
    return VetReport(
        agent_id=agent.get("agentId", "?"),
        trust_score=score,
        signals=signals,
        summary=f"{score_band(score)} ({score}/100). "
                f"{sum(1 for s in signals if s.triggered)} risk signal(s) triggered. "
                f"Every finding below links to a source.",
        fetched_at=_now_iso(),
    )
