"""SQLAlchemy 异步引擎与 ORM 基类。

当前版本未在审查管道中使用（任务存内存），
保留此模块供将来多用户持久化。
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

# 异步 SQLite 引擎（不含查询日志噪音）
engine = create_async_engine(
    f"sqlite+aiosqlite:///{settings.repoguardian_db_path}",
    echo=False,
)
# 会话工厂，每个请求一个 AsyncSession
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    """ORM 声明式基类。"""
    pass


async def init_db() -> None:
    """创建所有 ORM 表（幂等，数据库不存在时自动创建）。"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
