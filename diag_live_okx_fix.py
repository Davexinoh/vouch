"""Probe live Vouch after OKX feedback fixes."""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

BASE = "https://vouch-production-cbf2.up.railway.app"
PAY = base64.b64encode(b'{"scheme":"exact","payload":{}}').decode()


def call(method: str, path: str, body: bytes | None = None, paid: bool = False):
    headers = {"Content-Type": "application/json"}
    if paid:
        headers["X-PAYMENT"] = PAY
    req = urllib.request.Request(BASE + path, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)


def main() -> None:
    code, text, _ = call("GET", "/health")
    h = json.loads(text)
    print("health", code, "version", h.get("version"))
    print(" marketplace_resolve", h.get("marketplace_resolve"))
    print(" credentials", h.get("credentials_configured"))

    code, text, _ = call("GET", "/schema")
    s = json.loads(text)
    print("schema", code, "keys", list(s.keys()))

    code, text, _ = call("GET", "/sample")
    sample = json.loads(text)
    print(
        "sample",
        code,
        "request_schema" in sample,
        "how_to_call" in sample,
    )

    code, text, hdrs = call("GET", "/vet_agent")
    print(
        "GET vet_agent",
        code,
        "PR",
        bool(hdrs.get("payment-required") or hdrs.get("Payment-Required")),
    )

    code, text, _ = call("POST", "/vet_agent", b"{}", paid=True)
    print("empty paid", code, text[:220])

    code, text, _ = call(
        "POST",
        "/vet_agent",
        json.dumps({"agent_id": "5127"}).encode(),
        paid=True,
    )
    print("agent_id paid", code, text[:350])

    full = {
        "agent": {
            "agentId": "5127",
            "ownerAddress": "0x2e8e85c1089c53ba04dabb47439c2fd1235652d8",
            "createdAt": 1783835043299,
            "name": "EdgeProof",
            "onlineStatus": 1,
            "soldCount": 1,
        },
        "reviews": {"list": [], "distribution": {}, "total": 0},
    }
    code, text, _ = call(
        "POST", "/vet_agent", json.dumps(full).encode(), paid=True
    )
    print("full agent paid", code, text[:350])


if __name__ == "__main__":
    main()
