"""OKX x402 payment gating. Facilitator pattern.

EIP-712 domain (extra) MUST match the token contract. USD_T0 uses name
"USD\u20ae0" (U+20AE tether sign), verified via onchain DOMAIN_SEPARATOR.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone

import httpx

OKX_BASE = "https://web3.okx.com"
XLAYER_CAIP2 = "eip155:196"
USDT_XLAYER = os.environ.get("USDT_XLAYER_ADDRESS", "")
PAY_TO = os.environ.get("VOUCH_PAYOUT_WALLET", "")

TOKEN_NAME = os.environ.get("X402_TOKEN_NAME", "USD\u20ae0")
TOKEN_VERSION = os.environ.get("X402_TOKEN_VERSION", "1")

OKX_API_KEY = os.environ.get("OKX_API_KEY", "")
OKX_SECRET = os.environ.get("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "")


def credentials_present() -> bool:
    return bool(OKX_API_KEY and OKX_SECRET and OKX_PASSPHRASE)


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


def payment_requirements(price_smallest_unit: str, resource_url: str,
                         description: str) -> dict:
    resource = {"url": resource_url, "description": description,
                "mimeType": "application/json"}
    base = {
        "network": XLAYER_CAIP2,
        "amount": str(price_smallest_unit),
        "decimals": 6,
        "asset": USDT_XLAYER,
        "payTo": PAY_TO,
        "maxTimeoutSeconds": 60,
        "resource": resource,
        # EIP-712 domain, shared by both schemes -- sessionCert is NOT here,
        # it comes from the buyer's own payload (aggr_deferred only).
        # Must match the on-chain USDT0 domain name (USD + U+20AE + 0).
        "extra": {"name": TOKEN_NAME, "version": TOKEN_VERSION},
    }
    accepts = [
        {"scheme": "exact", **base},          # plain EOA wallets
        {"scheme": "aggr_deferred", **base},  # OKX agentic (AA) wallets
    ]
    return {"x402Version": 2, "resource": resource, "accepts": accepts}


async def _call(client: httpx.AsyncClient, path: str, payload: dict) -> dict:
    # Compact JSON so the HMAC body matches the bytes on the wire exactly.
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    r = await client.post(OKX_BASE + path, headers=_headers("POST", path, body),
                          content=body.encode("utf-8"), timeout=30)
    try:
        parsed = r.json()
    except Exception:
        parsed = {"raw": r.text[:500]}
    return {"http_status": r.status_code, "body": parsed}


async def verify_payment(client, payment_payload, requirements):
    return await _call(client, "/api/v6/pay/x402/verify",
                       {"x402Version": 2, "paymentPayload": payment_payload,
                        "paymentRequirements": requirements})


async def settle_payment(client, payment_payload, requirements):
    return await _call(client, "/api/v6/pay/x402/settle",
                       {"x402Version": 2, "paymentPayload": payment_payload,
                        "paymentRequirements": requirements})


def outcome(resp: dict):
    body = resp.get("body", {})
    http_status = resp.get("http_status")
    if not isinstance(body, dict):
        return False, f"non-json (http {http_status})"
    code = str(body.get("code", ""))
    msg = str(body.get("msg", "") or body.get("error_message", "") or "")
    # Auth / permission failures often return code != "0" with empty data.
    if code and code not in ("0", "None"):
        return False, f"okx code {code}: {msg or 'auth or request rejected'}"
    data = body.get("data")
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        if data.get("success") is True or data.get("isValid") is True:
            return True, ""
        reason = (data.get("invalidReason") or data.get("errorReason")
                  or data.get("message") or data.get("msg") or msg or "")
        if reason:
            return False, str(reason)
        # Keep a short body dump so production /status is actionable.
        try:
            snippet = json.dumps(data, ensure_ascii=False)[:240]
        except Exception:
            snippet = str(data)[:240]
        return False, f"success not true (http {http_status}) data={snippet}"
    if msg:
        return False, msg
    return False, f"unrecognized (http {http_status}) body={str(body)[:240]}"


def decode_x_payment(header_value: str) -> dict:
    raw = base64.b64decode(header_value)
    return json.loads(raw)