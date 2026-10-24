"""候选补丁的 GitHub/报告展示转换。"""

from io import StringIO

from unidiff import PatchSet

from app.models.review import PatchPresentation, PatchProposal, PatchStatus


UNVERIFIED_PATCH_WARNING = "候选修复，尚未运行项目测试。"
VERIFIED_PATCH_NOTICE = "该补丁已通过指定验证后端。"


def build_patch_presentation(patch: PatchProposal) -> PatchPresentation:
    """只有单文件、单 hunk、小范围替换才转换为 GitHub suggestion。"""
    warning = (
        VERIFIED_PATCH_NOTICE
        if patch.status == PatchStatus.verified
        else UNVERIFIED_PATCH_WARNING
    )
    try:
        parsed = PatchSet(StringIO(patch.unified_diff))
    except Exception:
        return PatchPresentation(
            inline_suggestion=None,
            full_diff=patch.unified_diff,
            warning=warning,
        )

    if len(parsed) == 1 and len(parsed[0]) == 1:
        hunk = parsed[0][0]
        added = [line.value.rstrip("\n") for line in hunk if line.is_added]
        deleted = [line for line in hunk if line.is_removed]
        if deleted and added and len(added) + len(deleted) <= 20:
            return PatchPresentation(
                inline_suggestion="```suggestion\n" + "\n".join(added) + "\n```",
                full_diff=None,
                warning=warning,
            )

    return PatchPresentation(
        inline_suggestion=None,
        full_diff=patch.unified_diff,
        warning=warning,
    )
