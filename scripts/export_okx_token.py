"""Attempt to obtain a marketplace access token for Vouch.

Strategies:
1. If OKX_ACCESS_TOKEN already in env/.env — print ready (masked).
2. Try agentic AK login paths with HMAC from vouch/.env.
3. Fall back: instruct user if only email-wallet session is available.

Does not print full secrets unless --show is passed.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get("OKX_BASE", "https://web3.okx.com").rstrip("/")


def load_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def ts() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def call(
    method: str,
    path: str,
    body: str,
    key: str,
    secret: str,
    passphrase: str,
    token: str = "",
) -> tuple[int | None, dict | str]:
    t = ts()
    msg = f"{t}{method}{path}{body}"
    sign = base64.b64encode(
        hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "OK-ACCESS-KEY": key,
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-PASSPHRASE": passphrase,
        "OK-ACCESS-TIMESTAMP": t,
        "User-Agent": "vouch-token-export/1.0",
    }
    if token:
        bare = token.removeprefix("Bearer ").strip()
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
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw
    except URLError as e:
        return None, f"URLError: {e}"


def extract_token(data) -> str:
    if not isinstance(data, dict):
        return ""
    blob = data.get("data", data)
    if isinstance(blob, list) and blob:
        blob = blob[0]
    if not isinstance(blob, dict):
        return ""
    return (
        blob.get("accessToken")
        or blob.get("access_token")
        or blob.get("token")
        or ""
    )


def mask(tok: str) -> str:
    if len(tok) <= 12:
        return "***"
    return f"{tok[:8]}…{tok[-6:]} (len={len(tok)})"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true", help="print full token")
    ap.add_argument(
        "--out",
        type=Path,
        default=ROOT / ".okx_access_token",
        help="write token to this file (gitignored)",
    )
    args = ap.parse_args()

    env = {**os.environ, **load_dotenv(ROOT / ".env")}
    key = env.get("OKX_API_KEY", "")
    secret = env.get("OKX_SECRET_KEY", "")
    passphrase = env.get("OKX_PASSPHRASE", "")
    existing = env.get("OKX_ACCESS_TOKEN") or env.get("OKX_JWT") or ""

    if existing:
        print("FOUND existing OKX_ACCESS_TOKEN in env/.env:", mask(existing))
        if args.show:
            print(existing)
        args.out.write_text(existing, encoding="utf-8")
        print("wrote", args.out)
        return 0

    if not (key and secret and passphrase):
        print("MISSING OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE in vouch/.env")
        return 1

    print("Trying AK agentic login paths against", BASE)
    attempts = [
        ("POST", "/web3/ak/agentic/login", ""),
        ("POST", "/web3/ak/agentic/login", "{}"),
        ("POST", "/web3/ak/agentic/login?locale=en_US", "{}"),
        ("POST", "/priapi/v5/wallet/agentic/auth/ak/init", "{}"),
        (
            "POST",
            "/priapi/v5/wallet/agentic/auth/ak/init",
            json.dumps({"projectId": "agentic-wallet-project01"}, separators=(",", ":")),
        ),
        (
            "POST",
            "/priapi/v5/wallet/agentic/auth/ak/init",
            json.dumps({"apiKey": key}, separators=(",", ":")),
        ),
    ]

    token = ""
    init_blob = None
    for method, path, body in attempts:
        code, data = call(method, path, body, key, secret, passphrase)
        print(f"  {method} {path} -> {code}")
        if isinstance(data, dict):
            print(f"    code={data.get('code')} msg={data.get('msg')}")
            tok = extract_token(data)
            if tok:
                token = tok
                print("    got token", mask(tok))
                break
            if path.endswith("/ak/init") and str(data.get("code", "0")) in ("0", ""):
                init_blob = data.get("data")
                if isinstance(init_blob, list) and init_blob:
                    init_blob = init_blob[0]
        else:
            print("   ", str(data)[:160])

    if not token and isinstance(init_blob, dict):
        verify_path = "/priapi/v5/wallet/agentic/auth/ak/verify"
        payload = {k: init_blob[k] for k in init_blob if k not in ("accessToken",)}
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        code, data = call("POST", verify_path, body, key, secret, passphrase)
        print(f"  POST {verify_path} -> {code}")
        if isinstance(data, dict):
            print(f"    code={data.get('code')} msg={data.get('msg')}")
            token = extract_token(data)
            if token:
                print("    got token", mask(token))

    if not token:
        print()
        print("FAILED to obtain access token via API key.")
        print("Your payment keys work for x402 but AK agentic login is rejected.")
        print()
        print("Next options:")
        print("  A) In OKX Dev Portal, create/use a key with agentic/marketplace scope")
        print("  B) Capture a browser/app JWT (Authorization Bearer) while logged into")
        print("     web3.okx.com agent marketplace and set it as OKX_ACCESS_TOKEN")
        print("  C) Paste a token: set OKX_ACCESS_TOKEN in vouch/.env then re-run")
        return 2

    args.out.write_text(token, encoding="utf-8")
    print("SUCCESS wrote", args.out, mask(token))
    if args.show:
        print(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
