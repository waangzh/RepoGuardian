"""Static Git and diff types shared by repository tools."""
from app.core.paths import validate_repo_relative_path as _validate_repo_relative_path
from app.models.review.diff import (
    ChangedFile, ChangedLine, DiffHunk, DiffLine, FileChangeEvidence,
    FileChangeSide, PullRequestInfo, PullRequestRef,
)

__all__ = ["ChangedFile", "ChangedLine", "DiffHunk", "DiffLine", "FileChangeEvidence",
           "FileChangeSide", "PullRequestInfo", "PullRequestRef", "_validate_repo_relative_path"]
