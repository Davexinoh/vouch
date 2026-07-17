"""Vouch API — the A2MCP service. Live on X Layer mainnet."""
from __future__ import annotations

import base64 as _b64
import json as _json
import os
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import asyncio
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .vetting import vet_agent
from . import x402
from .resolver import resolve_agent, ResolveError

app = FastAPI(title="Vouch", version="1.5.0")

XLAYER_RPC = os.environ.get("XLAYER_RPC", "")
VET_PRICE = os.environ.get("VET_PRICE", "1000000")
SELF_URL = os.environ.get("SELF_URL", "https://vouch-4ib4.onrender.com")

READY = {"vet_agent": True}
ATTEMPTS: deque = deque(maxlen=20)

SAMPLE_DATA_PATH = Path(__file__).parent / "sample_data.json"
SAMPLE_NOTE = ("This is Vouch's public self-audit. Paid reports run the same "
               "engine on any agent.")
AGENT_ID_COMING_SOON = ("pass full agent+reviews objects to vet immediately; "
                        "agent_id resolution coming.")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log_attempt(stage: str, ok: bool, detail: str = "") -> None:
    entry = {"fetched_at": _now_iso(), "stage": stage, "ok": ok,
             "detail": detail[:300]}
    ATTEMPTS.append(entry)
    print(f"[vouch] {entry}", flush=True)


def _requirements() -> dict:
    return x402.payment_requirements(
        VET_PRICE, f"{SELF_URL}/vet_agent",
        "Vouch agent due-diligence report")


def _challenge(extra: dict | None = None) -> JSONResponse:
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



KEEPALIVE_INTERVAL = int(os.environ.get("KEEPALIVE_SECONDS", "300"))

async def _self_ping():
    """Hit our own /health every 5 min so Render free tier never sleeps."""
    await asyncio.sleep(10)
    while True:
        try:
            async with httpx.AsyncClient() as c:
                await c.get(f"{SELF_URL}/health", timeout=10)
        except Exception:
            pass
        await asyncio.sleep(KEEPALIVE_INTERVAL)


@app.on_event("startup")
async def _start_keepalive():
    asyncio.create_task(_self_ping())


@app.get("/health")
async def health():
    return {"status": "ok", "service": "vouch", "ready": READY,
            "credentials_configured": x402.credentials_present(),
            "fetched_at": _now_iso()}


@app.get("/status")
async def status():
    return {"service": "vouch", "fetched_at": _now_iso(),
            "credentials_configured": x402.credentials_present(),
            "recent_attempts": list(ATTEMPTS)}


@app.get("/vet_agent")
@app.head("/vet_agent")
@app.options("/vet_agent")
async def vet_agent_probe():
    return _challenge()


def _select_accepted(requirements: dict, payment_payload: dict) -> dict:
    """Pick the accepts[] entry matching the buyer's payload, not always [0] --
    exact (EOA) and aggr_deferred (OKX agentic/AA wallets) now both appear in
    accepts, and verify/settle must be called with the entry the buyer signed
    against.

    onchainos / x402 v2 puts scheme on paymentPayload.accepted.scheme (and for
    aggr_deferred, sessionCert on accepted.extra) — not on the top-level payload.
    Prefer the buyer's own accepted object when present so facilitator fields
    match the signed authorization byte-for-byte.
    """
    accepts = requirements["accepts"]
    buyer_accepted = payment_payload.get("accepted")
    if isinstance(buyer_accepted, dict) and buyer_accepted.get("scheme"):
        scheme = buyer_accepted.get("scheme")
        # Prefer seller's current entry of the same scheme (authoritative payTo/asset),
        # but fall back to the buyer's accepted blob if the scheme is no longer offered.
        match = next((a for a in accepts if a.get("scheme") == scheme), None)
        if match:
            # Overlay buyer-signed fields the facilitator checks against the signature
            # (amount/asset/payTo/network/extra) when present — avoids regen drift.
            merged = {**match, **{k: buyer_accepted[k] for k in
                                  ("amount", "asset", "payTo", "network", "extra",
                                   "maxTimeoutSeconds", "resource", "scheme")
                                  if k in buyer_accepted}}
            return merged
        return buyer_accepted

    scheme = payment_payload.get("scheme")
    if not scheme and isinstance(payment_payload.get("payload"), dict):
        scheme = payment_payload["payload"].get("scheme")
    if scheme:
        match = next((a for a in accepts if a.get("scheme") == scheme), None)
        if match:
            return match

    # Infer from shape: sessionCert may live under accepted.extra (agentic) or
    # under payload (some wallets).
    accepted = buyer_accepted if isinstance(buyer_accepted, dict) else {}
    extra = accepted.get("extra") if isinstance(accepted.get("extra"), dict) else {}
    inner = payment_payload.get("payload") if isinstance(payment_payload.get("payload"), dict) else {}
    wanted = "aggr_deferred" if ("sessionCert" in extra or "sessionCert" in inner) else "exact"
    return next((a for a in accepts if a.get("scheme") == wanted), accepts[0])


