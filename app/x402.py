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
    accept = {
        "scheme": "exact",
        "network": XLAYER_CAIP2,
        "amount": price_smallest_unit,
        "asset": USDT_XLAYER,
        "payTo": PAY_TO,
        "maxTimeoutSeconds": 60,
        "resource": resource,
        "extra": {"name": TOKEN_NAME, "version": TOKEN_VERSION},
    }
    return {"x402Version": 2, "resource": resource, "accepts": [accept]}


async def _call(client: httpx.AsyncClient, path: str, payload: dict) -> dict:
    body = json.dumps(payload)
    r = await client.post(OKX_BASE + path, headers=_headers("POST", path, body),
                          content=body, timeout=30)
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
    if not isinstance(body, dict):
        return False, f"non-json (http {resp.get('http_status')})"
    data = body.get("data")
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        if data.get("success") is True:
            return True, ""
        reason = (data.get("invalidReason") or data.get("errorReason")
                  or body.get("msg") or "")
        return False, str(reason) or f"success not true (http {resp.get('http_status')})"
    code = str(body.get("code", ""))
    msg = str(body.get("msg", ""))
    if code and code != "0":
        return False, f"okx code {code}: {msg}"
    return False, msg or f"unrecognized (http {resp.get('http_status')})"


def decode_x_payment(header_value: str) -> dict:
    raw = base64.b64decode(header_value)
    return json.loads(raw)