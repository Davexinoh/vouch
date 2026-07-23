"""
Local read-only OKX.AI marketplace sweep → State of the Agent Economy.
Caches raw data; runs app.vetting.vet_agent; writes sweep_raw.csv + sweep_summary.md.
"""
from __future__ import annotations

import asyncio
import csv
import json
import re
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

# vouch package root
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.vetting import vet_agent  # noqa: E402
from app.scoring import SIGNAL_WEIGHTS  # noqa: E402

CACHE_PATH = ROOT / "data" / "marketplace_sweep_cache.json"
OUT_CSV = ROOT / "data" / "sweep_raw.csv"
OUT_MD = ROOT / "data" / "sweep_summary.md"
XLAYER_RPC = "https://rpc.xlayer.tech"

# Canonical signal set for frequency table = actual SIGNAL_WEIGHTS
CANONICAL_SIGNALS = list(SIGNAL_WEIGHTS.keys())


def run_cli(*args: str, timeout: int = 90) -> dict:
    p = subprocess.run(
        ["onchainos", *args],
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    out = (p.stdout or "") + (p.stderr or "")
    out = out.strip()
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}\s*$", out)
        if not m:
            raise RuntimeError(f"CLI no JSON (exit {p.returncode}): {out[:300]}")
        return json.loads(m.group())


def discover_top_asps(limit: int = 25) -> list[dict]:
    """Paginate agent search across queries; rank by soldCount desc; keep status=1 ASPs."""
    queries = [
        "a", "e", "i", "o", "u", "s", "t", "n",
        "agent", "asp", "api", "ai", "defi", "data", "service",
        "tool", "bot", "mcp", "trade", "token", "web3", "okx",
        "finance", "security", "research", "launch", "market",
    ]
    seen: dict[str, dict] = {}
    for q in queries:
        for page in range(1, 8):
            try:
                data = run_cli(
                    "agent", "search",
                    "--query", q,
                    "--page", str(page),
                    "--page-size", "50",
                    timeout=60,
                )
            except Exception as e:
                print(f"[search] fail {q!r} p{page}: {e}", flush=True)
                break
            if not data.get("ok"):
                break
            lst = (data.get("data") or {}).get("list") or []
            if not lst:
                break
            for a in lst:
                aid = str(a.get("agentId"))
                sc = a.get("soldCount")
                sc_n = sc if isinstance(sc, (int, float)) else -1
                prev = seen.get(aid)
                prev_sc = prev.get("soldCount") if prev else None
                prev_n = prev_sc if isinstance(prev_sc, (int, float)) else -1
                if prev is None or sc_n > prev_n:
                    seen[aid] = a
            print(f"[search] {q!r} p{page} +{len(lst)} unique={len(seen)}", flush=True)
            total = (data.get("data") or {}).get("total")
            if total is not None and page * 50 >= int(total):
                break

    # Listed ASPs: status 1 (active), prefer soldCount present
    candidates = []
    for a in seen.values():
        if a.get("status") not in (1, "1", None):
            # still include status 1 only for "listed"
            if a.get("status") != 1:
                continue
        candidates.append(a)

    ranked = sorted(
        candidates,
        key=lambda x: (
            isinstance(x.get("soldCount"), (int, float)),
            x.get("soldCount") if isinstance(x.get("soldCount"), (int, float)) else -1,
        ),
        reverse=True,
    )
    # Prefer agents with soldCount; take top N
    top = [a for a in ranked if isinstance(a.get("soldCount"), (int, float))][:limit]
    if len(top) < limit:
        # pad with remaining actives
        ids = {str(a["agentId"]) for a in top}
        for a in ranked:
            if str(a.get("agentId")) in ids:
                continue
            top.append(a)
            if len(top) >= limit:
                break
    return top[:limit]


def fetch_identity(agent_id: str) -> dict:
    data = run_cli("agent", "get-agents", "--agent-ids", agent_id)
    if not data.get("ok"):
        raise RuntimeError(f"get-agents {agent_id}: {data}")
    lst = data.get("data") or []
    if not lst:
        raise RuntimeError(f"get-agents {agent_id}: empty")
    return lst[0]


