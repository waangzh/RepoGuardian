"""Trusted dependencies are injected at invocation time, never model arguments."""

from dataclasses import dataclass, field

from app.review.evidence import EvidenceService


@dataclass
class AgentContext:
    evidence: EvidenceService
    changed_files: list[dict] = field(default_factory=list)
    symbols: list[dict] = field(default_factory=list)
    repository_graph: dict = field(default_factory=dict)
    discussions: list[dict] = field(default_factory=list)
