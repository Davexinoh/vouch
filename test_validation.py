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
    return {"body": {"success": True, "isValid": True}}


async def fake_settle(client, payload, accepted):
    SETTLE_CALLS.append(accepted)
    return {"body": {"success": True}}


def fake_outcome(resp):
    b = resp.get("body", {})
    return (bool(b.get("success")), "ok" if b.get("success") else "fail")


main.x402.verify_payment = fake_verify
main.x402.settle_payment = fake_settle
main.x402.outcome = fake_outcome

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


def test_full_agent_settles_and_reports():
    SETTLE_CALLS.clear()
    r = client.post("/vet_agent", headers={"X-PAYMENT": PAY_HEADER}, json=FULL_AGENT)
    assert r.status_code == 200, (r.status_code, r.text)
    body = r.json()
    assert body.get("agent_id") == "4984", body
    assert isinstance(body.get("trust_score"), int), body
    assert len(SETTLE_CALLS) == 1, f"expected exactly 1 settle, got {SETTLE_CALLS}"
    print("PASS: full agent -> 200 report, trust_score =", body["trust_score"],
          ", settle called once")


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
    test_empty_body_402_no_settle()
    test_stub_agent_402_no_settle()
    test_full_agent_settles_and_reports()
    print("\nALL GREEN")