def fetch_reviews(agent_id: str) -> dict:
    data = run_cli("agent", "feedback-list", "--agent-id", agent_id, "--page-size", "50")
    if not data.get("ok"):
        return {"list": [], "distribution": {}, "total": 0, "error": data.get("error")}
    return data.get("data") or {"list": [], "distribution": {}, "total": 0}


def fetch_services(agent_id: str) -> list:
    try:
        data = run_cli("agent", "service-list", "--agent-id", agent_id)
    except Exception as e:
        return []
    if not data.get("ok"):
        return []
    block = data.get("data")
    if isinstance(block, list) and block:
        # shape: [{agentInfo, list: [...]}, ...] or nested
        if isinstance(block[0], dict) and "list" in block[0]:
            return block[0].get("list") or []
        return block
    if isinstance(block, dict):
        return block.get("list") or []
    return []


def load_or_build_cache(force: bool = False) -> dict:
    if CACHE_PATH.exists() and not force:
        print(f"[cache] loading {CACHE_PATH}", flush=True)
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))

    print("[discover] top ASPs by soldCount…", flush=True)
    top = discover_top_asps(25)
    print(f"[discover] selected {len(top)} agents", flush=True)

    agents = []
    for i, stub in enumerate(top, 1):
        aid = str(stub.get("agentId"))
        print(f"[fetch] {i}/{len(top)} agent {aid}…", flush=True)
        try:
            identity = fetch_identity(aid)
        except Exception as e:
            print(f"  identity fail: {e}", flush=True)
            identity = dict(stub)
            identity["_identity_error"] = str(e)
        try:
            reviews = fetch_reviews(aid)
        except Exception as e:
            reviews = {"list": [], "distribution": {}, "total": 0, "error": str(e)}
        try:
            services = fetch_services(aid)
        except Exception:
            services = []
        # Merge services into agent for vetting endpoint probe
        if services and not identity.get("services"):
            identity["services"] = [
                {"endpoint": s.get("endpoint"), "serviceName": s.get("serviceName"),
                 "serviceType": s.get("serviceType")}
                for s in services
            ]
        elif not identity.get("services") and stub.get("services"):
            identity["services"] = stub["services"]
        # soldCount from search is more reliable if identity omits it
        if identity.get("soldCount") is None and stub.get("soldCount") is not None:
            identity["soldCount"] = stub["soldCount"]
        agents.append({
            "agentId": aid,
            "searchStub": stub,
            "identity": identity,
            "reviews": reviews,
            "services": services,
        })

    cache = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "onchainos agent search/get-agents/feedback-list/service-list",
        "agents": agents,
    }
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[cache] wrote {CACHE_PATH}", flush=True)
    return cache


def build_owner_map(agents: list[dict]) -> dict[str, list[str]]:
    m: dict[str, list[str]] = defaultdict(list)
    for a in agents:
        ident = a.get("identity") or {}
        owner = (ident.get("ownerAddress") or "").lower()
        aid = str(ident.get("agentId") or a.get("agentId"))
        if owner:
            if aid not in m[owner]:
                m[owner].append(aid)
    return dict(m)


