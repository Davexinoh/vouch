"""Evidence collectors. Each returns real, sourced facts or an explicit gap.

Never fabricate a value to fill a field. If data is missing, the signal
reports missing, and the report says so. That honesty is the product.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from .models import Evidence, Severity
from .scoring import build_signal


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# X Layer wallet forensics
# ---------------------------------------------------------------------------
# X Layer is EVM. Use its RPC or explorer API. Wire the real endpoint in config.
# Below is the shape. Fill XLAYER_RPC and the explorer key on day 1.

async def fetch_wallet_facts(client: httpx.AsyncClient, wallet: str, rpc_url: str) -> dict:
    """Return wallet age, tx count, first-seen block. Real RPC calls.

    Returns a dict of raw facts. Signals are derived in analyze_wallet.
    """
    facts = {"wallet": wallet, "fetched_at": _now_iso()}

    # eth_getTransactionCount for nonce (activity proxy)
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_getTransactionCount",
        "params": [wallet, "latest"],
        "id": 1,
    }
    r = await client.post(rpc_url, json=payload, timeout=15)
    r.raise_for_status()
    nonce_hex = r.json().get("result", "0x0")
    facts["tx_count"] = int(nonce_hex, 16)

    # First-seen block requires an explorer API (e.g. OKLink for X Layer).
    # Placeholder key: wire OKLINK_KEY. Do not fake first_seen_block.
    facts["first_seen_block"] = None  # filled by explorer call once wired

    return facts


def analyze_wallet(facts: dict) -> list:
    """Turn wallet facts into signals with evidence. No invented data."""
    signals = []

    tx_count = facts.get("tx_count", 0)
    zero_payouts = tx_count == 0
    signals.append(build_signal(
        name="zero_prior_payouts",
        triggered=zero_payouts,
        severity=Severity.medium,
        evidence=[Evidence(
            claim=f"Wallet has {tx_count} outbound transactions",
            source_type="rpc",
            source_ref=facts["wallet"],
            fetched_at=facts["fetched_at"],
        )],
    ))

    # wallet_age_under_7d needs first_seen_block. If unavailable, report gap.
    first_block = facts.get("first_seen_block")
    if first_block is None:
        # Do not trigger the signal on missing data. Report the gap instead.
        signals.append(build_signal(
            name="wallet_age_under_7d",
            triggered=False,
            severity=Severity.info,
            evidence=[Evidence(
                claim="Wallet age not resolved (explorer API not wired yet)",
                source_type="gap",
                source_ref=facts["wallet"],
                fetched_at=facts["fetched_at"],
            )],
        ))
    return signals


# ---------------------------------------------------------------------------
# Repo forensics via GitHub API
# ---------------------------------------------------------------------------

async def fetch_repo_facts(client: httpx.AsyncClient, repo_url: str) -> dict:
    """Check repo exists, is fork, last commit date. Real GitHub API."""
    facts = {"repo_url": repo_url, "fetched_at": _now_iso(), "exists": False}

    # Parse owner/name from url
    parts = repo_url.rstrip("/").split("/")
    if len(parts) < 2:
        return facts
    owner, name = parts[-2], parts[-1]

    api = f"https://api.github.com/repos/{owner}/{name}"
    r = await client.get(api, timeout=15)
    if r.status_code == 404:
        return facts  # exists stays False
    r.raise_for_status()
    data = r.json()
    facts["exists"] = True
    facts["is_fork"] = data.get("fork", False)
    facts["pushed_at"] = data.get("pushed_at")
    facts["created_at"] = data.get("created_at")
    facts["parent"] = data.get("parent", {}).get("html_url") if data.get("fork") else None
    return facts


def analyze_repo(facts: dict, listing_created_at: str | None = None) -> list:
    signals = []
    fetched = facts["fetched_at"]

    if not facts.get("exists"):
        signals.append(build_signal(
            name="repo_missing",
            triggered=True,
            severity=Severity.high,
            evidence=[Evidence(
                claim="Linked repository returns 404 or is inaccessible",
                source_type="repo",
                source_ref=facts["repo_url"],
                fetched_at=fetched,
            )],
        ))
        return signals

    is_fork = facts.get("is_fork", False)
    signals.append(build_signal(
        name="repo_is_fork",
        triggered=is_fork,
        severity=Severity.medium,
        evidence=[Evidence(
            claim=f"Repository is a fork of {facts.get('parent')}" if is_fork
                  else "Repository is original, not a fork",
            source_type="repo",
            source_ref=facts.get("parent") or facts["repo_url"],
            fetched_at=fetched,
        )],
    ))
    return signals
