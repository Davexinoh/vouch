"""Try marketplace agent-list using local onchainos sessionCert + DoH-resolved IP."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import ssl
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSION = Path.home() / ".onchainos" / "session.json"
ENV = ROOT / ".env"


def load_env() -> dict[str, str]:
    out = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def doh_a(host: str) -> str:
    url = f"https://cloudflare-dns.com/dns-query?name={host}&type=A"
    req = urllib.request.Request(url, headers={"Accept": "application/dns-json"})
    data = json.loads(urllib.request.urlopen(req, timeout=15).read())
    for ans in data.get("Answer") or []:
        if ans.get("type") == 1:
            return ans["data"]
    raise RuntimeError(f"no A record for {host}: {data}")


def ts() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def main() -> None:
    env = load_env()
    sess = json.loads(SESSION.read_text(encoding="utf-8"))
    ip = doh_a("web3.okx.com")
    print("resolved web3.okx.com ->", ip)
    print("session keys", list(sess.keys()))

    key, secret, passp = (
        env["OKX_API_KEY"],
        env["OKX_SECRET_KEY"],
        env["OKX_PASSPHRASE"],
    )
    path = "/priapi/v5/wallet/agentic/agent/agent-list?chainIndex=196&agentIdList=5127"
    body = ""
    t = ts()
    sign = base64.b64encode(
        hmac.new(
            secret.encode(), f"{t}GET{path}{body}".encode(), hashlib.sha256
        ).digest()
    ).decode()

    header_variants = [
        {
            "name": "hmac_only",
            "h": {
                "Host": "web3.okx.com",
                "Content-Type": "application/json",
                "OK-ACCESS-KEY": key,
                "OK-ACCESS-SIGN": sign,
                "OK-ACCESS-PASSPHRASE": passp,
                "OK-ACCESS-TIMESTAMP": t,
            },
        },
        {
            "name": "sessionCert_header",
            "h": {
                "Host": "web3.okx.com",
                "Content-Type": "application/json",
                "OK-ACCESS-KEY": key,
                "OK-ACCESS-SIGN": sign,
                "OK-ACCESS-PASSPHRASE": passp,
                "OK-ACCESS-TIMESTAMP": t,
                "sessionCert": sess.get("sessionCert", ""),
                "X-Session-Cert": sess.get("sessionCert", ""),
                "teeId": sess.get("teeId", ""),
                "X-Tee-Id": sess.get("teeId", ""),
            },
        },
        {
            "name": "sessionCert_as_bearer",
            "h": {
                "Host": "web3.okx.com",
                "Content-Type": "application/json",
                "OK-ACCESS-KEY": key,
                "OK-ACCESS-SIGN": sign,
                "OK-ACCESS-PASSPHRASE": passp,
                "OK-ACCESS-TIMESTAMP": t,
                "Authorization": f"Bearer {sess.get('sessionCert','')}",
                "OK-ACCESS-TOKEN": sess.get("sessionCert", ""),
            },
        },
    ]

    ctx = ssl.create_default_context()
    # SNI still web3.okx.com
    for variant in header_variants:
        url = f"https://{ip}{path}"
        req = urllib.request.Request(url, headers=variant["h"], method="GET")
        try:
            with urllib.request.urlopen(req, timeout=25, context=ctx) as resp:
                raw = resp.read().decode("utf-8", "replace")
                print(variant["name"], "->", resp.status, raw[:300])
        except Exception as e:
            body = ""
            if hasattr(e, "read"):
                try:
                    body = e.read().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
            print(variant["name"], "->", getattr(e, "code", type(e).__name__), body or e)


if __name__ == "__main__":
    main()
