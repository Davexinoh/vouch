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


def test_empty_body_402_no_settle():
    SETTLE_CALLS.clear()
    VERIFY_CALLS.clear()
    r = client.post("/vet_agent", headers={"X-PAYMENT": PAY_HEADER}, json={})
    assert r.status_code == 402, r.status_code
    body = r.json()
    assert body.get("error") == "invalid_request", body
    assert "agentId" in body.get("detail", "")
    assert len(SETTLE_CALLS) == 0, f"settle was called! {SETTLE_CALLS}"
    print("PASS: empty body -> 402 invalid_request, settle NOT called")
    print("      verify calls:", len(VERIFY_CALLS), "| settle calls:", len(SETTLE_CALLS))


def test_stub_agent_402_no_settle():
    SETTLE_CALLS.clear()
    r = client.post("/vet_agent", headers={"X-PAYMENT": PAY_HEADER},
                    json={"agent": {"agentId": "4984"}})  # missing owner/createdAt
    assert r.status_code == 402, r.status_code
    assert r.json().get("error") == "invalid_request"
    assert len(SETTLE_CALLS) == 0
    print("PASS: stub agent (partial) -> 402 invalid_request, settle NOT called")


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
    test_empty_body_402_no_settle()
    test_stub_agent_402_no_settle()
    test_full_agent_settles_and_reports()
    print("\nALL GREEN")
