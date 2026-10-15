"""写者租约丢失信号；task_review_v1 持久化与取消路径共用。"""

import asyncio


class CoordinationLeaseLost(asyncio.CancelledError):
    """取消或写者已被替代；不能转成普通失败后继续发布。"""
