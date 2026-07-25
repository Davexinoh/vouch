"""Local marketplace resolve proxy backed by onchainos CLI.

Vouch on Railway cannot complete raw HTTP AK login (needs TEE wallet).
This proxy runs where onchainos is logged in and exposes:

  GET /resolve/{agent_id}  -> { "agent": {...}, "reviews": {...} }
  GET /health

Point Railway Vouch at it with:
  RESOLVE_PROXY_URL=https://<tunnel-host>
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# allow importing app.*
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# load .env
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

os.environ.setdefault("ONCHAINOS_BIN", r"C:\Users\Hp\onchainos\onchainos.exe")

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from app.cli_resolve import resolve_agent_cli, ensure_ak_login, available

app = FastAPI(title="Vouch resolve proxy", version="1.0.0")
SECRET = os.environ.get("RESOLVE_PROXY_SECRET", "vouch-resolve-local")


@app.on_event("startup")
def _boot():
    if not available() and not os.environ.get("ONCHAINOS_BIN"):
        raise RuntimeError("onchainos not found")
    ensure_ak_login()
    print("[resolve-proxy] AK login ok, ready", flush=True)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "onchainos": bool(os.environ.get("ONCHAINOS_BIN") or available()),
        "logged_in": True,
    }


@app.get("/resolve/{agent_id}")
def resolve(agent_id: str, key: str = ""):
    if SECRET and key != SECRET:
        # also accept header-style via query for simplicity
        raise HTTPException(status_code=401, detail="bad key")
    try:
        agent, reviews = resolve_agent_cli(agent_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {"agent": agent, "reviews": reviews}


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8787"))
    uvicorn.run(app, host="0.0.0.0", port=port)
