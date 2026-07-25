"""Narrow matrix for ak/verify after successful init with apiKey body."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import ssl
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CF = "d1a9ug9i3w9ke0.cloudfront.net"
HOST = "web3.okx.com"
PROJECT = "agentic-wallet-project01"


def env() -> dict[str, str]:
    out = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def ts() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def call(key, secret, passp, method, path, body="", extra_headers=None):
    t = ts()
    sign = base64.b64encode(
        hmac.new(secret.encode(), f"{t}{method}{path}{body}".encode(), hashlib.sha256).digest()
    ).decode()
    headers = {
        "Host": HOST,
        "Content-Type": "application/json",
        "OK-ACCESS-KEY": key,
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-PASSPHRASE": passp,
        "OK-ACCESS-TIMESTAMP": t,
        "OK-ACCESS-PROJECT": PROJECT,
    }
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(
        f"https://{CF}{path}",
        data=body.encode() if body else None,
        headers=headers,
        method=method,
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=25, context=ctx) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        raw = e.read().decode() if hasattr(e, "read") else str(e)
        try:
            return json.loads(raw)
        except Exception:
            return {"err": raw}


def main():
    e = env()
    key, secret, passp = e["OKX_API_KEY"], e["OKX_SECRET_KEY"], e["OKX_PASSPHRASE"]
    init_body = json.dumps({"apiKey": key, "projectId": PROJECT}, separators=(",", ":"))
    init = call(key, secret, passp, "POST", "/priapi/v5/wallet/agentic/auth/ak/init", init_body)
    print("init", init)
    data = init["data"][0]
    nonce, iss = data["nonce"], data["iss"]
    # OKX standard: often sign is HMAC of nonce with secret, base64
    sig = base64.b64encode(
        hmac.new(secret.encode(), nonce.encode(), hashlib.sha256).digest()
    ).decode()
    # Also try passphrase as hmac key
    sig2 = base64.b64encode(
        hmac.new(passp.encode(), nonce.encode(), hashlib.sha256).digest()
    ).decode()
    # try sign as base64(hmac(secret, timestamp+nonce))
    tnow = ts()
    sig3 = base64.b64encode(
        hmac.new(secret.encode(), f"{tnow}{nonce}".encode(), hashlib.sha256).digest()
    ).decode()

    bodies = [
        {"apiKey": key, "nonce": nonce, "iss": iss, "sign": sig},
        {"apiKey": key, "nonce": nonce, "iss": iss, "signature": sig},
        {"apiKey": key, "nonce": nonce, "sign": sig},
        {"apiKey": key, "nonce": nonce, "iss": iss, "sign": sig2},
        {"apiKey": key, "nonce": nonce, "iss": iss, "sign": sig3},
        # put sign in header style
        {"apiKey": key, "nonce": nonce, "iss": iss, "sign": sig, "projectId": PROJECT},
    ]
    for i, b in enumerate(bodies):
        body = json.dumps(b, separators=(",", ":"))
        res = call(key, secret, passp, "POST", "/priapi/v5/wallet/agentic/auth/ak/verify", body)
        print(i, res.get("code"), res.get("msg"), str(res.get("data"))[:80])
        tok = None
        d = res.get("data")
        if isinstance(d, list) and d:
            d = d[0]
        if isinstance(d, dict):
            tok = d.get("accessToken") or d.get("access_token")
        if tok:
            print("TOKEN", len(tok))
            Path(ROOT / ".okx_access_token").write_text(tok, encoding="utf-8")
            # test list
            path = "/priapi/v5/wallet/agentic/agent/agent-list?chainIndex=196&agentIdList=5127"
            # call with token
            t = ts()
            hsign = base64.b64encode(
                hmac.new(secret.encode(), f"{t}GET{path}".encode(), hashlib.sha256).digest()
            ).decode()
            # reuse call with Authorization via modifying - quick path
            print("saved token")
            return


if __name__ == "__main__":
    main()