def _parse_agent_id(body: dict):
    """int or numeric-string 'agent_id' -> normalized str id, else None."""
    raw = body.get("agent_id")
    if raw is None:
        return None
    s = str(raw).strip()
    return s if s.isdigit() else ""  # "" signals present-but-invalid


@app.post("/vet_agent")
async def vet_agent_post(request: Request):
    requirements = _requirements()

    pay_header = (request.headers.get("X-PAYMENT")
                  or request.headers.get("PAYMENT-SIGNATURE"))
    if not pay_header:
        _log_attempt("challenge", True, "unpaid POST, 402 issued")
        return _challenge()

    try:
        payload = x402.decode_x_payment(pay_header)
    except Exception as e:
        _log_attempt("decode", False, f"bad payment header: {e}")
        return _challenge({"error": "invalid_payment_header",
                           "detail": "payment header must be base64-encoded JSON"})

    try:
        body = await request.json()
    except Exception:
        body = {}
    agent_id = _parse_agent_id(body)
    if agent_id == "":
        _log_attempt("resolve", False, f"invalid agent_id: {body.get('agent_id')!r}")
        return _challenge({"error": "invalid_agent_id",
                           "detail": "agent_id must be an int or numeric string"})

    accepted = _select_accepted(requirements, payload)
    try:
        async with httpx.AsyncClient() as client:
            verify = await x402.verify_payment(client, payload, accepted)
            v_ok, v_reason = x402.outcome(verify)
            # Surface raw facilitator body so /status shows real invalidReason
            # instead of a generic "success not true".
            _log_attempt(
                "verify", v_ok,
                v_reason if v_ok else f"{v_reason} | raw={_json.dumps(verify.get('body'), ensure_ascii=False)[:400]}"
            )
            if not v_ok:
                return _challenge({"error": "payment_verification_failed",
                                   "detail": v_reason,
                                   "facilitator": verify.get("body")})

            if agent_id is not None:
                try:
                    agent, reviews = await resolve_agent(agent_id)
                    _log_attempt("resolve", True, f"resolved agent_id={agent_id}")
                except ResolveError as e:
                    # Fallback: buyer (or client agent) may already have the
                    # marketplace snapshot — use it so a missing server JWT
                    # does not brick paid reports. Still not settled yet.
                    body_agent = body.get("agent") if isinstance(body.get("agent"), dict) else None
                    if body_agent:
                        agent = body_agent
                        reviews = body.get("reviews") if isinstance(body.get("reviews"), dict) else {}
                        _log_attempt(
                            "resolve", True,
                            f"agent_id={agent_id} via body snapshot after: {e}"
                        )
                    else:
                        _log_attempt("resolve", False, str(e))
                        # Verified but not settled -- buyer isn't charged for a failed lookup.
                        return _challenge({"error": "agent_not_found",
                                           "detail": f"{e} — {AGENT_ID_COMING_SOON}"})
            else:
                agent = body.get("agent", {}) or {}
                reviews = body.get("reviews", {}) or {}

            report = await vet_agent(agent, reviews, XLAYER_RPC)

            settle = await x402.settle_payment(client, payload, accepted)
            s_ok, s_reason = x402.outcome(settle)
            _log_attempt(
                "settle", s_ok,
                s_reason if s_ok else f"{s_reason} | raw={_json.dumps(settle.get('body'), ensure_ascii=False)[:400]}"
            )
            if not s_ok:
                return _challenge({"error": "settlement_failed",
                                   "detail": s_reason,
                                   "facilitator": settle.get("body")})
    except httpx.HTTPError as e:
        _log_attempt("facilitator", False, f"unreachable: {e}")
        return _challenge({"error": "facilitator_unavailable",
                           "detail": str(e)})

    _log_attempt("delivered", True, "report returned")
    return report.to_dict()


@app.get("/sample")
async def sample():
    try:
        raw = _json.loads(SAMPLE_DATA_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        _log_attempt("sample", False, f"sample_data.json unreadable: {e}")
        return JSONResponse(status_code=502, content={"error": "sample_unavailable",
                                                       "detail": str(e)})

    agent = raw.get("agent", {}) or {}
    reviews = raw.get("reviews", {}) or {}
    snapshot_at = raw.get("snapshot_at")

    report = await vet_agent(agent, reviews, XLAYER_RPC)
    _log_attempt("sample", True, f"vetted from snapshot {snapshot_at}")

    payload = report.to_dict()
    payload["snapshot_at"] = snapshot_at
    payload["note"] = SAMPLE_NOTE
    return payload