"""带租约的持久化预览；HTTP 断连不取消工作，旧执行者不得发布。"""
import asyncio
import copy
import logging
import threading
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from app.models.orm import PreviewJobOrm, utcnow
from app.models.preview_job import PreviewJobResponse
from app.models.review import ReviewPreviewRequest
from app.tools.git_tool import GitToolError, GitToolTimeout, DiffPreparationError

logger = logging.getLogger("RepoGuardian.Preview")
LEASE_SECONDS = 30


def as_utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


class PreviewLeaseLost(Exception):
    pass


class PreviewJobStore:
    def __init__(self, repository):
        self.repository = repository
        self.sessions = repository._session_factory

    def create(self, identity, request):
        payload = request.model_dump(mode="json")
        try:
            with self.sessions.begin() as session:
                row = session.get(PreviewJobOrm, identity)
                if row:
                    if row.request != payload:
                        raise ValueError("preview_request_id_conflict")
                    return
                session.add(PreviewJobOrm(id=identity, request=payload))
        except IntegrityError:
            # 并发相同幂等键只能重放同一结构化请求。
            with self.sessions() as session:
                row = session.get(PreviewJobOrm, identity)
                if row is None or row.request != payload:
                    raise ValueError("preview_request_id_conflict") from None

    def get(self, identity):
        with self.sessions() as session:
            row = session.get(PreviewJobOrm, identity)
            if row is None:
                return None
            value = {name: getattr(row, name) for name in (
                "id", "request", "status", "stages", "cache_hit", "error",
                "created_at", "updated_at", "completed_at")}
            for name in ("created_at", "updated_at", "completed_at"):
                value[name] = as_utc(value[name])
            preview_id = row.preview_id
        if value["status"] == "completed":
            record = self.repository.get_preview(preview_id)
            if record and record.get("consumed_task_id"):
                value.update(status="consumed", consumed_task_id=record["consumed_task_id"])
            elif not record or as_utc(datetime.fromisoformat(
                    record["response"]["expires_at"])) <= utcnow():
                value.update(status="expired", error="方案已过期，请重新分析 PR。")
            else:
                value["result"] = record["response"]
        return PreviewJobResponse.model_validate(value)

    def claim(self, owner):
        now = utcnow()
        eligible = or_(PreviewJobOrm.status == "queued",
            (PreviewJobOrm.status == "running") & (PreviewJobOrm.lease_until < now))
        with self.sessions.begin() as session:
            identity = session.scalar(select(PreviewJobOrm.id).where(eligible)
                                      .order_by(PreviewJobOrm.created_at).limit(1))
            if not identity:
                return None
            changed = session.execute(update(PreviewJobOrm).where(
                PreviewJobOrm.id == identity, eligible).values(status="running", lease_owner=owner,
                    lease_until=now + timedelta(seconds=LEASE_SECONDS), updated_at=now))
            if changed.rowcount != 1:
                return None
            row = session.get(PreviewJobOrm, identity)
            # 过期 worker 的未完成阶段保留，明确结束在恢复时刻。
            stages = copy.deepcopy(row.stages)
            if stages and not stages[-1].get("finished_at"):
                stages[-1]["finished_at"] = now.isoformat()
                stages.append({"phase": "recovery", "message": "上次执行中断，重新准备预览",
                               "started_at": now.isoformat(), "finished_at": now.isoformat()})
            row.stages = stages
            return identity, ReviewPreviewRequest.model_validate(row.request)

    @staticmethod
    def owned(identity, owner):
        return (PreviewJobOrm.id == identity, PreviewJobOrm.status == "running",
                PreviewJobOrm.lease_owner == owner, PreviewJobOrm.lease_until > utcnow())

    def heartbeat(self, identity, owner):
        with self.sessions.begin() as session:
            changed = session.execute(update(PreviewJobOrm).where(*self.owned(identity, owner))
                .values(lease_until=utcnow() + timedelta(seconds=LEASE_SECONDS)))
            return changed.rowcount == 1

    def progress(self, identity, owner, phase, message, percent=None, operation=None, cache_hit=False):
        with self.sessions.begin() as session:
            row = session.scalar(select(PreviewJobOrm).where(*self.owned(identity, owner)))
            if row is None:
                raise PreviewLeaseLost()
            now = utcnow()
            stages = copy.deepcopy(row.stages)
            if not stages or stages[-1]["phase"] != phase:
                if stages and not stages[-1].get("finished_at"):
                    stages[-1]["finished_at"] = now.isoformat()
                stages.append({"phase": phase, "started_at": now.isoformat()})
                logger.info("预览 %s 阶段 %s：%s", identity[:8], phase, message)
            stages[-1].update(message=message, percent=percent, operation=operation)
            changed = session.execute(update(PreviewJobOrm).where(*self.owned(identity, owner))
                .values(stages=stages, cache_hit=row.cache_hit or cache_hit, updated_at=now)
                .execution_options(synchronize_session=False))
            if changed.rowcount != 1:
                raise PreviewLeaseLost()

    def finish(self, identity, owner, status, *, preview_id=None, error=None):
        with self.sessions.begin() as session:
            now = utcnow()
            row = session.scalar(select(PreviewJobOrm).where(*self.owned(identity, owner)))
            if row is None:
                return False
            stages = copy.deepcopy(row.stages)
            if stages and not stages[-1].get("finished_at"):
                stages[-1]["finished_at"] = now.isoformat()
            changed = session.execute(update(PreviewJobOrm).where(*self.owned(identity, owner)).values(
                status=status, stages=stages, preview_id=preview_id, error=error,
                updated_at=now, completed_at=now if status != "queued" else None,
                lease_owner=None, lease_until=None).execution_options(synchronize_session=False))
            return changed.rowcount == 1

    def cancel(self, identity):
        with self.sessions.begin() as session:
            session.execute(update(PreviewJobOrm).where(PreviewJobOrm.id == identity,
                PreviewJobOrm.status.in_(["queued", "running"])).values(status="cancelled",
                    completed_at=utcnow(), updated_at=utcnow(), lease_owner=None, lease_until=None))


