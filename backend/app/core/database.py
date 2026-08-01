"""SQLAlchemy 数据库基础设施。

表结构只通过 Alembic migration 管理；应用启动不会调用 ``create_all``。
同步会话用于保持现有服务层 API 兼容，异步会话供 API 和 worker 扩展使用。
"""

from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

Path(settings.repoguardian_db_path).parent.mkdir(parents=True, exist_ok=True)
_sqlite_path = Path(settings.repoguardian_db_path).resolve().as_posix()
ASYNC_DATABASE_URL = f"sqlite+aiosqlite:///{_sqlite_path}"
SYNC_DATABASE_URL = f"sqlite:///{_sqlite_path}"

engine = create_async_engine(ASYNC_DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

sync_engine = create_engine(
    SYNC_DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False, "timeout": 30},
)
sync_session = sessionmaker(sync_engine, class_=Session, expire_on_commit=False)


@event.listens_for(sync_engine, "connect")
def _configure_sqlite(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


class Base(DeclarativeBase):
    """ORM 声明式基类。"""


def schema_is_current() -> bool:
    """只读检查数据库是否已由 Alembic 初始化。"""
    if "alembic_version" not in inspect(sync_engine).get_table_names():
        return False
    with sync_engine.connect() as connection:
        version = connection.scalar(text("SELECT version_num FROM alembic_version"))
    return version == "20260801_0001"


async def init_db() -> None:
    """兼容旧入口：仅验证 migration，不自动建表。"""
    if not schema_is_current():
        raise RuntimeError("database is not migrated; run `alembic upgrade head`")
