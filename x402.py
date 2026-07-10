"""OKX x402 payment gating. Facilitator pattern — OKX verifies and settles.

Flow per the OKX Onchain OS payment docs:
  1. Buyer calls endpoint with no X-PAYMENT header  -> we return 402 + requirements
  2. Buyer retries with X-PAYMENT (base64 signed payload)
  3. We POST to OKX /verify. If valid, do the work.
  4. We POST to OKX /settle. On success:true, return the resource.

X Layer network id (CAIP-2): eip155:196
Settle/verify base: https://web3.okx.com/api/v6/pay/x402
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
# USDT on X Layer. Confirm the exact asset address in the OKX token list before go-live.
USDT_XLAYER = os.environ.get("USDT_XLAYER_ADDRESS", "")
PAY_TO = os.environ.get("VOUCH_PAYOUT_WALLET", "")

OKX_API_KEY = os.environ.get("OKX_API_KEY", "")
OKX_SECRET = os.environ.get("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "")


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


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


def payment_requirements(price_usdt: str, resource_url: str, description: str) -> dict:
    """The 402 body. amount is in smallest unit; USDT has 6 decimals on most chains.
    Confirm decimals for X Layer USDT before go-live.
    """
    # price given like "1.00" -> smallest unit. Adjust decimals once asset confirmed.
    return {
        "x402Version": 2,
        "accepts": [{
            "scheme": "exact",
            "network": XLAYER_CAIP2,
            "amount": price_usdt,        # set as smallest-unit string once decimals confirmed
            "asset": USDT_XLAYER,
            "payTo": PAY_TO,
            "maxTimeoutSeconds": 60,
            "resource": {"url": resource_url, "description": description,
                         "mimeType": "application/json"},
        }],
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
    """X-PAYMENT header is base64 JSON."""
    raw = base64.b64decode(header_value)
    return json.loads(raw)
