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

from .vetting import vet_agent, _has_real_data, REQUIRED_AGENT_FIELDS
from . import x402
from .resolver import resolve_agent, ResolveError, resolve_ready

app = FastAPI(title="Vouch", version="1.6.0")

XLAYER_RPC = os.environ.get("XLAYER_RPC", "")
VET_PRICE = os.environ.get("VET_PRICE", "1000000")
SELF_URL = os.environ.get(
    "SELF_URL", "https://vouch-production-cbf2.up.railway.app"
).rstrip("/")

READY = {"vet_agent": True}
ATTEMPTS: deque = deque(maxlen=20)

SAMPLE_DATA_PATH = Path(__file__).parent / "sample_data.json"
SAMPLE_NOTE = (
    "This is Vouch's public self-audit. Paid reports run the same "
    "engine on any agent. Prefer POST {\"agent_id\": \"<id>\"} — Vouch "
    "resolves identity server-side."
)

# Request body schema (documented on GET /sample and GET /schema)
REQUEST_SCHEMA = {
    "preferred": {
        "agent_id": "5127",
        "description": (
            "Numeric marketplace agent id (int or string). "
            "Vouch fetches ownerAddress, createdAt, reviews server-side."
        ),
    },
    "accepted_aliases": {
        "agent_id": ["agent_id", "agentId", "id"],
        "agent_object_keys": ["agent", "identity", "target"],
        "required_on_agent_object": list(REQUIRED_AGENT_FIELDS),
    },
    "fallback_body": {
        "agent": {
            "agentId": "5127",
            "ownerAddress": "0x…",
            "createdAt": 1783835043299,
            "name": "optional",
            "onlineStatus": 1,
            "soldCount": 0,
            "securityRate": None,
            "services": [{"endpoint": "https://…"}],
        },
        "reviews": {
            "list": [],
            "distribution": {},
            "total": 0,
        },
    },
    "payment": {
        "unpaid": "HTTP 402 + PAYMENT-REQUIRED header (x402 v2)",
        "paid_headers": ["X-PAYMENT", "PAYMENT-SIGNATURE"],
        "success": "HTTP 200 JSON report",
        "business_errors": "HTTP 400/404 JSON — never 402 after a paid attempt",
    },
}


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
        'Vouch agent due-diligence report. POST {"agent_id":"<id>"} '
        "(preferred — server resolves identity) or "
        '{"agent":{"agentId","ownerAddress","createdAt"}, "reviews":{…}}. '
        "See GET /sample and GET /schema.",
    )


def _challenge(extra: dict | None = None) -> JSONResponse:
    """Payment-required challenge ONLY. Do not use for business errors."""
    content = _requirements()
    header_payload = _b64.b64encode(
        _json.dumps(content, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    ).decode("ascii")
    if extra:
        content = {**extra, **content}
    return JSONResponse(
        status_code=402,
        content=content,
        headers={
            "PAYMENT-REQUIRED": header_payload,
            "Content-Encoding": "identity",
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": (
                "Content-Type, X-PAYMENT, PAYMENT-SIGNATURE, PAYMENT-REQUIRED"
            ),
            "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS, POST",
            "Access-Control-Expose-Headers": "PAYMENT-REQUIRED, PAYMENT-RESPONSE",
        },
    )


def _err(status: int, error: str, detail: str, **extra) -> JSONResponse:
    """Business / client error — never 402 (reserved for payment challenge)."""
    body = {"error": error, "detail": detail, **extra}
    return JSONResponse(
        status_code=status,
        content=body,
        headers={
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*",
            "Content-Encoding": "identity",
        },
    )


KEEPALIVE_INTERVAL = int(os.environ.get("KEEPALIVE_SECONDS", "300"))


async def _self_ping():
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
    if KEEPALIVE_INTERVAL > 0:
        asyncio.create_task(_self_ping())


@app.get("/health")
async def health():
    resolve = await resolve_ready()
    return {
        "status": "ok",
        "service": "vouch",
        "version": "1.6.0",
        "ready": READY,
        "credentials_configured": x402.credentials_present(),
        "marketplace_resolve": resolve,
        "fetched_at": _now_iso(),
    }


