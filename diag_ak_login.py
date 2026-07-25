"""Probe agentic AK login and agent-list with Vouch API keys."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx

BASE = "https://web3.okx.com"


def load_env() -> None:
    for line in (Path(__file__).parent / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def ts() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def headers(method: str, path: str, body: str, extra: dict | None = None) -> dict:
    t = ts()
    secret = os.environ["OKX_SECRET_KEY"]
    sign = base64.b64encode(
        hmac.new(secret.encode(), f"{t}{method}{path}{body}".encode(), hashlib.sha256).digest()
    ).decode()
    h = {
        "Content-Type": "application/json",
        "OK-ACCESS-KEY": os.environ["OKX_API_KEY"],
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-PASSPHRASE": os.environ["OKX_PASSPHRASE"],
        "OK-ACCESS-TIMESTAMP": t,
    }
    if extra:
        h.update(extra)
    return h


def main() -> None:
    load_env()
    with httpx.Client(timeout=40) as c:
        for path, body_obj in [
            ("/priapi/v5/wallet/agentic/auth/ak/init", {}),
            ("/priapi/v5/wallet/agentic/auth/ak/init", {"locale": "en_US"}),
            ("/priapi/v5/wallet/agentic/auth/init", {}),
        ]:
            body = json.dumps(body_obj, separators=(",", ":"))
            r = c.post(BASE + path, headers=headers("POST", path, body), content=body)
            print("===", path, r.status_code, "===")
            print(r.text[:1200])
            print()

        # try agent-search with API key only
        path = "/priapi/v5/wallet/agentic/search/agent-search"
        body = json.dumps({"query": "6086", "page": 1, "pageSize": 5}, separators=(",", ":"))
        r = c.post(BASE + path, headers=headers("POST", path, body), content=body)
        print("=== agent-search", r.status_code, "===")
        print(r.text[:800])


if __name__ == "__main__":
    main()
