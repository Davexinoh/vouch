"""Vouch API — the A2MCP service. Live on X Layer mainnet.

Endpoint contract:
  GET/HEAD/OPTIONS/POST /vet_agent without valid payment -> standard 402 challenge
  POST /vet_agent with valid X-PAYMENT                   -> full vet report
  GET  /health                                           -> status, no payment

Any unpaid or malformed probe on the paid resource gets a clean 402 with
payment terms. The server never 500s on bad input — a broken payment header
is a payment problem (402), not a server problem.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .vetting import vet_agent
from . import x402

app = FastAPI(title="Vouch", version="1.2.0")

XLAYER_RPC = os.environ.get("XLAYER_RPC", "")
VET_PRICE = os.environ.get("VET_PRICE", "1000000")
SELF_URL = os.environ.get("SELF_URL", "https://vouch-4ib4.onrender.com")

READY = {"vet_agent": True}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _requirements() -> dict:
    return x402.payment_requirements(
        VET_PRICE, f"{SELF_URL}/vet_agent",
        "Vouch agent due-diligence report")


def _challenge(extra: dict | None = None) -> JSONResponse:
    """Standard 402. The payment requirements are carried BOTH ways:

    1. PAYMENT-REQUIRED header: base64-encoded JSON of the requirements.
       This is what x402 v2 validators parse. An empty or missing header
       reads as "accepts is empty" regardless of the body.
    2. JSON body: same object, human- and curl-readable.

    Content-Encoding: identity stops the CDN from brotli-compressing the
    body for clients that offer br but do not decode it.
    """
    import base64 as _b64
    import json as _json

    content = _requirements()
    header_payload = _b64.b64encode(
        _json.dumps(content, separators=(",", ":")).encode()
    ).decode()
    if extra:
        content = {**extra, **content}
    return JSONResponse(
        status_code=402,
        content=content,
        headers={
            "PAYMENT-REQUIRED": header_payload,
            "Content-Encoding": "identity",
            "Cache-Control": "no-store",
        },
    )

@app.get("/health")
async def health():
    return {"status": "ok", "service": "vouch", "ready": READY,
            "fetched_at": _now_iso()}


# --- paid resource: every method answered, never 405, never 500 ---

@app.get("/vet_agent")
@app.head("/vet_agent")
@app.options("/vet_agent")
async def vet_agent_probe():
    """Discovery / availability probe -> advertise payment terms."""
    return _challenge()


@app.post("/vet_agent")
async def vet_agent_post(request: Request):
    requirements = _requirements()

    pay_header = (request.headers.get("X-PAYMENT")
                  or request.headers.get("PAYMENT-SIGNATURE"))
    if not pay_header:
        return _challenge()

    # malformed payment header is a payment error, not a server error
    try:
        payload = x402.decode_x_payment(pay_header)
    except Exception:
        return _challenge({"error": "invalid_payment_header",
                           "detail": "X-PAYMENT must be base64-encoded JSON"})

    try:
        body = await request.json()
    except Exception:
        body = {}
    agent = body.get("agent", {}) or {}
    reviews = body.get("reviews", {}) or {}

    accepted = requirements["accepts"][0]
    try:
        async with httpx.AsyncClient() as client:
            verify = await x402.verify_payment(client, payload, accepted)
            if not verify.get("data", {}).get("success", False):
                return _challenge({
                    "error": "payment_verification_failed",
                    "detail": verify.get("data", {}).get("invalidReason")
                              or verify.get("data", {}).get("errorReason")})

            report = await vet_agent(agent, reviews, XLAYER_RPC)

            settle = await x402.settle_payment(client, payload, accepted)
            if not settle.get("data", {}).get("success", False):
                return _challenge({
                    "error": "settlement_failed",
                    "detail": settle.get("data", {}).get("errorReason")})
    except httpx.HTTPError as e:
        # facilitator unreachable: fail closed, no free work, no 500
        return _challenge({"error": "facilitator_unavailable",
                           "detail": str(e)})

    return report.to_dict()
