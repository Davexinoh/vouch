"""Evidence collectors. Each returns real, sourced facts or an explicit gap.

Never fabricate a value to fill a field. If data is missing, the signal
is not_evaluated — never a silent clean pass.
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

async def fetch_wallet_facts(client: httpx.AsyncClient, wallet: str, rpc_url: str) -> dict:
    """Return wallet tx count and optional first-seen. Real RPC calls only.

    first_seen_block stays None until an explorer API is wired — do not invent it.
    """
    facts = {"wallet": wallet, "fetched_at": _now_iso(), "first_seen_block": None,
             "first_seen_ts": None}

    payload = {
        "jsonrpc": "2.0",
        "method": "eth_getTransactionCount",
        "params": [wallet, "latest"],
        "id": 1,
    }
    try:
        r = await client.post(rpc_url, json=payload, timeout=15)
        r.raise_for_status()
        nonce_hex = r.json().get("result", "0x0")
        facts["tx_count"] = int(nonce_hex, 16)
        facts["rpc_ok"] = True
    except Exception as e:
        facts["tx_count"] = None
        facts["rpc_ok"] = False
        facts["rpc_error"] = str(e)[:180]
    return facts


def analyze_wallet(facts: dict) -> list:
    """Turn wallet facts into signals. Gaps are not_evaluated, never clean."""
    signals = []
    fetched = facts["fetched_at"]
    wallet = facts["wallet"]

    if not facts.get("rpc_ok", True) or facts.get("tx_count") is None:
        signals.append(build_signal(
            name="zero_prior_payouts",
            triggered=False,
            severity=Severity.medium,
            evaluated=False,
            evidence=[Evidence(
                claim=f"Outbound activity not checked: RPC error "
                      f"({facts.get('rpc_error', 'unknown')})",
                source_type="gap",
                source_ref=wallet,
                fetched_at=fetched,
            )],
        ))
    else:
        tx_count = facts.get("tx_count", 0)
        zero_payouts = tx_count == 0
        signals.append(build_signal(
            name="zero_prior_payouts",
            triggered=zero_payouts,
            severity=Severity.medium,
            evidence=[Evidence(
                claim=f"Wallet has {tx_count} outbound transactions (eth_getTransactionCount)",
                source_type="rpc",
                source_ref=wallet,
                fetched_at=fetched,
            )],
        ))

    # wallet_age_under_7d requires first-seen time from an explorer.
    # Until that is wired, emit NOT EVALUATED — never a clean pass.
    first_block = facts.get("first_seen_block")
    first_ts = facts.get("first_seen_ts")
    if first_block is None and first_ts is None:
        signals.append(build_signal(
            name="wallet_age_under_7d",
            triggered=False,
            severity=Severity.low,
            evaluated=False,
            evidence=[Evidence(
                claim=(
                    "Wallet age not checked: no X Layer explorer first-seen API "
                    "configured (first_seen_block unavailable). Not a clean pass."
                ),
                source_type="gap",
                source_ref=wallet,
                fetched_at=fetched,
            )],
        ))
    else:
        # If explorer ever wires first_ts (unix s or ms), evaluate age.
        try:
            ts = float(first_ts)
            if ts > 1e12:
                ts /= 1000.0
            age_days = (datetime.now(timezone.utc).timestamp() - ts) / 86_400
            young = age_days < 7
            signals.append(build_signal(
                name="wallet_age_under_7d",
                triggered=young,
                severity=Severity.low,
                evidence=[Evidence(
                    claim=f"Wallet first-seen ~{age_days:.1f} days ago "
                          f"(block={first_block})",
                    source_type="block",
                    source_ref=str(first_block or first_ts),
                    fetched_at=fetched,
                )],
            ))
        except Exception:
            signals.append(build_signal(
                name="wallet_age_under_7d",
                triggered=False,
                severity=Severity.low,
                evaluated=False,
                evidence=[Evidence(
                    claim="Wallet age not checked: first_seen timestamp unparseable",
                    source_type="gap",
                    source_ref=wallet,
                    fetched_at=fetched,
                )],
            ))
    return signals


# ---------------------------------------------------------------------------
# Repo forensics via GitHub API
# ---------------------------------------------------------------------------

async def fetch_repo_facts(client: httpx.AsyncClient, repo_url: str) -> dict:
    """Check repo exists, is fork, last commit date. Real GitHub API."""
    facts = {"repo_url": repo_url, "fetched_at": _now_iso(), "exists": False}

    parts = repo_url.rstrip("/").split("/")
    if len(parts) < 2:
        return facts
    owner, name = parts[-2], parts[-1]

    api = f"https://api.github.com/repos/{owner}/{name}"
    r = await client.get(api, timeout=15)
    if r.status_code == 404:
        return facts
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
