"""Test the empty-body fail-safe: no settle on invalid body, and no fabricated
score when nothing real was vetted."""
import asyncio
import base64
import json
import os

os.environ.setdefault("XLAYER_RPC", "")  # no RPC locally

from fastapi.testclient import TestClient

import app.main as main
from app.vetting import vet_agent, _has_real_data

# --- Track whether settle is ever called ---
SETTLE_CALLS = []
VERIFY_CALLS = []


async def fake_verify(client, payload, accepted):
    VERIFY_CALLS.append(accepted)
    return {"body": {"code": "0", "data": [{"isValid": True,
                                            "payer": PAYER_ADDR}]}}


async def fake_settle(client, payload, accepted):
    SETTLE_CALLS.append(accepted)
    return {"body": {"code": "0", "data": [{"success": True,
                                            "payer": PAYER_ADDR}]}}


PAYER_ADDR = "0xc385e2df2aa27a3fbe809e0faf7c5c357b716c63"

# Use the REAL outcome() so payer extraction is exercised end to end.
from app.x402 import outcome as real_outcome  # noqa: E402

from contextlib import contextmanager  # noqa: E402

from app.resolver import ResolveError  # noqa: E402

EMPTY_REVIEWS = {"list": [], "distribution": {}, "total": 0}


@contextmanager
def resolver_returns(agent, reviews=None):
    """Stub server-side resolution so tests never depend on live credentials."""
    original = main.resolve_agent

    async def fake_resolve(agent_id):
        return dict(agent), dict(reviews or EMPTY_REVIEWS)

    main.resolve_agent = fake_resolve
    main._RESOLVE_CACHE.clear()  # stub must control the outcome, not the cache
    try:
        yield
    finally:
        main.resolve_agent = original
        main._RESOLVE_CACHE.clear()


@contextmanager
def resolver_raises(detail):
    """Stub a resolution failure (no marketplace session, unknown id, ...)."""
    original = main.resolve_agent

    async def fake_resolve(agent_id):
        raise ResolveError(detail)

    main.resolve_agent = fake_resolve
    main._RESOLVE_CACHE.clear()  # stub must control the outcome, not the cache
    try:
        yield
    finally:
        main.resolve_agent = original
        main._RESOLVE_CACHE.clear()


main.x402.verify_payment = fake_verify
main.x402.settle_payment = fake_settle
# leave main.x402.outcome as the real implementation

client = TestClient(main.app)

PAY_HEADER = base64.b64encode(json.dumps(
    {"scheme": "exact", "payload": {}}).encode()).decode()

FULL_AGENT = {
    "agent": {
        "agentId": "4984",
        "ownerAddress": "0xcd782ca4a7dbd69f31229cc702292020aa8277c4",
        "createdAt": 1783702468285,
        "securityRate": None,
        "soldCount": 0,
        "onlineStatus": 1,
    },
    "reviews": {},
}


def test_empty_body_400_no_settle():
    SETTLE_CALLS.clear()
    VERIFY_CALLS.clear()
    r = client.post("/vet_agent", headers={"X-PAYMENT": PAY_HEADER}, json={})
    # Business validation errors must NOT be 402 (reserved for payment challenge)
    assert r.status_code == 400, r.status_code
    body = r.json()
    assert body.get("error") == "invalid_request", body
    assert "agent_id" in body.get("detail", "") or "agentId" in body.get("detail", "")
    assert len(SETTLE_CALLS) == 0, f"settle was called! {SETTLE_CALLS}"
    assert len(VERIFY_CALLS) == 0, "verify must not run before body validation"
    print("PASS: empty body -> 400 invalid_request, settle/verify NOT called")


def test_partial_agent_resolves_by_id():
    """agent_id alone is the buyer contract: a partial object carrying only an
    id is upgraded to a server-side lookup, not rejected."""
    SETTLE_CALLS.clear()
    VERIFY_CALLS.clear()
    with resolver_returns(FULL_AGENT["agent"]):
        r = client.post("/vet_agent", headers={"X-PAYMENT": PAY_HEADER},
                        json={"agent": {"agentId": "4984"}})
    assert r.status_code == 200, (r.status_code, r.text)
    body = r.json()
    assert body.get("agent_id") == "4984", body
    assert isinstance(body.get("trust_score"), int), body
    assert len(SETTLE_CALLS) == 1, f"expected exactly 1 settle, got {SETTLE_CALLS}"
    print("PASS: partial agent (id only) -> 200 report via server resolve")