class PreviewJobs:
    def __init__(self, review_service):
        self.service = review_service
        self.store = PreviewJobStore(review_service.repository)
        self.owner = uuid4().hex
        self.task = None
        self.stopping = False

    def start(self):
        if self.task is None or self.task.done():
            self.stopping = False
            self.task = asyncio.create_task(self.run())

    async def close(self):
        self.stopping = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def run(self):
        while not self.stopping:
            try:
                claimed = await asyncio.to_thread(self.store.claim, self.owner)
                if claimed:
                    await self.execute(*claimed)
                else:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("预览 worker 异常：%s", type(exc).__name__)
                await asyncio.sleep(1)

    async def execute(self, identity, request):
        cancel = threading.Event()

        def progress(phase, message, **extra):
            if cancel.is_set():
                raise PreviewLeaseLost()
            self.store.progress(identity, self.owner, phase, message, **extra)

        async def heartbeat():
            while True:
                await asyncio.sleep(1)
                if not await asyncio.to_thread(self.store.heartbeat, identity, self.owner):
                    cancel.set()
                    raise PreviewLeaseLost()

        async def prepare():
            async with asyncio.timeout(1800):
                return await self.service.preview(request, progress=progress, cancel_event=cancel)

        runner = asyncio.create_task(prepare())
        lease = asyncio.create_task(heartbeat())
        try:
            done, _ = await asyncio.wait((runner, lease), return_when=asyncio.FIRST_COMPLETED)
            if lease in done:
                await lease
            result = await runner
            await asyncio.to_thread(self.store.finish, identity, self.owner, "completed", preview_id=result.preview_id)
        except asyncio.CancelledError:
            cancel.set()
            await asyncio.to_thread(self.store.finish, identity, self.owner, "queued")
            raise
        except PreviewLeaseLost:
            cancel.set()
        except Exception as exc:
            logger.error("预览 %s 失败：%s", identity[:8], type(exc).__name__)
            # 不把可能含凭据、仓库内容或本地路径的第三方错误传给页面。
            label = "预览准备失败，请重试；如持续失败，请检查服务端日志。"
            from app.tools.github.github_tool import GitHubToolError
            if isinstance(exc, GitHubToolError):
                label = str(exc)
            elif isinstance(exc, (TimeoutError, httpx.TimeoutException, GitToolTimeout)):
                label = "预览准备超时，请检查网络连接后重试。"
            elif isinstance(exc, DiffPreparationError):
                label = "PR 的 Base 和 Head 无法确定唯一比较基准，请检查 PR 快照。"
            elif isinstance(exc, GitToolError):
                label = "仓库准备失败，请检查 GitHub 网络连接、Git 安装与仓库访问权限。"
            elif isinstance(exc, httpx.HTTPStatusError):
                label = {401: "GitHub 身份验证失败，请检查服务端凭据。",
                         403: "GitHub 拒绝访问，请检查仓库权限和 API 配额。",
                         404: "GitHub PR 不存在或当前身份无权访问。",
                         429: "GitHub 请求过于频繁，请稍后重试。"}.get(
                             exc.response.status_code, "获取 GitHub PR 信息失败，请稍后重试。")
            await asyncio.to_thread(self.store.finish, identity, self.owner, "failed", error=label)
        finally:
            cancel.set()
            runner.cancel()
            lease.cancel()
            await asyncio.gather(runner, lease, return_exceptions=True)
