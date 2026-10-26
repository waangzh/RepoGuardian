"""Hunk 身份计算；规划器、目标派生与只读工具共用同一实现。"""

from app.tools.diff_parser import stable_hunk_id


def hunk_identity(file_path: str, index: int, hunk: dict) -> str:
    """优先使用已有 hunk_id；否则按路径、双侧范围与内容重新计算。"""
    del index  # 兼容旧调用签名；稳定 ID 不依赖 hunk 在文件中的序号。
    if hunk.get("hunk_id"):
        return str(hunk["hunk_id"])
    return stable_hunk_id(
        file_path,
        int(hunk.get("old_start", 0)),
        int(hunk.get("old_length", 0)),
        int(hunk.get("new_start", 0)),
        int(hunk.get("new_length", 0)),
        list(hunk.get("lines") or []),
    )