def test_unresolvable_id_404_no_settle():
    """When the id cannot be resolved and the buyer sent no usable snapshot,
    fail with 404 — never 402, and never charge."""
    SETTLE_CALLS.clear()
    VERIFY_CALLS.clear()
    with resolver_raises("marketplace session unavailable"):
        r = client.post("/vet_agent", headers={"X-PAYMENT": PAY_HEADER},
                        json={"agent_id": "4984"})
    assert r.status_code == 404, (r.status_code, r.text)
    assert r.json().get("error") == "agent_not_found", r.json()
    assert len(SETTLE_CALLS) == 0, f"settle was called! {SETTLE_CALLS}"
    assert len(VERIFY_CALLS) == 0, "verify must not run when resolve failed"
    print("PASS: unresolvable agent_id -> 404 agent_not_found, settle NOT called")

    # Agent object missing id entirely and incomplete → 400 before verify
    r2 = client.post("/vet_agent", headers={"X-PAYMENT": PAY_HEADER},
                     json={"agent": {"name": "x"}})
    assert r2.status_code == 400, r2.status_code
    assert r2.json().get("error") == "invalid_request"
    assert len(SETTLE_CALLS) == 0
    print("PASS: incomplete agent without id -> 400 invalid_request")


def test_outcome_extracts_payer():
    from app.x402 import outcome, _extract_payer
    ok, reason, payer = outcome(
        {"body": {"code": "0", "data": [{"success": True, "payer": PAYER_ADDR}]}})
    assert ok and payer == PAYER_ADDR, (ok, payer)
    # alternate field names / nesting (aggr_deferred style)
    ok2, _, payer2 = outcome(
        {"body": {"code": "0", "data": {"isValid": True,
                                        "authorization": {"from": PAYER_ADDR}}}})
    assert ok2 and payer2 == PAYER_ADDR, (ok2, payer2)
    # failure path still returns a payer if the body carried one
    ok3, reason3, payer3 = outcome(
        {"body": {"code": "0", "data": [{"success": False,
                                         "invalidReason": "expired",
                                         "payerAddress": PAYER_ADDR}]}})
    assert (not ok3) and reason3 == "expired" and payer3 == PAYER_ADDR
    # no payer present -> "" (never raises)
    _, _, payer4 = outcome({"body": {"code": "0", "data": [{"isValid": True}]}})
    assert payer4 == ""
    assert _extract_payer({"sender": PAYER_ADDR}) == PAYER_ADDR
    assert _extract_payer(None, {}, {"nope": 1}) == ""
    print("PASS: outcome() returns (ok, reason, payer); _extract_payer robust")


def test_full_agent_settles_and_reports():
    SETTLE_CALLS.clear()
    main.ATTEMPTS.clear()
    r = client.post("/vet_agent", headers={"X-PAYMENT": PAY_HEADER}, json=FULL_AGENT)
    assert r.status_code == 200, (r.status_code, r.text)
    body = r.json()
    assert body.get("agent_id") == "4984", body
    assert isinstance(body.get("trust_score"), int), body
    assert len(SETTLE_CALLS) == 1, f"expected exactly 1 settle, got {SETTLE_CALLS}"
    # payer must be captured in the settle + delivered log entries
    details = {a["stage"]: a["detail"] for a in main.ATTEMPTS}
    assert PAYER_ADDR in details.get("settle", ""), details.get("settle")
    assert PAYER_ADDR in details.get("delivered", ""), details.get("delivered")
    print("PASS: full agent -> 200 report, trust_score =", body["trust_score"],
          ", settle called once, payer logged:", PAYER_ADDR[:10] + "…")


def test_engine_withholds_score_on_empty():
    rep = asyncio.run(vet_agent({}, {}, ""))
    d = rep.to_dict()
    assert d["trust_score"] is None, d
    assert "No trust score" in d["summary"]
    print("PASS: engine on empty agent -> trust_score is None (no fabricated number)")


def test_has_real_data():
    assert _has_real_data(FULL_AGENT["agent"]) is True
    assert _has_real_data({}) is False
    assert _has_real_data({"agentId": "4984"}) is False
    assert _has_real_data({"agentId": "4984", "ownerAddress": "0x",
                           "createdAt": ""}) is False
    print("PASS: _has_real_data field checks")


if __name__ == "__main__":
    test_has_real_data()
    test_engine_withholds_score_on_empty()
    test_outcome_extracts_payer()
    test_empty_body_400_no_settle()
    test_partial_agent_resolves_by_id()
    test_unresolvable_id_404_no_settle()
    test_full_agent_settles_and_reports()
    print("\nALL GREEN")
