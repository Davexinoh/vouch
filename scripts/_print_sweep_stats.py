import csv
from collections import Counter
from pathlib import Path

rows = list(csv.DictReader(Path("data/sweep_raw.csv").open(encoding="utf-8")))
scores = [int(r["score"]) for r in rows if r["score"]]
print(
    "SCORES min/med/max",
    min(scores),
    sorted(scores)[len(scores) // 2],
    max(scores),
    "mean",
    round(sum(scores) / len(scores), 1),
)
print(
    "bands low",
    sum(1 for s in scores if s >= 80),
    "mod",
    sum(1 for s in scores if 55 <= s < 80),
)
c = Counter(r["sig_sybil_review_pattern"] for r in rows)
ev = c.get("triggered", 0) + c.get("clean", 0)
rate = 100 * c.get("triggered", 0) / max(ev, 1)
print("sybil", dict(c), "trigger_rate", f"{c.get('triggered', 0)}/{ev} = {rate:.0f}%")
print("endpoint_wrong", dict(Counter(r["sig_endpoint_wrong_status"] for r in rows)))
print("endpoint_dead", dict(Counter(r["sig_endpoint_dead"] for r in rows)))
print("wallet_age", dict(Counter(r["sig_wallet_age_under_7d"] for r in rows)))
print(
    "top5",
    [(r["name"], r["score"]) for r in sorted(rows, key=lambda x: -int(x["score"] or 0))[:5]],
)
