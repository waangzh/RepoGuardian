"""Finite work scheduling inside one existing WorkerJob lease."""

import asyncio

from app.review.budget import BudgetGate
from app.review.domain import utcnow
from app.review.report import build_report, generate_overview
from app.review.store import LeaseLost, ReviewStopped


class ReviewOrchestrator:
    def __init__(self, store, runner):
        self.store, self.runner = store, runner

    async def run(self, run_id, provider, lease):
        run = await asyncio.to_thread(self.store.begin, run_id, lease)
        provider.run = run
        budget = BudgetGate(self.store, run, lease)
        running = {}
        try:
            while True:
                await asyncio.to_thread(self.store.assert_active, run_id, lease)
                work_items = await asyncio.to_thread(self.store.work_items, run_id)
                queued = [item for item in work_items if item.status == "queued"]
                if utcnow() >= run.hard_deadline:
                    for task in running:
                        task.cancel()
                    await asyncio.gather(*running, return_exceptions=True)
                    running.clear()
                    for item in work_items:
                        if item.status in {"queued", "running"}:
                            await asyncio.to_thread(self.store.skip, run_id, item.id, "hard_deadline", lease)
                    break
                for item in list(queued):
                    if item.kind == "review" and utcnow() >= run.review_cutoff:
                        await asyncio.to_thread(self.store.skip, run_id, item.id, "review_cutoff", lease)
                        queued.remove(item)
                priority = {unit.id: index for index, unit in enumerate(run.plan.units)}
                queued.sort(key=lambda item: (item.kind != "verification", priority[item.unit_id]))
                while queued and len(running) < run.configuration.options.limits.concurrency:
                    item = queued.pop(0)
                    reason = await asyncio.to_thread(self.store.can_start, run_id, item)
                    if reason:
                        if not running:
                            await asyncio.to_thread(self.store.skip, run_id, item.id, reason, lease)
                        continue
                    claimed = await asyncio.to_thread(self.store.claim, run_id, item.id, lease)
                    if claimed:
                        task = asyncio.create_task(self.runner.run(claimed, provider, budget))
                        running[task] = claimed
                if not running:
                    break
                done, _ = await asyncio.wait(running, timeout=min(0.2,
                    max(0, (run.hard_deadline - utcnow()).total_seconds())),
                    return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    running.pop(task)
                    task.result()
        except (LeaseLost, ReviewStopped, asyncio.CancelledError):
            raise
        finally:
            for task in running:
                task.cancel()
            await asyncio.gather(*running, return_exceptions=True)
        await asyncio.to_thread(self.store.assert_active, run_id, lease)
        report = await asyncio.to_thread(build_report, run,
            await asyncio.to_thread(self.store.work_items, run_id),
            await asyncio.to_thread(self.store.candidates, run_id),
            await asyncio.to_thread(self.store.usage, run_id))
        await asyncio.to_thread(self.store.save_report, run_id, report, lease)
        report.overview = await generate_overview(report, provider, budget, self.runner.model)
        report.usage = await asyncio.to_thread(self.store.usage, run_id)
        # Content version binds facts and overview; counters do not mutate confirmed text.
        from app.review.planner import identity
        report.version = identity([report.version, report.overview.model_dump()])
        await asyncio.to_thread(self.store.save_report, run_id, report, lease, finished=True)
        return report
