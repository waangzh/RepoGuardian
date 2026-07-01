from typing import Any

from app.graph.nodes._events import append_step
from app.graph.state import ReviewState
from app.tools.diff_parser import DiffParser


async def diff_parse_node(state: ReviewState) -> ReviewState:
    parser: Any = state.get("_diff_parser") or DiffParser()
    changed_files = parser.parse(state.get("diff_text") or "")
    changed_files_dicts = [file.model_dump(mode="json") for file in changed_files]
    return ReviewState(
        changed_files=changed_files_dicts,
        step_progress=append_step(
            state,
            "diff_parse",
            "completed",
            f"解析到 {len(changed_files)} 个变更文件",
        ),
    )
