"""Vouch API — the A2MCP service. Live on X Layer mainnet.

Endpoint contract:
  GET  /health                     -> status, no payment
  POST /vet_agent  (paid, x402)    -> full vet report on an agent

Payment gating uses OKX x402 facilitator (verify + settle).
Unpaid calls get a 402 with payment requirements.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .vetting import vet_agent
from . import x402

app = FastAPI(title="Vouch", version="1.0.0")

XLAYER_RPC = os.environ.get("XLAYER_RPC", "")
VET_PRICE = os.environ.get("VET_PRICE", "1000000")  # smallest unit; confirm USDT decimals
SELF_URL = os.environ.get("SELF_URL", "https://vouch.onrender.com")

READY = {"vet_agent": True}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class VetRequest(BaseModel):
    # Buyer passes the target agent's identity + reviews, OR just an agent_id
    # that we resolve via the OKX agent-search API (wired at deploy).
    agent: dict
    reviews: dict = {}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "vouch", "ready": READY,
            "fetched_at": _now_iso()}


@app.post("/vet_agent")
async def vet_agent_endpoint(req: VetRequest, request: Request):
    resource_url = f"{SELF_URL}/vet_agent"
    requirements = x402.payment_requirements(
        VET_PRICE, resource_url, "Vouch agent due-diligence report")

    # 1. no payment header -> 402 challenge
    pay_header = request.headers.get("X-PAYMENT")
    if not pay_header:
        return JSONResponse(
            status_code=402,
            content=requirements,
            headers={"PAYMENT-REQUIRED": ""},
        )

    # 2. verify with OKX facilitator
    async with httpx.AsyncClient() as client:
        payload = x402.decode_x_payment(pay_header)
        verify = await x402.verify_payment(client, payload, requirements["accepts"][0])
        if not verify.get("data", {}).get("success", False):
            return JSONResponse(status_code=402, content={
                "error": "payment_verification_failed",
                "detail": verify.get("data", {}).get("errorReason")})

        # 3. do the work
        report = await vet_agent(req.agent, req.reviews, XLAYER_RPC)

        # 4. settle
        settle = await x402.settle_payment(client, payload, requirements["accepts"][0])
        if not settle.get("data", {}).get("success", False):
            return JSONResponse(status_code=402, content={
                "error": "settlement_failed",
                "detail": settle.get("data", {}).get("errorReason")})

    return report.to_dict()