@app.get("/status")
async def status():
    return {
        "service": "vouch",
        "fetched_at": _now_iso(),
        "credentials_configured": x402.credentials_present(),
        "recent_attempts": list(ATTEMPTS),
    }


@app.get("/schema")
async def schema():
    """Explicit request/response contract for buyers and OKX validators."""
    return {
        "service": "vouch",
        "endpoint": f"{SELF_URL}/vet_agent",
        "methods": {
            "GET /vet_agent": "402 payment challenge (availability probe)",
            "POST /vet_agent": "paid report (X-PAYMENT or PAYMENT-SIGNATURE)",
            "GET /sample": "free sample report + request schema",
            "GET /schema": "this document",
            "GET /health": "liveness + marketplace_resolve status",
        },
        "request": REQUEST_SCHEMA,
        "price_usdt": float(VET_PRICE) / 1_000_000,
        "network": "eip155:196",
    }


@app.get("/vet_agent")
@app.head("/vet_agent")
@app.options("/vet_agent")
async def vet_agent_probe():
    return _challenge()


def _select_accepted(requirements: dict, payment_payload: dict) -> dict:
    accepts = requirements["accepts"]
    buyer_accepted = payment_payload.get("accepted")
    if isinstance(buyer_accepted, dict) and buyer_accepted.get("scheme"):
        scheme = buyer_accepted.get("scheme")
        match = next((a for a in accepts if a.get("scheme") == scheme), None)
        if match:
            merged = {
                **match,
                **{
                    k: buyer_accepted[k]
                    for k in (
                        "amount", "asset", "payTo", "network", "extra",
                        "maxTimeoutSeconds", "resource", "scheme",
                    )
                    if k in buyer_accepted
                },
            }
            return merged
        return buyer_accepted

    scheme = payment_payload.get("scheme")
    if not scheme and isinstance(payment_payload.get("payload"), dict):
        scheme = payment_payload["payload"].get("scheme")
    if scheme:
        match = next((a for a in accepts if a.get("scheme") == scheme), None)
        if match:
            return match

    accepted = buyer_accepted if isinstance(buyer_accepted, dict) else {}
    extra = accepted.get("extra") if isinstance(accepted.get("extra"), dict) else {}
    inner = (
        payment_payload.get("payload")
        if isinstance(payment_payload.get("payload"), dict)
        else {}
    )
    wanted = (
        "aggr_deferred"
        if ("sessionCert" in extra or "sessionCert" in inner)
        else "exact"
    )
    return next((a for a in accepts if a.get("scheme") == wanted), accepts[0])