async def probe_endpoint_detail(endpoint: str) -> dict:
    """Extra endpoint classification for summary (beyond vet_agent signals)."""
    import httpx

    detail = {
        "endpoint": endpoint,
        "get_status": None,
        "post_status": None,
        "error": None,
        "payment_required_header": None,
        "schemes": [],
        "failure_types": [],
    }
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        try:
            r = await client.get(endpoint)
            detail["get_status"] = r.status_code
            pr = r.headers.get("payment-required") or r.headers.get("PAYMENT-REQUIRED")
            detail["payment_required_header"] = "present" if pr else "absent"
            if pr:
                try:
                    # may be base64
                    import base64
                    raw = pr
                    try:
                        decoded = base64.b64decode(raw + "==")
                        payload = json.loads(decoded)
                    except Exception:
                        payload = json.loads(raw)
                    accepts = payload.get("accepts") or []
                    detail["schemes"] = [a.get("scheme") for a in accepts if isinstance(a, dict)]
                except Exception:
                    detail["payment_required_header"] = "present_unparseable"
            if r.status_code == 405:
                detail["failure_types"].append("405_on_GET")
            elif r.status_code == 404:
                detail["failure_types"].append("404_on_GET")
            elif r.status_code not in (200, 402):
                detail["failure_types"].append(f"GET_status_{r.status_code}")
            if r.status_code == 402 and not pr:
                detail["failure_types"].append("empty_or_missing_PAYMENT_REQUIRED_header")
            if detail["schemes"] == ["exact"]:
                detail["failure_types"].append("only_exact_scheme_advertised")
            if detail["schemes"] and "aggr_deferred" not in detail["schemes"] and "exact" in detail["schemes"]:
                if "only_exact_scheme_advertised" not in detail["failure_types"]:
                    detail["failure_types"].append("only_exact_scheme_advertised")
        except Exception as e:
            detail["error"] = str(e)[:200]
            detail["failure_types"].append("GET_connection_error")

        try:
            r2 = await client.post(endpoint, json={}, headers={"Content-Type": "application/json"})
            detail["post_status"] = r2.status_code
            pr2 = r2.headers.get("payment-required") or r2.headers.get("PAYMENT-REQUIRED")
            if r2.status_code == 402 and pr2 and not detail["schemes"]:
                try:
                    import base64
                    try:
                        payload = json.loads(base64.b64decode(pr2 + "=="))
                    except Exception:
                        payload = json.loads(pr2)
                    accepts = payload.get("accepts") or []
                    detail["schemes"] = [a.get("scheme") for a in accepts if isinstance(a, dict)]
                    detail["payment_required_header"] = "present_on_POST"
                    if detail["schemes"] == ["exact"] or (
                        detail["schemes"] and set(detail["schemes"]) == {"exact"}
                    ):
                        if "only_exact_scheme_advertised" not in detail["failure_types"]:
                            detail["failure_types"].append("only_exact_scheme_advertised")
                except Exception:
                    pass
            if r2.status_code == 405:
                detail["failure_types"].append("405_on_POST")
            if r2.status_code == 402 and not pr2:
                detail["failure_types"].append("POST_402_missing_PAYMENT_REQUIRED")
        except Exception as e:
            if "POST" not in (detail.get("error") or ""):
                detail["post_error"] = str(e)[:120]

    # classify dead
    if detail["get_status"] is None and detail.get("error"):
        detail["failure_types"].append("endpoint_unreachable")
    return detail


def listing_age_days(created_at) -> float | None:
    if not created_at:
        return None
    try:
        now_ms = datetime.now(timezone.utc).timestamp() * 1000
        return (now_ms - float(created_at)) / 86_400_000
    except Exception:
        return None


