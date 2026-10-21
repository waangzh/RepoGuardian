"""任务生命周期写入栅栏；先取得写锁，再检查关联状态。"""

from sqlalchemy import update

from app.models.orm import ReviewTaskOrm


def lock_live_review(session, task_id) -> bool:
    # SQLite 的首次写入串行化删除、入队与快照写回，避免先读后写升级竞争。
    result = session.execute(update(ReviewTaskOrm).where(
        ReviewTaskOrm.id == task_id, ReviewTaskOrm.deleted_at.is_(None),
    ).values(updated_at=ReviewTaskOrm.updated_at).execution_options(synchronize_session=False))
    return result.rowcount == 1
