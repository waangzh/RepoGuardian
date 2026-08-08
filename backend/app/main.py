import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.reviews import get_review_service, router as reviews_router
from app.api.validation_requests import router as validation_requests_router
from app.api.project_ci import router as project_ci_router
from app.api.validation_backends import router as validation_backends_router
from app.api.system import router as system_router
from app.graph.checkpointer import close_checkpointer
from app.services.maintenance_service import MaintenanceService
from app.validation.project_ci import get_project_ci_service


@asynccontextmanager
async def lifespan(_app: FastAPI):
    service = get_review_service()
    maintenance = MaintenanceService(
        service._repository,
        active_workspace_paths=lambda: tuple(service._repo_paths.values()),
    )
    await maintenance.run_once()
    maintenance_task = asyncio.create_task(maintenance.run_forever())
    service._ensure_worker_started()
    project_ci = get_project_ci_service()
    if project_ci is not None:
        project_ci.start_pending_polling()
    try:
        yield
    finally:
        maintenance.stop()
        await maintenance_task
        if project_ci is not None:
            await project_ci.close()
        if service._worker:
            service._worker.stop()
        if service._worker_task:
            await service._worker_task
        await close_checkpointer()

# ---------------------------------------------------------------------------
# 统一日志配置：所有模块共享同一 Logger，控制台输出中文流程日志
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
# 抑制第三方库的 DEBUG / INFO 噪音（httpx、tree_sitter 等）
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("tree_sitter").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)


app = FastAPI(title="RepoGuardian API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(reviews_router, prefix="/api")
app.include_router(validation_requests_router, prefix="/api")
app.include_router(project_ci_router, prefix="/api")
app.include_router(validation_backends_router, prefix="/api")
app.include_router(system_router, prefix="/api")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
