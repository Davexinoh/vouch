"""Deterministic scoring. The LLM never touches these numbers.

Only signals that a collector actually emits belong in SIGNAL_WEIGHTS.
Same inputs, same score, every time.
"""
from __future__ import annotations

from .models import RiskSignal, Severity

# Signals that run in production collectors (count = 11).
# Removed: owner_wallet_young (alias of wallet_age_under_7d),
#          unverifiable_claim (no collector).
SIGNAL_WEIGHTS = {
    # Identity / listing (analyze_listing_meta)
    "listing_age_under_7d": 15,
    "security_rate_missing": 10,
    "offline_or_stale": 15,
    "zero_sales": 10,

    # Wallet forensics (analyze_wallet + known_owner_listings)
    "zero_prior_payouts": 15,            # RPC nonce == 0
    "wallet_age_under_7d": 15,           # needs first_seen; may be not_evaluated
    "wallet_reused_across_listings": 25,

    # Review forensics
    "self_review_detected": 30,
    # Internal id; public claims say "coordinated review timing/burst"
    "sybil_review_pattern": 25,
    "review_data_inconsistent": 15,

    # Endpoint probing (GET then POST)
    "endpoint_dead": 25,                 # neither method responds
    "endpoint_wrong_status": 15,         # responses but no valid 200/402
}

# Public count advertised in docs/product copy
ACTIVE_SIGNAL_COUNT = len(SIGNAL_WEIGHTS)


def build_signal(
    name: str,
    triggered: bool,
    severity: Severity,
    evidence: list,
    *,
    evaluated: bool = True,
) -> RiskSignal:
    weight = SIGNAL_WEIGHTS.get(name, 0) if evaluated else 0
    return RiskSignal(
        name=name,
        triggered=bool(triggered) if evaluated else False,
        weight=weight,
        severity=severity,
        evidence=evidence,
        evaluated=evaluated,
    )


def compute_trust_score(signals: list[RiskSignal]) -> int:
    """0 = maximum risk, 100 = clean. Only evaluated+triggered signals penalize."""
    penalty = sum(s.weight for s in signals if s.evaluated and s.triggered)
    return max(0, 100 - penalty)


def score_band(score: int) -> str:
    if score >= 80:
        return "low risk"
    if score >= 55:
        return "moderate risk"
    if score >= 30:
        return "elevated risk"
    return "high risk"
