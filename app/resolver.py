"""Resolve an OKX.AI agentId to (agent_dict, reviews_dict) server-side.

Marketplace identity routes need a session token when possible. We try, in
order:

1. OKX_ACCESS_TOKEN / OKX_JWT env (sticky wallet/JWT session)
2. AK agentic login bootstrap from API key (several path variants)
3. HMAC-only public-ish search/list endpoints (no session) as last resort

Plain ``{"agent_id": N}`` paid calls must work for end buyers without them
shipping ownerAddress/createdAt.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx

OKX_BASE = os.environ.get("OKX_BASE", "https://web3.okx.com").rstrip("/")
CHAIN_INDEX = "196"
RESOLVE_TIMEOUT = 25.0
PROJECT_ID = os.environ.get("OKX_PROJECT_ID", "agentic-wallet-project01")

# In-process session cache (per container instance).
_session: dict[str, Any] = {
    "accessToken": "",
    "refreshToken": "",
    "expiresAt": 0.0,
}


class ResolveError(Exception):
    """Clean, user-facing reason an agent_id could not be resolved."""


def _creds() -> tuple[str, str, str]:
    """Read credentials at call-time (not import-time) so restarts pick env."""
    key = os.environ.get("OKX_API_KEY", "") or ""
    secret = os.environ.get("OKX_SECRET_KEY", "") or ""
    passphrase = os.environ.get("OKX_PASSPHRASE", "") or ""
    return key, secret, passphrase


def _env_token() -> str:
    return (
        os.environ.get("OKX_ACCESS_TOKEN", "")
        or os.environ.get("OKX_JWT", "")
        or ""
    )


def _ts() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _sign(secret: str, ts: str, method: str, path: str, body: str) -> str:
    msg = f"{ts}{method}{path}{body}"
    mac = hmac.new(secret.encode(), msg.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()


def _headers(
    method: str,
    path: str,
    body: str,
    *,
    with_token: bool = True,
    token_override: str = "",
) -> dict:
    key, secret, passphrase = _creds()
    ts = _ts()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "vouch-resolver/1.6",
    }
    if key and secret and passphrase:
        headers.update({
            "OK-ACCESS-KEY": key,
            "OK-ACCESS-SIGN": _sign(secret, ts, method, path, body),
            "OK-ACCESS-PASSPHRASE": passphrase,
            "OK-ACCESS-TIMESTAMP": ts,
        })
    token = token_override or (
        _session.get("accessToken") if with_token else ""
    ) or ""
    if not token and with_token:
        token = _env_token()
    if token:
        bare = token.removeprefix("Bearer ").removeprefix("bearer ").strip()
        headers["Authorization"] = f"Bearer {bare}"
        headers["OK-ACCESS-TOKEN"] = bare
    # onchainos sends OK-ACCESS-PROJECT for agentic routes — without it AK
    # login often replies "An API key is required" even when the key is present.
    if PROJECT_ID:
        headers["OK-ACCESS-PROJECT"] = PROJECT_ID
        headers["OK-PROJECT-ID"] = PROJECT_ID
        headers["x-project-id"] = PROJECT_ID
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
        expires_at = time.time() + 3500

    if access:
        _session["accessToken"] = access
        _session["refreshToken"] = refresh or _session.get("refreshToken") or ""
        _session["expiresAt"] = expires_at


def _token_fresh() -> bool:
    tok = (_session.get("accessToken") or "") or _env_token()
    if not tok:
        return False
    if _session.get("accessToken"):
        exp = float(_session.get("expiresAt") or 0)
        if exp and time.time() >= exp:
            return False
    return True


async def _ak_login(client: httpx.AsyncClient) -> None:
    """Bootstrap wallet session from API Key."""
    key, secret, passphrase = _creds()
    if not (key and secret and passphrase):
        raise ResolveError(
            "OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE not configured"
        )

    # Seed sticky env token if present
    env_tok = _env_token()
    if env_tok and not _session.get("accessToken"):
        _session["accessToken"] = env_tok
        _session["expiresAt"] = time.time() + 3500
        return

    last_err = "ak/login failed: no accessToken returned"
    login_attempts: list[tuple[str, str]] = [
        ("POST", "/web3/ak/agentic/login"),
        ("POST", "/web3/ak/agentic/login?locale=en_US"),
        ("POST", "/priapi/v5/wallet/agentic/auth/ak/login"),
    ]
    for method, path in login_attempts:
        for body in (
            "",
            "{}",
            json_dumps({"projectId": PROJECT_ID}),
            json_dumps({"apiKey": key, "projectId": PROJECT_ID}),
        ):
            try:
                r = await client.request(
                    method,
                    OKX_BASE + path,
                    headers=_headers(method, path, body, with_token=False),
                    content=body or None,
                    timeout=RESOLVE_TIMEOUT,
                )
                data = r.json()
            except Exception as e:
                last_err = f"{path}: {e}"
                continue
            if str(data.get("code", "0")) not in ("0", ""):
                last_err = f"{path}: code {data.get('code')} {data.get('msg', '')}"
                continue
            if data.get("data") is not None:
                blob = _unwrap_data(data)
                if isinstance(blob, dict) and (
                    blob.get("accessToken") or blob.get("access_token")
                ):
                    _store_tokens(blob)
                    return

    # Path B — ak/init MUST include apiKey in body (empty body → "An API key is required")
    init_path = "/priapi/v5/wallet/agentic/auth/ak/init"
    for init_body in (
        json_dumps({"apiKey": key, "projectId": PROJECT_ID}),
        json_dumps({"apiKey": key}),
        json_dumps({"projectId": PROJECT_ID, "apiKey": key, "locale": "en_US"}),
    ):
        try:
            r = await client.post(
                OKX_BASE + init_path,
                headers=_headers("POST", init_path, init_body, with_token=False),
                content=init_body,
                timeout=RESOLVE_TIMEOUT,
            )
            init_json = r.json()
        except Exception as e:
            last_err = f"ak/init: {e}"
            continue

        if str(init_json.get("code", "0")) not in ("0", ""):
            last_err = (
                f"ak/init: code {init_json.get('code')} {init_json.get('msg', '')}"
            )
            continue

        init_data = _unwrap_data(init_json)
        if isinstance(init_data, dict) and (
            init_data.get("accessToken") or init_data.get("access_token")
        ):
            _store_tokens(init_data)
            return

        verify_path = "/priapi/v5/wallet/agentic/auth/ak/verify"
        verify_payload: dict[str, Any] = {"projectId": PROJECT_ID}
        if isinstance(init_data, dict):
            for k in (
                "nonce", "iss", "sign", "challenge", "requestId", "token",
                "sessionId", "data",
            ):
                if k in init_data:
                    verify_payload[k] = init_data[k]
            if not any(k in verify_payload for k in ("nonce", "challenge", "token")):
                verify_payload.update(
                    {k: v for k, v in init_data.items() if k not in ("accessToken",)}
                )
        verify_body = json_dumps(verify_payload)
        try:
            r = await client.post(
                OKX_BASE + verify_path,
                headers=_headers("POST", verify_path, verify_body, with_token=False),
                content=verify_body,
                timeout=RESOLVE_TIMEOUT,
            )
            verify_json = r.json()
        except Exception as e:
            last_err = f"ak/verify non-JSON: {e}"
            continue

        if str(verify_json.get("code", "0")) not in ("0", ""):
            last_err = (
                f"ak/verify failed: code {verify_json.get('code')} "
                f"{verify_json.get('msg', '')}"
            )
            continue

        blob = _unwrap_data(verify_json)
        if isinstance(blob, dict) and (
            blob.get("accessToken") or blob.get("access_token")
        ):
            _store_tokens(blob)
            return
        last_err = f"ak/verify returned no accessToken: {str(verify_json)[:200]}"

    if _env_token():
        _session["accessToken"] = _env_token()
        _session["expiresAt"] = time.time() + 3500
        return

    raise ResolveError(
        f"{last_err}. Set OKX_ACCESS_TOKEN from a wallet session if AK login "
        "is not permitted for this API key."
    )


async def _refresh(client: httpx.AsyncClient) -> None:
    path = "/priapi/v5/wallet/agentic/auth/refresh"
    payload = {"refreshToken": _session.get("refreshToken") or ""}
    body = json_dumps(payload)
    r = await client.post(
        OKX_BASE + path,
        headers=_headers("POST", path, body, with_token=True),
        content=body,
        timeout=RESOLVE_TIMEOUT,
    )
    try:
        data = r.json()
    except Exception as e:
        raise ResolveError(f"auth/refresh non-JSON: {e}") from e
    if str(data.get("code", "0")) not in ("0", ""):
        _session["accessToken"] = ""
        await _ak_login(client)
        return
    blob = _unwrap_data(data)
    if isinstance(blob, dict):
        _store_tokens(blob)


async def ensure_session(client: httpx.AsyncClient) -> None:
    # Prefer env token
    if _env_token() and not _session.get("accessToken"):
        _session["accessToken"] = _env_token()
        _session["expiresAt"] = time.time() + 3500
    if _token_fresh() and (_session.get("accessToken") or _env_token()):
        if not _session.get("accessToken"):
            _session["accessToken"] = _env_token()
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
        raise ResolveError(
            "failed to obtain marketplace access token via API-key login"
        )


async def _get(
    client: httpx.AsyncClient,
    path: str,
    *,
    retry_auth: bool = True,
    require_token: bool = True,
) -> dict:
    if require_token:
        await ensure_session(client)
    try:
        r = await client.get(
            OKX_BASE + path,
            headers=_headers("GET", path, "", with_token=require_token),
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
    if retry_auth and require_token and (
        code == "10008" or "access token" in msg.lower() or code == "401"
    ):
        _session["accessToken"] = ""
        _session["expiresAt"] = 0
        await ensure_session(client)
        return await _get(client, path, retry_auth=False, require_token=True)
    raise ResolveError(f"{path} -> okx code {code}: {msg}")


async def _post(
    client: httpx.AsyncClient,
    path: str,
    payload: dict,
    *,
    require_token: bool = True,
) -> dict:
    body = json_dumps(payload)
    if require_token:
        await ensure_session(client)
    try:
        r = await client.post(
            OKX_BASE + path,
            headers=_headers("POST", path, body, with_token=require_token),
            content=body,
            timeout=RESOLVE_TIMEOUT,
        )
    except httpx.HTTPError as e:
        raise ResolveError(f"POST {path} failed: {e}") from e
    try:
        data = r.json()
    except Exception as e:
        raise ResolveError(f"POST {path} non-JSON: {e}") from e
    if str(data.get("code", "0")) not in ("0", ""):
        raise ResolveError(
            f"POST {path} -> code {data.get('code')}: {data.get('msg', '')}"
        )
    return data


def _unwrap_list(data) -> list:
    if isinstance(data, list):
        if data and isinstance(data[0], dict) and "list" in data[0]:
            return data[0].get("list") or []
        if data and isinstance(data[0], dict) and "agentList" in data[0]:
            return data[0].get("agentList") or []
        return data
    if isinstance(data, dict):
        return data.get("list") or data.get("agentList") or []
    return []


def _pick_agent(candidates: list, agent_id: str) -> dict | None:
    agent = next(
        (
            a
            for a in candidates
            if isinstance(a, dict) and str(a.get("agentId")) == agent_id
        ),
        None,
    )
    if agent is None and len(candidates) == 1 and isinstance(candidates[0], dict):
        agent = candidates[0]
    return agent


async def _fetch_identity(client: httpx.AsyncClient, agent_id: str) -> dict:
    """Try several endpoints / auth modes to load the agent identity."""
    errors: list[str] = []

    # 1) Authed agent-list
    try:
        identity_path = (
            f"/priapi/v5/wallet/agentic/agent/agent-list"
            f"?chainIndex={CHAIN_INDEX}&agentIdList={agent_id}"
        )
        identity_body = await _get(client, identity_path, require_token=True)
        agent = _pick_agent(_unwrap_list(identity_body.get("data")), agent_id)
        if agent:
            return agent
        errors.append(f"agent-list authed: no match in {str(identity_body)[:180]}")
    except ResolveError as e:
        errors.append(f"agent-list authed: {e}")

    # 2) HMAC-only agent-list (some keys work without session)
    try:
        identity_path = (
            f"/priapi/v5/wallet/agentic/agent/agent-list"
            f"?chainIndex={CHAIN_INDEX}&agentIdList={agent_id}"
        )
        identity_body = await _get(client, identity_path, require_token=False)
        agent = _pick_agent(_unwrap_list(identity_body.get("data")), agent_id)
        if agent:
            return agent
        errors.append("agent-list hmac: no match")
    except ResolveError as e:
        errors.append(f"agent-list hmac: {e}")

    # 3) Search by query = agent id
    for require_token in (True, False):
        try:
            search_path = "/priapi/v5/wallet/agentic/search/agent-search"
            data = await _post(
                client,
                search_path,
                {"query": agent_id, "page": 1, "pageSize": 10},
                require_token=require_token,
            )
            agent = _pick_agent(_unwrap_list(data.get("data")), agent_id)
            if agent:
                return agent
            # search may return ranked list — scan for id
            for a in _unwrap_list(data.get("data")):
                if isinstance(a, dict) and str(a.get("agentId")) == agent_id:
                    return a
            errors.append(f"search token={require_token}: no match")
        except ResolveError as e:
            errors.append(f"search token={require_token}: {e}")

    # 4) Batch get style used by CLI get-agents
    for path, payload in (
        (
            "/priapi/v5/wallet/agentic/agent/batch-get",
            {"agentIdList": [agent_id], "chainIndex": CHAIN_INDEX},
        ),
        (
            "/priapi/v5/wallet/agentic/agent/detail",
            {"agentId": agent_id, "chainIndex": CHAIN_INDEX},
        ),
    ):
        try:
            data = await _post(client, path, payload, require_token=True)
            candidates = _unwrap_list(data.get("data"))
            if not candidates and isinstance(data.get("data"), dict):
                candidates = [data["data"]]
            agent = _pick_agent(candidates, agent_id)
            if agent:
                return agent
        except ResolveError as e:
            errors.append(f"{path}: {e}")

    raise ResolveError(
        f"agent {agent_id} not found; tried {len(errors)} paths: "
        + " | ".join(errors)[:500]
    )


async def resolve_agent(agent_id: int | str) -> tuple[dict, dict]:
    """Fetch identity + services + reviews for agent_id."""
    agent_id = str(agent_id).strip().lstrip("#")
    if not agent_id.isdigit():
        raise ResolveError(f"agent_id must be numeric, got {agent_id!r}")

    # 0) Optional remote proxy (local onchainos + tunnel → Railway)
    proxy = (os.environ.get("RESOLVE_PROXY_URL") or "").rstrip("/")
    proxy_key = os.environ.get("RESOLVE_PROXY_SECRET", "vouch-resolve-local")
    if proxy:
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                r = await client.get(
                    f"{proxy}/resolve/{agent_id}",
                    params={"key": proxy_key},
                )
            if r.status_code == 200:
                payload = r.json()
                agent = payload.get("agent") or {}
                reviews = payload.get("reviews") or {
                    "list": [], "distribution": {}, "total": 0
                }
                if agent.get("ownerAddress") and agent.get("createdAt"):
                    return agent, reviews
            raise ResolveError(
                f"resolve proxy HTTP {r.status_code}: {r.text[:200]}"
            )
        except ResolveError:
            raise
        except Exception as e:
            raise ResolveError(f"resolve proxy failed: {e}") from e

    # 1) onchainos CLI when available — AK login + get-agents (TEE session).
    # Runs in a worker thread: the CLI subprocess can take tens of seconds
    # and must not block the event loop (health checks, other requests).
    cli_err = ""
    try:
        from .cli_resolve import available as cli_available, resolve_agent_cli
        if cli_available() or os.environ.get("ONCHAINOS_BIN"):
            return await asyncio.to_thread(resolve_agent_cli, agent_id)
    except ResolveError:
        raise
    except Exception as e:
        # fall through to HTTP paths
        cli_err = str(e)

    key, secret, passphrase = _creds()
    if not (key and secret and passphrase) and not _env_token():
        raise ResolveError(
            "OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE not configured "
            "(and no OKX_ACCESS_TOKEN / onchainos CLI)"
        )

    async with httpx.AsyncClient() as client:
        try:
            agent = await _fetch_identity(client, agent_id)
        except ResolveError as e:
            extra = f" | cli: {cli_err}" if cli_err else ""
            raise ResolveError(f"{e}{extra}") from e

        # services (best-effort)
        try:
            services_path = (
                f"/priapi/v5/wallet/agentic/agent/services"
                f"?chainIndex={CHAIN_INDEX}&agentId={agent_id}"
            )
            services_body = await _get(client, services_path, require_token=True)
            svc_list = _unwrap_list(services_body.get("data"))
            agent["services"] = [
                {"endpoint": s.get("endpoint")}
                for s in svc_list
                if isinstance(s, dict) and s.get("endpoint")
            ]
        except ResolveError:
            agent.setdefault("services", agent.get("services") or [])

        # reviews (best-effort — empty reviews still allow a report)
        reviews: dict = {"list": [], "distribution": {}, "total": 0}
        try:
            reviews_path = (
                f"/priapi/v5/wallet/agentic/agent/reviews"
                f"?chainIndex={CHAIN_INDEX}&agentId={agent_id}"
            )
            reviews_body = await _get(client, reviews_path, require_token=True)
            reviews_data = reviews_body.get("data")
            if isinstance(reviews_data, list) and reviews_data:
                blob = reviews_data[0]
                if isinstance(blob, dict):
                    reviews = blob
            elif isinstance(reviews_data, dict):
                reviews = reviews_data
        except ResolveError:
            pass

        # Normalize canonical fields buyers / scorer expect
        if not agent.get("agentId"):
            agent["agentId"] = agent_id
        if not agent.get("ownerAddress"):
            agent["ownerAddress"] = (
                agent.get("agentWalletAddress")
                or agent.get("owner")
                or ""
            )

    if not agent.get("ownerAddress") or not agent.get("createdAt"):
        raise ResolveError(
            f"agent {agent_id} resolved but missing ownerAddress/createdAt: "
            f"keys={list(agent.keys())[:20]}"
        )

    return agent, reviews if isinstance(reviews, dict) else {}


# Cache CLI wallet-status checks so /health stays fast under polling.
_READY_CACHE: dict[str, Any] = {"at": 0.0, "value": None}
_READY_TTL = 60.0


async def resolve_ready() -> dict:
    """Health helper: can this instance resolve agent_id without body snapshot?"""
    now = time.time()
    if _READY_CACHE["value"] is not None and now - _READY_CACHE["at"] < _READY_TTL:
        return _READY_CACHE["value"]

    result: dict
    # Preferred: onchainos CLI TEE session (AK login from OKX_API_KEY).
    try:
        from .cli_resolve import available as cli_available, wallet_logged_in
        if cli_available() or os.environ.get("ONCHAINOS_BIN"):
            logged_in = await asyncio.to_thread(wallet_logged_in)
            result = {
                "marketplace_session": logged_in,
                "via": "onchainos_cli",
                "has_api_key": bool(_creds()[0]),
            }
            _READY_CACHE.update(at=now, value=result)
            return result
    except Exception as e:
        cli_err = str(e)[:240]
    else:
        cli_err = ""

    try:
        async with httpx.AsyncClient() as client:
            await ensure_session(client)
        result = {
            "marketplace_session": True,
            "via": "http_session",
            "has_access_token": bool(_session.get("accessToken") or _env_token()),
            "has_api_key": bool(_creds()[0]),
        }
    except Exception as e:
        result = {
            "marketplace_session": False,
            "has_access_token": bool(_session.get("accessToken") or _env_token()),
            "has_api_key": bool(_creds()[0]),
            "error": (cli_err or str(e))[:240],
        }
    _READY_CACHE.update(at=now, value=result)
    return result