async def run_vetting(cache: dict) -> list[dict]:
    agents = cache["agents"]
    owner_map = build_owner_map(agents)
    print(f"[owners] unique owners={len(owner_map)} multi-listing={sum(1 for v in owner_map.values() if len(v)>1)}", flush=True)

    # RPC smoke test
    import httpx
    rpc_ok = False
    rpc_err = None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                XLAYER_RPC,
                json={"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1},
            )
            r.raise_for_status()
            if r.json().get("result"):
                rpc_ok = True
                print(f"[rpc] OK {XLAYER_RPC} block={r.json()['result']}", flush=True)
    except Exception as e:
        rpc_err = str(e)
        print(f"[rpc] FAIL {XLAYER_RPC}: {e}", flush=True)

    rows = []
    for i, a in enumerate(agents, 1):
        ident = a["identity"]
        reviews = a["reviews"]
        aid = str(ident.get("agentId") or a["agentId"])
        name = ident.get("name") or (a.get("searchStub") or {}).get("name") or "?"
        owner = ident.get("ownerAddress") or ""
        sold = ident.get("soldCount")
        if sold is None:
            sold = (a.get("searchStub") or {}).get("soldCount")

        print(f"[vet] {i}/{len(agents)} #{aid}…", flush=True)
        report = await vet_agent(
            ident,
            reviews,
            XLAYER_RPC if rpc_ok else "",
            known_owner_listings=owner_map,
        )

        # coverage: which signals present
        sig_by_name = {s.name: s for s in report.signals}
        coverage_notes = []
        if not rpc_ok:
            coverage_notes.append(f"wallet_rpc_skipped: {rpc_err}")
        if not owner:
            coverage_notes.append("no_ownerAddress")
        if not (reviews.get("list") or reviews.get("total")):
            coverage_notes.append("no_reviews_block_or_empty")
        # sybil only if reviewers + rpc
        reviewers = [r.get("reviewerAddress") for r in (reviews.get("list") or []) if r.get("reviewerAddress")]
        if not reviewers:
            coverage_notes.append("sybil_review_pattern_skipped_no_reviewers")
        elif not rpc_ok:
            coverage_notes.append("sybil_review_pattern_skipped_no_rpc")

        # endpoint detail
        endpoint = None
        for s in ident.get("services") or a.get("services") or []:
            if isinstance(s, dict) and s.get("endpoint"):
                endpoint = s["endpoint"]
                break
        ep_detail = None
        if endpoint:
            ep_detail = await probe_endpoint_detail(endpoint)
        else:
            coverage_notes.append("endpoint_probe_skipped_no_endpoint")

        # wallet forensics executed?
        wallet_signals_ran = any(
            n in sig_by_name for n in ("zero_prior_payouts", "wallet_age_under_7d", "owner_wallet_young")
        )
        if rpc_ok and owner and not wallet_signals_ran:
            coverage_notes.append("wallet_forensics_missing_unexpected")
        if not (rpc_ok and owner):
            coverage_notes.append("wallet_forensics_not_run")

        def _sig_status(s) -> str:
            if not getattr(s, "evaluated", True):
                return "not_evaluated"
            return "triggered" if s.triggered else "clean"

        row = {
            "agent_id": aid,
            "name": name,
            "score": report.trust_score,
            "soldCount": sold if sold is not None else "",
            "listing_age_days": listing_age_days(ident.get("createdAt")),
            "owner_wallet": owner,
            "summary": report.summary,
            "signals": {s.name: _sig_status(s) for s in report.signals},
            "signal_evidence": {
                s.name: (s.evidence[0].claim if s.evidence else "") for s in report.signals
            },
            "coverage_notes": coverage_notes,
            "endpoint": endpoint,
            "endpoint_detail": ep_detail,
            "owner_listing_count": len(owner_map.get(owner.lower(), [])) if owner else 0,
            "report": report.to_dict(),
        }
        rows.append(row)

    return rows, {
        "rpc_ok": rpc_ok,
        "rpc_err": rpc_err,
        "rpc_url": XLAYER_RPC,
        "owner_map_size": len(owner_map),
        "multi_owner_count": sum(1 for v in owner_map.values() if len(v) > 1),
    }


def write_csv(rows: list[dict]) -> None:
    # all signal names observed
    all_sigs = []
    for n in CANONICAL_SIGNALS:
        if n not in all_sigs:
            all_sigs.append(n)
    for r in rows:
        for n in r["signals"]:
            if n not in all_sigs:
                all_sigs.append(n)

    fieldnames = [
        "agent_id", "name", "score", "soldCount", "listing_age_days", "owner_wallet",
    ] + [f"sig_{n}" for n in all_sigs] + [
        "owner_listing_count", "endpoint", "endpoint_failure_types", "coverage_notes",
    ]

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            row = {
                "agent_id": r["agent_id"],
                "name": r["name"],
                "score": r["score"] if r["score"] is not None else "",
                "soldCount": r["soldCount"],
                "listing_age_days": (
                    f"{r['listing_age_days']:.2f}" if r["listing_age_days"] is not None else ""
                ),
                "owner_wallet": r["owner_wallet"],
                "owner_listing_count": r["owner_listing_count"],
                "endpoint": r.get("endpoint") or "",
                "endpoint_failure_types": ",".join(
                    (r.get("endpoint_detail") or {}).get("failure_types") or []
                ),
                "coverage_notes": ";".join(r.get("coverage_notes") or []),
            }
            for n in all_sigs:
                # Prefer engine status; default not_evaluated if absent
                row[f"sig_{n}"] = r["signals"].get(n, "not_evaluated")
            w.writerow(row)
    print(f"[out] {OUT_CSV}", flush=True)


