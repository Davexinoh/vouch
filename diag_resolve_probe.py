"""Probe AK auth + agent-list with local .env keys (no secret printing)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = "https://web3.okx.com"


def load_env() -> None:
    env_path = Path(__file__).parent / ".env"
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def ts() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def call(method: str, path: str, body: str = "", token: str = "") -> tuple[int | None, str]:
    secret = os.environ["OKX_SECRET_KEY"]
    t = ts()
    msg = f"{t}{method}{path}{body}"
    sign = base64.b64encode(
        hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()
    headers = {
        "Content-Type": "application/json",
        "OK-ACCESS-KEY": os.environ["OKX_API_KEY"],
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-PASSPHRASE": os.environ["OKX_PASSPHRASE"],
        "OK-ACCESS-TIMESTAMP": t,
    }
    if token:
        bare = token.removeprefix("Bearer ").removeprefix("bearer ")
        headers["Authorization"] = f"Bearer {bare}"
        headers["OK-ACCESS-TOKEN"] = bare
    req = Request(
        BASE + path,
        data=body.encode() if body else None,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")[:1200]
    except HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:1200]
    except URLError as e:
        return None, f"URLError: {e}"


def main() -> None:
    load_env()
    print("key_len", len(os.environ.get("OKX_API_KEY", "")))
    token = ""
    for method, path, body in [
        ("POST", "/priapi/v5/wallet/agentic/auth/ak/init", "{}"),
        ("POST", "/web3/ak/agentic/login", ""),
        ("POST", "/web3/ak/agentic/login?locale=en_US", ""),
        (
            "GET",
            "/priapi/v5/wallet/agentic/agent/agent-list?chainIndex=196&agentIdList=5127",
            "",
        ),
        (
            "POST",
            "/priapi/v5/wallet/agentic/search/agent-search",
            json.dumps({"query": "5127", "page": 1, "pageSize": 5}, separators=(",", ":")),
        ),
    ]:
        code, text = call(method, path, body, token=token)
        print("===", method, path, "->", code)
        print(text)
        print()
        # harvest token if present
        try:
            data = json.loads(text)
            blob = data.get("data")
            if isinstance(blob, list) and blob:
                blob = blob[0]
            if isinstance(blob, dict):
                t = blob.get("accessToken") or blob.get("access_token")
                if t:
                    token = t
                    print("[got accessToken len]", len(t))
        except Exception:
            pass

    if token:
        path = "/priapi/v5/wallet/agentic/agent/agent-list?chainIndex=196&agentIdList=5127"
        code, text = call("GET", path, "", token=token)
        print("=== GET agent-list WITH token ->", code)
        print(text[:1200])


if __name__ == "__main__":
    main()
