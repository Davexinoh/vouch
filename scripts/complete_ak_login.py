"""Complete AK login: init with apiKey body -> sign nonce -> verify -> agent-list."""
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
CF_HOST = "d1a9ug9i3w9ke0.cloudfront.net"
API_HOST = "web3.okx.com"
PROJECT = "agentic-wallet-project01"


def load_env() -> dict[str, str]:
    out = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def ts() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def main() -> None:
    env = load_env()
    key, secret, passp = env["OKX_API_KEY"], env["OKX_SECRET_KEY"], env["OKX_PASSPHRASE"]

    def call(method: str, path: str, body: str = "", token: str = "") -> dict:
        t = ts()
        sign = base64.b64encode(
            hmac.new(
                secret.encode(), f"{t}{method}{path}{body}".encode(), hashlib.sha256
            ).digest()
        ).decode()
        headers = {
            "Host": API_HOST,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "OK-ACCESS-KEY": key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-PASSPHRASE": passp,
            "OK-ACCESS-TIMESTAMP": t,
            "OK-ACCESS-PROJECT": PROJECT,
            "User-Agent": "okhttp/4.12.0",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
            headers["OK-ACCESS-TOKEN"] = token
        url = f"https://{CF_HOST}{path}"
        req = urllib.request.Request(
            url, data=body.encode() if body else None, headers=headers, method=method
        )
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            raw = e.read().decode() if hasattr(e, "read") else str(e)
            try:
                return json.loads(raw)
            except Exception:
                return {"error": raw, "http": getattr(e, "code", None)}

    init_path = "/priapi/v5/wallet/agentic/auth/ak/init"
    init_body = json.dumps(
        {"apiKey": key, "projectId": PROJECT}, separators=(",", ":")
    )
    init = call("POST", init_path, init_body)
    print("init", json.dumps(init)[:400])
    data = init.get("data")
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict) or "nonce" not in data:
        print("FAIL no nonce")
        return

    nonce = data["nonce"]
    iss = data.get("iss", API_HOST)

    # try several signature constructions
    candidates = []
    for msg in (
        nonce,
        f"{nonce}{iss}",
        f"{iss}{nonce}",
        f"{key}{nonce}",
        f"{nonce}{key}",
        json.dumps({"nonce": nonce, "iss": iss}, separators=(",", ":")),
    ):
        candidates.append(
            (
                msg[:40],
                base64.b64encode(
                    hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()
                ).decode(),
            )
        )
        # also hex digest
        candidates.append(
            (
                "hex:" + msg[:30],
                hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest(),
            )
        )

    verify_path = "/priapi/v5/wallet/agentic/auth/ak/verify"
    token = ""
    for label, sig in candidates:
        for payload in (
            {"nonce": nonce, "iss": iss, "sign": sig},
            {"nonce": nonce, "iss": iss, "signature": sig},
            {"nonce": nonce, "iss": iss, "sign": sig, "apiKey": key},
            {"nonce": nonce, "iss": iss, "sign": sig, "projectId": PROJECT},
        ):
            body = json.dumps(payload, separators=(",", ":"))
            res = call("POST", verify_path, body)
            code = str(res.get("code", ""))
            if code in ("0", ""):
                blob = res.get("data")
                if isinstance(blob, list) and blob:
                    blob = blob[0]
                if isinstance(blob, dict):
                    token = blob.get("accessToken") or blob.get("access_token") or ""
                if token:
                    print("SUCCESS with", label, "payload keys", list(payload.keys()))
                    print("token len", len(token), "prefix", token[:12])
                    break
            else:
                # only print distinct errors once
                pass
        if token:
            break
        print("try", label, "->", res.get("code"), res.get("msg"))

    if not token:
        print("NO TOKEN after verify attempts; last", res)
        return

    # agent-list smoke
    path = "/priapi/v5/wallet/agentic/agent/agent-list?chainIndex=196&agentIdList=5127"
    res = call("GET", path, "", token=token)
    print("agent-list", res.get("code"), str(res)[:400])
    out = ROOT / ".okx_access_token"
    out.write_text(token, encoding="utf-8")
    print("wrote", out)


if __name__ == "__main__":
    main()