def write_summary(rows: list[dict], meta: dict) -> None:
    n = len(rows)
    scores = [r["score"] for r in rows if r["score"] is not None]
    bands = Counter()
    for s in scores:
        if s >= 80:
            bands["low risk"] += 1
        elif s >= 55:
            bands["moderate risk"] += 1
        elif s >= 30:
            bands["elevated risk"] += 1
        else:
            bands["high risk"] += 1

    # signal frequency among agents where signal was evaluated
    sig_trig = Counter()
    sig_eval = Counter()
    sig_not_eval = Counter()
    for r in rows:
        for name, state in r["signals"].items():
            if state == "not_evaluated":
                sig_not_eval[name] += 1
                continue
            sig_eval[name] += 1
            if state == "triggered":
                sig_trig[name] += 1

    # review inconsistency
    n_review_inconsistent = sum(
        1 for r in rows if r["signals"].get("review_data_inconsistent") == "triggered"
    )

    # endpoint failures
    ep_agents = [r for r in rows if r.get("endpoint")]
    n_ep = len(ep_agents)
    n_dead_or_wrong = 0
    failure_type_counter = Counter()
    for r in ep_agents:
        det = r.get("endpoint_detail") or {}
        ftypes = det.get("failure_types") or []
        wrong = r["signals"].get("endpoint_wrong_status") == "triggered"
        dead = r["signals"].get("endpoint_dead") == "triggered"
        if wrong or dead or ftypes:
            n_dead_or_wrong += 1
        for ft in ftypes:
            failure_type_counter[ft] += 1
        if dead and "endpoint_dead_signal" not in failure_type_counter:
            failure_type_counter["endpoint_dead_signal"] += 1 if dead else 0
        if dead:
            failure_type_counter["vet_signal_endpoint_dead"] += 1
        if wrong:
            failure_type_counter["vet_signal_endpoint_wrong_status"] += 1

    # wallet reuse
    n_reused = sum(
        1 for r in rows if r["signals"].get("wallet_reused_across_listings") == "triggered"
    )
    n_reuse_eval = sum(
        1 for r in rows if "wallet_reused_across_listings" in r["signals"]
    )

    # top 5 by score (names allowed)
    scored = [r for r in rows if r["score"] is not None]
    top5 = sorted(scored, key=lambda r: (-r["score"], -(r["soldCount"] or 0)))[:5]

    # coverage honesty for wallet signals
    n_wallet_ran = sum(
        1 for r in rows
        if "wallet_forensics_not_run" not in (r.get("coverage_notes") or [])
        and any(k in r["signals"] for k in ("zero_prior_payouts", "wallet_age_under_7d"))
    )
    n_sybil_ran = sum(1 for r in rows if "sybil_review_pattern" in r["signals"])

    lines = []
    lines.append("# State of the Agent Economy — Marketplace Sweep Summary")
    lines.append("")
    lines.append(f"_Generated: {datetime.now(timezone.utc).isoformat()}_  ")
    lines.append(f"_Source cache: `{CACHE_PATH.name}`_  ")
    lines.append(f"_Raw table: `{OUT_CSV.name}` (all aggregates traceable row-by-row)_  ")
    lines.append(f"_RPC: `{meta['rpc_url']}` — **{'reachable' if meta['rpc_ok'] else 'UNREACHABLE: ' + str(meta.get('rpc_err'))}**_")
    lines.append("")
    lines.append("## Sample")
    lines.append("")
    lines.append(f"- **n agents swept:** {n}")
    lines.append(f"- Selection: top listed marketplace agents by `soldCount` (desc) via `onchainos agent search` merge + identity hydrate.")
    lines.append("")
    lines.append("## Trust score distribution")
    lines.append("")
    if scores:
        lines.append(f"- **min / median / max:** {min(scores)} / {statistics.median(scores):.1f} / {max(scores)}")
        lines.append(f"- **mean:** {statistics.mean(scores):.1f}")
        lines.append(f"- Scored agents: {len(scores)} of {n}" + (
            f" ({n - len(scores)} unscored — see coverage)" if len(scores) < n else ""
        ))
    else:
        lines.append("- No scores computed.")
    lines.append("")
    lines.append("| Risk band | Count | % of scored |")
    lines.append("| --- | ---: | ---: |")
    for band in ("low risk", "moderate risk", "elevated risk", "high risk"):
        c = bands.get(band, 0)
        pct = (100 * c / len(scores)) if scores else 0
        lines.append(f"| {band} | {c} | {pct:.0f}% |")
    lines.append("")
    lines.append("Bands: low ≥80 · moderate ≥55 · elevated ≥30 · high <30 (matches `app.scoring.score_band`).")
    lines.append("")
    lines.append("## Signal frequency (ranked)")
    lines.append("")
    lines.append("Percent of agents **where the signal was evaluated** that triggered it.")
    lines.append("")
    lines.append("| Rank | Signal | Triggered | Evaluated | % of evaluated |")
    lines.append("| ---: | --- | ---: | ---: | ---: |")
    ranked_sigs = sorted(
        sig_eval.keys(),
        key=lambda n: (sig_trig[n] / max(sig_eval[n], 1), sig_trig[n]),
        reverse=True,
    )
    for i, name in enumerate(ranked_sigs, 1):
        t, e = sig_trig[name], sig_eval[name]
        pct = 100 * t / e if e else 0
        ne = sig_not_eval.get(name, 0)
        ne_note = f" (+{ne} not_evaluated)" if ne else ""
        lines.append(f"| {i} | `{name}` | {t} | {e}{ne_note} | {pct:.0f}% |")
    # signals only not_evaluated
    only_gap = [n for n in sorted(sig_not_eval) if sig_eval.get(n, 0) == 0]
    if only_gap:
        lines.append("")
        lines.append(
            "**Emitted only as not_evaluated (check did not run):** "
            + ", ".join(f"`{n}` ({sig_not_eval[n]}/25)" for n in only_gap)
        )
    missing_canon = [n for n in CANONICAL_SIGNALS if n not in sig_eval and n not in sig_not_eval]
    if missing_canon:
        lines.append("")
        lines.append(
            f"**Never emitted by engine on this run:** {', '.join(f'`{n}`' for n in missing_canon)}."
        )
    lines.append("")
    lines.append("## Review-data inconsistencies")
    lines.append("")
    lines.append(
        f"- **{n_review_inconsistent}** of {sig_eval.get('review_data_inconsistent', n)} agents "
        f"triggered `review_data_inconsistent` "
        f"({100 * n_review_inconsistent / max(sig_eval.get('review_data_inconsistent', 1), 1):.0f}% of evaluated)."
    )
    lines.append("")
    lines.append("## Endpoints (dead / non-spec)")
    lines.append("")
    lines.append(f"- Agents with a probeable endpoint URL: **{n_ep}** of {n}")
    lines.append(f"- Agents with dead or non-spec-compliant endpoint signals and/or probe failures: **{n_dead_or_wrong}** of {n_ep} with endpoints")
    lines.append("")
    lines.append("### Failure-type breakdown (extra live probe; may double-count agents)")
    lines.append("")
    if failure_type_counter:
        lines.append("| Failure type | Agent count |")
        lines.append("| --- | ---: |")
        for ft, c in failure_type_counter.most_common():
            lines.append(f"| `{ft}` | {c} |")
    else:
        lines.append("_No endpoint failure types recorded._")
    lines.append("")
    lines.append(
        "Note: `app.vetting` endpoint probe uses **unpaid GET** and flags non-{200,402} as "
        "`endpoint_wrong_status`. Many A2MCP routes only implement **POST** (405 on GET) — "
        "that is counted as wrong-status by the engine even when POST returns a valid 402."
    )
    lines.append("")
    lines.append("## Owner wallet reuse across listings")
    lines.append("")
    lines.append(
        f"- **{n_reused}** of {n_reuse_eval} agents triggered `wallet_reused_across_listings` "
        f"(map built from this sweep’s {meta['owner_map_size']} unique owner addresses; "
        f"{meta['multi_owner_count']} owners appear on ≥2 listings in-sample)."
    )
    lines.append("")
    lines.append("## Coverage honesty")
    lines.append("")
    lines.append(f"- Wallet forensics (RPC nonce / age gap signals) ran for **{n_wallet_ran}** of {n} agents.")
    if not meta["rpc_ok"]:
        lines.append(f"- RPC was down: wallet signals skipped for all agents ({meta.get('rpc_err')}).")
    lines.append(
        f"- Coordinated review timing (`sybil_review_pattern`) evaluated for "
        f"**{n_sybil_ran}** of {n} (requires reviewer addresses + RPC)."
    )
    lines.append(f"- `wallet_reused_across_listings` evaluated for **{n_reuse_eval}** of {n} (requires ownerAddress).")
    lines.append(f"- Endpoint probe evaluated for **{n_ep}** of {n} (requires services[].endpoint).")
    # agents with skipped wallet
    skipped_wallet = [
        r["agent_id"] for r in rows
        if "wallet_forensics_not_run" in (r.get("coverage_notes") or [])
    ]
    if skipped_wallet:
        lines.append(
            f"- Wallet forensics **skipped** for agent_ids: {', '.join(skipped_wallet)} "
            f"(no ownerAddress and/or RPC unavailable) — scores for these rows omit RPC-backed penalties."
        )
    lines.append("")
    lines.append("## Highest-scoring agents (top 5 by name)")
    lines.append("")
    lines.append("| Rank | Name | Score | soldCount |")
    lines.append("| ---: | --- | ---: | ---: |")
    for i, r in enumerate(top5, 1):
        lines.append(
            f"| {i} | **{r['name']}** | {r['score']} | {r['soldCount'] if r['soldCount'] != '' else '—'} |"
        )
    lines.append("")
    lines.append("_Low-scoring agents are intentionally not named in this public summary._")
    lines.append("")
    lines.append("## Surprises / notes")
    lines.append("")
    surprises = []
    if scores and statistics.median(scores) >= 70:
        surprises.append(
            f"Median trust score is relatively high ({statistics.median(scores):.0f}) — "
            "many top-sold agents look clean on listing-meta + review signals alone."
        )
    if n_ep and failure_type_counter.get("405_on_GET", 0) >= max(3, n_ep // 3):
        surprises.append(
            f"{failure_type_counter.get('405_on_GET', 0)} endpoints return **405 on GET** — "
            "common for POST-only x402 routes; the stock Vouch probe under-counts healthy POST-402 rails."
        )
    if n_reused:
        surprises.append(
            f"Owner-wallet reuse appeared {n_reused} times inside this top-sold sample — "
            "worth watching for multi-identity operators."
        )
    if n_review_inconsistent:
        surprises.append(
            f"{n_review_inconsistent} agents show review distribution/list/total mismatches — "
            "marketplace review API shape is still noisy."
        )
    only_exact = failure_type_counter.get("only_exact_scheme_advertised", 0)
    if only_exact:
        surprises.append(
            f"{only_exact} endpoints advertise **only `exact`** (no `aggr_deferred`) when a 402 body was parseable — "
            "agentic-wallet buyers may need the exact path."
        )
    # soldCount concentration
    solds = [r["soldCount"] for r in rows if isinstance(r["soldCount"], (int, float))]
    if solds and max(solds) > 10 * (statistics.median(solds) or 1):
        surprises.append(
            f"Sales are concentrated: max soldCount={max(solds)} vs median={statistics.median(solds):.0f} in this cohort."
        )
    if not surprises:
        surprises.append("No major anomalies beyond coverage limits noted above.")
    for s in surprises:
        lines.append(f"- {s}")
    lines.append("")
    lines.append("---")
    lines.append("_Read-only local sweep. No marketplace state changed._")

    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"[out] {OUT_MD}", flush=True)


async def main():
    force = "--refresh" in sys.argv
    cache = load_or_build_cache(force=force)
    rows, meta = await run_vetting(cache)
    # persist scored results into cache sidecar
    scored_path = ROOT / "data" / "marketplace_sweep_scored.json"
    scored_path.write_text(
        json.dumps({"meta": meta, "rows": [
            {k: v for k, v in r.items() if k != "report"} | {"report": r["report"]}
            for r in rows
        ]}, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    write_csv(rows)
    write_summary(rows, meta)
    print("[done]", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
