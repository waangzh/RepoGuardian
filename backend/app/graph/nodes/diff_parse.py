import logging
from typing import Any

from app.graph.nodes._events import append_step
from app.graph.state import ReviewState
from app.tools.diff_parser import DiffParser

logger = logging.getLogger("RepoGuardian.Node")


async def diff_parse_node(state: ReviewState) -> ReviewState:
    """解析节点：将 unified diff 文本解析为结构化变更文件列表。"""
    parser: Any = state.get("_diff_parser") or DiffParser()
    diff_text = state.get("diff_text") or ""
    logger.info("📄 [解析] 开始解析 diff（长度: %d 字符）...", len(diff_text))
    changed_files = parser.parse(diff_text)
    changed_files_dicts = [file.model_dump(mode="json") for file in changed_files]
    logger.info("📄 [解析] 解析完成: %d 个变更文件", len(changed_files))
    return ReviewState(
        changed_files=changed_files_dicts,
        step_progress=append_step(
            state,
            "diff_parse",
            "completed",
            f"解析到 {len(changed_files)} 个变更文件",
        ),
    )
