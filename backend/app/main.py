import logging
import sys

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.reviews import router as reviews_router

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


app = FastAPI(title="RepoGuardian API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(reviews_router, prefix="/api")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}

