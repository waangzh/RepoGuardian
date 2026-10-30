"""One request gate for discovery, verification, corrections and overview."""

import asyncio
from dataclasses import dataclass

from app.review.domain import utcnow
from app.review.store import BudgetExceeded


@dataclass
class BudgetGate:
    store: object
    run: object
    lease: dict

    async def active(self):
        await asyncio.to_thread(self.store.assert_active, self.run.id, self.lease)
        if utcnow() >= self.run.hard_deadline:
            raise BudgetExceeded("hard_deadline")

    async def reserve(self, work, estimate, *, overview=False):
        return await asyncio.to_thread(self.store.reserve, self.run.id, work,
                                      estimate, self.lease, overview=overview)

    async def settle(self, ticket, usage):
        await asyncio.to_thread(self.store.settle, self.run.id, ticket, usage, self.lease)
