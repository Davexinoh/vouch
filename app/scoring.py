"""Deterministic scoring. The LLM never touches these numbers.

Signals rebuilt against the REAL OKX.AI agent schema (verified 2026-07-10
from live agent get-agents / feedback-list output). Every signal maps to a
field that actually exists. Same inputs, same score, every time.
"""
from __future__ import annotations

from .models import RiskSignal, Severity


# Signal weights. Verified-feedable signals only.
SIGNAL_WEIGHTS = {
    # Identity / listing signals (from agent get-agents)
    "listing_age_under_7d": 15,        # createdAt
    "security_rate_missing": 10,       # securityRate == ""
    "offline_or_stale": 15,            # onlineStatus / lastOnlineTime null or old
    "zero_sales": 10,                  # soldCount == 0

    # Wallet forensics (agentWalletAddress, ownerAddress, communicationAddress)
    "owner_wallet_young": 15,          # first activity < 7d on X Layer
    "wallet_reused_across_listings": 25,  # same ownerAddress on multiple agents

    # Review forensics (from feedback-list, reviewer addresses are public)
    "self_review_detected": 30,        # reviewerAddress == ownerAddress
    "sybil_review_pattern": 25,        # reviewer wallets funded by owner / same-day cluster
    "review_data_inconsistent": 15,    # distribution vs list vs totalScore mismatch

    # Endpoint probing (services[].endpoint)
    "endpoint_dead": 25,               # no valid x402 402 challenge response
    "endpoint_wrong_status": 15,       # responds but not per x402 spec (e.g. 405)

    # Description claims
    "unverifiable_claim": 10,          # claimed integration/URL that does not resolve
}


def build_signal(name: str, triggered: bool, severity: Severity, evidence: list) -> RiskSignal:
    weight = SIGNAL_WEIGHTS.get(name, 0)
    return RiskSignal(
        name=name,
        triggered=triggered,
        weight=weight,
        severity=severity,
        evidence=evidence,
    )


def compute_trust_score(signals: list[RiskSignal]) -> int:
    """0 = maximum risk, 100 = clean. Pure function of triggered signals."""
    penalty = sum(s.weight for s in signals if s.triggered)
    return max(0, 100 - penalty)


def score_band(score: int) -> str:
    if score >= 80:
        return "low risk"
    if score >= 55:
        return "moderate risk"
    if score >= 30:
        return "elevated risk"
    return "high risk"
