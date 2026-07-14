"""Resolve an OKX.AI agentId to (agent_dict, reviews_dict) server-side.

Same OK-ACCESS-* signed-request scheme as x402.py, hitting the OKX.AI
marketplace backend directly -- the same private API `onchainos agent
get-agents` / `service-list` / `feedback-list` use, confirmed by extracting
the literal path strings from the onchainos binary (agent-list path also
independently confirmed from a live onchainos network error earlier).
Query-parameter names (chainIndex/agentId/pageNo/pageSize) follow the same
convention onchainos itself uses and could not be independently confirmed
from this network -- resolve_agent fails loudly (ResolveError, full raw
body) rather than silently on a bad shape, so a wrong assumption surfaces
immediately instead of returning wrong data.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from datetime import datetime, timezone

import httpx

OKX_BASE = "https://web3.okx.com"
CHAIN_INDEX = "196"
RESOLVE_TIMEOUT = 15.0

OKX_API_KEY = os.environ.get("OKX_API_KEY", "")
OKX_SECRET = os.environ.get("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "")


class ResolveError(Exception):
    """Clean, user-facing reason an agent_id could not be resolved."""


def _ts() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _sign(ts: str, method: str, path: str, body: str) -> str:
    msg = f"{ts}{method}{path}{body}"
    mac = hmac.new(OKX_SECRET.encode(), msg.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()


def _headers(method: str, path: str, body: str) -> dict:
    ts = _ts()
    return {
        "Content-Type": "application/json",
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": _sign(ts, method, path, body),
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "OK-ACCESS-TIMESTAMP": ts,
    }


async def _get(client: httpx.AsyncClient, path: str) -> dict:
    try:
        r = await client.get(OKX_BASE + path, headers=_headers("GET", path, ""),
                             timeout=RESOLVE_TIMEOUT)
    except httpx.HTTPError as e:
        raise ResolveError(f"request to {path} failed: {e}") from e
    try:
        body = r.json()
    except Exception as e:
        raise ResolveError(
            f"non-JSON response from {path} (http {r.status_code}): {r.text[:200]}"
        ) from e
    if r.status_code != 200:
        raise ResolveError(f"{path} -> http {r.status_code}: {body}")
    code = str(body.get("code", "0"))
    if code not in ("0", ""):
        raise ResolveError(f"{path} -> okx code {code}: {body.get('msg', '')}")
    return body


def _unwrap_list(data) -> list:
    """OKX list envelopes vary: a flat list, or [{"list": [...]}]. Handle both."""
    if isinstance(data, list):
        if data and isinstance(data[0], dict) and "list" in data[0]:
            return data[0].get("list") or []
        if data and isinstance(data[0], dict) and "agentList" in data[0]:
            return data[0].get("agentList") or []
        return data
    if isinstance(data, dict):
        return data.get("list") or data.get("agentList") or []
    return []


async def resolve_agent(agent_id: int | str) -> tuple[dict, dict]:
    """Fetch identity + services + reviews for agent_id. Raises ResolveError on any failure."""
    if not (OKX_API_KEY and OKX_SECRET and OKX_PASSPHRASE):
        raise ResolveError("OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE not configured")

    agent_id = str(agent_id).strip()
    if not agent_id.isdigit():
        raise ResolveError(f"agent_id must be numeric, got {agent_id!r}")

    async with httpx.AsyncClient() as client:
        identity_path = (f"/priapi/v5/wallet/agentic/agent/agent-list"
                          f"?chainIndex={CHAIN_INDEX}&agentIdList={agent_id}")
        identity_body = await _get(client, identity_path)
        candidates = _unwrap_list(identity_body.get("data"))
        agent = next((a for a in candidates
                      if isinstance(a, dict) and str(a.get("agentId")) == agent_id), None)
        if agent is None and len(candidates) == 1:
            agent = candidates[0]
        if agent is None:
            raise ResolveError(
                f"agent {agent_id} not found in agent-list response: {identity_body}")

        services_path = (f"/priapi/v5/wallet/agentic/agent/services"
                          f"?chainIndex={CHAIN_INDEX}&agentId={agent_id}")
        try:
            services_body = await _get(client, services_path)
            svc_list = _unwrap_list(services_body.get("data"))
            agent["services"] = [{"endpoint": s.get("endpoint")} for s in svc_list
                                  if isinstance(s, dict) and s.get("endpoint")]
        except ResolveError:
            agent.setdefault("services", [])

        reviews_path = (f"/priapi/v5/wallet/agentic/agent/reviews"
                         f"?chainIndex={CHAIN_INDEX}&agentId={agent_id}")
        reviews_body = await _get(client, reviews_path)
        reviews_data = reviews_body.get("data")
        if isinstance(reviews_data, list) and reviews_data:
            reviews = reviews_data[0]
        elif isinstance(reviews_data, dict):
            reviews = reviews_data
        else:
            reviews = {}
        if not isinstance(reviews, dict):
            raise ResolveError(f"unexpected reviews shape for {agent_id}: {reviews_body}")

    return agent, reviews
