"""Diagnose OKX facilitator verify against a real onchainos payment signature."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
from pathlib import Path

import httpx

from app import x402


def load_env() -> None:
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def select_accepted_fixed(requirements: dict, payment_payload: dict) -> dict:
    """Fixed selector: read scheme from accepted.scheme (CLI payment header shape)."""
    accepts = requirements["accepts"]
    accepted = payment_payload.get("accepted") if isinstance(payment_payload.get("accepted"), dict) else {}
    scheme = (
        payment_payload.get("scheme")
        or accepted.get("scheme")
        or (payment_payload.get("payload") or {}).get("scheme")
    )
    if scheme:
        match = next((a for a in accepts if a.get("scheme") == scheme), None)
        if match:
            return match
    # sessionCert may live under accepted.extra for aggr_deferred
    extra = accepted.get("extra") if isinstance(accepted.get("extra"), dict) else {}
    inner = payment_payload.get("payload") if isinstance(payment_payload.get("payload"), dict) else {}
    wanted = "aggr_deferred" if ("sessionCert" in extra or "sessionCert" in inner) else "exact"
    return next((a for a in accepts if a.get("scheme") == wanted), accepts[0])


async def main() -> None:
    load_env()
    print("creds_present", x402.credentials_present())
    print("VET_PRICE", os.environ.get("VET_PRICE"))
    print("PAY_TO", os.environ.get("VOUCH_PAYOUT_WALLET"))
    print("USDT", os.environ.get("USDT_XLAYER_ADDRESS"))
    print("SELF_URL", os.environ.get("SELF_URL"))

    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post("https://vouch-4ib4.onrender.com/vet_agent", json={})
        pr = r.headers.get("payment-required")
        live_reqs = json.loads(base64.b64decode(pr))
        print("live_amount", live_reqs["accepts"][0]["amount"])
        print("live_schemes", [a["scheme"] for a in live_reqs["accepts"]])

        # Sign with onchainos (exact = index 0)
        pay_out = subprocess.check_output(
            ["onchainos", "payment", "pay", "--payload", pr, "--selected-index", "0"],
            text=True,
        )
        pay = json.loads(pay_out)
        if not pay.get("ok"):
            print("PAY_FAILED", pay_out)
            return
        auth = pay["data"]["authorization_header"]
        payload = x402.decode_x_payment(auth)
        print("signed_scheme", (payload.get("accepted") or {}).get("scheme"))
        print("payload_keys", list(payload.keys()))
        print("accepted_keys", list((payload.get("accepted") or {}).keys()))

        # Old selector (current production bug path)
        old_accepted = x402  # placeholder
        # Inline old logic
        def select_old(requirements, payment_payload):
            accepts = requirements["accepts"]
            scheme = payment_payload.get("scheme")
            if not scheme and isinstance(payment_payload.get("payload"), dict):
                scheme = payment_payload["payload"].get("scheme")
            if scheme:
                match = next((a for a in accepts if a.get("scheme") == scheme), None)
                if match:
                    return match
            inner = payment_payload.get("payload") if isinstance(payment_payload.get("payload"), dict) else payment_payload
            wanted = "aggr_deferred" if "sessionCert" in inner else "exact"
            return next((a for a in accepts if a.get("scheme") == wanted), accepts[0])

        old = select_old(live_reqs, payload)
        new = select_accepted_fixed(live_reqs, payload)
        print("old_selected", old.get("scheme"))
        print("new_selected", new.get("scheme"))

        # Verify against LIVE requirements entry
        verify = await x402.verify_payment(client, payload, new)
        print("verify_http", verify.get("http_status"))
        print("verify_body", json.dumps(verify.get("body"), ensure_ascii=False)[:2000])
        ok, reason = x402.outcome(verify)
        print("verify_outcome", ok, reason)

        # Also try using buyer's accepted object as paymentRequirements
        buyer_accepted = payload.get("accepted") or new
        verify2 = await x402.verify_payment(client, payload, buyer_accepted)
        print("verify2_http", verify2.get("http_status"))
        print("verify2_body", json.dumps(verify2.get("body"), ensure_ascii=False)[:2000])
        ok2, reason2 = x402.outcome(verify2)
        print("verify2_outcome", ok2, reason2)

        # Try settle if verify worked
        if ok or ok2:
            target = new if ok else buyer_accepted
            settle = await x402.settle_payment(client, payload, target)
            print("settle_http", settle.get("http_status"))
            print("settle_body", json.dumps(settle.get("body"), ensure_ascii=False)[:2000])
            print("settle_outcome", x402.outcome(settle))


if __name__ == "__main__":
    asyncio.run(main())
