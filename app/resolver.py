"""Resolve an OKX.AI agentId to (agent_dict, reviews_dict) server-side.

Marketplace identity routes (`/priapi/v5/wallet/agentic/agent/*`) need a
wallet session token. Facilitator routes only need API-key HMAC.

This module bootstraps a session from the ASP's OKX API Key (same credentials
as x402 verify/settle) via the agentic AK login endpoints used by onchainos
`wallet login` without email, caches accessToken in-process, and refreshes
on code 10008 so plain `{"agent_id": N}` paid calls work for end users.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx

OKX_BASE = "https://web3.okx.com"
CHAIN_INDEX = "196"
RESOLVE_TIMEOUT = 20.0

OKX_API_KEY = os.environ.get("OKX_API_KEY", "")
OKX_SECRET = os.environ.get("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "")
# Optional static override (rarely needed once AK bootstrap works).
OKX_ACCESS_TOKEN_ENV = os.environ.get("OKX_ACCESS_TOKEN", "") or os.environ.get(
    "OKX_JWT", ""
)

# In-process session cache (per Render instance).
_session: dict[str, Any] = {
    "accessToken": OKX_ACCESS_TOKEN_ENV or "",
    "refreshToken": "",
    "expiresAt": 0.0,  # epoch seconds; 0 = unknown / treat as sticky
}


class ResolveError(Exception):
    """Clean, user-facing reason an agent_id could not be resolved."""


def _ts() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _sign(ts: str, method: str, path: str, body: str) -> str:
    msg = f"{ts}{method}{path}{body}"
    mac = hmac.new(OKX_SECRET.encode(), msg.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()


def _ak_headers(method: str, path: str, body: str, with_token: bool = True) -> dict:
    ts = _ts()
    headers = {
        "Content-Type": "application/json",
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": _sign(ts, method, path, body),
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "OK-ACCESS-TIMESTAMP": ts,
    }
    token = _session.get("accessToken") or ""
    if with_token and token:
        bare = token.removeprefix("Bearer ").removeprefix("bearer ")
        headers["Authorization"] = f"Bearer {bare}"
        headers["OK-ACCESS-TOKEN"] = bare
    return headers


def _unwrap_data(body: dict) -> Any:
    data = body.get("data")
    if isinstance(data, list) and data:
        return data[0]
    return data


def _store_tokens(blob: dict) -> None:
    if not isinstance(blob, dict):
        return
    access = blob.get("accessToken") or blob.get("access_token") or ""
    refresh = blob.get("refreshToken") or blob.get("refresh_token") or ""
    # expire fields vary: expireAt / expiresIn / tokenExpireTime
    expires_at = 0.0
    if blob.get("expiresIn"):
        try:
            expires_at = time.time() + float(blob["expiresIn"]) - 60
        except (TypeError, ValueError):
            expires_at = time.time() + 3500
    elif blob.get("expireAt") or blob.get("expiresAt"):
        raw = blob.get("expireAt") or blob.get("expiresAt")
        try:
            expires_at = float(raw) / (1000 if float(raw) > 1e12 else 1) - 60
        except (TypeError, ValueError):
            expires_at = time.time() + 3500
    else:
        # sticky ~1h if server omitted expiry
        expires_at = time.time() + 3500

    if access:
        _session["accessToken"] = access
        _session["refreshToken"] = refresh or _session.get("refreshToken") or ""
        _session["expiresAt"] = expires_at


def _token_fresh() -> bool:
    tok = _session.get("accessToken") or ""
    if not tok:
        return False
    exp = float(_session.get("expiresAt") or 0)
    if exp and time.time() >= exp:
        return False
    return True


async def _ak_login(client: httpx.AsyncClient) -> None:
    """Bootstrap wallet session from API Key (onchainos wallet login without email)."""
    if not (OKX_API_KEY and OKX_SECRET and OKX_PASSPHRASE):
        raise ResolveError("OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE not configured")

    # Path A — single-shot web3 AK agentic login (onchainos)
    for path in (
        "/web3/ak/agentic/login",
        "/web3/ak/agentic/login?locale=en_US",
    ):
        body = ""
        try:
            r = await client.post(
                OKX_BASE + path,
                headers=_ak_headers("POST", path, body, with_token=False),
                content=body or None,
                timeout=RESOLVE_TIMEOUT,
            )
            data = r.json()
        except Exception as e:
            continue
        if str(data.get("code", "0")) in ("0", "") and data.get("data") is not None:
            blob = _unwrap_data(data)
            if isinstance(blob, dict) and (
                blob.get("accessToken") or blob.get("access_token")
            ):
                _store_tokens(blob)
                return

    # Path B — ak/init → ak/verify
    init_path = "/priapi/v5/wallet/agentic/auth/ak/init"
    init_body = "{}"
    r = await client.post(
        OKX_BASE + init_path,
        headers=_ak_headers("POST", init_path, init_body, with_token=False),
        content=init_body,
        timeout=RESOLVE_TIMEOUT,
    )
    try:
        init_json = r.json()
    except Exception as e:
        raise ResolveError(f"ak/init non-JSON: {e}") from e

    init_data = _unwrap_data(init_json)
    # Some builds return tokens already at init.
    if isinstance(init_data, dict) and (
        init_data.get("accessToken") or init_data.get("access_token")
    ):
        _store_tokens(init_data)
        return

    verify_path = "/priapi/v5/wallet/agentic/auth/ak/verify"
    # Pass through whatever init returned (nonce / challenge / empty).
    verify_payload: dict[str, Any] = {}
    if isinstance(init_data, dict):
        for k in ("nonce", "iss", "sign", "challenge", "requestId", "token"):
            if k in init_data:
                verify_payload[k] = init_data[k]
    verify_body = json_dumps(verify_payload)
    r = await client.post(
        OKX_BASE + verify_path,
        headers=_ak_headers("POST", verify_path, verify_body, with_token=False),
        content=verify_body,
        timeout=RESOLVE_TIMEOUT,
    )
    try:
        verify_json = r.json()
    except Exception as e:
        raise ResolveError(f"ak/verify non-JSON: {e}") from e

    if str(verify_json.get("code", "0")) not in ("0", ""):
        raise ResolveError(
            f"ak/verify failed: code {verify_json.get('code')} "
            f"{verify_json.get('msg', '')}"
        )
    blob = _unwrap_data(verify_json)
    if not isinstance(blob, dict) or not (
        blob.get("accessToken") or blob.get("access_token")
    ):
        # Last resort: try refresh if we have a refresh token from env prior runs
        if _session.get("refreshToken"):
            await _refresh(client)
            return
        raise ResolveError(
            f"ak/verify returned no accessToken: {str(verify_json)[:300]}"
        )
    _store_tokens(blob)


async def _refresh(client: httpx.AsyncClient) -> None:
    path = "/priapi/v5/wallet/agentic/auth/refresh"
    payload = {"refreshToken": _session.get("refreshToken") or ""}
    body = json_dumps(payload)
    r = await client.post(
        OKX_BASE + path,
        headers=_ak_headers("POST", path, body, with_token=True),
        content=body,
        timeout=RESOLVE_TIMEOUT,
    )
    try:
        data = r.json()
    except Exception as e:
        raise ResolveError(f"auth/refresh non-JSON: {e}") from e
    if str(data.get("code", "0")) not in ("0", ""):
        # force full re-login
        _session["accessToken"] = ""
        await _ak_login(client)
        return
    blob = _unwrap_data(data)
    if isinstance(blob, dict):
        _store_tokens(blob)


def json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


async def ensure_session(client: httpx.AsyncClient) -> None:
    if _token_fresh():
        return
    if _session.get("refreshToken"):
        try:
            await _refresh(client)
            if _token_fresh():
                return
        except ResolveError:
            pass
    await _ak_login(client)
    if not _token_fresh():
        raise ResolveError("failed to obtain marketplace access token via API-key login")


async def _get(client: httpx.AsyncClient, path: str, *, retry_auth: bool = True) -> dict:
    await ensure_session(client)
    try:
        r = await client.get(
            OKX_BASE + path,
            headers=_ak_headers("GET", path, "", with_token=True),
            timeout=RESOLVE_TIMEOUT,
        )
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
    if code in ("0", ""):
        return body
    msg = str(body.get("msg", ""))
    if retry_auth and (code == "10008" or "access token" in msg.lower()):
        _session["accessToken"] = ""
        _session["expiresAt"] = 0
        await ensure_session(client)
        return await _get(client, path, retry_auth=False)
    raise ResolveError(f"{path} -> okx code {code}: {msg}")


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
        identity_path = (
            f"/priapi/v5/wallet/agentic/agent/agent-list"
            f"?chainIndex={CHAIN_INDEX}&agentIdList={agent_id}"
        )
        identity_body = await _get(client, identity_path)
        candidates = _unwrap_list(identity_body.get("data"))
        agent = next(
            (
                a
                for a in candidates
                if isinstance(a, dict) and str(a.get("agentId")) == agent_id
            ),
            None,
        )
        if agent is None and len(candidates) == 1:
            agent = candidates[0]
        if agent is None:
            raise ResolveError(
                f"agent {agent_id} not found in agent-list response: {identity_body}"
            )

        services_path = (
            f"/priapi/v5/wallet/agentic/agent/services"
            f"?chainIndex={CHAIN_INDEX}&agentId={agent_id}"
        )
        try:
            services_body = await _get(client, services_path)
            svc_list = _unwrap_list(services_body.get("data"))
            agent["services"] = [
                {"endpoint": s.get("endpoint")}
                for s in svc_list
                if isinstance(s, dict) and s.get("endpoint")
            ]
        except ResolveError:
            agent.setdefault("services", [])

        reviews_path = (
            f"/priapi/v5/wallet/agentic/agent/reviews"
            f"?chainIndex={CHAIN_INDEX}&agentId={agent_id}"
        )
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


async def resolve_ready() -> dict:
    """Health helper: can this instance resolve agent_id without body snapshot?"""
    try:
        async with httpx.AsyncClient() as client:
            await ensure_session(client)
        return {
            "marketplace_session": True,
            "has_access_token": bool(_session.get("accessToken")),
        }
    except Exception as e:
        return {
            "marketplace_session": False,
            "has_access_token": bool(_session.get("accessToken")),
            "error": str(e)[:200],
        }
