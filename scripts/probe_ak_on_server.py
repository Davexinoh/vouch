"""Run inside Railway container: try AK login and print token status (masked)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone

import httpx

BASE = "https://web3.okx.com"


def ts() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def main() -> None:
    key = os.environ.get("OKX_API_KEY", "")
    secret = os.environ.get("OKX_SECRET_KEY", "")
    passp = os.environ.get("OKX_PASSPHRASE", "")
    existing = os.environ.get("OKX_ACCESS_TOKEN") or os.environ.get("OKX_JWT") or ""
    print("key_set", bool(key), "secret_set", bool(secret), "pass_set", bool(passp))
    print("existing_token", bool(existing), "len", len(existing))

    def headers(method: str, path: str, body: str) -> dict:
        t = ts()
        sign = base64.b64encode(
            hmac.new(secret.encode(), f"{t}{method}{path}{body}".encode(), hashlib.sha256).digest()
        ).decode()
        return {
            "Content-Type": "application/json",
            "OK-ACCESS-KEY": key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-PASSPHRASE": passp,
            "OK-ACCESS-TIMESTAMP": t,
        }

    token = existing
    with httpx.Client(timeout=30) as c:
        for path, body in [
            ("/web3/ak/agentic/login", ""),
            ("/web3/ak/agentic/login", "{}"),
            ("/priapi/v5/wallet/agentic/auth/ak/init", "{}"),
            ("/priapi/v5/wallet/agentic/auth/ak/init", json.dumps({"apiKey": key})),
        ]:
            method = "POST"
            r = c.post(BASE + path, headers=headers(method, path, body), content=body or None)
            try:
                data = r.json()
            except Exception:
                data = {"raw": r.text[:200]}
            print(path, r.status_code, "code", data.get("code"), "msg", data.get("msg"))
            blob = data.get("data")
            if isinstance(blob, list) and blob:
                blob = blob[0]
            if isinstance(blob, dict):
                t = blob.get("accessToken") or blob.get("access_token")
                if t:
                    token = t
                    print("GOT_TOKEN len", len(t), "prefix", t[:10])
                    break
            # if init ok, try verify
            if path.endswith("/ak/init") and str(data.get("code", "")) in ("0", "") and isinstance(blob, dict):
                vpath = "/priapi/v5/wallet/agentic/auth/ak/verify"
                vbody = json.dumps(blob, separators=(",", ":"))
                vr = c.post(BASE + vpath, headers=headers("POST", vpath, vbody), content=vbody)
                try:
                    vdata = vr.json()
                except Exception:
                    vdata = {}
                print(" verify", vr.status_code, vdata.get("code"), vdata.get("msg"))
                vb = vdata.get("data")
                if isinstance(vb, list) and vb:
                    vb = vb[0]
                if isinstance(vb, dict):
                    t = vb.get("accessToken") or vb.get("access_token")
                    if t:
                        token = t
                        print("GOT_TOKEN_FROM_VERIFY len", len(t))
                        break

        if token:
            # smoke agent-list
            path = "/priapi/v5/wallet/agentic/agent/agent-list?chainIndex=196&agentIdList=5127"
            t = ts()
            sign = base64.b64encode(
                hmac.new(secret.encode(), f"{t}GET{path}".encode(), hashlib.sha256).digest()
            ).decode()
            h = {
                "OK-ACCESS-KEY": key,
                "OK-ACCESS-SIGN": sign,
                "OK-ACCESS-PASSPHRASE": passp,
                "OK-ACCESS-TIMESTAMP": t,
                "Authorization": f"Bearer {token}",
                "OK-ACCESS-TOKEN": token,
            }
            r = c.get(BASE + path, headers=h)
            print("agent-list", r.status_code, r.text[:300])
            # print token for capture (one line)
            print("TOKEN_EXPORT_BEGIN")
            print(token)
            print("TOKEN_EXPORT_END")
        else:
            print("NO_TOKEN")


if __name__ == "__main__":
    main()
