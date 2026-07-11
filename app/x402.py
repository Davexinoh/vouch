"""OKX x402 payment gating. Facilitator pattern — OKX verifies and settles.

402 body aligned to what OKX's checker and facilitator parse:
  - x402Version: 2
  - accepts: [ { scheme, network, amount, asset, payTo, maxTimeoutSeconds, extra } ]
  - resource advertised both at top level and inside the accepts entry for
    maximum compatibility with strict and lenient parsers.

X Layer network id (CAIP-2): eip155:196
Facilitator base: https://web3.okx.com/api/v6/pay/x402
Auth: OK-ACCESS-* headers signed with API key/secret/passphrase.
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

OKX_API_KEY = os.environ.get("OKX_API_KEY", "")
OKX_SECRET = os.environ.get("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "")


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
        "extra": {"name": "USDT", "version": "1"},
    }
    return {
        "x402Version": 2,
        "resource": resource,          # top-level too, for lenient parsers
        "accepts": [accept],
    }


async def verify_payment(client: httpx.AsyncClient, payment_payload: dict,
                         requirements: dict) -> dict:
    path = "/api/v6/pay/x402/verify"
    body = json.dumps({"x402Version": 2, "paymentPayload": payment_payload,
                       "paymentRequirements": requirements})
    r = await client.post(OKX_BASE + path, headers=_headers("POST", path, body),
                          content=body, timeout=30)
    r.raise_for_status()
    return r.json()


async def settle_payment(client: httpx.AsyncClient, payment_payload: dict,
                         requirements: dict) -> dict:
    path = "/api/v6/pay/x402/settle"
    body = json.dumps({"x402Version": 2, "paymentPayload": payment_payload,
                       "paymentRequirements": requirements})
    r = await client.post(OKX_BASE + path, headers=_headers("POST", path, body),
                          content=body, timeout=30)
    r.raise_for_status()
    return r.json()


def decode_x_payment(header_value: str) -> dict:
    raw = base64.b64decode(header_value)
    return json.loads(raw)
