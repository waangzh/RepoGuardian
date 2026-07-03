"""LangGraph SQLite 检查点持久化。

AsyncSqliteSaver 允许 StateGraph 在执行中断后从检查点恢复。
当前版本未在管道中使用（graph.compile() 不带 checkpointer），
保留此模块供将来支持断点续跑。
"""

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.core.config import settings

_checkpointer: AsyncSqliteSaver | None = None


async def get_checkpointer() -> AsyncSqliteSaver:
    """获取全局单例 AsyncSqliteSaver，延迟初始化。"""
    global _checkpointer
    if _checkpointer is None:
        conn_string = f"sqlite+aiosqlite:///{settings.repoguardian_checkpoint_db}"
        _checkpointer = AsyncSqliteSaver.from_conn_string(conn_string)
        await _checkpointer.setup()
    return _checkpointer
