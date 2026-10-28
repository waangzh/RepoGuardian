"""Trusted dependencies are injected at invocation time, never model arguments."""

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.review.evidence import EvidenceService
from app.review.models import ReviewLimits


@dataclass
class AgentContext:
    evidence: EvidenceService
    changed_files: list[dict] = field(default_factory=list)
    denied_paths: frozenset[str] = frozenset()
    symbols: list[dict] = field(default_factory=list)
    repository_graph: dict = field(default_factory=dict)
    discussions: list[dict] = field(default_factory=list)
    author_context: dict = field(default_factory=dict)
    check_active: Callable[[], Awaitable[None]] | None = None
    discussions_enabled: bool = False
    batch_verification: bool = False
    budget: Any = None
    budget_agent_id: str = ""
    discovery_agent_ids: frozenset[str] = frozenset()


@dataclass
class ReviewContext:
    load: Callable[[], Awaitable[AgentContext]]
    model: Any
    limits: ReviewLimits = field(default_factory=ReviewLimits)
    depth: str = "standard"
    focus: tuple[str, ...] = ("correctness", "security", "performance", "compatibility")
    selected_paths: frozenset[str] | None = None
    replaying_nodes: frozenset[str] = frozenset()
    retry_results: dict[tuple[str, str], dict] = field(default_factory=dict)
    budget: Any = None
    check_active: Callable[[], Awaitable[None]] | None = None
    progress: Callable[[dict], Awaitable[None]] | None = None
    _agent_context: AgentContext | None = None
    _load_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def active(self):
        if self.check_active:
            await self.check_active()

    async def sources(self) -> AgentContext:
        await self.active()
        async with self._load_lock:
            if self.budget is None:
                from app.review.budget import ReviewBudget
                self.budget = ReviewBudget(self.limits, active=self.active, depth=self.depth)
            if self._agent_context is None:
                self._agent_context = await self.load()
                self._agent_context.check_active = self.active
        return self._agent_context

    async def emit(self, **event):
        await self.active()
        if self.progress:
            await self.progress(event)
