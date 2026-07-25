"""Hit OKX via CloudFront alias used by onchainos DoH proxy; try AK login."""
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
# onchainos DoH proxy target
CF_HOST = "d1a9ug9i3w9ke0.cloudfront.net"
# SNI / Host as used by OKX web3 API
API_HOST = "web3.okx.com"


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

    def call(method: str, path: str, body: str = "") -> tuple[int | None, str]:
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
            "OK-ACCESS-PROJECT": "agentic-wallet-project01",
            "User-Agent": "okhttp/4.12.0",
        }
        url = f"https://{CF_HOST}{path}"
        req = urllib.request.Request(
            url, data=body.encode() if body else None, headers=headers, method=method
        )
        ctx = ssl.create_default_context()
        # CloudFront cert is for cloudfront, not web3.okx.com — may need check_hostname False
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")[:800]
        except Exception as e:
            body_txt = ""
            if hasattr(e, "read"):
                try:
                    body_txt = e.read().decode("utf-8", "replace")[:800]
                except Exception:
                    pass
            return getattr(e, "code", None), body_txt or str(e)

    for path, body in [
        ("/web3/ak/agentic/login", ""),
        ("/web3/ak/agentic/login", "{}"),
        ("/priapi/v5/wallet/agentic/auth/ak/init", "{}"),
        (
            "/priapi/v5/wallet/agentic/auth/ak/init",
            json.dumps({"apiKey": key, "projectId": "agentic-wallet-project01"}),
        ),
        (
            "/priapi/v5/wallet/agentic/agent/agent-list?chainIndex=196&agentIdList=5127",
            "",
        ),
    ]:
        method = "GET" if "agent-list" in path else "POST"
        code, text = call(method, path, body if method == "POST" else "")
        print("===", method, path, "->", code)
        print(text[:500])
        print()


if __name__ == "__main__":
    main()