def _coerce_agent_id(raw) -> str | None:
    """Return digit string, '' if present-but-invalid, None if absent."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # strip leading # (buyers sometimes send "#5127")
    if s.startswith("#"):
        s = s[1:].strip()
    return s if s.isdigit() else ""


def _pick_agent_blob(body: dict) -> dict | None:
    """Find an agent-like object under common keys."""
    if not isinstance(body, dict):
        return None
    for key in ("agent", "identity", "target", "profile"):
        v = body.get(key)
        if isinstance(v, dict) and v:
            return v
    # Some clients send the agent fields at the top level
    if any(k in body for k in ("agentId", "ownerAddress", "createdAt")):
        return {
            k: body[k]
            for k in (
                "agentId", "ownerAddress", "createdAt", "name",
                "onlineStatus", "soldCount", "securityRate", "services",
                "agentWalletAddress", "profileDescription", "profilePicture",
            )
            if k in body
        }
    return None


def _normalize_agent(agent: dict, agent_id: str | None = None) -> dict:
    """Map common aliases → canonical marketplace fields."""
    if not isinstance(agent, dict):
        agent = {}
    out = dict(agent)

    # id aliases
    aid = (
        out.get("agentId")
        or out.get("agent_id")
        or out.get("id")
        or agent_id
    )
    if aid is not None:
        s = str(aid).strip().lstrip("#")
        if s:
            out["agentId"] = s

    # owner aliases
    owner = (
        out.get("ownerAddress")
        or out.get("owner_address")
        or out.get("owner")
        or out.get("agentWalletAddress")
        or out.get("walletAddress")
    )
    if owner:
        out["ownerAddress"] = str(owner).strip()

    # createdAt aliases (ms epoch)
    created = (
        out.get("createdAt")
        or out.get("created_at")
        or out.get("createTime")
        or out.get("created")
    )
    if created is not None and created != "":
        try:
            out["createdAt"] = int(created)
        except (TypeError, ValueError):
            out["createdAt"] = created

    return out


def _extract_request(body: dict) -> tuple[str | None, dict | None, dict, str | None]:
    """
    Returns (agent_id | None, agent_dict | None, reviews, error_detail | None).
    error_detail set when agent_id present but invalid.
    """
    if not isinstance(body, dict):
        body = {}

    raw_id = None
    for k in ("agent_id", "agentId", "id"):
        if body.get(k) is not None:
            raw_id = body.get(k)
            break
    # also allow agent.agentId as the id source
    agent_blob = _pick_agent_blob(body)
    if raw_id is None and isinstance(agent_blob, dict):
        for k in ("agentId", "agent_id", "id"):
            if agent_blob.get(k) is not None:
                raw_id = agent_blob.get(k)
                break

    agent_id = _coerce_agent_id(raw_id)
    if agent_id == "":
        return None, None, {}, f"agent_id must be numeric, got {raw_id!r}"

    reviews = body.get("reviews") if isinstance(body.get("reviews"), dict) else {}
    agent = _normalize_agent(agent_blob or {}, agent_id)
    if not agent and agent_id is None:
        return None, None, reviews, None
    if not agent:
        agent = None
    return agent_id, agent, reviews, None


@app.post("/vet_agent")
async def vet_agent_post(request: Request):
    requirements = _requirements()

    pay_header = (
        request.headers.get("X-PAYMENT")
        or request.headers.get("PAYMENT-SIGNATURE")
    )
    if not pay_header:
        _log_attempt("challenge", True, "unpaid POST, 402 issued")
        return _challenge()

    # --- decode payment header (malformed → 402, still payment-layer) ---
    try:
        payload = x402.decode_x_payment(pay_header)
    except Exception as e:
        _log_attempt("decode", False, f"bad payment header: {e}")
        return _challenge({
            "error": "invalid_payment_header",
            "detail": "payment header must be base64-encoded JSON",
        })

    try:
        body = await request.json()
    except Exception:
        body = {}

    # --- validate business input BEFORE verify/settle (OKX feedback #4) ---
    agent_id, agent_obj, reviews, id_err = _extract_request(
        body if isinstance(body, dict) else {}
    )
    if id_err:
        _log_attempt("validate", False, id_err)
        return _err(400, "invalid_agent_id", id_err, schema=REQUEST_SCHEMA)

    if agent_id is None and not (agent_obj and _has_real_data(agent_obj)):
        detail = (
            "body must include agent_id (preferred) or a full agent object "
            f"with {', '.join(REQUIRED_AGENT_FIELDS)}. See GET /schema."
        )
        _log_attempt("validate", False, detail)
        return _err(400, "invalid_request", detail, schema=REQUEST_SCHEMA)

    # If only agent_id: resolve server-side BEFORE charging (OKX feedback #2)
    resolved_via = None
    if agent_id is not None:
        try:
            agent_obj, reviews = await resolve_agent(agent_id)
            resolved_via = "server"
            _log_attempt("resolve", True, f"resolved agent_id={agent_id}")
        except ResolveError as e:
            # Fallback to buyer-supplied snapshot if it is complete
            if agent_obj and _has_real_data(agent_obj):
                agent_obj = _normalize_agent(agent_obj, agent_id)
                resolved_via = "body_snapshot"
                _log_attempt(
                    "resolve", True,
                    f"agent_id={agent_id} via body snapshot after: {e}",
                )
            else:
                _log_attempt("resolve", False, str(e))
                return _err(
                    404,
                    "agent_not_found",
                    (
                        f"Could not resolve agent_id={agent_id} server-side: {e}. "
                        "Retry with a full agent object "
                        f"({', '.join(REQUIRED_AGENT_FIELDS)}) or fix marketplace "
                        "credentials (OKX_API_KEY / OKX_ACCESS_TOKEN)."
                    ),
                    agent_id=agent_id,
                )
    else:
        agent_obj = _normalize_agent(agent_obj or {})
        if not _has_real_data(agent_obj):
            detail = (
                f"agent object missing required fields "
                f"{REQUIRED_AGENT_FIELDS}. See GET /schema."
            )
            _log_attempt("validate", False, detail)
            return _err(400, "invalid_request", detail, schema=REQUEST_SCHEMA)
        resolved_via = "body"

    accepted = _select_accepted(requirements, payload)
    try:
        async with httpx.AsyncClient() as client:
            verify = await x402.verify_payment(client, payload, accepted)
            v_ok, v_reason, v_payer = x402.outcome(verify)
            payer = v_payer
            _log_attempt(
                "verify",
                v_ok,
                (
                    f"payer={v_payer or '?'} | {v_reason}".strip(" |")
                    if v_ok
                    else (
                        f"payer={v_payer or '?'} | {v_reason} | "
                        f"raw={_json.dumps(verify.get('body'), ensure_ascii=False)[:400]}"
                    )
                ),
            )
            if not v_ok:
                # Payment-layer failure → 402 challenge (buyer can retry pay)
                return _challenge({
                    "error": "payment_verification_failed",
                    "detail": v_reason,
                    "facilitator": verify.get("body"),
                })

            report = await vet_agent(agent_obj, reviews, XLAYER_RPC)

            settle = await x402.settle_payment(client, payload, accepted)
            s_ok, s_reason, s_payer = x402.outcome(settle)
            payer = s_payer or payer
            _log_attempt(
                "settle",
                s_ok,
                (
                    f"payer={payer or '?'} settled {accepted.get('amount', '?')} "
                    f"{accepted.get('asset', '?')} scheme={accepted.get('scheme', '?')} "
                    f"via={resolved_via}"
                    if s_ok
                    else (
                        f"payer={payer or '?'} | {s_reason} | "
                        f"raw={_json.dumps(settle.get('body'), ensure_ascii=False)[:400]}"
                    )
                ),
            )
            if not s_ok:
                return _challenge({
                    "error": "settlement_failed",
                    "detail": s_reason,
                    "facilitator": settle.get("body"),
                })
    except httpx.HTTPError as e:
        _log_attempt("facilitator", False, f"unreachable: {e}")
        return _err(503, "facilitator_unavailable", str(e))

    _log_attempt("delivered", True, f"report returned to payer={payer or '?'}")
    out = report.to_dict()
    out["resolved_via"] = resolved_via
    return out


@app.get("/sample")
async def sample():
    """Free sample report + the request schema buyers should use."""
    try:
        raw = _json.loads(SAMPLE_DATA_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        _log_attempt("sample", False, f"sample_data.json unreadable: {e}")
        return _err(502, "sample_unavailable", str(e))

    agent = raw.get("agent", {}) or {}
    reviews = raw.get("reviews", {}) or {}
    snapshot_at = raw.get("snapshot_at")

    report = await vet_agent(agent, reviews, XLAYER_RPC)
    _log_attempt("sample", True, f"vetted from snapshot {snapshot_at}")

    payload = report.to_dict()
    payload["snapshot_at"] = snapshot_at
    payload["note"] = SAMPLE_NOTE
    payload["request_schema"] = REQUEST_SCHEMA
    payload["how_to_call"] = {
        "preferred": 'POST /vet_agent  body: {"agent_id": "5127"}  + payment header',
        "fallback": (
            'POST /vet_agent  body: {"agent": {"agentId","ownerAddress","createdAt"}, '
            '"reviews": {...}}  + payment header'
        ),
        "docs": [
            f"{SELF_URL}/schema",
            f"{SELF_URL}/sample",
            f"{SELF_URL}/health",
        ],
    }
    return payload
