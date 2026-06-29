from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.core.config import settings

_checkpointer: AsyncSqliteSaver | None = None


async def get_checkpointer() -> AsyncSqliteSaver:
    global _checkpointer
    if _checkpointer is None:
        conn_string = f"sqlite+aiosqlite:///{settings.repoguardian_checkpoint_db}"
        _checkpointer = AsyncSqliteSaver.from_conn_string(conn_string)
        await _checkpointer.setup()
    return _checkpointer
