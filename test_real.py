"""Test the vetting pipeline on the REAL agents Davexinoh pulled from OKX.AI.
No RPC (offline test) — meta + review forensics only, which is enough to prove
the engine works on live data.
"""
import asyncio
from app.vetting import vet_agent

# Real data from the live read (2026-07-10)
coinwm = {
    "agentId": "3118", "name": "CoinWM Open API",
    "ownerAddress": "0x5892fe5d4e0", "agentWalletAddress": "0x5892fdcf4e0",
    "createdAt": 1782964805226, "lastOnlineTime": 1783698899754,
    "onlineStatus": 1, "securityRate": "5.0", "soldCount": 1557,
    "services": [{"endpoint": "https://api.coinwm.com/twitter/profile"}],
}
coinwm_reviews = {"distribution": {"1":0,"2":0,"3":0,"4":0,"5":1},
                  "list": [{"reviewerAddress":"0xc385e2df2aa27a3fbe809e0faf7c5c357b716c63"}],
                  "total": 1}

coinank = {
    "agentId": "2013", "name": "CoinAnk OpenAPI",
    "ownerAddress": "0xb5538ba5409d3b6286d7cc1df8d7c2556e0cba3c",
    "createdAt": 1781779522040, "lastOnlineTime": None,
    "onlineStatus": 1, "securityRate": "", "soldCount": 1373,
    "services": [{"endpoint": "https://open-api.coinank.com/v1/kline"}],
}
# note the inconsistency: distribution says 1, list empty, total 0
coinank_reviews = {"distribution": {"1":1,"2":0,"3":0,"4":0,"5":0},
                   "list": [], "total": 0}

async def main():
    for agent, rev in [(coinwm, coinwm_reviews), (coinank, coinank_reviews)]:
        report = await vet_agent(agent, rev, rpc_url="")  # no RPC offline
        print(f"\n=== {agent['name']} (#{agent['agentId']}) ===")
        print(f"TRUST SCORE: {report.trust_score}/100 — {report.summary}")
        for s in report.signals:
            if s.triggered:
                ev = s.evidence[0].claim if s.evidence else ""
                print(f"  [TRIGGERED] {s.name} (-{s.weight}): {ev}")

asyncio.run(main())
