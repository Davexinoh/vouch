"""Data models. Every claim carries its source. No exceptions."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Severity(str, Enum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"
    info = "info"


@dataclass
class Evidence:
    """One verifiable fact. If it has no source, it does not go in a report."""
    claim: str
    source_type: str          # "tx_hash" | "block" | "repo" | "agent_id" | "timestamp" | "gap" | "live_probe" | "rpc"
    source_ref: str           # the actual hash, url, block number, id
    fetched_at: str           # ISO string, never datetime.now() inline

    def to_dict(self) -> dict:
        return {
            "claim": self.claim,
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "fetched_at": self.fetched_at,
        }


@dataclass
class RiskSignal:
    """A deterministic flag derived from evidence. Score comes from these, not the LLM.

    `evaluated` is critical for honesty:
    - evaluated=True, triggered=False  → clean pass (check ran, no risk found)
    - evaluated=True, triggered=True   → risk found
    - evaluated=False                  → not checked (missing data / capability gap)
      Must NEVER look like a clean pass; score ignores it.
    """
    name: str
    triggered: bool
    weight: int               # contribution to risk score when triggered AND evaluated
    severity: Severity
    evidence: list[Evidence] = field(default_factory=list)
    evaluated: bool = True

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "triggered": self.triggered if self.evaluated else False,
            "evaluated": self.evaluated,
            "weight": self.weight if self.evaluated else 0,
            "severity": self.severity.value,
            "evidence": [e.to_dict() for e in self.evidence],
            "status": (
                "triggered" if self.evaluated and self.triggered
                else "clean" if self.evaluated
                else "not_evaluated"
            ),
        }


@dataclass
class VetReport:
    agent_id: str
    trust_score: Optional[int]  # 0-100, or None when there was no real data to score
    signals: list[RiskSignal]
    summary: str              # prose wrapping facts; invents nothing
    fetched_at: str

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "trust_score": self.trust_score,
            "signals": [s.to_dict() for s in self.signals],
            "summary": self.summary,
            "fetched_at": self.fetched_at,
        }
