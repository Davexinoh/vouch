"""Resolve agent_id via onchainos CLI (AK login already proven to work).

Used when:
- ONCHAINOS_BIN points at the binary, or `onchainos` is on PATH
- Env has OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE (for `wallet login`)

This is the reliable path for marketplace identity; raw HTTP AK login does not
complete without the TEE wallet that onchainos manages after AK login.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from typing import Any

from .resolver import ResolveError

_logged_in = False
_login_lock = threading.Lock()


def _bin() -> str | None:
    explicit = os.environ.get("ONCHAINOS_BIN", "").strip()
    if explicit and os.path.isfile(explicit):
        return explicit
    return shutil.which("onchainos")


def available() -> bool:
    return _bin() is not None


def _run(args: list[str], env: dict[str, str] | None = None, timeout: float = 60) -> dict:
    binary = _bin()
    if not binary:
        raise ResolveError("onchainos binary not found (set ONCHAINOS_BIN)")
    full_env = {**os.environ, **(env or {})}
    # ensure API keys visible under common names
    for src, dsts in (
        ("OKX_API_KEY", ("OKX_API_KEY", "API_KEY")),
        ("OKX_SECRET_KEY", ("OKX_SECRET_KEY", "SECRET_KEY")),
        ("OKX_PASSPHRASE", ("OKX_PASSPHRASE", "PASSPHRASE")),
    ):
        if full_env.get(src):
            for d in dsts:
                full_env.setdefault(d, full_env[src])
    r = subprocess.run(
        [binary, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=full_env,
        timeout=timeout,
    )
    out = (r.stdout or "").strip()
    if not out:
        raise ResolveError(
            f"onchainos {' '.join(args)} empty stdout (code={r.returncode}): "
            f"{(r.stderr or '')[:200]}"
        )
    try:
        data = json.loads(out)
    except json.JSONDecodeError as e:
        raise ResolveError(
            f"onchainos {' '.join(args)} non-JSON: {out[:200]}"
        ) from e
    if not data.get("ok", True) and data.get("error"):
        raise ResolveError(f"onchainos {' '.join(args)}: {data.get('error')}")
    return data


def wallet_logged_in() -> bool:
    """Local session probe. NOTE: in AK mode `wallet status` does not reliably
    reflect backend session validity — use marketplace_ready() for /health."""
    try:
        st = _run(["wallet", "status"], timeout=30)
        return bool(st.get("data", {}).get("loggedIn"))
    except Exception:
        return False


def ensure_ak_login(force: bool = False) -> None:
    """AK-mode login (wallet login with no email). The TEE backend keeps ONE
    active session per account — a login elsewhere (e.g. the owner's laptop)
    expires ours, so callers must be able to force a fresh login."""
    global _logged_in
    with _login_lock:
        if _logged_in and not force:
            return
        try:
            _run(["wallet", "login"], timeout=90)
            _logged_in = True
        except Exception as e:
            if not force and wallet_logged_in():
                _logged_in = True
                return
            raise ResolveError(f"onchainos AK login failed: {e}") from e


def _run_authed(args: list[str], timeout: float = 60) -> dict:
    """Run an authed CLI command; on 'session expired' re-login once and
    retry (self-heals when another device stole the TEE session)."""
    global _logged_in
    ensure_ak_login()
    try:
        return _run(args, timeout=timeout)
    except ResolveError as e:
        if "session expired" not in str(e).lower():
            raise
        _logged_in = False
        ensure_ak_login(force=True)
        return _run(args, timeout=timeout)


def marketplace_ready() -> dict:
    """Real backend probe for /health: run a cheap authed read."""
    try:
        _run_authed(["agent", "get-my-agents"], timeout=45)
        return {"marketplace_session": True, "via": "onchainos_cli"}
    except Exception as e:
        return {
            "marketplace_session": False,
            "via": "onchainos_cli",
            "error": str(e)[:240],
        }


def resolve_agent_cli(agent_id: str) -> tuple[dict, dict]:
    agent_id = str(agent_id).strip().lstrip("#")
    if not agent_id.isdigit():
        raise ResolveError(f"agent_id must be numeric, got {agent_id!r}")

    data = _run_authed(["agent", "get-agents", "--agent-ids", agent_id], timeout=60)
    rows = data.get("data") or []
    if not rows:
        raise ResolveError(f"onchainos get-agents empty for {agent_id}")
    agent = rows[0] if isinstance(rows[0], dict) else None
    if not agent or str(agent.get("agentId")) != agent_id:
        # sometimes nested
        if isinstance(rows[0], dict) and "agentList" in rows[0]:
            alist = rows[0].get("agentList") or []
            agent = next(
                (a for a in alist if str(a.get("agentId")) == agent_id),
                alist[0] if alist else None,
            )
    if not isinstance(agent, dict):
        raise ResolveError(f"onchainos get-agents unexpected shape for {agent_id}")

    # normalize fields
    if not agent.get("ownerAddress"):
        agent["ownerAddress"] = agent.get("agentWalletAddress") or ""
    if not agent.get("agentId"):
        agent["agentId"] = agent_id

    reviews: dict[str, Any] = {"list": [], "distribution": {}, "total": 0}
    try:
        fb = _run_authed(
            ["agent", "feedback-list", "--agent-id", agent_id, "--page-size", "50"],
            timeout=45,
        )
        payload = fb.get("data") or fb
        if isinstance(payload, dict):
            if "list" in payload or "items" in payload:
                reviews = {
                    "list": payload.get("list") or payload.get("items") or [],
                    "distribution": payload.get("distribution") or {},
                    "total": payload.get("total") or 0,
                }
            elif "items" in payload:
                reviews = {"list": payload["items"], "distribution": {}, "total": len(payload["items"])}
    except Exception:
        pass

    try:
        svc = _run_authed(["agent", "service-list", "--agent-id", agent_id], timeout=45)
        # shape: data[0].list
        d0 = (svc.get("data") or [{}])[0] if isinstance(svc.get("data"), list) else svc.get("data")
        if isinstance(d0, dict):
            slist = d0.get("list") or []
            agent["services"] = [
                {"endpoint": s.get("endpoint")}
                for s in slist
                if isinstance(s, dict) and s.get("endpoint")
            ]
    except Exception:
        agent.setdefault("services", [])

    if not agent.get("ownerAddress") or not agent.get("createdAt"):
        raise ResolveError(
            f"onchainos agent {agent_id} missing ownerAddress/createdAt: "
            f"{list(agent.keys())[:15]}"
        )
    return agent, reviews
