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
    source_type: str          # "tx_hash" | "block" | "repo" | "agent_id" | "timestamp"
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
    """A deterministic flag derived from evidence. Score comes from these, not the LLM."""
    name: str
    triggered: bool
    weight: int               # contribution to risk score when triggered
    severity: Severity
    evidence: list[Evidence] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "triggered": self.triggered,
            "weight": self.weight,
            "severity": self.severity.value,
            "evidence": [e.to_dict() for e in self.evidence],
        }


@dataclass
class VetReport:
    agent_id: str
    trust_score: int          # 0-100, computed from signals
    signals: list[RiskSignal]
    summary: str              # LLM prose, wraps facts, invents nothing
    fetched_at: str

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "trust_score": self.trust_score,
            "signals": [s.to_dict() for s in self.signals],
            "summary": self.summary,
            "fetched_at": self.fetched_at,
        }
