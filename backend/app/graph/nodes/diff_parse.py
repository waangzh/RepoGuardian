from datetime import datetime, timezone
from typing import Any

from app.graph.state import ReviewState
from app.tools.diff_parser import DiffParser


async def diff_parse_node(state: ReviewState) -> ReviewState:
    diff_text = state.get("diff_text", "")
    parser: Any = state.get("_diff_parser") or DiffParser()
    changed_files = parser.parse(diff_text)

    changed_files_dicts = [f.model_dump() for f in changed_files]
    step_progress: list[dict] = list(state.get("step_progress") or [])
    step_progress.append({
        "node": "diff_parse",
        "status": "completed",
        "message": f"解析到 {len(changed_files)} 个变更文件",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return ReviewState(
        changed_files=changed_files_dicts,
        step_progress=step_progress,
    )
